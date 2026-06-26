"""Per-platform extraction of content fields from a raw payload.

Each ``extract_*`` function reads the stored ``raw`` dict (the complete
platform payload preserved at scrape time) and returns an ``Extraction``
dataclass with all the fields that go into a ``post_content`` row plus a
list of ``MediaItem`` objects describing what to download.

NO understanding happens here — pure field extraction only. The callers
decide what to do with the URLs (download, record expired, etc.).

Supported platforms
-------------------
* tiktok   — raw is the TikTok ``item_list`` API item object (signed path).
             yt-dlp fallback items are also handled (different shape).
* threads  — raw is the Threads GraphQL post node (Meta private API).
* instagram — raw is the instagrapi private-API item (``/api/v1/...``).

See enrichment/README.md for the complete per-platform field map.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

@dataclass
class MediaItem:
    """One file to download as part of a Content Bundle."""
    url: str
    filename: str             # suggested filename under the post's media dir
    headers: dict[str, str] = field(default_factory=dict)
    # If True, the url/headers approach is unlikely to work; caller should
    # use yt-dlp with the provided yt_dlp_url (TikTok video signing).
    fallback_yt_dlp: bool = False
    yt_dlp_url: Optional[str] = None  # e.g. "https://www.tiktok.com/@handle/video/id"


@dataclass
class Extraction:
    """Everything extracted from a raw payload for one post."""
    platform: str
    platform_post_id: str
    caption: Optional[str]
    spoiler_text: Optional[str]        # None unless platform exposes it (Threads CW)
    sound_id: Optional[str]
    sound_name: Optional[str]
    sound_author: Optional[str]
    author_display_name: Optional[str]
    media_items: list[MediaItem]       # all files to download (video(s), images, cover)
    thumbnail_item: Optional[MediaItem]  # the cover/thumbnail to download for every post


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _safe_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s not in ("0", "None") else None


def _first_url(candidates: list[dict]) -> Optional[str]:
    """Return url from the first non-empty candidates entry."""
    for c in (candidates or []):
        if isinstance(c, dict):
            u = c.get("url") or c.get("download_url")
            if u:
                return str(u)
    return None


def _image_url_from_iv2(node: dict) -> Optional[str]:
    """Extract best image URL from an ``image_versions2`` dict (IG/Threads/Meta)."""
    iv2 = node.get("image_versions2") or {}
    return _first_url(iv2.get("candidates") or []) if isinstance(iv2, dict) else None


def _video_url_from_versions(node: dict) -> Optional[str]:
    """Extract video URL from ``video_versions`` list (IG/Threads/Meta)."""
    versions = node.get("video_versions") or []
    return _first_url(versions)


def _meta_music(raw: dict) -> dict:
    """Return the music_asset_info dict from a Meta (IG/Threads) clips_metadata."""
    clips = raw.get("clips_metadata") or {}
    if not isinstance(clips, dict):
        return {}
    mi = clips.get("music_info") or {}
    if isinstance(mi, dict):
        return mi.get("music_asset_info") or {}
    return {}


_TIKTOK_CDN_HEADERS = {
    "Referer": "https://www.tiktok.com/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# TikTok
# ---------------------------------------------------------------------------

def _is_tiktokapi_item(raw: dict) -> bool:
    """True if raw looks like a TikTok-Api item_list object (has 'video' or 'imagePost')."""
    return ("video" in raw or "imagePost" in raw) and "author" in raw


def _tiktok_best_h264_url(video: dict) -> Optional[str]:
    """Highest-bitrate H.264 (avc) variant URL from a TikTok ``video.bitrateInfo``.

    TikTok serves multiple codec variants; the highest-quality one is usually
    H.265/HEVC (``CodecType`` like ``h265_hvc1``), which browsers can't decode.
    We deliberately pick H.264 (``h264`` / ``avc``) even if it's lower-resolution,
    because a playable file beats an unplayable one. Returns None when no H.264
    variant exists (caller falls back to playAddr/downloadAddr, then yt-dlp).
    """
    bitrate_info = video.get("bitrateInfo") or video.get("bitrate_info") or []
    candidates: list[tuple[int, str]] = []
    for b in bitrate_info:
        if not isinstance(b, dict):
            continue
        codec = str(b.get("CodecType") or b.get("codec_type") or "").lower()
        if not (codec.startswith("h264") or "avc" in codec):
            continue
        play_addr = b.get("PlayAddr") or b.get("play_addr") or {}
        urls = play_addr.get("UrlList") or play_addr.get("url_list") or []
        if urls:
            try:
                bitrate = int(b.get("Bitrate") or b.get("bitrate") or 0)
            except (TypeError, ValueError):
                bitrate = 0
            candidates.append((bitrate, str(urls[-1])))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


def extract_tiktok(raw: dict, platform_post_id: str) -> Extraction:
    """Extract content fields from a TikTok raw item.

    Two raw shapes are handled:
    - TikTok-Api ``item_list`` object (primary path, rich): has ``video``,
      ``music``, ``author``, ``imagePost``.
    - yt-dlp flat entry (fallback, thinner): has ``webpage_url``, ``uploader``,
      ``formats`` (or just ``url``).
    """
    if _is_tiktokapi_item(raw):
        return _extract_tiktok_api(raw, platform_post_id)
    return _extract_tiktok_ytdlp(raw, platform_post_id)


def _extract_tiktok_api(raw: dict, platform_post_id: str) -> Extraction:
    caption = _safe_str(raw.get("desc"))
    author = raw.get("author") or {}
    author_display = _safe_str(author.get("nickname")) or _safe_str(author.get("uniqueId"))

    music = raw.get("music") or {}
    sound_id_raw = music.get("id")
    # TikTok uses "0" as a no-music placeholder
    sound_id = _safe_str(sound_id_raw) if sound_id_raw not in (None, "0", 0) else None
    sound_name = _safe_str(music.get("title"))
    sound_author = _safe_str(music.get("authorName"))

    media_items: list[MediaItem] = []
    thumbnail_item: Optional[MediaItem] = None

    image_post = raw.get("imagePost")
    if image_post and isinstance(image_post, dict):
        # Image/carousel post — images list
        images = image_post.get("images") or []
        for i, img in enumerate(images):
            # TikTok imagePost image shape: {imageURL: {urlList: [...]}, thumbnails: {urlList: [...]}}
            img_url = None
            img_url_obj = img.get("imageURL") or {}
            if isinstance(img_url_obj, dict):
                url_list = img_url_obj.get("urlList") or img_url_obj.get("url_list") or []
                if url_list:
                    img_url = str(url_list[0])
            if not img_url:
                img_url = img.get("url") or img.get("download_url")
            if img_url:
                media_items.append(MediaItem(
                    url=img_url,
                    filename=f"image_{i:02d}.jpg",
                    headers=_TIKTOK_CDN_HEADERS,
                ))
        # Cover from first image's thumbnail (or video.cover if present)
        video = raw.get("video") or {}
        cover_url = _safe_str(video.get("cover") or video.get("originCover"))
        if not cover_url and images:
            thumb_obj = images[0].get("thumbnails") or {}
            if isinstance(thumb_obj, dict):
                tl = thumb_obj.get("urlList") or thumb_obj.get("url_list") or []
                if tl:
                    cover_url = str(tl[0])
        if cover_url:
            thumbnail_item = MediaItem(
                url=cover_url, filename="cover.jpg", headers=_TIKTOK_CDN_HEADERS
            )
    else:
        # Video post
        video = raw.get("video") or {}
        download_addr = _safe_str(video.get("downloadAddr"))
        play_addr = _safe_str(video.get("playAddr"))
        # Prefer an H.264 (avc) variant: TikTok's default playAddr/downloadAddr is
        # often H.265/HEVC, which most browsers (esp. Chrome on Linux) cannot decode
        # — the <video> tag then plays only the audio track. Pick the highest-bitrate
        # H.264 variant from bitrateInfo so the file is universally playable.
        h264_url = _tiktok_best_h264_url(video)
        video_url = h264_url or download_addr or play_addr

        handle = _safe_str(author.get("uniqueId")) or "unknown"
        yt_dlp_url = f"https://www.tiktok.com/@{handle}/video/{platform_post_id}"

        if video_url:
            media_items.append(MediaItem(
                url=video_url,
                filename="video.mp4",
                headers=_TIKTOK_CDN_HEADERS,
                fallback_yt_dlp=True,   # TikTok video URLs often 403 without signing
                yt_dlp_url=yt_dlp_url,
            ))
        else:
            # No URL in raw — yt-dlp only
            media_items.append(MediaItem(
                url="",
                filename="video.mp4",
                headers={},
                fallback_yt_dlp=True,
                yt_dlp_url=yt_dlp_url,
            ))

        cover_url = (
            _safe_str(video.get("cover"))
            or _safe_str(video.get("originCover"))
            or _safe_str(video.get("dynamicCover"))
        )
        if cover_url:
            thumbnail_item = MediaItem(
                url=cover_url, filename="cover.jpg", headers=_TIKTOK_CDN_HEADERS
            )

    return Extraction(
        platform="tiktok",
        platform_post_id=platform_post_id,
        caption=caption,
        spoiler_text=None,         # TikTok has no spoiler/CW feature
        sound_id=sound_id,
        sound_name=sound_name,
        sound_author=sound_author,
        author_display_name=author_display,
        media_items=media_items,
        thumbnail_item=thumbnail_item,
    )


def _extract_tiktok_ytdlp(raw: dict, platform_post_id: str) -> Extraction:
    """Extract from a yt-dlp flat-playlist entry (thinner data, no stable IDs)."""
    caption = _safe_str(raw.get("description") or raw.get("title"))
    author_display = _safe_str(raw.get("uploader") or raw.get("channel"))
    sound_name = _safe_str(raw.get("track"))

    handle = _safe_str(raw.get("uploader_id") or raw.get("uploader") or "unknown")
    yt_dlp_url = (
        raw.get("webpage_url")
        or f"https://www.tiktok.com/@{handle}/video/{platform_post_id}"
    )

    # yt-dlp entries don't have signed download URLs we can reliably GET;
    # always use the yt-dlp fallback for the actual file.
    media_items = [MediaItem(
        url="",
        filename="video.mp4",
        headers={},
        fallback_yt_dlp=True,
        yt_dlp_url=yt_dlp_url,
    )]

    thumbnail_url = _safe_str(raw.get("thumbnail"))
    if not thumbnail_url:
        thumbs = raw.get("thumbnails") or []
        if thumbs:
            thumbnail_url = _safe_str(thumbs[-1].get("url") if isinstance(thumbs[-1], dict) else thumbs[-1])
    thumbnail_item = MediaItem(
        url=thumbnail_url or "",
        filename="cover.jpg",
        headers=_TIKTOK_CDN_HEADERS,
    ) if thumbnail_url else None

    return Extraction(
        platform="tiktok",
        platform_post_id=platform_post_id,
        caption=caption,
        spoiler_text=None,
        sound_id=None,          # not available in yt-dlp flat entries
        sound_name=sound_name,
        sound_author=None,
        author_display_name=author_display,
        media_items=media_items,
        thumbnail_item=thumbnail_item,
    )


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

def extract_threads(raw: dict, platform_post_id: str) -> Extraction:
    """Extract content fields from a Threads GraphQL post node.

    The node is the complete Barcelona/Meta post shape as intercepted from the
    Threads profile-feed GraphQL response. It shares Meta's private-API field
    naming (same as IG): ``image_versions2``, ``video_versions``, ``carousel_media``,
    ``clips_metadata``, ``user``, ``caption``.

    Spoiler (tap-to-reveal): Threads marks these with the boolean
    ``text_post_app_info.is_spoiler_media`` (verified against live data 2026-06-26).
    The earlier-guessed fields ``post_text_hidden_content_type`` /
    ``is_content_warning`` do NOT exist in the real payload and never fired. The
    caption text itself is always in ``caption.text``; the spoiler is purely a UI
    gate over the media (and caption). We surface ``spoiler_text`` (non-None) so the
    specimen viewer can blur-to-reveal.
    """
    caption_obj = raw.get("caption") or {}
    caption_text = (
        _safe_str(caption_obj.get("text")) if isinstance(caption_obj, dict) else None
    )

    # Spoiler / tap-to-reveal detection — the real live field is is_spoiler_media.
    tpa = raw.get("text_post_app_info") or {}
    spoiler_text: Optional[str] = None
    if isinstance(tpa, dict) and tpa.get("is_spoiler_media") is True:
        # The gated content is the caption (media is still downloaded). Fall back to
        # a marker for media-only spoilers so has_spoiler stays True downstream.
        spoiler_text = caption_text or "(spoiler-hidden media)"

    user = raw.get("user") or {}
    author_display = (
        _safe_str(user.get("full_name"))
        or _safe_str(user.get("username"))
    )

    # Music (rare on Threads but present on video posts that use a licensed track)
    music_asset = _meta_music(raw)
    sound_id = _safe_str(music_asset.get("audio_cluster_id") or music_asset.get("id"))
    sound_name = _safe_str(music_asset.get("title"))
    sound_author = _safe_str(music_asset.get("display_artist"))

    media_items: list[MediaItem] = []
    thumbnail_item: Optional[MediaItem] = None

    carousel = raw.get("carousel_media") or []
    if carousel and isinstance(carousel, list):
        for i, slide in enumerate(carousel):
            if not isinstance(slide, dict):
                continue
            vid_url = _video_url_from_versions(slide)
            img_url = _image_url_from_iv2(slide)
            if vid_url:
                media_items.append(MediaItem(url=vid_url, filename=f"slide_{i:02d}.mp4"))
            elif img_url:
                media_items.append(MediaItem(url=img_url, filename=f"slide_{i:02d}.jpg"))
        # Thumbnail = first slide's image
        first_slide = carousel[0] if carousel else {}
        thumb_url = _image_url_from_iv2(first_slide) if isinstance(first_slide, dict) else None
        if thumb_url:
            thumbnail_item = MediaItem(url=thumb_url, filename="cover.jpg")
    else:
        vid_url = _video_url_from_versions(raw)
        img_url = _image_url_from_iv2(raw)
        if vid_url:
            media_items.append(MediaItem(url=vid_url, filename="video.mp4"))
        if img_url:
            if not vid_url:
                media_items.append(MediaItem(url=img_url, filename="image.jpg"))
            thumbnail_item = MediaItem(url=img_url, filename="cover.jpg")

    return Extraction(
        platform="threads",
        platform_post_id=platform_post_id,
        caption=caption_text,
        spoiler_text=spoiler_text,
        sound_id=sound_id,
        sound_name=sound_name,
        sound_author=sound_author,
        author_display_name=author_display,
        media_items=media_items,
        thumbnail_item=thumbnail_item,
    )


# ---------------------------------------------------------------------------
# Instagram
# ---------------------------------------------------------------------------

def extract_instagram(raw: dict, platform_post_id: str) -> Extraction:
    """Extract content fields from an instagrapi private-API media item.

    The raw item is the complete untouched ``/api/v1/feed/user/{id}/`` item
    (~97-122 keys). It shares Meta's field naming with Threads (same backend).

    Extra keys produced by the IG adapter's post-processing live under:
    - ``raw['_normalized_extra']``: ``audio_id``, ``audio_name``, ``is_reel``, etc.
    - ``raw['_account_info']``: full user/account payload (one call per account).
    """
    # Caption
    caption_obj = raw.get("caption") or {}
    if isinstance(caption_obj, dict):
        caption_text = _safe_str(caption_obj.get("text"))
    else:
        caption_text = _safe_str(raw.get("caption_text"))

    user = raw.get("user") or {}
    account_info = raw.get("_account_info") or {}
    author_display = (
        _safe_str(user.get("full_name"))
        or _safe_str((account_info.get("user") or account_info).get("full_name"))
        or _safe_str(user.get("username"))
    )

    # Music: try clips_metadata first (reels), then _normalized_extra fallback
    music_asset = _meta_music(raw)
    if music_asset:
        sound_id = _safe_str(music_asset.get("audio_cluster_id") or music_asset.get("id"))
        sound_name = _safe_str(music_asset.get("title"))
        sound_author = _safe_str(music_asset.get("display_artist"))
    else:
        extra = raw.get("_normalized_extra") or {}
        sound_id = _safe_str(extra.get("audio_id"))
        sound_name = _safe_str(extra.get("audio_name"))
        sound_author = None

    media_items: list[MediaItem] = []
    thumbnail_item: Optional[MediaItem] = None

    carousel = raw.get("carousel_media") or []
    if carousel and isinstance(carousel, list):
        for i, slide in enumerate(carousel):
            if not isinstance(slide, dict):
                continue
            vid_url = _video_url_from_versions(slide)
            img_url = _image_url_from_iv2(slide)
            if vid_url:
                media_items.append(MediaItem(url=vid_url, filename=f"slide_{i:02d}.mp4"))
            elif img_url:
                media_items.append(MediaItem(url=img_url, filename=f"slide_{i:02d}.jpg"))
        # Thumbnail = first slide's image (or the parent's image_versions2)
        first_img = _image_url_from_iv2(carousel[0]) if carousel else None
        parent_img = _image_url_from_iv2(raw)
        thumb_url = first_img or parent_img
        if thumb_url:
            thumbnail_item = MediaItem(url=thumb_url, filename="cover.jpg")
    else:
        vid_url = _video_url_from_versions(raw)
        img_url = _image_url_from_iv2(raw)
        if vid_url:
            media_items.append(MediaItem(url=vid_url, filename="video.mp4"))
        if img_url:
            if not vid_url:
                media_items.append(MediaItem(url=img_url, filename="image.jpg"))
            thumbnail_item = MediaItem(url=img_url, filename="cover.jpg")

    return Extraction(
        platform="instagram",
        platform_post_id=platform_post_id,
        caption=caption_text,
        spoiler_text=None,         # Instagram has no spoiler/CW feature
        sound_id=sound_id,
        sound_name=sound_name,
        sound_author=sound_author,
        author_display_name=author_display,
        media_items=media_items,
        thumbnail_item=thumbnail_item,
    )


# ---------------------------------------------------------------------------
# X (Twitter)
# ---------------------------------------------------------------------------

def _x_tweet_node(raw: dict) -> dict:
    """Return the legacy-shaped tweet dict from either X raw shape.

    The X adapter stores two shapes (adapters/x/adapter.py):
    - syndication path: ``raw`` IS the tweet dict.
    - GraphQL path: the tweet fields live under ``raw['legacy']``.
    """
    legacy = raw.get("legacy")
    return legacy if isinstance(legacy, dict) else raw


def _x_author_display(raw: dict, tweet: dict) -> Optional[str]:
    user = tweet.get("user") or {}
    name = _safe_str(user.get("name")) or _safe_str(user.get("screen_name"))
    if name:
        return name
    # GraphQL: the user lives under core.user_results.result
    core = (raw.get("core") or {}).get("user_results", {}).get("result", {})
    core_legacy = core.get("legacy") or {}
    return (
        _safe_str(core_legacy.get("name"))
        or _safe_str((core.get("core") or {}).get("name"))
        or _safe_str(core_legacy.get("screen_name"))
    )


def _x_inner_result(result: dict) -> dict:
    """Unwrap a tweet ``result`` node, handling the TweetWithVisibilityResults wrapper."""
    if not isinstance(result, dict):
        return {}
    if isinstance(result.get("tweet"), dict):  # TweetWithVisibilityResults
        return result["tweet"]
    return result


def _x_media_list(legacy: dict) -> list:
    """Media array from a legacy tweet node (extended_entities preferred)."""
    if not isinstance(legacy, dict):
        return []
    return (
        (legacy.get("extended_entities") or {}).get("media")
        or (legacy.get("entities") or {}).get("media")
        or []
    )


def _x_media_carrier(raw: dict, top: dict) -> tuple[dict, dict, list]:
    """Resolve which tweet actually carries the media.

    Retweets and quote tweets nest the real content under
    ``retweeted_status_result`` / ``quoted_status_result``. The top-level tweet's
    own ``extended_entities`` is empty in that case, so descend into the embedded
    tweet. Returns ``(carrier_result_node, carrier_legacy, media_list)``; the
    result node is needed for author resolution.
    """
    media_list = _x_media_list(top)
    if media_list:
        return raw, top, media_list
    for key in ("retweeted_status_result", "quoted_status_result"):
        rtr = top.get(key) or raw.get(key)
        if not isinstance(rtr, dict):
            continue
        inner = _x_inner_result(rtr.get("result"))
        inner_legacy = inner.get("legacy") if isinstance(inner, dict) else None
        cand = _x_media_list(inner_legacy or {})
        if cand:
            return inner, inner_legacy, cand
    return raw, top, []


def _x_best_video_url(video_info: dict) -> Optional[str]:
    """Highest-bitrate mp4 variant from a tweet media ``video_info``."""
    variants = (video_info or {}).get("variants") or []
    mp4s = [
        v for v in variants
        if isinstance(v, dict) and v.get("content_type") == "video/mp4" and v.get("url")
    ]
    if mp4s:
        best = max(mp4s, key=lambda v: v.get("bitrate") or 0)
        return str(best["url"])
    any_url = [v for v in variants if isinstance(v, dict) and v.get("url")]
    return str(any_url[0]["url"]) if any_url else None


def extract_x(raw: dict, platform_post_id: str) -> Extraction:
    """Extract content from an X (Twitter) raw payload.

    Handles both stored shapes (syndication tweet dict and GraphQL result node).
    Media lives in ``extended_entities.media`` (photos -> ``media_url_https``;
    video / animated_gif -> ``video_info.variants``, highest-bitrate mp4). The
    photo URL doubles as the video poster. X has no sound or spoiler/CW feature.
    """
    tweet = _x_tweet_node(raw)
    # Retweets / quote tweets carry their media in an embedded tweet node.
    carrier_raw, carrier_legacy, media_list = _x_media_carrier(raw, tweet)
    caption = _safe_str(
        carrier_legacy.get("full_text") or carrier_legacy.get("text")
    ) or _safe_str(tweet.get("full_text") or tweet.get("text"))
    author_display = _x_author_display(carrier_raw, carrier_legacy)
    multi = len(media_list) > 1

    media_items: list[MediaItem] = []
    thumbnail_item: Optional[MediaItem] = None
    for i, m in enumerate(media_list):
        if not isinstance(m, dict):
            continue
        poster = _safe_str(m.get("media_url_https"))
        if m.get("type") in ("video", "animated_gif"):
            vurl = _x_best_video_url(m.get("video_info") or {})
            if vurl:
                media_items.append(MediaItem(
                    url=vurl, filename=f"video_{i:02d}.mp4" if multi else "video.mp4",
                ))
            if poster and thumbnail_item is None:
                thumbnail_item = MediaItem(url=poster, filename="cover.jpg")
        else:  # photo
            if poster:
                media_items.append(MediaItem(
                    url=poster, filename=f"image_{i:02d}.jpg" if multi else "image.jpg",
                ))
                if thumbnail_item is None:
                    thumbnail_item = MediaItem(url=poster, filename="cover.jpg")

    return Extraction(
        platform="x",
        platform_post_id=platform_post_id,
        caption=caption,
        spoiler_text=None,          # X has no spoiler/CW feature
        sound_id=None,              # X has no sound concept
        sound_name=None,
        sound_author=None,
        author_display_name=author_display,
        media_items=media_items,
        thumbnail_item=thumbnail_item,
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_EXTRACTORS = {
    "tiktok": extract_tiktok,
    "threads": extract_threads,
    "instagram": extract_instagram,
    "x": extract_x,
}


def extract(platform: str, raw: dict, platform_post_id: str) -> Extraction:
    """Dispatch extraction to the correct per-platform function.

    Raises ``ValueError`` for unsupported platforms.
    """
    fn = _EXTRACTORS.get(platform)
    if fn is None:
        raise ValueError(
            f"No extractor for platform {platform!r}. "
            f"Supported: {sorted(_EXTRACTORS)}"
        )
    return fn(raw, platform_post_id)
