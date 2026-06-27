"""FastAPI digest API — READ-ONLY over SQLite.

Endpoints
---------
GET    /digest                              Ranked digest cards (filtered + sorted; unseen_only/include_hidden).
GET    /digest/meta                         Sort availability matrix (DB-backed) for given platform.
GET    /post/{platform}/{platform_post_id}  Full Content Bundle for one post (incl. note + flags).
GET    /post/{plat}/{pid}/snapshots         Engagement time series + velocity (powers the stats graph).
POST   /post/{plat}/{pid}/resnapshot        Re-observe the post live now; append a snapshot + return series.
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
import threading
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
from core import songs as _songs
from core import storage as _storage
from core.storage import (
    Collection,
    CollectionItem,
    Post,
    PostContent,
    PostFlag,
    PostNote,
    PostSnapshot,
    SongFlag,
    Sound,
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
    # Multi-platform selection (e.g. ["tiktok","x"]); overrides `platform`. None/empty
    # or all four = every platform. Scopes both the live harvest and the working set.
    platforms: Optional[list[str]] = None
    geo: Optional[str] = None
    period: int = 30
    sort: str = "engagement_rate"
    limit: int = 50
    # Target NEW posts per platform for a live harvest. 500 = a full corpus sweep
    # (slow + ban-heavier, esp. IG/Threads — runs in the background); lower it for a
    # quick top-up. Best-effort: bounded by available handles × per-handle depth.
    live_per_platform: int = 500


class SongFlagUpdate(BaseModel):
    platform: str
    key: str
    hidden: Optional[bool] = None
    pinned: Optional[bool] = None


class SongHardRefreshRequest(BaseModel):
    """Song-list analogue of HardRefreshRequest (see /songs/refresh/hard)."""

    source: Literal["corpus", "live"] = "corpus"
    # [[platform, song_key], ...] currently-shown songs to rotate out (mark served).
    # Pinned songs in this list are skipped (they stay).
    serve_keys: list[list[str]] = []
    platform: Optional[str] = None  # tiktok | instagram | None (both)
    geo: Optional[str] = None
    period: int = 30
    sort: str = _songs.DEFAULT_SONG_SORT
    limit: int = 60
    live_per_platform: int = 500
    # Pivot trending/corpus sounds for authoritative reuse counts (the "reused most"
    # path). Runs on live refresh; also usable on corpus refresh to backfill counts.
    pivot_sounds: bool = True
    sounds_per_platform: int = 8
    videos_per_sound: int = 20


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


_VALID_PLATFORMS = {"tiktok", "instagram", "x", "threads"}


def _parse_platforms(value: Optional[object]) -> Optional[set[str]]:
    """Normalize a platform selection into a lowercase set, or None for 'all'.

    Accepts a comma-string ('tiktok,x') or a list (['tiktok','x']). An empty or
    full selection collapses to None so callers take the unscoped 'all' path.
    """
    if value is None:
        return None
    if isinstance(value, str):
        items = [p.strip().lower() for p in value.split(",") if p.strip()]
    else:
        items = [str(p).strip().lower() for p in value if str(p).strip()]
    sel = {p for p in items if p in _VALID_PLATFORMS}
    if not sel or sel == _VALID_PLATFORMS:
        return None
    return sel


@app.get("/digest")
def digest(
    platform: Optional[str] = Query(None, description="Filter: tiktok|instagram|x|threads"),
    platforms: Optional[str] = Query(None, description="Multi-filter: comma-list e.g. 'tiktok,x' (overrides platform)"),
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

    `platforms` (comma-list) selects a subset of platforms; when set it overrides
    `platform`. A single value uses the fast platform-scoped query; a subset ranks
    across all and post-filters.
    """
    valid_sorts = set(_ranker.ALL_SORTS)
    if sort not in valid_sorts:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort '{sort}'. Valid: {sorted(valid_sorts)}",
        )

    plat_set = _parse_platforms(platforms)
    # Single-platform fast path: push the filter into the query.
    scope_platform = platform
    if plat_set is not None:
        scope_platform = next(iter(plat_set)) if len(plat_set) == 1 else None

    session = get_session()
    try:
        hidden = set() if include_hidden else _storage.flag_ids(session, hidden=True)
        served = _storage.served_ids(session) if unseen_only else set()
        pinned = _storage.flag_ids(session, pinned=True) if unseen_only else set()
        excluded = hidden | (served - pinned)

        # Over-fetch so post-filtering (excluded + multi-platform) still fills the page.
        cards = _ranker.rank(
            session,
            platform=scope_platform,
            geo_tier=geo,
            period_days=period,
            sort=sort,  # type: ignore[arg-type]
            limit=min(200, limit + len(excluded)),
        )

        if plat_set is not None and len(plat_set) > 1:
            cards = [c for c in cards if c["platform"] in plat_set]
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
            "platforms": sorted(plat_set) if plat_set is not None else None,
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


# ---------------------------------------------------------------------------
# Songs (viral sounds) — TikTok + Instagram only
# ---------------------------------------------------------------------------


def _song_scope_platform(platform: Optional[str]) -> Optional[str]:
    """Normalize a song platform filter to tiktok|instagram, or None for both."""
    if platform and platform.lower() in _songs.SONG_PLATFORMS:
        return platform.lower()
    return None


def _attach_song_extras(session, song: dict) -> dict:
    """Attach cover thumbnail, sound_author, and hidden/pinned flags to a song dict."""
    plat = song["platform"]
    key = song["key"]
    top_id = song.get("top_platform_post_id")

    thumbnail = None
    if top_id:
        content = session.get(PostContent, (plat, top_id))
        post_row = session.get(Post, (plat, top_id))
        thumbnail = _resolve_thumbnail(session, plat, top_id, content, post_row)
        # Prefer a known sound_author from the top post's bundle.
        if content and content.sound_author and not song.get("sound_author"):
            song["sound_author"] = content.sound_author
    # Fall back to the authoritative Sound row's cover art (from the pivot) when no
    # post thumbnail is available — keeps the song card alive even with no media yet.
    song["thumbnail"] = thumbnail or song.get("cover_url")

    flag = session.get(SongFlag, (plat, key))
    song["hidden"] = bool(flag.hidden) if flag else False
    song["pinned"] = bool(flag.pinned) if flag else False
    return song


@app.get("/songs")
def songs(
    platform: Optional[str] = Query(None, description="tiktok|instagram (omit = both)"),
    geo: Optional[str] = Query(None, description="KZ|CIS|World"),
    period: int = Query(30, ge=1, le=365, description="Days since first_seen_at"),
    sort: str = Query(_songs.DEFAULT_SONG_SORT, description="Song ranking key"),
    limit: int = Query(60, ge=1, le=200),
    include_hidden: bool = Query(False, description="Include songs the user hid"),
    unseen_only: bool = Query(False, description="Only never-served songs (+ pinned)"),
) -> dict[str, Any]:
    """Ranked list of viral songs (per-platform), with every ranking metric attached.

    Mirrors /digest: hidden songs excluded by default; unseen_only yields the rotating
    working set that the song hard refresh advances.
    """
    if sort not in _songs.ALL_SONG_SORTS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid song sort '{sort}'. Valid: {sorted(_songs.ALL_SONG_SORTS)}",
        )
    scope_platform = _song_scope_platform(platform)

    session = get_session()
    try:
        hidden = set() if include_hidden else _storage.song_flag_ids(session, hidden=True)
        served = _storage.served_song_keys(session) if unseen_only else set()
        pinned = _storage.song_flag_ids(session, pinned=True) if unseen_only else set()
        exclude_keys = hidden | (served - pinned)

        rows = _songs.rank_songs(
            session,
            platform=scope_platform,
            geo_tier=geo,
            period_days=period,
            sort=sort,  # type: ignore[arg-type]
            limit=limit,
            exclude_keys=exclude_keys,
            pinned_keys=pinned,
        )
        for song in rows:
            _attach_song_extras(session, song)

        return {
            "count": len(rows),
            "platform": scope_platform,
            "geo_tier": geo,
            "period_days": period,
            "sort": sort,
            "unseen_only": unseen_only,
            "all_sorts": list(_songs.ALL_SONG_SORTS),
            "default_sort": _songs.DEFAULT_SONG_SORT,
            "songs": rows,
        }
    finally:
        session.close()


@app.get("/song")
def song_detail(
    platform: str = Query(..., description="tiktok|instagram"),
    key: str = Query(..., description="song key (sound id or name:<...>)"),
    geo: Optional[str] = Query(None),
    period: int = Query(30, ge=1, le=365),
    sort: str = Query(_songs.DEFAULT_SONG_SORT),
) -> dict[str, Any]:
    """One song's aggregate + the posts that use it, as digest cards."""
    scope_platform = _song_scope_platform(platform)
    if scope_platform is None:
        raise HTTPException(status_code=400, detail="platform must be tiktok or instagram")
    if sort not in _songs.ALL_SONG_SORTS:
        sort = _songs.DEFAULT_SONG_SORT

    session = get_session()
    try:
        meta = _songs.song_meta(
            session, scope_platform, key, period_days=period, geo_tier=geo, sort=sort
        )
        if meta is None:
            raise HTTPException(status_code=404, detail="Song not found in this window")
        _attach_song_extras(session, meta)

        ranked = _songs.song_post_ids(
            session, scope_platform, key, period_days=period, geo_tier=geo
        )
        ids = [(p, i) for (p, i, _r) in ranked]
        rate_by_id = {(p, i): r for (p, i, r) in ranked}
        cards = _build_cards_for_ids(session, ids)
        for card in cards:
            cid = (card["platform"], card["platform_post_id"])
            card["score"] = rate_by_id.get(cid)
            card["sort_used"] = "engagement_rate"
            card["sort_requested"] = "engagement_rate"

        return {"song": meta, "cards": cards}
    finally:
        session.close()


@app.put("/song/flags")
def put_song_flags(body: SongFlagUpdate) -> dict[str, Any]:
    """Set hidden/pinned for a song (drives the song-list refresh working set)."""
    scope_platform = _song_scope_platform(body.platform)
    if scope_platform is None:
        raise HTTPException(status_code=400, detail="platform must be tiktok or instagram")
    session = get_session()
    try:
        if body.hidden is not None:
            _storage.set_song_hidden(session, scope_platform, body.key, body.hidden)
        if body.pinned is not None:
            _storage.set_song_pinned(session, scope_platform, body.key, body.pinned)
        session.commit()
        flag = session.get(SongFlag, (scope_platform, body.key))
        return {
            "hidden": bool(flag.hidden) if flag else False,
            "pinned": bool(flag.pinned) if flag else False,
        }
    finally:
        session.close()


def _count_unseen_songs(
    session, *, platform: Optional[str], geo: Optional[str], period: int,
    served: set, hidden: set, pinned: set,
) -> int:
    """How many in-window songs have never been served (and aren't hidden)."""
    aggs = _songs.aggregate_songs(
        session, platform=platform, geo_tier=geo, period_days=period
    )
    blocked = (served | hidden) - pinned
    return sum(1 for k in aggs.keys() if k not in blocked)


def _run_song_hard_refresh(job_id: str, req: SongHardRefreshRequest) -> None:
    """Hard/selective refresh for the song list (mirror of _run_hard_refresh).

    1. Mark outgoing (shown, non-pinned) songs served.
    2. If source=live, harvest brand-new posts (new posts surface new songs).
    3. Recycle least-recently-seen songs if the unseen pool can't fill the page.
    The frontend then reloads GET /songs?unseen_only=true.
    """
    job = _refresh_jobs[job_id]
    job["status"] = "running"
    session = get_session()
    try:
        scope_platform = _song_scope_platform(req.platform)
        plat_set = {scope_platform} if scope_platform else set(_songs.SONG_PLATFORMS)

        pinned = _storage.song_flag_ids(session, pinned=True)
        outgoing = [
            (p, k) for p, k in (tuple(x) for x in req.serve_keys)
            if (p, k) not in pinned
        ]
        _storage.mark_songs_served(session, outgoing)
        session.commit()

        live_summary = None
        if req.source == "live":
            ingest = _load_ingest()
            live_platforms = sorted(plat_set)
            print(
                f"[song-refresh {job_id}] live harvest start "
                f"platforms={live_platforms} per_platform={req.live_per_platform}"
            )
            live_summary = ingest.harvest_live(
                platforms=live_platforms, per_platform=req.live_per_platform
            )

        # Pivot sounds for authoritative reuse counts (the "reused most" signal).
        # This is a LIVE operation (drives the TikTok browser / IG burner per sound),
        # so it runs ONLY on a live refresh. A corpus refresh stays instant — it just
        # rotates the working set over data we already have (counts were filled by an
        # earlier live refresh). Bounded by sounds_per_platform so it terminates.
        sound_summary = None
        if req.pivot_sounds and req.source == "live":
            try:
                from core import sound_harvest
                print(
                    f"[song-refresh {job_id}] sound pivot start "
                    f"platforms={sorted(plat_set)} max={req.sounds_per_platform}"
                )
                sound_summary = sound_harvest.harvest_sounds(
                    platforms=sorted(plat_set),
                    max_sounds_per_platform=req.sounds_per_platform,
                    videos_per_sound=req.videos_per_sound,
                    include_trending=True,
                    include_corpus=True,
                )
            except Exception as exc:
                import traceback as _tb
                _tb.print_exc()
                sound_summary = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}

        # Recycle oldest-served songs if the unseen pool can't fill the page.
        hidden = _storage.song_flag_ids(session, hidden=True)
        served = _storage.served_song_keys(session)
        unseen = _count_unseen_songs(
            session, platform=scope_platform, geo=req.geo, period=req.period,
            served=served, hidden=hidden, pinned=pinned,
        )
        recycled = 0
        if unseen < req.limit:
            recycled = _storage.recycle_oldest_served_songs(
                session, req.limit - unseen, platforms=plat_set
            )
            session.commit()

        job["status"] = "done"
        job["summary"] = {
            "source": req.source,
            "served_out": len(outgoing),
            "recycled": recycled,
            "live": live_summary,
            "sounds": sound_summary,
        }
    except Exception as exc:
        import traceback
        traceback.print_exc()
        job["status"] = "error"
        job["error"] = str(exc)
    finally:
        job["finished_at"] = time.time()
        session.close()


@app.post("/songs/refresh/hard", response_model=RefreshResponse)
def refresh_songs_hard(req: SongHardRefreshRequest):
    """Background-trigger a hard/selective song refresh. Poll GET /refresh/status/{job_id}."""
    job_id = str(uuid.uuid4())[:8]
    _refresh_jobs[job_id] = {
        "status": "queued",
        "started_at": time.time(),
        "finished_at": None,
        "summary": None,
        "error": None,
    }
    threading.Thread(target=_run_song_hard_refresh, args=(job_id, req), daemon=True).start()
    return RefreshResponse(
        job_id=job_id,
        status="queued",
        message=f"Song hard refresh ({req.source}) queued. Poll GET /refresh/status/{job_id}",
    )


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


class SnapshotPoint(BaseModel):
    fetched_at: str
    view_count: Optional[int]
    like_count: Optional[int]
    comment_count: Optional[int]
    share_count: Optional[int]
    save_count: Optional[int]
    author_follower_count: Optional[int]
    source: Optional[str]


class SnapshotSeriesResponse(BaseModel):
    platform: str
    platform_post_id: str
    points: list[SnapshotPoint]
    # Δ(views|likes)/hour between the two most recent snapshots (ranker's velocity).
    velocity_per_hour: Optional[float]
    velocity_metric: Optional[str]  # "views" | "likes" | None


def _build_snapshot_series(
    session, platform: str, platform_post_id: str
) -> SnapshotSeriesResponse:
    """Assemble the snapshot time series + velocity for one post (raises 404 if none)."""
    snaps = (
        session.query(PostSnapshot)
        .filter_by(platform=platform, platform_post_id=platform_post_id)
        .order_by(PostSnapshot.fetched_at.asc())
        .all()
    )
    if not snaps:
        raise HTTPException(
            status_code=404,
            detail=f"No snapshots for {platform}/{platform_post_id}",
        )

    points = [
        SnapshotPoint(
            fetched_at=s.fetched_at.isoformat(),
            view_count=s.view_count,
            like_count=s.like_count,
            comment_count=s.comment_count,
            share_count=s.share_count,
            save_count=s.save_count,
            author_follower_count=s.author_follower_count,
            source=s.source,
        )
        for s in snaps
    ]

    velocity = _ranker._score_velocity(session, platform, platform_post_id)
    metric: Optional[str] = None
    if velocity is not None and len(snaps) >= 2:
        a, b = snaps[-2], snaps[-1]
        metric = "views" if (a.view_count is not None and b.view_count is not None) else "likes"

    return SnapshotSeriesResponse(
        platform=platform,
        platform_post_id=platform_post_id,
        points=points,
        velocity_per_hour=velocity,
        velocity_metric=metric,
    )


@app.get(
    "/post/{platform}/{platform_post_id}/snapshots",
    response_model=SnapshotSeriesResponse,
)
def get_post_snapshots(platform: str, platform_post_id: str) -> SnapshotSeriesResponse:
    """Time series of a post's engagement snapshots (oldest→newest) + velocity.

    Each snapshot is one re-observation of the post; the series is what powers the
    in-lightbox stats graph. `velocity_per_hour` mirrors the ranker's velocity sort
    (Δ between the latest two points). A single snapshot yields a flat series and a
    null velocity — that's expected until the post is re-observed.
    """
    session = get_session()
    try:
        return _build_snapshot_series(session, platform, platform_post_id)
    finally:
        session.close()


class ResnapshotResponse(BaseModel):
    # updated = a fresh point was appended; not_found = post not in author's recent
    # feed anymore; error = adapter/fetch failure (error carries the reason).
    status: Literal["updated", "not_found", "error"]
    error: Optional[str] = None
    fetched: int = 0
    series: SnapshotSeriesResponse


# Coalesce concurrent re-observations of the same post (live fetch is expensive).
_resnap_lock = threading.Lock()
_resnap_inflight: set[tuple[str, str]] = set()


@app.post(
    "/post/{platform}/{platform_post_id}/resnapshot",
    response_model=ResnapshotResponse,
)
def resnapshot_post(platform: str, platform_post_id: str) -> ResnapshotResponse:
    """Re-observe this post LIVE right now, append a snapshot, return the new series.

    Synchronous (the user pressed "fetch fresh data" and is waiting): re-fetches the
    author's recent posts via the adapter, finds this post, and appends a fresh
    `post_snapshots` row with current metrics — adding a new point to the stats graph.
    `status=not_found` means the post has scrolled out of the author's recent feed.
    """
    key = (platform, platform_post_id)
    with _resnap_lock:
        if key in _resnap_inflight:
            # Someone else is already refreshing this post — just return current series.
            session = get_session()
            try:
                return ResnapshotResponse(
                    status="updated",
                    fetched=0,
                    series=_build_snapshot_series(session, platform, platform_post_id),
                )
            finally:
                session.close()
        _resnap_inflight.add(key)

    try:
        ingest = _load_ingest()
        result = ingest.resnapshot_post(platform, platform_post_id)
        session = get_session()
        try:
            series = _build_snapshot_series(session, platform, platform_post_id)
        finally:
            session.close()
        return ResnapshotResponse(
            status=result.get("status", "error"),
            error=result.get("error"),
            fetched=result.get("fetched", 0),
            series=series,
        )
    finally:
        with _resnap_lock:
            _resnap_inflight.discard(key)


# Serialize on-demand enrichment so two viewers opening the same post (or many
# posts at once) don't trigger duplicate downloads / hammer the source.
_enrich_lock = threading.Lock()
_enrich_inflight: set[tuple[str, str]] = set()


@app.post("/post/{platform}/{platform_post_id}/enrich", response_model=ContentBundleResponse)
def enrich_post(platform: str, platform_post_id: str, force: bool = Query(False)) -> ContentBundleResponse:
    """Priority-enrich a single post on demand (when the user opens its detail).

    Synchronous: downloads the Content Bundle for just this post and returns the
    fresh bundle with media. Idempotent — if already enriched it returns
    immediately (use force=true to re-download). This is how deep posts that were
    never in a top-N pass get their media the moment a user opens them.
    """
    session = get_session()
    try:
        if session.get(Post, (platform, platform_post_id)) is None:
            raise HTTPException(status_code=404, detail=f"Post {platform}/{platform_post_id} not found")
        existing = session.get(PostContent, (platform, platform_post_id))
        already = existing is not None and existing.status == "done"
        if force and existing is not None:
            session.delete(existing)
            session.commit()
            already = False
    finally:
        session.close()

    key = (platform, platform_post_id)
    if not already:
        # Coalesce concurrent requests for the same post.
        with _enrich_lock:
            mine = key not in _enrich_inflight
            if mine:
                _enrich_inflight.add(key)
        if mine:
            try:
                ingest = _load_ingest()
                ingest._call_enrichment([key])
            except Exception as exc:  # non-fatal — fall through to whatever exists
                print(f"[api] on-demand enrich {platform}:{platform_post_id} failed: {exc}")
            finally:
                with _enrich_lock:
                    _enrich_inflight.discard(key)
        else:
            # Another request is enriching this post; wait briefly for it.
            for _ in range(60):
                time.sleep(0.5)
                with _enrich_lock:
                    if key not in _enrich_inflight:
                        break

    return get_post(platform, platform_post_id)


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

        # Resolve the platform scope (multi-select wins; fall back to single).
        plat_set = _parse_platforms(req.platforms)
        if plat_set is None and req.platform:
            plat_set = _parse_platforms([req.platform]) or {req.platform.lower()}

        live_summary = None
        if req.source == "live":
            # Scope the live harvest to the selected platforms (so "hard refresh
            # Instagram" pulls only IG), else harvest all four.
            live_platforms = sorted(plat_set) if plat_set else None
            print(
                f"[hard-refresh {job_id}] live harvest start "
                f"platforms={live_platforms or 'all'} per_platform={req.live_per_platform}"
            )
            live_summary = ingest.harvest_live(
                platforms=live_platforms, per_platform=req.live_per_platform
            )
            parsed_total = sum(p.get("fetched", 0) for p in live_summary.values())
            new_total = sum(p.get("new", 0) for p in live_summary.values())
            print(
                f"[hard-refresh {job_id}] live harvest done: "
                f"parsed={parsed_total} new={new_total} detail={live_summary}"
            )

        # Recycle oldest-served if the unseen pool can't fill the requested page.
        # Scope the measurement + recycle to the selected platforms so a scoped
        # refresh fills from its own pool, not unrelated platforms'.
        unseen = _storage.count_unseen_eligible(session, platforms=plat_set)
        recycled = 0
        if unseen < req.limit:
            recycled = _storage.recycle_oldest_served(
                session, req.limit - unseen, platforms=plat_set
            )
            session.commit()

        # Next unseen working set.
        hidden = _storage.flag_ids(session, hidden=True)
        served = _storage.served_ids(session)
        excluded = hidden | (served - pinned)
        scope_platform = next(iter(plat_set)) if plat_set and len(plat_set) == 1 else None
        cards = _ranker.rank(
            session,
            platform=scope_platform,
            geo_tier=req.geo,
            period_days=req.period,
            sort=req.sort,  # type: ignore[arg-type]
            limit=min(200, req.limit + len(excluded)),
        )
        batch = [
            (c["platform"], c["platform_post_id"]) for c in cards
            if (c["platform"], c["platform_post_id"]) not in excluded
            and (plat_set is None or c["platform"] in plat_set)
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


def _safe_filename(name: str) -> str:
    """A filesystem/Content-Disposition-safe base name from a sound title."""
    import re as _re
    base = _re.sub(r"[^\w\-. ]+", "", (name or "sound")).strip() or "sound"
    return base[:80]


@app.get("/song/audio")
def song_audio(
    platform: str = Query(..., description="tiktok|instagram"),
    key: str = Query(..., description="song key (sound id or name:<...>)"),
):
    """Download a song's audio as a file.

    Serves the cached audio if the sound was pivoted (pre-downloaded then), else
    fetches it from the stored ``play_url`` on demand and caches it. 404 when we
    have no audio source for the sound yet (pivot it first via a live song refresh).
    """
    scope_platform = _song_scope_platform(platform)
    if scope_platform is None:
        raise HTTPException(status_code=400, detail="platform must be tiktok or instagram")

    from core import sound_harvest

    cached = sound_harvest.cached_audio_file(scope_platform, key)
    title = None
    if cached is None:
        session = get_session()
        try:
            sound = session.get(Sound, (scope_platform, key))
            play_url = sound.play_url if sound else None
            title = sound.title if sound else None
        finally:
            session.close()
        cached = sound_harvest.download_sound_audio(play_url, scope_platform, key)
        if cached is None:
            raise HTTPException(
                status_code=404,
                detail="No audio for this sound yet. Run a live song refresh to pivot it.",
            )
    else:
        session = get_session()
        try:
            sound = session.get(Sound, (scope_platform, key))
            title = sound.title if sound else None
        finally:
            session.close()

    import mimetypes
    media_type = mimetypes.guess_type(str(cached))[0] or "application/octet-stream"
    download_name = f"{_safe_filename(title or key)}{cached.suffix}"
    return FileResponse(str(cached), media_type=media_type, filename=download_name)


# ---------------------------------------------------------------------------
# Single-page app (production / Docker)
# ---------------------------------------------------------------------------
# Serve the built Vite frontend from the same origin as the API so the whole
# product is one container on one port (the frontend uses relative API URLs).
# Mounted LAST, after every API route, so "/" never shadows an API endpoint.
# No-op in local dev where web/dist doesn't exist (Vite serves the UI itself).
_WEB_DIST = _ROOT / "web" / "dist"
if _WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=str(_WEB_DIST), html=True), name="spa")
