"""FastAPI digest API — READ-ONLY over SQLite.

Endpoints
---------
GET    /digest                              Ranked digest cards (filtered + sorted; unseen_only/include_hidden).
GET    /digest/meta                         Sort availability matrix (DB-backed) for given platform.
GET    /post/{platform}/{platform_post_id}  Full Content Bundle for one post (incl. note + flags).
GET    /media/{rest_of_path}                Static-serve downloaded media files.
GET    /thumbnails/{platform}/{filename}    Static-serve post thumbnails.

GET    /collections                         List collections (with item counts).
POST   /collections                         Create a collection.
PATCH  /collections/{id}                    Rename / re-describe a collection.
DELETE /collections/{id}                     Delete a collection (posts untouched).
GET    /collections/{id}                     Collection detail + its posts as digest cards.
POST   /collections/{id}/items               Add a post to a collection (idempotent).
DELETE /collections/{id}/items/{plat}/{pid}  Remove a post from a collection.

PUT    /post/{plat}/{pid}/note               Upsert a post's global note (empty body deletes).
DELETE /post/{plat}/{pid}/note               Delete a post's note.
PUT    /post/{plat}/{pid}/flags              Set hidden/pinned flags.

POST   /refresh                              Soft refresh — background run_ingestion().
POST   /refresh/hard                         Hard/selective refresh — rotate seen + serve unseen.
GET    /refresh/status/{job_id}              Poll refresh status (running / done / error).
GET    /health                               Liveness probe.

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
from core import storage as _storage
from core.storage import (
    Collection,
    CollectionItem,
    Post,
    PostContent,
    PostFlag,
    PostNote,
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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
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


class CollectionCreate(BaseModel):
    title: str
    description: Optional[str] = None


class CollectionUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None


class CollectionItemAdd(BaseModel):
    platform: str
    platform_post_id: str


class NoteBody(BaseModel):
    body: str


class FlagUpdate(BaseModel):
    hidden: Optional[bool] = None
    pinned: Optional[bool] = None


class HardRefreshRequest(BaseModel):
    source: Literal["corpus", "live"] = "corpus"
    # [[platform, post_id], ...] currently-shown posts to rotate out (mark served).
    # Pinned posts in this list are skipped (they stay). For a full hard refresh the
    # UI sends the whole visible set; for a selective refresh, just the chosen cards.
    serve_ids: list[list[str]] = []
    # Filters describing the working set to (re)enrich.
    platform: Optional[str] = None
    geo: Optional[str] = None
    period: int = 30
    sort: str = "engagement_rate"
    limit: int = 50
    # Bound on a live harvest so it can't grind the IP.
    live_per_platform: int = 12


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

    # User state
    note: Optional[str] = None
    hidden: bool = False
    pinned: bool = False
    collection_ids: list[int] = []


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


def _resolve_thumbnail(session, plat: str, post_id: str, content: PostContent | None, post_row: Post | None) -> Optional[str]:
    """Best thumbnail URL for a card: on-disk cover, then content media, then post thumb."""
    cover_on_disk = _MEDIA_DIR / plat / post_id / "cover.jpg"
    if cover_on_disk.exists():
        return f"/media/{plat}/{post_id}/cover.jpg"
    if content and content.media_paths:
        paths = json.loads(content.media_paths)
        for p in paths:
            if "cover.jpg" in p:
                return f"/media/{p}" if not p.startswith("/") else _media_path_to_url(
                    str(_ROOT / "data" / "media" / p)
                )
        if paths:
            first = paths[0]
            if not first.endswith(".mp4") and not first.endswith(".webm"):
                return f"/media/{first}" if not first.startswith("/") else _media_path_to_url(
                    str(_ROOT / "data" / "media" / first)
                )
    if post_row and post_row.thumbnail_path:
        return f"/thumbnails/{plat}/{Path(post_row.thumbnail_path).name}"
    return None


def _attach_card_extras(session, card: dict) -> dict:
    """Attach has_content_bundle, thumbnail, note, hidden/pinned flags, collection_ids."""
    plat = card["platform"]
    post_id = card["platform_post_id"]
    content = session.get(PostContent, (plat, post_id))
    post_row = session.get(Post, (plat, post_id))

    card["has_content_bundle"] = content is not None and content.status == "done"
    card["thumbnail"] = _resolve_thumbnail(session, plat, post_id, content, post_row)

    note = session.get(PostNote, (plat, post_id))
    card["note"] = note.body if note else None
    flag = session.get(PostFlag, (plat, post_id))
    card["hidden"] = bool(flag.hidden) if flag else False
    card["pinned"] = bool(flag.pinned) if flag else False
    card["collection_ids"] = _storage.collection_ids_for_post(session, plat, post_id)
    return card


@app.get("/digest")
def digest(
    platform: Optional[str] = Query(None, description="Filter: tiktok|instagram|x|threads"),
    geo: Optional[str] = Query(None, description="Filter: KZ|CIS|World"),
    period: int = Query(30, ge=1, le=365, description="Days since first_seen_at"),
    sort: str = Query("engagement_rate", description="Sort strategy key"),
    limit: int = Query(50, ge=1, le=200),
    include_hidden: bool = Query(False, description="Include posts the user has hidden"),
    unseen_only: bool = Query(False, description="Only never-served posts (+ pinned) — the hard-refresh working set"),
) -> dict[str, Any]:
    """Return ranked digest cards with has_content_bundle + thumbnail + note + flags.

    Hidden posts are excluded by default (the user said "don't show me this").
    With unseen_only=true, also excludes already-served posts (except pinned) —
    this is the rotating working set that hard refresh advances.
    """
    valid_sorts = set(_ranker.ALL_SORTS)
    if sort not in valid_sorts:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort '{sort}'. Valid: {sorted(valid_sorts)}",
        )

    session = get_session()
    try:
        hidden = set() if include_hidden else _storage.flag_ids(session, hidden=True)
        served = _storage.served_ids(session) if unseen_only else set()
        pinned = _storage.flag_ids(session, pinned=True) if unseen_only else set()
        excluded = hidden | (served - pinned)

        # Over-fetch so post-filtering still fills the page.
        cards = _ranker.rank(
            session,
            platform=platform,
            geo_tier=geo,
            period_days=period,
            sort=sort,  # type: ignore[arg-type]
            limit=min(200, limit + len(excluded)),
        )

        if excluded:
            cards = [
                c for c in cards
                if (c["platform"], c["platform_post_id"]) not in excluded
            ]
        cards = cards[:limit]

        for card in cards:
            _attach_card_extras(session, card)

        return {
            "count": len(cards),
            "platform": platform,
            "geo_tier": geo,
            "period_days": period,
            "sort": sort,
            "unseen_only": unseen_only,
            "cards": cards,
        }
    finally:
        session.close()


@app.get("/digest/meta")
def digest_meta(
    platform: Optional[str] = Query(None),
    has_history: bool = Query(False),
) -> dict[str, Any]:
    """Return sort availability — computed from what the corpus actually supports.

    Inspects the DB (snapshots, account post counts, source breadth, follower
    counts) so e.g. relative_baseline is enabled whenever any account has enough
    posts — rather than pessimistically off. `has_history` is kept for backward
    compatibility but no longer drives the result.
    """
    session = get_session()
    try:
        availability = _ranker.real_sort_availability(session, platform)
    finally:
        session.close()
    return {
        "platform": platform,
        "has_history": has_history,
        "sort_availability": availability,
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
            note=_storage.get_note(session, platform, platform_post_id),
            hidden=bool(getattr(session.get(PostFlag, (platform, platform_post_id)), "hidden", False)),
            pinned=bool(getattr(session.get(PostFlag, (platform, platform_post_id)), "pinned", False)),
            collection_ids=_storage.collection_ids_for_post(session, platform, platform_post_id),
        )
    finally:
        session.close()


def _build_cards_for_ids(session, ids: list[tuple[str, str]]) -> list[dict]:
    """Build digest-card-shaped dicts for an explicit ordered list of posts.

    Used by the collection view. Preserves the order of `ids`. Skips posts with
    no snapshot (nothing to render). Reuses _attach_card_extras for parity with
    the /digest cards.
    """
    cards: list[dict] = []
    for plat, pid in ids:
        post = session.get(Post, (plat, pid))
        if post is None:
            continue
        snap = (
            session.query(PostSnapshot)
            .filter_by(platform=plat, platform_post_id=pid)
            .order_by(PostSnapshot.fetched_at.desc())
            .first()
        )
        card = {
            "platform": plat,
            "platform_post_id": pid,
            "account_handle": post.account_handle,
            "url": post.url,
            "caption": post.caption,
            "hashtags": json.loads(post.hashtags or "[]"),
            "sound_id": post.sound_id,
            "sound_name": post.sound_name,
            "media_type": post.media_type,
            "geo_tier": post.geo_tier,
            "thumbnail_path": post.thumbnail_path,
            "posted_at": post.posted_at.isoformat() if post.posted_at else None,
            "first_seen_at": post.first_seen_at.isoformat(),
            "last_seen_at": post.last_seen_at.isoformat(),
            "view_count": snap.view_count if snap else None,
            "like_count": snap.like_count if snap else None,
            "comment_count": snap.comment_count if snap else None,
            "share_count": snap.share_count if snap else None,
            "save_count": snap.save_count if snap else None,
            "author_follower_count": snap.author_follower_count if snap else None,
            "score": None,
            "sort_used": None,
            "sort_requested": None,
            "has_content": snap is not None,
        }
        _attach_card_extras(session, card)
        cards.append(card)
    return cards


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


def _collection_dict(coll: Collection, count: int) -> dict:
    return {
        "id": coll.id,
        "title": coll.title,
        "description": coll.description,
        "item_count": count,
        "created_at": coll.created_at.isoformat() if coll.created_at else None,
        "updated_at": coll.updated_at.isoformat() if coll.updated_at else None,
    }


@app.get("/collections")
def list_collections() -> dict[str, Any]:
    session = get_session()
    try:
        rows = _storage.list_collections(session)
        return {"collections": [_collection_dict(c, n) for c, n in rows]}
    finally:
        session.close()


@app.post("/collections")
def create_collection(body: CollectionCreate) -> dict[str, Any]:
    if not body.title or not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    session = get_session()
    try:
        coll = _storage.create_collection(session, body.title, body.description)
        session.commit()
        return _collection_dict(coll, 0)
    finally:
        session.close()


@app.patch("/collections/{collection_id}")
def patch_collection(collection_id: int, body: CollectionUpdate) -> dict[str, Any]:
    session = get_session()
    try:
        coll = _storage.update_collection(
            session, collection_id, title=body.title, description=body.description
        )
        if coll is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        session.commit()
        count = session.query(CollectionItem).filter_by(collection_id=collection_id).count()
        return _collection_dict(coll, count)
    finally:
        session.close()


@app.delete("/collections/{collection_id}")
def delete_collection(collection_id: int) -> dict[str, Any]:
    session = get_session()
    try:
        ok = _storage.delete_collection(session, collection_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Collection not found")
        session.commit()
        return {"deleted": collection_id}
    finally:
        session.close()


@app.get("/collections/{collection_id}")
def get_collection(collection_id: int) -> dict[str, Any]:
    session = get_session()
    try:
        coll = session.get(Collection, collection_id)
        if coll is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        ids = _storage.collection_post_ids(session, collection_id)
        cards = _build_cards_for_ids(session, ids)
        return {
            **_collection_dict(coll, len(ids)),
            "cards": cards,
        }
    finally:
        session.close()


@app.post("/collections/{collection_id}/items")
def add_collection_item(collection_id: int, body: CollectionItemAdd) -> dict[str, Any]:
    session = get_session()
    try:
        if session.get(Collection, collection_id) is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        if session.get(Post, (body.platform, body.platform_post_id)) is None:
            raise HTTPException(status_code=404, detail="Post not found")
        added = _storage.add_to_collection(
            session, collection_id, body.platform, body.platform_post_id
        )
        session.commit()
        return {"added": added, "collection_id": collection_id}
    finally:
        session.close()


@app.delete("/collections/{collection_id}/items/{platform}/{platform_post_id}")
def remove_collection_item(collection_id: int, platform: str, platform_post_id: str) -> dict[str, Any]:
    session = get_session()
    try:
        removed = _storage.remove_from_collection(
            session, collection_id, platform, platform_post_id
        )
        session.commit()
        return {"removed": removed}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Notes + flags (hide / pin)
# ---------------------------------------------------------------------------


@app.put("/post/{platform}/{platform_post_id}/note")
def put_note(platform: str, platform_post_id: str, body: NoteBody) -> dict[str, Any]:
    session = get_session()
    try:
        if session.get(Post, (platform, platform_post_id)) is None:
            raise HTTPException(status_code=404, detail="Post not found")
        _storage.set_note(session, platform, platform_post_id, body.body)
        session.commit()
        return {"note": _storage.get_note(session, platform, platform_post_id)}
    finally:
        session.close()


@app.delete("/post/{platform}/{platform_post_id}/note")
def delete_note(platform: str, platform_post_id: str) -> dict[str, Any]:
    session = get_session()
    try:
        _storage.set_note(session, platform, platform_post_id, "")
        session.commit()
        return {"note": None}
    finally:
        session.close()


@app.put("/post/{platform}/{platform_post_id}/flags")
def put_flags(platform: str, platform_post_id: str, body: FlagUpdate) -> dict[str, Any]:
    session = get_session()
    try:
        if session.get(Post, (platform, platform_post_id)) is None:
            raise HTTPException(status_code=404, detail="Post not found")
        if body.hidden is not None:
            _storage.set_hidden(session, platform, platform_post_id, body.hidden)
        if body.pinned is not None:
            _storage.set_pinned(session, platform, platform_post_id, body.pinned)
        session.commit()
        flag = session.get(PostFlag, (platform, platform_post_id))
        return {
            "hidden": bool(flag.hidden) if flag else False,
            "pinned": bool(flag.pinned) if flag else False,
        }
    finally:
        session.close()


def _load_ingest():
    """Load core/ingest.py fresh (so refresh picks up edits without an API restart)."""
    import importlib.util
    script = str(_ROOT / "core" / "ingest.py")
    spec = importlib.util.spec_from_file_location("ingest_run", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_hard_refresh(job_id: str, req: HardRefreshRequest) -> None:
    """Hard/selective refresh worker.

    1. Mark the outgoing (currently-shown, non-pinned) posts as served.
    2. If source=live, harvest brand-new posts from the adapters.
    3. Recycle least-recently-seen posts if the unseen pool can't fill the page.
    4. Compute the next unseen working set (exclude hidden+served, keep pinned).
    5. Enrich that set so it comes back with full media.
    The frontend then reloads GET /digest?unseen_only=true.
    """
    job = _refresh_jobs[job_id]
    job["status"] = "running"
    session = get_session()
    try:
        pinned = _storage.flag_ids(session, pinned=True)
        outgoing = [
            (p, i) for p, i in (tuple(x) for x in req.serve_ids)
            if (p, i) not in pinned
        ]
        _storage.mark_served(session, outgoing)
        session.commit()

        ingest = _load_ingest()

        live_summary = None
        if req.source == "live":
            live_summary = ingest.harvest_live(per_platform=req.live_per_platform)

        # Recycle oldest-served if the unseen pool can't fill the requested page.
        unseen = _storage.count_unseen_eligible(session)
        recycled = 0
        if unseen < req.limit:
            recycled = _storage.recycle_oldest_served(session, req.limit - unseen)
            session.commit()

        # Next unseen working set.
        hidden = _storage.flag_ids(session, hidden=True)
        served = _storage.served_ids(session)
        excluded = hidden | (served - pinned)
        cards = _ranker.rank(
            session,
            platform=req.platform,
            geo_tier=req.geo,
            period_days=req.period,
            sort=req.sort,  # type: ignore[arg-type]
            limit=min(200, req.limit + len(excluded)),
        )
        batch = [
            (c["platform"], c["platform_post_id"]) for c in cards
            if (c["platform"], c["platform_post_id"]) not in excluded
        ][: req.limit]

        # Enrich the working set so it returns with full media.
        enrich_summary = ingest._call_enrichment(batch)

        job["status"] = "done"
        job["summary"] = {
            "source": req.source,
            "served_out": len(outgoing),
            "recycled": recycled,
            "working_set": len(batch),
            "enrichment": enrich_summary,
            "live": live_summary,
        }
    except Exception as exc:
        import traceback
        traceback.print_exc()
        job["status"] = "error"
        job["error"] = str(exc)
    finally:
        job["finished_at"] = time.time()
        session.close()


@app.post("/refresh/hard", response_model=RefreshResponse)
def refresh_hard(req: HardRefreshRequest):
    """Background-trigger a hard/selective refresh. Poll GET /refresh/status/{job_id}."""
    job_id = str(uuid.uuid4())[:8]
    _refresh_jobs[job_id] = {
        "status": "queued",
        "started_at": time.time(),
        "finished_at": None,
        "summary": None,
        "error": None,
    }
    import threading
    threading.Thread(target=_run_hard_refresh, args=(job_id, req), daemon=True).start()
    return RefreshResponse(
        job_id=job_id,
        status="queued",
        message=f"Hard refresh ({req.source}) queued. Poll GET /refresh/status/{job_id}",
    )


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
