"""Media file downloader for the enrichment layer.

Two strategies:
1. Direct CDN GET — used for all non-TikTok-video media (images, covers,
   Instagram/Threads video) and attempted first for TikTok.
2. yt-dlp fallback — used for TikTok video files that require signing
   (the CDN URLs are signed but TikTok often returns 403 on direct GET
   without the JS-generated X-Bogus/X-Gnarly tokens present in the
   browser session). Only invoked when ``MediaItem.fallback_yt_dlp=True``
   and the direct GET fails.

On an already-expired URL (any HTTP ≥ 400 that is clearly a CDN
not-found/expiry, OR when the URL is empty/None), this module raises
``ExpiredUrlError`` — the caller records ``status=expired_url_miss`` and
moves on WITHOUT re-fetching (per CORE-SPINE § Enrichment rules).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

from enrichment.field_maps import MediaItem

# Status codes that signal an expired/gone CDN URL (do not retry)
_EXPIRED_CODES = frozenset({401, 403, 404, 410})

# Default timeout for CDN GET requests (generous — video files can be large)
_DEFAULT_TIMEOUT = (10, 120)   # (connect, read) seconds


class ExpiredUrlError(RuntimeError):
    """Raised when a CDN URL is expired, missing, or explicitly gone.

    The caller must record ``status=expired_url_miss`` in post_content.
    """


class DownloadError(RuntimeError):
    """Raised on a non-expiry download failure (network error, unexpected
    HTTP code, disk error). Unlike ExpiredUrlError, these may be transient.
    """


def _looks_expired(url: str) -> bool:
    """Heuristic pre-check: is the URL's embedded expiry timestamp already past?

    Handles:
    - TikTok CDN: ``x-expires=<unix_ts>`` query param
    - Meta/FB CDN: ``oe=<hex_unix_ts>`` query param (Instagram/Threads)
    """
    if not url:
        return True
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        # TikTok: x-expires
        if "x-expires" in qs:
            exp = int(qs["x-expires"][0])
            return exp < int(time.time())
        # Meta/FB CDN: oe (hex)
        if "oe" in qs:
            exp = int(qs["oe"][0], 16)
            return exp < int(time.time())
    except (ValueError, KeyError, IndexError):
        pass
    return False


def download_item(
    item: MediaItem,
    dest_dir: Path,
    *,
    skip_if_exists: bool = True,
    timeout: tuple[int, int] = _DEFAULT_TIMEOUT,
) -> Path:
    """Download a single MediaItem to ``dest_dir / item.filename``.

    Returns the absolute path of the downloaded file.

    Raises:
        ExpiredUrlError  — URL is empty, pre-expired, or returned ≥400.
        DownloadError    — Network / IO failure for a seemingly live URL.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / item.filename

    if skip_if_exists and dest_path.exists() and dest_path.stat().st_size > 0:
        return dest_path

    if not item.url:
        if item.fallback_yt_dlp and item.yt_dlp_url:
            return _download_via_ytdlp(item.yt_dlp_url, dest_path)
        raise ExpiredUrlError(f"Empty URL for {item.filename} — no yt-dlp fallback configured")

    if _looks_expired(item.url):
        if item.fallback_yt_dlp and item.yt_dlp_url:
            # For TikTok: even if the stored URL is expired, yt-dlp can re-sign
            return _download_via_ytdlp(item.yt_dlp_url, dest_path)
        raise ExpiredUrlError(
            f"URL for {item.filename} is expired (pre-check): {item.url[:80]}..."
        )

    # --- Attempt direct CDN GET ---
    try:
        resp = requests.get(
            item.url,
            headers=item.headers,
            stream=True,
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        if item.fallback_yt_dlp and item.yt_dlp_url:
            return _download_via_ytdlp(item.yt_dlp_url, dest_path)
        raise DownloadError(f"Network error downloading {item.filename}: {exc}") from exc

    if resp.status_code in _EXPIRED_CODES:
        if item.fallback_yt_dlp and item.yt_dlp_url:
            return _download_via_ytdlp(item.yt_dlp_url, dest_path)
        raise ExpiredUrlError(
            f"CDN returned HTTP {resp.status_code} for {item.filename} — treating as expired. "
            f"URL: {item.url[:80]}..."
        )

    if not resp.ok:
        raise DownloadError(
            f"Unexpected HTTP {resp.status_code} downloading {item.filename}"
        )

    # Stream to disk
    try:
        tmp = dest_path.with_suffix(dest_path.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
        tmp.rename(dest_path)
    except OSError as exc:
        raise DownloadError(f"IO error writing {dest_path}: {exc}") from exc

    return dest_path


def download_thumbnail(
    thumbnail_url: Optional[str],
    dest_dir: Path,
    filename: str = "cover.jpg",
    headers: Optional[dict] = None,
    *,
    skip_if_exists: bool = True,
) -> Optional[Path]:
    """Download a thumbnail image to ``dest_dir / filename``.

    Returns the path on success, None if URL is empty/expired (graceful —
    thumbnails are best-effort; a missing thumbnail never raises).
    """
    if not thumbnail_url:
        return None
    item = MediaItem(url=thumbnail_url, filename=filename, headers=headers or {})
    try:
        return download_item(item, dest_dir, skip_if_exists=skip_if_exists)
    except (ExpiredUrlError, DownloadError):
        return None


def _download_via_ytdlp(source_url: str, dest_path: Path) -> Path:
    """Download a TikTok video using yt-dlp, which handles signing internally.

    yt-dlp is invoked as a subprocess so we don't need to import it at module
    level (it lives in adapters/tiktok/requirements.txt; the venv already has
    it but callers outside the tiktok adapter needn't depend on it).

    Raises:
        ExpiredUrlError — if yt-dlp reports the video is unavailable/gone.
        DownloadError   — on other yt-dlp failures.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    # yt-dlp outtmpl: we want exactly dest_path (strip extension — yt-dlp adds it)
    outtmpl = str(dest_path.with_suffix(""))

    import sys
    ytdlp_bin = Path(sys.executable).parent / "yt-dlp"
    if not ytdlp_bin.exists():
        ytdlp_bin = "yt-dlp"   # rely on PATH

    cmd = [
        str(ytdlp_bin),
        "--quiet",
        "--no-warnings",
        "--no-playlist",
        # Prefer H.264 (avc) over H.265/HEVC — browsers (esp. Chrome on Linux)
        # can't decode HEVC, so an HEVC <video> plays audio only. -S makes vcodec
        # the primary sort key so "best" resolves to the highest-res H.264 variant.
        "-S", "vcodec:h264",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "-o", outtmpl + ".%(ext)s",
        source_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        if any(
            phrase in stderr
            for phrase in ("video unavailable", "not available", "removed", "private video", "does not exist")
        ):
            raise ExpiredUrlError(
                f"yt-dlp: video unavailable or removed: {source_url}"
            )
        raise DownloadError(
            f"yt-dlp failed (code {result.returncode}) for {source_url}: {result.stderr[:300]}"
        )

    # yt-dlp may have written with a different extension (.webm, .mp4, etc.)
    # Find the file it wrote (outtmpl base + any extension)
    base = Path(outtmpl)
    for candidate in sorted(base.parent.glob(base.name + ".*")):
        if candidate.suffix not in (".tmp", ".part", ".ytdl"):
            # Rename to the requested dest_path (preserve .mp4 convention)
            if candidate != dest_path:
                candidate.rename(dest_path)
            return dest_path

    raise DownloadError(f"yt-dlp succeeded but output file not found near {outtmpl!r}")
