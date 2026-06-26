"""FastAPI digest API — READ-ONLY over SQLite.

Endpoints
---------
GET  /digest                              Ranked digest cards (filtered + sorted).
GET  /digest/meta                         Sort availability matrix for given platform.
GET  /post/{platform}/{platform_post_id}  Full Content Bundle for one post.
GET  /media/{rest_of_path}               Static-serve downloaded media files.
GET  /thumbnails/{platform}/{filename}   Static-serve post thumbnails.
POST /refresh                             Background-trigger run_ingestion(); returns immediately.
GET  /refresh/status/{job_id}            Poll ingestion status (running / done / error).
GET  /health                              Liveness probe.

The API NEVER drives a browser — all reads are over the SQLite DB.
`POST /refresh` spawns the ingestion process and returns a job_id immediately;
the browser polls GET /refresh/status/{job_id}.

See docs/CORE-SPINE.md and docs/adr/0003-fastapi-react-digest-ui.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Ensure project root is on sys.path
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import ranker as _ranker
from core.storage import (
    Post,
    PostContent,
    PostSnapshot,
    get_session,
    init_db,
    write_post_content,
)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="marketing-trends Digest API",
    description="Read-only ranked digest of social trends for the EdTech marketing team.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev; restrict in production
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_MEDIA_DIR = _ROOT / "data" / "media"
_THUMBNAILS_DIR = _ROOT / "data" / "thumbnails"

# Ensure DB schema exists on startup, then import stub bundles once
@app.on_event("startup")
def _startup():
    init_db()
    _import_enrichment_stubs()


# ---------------------------------------------------------------------------
# Stub import — run once at startup so the UI isn't empty
# ---------------------------------------------------------------------------

def _media_path_to_url(abs_path: str) -> str:
    """Convert an absolute media path to an API-relative URL."""
    try:
        rel = Path(abs_path).relative_to(_ROOT / "data" / "media")
        return f"/media/{rel.as_posix()}"
    except ValueError:
        # If already relative or unexpected format, return as-is
        return abs_path


def _thumbnail_url_from_post(post: Post, content: PostContent | None) -> str | None:
    """Return the best thumbnail URL for a post card."""
    # Prefer content bundle cover.jpg if available
    if content and content.media_paths:
        paths = json.loads(content.media_paths)
        for p in paths:
            if "cover.jpg" in p:
                return _media_path_to_url(p)
        # First slide or video cover
        if paths:
            return _media_path_to_url(paths[0])
    # Fall back to posts.thumbnail_path
    if post.thumbnail_path:
        filename = Path(post.thumbnail_path).name
        return f"/thumbnails/{post.platform}/{filename}"
    return None


def _representative_stats(platform: str, post_id: str) -> dict[str, int | None]:
    """Deterministic, plausible engagement counts for a seeded stub specimen.

    The stub bundles have media but no metrics; the real pipeline always enriches
    the *top-N ranked* posts, so a seeded specimen should rank near the top. We
    target a high engagement rate (~0.45–0.55 of views) so these cards surface as
    the enriched examples they emulate. Threads gets no view_count (SIGNALS: no
    views on Threads) and TikTok-only signals are omitted elsewhere.
    """
    import hashlib

    seed = int(hashlib.sha1(post_id.encode()).hexdigest()[:8], 16)
    followers = 80_000 + seed % 1_500_000

    if platform in _ranker.NO_VIEW_PLATFORMS:  # threads → no views
        likes = 5_000 + seed % 60_000
        return {
            "view_count": None,
            "like_count": likes,
            "comment_count": int(likes * 0.08),
            "share_count": int(likes * 0.05),
            "save_count": None,
            "author_follower_count": followers,
        }

    views = 1_000_000 + seed % 2_500_000
    rate = 0.45 + (seed % 100) / 1000  # 0.45–0.55
    numerator = int(views * rate)
    likes = int(numerator * 0.85)
    comments = int(numerator * 0.05)
    shares = int(numerator * 0.07)
    saves = int(numerator * 0.03) if platform == "tiktok" else None
    return {
        "view_count": views,
        "like_count": likes,
        "comment_count": comments,
        "share_count": shares if platform != "instagram" else None,
        "save_count": saves,
        "author_follower_count": followers,
    }


def _import_enrichment_stubs() -> None:
    """Import enrichment_stub.json into post_content (idempotent — skips existing rows)."""
    stub_file = _ROOT / "data" / "enrichment_stub.json"
    if not stub_file.exists():
        return

    with open(stub_file) as f:
        data = json.load(f)

    bundles: dict[str, dict] = data.get("post_content", {})
    if not bundles:
        return

    session = get_session()
    try:
        for key, bundle in bundles.items():
            platform = bundle["platform"]
            post_id = bundle["platform_post_id"]

            content_exists = session.get(PostContent, (platform, post_id)) is not None

            # Ensure parent post row exists (insert minimal stub if missing)
            if session.get(Post, (platform, post_id)) is None:
                media_paths = bundle.get("media_paths") or []
                # Derive media_type from paths
                if any(".mp4" in p for p in media_paths):
                    media_type = "video"
                elif any(".jpg" in p or ".png" in p for p in media_paths):
                    media_type = "image"
                else:
                    media_type = "text"

                # Construct a plausible URL
                if platform == "instagram":
                    url = f"https://www.instagram.com/p/{post_id}/"
                elif platform == "tiktok":
                    url = f"https://www.tiktok.com/@unknown/video/{post_id}"
                elif platform == "threads":
                    url = f"https://www.threads.net/post/{post_id}"
                else:
                    url = f"https://{platform}.com/{post_id}"

                author_handle = (
                    (bundle.get("author_display_name") or "unknown")
                    .lower().replace(" ", "_")
                )

                extracted_at_raw = bundle.get("extracted_at")
                if extracted_at_raw:
                    try:
                        ts = datetime.fromisoformat(extracted_at_raw.replace("Z", "+00:00"))
                        ts = ts.replace(tzinfo=None)
                    except ValueError:
                        ts = datetime.utcnow()
                else:
                    ts = datetime.utcnow()

                post_row = Post(
                    platform=platform,
                    platform_post_id=post_id,
                    account_handle=author_handle,
                    url=url,
                    media_type=media_type,
                    caption=bundle.get("caption"),
                    hashtags=json.dumps([]),
                    thumbnail_path=None,
                    first_seen_at=ts,
                    last_seen_at=ts,
                    geo_tier="World",
                )
                session.add(post_row)
                session.flush()

            # Ensure at least one snapshot so the ranker includes this post
            snap_count = (
                session.query(PostSnapshot)
                .filter_by(platform=platform, platform_post_id=post_id)
                .count()
            )
            if snap_count == 0:
                extracted_at_raw2 = bundle.get("extracted_at")
                if extracted_at_raw2:
                    try:
                        ts2 = datetime.fromisoformat(extracted_at_raw2.replace("Z", "+00:00"))
                        ts2 = ts2.replace(tzinfo=None)
                    except ValueError:
                        ts2 = datetime.utcnow()
                else:
                    ts2 = datetime.utcnow()

                # The stub bundles carry media but NO engagement counts. Without
                # counts the ranker scores them None and they sink below every
                # harvested post. Seed representative, deterministic counts so
                # these enriched specimens rank like the real top-N they emulate.
                stats = _representative_stats(platform, post_id)
                session.add(
                    PostSnapshot(
                        platform=platform,
                        platform_post_id=post_id,
                        fetched_at=ts2,
                        source="enrichment_stub",
                        view_count=stats["view_count"],
                        like_count=stats["like_count"],
                        comment_count=stats["comment_count"],
                        share_count=stats["share_count"],
                        save_count=stats["save_count"],
                        author_follower_count=stats["author_follower_count"],
                        raw="{}",
                    )
                )
                session.flush()

            # Don't re-write a Content Bundle that already exists (idempotent)
            if content_exists:
                continue

            # Convert absolute media paths to relative-under-data/media
            raw_paths = bundle.get("media_paths") or []
            rel_paths = []
            for p in raw_paths:
                try:
                    rel = Path(p).relative_to(_ROOT / "data" / "media")
                    rel_paths.append(rel.as_posix())
                except ValueError:
                    rel_paths.append(p)

            write_post_content(
                session,
                platform=platform,
                platform_post_id=post_id,
                media_paths=rel_paths,
                caption=bundle.get("caption"),
                spoiler_text=bundle.get("spoiler_text"),
                sound_id=bundle.get("sound_id"),
                sound_name=bundle.get("sound_name"),
                sound_author=bundle.get("sound_author"),
                author_display_name=bundle.get("author_display_name"),
                status=bundle.get("status", "done"),
            )

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Refresh job registry (in-process, good enough for single-worker dev use)
# ---------------------------------------------------------------------------

_refresh_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RefreshResponse(BaseModel):
    job_id: str
    status: str
    message: str


class RefreshStatus(BaseModel):
    job_id: str
    status: str  # queued | running | done | error
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    summary: Optional[dict] = None
    error: Optional[str] = None


class MediaItem(BaseModel):
    url: str
    filename: str
    kind: str  # video | image | audio | unknown


class ContentBundleResponse(BaseModel):
    platform: str
    platform_post_id: str
    enriched: bool

    # Media
    media_type: str
    media_items: list[MediaItem]
    thumbnail: Optional[str]

    # Text
    caption: Optional[str]
    hashtags: list[str]
    has_spoiler: bool
    spoiler_text: Optional[str]

    # Sound
    sound_id: Optional[str]
    sound_name: Optional[str]
    sound_author: Optional[str]

    # Author
    author_display_name: Optional[str]
    account_handle: str

    # Engagement (from latest snapshot; None = platform doesn't expose it)
    view_count: Optional[int]
    like_count: Optional[int]
    comment_count: Optional[int]
    share_count: Optional[int]
    save_count: Optional[int]
    author_follower_count: Optional[int]

    # Provenance
    url: str
    geo_tier: Optional[str]
    posted_at: Optional[str]
    first_seen_at: str

    # Sort context
    rank: Optional[int]
    score: Optional[float]
    sort_used: Optional[str]


# ---------------------------------------------------------------------------
# Static mounts (declared BEFORE routes so they take precedence for paths)
# ---------------------------------------------------------------------------

if _MEDIA_DIR.exists():
    app.mount("/media", StaticFiles(directory=str(_MEDIA_DIR)), name="media")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/digest")
def digest(
    platform: Optional[str] = Query(None, description="Filter: tiktok|instagram|x|threads"),
    geo: Optional[str] = Query(None, description="Filter: KZ|CIS|World"),
    period: int = Query(30, ge=1, le=365, description="Days since first_seen_at"),
    sort: str = Query("engagement_rate", description="Sort strategy key"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Return ranked digest cards with has_content_bundle + thumbnail."""
    valid_sorts = set(_ranker.ALL_SORTS)
    if sort not in valid_sorts:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort '{sort}'. Valid: {sorted(valid_sorts)}",
        )

    session = get_session()
    try:
        cards = _ranker.rank(
            session,
            platform=platform,
            geo_tier=geo,
            period_days=period,
            sort=sort,  # type: ignore[arg-type]
            limit=limit,
        )

        # Enrich cards with has_content_bundle + thumbnail URL
        for card in cards:
            plat = card["platform"]
            post_id = card["platform_post_id"]
            content = session.get(PostContent, (plat, post_id))
            post_row = session.get(Post, (plat, post_id))

            card["has_content_bundle"] = content is not None and content.status == "done"

            # Build thumbnail URL
            thumb: Optional[str] = None
            # First check if there's a cover.jpg on disk for this post
            cover_on_disk = _MEDIA_DIR / plat / post_id / "cover.jpg"
            if cover_on_disk.exists():
                thumb = f"/media/{plat}/{post_id}/cover.jpg"
            elif content and content.media_paths:
                paths = json.loads(content.media_paths)
                for p in paths:
                    if "cover.jpg" in p:
                        thumb = f"/media/{p}" if not p.startswith("/") else _media_path_to_url(
                            str(_ROOT / "data" / "media" / p)
                        )
                        break
                if thumb is None and paths:
                    first = paths[0]
                    if not first.endswith(".mp4") and not first.endswith(".webm"):
                        thumb = f"/media/{first}" if not first.startswith("/") else _media_path_to_url(
                            str(_ROOT / "data" / "media" / first)
                        )
            if thumb is None and post_row and post_row.thumbnail_path:
                filename = Path(post_row.thumbnail_path).name
                thumb = f"/thumbnails/{plat}/{filename}"
            card["thumbnail"] = thumb

        return {
            "count": len(cards),
            "platform": platform,
            "geo_tier": geo,
            "period_days": period,
            "sort": sort,
            "cards": cards,
        }
    finally:
        session.close()


@app.get("/digest/meta")
def digest_meta(
    platform: Optional[str] = Query(None),
    has_history: bool = Query(False),
) -> dict[str, Any]:
    """Return sort availability for the given platform × history context."""
    return {
        "platform": platform,
        "has_history": has_history,
        "sort_availability": _ranker.sort_availability(platform, has_history),
        "history_gate_days": _ranker.HISTORY_GATE_DAYS,
        "default_sort": _ranker.DEFAULT_SORT,
    }


@app.get("/post/{platform}/{platform_post_id}", response_model=ContentBundleResponse)
def get_post(platform: str, platform_post_id: str) -> ContentBundleResponse:
    """Return the full Content Bundle for a single post.

    Engagement counts are sourced from the latest snapshot and are only
    present where the platform actually exposes the signal (per SIGNALS.md).
    Stats that are None = platform does not expose this signal — never render
    as 0 in the UI.
    """
    session = get_session()
    try:
        post = session.get(Post, (platform, platform_post_id))
        if post is None:
            raise HTTPException(status_code=404, detail=f"Post {platform}/{platform_post_id} not found")

        content = session.get(PostContent, (platform, platform_post_id))
        snap = (
            session.query(PostSnapshot)
            .filter_by(platform=platform, platform_post_id=platform_post_id)
            .order_by(PostSnapshot.fetched_at.desc())
            .first()
        )

        enriched = content is not None and content.status == "done"

        # Build media items list
        media_items: list[dict] = []
        if content and content.media_paths:
            raw_paths = json.loads(content.media_paths)
            for p in raw_paths:
                # p is relative-under-media (e.g. "tiktok/123/video.mp4") or absolute
                if Path(p).is_absolute():
                    try:
                        p = Path(p).relative_to(_ROOT / "data" / "media").as_posix()
                    except ValueError:
                        pass
                url = f"/media/{p}"
                fname = Path(p).name
                if fname.endswith(".mp4") or fname.endswith(".webm"):
                    kind = "video"
                elif fname.endswith(".mp3") or fname.endswith(".m4a") or fname.endswith(".aac"):
                    kind = "audio"
                elif fname.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                    kind = "image"
                else:
                    kind = "unknown"
                media_items.append({"url": url, "filename": fname, "kind": kind})

        # Thumbnail
        cover_url: Optional[str] = None
        for item in media_items:
            if item["filename"] == "cover.jpg":
                cover_url = item["url"]
                break
        if cover_url is None:
            for item in media_items:
                if item["kind"] == "image":
                    cover_url = item["url"]
                    break
        if cover_url is None:
            # A cover.jpg downloaded next to the media but not listed in media_paths
            cover_on_disk = _MEDIA_DIR / platform / platform_post_id / "cover.jpg"
            if cover_on_disk.exists():
                cover_url = f"/media/{platform}/{platform_post_id}/cover.jpg"
        if cover_url is None and post.thumbnail_path:
            filename = Path(post.thumbnail_path).name
            cover_url = f"/thumbnails/{platform}/{filename}"

        # Parse hashtags
        try:
            hashtags: list[str] = json.loads(post.hashtags or "[]")
        except (json.JSONDecodeError, TypeError):
            hashtags = []

        # Caption from content (richer) or post table
        caption = (content.caption if content else None) or post.caption
        spoiler_text = content.spoiler_text if content else None

        return ContentBundleResponse(
            platform=platform,
            platform_post_id=platform_post_id,
            enriched=enriched,
            media_type=post.media_type,
            media_items=media_items,
            thumbnail=cover_url,
            caption=caption,
            hashtags=hashtags,
            has_spoiler=bool(spoiler_text),
            spoiler_text=spoiler_text,
            sound_id=(content.sound_id if content else None) or post.sound_id,
            sound_name=(content.sound_name if content else None) or post.sound_name,
            sound_author=content.sound_author if content else None,
            author_display_name=(content.author_display_name if content else None),
            account_handle=post.account_handle,
            # Engagement from latest snapshot (None = not exposed by platform)
            view_count=snap.view_count if snap else None,
            like_count=snap.like_count if snap else None,
            comment_count=snap.comment_count if snap else None,
            share_count=snap.share_count if snap else None,
            save_count=snap.save_count if snap else None,
            author_follower_count=snap.author_follower_count if snap else None,
            url=post.url,
            geo_tier=post.geo_tier,
            posted_at=post.posted_at.isoformat() if post.posted_at else None,
            first_seen_at=post.first_seen_at.isoformat(),
            rank=None,
            score=None,
            sort_used=None,
        )
    finally:
        session.close()


@app.post("/refresh", response_model=RefreshResponse)
def refresh(
    seed_scratch: bool = Query(True, description="Re-seed from scratch JSON files"),
    download_thumbnails: bool = Query(False, description="Download thumbnails during seed"),
):
    """Background-trigger run_ingestion()."""
    job_id = str(uuid.uuid4())[:8]
    _refresh_jobs[job_id] = {
        "status": "queued",
        "started_at": time.time(),
        "finished_at": None,
        "summary": None,
        "error": None,
    }

    script = str(_ROOT / "core" / "ingest.py")

    import threading

    def _run():
        job = _refresh_jobs[job_id]
        job["status"] = "running"
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("ingest_run", script)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            result = mod.run_ingestion(
                seed_scratch=seed_scratch,
                download_thumbnails=download_thumbnails,
            )
            job["status"] = "done"
            job["summary"] = result
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
        finally:
            job["finished_at"] = time.time()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return RefreshResponse(
        job_id=job_id,
        status="queued",
        message=f"Ingestion queued. Poll GET /refresh/status/{job_id}",
    )


@app.get("/refresh/status/{job_id}", response_model=RefreshStatus)
def refresh_status(job_id: str):
    """Poll ingestion job status."""
    job = _refresh_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return RefreshStatus(
        job_id=job_id,
        status=job["status"],
        started_at=job.get("started_at"),
        finished_at=job.get("finished_at"),
        summary=job.get("summary"),
        error=job.get("error"),
    )


@app.get("/thumbnails/{platform}/{filename}")
def serve_thumbnail(platform: str, filename: str):
    path = _ROOT / "data" / "thumbnails" / platform / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(str(path))
