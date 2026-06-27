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

from core.schema import PostRecord, WatchedAccount
from core.storage import Account, Post, get_session, init_db, set_thumbnail_path, upsert_post
from core import ranker as _ranker

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = _ROOT / "data"
MEDIA_DIR = DATA_DIR / "media"
THUMBNAIL_DIR = DATA_DIR / "thumbnails"

TOP_N_DEFAULT = 25

# ---------------------------------------------------------------------------
# Celebrity filter — applied DURING scraping so a requested N yields N *good*
# posts (mega-accounts are skipped before they count toward the target, never
# scraped-then-deleted). A marketing team hunting trends doesn't want a feed of
# Ronaldo/NASA/Nike; breakout content from smaller creators is the signal.
# ---------------------------------------------------------------------------

# Followers above this = "celebrity", filtered out. Mid-tier/creator accounts
# (the trend signal) sit well below this.
CELEBRITY_FOLLOWER_CAP = 5_000_000

# Known mega-accounts to drop even when a follower count isn't available (e.g.
# Instagram Explore payloads omit it). Lowercase, no leading '@'.
_CELEBRITY_HANDLES: frozenset[str] = frozenset({
    "cristiano", "leomessi", "nike", "natgeo", "nasa", "instagram",
    "nytimes", "9gag", "espn", "bbcnews", "netflix", "spotify",
    "elonmusk", "billgates", "google", "openai", "bbcbreaking", "cnn",
    "reuters", "nba", "theeconomist", "verge", "wired", "techcrunch",
    "forbes", "mit", "zuck", "mosseri", "harvard", "khanacademy",
    "washingtonpost", "realmadrid", "fcbarcelona", "championsleague",
    "kyliejenner", "kimkardashian", "therock", "selenagomez", "beyonce",
    "khaby.lame", "mrbeast",
})


def purge_celebrity_posts(session) -> dict:
    """Delete posts authored by celebrity/mega-accounts already in the corpus.

    A post is a celebrity post if its handle is denylisted OR its max observed
    follower count exceeds the cap. Cascades to snapshots, content, flags, notes,
    and collection items. Returns ``{posts, by_handle}``.
    """
    from sqlalchemy import func as _func
    from core.storage import (
        PostSnapshot, PostContent, PostFlag, PostNote, CollectionItem,
    )

    # Max follower count observed per post (snapshots may vary).
    fol_by_post: dict[tuple[str, str], Optional[int]] = {}
    for plat, pid, fol in session.query(
        PostSnapshot.platform,
        PostSnapshot.platform_post_id,
        _func.max(PostSnapshot.author_follower_count),
    ).group_by(PostSnapshot.platform, PostSnapshot.platform_post_id).all():
        fol_by_post[(plat, pid)] = fol

    victims: list[tuple[str, str]] = []
    by_handle: dict[str, int] = {}
    for post in session.query(Post).all():
        key = (post.platform, post.platform_post_id)
        if is_celebrity(post.account_handle, fol_by_post.get(key)):
            victims.append(key)
            by_handle[post.account_handle] = by_handle.get(post.account_handle, 0) + 1

    for plat, pid in victims:
        for model in (PostSnapshot, PostContent, PostFlag, PostNote, CollectionItem):
            session.query(model).filter(
                model.platform == plat, model.platform_post_id == pid
            ).delete(synchronize_session=False)
        session.query(Post).filter(
            Post.platform == plat, Post.platform_post_id == pid
        ).delete(synchronize_session=False)
    session.commit()
    return {"posts": len(victims), "by_handle": by_handle}


def is_celebrity(handle: Optional[str], follower_count: Optional[int] = None) -> bool:
    """True if this author should be filtered out as a celebrity/mega-account.

    Handle denylist catches known mega-accounts even with no follower data
    (Explore omits it); the follower cap catches everyone else when we do have it.
    """
    h = (handle or "").strip().lower().lstrip("@")
    if h and h in _CELEBRITY_HANDLES:
        return True
    if follower_count is not None and follower_count > CELEBRITY_FOLLOWER_CAP:
        return True
    return False


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
# Live harvest — pull BRAND-NEW posts from the adapters (hard-refresh "live")
# ---------------------------------------------------------------------------

# Small fallback seed sets used only when the DB watchlist is empty for a
# platform. Kept tiny on purpose — live harvest is the expensive path.
# Seed handles used when the DB watchlist is empty for a platform. Kept broad so a
# deep live harvest (live_per_platform up to ~500) can reach volume by BREADTH —
# many accounts shallow — instead of hammering 3 accounts deep (safer + more
# diverse). Curate domain-specific accounts via Account.on_watchlist.
_FALLBACK_SEEDS: dict[str, list[str]] = {
    "x": [
        "NASA", "OpenAI", "Google", "nytimes", "BBCBreaking", "CNN", "Reuters",
        "espn", "NBA", "TheEconomist", "verge", "WIRED", "TechCrunch", "Forbes",
        "elonmusk", "BillGates",
    ],
    "threads": [
        "zuck", "mosseri", "natgeo", "nasa", "instagram", "espn", "nba",
        "bbcnews", "cnn", "theverge", "wired", "netflix",
    ],
    # Instagram is harvested from the Explore tab (discovery), not seed accounts —
    # see harvest_instagram_explore. No seed list needed.
    "instagram": [],
    # TikTok primarily uses discovery (fetch_viral_posts); these seed accounts let
    # it top up toward the target via fetch_account_posts when discovery is thin.
    "tiktok": [
        "khaby.lame", "mrbeast", "nasa", "espn", "nba", "netflix",
        "spotify", "washingtonpost",
    ],
}


def _watchlist_handles(session, platform: str) -> list[str]:
    """Watchlist handles for a platform from the DB, else the small fallback seeds."""
    rows = (
        session.query(Account.handle)
        .filter_by(platform=platform, on_watchlist=True)
        .all()
    )
    handles = [r[0] for r in rows]
    return handles or _FALLBACK_SEEDS.get(platform, [])


def _make_adapter(platform: str, *, ig_hydrate_views: bool = False):
    """Lazily construct a platform adapter (imports are heavy/optional).

    ``ig_hydrate_views`` only affects Instagram: off for bulk live harvest (mass
    per-video view hydration is ban bait + slow), on for single-post resnapshot
    where one extra call to recover the real view count is worth it.
    """
    if platform == "x":
        from adapters.x import XAdapter
        # prefer_graphql recovers the real views.count for recent tweets (the
        # syndication path can't see it); falls back to syndication on failure.
        return XAdapter(prefer_graphql=True)
    if platform == "threads":
        from adapters.threads import ThreadsAdapter
        return ThreadsAdapter(headless=True)
    if platform == "tiktok":
        from adapters.tiktok import TikTokAdapter
        return TikTokAdapter()
    if platform == "instagram":
        return _make_instagram_adapter(hydrate_views=ig_hydrate_views)
    raise ValueError(f"unknown platform {platform!r}")


_SECRETS_DIR = _ROOT / "secrets"
_IG_SESSION_FILE = _SECRETS_DIR / "ig_browser_session.json"
_IG_BURNER_ENV = _SECRETS_DIR / "ig_burner.env"


def _ig_settings_file() -> str:
    """Path to the durable instagrapi settings (device + auth) file.

    Defaults under secrets/ so a self-heal relogin persists a STABLE device
    fingerprint across runs (reusing the same emulated phone is far less
    challenge-prone than a fresh device every time)."""
    return os.environ.get("IG_SETTINGS_FILE") or str(_SECRETS_DIR / "ig_settings.json")


def _load_ig_credentials() -> tuple[Optional[str], Optional[str]]:
    """Burner username/password from env, falling back to secrets/ig_burner.env.

    These are ONLY needed for self-heal (auto-relogin when the session lapses).
    A bare sessionid can't regenerate itself, so without credentials a dead
    session means 'skip Instagram until someone re-mints it'."""
    user = os.environ.get("IG_USERNAME")
    pw = os.environ.get("IG_PASSWORD")
    if user and pw:
        return user, pw
    if _IG_BURNER_ENV.exists():
        try:
            kv: dict[str, str] = {}
            for line in _IG_BURNER_ENV.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip().strip('"').strip("'")
            return (user or kv.get("IG_USERNAME"), pw or kv.get("IG_PASSWORD"))
        except OSError:
            pass
    return user, pw


def _read_sessionid() -> Optional[str]:
    """sessionid from env or secrets/ig_browser_session.json."""
    sessionid = os.environ.get("IG_SESSIONID")
    if sessionid:
        return sessionid
    if _IG_SESSION_FILE.exists():
        try:
            return json.loads(_IG_SESSION_FILE.read_text()).get("sessionid")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _persist_ig_session(cl) -> None:
    """Write the client's CURRENT sessionid back to the drop-in file so the next
    run starts from the freshly-relogged-in cookie (keeps the file teammates
    receive the canonical source of truth)."""
    try:
        sid = cl.sessionid
    except Exception:
        return
    if not sid:
        return
    try:
        _SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        _IG_SESSION_FILE.write_text(
            json.dumps({"sessionid": sid, "ds_user_id": str(getattr(cl, "user_id", "") or "")})
        )
    except OSError as exc:
        print(f"[ingest] could not persist refreshed IG session (non-fatal): {exc}")


def _ig_session_is_valid(cl) -> bool:
    """Cheap authenticated probe — True if the session still works.

    Distinguishes a dead session (LoginRequired → heal) from transient trouble
    (network/backoff → leave alone, don't trigger a needless relogin storm)."""
    from instagrapi.exceptions import LoginRequired

    try:
        cl.account_info()
        return True
    except LoginRequired:
        return False
    except Exception:
        # Not an auth problem (rate-limit, network, etc.) — assume still logged in
        # and let the real harvest call surface/handle it.
        return True


def _make_instagram_adapter(*, hydrate_views: bool = False):
    """Build a LOGGED-IN, SELF-HEALING Instagram adapter from the burner session.

    IG's mobile private API rejects anonymous callers, so we must authenticate.
    Auth sources, in order of preference:
      1. IG_SETTINGS_FILE / secrets/ig_settings.json — a warmed instagrapi session
         (device + auth); best, and what self-heal writes back to.
      2. IG_SESSIONID / secrets/ig_browser_session.json — a raw sessionid cookie.

    Self-heal: the chosen session is probed once; if it has EXPIRED and burner
    credentials are available (IG_USERNAME/IG_PASSWORD or secrets/ig_burner.env),
    we relogin with the persisted device fingerprint and write the refreshed
    session back to disk. Without credentials (or if relogin hits a checkpoint),
    we raise so the caller skips Instagram — the other platforms are unaffected.

    The Client is pinned to the KZ / ru-RU locale the burner was warmed under so
    the device fingerprint stays consistent (a mismatch is challenge-bait), and
    instagrapi's per-request delay is kept slow (4–9s) per the adapter README.
    """
    from instagrapi import Client
    from adapters.instagram import InstagramAdapter

    cl = Client()
    # Match the locale/device the session was minted under (see
    # scratch_login_harvest.py); a region/locale mismatch raises IG's suspicion.
    cl.set_locale("ru_RU")
    cl.set_country("KZ")
    cl.set_country_code(7)
    cl.set_timezone_offset(5 * 3600)
    cl.delay_range = [4.0, 9.0]

    settings_file = _ig_settings_file()
    adapter = InstagramAdapter(client=cl, hydrate_views=hydrate_views)

    # 1) Establish a session from the best available source.
    established = False
    if os.path.exists(settings_file):
        adapter.load_session(settings_file)
        established = True
    else:
        sessionid = _read_sessionid()
        if sessionid:
            cl.login_by_sessionid(sessionid)
            established = True

    # 2) Probe it; self-heal if it's expired.
    if established and _ig_session_is_valid(cl):
        return adapter

    user, pw = _load_ig_credentials()
    if not user or not pw:
        raise RuntimeError(
            "Instagram session missing or expired and no burner credentials to "
            "self-heal. Provide a fresh secrets/ig_browser_session.json, or set "
            "IG_USERNAME/IG_PASSWORD (or secrets/ig_burner.env) to enable "
            "auto-relogin. (See docs/DEPLOY.md.)"
        )

    # Relogin reuses the device in settings_file (if any) and dumps the refreshed
    # session back to it; then mirror the new sessionid into the drop-in file.
    print("[ingest] Instagram session expired — attempting auto-relogin…")
    try:
        adapter.login(user, pw, settings_file=settings_file)
    except Exception as exc:
        raise RuntimeError(
            "Instagram auto-relogin failed (likely a checkpoint/2FA that needs the "
            f"account owner): {exc}. Re-mint secrets/ig_browser_session.json "
            "manually from a trusted IP. (See docs/DEPLOY.md.)"
        ) from exc
    _persist_ig_session(cl)
    print("[ingest] Instagram re-login OK — session refreshed.")
    return adapter


def harvest_instagram_explore(
    session,
    *,
    target: int,
    geo_tier: str = "KZ",
    headless: bool = True,
) -> dict:
    """Harvest fresh, celebrity-filtered posts from the Instagram **Explore tab**.

    This is IG's discovery surface — organic trending content from many creators,
    not a fixed set of seed accounts (which is why the old seed path only ever
    surfaced celebrities). Drives the browser Explore harvester and, for each
    newly-intercepted item, drops celebrities, normalizes to a PostRecord, and
    upserts it — counting only KEPT, new-to-corpus posts toward ``target``. So a
    request for N yields up to N good posts with no scrape-then-delete.

    Best-effort: on captcha/checkpoint/session-expiry the harvester stops cleanly
    and we return what we collected. Returns
    ``{fetched, new, skipped_celebrity, blocked, blocked_reason}``.
    """
    from adapters.instagram.explore_harvest import harvest_explore
    from adapters.instagram import InstagramAdapter

    # Offline normalizer — no login/network needed to turn a raw media dict into a
    # PostRecord (the browser persona does the actual fetching).
    conv = InstagramAdapter(hydrate_views=False)
    stats = {"fetched": 0, "new": 0, "skipped_celebrity": 0}

    def _on_new_item(pk: str, raw: dict) -> None:
        stats["fetched"] += 1
        author = (raw.get("user") or {}).get("username")
        if not author:
            return  # filler/carousel-child without an author — not a real post
        # Explore payloads omit follower_count, so filter by handle denylist here.
        if is_celebrity(author):
            stats["skipped_celebrity"] += 1
            return
        acct = WatchedAccount(
            handle=author, platform="instagram", segment="adjacent", geo_tier=geo_tier
        )
        try:
            rec = conv._to_record(raw, acct)
        except Exception:
            rec = None
        if rec is None or not rec.raw:
            return
        if session.get(Post, (rec.platform, rec.platform_post_id)) is not None:
            return  # already in corpus — not fresh
        upsert_post(session, rec, source="live_instagram_explore")
        session.commit()
        stats["new"] += 1

    def _reached() -> bool:
        return stats["new"] >= target

    summary = harvest_explore(
        target=target,
        headless=headless,
        on_new_item=_on_new_item,
        target_reached=_reached,
    )
    return {
        **stats,
        "blocked": summary.get("blocked"),
        "blocked_reason": summary.get("blocked_reason"),
    }


def harvest_live(
    *,
    platforms: Optional[list[str]] = None,
    per_platform: int = 12,
    geo_tier: str = "World",
) -> dict:
    """Pull brand-new posts from the live adapters (the hard-refresh "live" source).

    Best-effort and bounded: each platform is wrapped so one failure (browser
    missing, session expired, rate-limit) never sinks the others. New posts are
    upserted via core.storage; the caller then ranks + enriches as usual.

    - x:        auth-free syndication (most reliable).
    - threads:  Playwright profile harvest of seed handles.
    - tiktok:   Explore/FYP discovery (no account list needed).
    - instagram: Explore-tab discovery via a browser persona, celebrity-filtered
      inline (harvest_instagram_explore) — organic trends, not seed accounts.

    All platforms drop celebrity/mega-accounts during the scrape so a requested
    per-platform target yields that many *good* posts (no scrape-then-delete).
    """
    init_db()
    platforms = platforms or ["x", "threads", "tiktok", "instagram"]
    session = get_session()
    summary: dict[str, dict] = {}
    try:
        for plat in platforms:
            result = {"fetched": 0, "new": 0, "error": None}
            try:
                # IG uses the browser Explore persona (built inside
                # harvest_instagram_explore), not the instagrapi adapter — so
                # don't construct/login it here (that would be needless + can raise).
                adapter = None if plat == "instagram" else _make_adapter(plat)

                # Persist + COMMIT per batch so partial progress survives an
                # interruption (IG's anti-ban pacing makes a full sweep slow).
                new_count = 0

                def _persist(recs: list[PostRecord]) -> None:
                    nonlocal new_count
                    for rec in recs:
                        if new_count >= per_platform:
                            break
                        if not rec.raw:  # never persist an empty-raw hole
                            continue
                        # Filter celebrities IN the scrape loop: skipped posts never
                        # count toward the target and are never stored, so N requested
                        # yields N good posts (no scrape-then-delete).
                        if is_celebrity(rec.account_handle, rec.author_follower_count):
                            continue
                        is_new = session.get(Post, (rec.platform, rec.platform_post_id)) is None
                        upsert_post(session, rec, source=f"live_{plat}")
                        if is_new:
                            new_count += 1
                    session.commit()

                # 1) Discovery surface (TikTok today): brand-new viral posts, no
                #    account needed. Best-effort top-up toward the target.
                if plat == "tiktok":
                    try:
                        recs = adapter.fetch_viral_posts(geo_tier="KZ", period_days=7)
                    except NotImplementedError:
                        recs = []
                    result["fetched"] += len(recs)
                    print(f"[live] {plat}: parsed {len(recs)} discovery posts")
                    _persist(recs)

                # 1b) Instagram discovery = the Explore tab (organic trends across
                #     many creators), celebrity-filtered inline. Replaces the old
                #     seed-account path that only ever surfaced celebrities.
                if plat == "instagram":
                    print(
                        f"[live] instagram: harvesting Explore tab toward "
                        f"{per_platform} (celebrity-filtered)"
                    )
                    try:
                        ex = harvest_instagram_explore(
                            session, target=per_platform, geo_tier="KZ", headless=True
                        )
                        result["fetched"] += ex.get("fetched", 0)
                        new_count += ex.get("new", 0)
                        print(
                            f"[live] instagram: explore parsed={ex.get('fetched')} "
                            f"new={ex.get('new')} skipped_celebrity={ex.get('skipped_celebrity')} "
                            f"blocked={ex.get('blocked')}"
                        )
                        if ex.get("blocked"):
                            print(f"[live] instagram: explore blocked: {ex.get('blocked_reason')}")
                    except Exception as exc:
                        print(
                            f"[live] instagram explore failed (non-fatal): "
                            f"{type(exc).__name__}: {str(exc)[:160]}"
                        )
                    result["new"] = new_count
                    print(f"[live] {plat}: TOTAL parsed={result['fetched']} new={result['new']}")
                    summary[plat] = result
                    continue  # skip the seed-account path for IG

                # 2) Account-handle harvesting (X / Threads). Spread the remaining
                #    target across handles — many accounts shallow — so we reach
                #    volume by breadth, not by hammering a few accounts deep.
                handles = _watchlist_handles(session, plat)
                if handles and new_count < per_platform:
                    per_handle = max(25, -(-per_platform // len(handles)))  # ceil div
                    print(
                        f"[live] {plat}: harvesting {len(handles)} handle(s) "
                        f"~{per_handle}/handle toward {per_platform}: {handles}"
                    )
                    for handle in handles:
                        if new_count >= per_platform:
                            break
                        acct = WatchedAccount(
                            handle=handle,
                            platform=plat,
                            segment="adjacent",
                            geo_tier=geo_tier,
                        )
                        try:
                            got = adapter.fetch_account_posts(acct, limit=per_handle)
                        except Exception as exc:  # one account failing is non-fatal
                            print(f"[live] {plat}@{handle}: {type(exc).__name__}: {str(exc)[:120]}")
                            continue
                        result["fetched"] += len(got)
                        before = new_count
                        _persist(got)  # commit this account before moving on
                        print(
                            f"[live] {plat}@{handle}: parsed {len(got)} posts "
                            f"(+{new_count - before} new, {result['fetched']} parsed, "
                            f"{new_count}/{per_platform} new so far)"
                        )

                result["new"] = new_count
                print(f"[live] {plat}: TOTAL parsed={result['fetched']} new={result['new']}")
            except Exception as exc:
                session.rollback()
                result["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                print(f"[live] {plat} harvest failed (non-fatal): {result['error']}")
            summary[plat] = result
    finally:
        session.close()
    print(f"[live] harvest summary: {summary}")
    return summary


def resnapshot_post(platform: str, platform_post_id: str, *, limit: int = 25) -> dict:
    """Re-observe a SINGLE post live right now and append a fresh snapshot.

    Adapters have no single-post fetch, so we pull the author's recent posts,
    find this id, and upsert it (which appends a `post_snapshots` row with the
    current metrics). Bounded + synchronous — this backs the lightbox
    "fetch fresh data" button that adds a new point to the stats graph.

    Returns ``{status: updated|not_found|error, fetched: int, error?: str}``.
    A post that has scrolled out of the author's recent feed yields ``not_found``.
    """
    init_db()
    session = get_session()
    try:
        post = session.get(Post, (platform, platform_post_id))
        if post is None:
            return {"status": "error", "error": "post not found in DB"}
        if not post.account_handle:
            return {"status": "error", "error": "post has no account handle to re-fetch"}
        try:
            adapter = _make_adapter(platform, ig_hydrate_views=True)
            acct = WatchedAccount(
                handle=post.account_handle,
                platform=platform,
                segment="adjacent",
                geo_tier=post.geo_tier or "World",
            )
            records = adapter.fetch_account_posts(acct, limit=limit)
        except Exception as exc:
            return {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

        match = next((r for r in records if r.platform_post_id == platform_post_id), None)
        if match is None:
            return {"status": "not_found", "fetched": len(records)}
        if not match.raw:
            return {"status": "error", "error": "re-fetched record had empty raw"}
        upsert_post(session, match, source="resnapshot")
        session.commit()
        return {"status": "updated", "fetched": len(records)}
    finally:
        session.close()


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
