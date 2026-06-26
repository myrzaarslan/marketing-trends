"""Ingestion entrypoint — run_ingestion().

Pulls from all configured adapters/watchlist, upserts posts + snapshots,
downloads thumbnails, ranks to get top-N, and calls enrichment lazily.

Enrichment is imported lazily so this module works even when the enrichment/
package isn't present yet (it's built in parallel). If the import fails, the
enrichment step is silently skipped.

Also includes seed_from_scratch_files() which imports the accumulated scratch
JSON files from data/ to bootstrap the DB without running live scrapers.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure project root on path so core/ resolves correctly when run as a script
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.schema import PostRecord
from core.storage import get_session, init_db, set_thumbnail_path, upsert_post
from core import ranker as _ranker

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = _ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"
THUMBNAIL_DIR = DATA_DIR / "thumbnails"

TOP_N_DEFAULT = 25


# ---------------------------------------------------------------------------
# Thumbnail download (cheap CDN image GET — one per post, keeps cards alive)
# ---------------------------------------------------------------------------


def _download_thumbnail(
    platform: str, post_id: str, url: Optional[str]
) -> Optional[str]:
    """Download thumbnail image from CDN. Returns relative path or None."""
    if not url:
        return None
    try:
        import urllib.request
        dest_dir = THUMBNAIL_DIR / platform
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Derive extension from URL (default .jpg)
        ext = ".jpg"
        for candidate in (".jpg", ".jpeg", ".png", ".webp"):
            if candidate in url.lower().split("?")[0]:
                ext = candidate
                break
        dest = dest_dir / f"{post_id}{ext}"
        if dest.exists():
            return str(dest.relative_to(_ROOT))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            dest.write_bytes(resp.read())
        return str(dest.relative_to(_ROOT))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Lazy enrichment call
# ---------------------------------------------------------------------------


def _media_path_to_rel(path: str) -> str:
    """Strip a leading ``data/media/`` so post_content stores paths relative to it."""
    p = str(path).replace("\\", "/")
    marker = "data/media/"
    i = p.find(marker)
    return p[i + len(marker):] if i != -1 else p


def _call_enrichment(top_ids: list[tuple[str, str]]) -> str:
    """Enrich the top-N for real, bridging enrichment.enrich into core.storage.

    Reads each post's stored ``raw`` from its latest snapshot, downloads the
    Content Bundle, and upserts a ``post_content`` row. Idempotent (skips posts
    already enriched). Paces between posts (jittered) for anti-ban discipline and
    commits per post so partial progress survives an interruption. Non-fatal: any
    single post failure is logged and skipped.
    """
    if not top_ids:
        return "skipped (no ids)"
    try:
        from enrichment.extractor import enrich
    except ModuleNotFoundError:
        print("[ingest] enrichment/ not present yet — skipping enrichment step")
        return "skipped (no module)"
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[ingest] enrichment import failed (non-fatal): {exc}")
        traceback.print_exc()
        return "skipped (import error)"

    import random
    import time as _time

    from core import storage as cs

    session = get_session()

    class _CoreStorageBridge:
        """Adapts core.storage to enrichment's StorageProtocol (commits per write)."""

        def is_in_post_content(self, platform: str, post_id: str) -> bool:
            pc = session.get(cs.PostContent, (platform, post_id))
            return pc is not None and pc.status == "done"

        def get_raw(self, platform: str, post_id: str) -> Optional[dict]:
            snap = (
                session.query(cs.PostSnapshot)
                .filter_by(platform=platform, platform_post_id=post_id)
                .order_by(cs.PostSnapshot.fetched_at.desc())
                .first()
            )
            if not snap or not snap.raw:
                return None
            try:
                return json.loads(snap.raw) or None
            except (json.JSONDecodeError, TypeError):
                return None

        def get_thumbnail_path(self, platform: str, post_id: str) -> Optional[str]:
            post = session.get(cs.Post, (platform, post_id))
            return post.thumbnail_path if post else None

        def set_thumbnail_path(self, platform: str, post_id: str, path: str) -> None:
            cs.set_thumbnail_path(session, platform, post_id, _media_path_to_rel(path))
            session.commit()

        def write_post_content(self, **kwargs) -> None:
            kwargs.pop("extracted_at", None)  # core sets this itself
            media_paths = [_media_path_to_rel(p) for p in (kwargs.pop("media_paths", None) or [])]
            cs.write_post_content(session, media_paths=media_paths, **kwargs)
            session.commit()

    bridge = _CoreStorageBridge()
    enriched = 0
    skipped = 0
    try:
        for plat, pid in top_ids:
            if bridge.is_in_post_content(plat, pid):
                skipped += 1
                continue
            try:
                enrich(
                    [(plat, pid)],
                    raw_reader=bridge.get_raw,
                    storage=bridge,
                    media_root=MEDIA_DIR,
                )
                enriched += 1
            except Exception as exc:
                print(f"[ingest] enrich {plat}:{pid} failed (non-fatal): {exc}")
            _time.sleep(random.uniform(1.0, 2.5))  # gentle anti-ban pacing
    finally:
        session.commit()
        session.close()

    print(f"[ingest] enrichment: {enriched} newly enriched, {skipped} already done")
    return f"enriched {enriched}, skipped {skipped}"


# ---------------------------------------------------------------------------
# Seed from scratch JSON files (bootstrap without live scrapers)
# ---------------------------------------------------------------------------


def _normalize_tiktok_raw(raw: dict, post_id: str) -> PostRecord:
    """Normalize a raw TikTok item_list payload into a PostRecord.

    Mirrors the logic in adapters/tiktok/adapter.py:_record_from_api so we can
    import the accumulator without importing the TikTok adapter (which needs
    Playwright + a browser).
    """
    stats = raw.get("statsV2") or raw.get("stats") or {}
    music = raw.get("music") or {}
    author = raw.get("author") or {}
    author_stats = raw.get("authorStats") or {}
    handle = author.get("uniqueId") or ""
    desc = raw.get("desc")

    create_time = raw.get("createTime")
    posted_at = datetime.fromtimestamp(int(create_time), tz=timezone.utc) if create_time else None

    video = raw.get("video") or {}
    thumbnail_url = (
        video.get("cover")
        or video.get("originCover")
        or video.get("dynamicCover")
    )

    def _int(v) -> Optional[int]:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    hashtags = [
        c.get("title", "") for c in (raw.get("challenges") or []) if c.get("title")
    ]
    if not hashtags and desc:
        import re
        hashtags = re.findall(r"#(\w+)", desc)

    sound_id_raw = music.get("id")
    sound_id = str(sound_id_raw) if sound_id_raw and str(sound_id_raw) != "0" else None

    return PostRecord(
        platform="tiktok",
        platform_post_id=str(post_id),
        account_handle=handle,
        url=f"https://www.tiktok.com/@{handle}/video/{post_id}",
        fetched_at=datetime.now(timezone.utc),
        media_type="video",
        raw=raw,
        posted_at=posted_at,
        caption=desc,
        hashtags=hashtags,
        sound_id=sound_id,
        sound_name=music.get("title"),
        duration_sec=float(video.get("duration", 0)) or None,
        view_count=_int(stats.get("playCount")),
        like_count=_int(stats.get("diggCount")),
        comment_count=_int(stats.get("commentCount")),
        share_count=_int(stats.get("shareCount")),
        save_count=_int(stats.get("collectCount")),
        thumbnail_url=thumbnail_url,
        geo_tier="KZ",  # accumulator was run from KZ home IP
        author_follower_count=_int(author_stats.get("followerCount")),
    )


def _normalize_threads_record(data: dict) -> Optional[PostRecord]:
    """Normalize a threads harvest record (already PostRecord-shaped)."""
    try:
        posted_at = None
        if data.get("posted_at"):
            try:
                posted_at = datetime.fromisoformat(str(data["posted_at"]))
            except Exception:
                pass
        fetched_at = datetime.now(timezone.utc)
        if data.get("fetched_at"):
            try:
                fetched_at = datetime.fromisoformat(str(data["fetched_at"]))
            except Exception:
                pass

        return PostRecord(
            platform="threads",
            platform_post_id=str(data.get("platform_post_id") or data.get("id", "")),
            account_handle=str(data.get("account_handle") or data.get("handle", "")),
            url=str(data.get("url", "")),
            fetched_at=fetched_at,
            media_type=str(data.get("media_type", "text")),
            raw=data.get("raw") or data,
            posted_at=posted_at,
            caption=data.get("caption"),
            hashtags=data.get("hashtags") or [],
            sound_id=data.get("sound_id"),
            sound_name=data.get("sound_name"),
            duration_sec=data.get("duration_sec"),
            view_count=data.get("view_count"),
            like_count=data.get("like_count"),
            comment_count=data.get("comment_count"),
            share_count=data.get("share_count"),
            save_count=data.get("save_count"),
            thumbnail_url=data.get("thumbnail_url"),
            geo_tier=data.get("geo_tier") or "KZ",
            author_follower_count=data.get("author_follower_count"),
        )
    except Exception as exc:
        print(f"[seed] skipping threads record: {exc}")
        return None


def _normalize_x_record(data: dict) -> Optional[PostRecord]:
    """Normalize an X harvest record."""
    try:
        posted_at = None
        if data.get("posted_at"):
            try:
                posted_at = datetime.fromisoformat(str(data["posted_at"]))
            except Exception:
                pass
        fetched_at = datetime.now(timezone.utc)
        if data.get("fetched_at"):
            try:
                fetched_at = datetime.fromisoformat(str(data["fetched_at"]))
            except Exception:
                pass

        return PostRecord(
            platform="x",
            platform_post_id=str(data.get("platform_post_id") or data.get("id", "")),
            account_handle=str(data.get("account_handle") or data.get("handle", "")),
            url=str(data.get("url", "")),
            fetched_at=fetched_at,
            media_type=str(data.get("media_type", "text")),
            raw=data.get("raw") or data,
            posted_at=posted_at,
            caption=data.get("caption"),
            hashtags=data.get("hashtags") or [],
            sound_id=None,
            sound_name=None,
            duration_sec=data.get("duration_sec"),
            view_count=data.get("view_count"),
            like_count=data.get("like_count"),
            comment_count=data.get("comment_count"),
            share_count=data.get("share_count"),
            save_count=data.get("save_count"),
            thumbnail_url=data.get("thumbnail_url"),
            geo_tier=data.get("geo_tier") or "World",
            author_follower_count=data.get("author_follower_count"),
        )
    except Exception as exc:
        print(f"[seed] skipping x record: {exc}")
        return None


def seed_from_scratch_files(download_thumbnails: bool = False) -> dict[str, int]:
    """Import data/*_accumulator.json and *_scratch.json into the DB.

    Safe to call multiple times — uses upsert semantics. Returns counts of
    inserted/updated records per platform.
    """
    init_db()
    session = get_session()
    counts: dict[str, int] = {}

    # --- TikTok accumulator ---
    tiktok_path = DATA_DIR / "tiktok_accumulator.json"
    if tiktok_path.exists():
        print(f"[seed] importing {tiktok_path}")
        data = json.loads(tiktok_path.read_text())
        items = data.get("items", {})
        n = 0
        for post_id, raw in items.items():
            try:
                record = _normalize_tiktok_raw(raw, post_id)
                upsert_post(session, record, source="tiktok_accumulator")
                if download_thumbnails and record.thumbnail_url:
                    path = _download_thumbnail("tiktok", post_id, record.thumbnail_url)
                    if path:
                        set_thumbnail_path(session, "tiktok", post_id, path)
                n += 1
            except Exception as exc:
                print(f"[seed] TikTok {post_id}: {exc}")
        session.commit()
        counts["tiktok"] = n
        print(f"[seed] TikTok: {n} records")

    # --- Threads harvest ---
    threads_path = DATA_DIR / "threads_harvest_scratch.json"
    if threads_path.exists():
        print(f"[seed] importing {threads_path}")
        data = json.loads(threads_path.read_text())
        posts = data.get("posts", {})
        # posts may be a dict keyed by id or a list
        if isinstance(posts, dict):
            post_list = list(posts.values())
        else:
            post_list = posts
        n = 0
        for item in post_list:
            record = _normalize_threads_record(item)
            if record:
                upsert_post(session, record, source="threads_harvest")
                if download_thumbnails and record.thumbnail_url:
                    path = _download_thumbnail("threads", record.platform_post_id, record.thumbnail_url)
                    if path:
                        set_thumbnail_path(session, "threads", record.platform_post_id, path)
                n += 1
        session.commit()
        counts["threads"] = n
        print(f"[seed] Threads: {n} records")

    # --- X harvest ---
    x_path = DATA_DIR / "x_harvest_500_scratch.json"
    if x_path.exists():
        print(f"[seed] importing {x_path}")
        data = json.loads(x_path.read_text())
        posts = data.get("posts", [])
        if isinstance(posts, dict):
            post_list = list(posts.values())
        else:
            post_list = posts
        n = 0
        for item in post_list:
            record = _normalize_x_record(item)
            if record:
                upsert_post(session, record, source="x_harvest")
                if download_thumbnails and record.thumbnail_url:
                    path = _download_thumbnail("x", record.platform_post_id, record.thumbnail_url)
                    if path:
                        set_thumbnail_path(session, "x", record.platform_post_id, path)
                n += 1
        session.commit()
        counts["x"] = n
        print(f"[seed] X: {n} records")

    session.close()
    return counts


# ---------------------------------------------------------------------------
# run_ingestion() — the main entrypoint (called by the API's POST /refresh
# and by the scheduler/cron)
# ---------------------------------------------------------------------------


def run_ingestion(
    *,
    seed_scratch: bool = True,
    download_thumbnails: bool = False,
    top_n: int = TOP_N_DEFAULT,
    geo_tier: Optional[str] = None,
) -> dict:
    """Run the full ingestion pipeline.

    1. Optionally seed DB from scratch JSON files.
    2. (Placeholder) Pull from live adapters — extend here when adapters are wired.
    3. Rank to get top-N per (platform × geo).
    4. Call enrichment lazily.

    Returns a summary dict.
    """
    print("[ingest] starting run_ingestion()")
    init_db()

    summary: dict = {"seeded": {}, "top_n_ids": [], "enrichment": "skipped"}

    # Step 1: seed from scratch files (safe if already seeded — upsert)
    if seed_scratch:
        summary["seeded"] = seed_from_scratch_files(
            download_thumbnails=download_thumbnails
        )

    # Step 2: live adapters (stub — extend here)
    # TODO: wire live adapters when the adapter pipeline is ready. For now the
    # scratch JSON seed is the data source.
    print("[ingest] live adapter harvest: skipped (stub)")

    # Step 3: rank to get current top-N
    session = get_session()
    try:
        platforms = [None] if not geo_tier else [None]
        ids: list[tuple[str, str]] = []
        for plat in (["tiktok", "instagram", "threads", "x"]):
            plat_ids = _ranker.top_n_ids(
                session, platform=plat, geo_tier=geo_tier, n=top_n
            )
            ids.extend(plat_ids)
        summary["top_n_ids"] = ids
        print(f"[ingest] ranked top-N: {len(ids)} posts across platforms")
    finally:
        session.close()

    # Step 4: enrichment (real — bridges into core.storage; idempotent + paced)
    if ids:
        print(f"[ingest] enriching {len(ids)} top-N posts")
        summary["enrichment"] = _call_enrichment(ids)

    print("[ingest] run_ingestion() complete")
    return summary


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Seed and/or run ingestion pipeline")
    parser.add_argument("--seed-only", action="store_true", help="Seed DB from scratch files and exit")
    parser.add_argument("--thumbnails", action="store_true", help="Download thumbnails during seed")
    args = parser.parse_args()

    if args.seed_only:
        counts = seed_from_scratch_files(download_thumbnails=args.thumbnails)
        print(f"Seeded: {counts}")
    else:
        result = run_ingestion(download_thumbnails=args.thumbnails)
        print(f"Done: {result}")
