"""Tests covering the three Layer-3 enrichment escalations.

Escalation #1 — Threads raw={} guard
  - _guard_nonempty_raw rejects a PostRecord with empty raw.
  - A non-empty raw PostRecord passes the guard and is persisted.
  - The harvest loop skips (not silently stores) empty-raw rows.

Escalation #2 — TikTok imagePost URL extraction
  Defensive unit test against a saved fixture of the documented
  ``imagePost.images[*].imageURL.urlList[0]`` shape.

Escalation #3 — TikTok direct-GET → yt-dlp fallback
  Simulate a direct CDN GET failure (HTTP 403) and confirm
  ``downloader.download_item`` cleanly falls back to yt-dlp.

All tests run offline (no network, no Playwright).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make repo root importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.threads.harvest_accumulate import _guard_nonempty_raw, _post_to_row
from core.schema import PostRecord, WatchedAccount
from enrichment.downloader import (
    ExpiredUrlError,
    DownloadError,
    download_item,
)
from enrichment.field_maps import MediaItem, extract_tiktok, extract_x, extract_threads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_post(raw: dict, post_id: str = "TEST_001") -> PostRecord:
    """Create a minimal PostRecord for testing."""
    return PostRecord(
        platform="threads",
        platform_post_id=post_id,
        account_handle="test_account",
        url=f"https://www.threads.com/@test_account/post/{post_id}",
        fetched_at=datetime.now(timezone.utc),
        media_type="text",
        raw=raw,
        like_count=100,
    )


# ---------------------------------------------------------------------------
# Escalation #1 — empty-raw guard
# ---------------------------------------------------------------------------

class TestEmptyRawGuard:
    """#1: _guard_nonempty_raw must reject empty/falsy raw."""

    def test_empty_raw_dict_rejected(self):
        """A PostRecord with raw={} must fail the guard."""
        post = _make_post(raw={})
        assert _guard_nonempty_raw(post) is False

    def test_nonempty_raw_passes(self):
        """A PostRecord with a populated raw dict must pass the guard."""
        post = _make_post(raw={"pk": "123", "code": "abc", "like_count": 42})
        assert _guard_nonempty_raw(post) is True

    def test_harvest_loop_skips_empty_raw(self):
        """The harvest main-loop pattern must NOT persist empty-raw rows.

        This mirrors the guard inserted in harvest_accumulate.main():
            if not _guard_nonempty_raw(p):
                continue
            posts_by_id[p.platform_post_id] = _post_to_row(p)
        """
        posts_by_id: dict = {}
        posts = [
            _make_post(raw={}, post_id="EMPTY_001"),
            _make_post(raw={}, post_id="EMPTY_002"),
            _make_post(raw={"pk": "333", "code": "xyz"}, post_id="REAL_003"),
        ]
        skipped = 0
        for p in posts:
            if p.platform_post_id not in posts_by_id:
                if not _guard_nonempty_raw(p):
                    skipped += 1
                    continue
                posts_by_id[p.platform_post_id] = _post_to_row(p)

        assert skipped == 2, "Both empty-raw posts must be skipped"
        assert "EMPTY_001" not in posts_by_id
        assert "EMPTY_002" not in posts_by_id
        assert "REAL_003" in posts_by_id, "Non-empty raw post must be persisted"

    def test_post_to_row_preserves_raw(self):
        """_post_to_row must write the full raw dict (no truncation)."""
        raw_payload = {
            "pk": "9876",
            "code": "abc123",
            "like_count": 50,
            "caption": {"text": "hello"},
        }
        post = _make_post(raw=raw_payload)
        row = _post_to_row(post)
        assert row["raw"] == raw_payload, "_post_to_row must preserve the complete raw dict"


# ---------------------------------------------------------------------------
# Escalation #2 — TikTok imagePost URL extraction
# ---------------------------------------------------------------------------

# Documented TikTok imagePost shape (from API schema + enrichment/README.md).
# This fixture mirrors what TikTok returns for a photo/carousel post.
_TIKTOK_IMAGE_POST_RAW = {
    "id": "7651999999999999999",
    "desc": "Check out these study tips! #studytips #education",
    "author": {
        "id": "user123",
        "uniqueId": "studygrammer",
        "nickname": "Study Gram",
        "avatarThumb": "https://example.tiktok.com/avatar.jpg",
    },
    "music": {
        "id": "7002000000000001234",
        "title": "Study Beats",
        "authorName": "Lo-Fi Artist",
    },
    "imagePost": {
        "images": [
            {
                "imageURL": {
                    "urlList": [
                        "https://p16-sign.tiktokcdn-us.com/img/slide0_full.jpg~tplv-example.jpg",
                        "https://p16-sign.tiktokcdn-us.com/img/slide0_small.jpg~tplv-example.jpg",
                    ]
                },
                "thumbnails": {
                    "urlList": [
                        "https://p16-sign.tiktokcdn-us.com/img/slide0_thumb.jpg~tplv-example.jpg",
                    ]
                },
            },
            {
                "imageURL": {
                    "urlList": [
                        "https://p16-sign.tiktokcdn-us.com/img/slide1_full.jpg~tplv-example.jpg",
                    ]
                },
                "thumbnails": {
                    "urlList": [
                        "https://p16-sign.tiktokcdn-us.com/img/slide1_thumb.jpg~tplv-example.jpg",
                    ]
                },
            },
            {
                # Third slide: flat url (alternate shape sometimes observed)
                "url": "https://p16-sign.tiktokcdn-us.com/img/slide2_direct.jpg~tplv-example.jpg",
                "imageURL": {},
            },
        ],
        "coverImage": None,
    },
    # imagePost items often still carry a video stub with a cover URL
    "video": {
        "cover": "https://p16-sign.tiktokcdn-us.com/img/carousel_cover.jpg~tplv-example.jpg",
    },
    "stats": {
        "diggCount": 9876,
        "shareCount": 432,
        "commentCount": 88,
        "playCount": 0,
    },
}


class TestTikTokImagePostExtraction:
    """#2: imagePost URL path in field_maps.extract_tiktok (defensive fixture test)."""

    def test_returns_extraction(self):
        """extract_tiktok on an imagePost raw must return an Extraction object."""
        ext = extract_tiktok(_TIKTOK_IMAGE_POST_RAW, "7651999999999999999")
        assert ext is not None
        assert ext.platform == "tiktok"

    def test_caption_extracted(self):
        ext = extract_tiktok(_TIKTOK_IMAGE_POST_RAW, "7651999999999999999")
        assert "study tips" in (ext.caption or "").lower()

    def test_sound_id_extracted(self):
        ext = extract_tiktok(_TIKTOK_IMAGE_POST_RAW, "7651999999999999999")
        assert ext.sound_id == "7002000000000001234"
        assert ext.sound_name == "Study Beats"
        assert ext.sound_author == "Lo-Fi Artist"

    def test_image_media_items_present(self):
        """imagePost should produce >=1 MediaItem for the slides."""
        ext = extract_tiktok(_TIKTOK_IMAGE_POST_RAW, "7651999999999999999")
        assert len(ext.media_items) >= 1, "Expected at least one image media item"

    def test_first_image_url_is_correct_path(self):
        """First image must use imageURL.urlList[0] (the documented primary path)."""
        ext = extract_tiktok(_TIKTOK_IMAGE_POST_RAW, "7651999999999999999")
        filenames = [m.filename for m in ext.media_items]
        assert "image_00.jpg" in filenames

        first = next(m for m in ext.media_items if m.filename == "image_00.jpg")
        assert "slide0_full" in first.url, (
            f"Expected slide0_full URL, got: {first.url!r}\n"
            "Residual: if TikTok changes the imagePost shape, update "
            "enrichment/field_maps._extract_tiktok_api."
        )

    def test_second_image_extracted(self):
        """All slides in imagePost.images should be captured."""
        ext = extract_tiktok(_TIKTOK_IMAGE_POST_RAW, "7651999999999999999")
        filenames = [m.filename for m in ext.media_items]
        assert "image_01.jpg" in filenames

    def test_flat_url_fallback_for_third_slide(self):
        """Slide with empty imageURL.urlList but a flat 'url' key must still be captured."""
        ext = extract_tiktok(_TIKTOK_IMAGE_POST_RAW, "7651999999999999999")
        filenames = [m.filename for m in ext.media_items]
        assert "image_02.jpg" in filenames

    def test_thumbnail_item_present(self):
        """A cover/thumbnail MediaItem should be produced for imagePost."""
        ext = extract_tiktok(_TIKTOK_IMAGE_POST_RAW, "7651999999999999999")
        assert ext.thumbnail_item is not None
        assert ext.thumbnail_item.filename == "cover.jpg"

    def test_no_yt_dlp_flag_on_images(self):
        """Image slides must NOT be flagged as yt-dlp (only video needs signing)."""
        ext = extract_tiktok(_TIKTOK_IMAGE_POST_RAW, "7651999999999999999")
        for item in ext.media_items:
            assert not item.fallback_yt_dlp, (
                f"Image item {item.filename!r} incorrectly flagged fallback_yt_dlp=True"
            )

    def test_spoiler_text_is_none(self):
        """TikTok has no spoiler/CW feature; spoiler_text must be None."""
        ext = extract_tiktok(_TIKTOK_IMAGE_POST_RAW, "7651999999999999999")
        assert ext.spoiler_text is None

    def test_url_list_with_alternate_key(self):
        """url_list (snake_case) is also accepted as fallback when urlList is absent."""
        raw = {
            "id": "111",
            "desc": "alt key test",
            "author": {"uniqueId": "tester"},
            "imagePost": {
                "images": [
                    {
                        "imageURL": {
                            # TikTok occasionally uses snake_case
                            "url_list": [
                                "https://p16.tiktokcdn.com/img/alt_key.jpg"
                            ]
                        }
                    }
                ]
            },
        }
        ext = extract_tiktok(raw, "111")
        assert len(ext.media_items) == 1
        assert "alt_key" in ext.media_items[0].url


# ---------------------------------------------------------------------------
# Escalation #3 — TikTok direct-GET → yt-dlp fallback
# ---------------------------------------------------------------------------

class TestYtDlpFallback:
    """#3: downloader.download_item falls back to yt-dlp on direct-GET failure."""

    @pytest.fixture
    def tmp_dest(self, tmp_path: Path) -> Path:
        return tmp_path

    def _make_tiktok_item(self, video_url: str = "https://v19.tiktokcdn.com/fake.mp4") -> MediaItem:
        return MediaItem(
            url=video_url,
            filename="video.mp4",
            headers={"Referer": "https://www.tiktok.com/"},
            fallback_yt_dlp=True,
            yt_dlp_url="https://www.tiktok.com/@studygrammer/video/7651999999999999999",
        )

    def test_403_triggers_ytdlp_fallback(self, tmp_dest: Path):
        """HTTP 403 from CDN GET must invoke yt-dlp (not raise ExpiredUrlError)."""
        item = self._make_tiktok_item()

        # Simulate 403 from CDN
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.ok = False

        ytdlp_dest = tmp_dest / "video.mp4"

        with patch("enrichment.downloader.requests.get", return_value=mock_resp) as mock_get, \
             patch("enrichment.downloader._download_via_ytdlp") as mock_ytdlp:
            mock_ytdlp.return_value = ytdlp_dest
            result = download_item(item, tmp_dest)

        mock_get.assert_called_once()
        mock_ytdlp.assert_called_once_with(item.yt_dlp_url, tmp_dest / item.filename)
        assert result == ytdlp_dest

    def test_404_triggers_ytdlp_fallback(self, tmp_dest: Path):
        """HTTP 404 must also invoke yt-dlp for TikTok items with fallback configured."""
        item = self._make_tiktok_item()

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.ok = False

        ytdlp_dest = tmp_dest / "video.mp4"

        with patch("enrichment.downloader.requests.get", return_value=mock_resp), \
             patch("enrichment.downloader._download_via_ytdlp", return_value=ytdlp_dest) as mock_ytdlp:
            result = download_item(item, tmp_dest)

        mock_ytdlp.assert_called_once()
        assert result == ytdlp_dest

    def test_network_error_triggers_ytdlp_fallback(self, tmp_dest: Path):
        """A requests.RequestException (network failure) must also fall back to yt-dlp."""
        import requests as req_lib
        item = self._make_tiktok_item()
        ytdlp_dest = tmp_dest / "video.mp4"

        with patch("enrichment.downloader.requests.get",
                   side_effect=req_lib.exceptions.ConnectionError("connection refused")), \
             patch("enrichment.downloader._download_via_ytdlp", return_value=ytdlp_dest) as mock_ytdlp:
            result = download_item(item, tmp_dest)

        mock_ytdlp.assert_called_once()
        assert result == ytdlp_dest

    def test_no_fallback_configured_raises_expired_on_403(self, tmp_dest: Path):
        """When fallback_yt_dlp=False, HTTP 403 must raise ExpiredUrlError, not call yt-dlp."""
        item = MediaItem(
            url="https://fbcdn.net/fake.jpg",
            filename="image.jpg",
            headers={},
            fallback_yt_dlp=False,   # no yt-dlp fallback (e.g. Threads/IG images)
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.ok = False

        with patch("enrichment.downloader.requests.get", return_value=mock_resp), \
             patch("enrichment.downloader._download_via_ytdlp") as mock_ytdlp:
            with pytest.raises(ExpiredUrlError):
                download_item(item, tmp_dest)

        mock_ytdlp.assert_not_called()

    def test_200_response_does_not_invoke_ytdlp(self, tmp_dest: Path):
        """A successful CDN GET must NOT invoke yt-dlp."""
        item = self._make_tiktok_item()
        dest_path = tmp_dest / item.filename
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.iter_content.return_value = [b"fake video bytes"]

        with patch("enrichment.downloader.requests.get", return_value=mock_resp), \
             patch("enrichment.downloader._download_via_ytdlp") as mock_ytdlp:
            download_item(item, tmp_dest)

        mock_ytdlp.assert_not_called()

    def test_empty_url_with_fallback_goes_direct_to_ytdlp(self, tmp_dest: Path):
        """An item with no URL at all (url='') must go straight to yt-dlp when configured."""
        item = MediaItem(
            url="",
            filename="video.mp4",
            headers={},
            fallback_yt_dlp=True,
            yt_dlp_url="https://www.tiktok.com/@user/video/123",
        )
        ytdlp_dest = tmp_dest / "video.mp4"

        with patch("enrichment.downloader._download_via_ytdlp", return_value=ytdlp_dest) as mock_ytdlp, \
             patch("enrichment.downloader.requests.get") as mock_get:
            result = download_item(item, tmp_dest)

        # Must not even attempt the GET when URL is empty
        mock_get.assert_not_called()
        mock_ytdlp.assert_called_once()
        assert result == ytdlp_dest

    def test_ytdlp_video_unavailable_raises_expired(self, tmp_dest: Path):
        """When yt-dlp itself reports video unavailable, ExpiredUrlError must propagate."""
        item = self._make_tiktok_item()

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.ok = False

        with patch("enrichment.downloader.requests.get", return_value=mock_resp), \
             patch("enrichment.downloader._download_via_ytdlp",
                   side_effect=ExpiredUrlError("yt-dlp: video unavailable")):
            with pytest.raises(ExpiredUrlError, match="unavailable"):
                download_item(item, tmp_dest)


# ---------------------------------------------------------------------------
# X (Twitter) extraction — extract_x against both stored raw shapes
# ---------------------------------------------------------------------------

class TestExtractX:
    """extract_x handles syndication (tweet dict) and GraphQL (legacy node) shapes."""

    def test_syndication_single_photo(self):
        raw = {
            "full_text": "a single photo tweet",
            "user": {"name": "Jane Doe", "screen_name": "jane"},
            "extended_entities": {
                "media": [
                    {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/AAA.jpg"}
                ]
            },
        }
        ex = extract_x(raw, "111")
        assert ex.platform == "x"
        assert ex.caption == "a single photo tweet"
        assert ex.author_display_name == "Jane Doe"
        assert [m.filename for m in ex.media_items] == ["image.jpg"]
        assert ex.media_items[0].url.endswith("AAA.jpg")
        assert ex.thumbnail_item is not None and ex.thumbnail_item.filename == "cover.jpg"
        assert ex.spoiler_text is None and ex.sound_id is None

    def test_syndication_multi_photo(self):
        raw = {
            "text": "two photos",
            "user": {"name": "Multi"},
            "extended_entities": {"media": [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/A.jpg"},
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/B.jpg"},
            ]},
        }
        ex = extract_x(raw, "222")
        assert [m.filename for m in ex.media_items] == ["image_00.jpg", "image_01.jpg"]

    def test_video_picks_highest_bitrate_mp4(self):
        raw = {
            "full_text": "a video",
            "user": {"name": "Vid"},
            "extended_entities": {"media": [{
                "type": "video",
                "media_url_https": "https://pbs.twimg.com/poster.jpg",
                "video_info": {"variants": [
                    {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/x.m3u8"},
                    {"content_type": "video/mp4", "bitrate": 256000, "url": "https://video.twimg.com/low.mp4"},
                    {"content_type": "video/mp4", "bitrate": 2176000, "url": "https://video.twimg.com/high.mp4"},
                ]},
            }]},
        }
        ex = extract_x(raw, "333")
        assert [m.filename for m in ex.media_items] == ["video.mp4"]
        assert ex.media_items[0].url.endswith("high.mp4")
        # poster becomes the cover thumbnail
        assert ex.thumbnail_item.url.endswith("poster.jpg")

    def test_graphql_shape_reads_legacy_node(self):
        raw = {
            "legacy": {
                "full_text": "graphql video",
                "extended_entities": {"media": [{
                    "type": "video",
                    "media_url_https": "https://pbs.twimg.com/g.jpg",
                    "video_info": {"variants": [
                        {"content_type": "video/mp4", "bitrate": 832000, "url": "https://video.twimg.com/g.mp4"},
                    ]},
                }]},
            },
            "core": {"user_results": {"result": {"legacy": {"name": "GQL Author"}}}},
            "views": {"count": "12345"},
        }
        ex = extract_x(raw, "444")
        assert ex.author_display_name == "GQL Author"
        assert ex.media_items[0].url.endswith("g.mp4")

    def test_text_only_tweet_has_no_media(self):
        raw = {"full_text": "just text", "user": {"name": "Texty"}}
        ex = extract_x(raw, "555")
        assert ex.media_items == []
        assert ex.thumbnail_item is None
        assert ex.caption == "just text"

    def test_retweet_media_pulled_from_embedded_tweet(self):
        """Retweets carry media under legacy.retweeted_status_result; the top-level
        tweet has no extended_entities. Extraction must descend into it."""
        raw = {
            "legacy": {
                "full_text": "RT @orig: check this",
                "entities": {"user_mentions": []},  # no media at top level
                "retweeted_status_result": {
                    "result": {
                        "legacy": {
                            "full_text": "the original video tweet",
                            "extended_entities": {"media": [{
                                "type": "video",
                                "media_url_https": "https://pbs.twimg.com/poster.jpg",
                                "video_info": {"variants": [
                                    {"content_type": "video/mp4", "bitrate": 2176000,
                                     "url": "https://video.twimg.com/orig.mp4"},
                                ]},
                            }]},
                        },
                        "core": {"user_results": {"result": {"legacy": {"name": "Original Author"}}}},
                    }
                },
            },
            "core": {"user_results": {"result": {"legacy": {"name": "Retweeter"}}}},
        }
        ex = extract_x(raw, "666")
        assert len(ex.media_items) == 1
        assert ex.media_items[0].url.endswith("orig.mp4")
        assert ex.media_items[0].filename == "video.mp4"
        assert ex.thumbnail_item is not None
        # caption + author should reflect the embedded (carried) tweet
        assert ex.caption == "the original video tweet"
        assert ex.author_display_name == "Original Author"

    def test_tweet_with_visibility_results_wrapper_unwrapped(self):
        """Quote/retweet whose result is a TweetWithVisibilityResults wrapper."""
        raw = {
            "legacy": {
                "full_text": "quoting",
                "quoted_status_result": {
                    "result": {
                        "tweet": {  # visibility wrapper
                            "legacy": {
                                "full_text": "quoted photo",
                                "extended_entities": {"media": [{
                                    "type": "photo",
                                    "media_url_https": "https://pbs.twimg.com/q.jpg",
                                }]},
                            },
                        }
                    }
                },
            },
        }
        ex = extract_x(raw, "777")
        assert len(ex.media_items) == 1
        assert ex.media_items[0].url.endswith("q.jpg")


# ---------------------------------------------------------------------------
# Threads spoiler / content-warning boolean — real field is is_spoiler_media
# ---------------------------------------------------------------------------

class TestThreadsSpoiler:
    """The live CW field is text_post_app_info.is_spoiler_media (verified live)."""

    def test_spoiler_true_sets_spoiler_text(self):
        raw = {
            "caption": {"text": "hidden punchline"},
            "user": {"full_name": "CW User"},
            "text_post_app_info": {"is_spoiler_media": True},
        }
        ex = extract_threads(raw, "S1")
        assert ex.spoiler_text == "hidden punchline"   # has_spoiler -> True downstream

    def test_spoiler_media_only_uses_marker(self):
        raw = {
            "user": {"full_name": "CW User"},
            "text_post_app_info": {"is_spoiler_media": True},
            "image_versions2": {"candidates": [{"url": "https://x/i.jpg"}]},
        }
        ex = extract_threads(raw, "S2")
        assert ex.spoiler_text == "(spoiler-hidden media)"

    def test_non_spoiler_real_shape_is_none(self):
        # Mirrors the live shape we observed: is_spoiler_media present but False.
        raw = {
            "caption": {"text": "normal post"},
            "user": {"full_name": "Normal"},
            "text_post_app_info": {"is_spoiler_media": False, "quote_count": 0},
        }
        ex = extract_threads(raw, "S3")
        assert ex.spoiler_text is None

    def test_legacy_guessed_fields_no_longer_trigger(self):
        # The old (wrong) fields must NOT flag a spoiler on their own.
        raw = {
            "caption": {"text": "x"},
            "text_post_app_info": {"post_text_hidden_content_type": "CW_STRING",
                                   "is_content_warning": True},
        }
        ex = extract_threads(raw, "S4")
        assert ex.spoiler_text is None
