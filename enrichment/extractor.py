"""Layer-3 enrichment entry points.

Public API
----------
``enrich(post_identities, ...)``
    The main function. For each (platform, post_id) that is not already in
    ``post_content``, reads its stored ``raw``, extracts content fields
    (field_maps), downloads every media file (downloader), and writes one
    ``post_content`` row via the storage interface.

``download_thumbnail(post, ...)``
    Helper the ingestion process calls for EVERY captured post (not just top-N).
    Downloads the post's thumbnail image to ``data/media/<platform>/<id>/``
    and records the path. Idempotent (skips if already on disk).

Integration
-----------
Both functions accept optional ``storage=`` and ``raw_reader=`` keyword
arguments so the spine agent can wire in the real ``core.storage`` once it
lands, without changing the function signatures:

    from core import storage as core_storage
    enrich(ids, storage=core_storage, raw_reader=core_storage.get_post_raw)

Until then, the defaults use the local stub (``enrichment._stub_storage``) and
a ``raw_reader`` that looks up the stub's ``raw_cache``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from enrichment.downloader import ExpiredUrlError, DownloadError, download_item, download_thumbnail as _dl_thumb
from enrichment.field_maps import extract, MediaItem

logger = logging.getLogger(__name__)

# Root directory under which all media lives: data/media/<platform>/<post_id>/
_DEFAULT_MEDIA_ROOT = Path("data/media")


# ---------------------------------------------------------------------------
# Storage protocol (structural — no ABC import needed)
# ---------------------------------------------------------------------------

class _StorageProtocol:
    """Documentation-only protocol — any object with these methods will work."""
    def is_in_post_content(self, platform: str, post_id: str) -> bool: ...
    def write_post_content(self, **kwargs) -> None: ...
    def set_thumbnail_path(self, platform: str, post_id: str, path: str) -> None: ...
    def get_raw(self, platform: str, post_id: str) -> Optional[dict]: ...


def _get_default_storage() -> Any:
    """Return the default stub storage (lazy import to avoid circular refs)."""
    from enrichment._stub_storage import make_stub
    return make_stub()


# ---------------------------------------------------------------------------
# enrich()
# ---------------------------------------------------------------------------

def enrich(
    post_identities: list[tuple[str, str]],
    *,
    raw_reader: Optional[Callable[[str, str], Optional[dict]]] = None,
    storage: Optional[Any] = None,
    media_root: Optional[Path] = None,
    skip_if_exists: bool = True,
) -> None:
    """Download the full Content Bundle for each listed post.

    Parameters
    ----------
    post_identities:
        List of (platform, platform_post_id) tuples identifying the top-N posts
        to enrich. YouTube is not supported and will be skipped with a warning.
    raw_reader:
        Callable (platform, post_id) -> dict | None that returns the stored raw
        payload for a post. If None, falls back to ``storage.get_raw()``.
    storage:
        Duck-typed object implementing the StorageProtocol above. If None, the
        local stub (``enrichment._stub_storage.StubStorage``) is used.
    media_root:
        Root directory for media files. Defaults to ``data/media/``.
    skip_if_exists:
        If True (default), skip posts already present in ``post_content``.
        Set to False to force re-enrichment (will not re-download existing files
        on disk, but will overwrite the post_content row).
    """
    storage = storage or _get_default_storage()
    media_root = media_root or _DEFAULT_MEDIA_ROOT

    if raw_reader is None:
        def raw_reader(platform: str, post_id: str) -> Optional[dict]:
            fn = getattr(storage, "get_raw", None)
            return fn(platform, post_id) if fn else None

    for platform, post_id in post_identities:
        if platform == "youtube":
            logger.warning("Skipping YouTube post %s — excluded from enrichment", post_id)
            continue
        if platform not in ("tiktok", "instagram", "threads", "x"):
            logger.warning("Skipping unsupported platform %r for post %s", platform, post_id)
            continue

        if skip_if_exists and storage.is_in_post_content(platform, post_id):
            logger.debug("Already enriched: %s:%s — skipping", platform, post_id)
            continue

        raw = raw_reader(platform, post_id)
        if raw is None:
            logger.warning(
                "No raw payload found for %s:%s — recording expired_url_miss",
                platform, post_id,
            )
            _write_miss(storage, platform, post_id)
            continue

        _enrich_one(platform, post_id, raw, storage=storage, media_root=media_root)


def _enrich_one(
    platform: str,
    post_id: str,
    raw: dict,
    *,
    storage: Any,
    media_root: Path,
) -> None:
    """Download all media and write one post_content row for a single post."""
    try:
        extraction = extract(platform, raw, post_id)
    except Exception as exc:
        logger.error("Extraction failed for %s:%s: %s", platform, post_id, exc)
        _write_miss(storage, platform, post_id)
        return

    dest_dir = media_root / platform / post_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    media_paths: list[str] = []
    any_expired = False
    any_error = False

    for item in extraction.media_items:
        try:
            path = download_item(item, dest_dir)
            media_paths.append(str(path))
            logger.info("  ✓ %s:%s — %s", platform, post_id, item.filename)
        except ExpiredUrlError as exc:
            logger.warning("  ✗ expired: %s:%s — %s: %s", platform, post_id, item.filename, exc)
            any_expired = True
        except DownloadError as exc:
            logger.warning("  ✗ error: %s:%s — %s: %s", platform, post_id, item.filename, exc)
            any_error = True

    # Thumbnail
    if extraction.thumbnail_item:
        try:
            thumb_path = download_item(extraction.thumbnail_item, dest_dir)
            storage.set_thumbnail_path(platform, post_id, str(thumb_path))
        except (ExpiredUrlError, DownloadError) as exc:
            logger.warning("  thumbnail download failed for %s:%s: %s", platform, post_id, exc)

    # Status: done only if at least something was downloaded (or no media to download)
    if not extraction.media_items:
        status = "done"      # text-only post (e.g. Threads text post)
    elif any_expired and not media_paths:
        status = "expired_url_miss"
    elif any_error and not media_paths:
        status = "expired_url_miss"   # treat hard errors like a miss for now
    else:
        status = "done"

    storage.write_post_content(
        platform=platform,
        platform_post_id=post_id,
        media_paths=media_paths,
        caption=extraction.caption,
        spoiler_text=extraction.spoiler_text,
        sound_id=extraction.sound_id,
        sound_name=extraction.sound_name,
        sound_author=extraction.sound_author,
        author_display_name=extraction.author_display_name,
        extracted_at=datetime.now(timezone.utc),
        status=status,
    )
    logger.info(
        "post_content written: %s:%s status=%s media_count=%d",
        platform, post_id, status, len(media_paths),
    )


def _write_miss(storage: Any, platform: str, post_id: str) -> None:
    storage.write_post_content(
        platform=platform,
        platform_post_id=post_id,
        media_paths=[],
        caption=None,
        spoiler_text=None,
        sound_id=None,
        sound_name=None,
        sound_author=None,
        author_display_name=None,
        extracted_at=datetime.now(timezone.utc),
        status="expired_url_miss",
    )


# ---------------------------------------------------------------------------
# download_thumbnail()
# ---------------------------------------------------------------------------

def download_thumbnail(
    post: Any,
    *,
    storage: Optional[Any] = None,
    media_root: Optional[Path] = None,
    skip_if_exists: bool = True,
) -> Optional[str]:
    """Download the thumbnail for a post and record the path in storage.

    Called for EVERY captured post at ingestion (not just top-N).
    Uses ``post.thumbnail_url`` (normalized field) or falls back to extracting
    from ``post.raw``.

    Parameters
    ----------
    post:
        A ``PostRecord`` (or any object with .platform, .platform_post_id,
        .thumbnail_url, .raw attributes).
    storage:
        Optional storage object. If provided, calls ``set_thumbnail_path``.
    media_root:
        Root directory. Defaults to ``data/media/``.

    Returns the relative path string, or None on failure.
    """
    storage = storage or _get_default_storage()
    media_root = media_root or _DEFAULT_MEDIA_ROOT

    platform = post.platform
    post_id = post.platform_post_id
    dest_dir = media_root / platform / post_id

    # Check if already recorded in storage
    existing = None
    get_fn = getattr(storage, "get_thumbnail_path", None)
    if get_fn:
        existing = get_fn(platform, post_id)
    if existing and Path(existing).exists() and skip_if_exists:
        return existing

    # Prefer the normalized thumbnail_url field; fall back to raw extraction
    thumbnail_url: Optional[str] = getattr(post, "thumbnail_url", None)
    headers: dict = {}

    if not thumbnail_url:
        raw = getattr(post, "raw", {}) or {}
        if raw:
            try:
                extraction = extract(platform, raw, post_id)
                if extraction.thumbnail_item:
                    thumbnail_url = extraction.thumbnail_item.url
                    headers = extraction.thumbnail_item.headers
            except Exception:
                pass

    if not thumbnail_url:
        return None

    path = _dl_thumb(
        thumbnail_url,
        dest_dir,
        filename="cover.jpg",
        headers=headers,
        skip_if_exists=skip_if_exists,
    )
    if path:
        rel = str(path)
        if storage:
            storage.set_thumbnail_path(platform, post_id, rel)
        return rel
    return None
