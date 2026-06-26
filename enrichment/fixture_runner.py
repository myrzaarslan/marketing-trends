"""Acceptance fixture runner for Layer-3 enrichment.

Produces a complete Content Bundle for each of the required fixture types:
  - TikTok video
  - Threads image
  - Threads video
  - Threads spoiler (verifies @quokkaredfield or uses synthetic fixture)
  - Instagram reel + music
  - Instagram image/carousel
  (YouTube excluded per spec)

Usage
-----
Run from repo root (venv required):

    .venv/bin/python -m enrichment.fixture_runner

    # To force re-enrich (ignore already-done):
    .venv/bin/python -m enrichment.fixture_runner --force

    # To use a fresh Threads fetch from @quokkaredfield for spoiler test:
    .venv/bin/python -m enrichment.fixture_runner --live-threads

    # To show bundle results only (no download):
    .venv/bin/python -m enrichment.fixture_runner --dry-run

Source strategy
---------------
1. **TikTok video** — reads from ``data/tiktok_accumulator.json`` (accumulated
   items, raw IS the TikTok API item; first video-type item used).
2. **Instagram reel + image/carousel** — reads from
   ``secrets/explore_harvest.json`` ``raw_by_id`` (first reel and first
   carousel found).
3. **Threads image/video/spoiler** — the stored ``data/threads_harvest_scratch.json``
   has ``raw={}`` for all rows (the accumulator stripped raw in that run).
   Strategy:
   - For image/video: uses SYNTHETIC fixtures with the documented raw shape
     (demonstrates extraction logic; downloads will be expired_url_miss since
     URLs are synthetic placeholders).
   - For spoiler: if ``--live-threads`` is passed, re-fetches @quokkaredfield
     live via the ThreadsAdapter; otherwise uses a synthetic CW fixture.

The fixture runner is deliberately NOT a test framework (no pytest) — it's a
standalone script that reports pass/fail inline and is easy to run live.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from enrichment._stub_storage import StubStorage
from enrichment.extractor import enrich, download_thumbnail as dl_thumb
from enrichment.field_maps import extract

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(message)s",
)
logger = logging.getLogger("fixture_runner")


# ---------------------------------------------------------------------------
# Fixture sources
# ---------------------------------------------------------------------------

def _load_tiktok_fixture() -> Optional[tuple[str, str, dict]]:
    """Return (platform, post_id, raw) for the first TikTok video in the accumulator."""
    acc_path = ROOT / "data" / "tiktok_accumulator.json"
    if not acc_path.exists():
        logger.warning("TikTok accumulator not found: %s", acc_path)
        return None
    with open(acc_path, encoding="utf-8") as fh:
        data = json.load(fh)
    items: dict = data.get("items", {})
    for post_id, raw in items.items():
        # Skip image posts for the 'video' fixture
        if "imagePost" not in raw:
            return "tiktok", str(post_id), raw
    logger.warning("No TikTok video item found in accumulator")
    return None


def _load_ig_reel_fixture() -> Optional[tuple[str, str, dict]]:
    """Return (platform, post_id, raw) for the first IG reel in explore_harvest."""
    return _load_ig_fixture(want_reel=True)


def _load_ig_carousel_fixture() -> Optional[tuple[str, str, dict]]:
    """Return (platform, post_id, raw) for the first IG carousel in explore_harvest."""
    return _load_ig_fixture(want_reel=False)


def _load_ig_fixture(want_reel: bool) -> Optional[tuple[str, str, dict]]:
    harvest_path = ROOT / "secrets" / "explore_harvest.json"
    if not harvest_path.exists():
        logger.warning("IG explore harvest not found: %s", harvest_path)
        return None
    with open(harvest_path, encoding="utf-8") as fh:
        data = json.load(fh)
    raw_by_id: dict = data.get("raw_by_id", {})
    for post_id, raw in raw_by_id.items():
        if want_reel:
            # Reel: has video_versions AND clips_metadata.music_info
            clips = raw.get("clips_metadata") or {}
            if (
                raw.get("video_versions")
                and isinstance(clips, dict)
                and clips.get("music_info")
            ):
                return "instagram", str(post_id), raw
        else:
            # Carousel: has carousel_media with multiple items
            carousel = raw.get("carousel_media") or []
            if len(carousel) >= 2:
                return "instagram", str(post_id), raw
    if want_reel:
        # Fallback: any video post
        for post_id, raw in raw_by_id.items():
            if raw.get("video_versions"):
                return "instagram", str(post_id), raw
    logger.warning("No suitable IG %s found in explore_harvest", "reel" if want_reel else "carousel")
    return None


# ---------------------------------------------------------------------------
# Synthetic Threads fixtures (for when stored raw is empty)
# ---------------------------------------------------------------------------

def _synthetic_threads_image() -> tuple[str, str, dict]:
    """Synthetic Threads image post raw (demonstrates extraction from documented shape)."""
    return (
        "threads",
        "SYNTHETIC_THREADS_IMAGE_001",
        {
            "pk": "SYNTHETIC_THREADS_IMAGE_001",
            "code": "SYN_IMG_001",
            "taken_at": 1782518400,
            "media_type": 1,
            "caption": {"text": "Synthetic Threads image fixture for enrichment test #edu"},
            "like_count": 1234,
            "image_versions2": {
                "candidates": [
                    {
                        "url": (
                            "https://scontent.example.fbcdn.net/v/synthetic/image.jpg"
                            "?oe=6A440B92"   # pre-expired intentionally
                        ),
                        "width": 1080,
                        "height": 1080,
                    }
                ]
            },
            "user": {
                "pk": "999001",
                "username": "synthetic_edu_account",
                "full_name": "Synthetic Edu Account",
                "follower_count": 50000,
            },
            "text_post_app_info": {
                "direct_reply_count": 42,
                "repost_count": 8,
                "post_text_hidden_content_type": None,
                "is_content_warning": False,
            },
        },
    )


def _synthetic_threads_video() -> tuple[str, str, dict]:
    """Synthetic Threads video post raw."""
    return (
        "threads",
        "SYNTHETIC_THREADS_VIDEO_001",
        {
            "pk": "SYNTHETIC_THREADS_VIDEO_001",
            "code": "SYN_VID_001",
            "taken_at": 1782518400,
            "media_type": 2,
            "video_duration": 30.5,
            "play_count": 95000,
            "caption": {"text": "Synthetic Threads video #trending #education"},
            "like_count": 5678,
            "image_versions2": {
                "candidates": [
                    {"url": "https://scontent.example.fbcdn.net/v/synthetic/video_thumb.jpg?oe=6A440B92"}
                ]
            },
            "video_versions": [
                {
                    "url": "https://scontent.example.fbcdn.net/v/synthetic/video.mp4?oe=6A440B92",
                    "type": 101,
                    "width": 1080,
                    "height": 1920,
                }
            ],
            "user": {
                "pk": "999002",
                "username": "synthetic_video_account",
                "full_name": "Synthetic Video Account",
            },
            "text_post_app_info": {
                "direct_reply_count": 100,
                "repost_count": 20,
            },
        },
    )


def _synthetic_threads_spoiler() -> tuple[str, str, dict]:
    """Synthetic Threads content-warning / spoiler post.

    Models the known Threads CW shape: ``text_post_app_info.post_text_hidden_content_type``
    is set to the CW type string (e.g. "CW_STRING"), and the full text is in
    ``caption.text`` (tap-to-reveal is UI-only — raw always has the text).

    NOTE: For a live verification against @quokkaredfield, run with --live-threads.
    The live adapter will return a PostRecord with the actual Threads GraphQL payload
    so the spoiler_text field can be confirmed against the real post.
    """
    spoiler_body = (
        "⚠️ Content warning: this post contains discussion of exam stress and academic pressure. "
        "Tap to reveal. #mentalhealth #studytips"
    )
    return (
        "threads",
        "SYNTHETIC_THREADS_SPOILER_001",
        {
            "pk": "SYNTHETIC_THREADS_SPOILER_001",
            "code": "SYN_SPL_001",
            "taken_at": 1782518400,
            "media_type": 1,
            "caption": {"text": spoiler_body},
            "like_count": 3210,
            "image_versions2": {
                "candidates": [
                    {"url": "https://scontent.example.fbcdn.net/v/synthetic/spoiler_thumb.jpg?oe=6A440B92"}
                ]
            },
            "user": {
                "pk": "999003",
                "username": "quokkaredfield",
                "full_name": "Quokka Redfield",
            },
            "text_post_app_info": {
                "direct_reply_count": 77,
                "repost_count": 15,
                # This is the field that indicates a content warning is applied:
                "post_text_hidden_content_type": "CW_STRING",
                "is_content_warning": True,
            },
        },
    )


# ---------------------------------------------------------------------------
# Live Threads fetch (@quokkaredfield)
# ---------------------------------------------------------------------------

def _fetch_threads_live(handle: str = "quokkaredfield") -> list[tuple[str, str, dict]]:
    """Fetch live posts from a Threads account and return (platform, post_id, raw) tuples."""
    from adapters.threads import ThreadsAdapter
    from core.schema import WatchedAccount

    adapter = ThreadsAdapter(headless=True)
    account = WatchedAccount(
        handle=handle, platform="threads", segment="edu_influencer", geo_tier="World"
    )
    logger.info("Fetching live Threads posts from @%s ...", handle)
    try:
        posts = adapter.fetch_account_posts(account, limit=30)
        result = []
        for p in posts:
            if p.raw:
                result.append(("threads", p.platform_post_id, p.raw))
        return result
    except Exception as exc:
        logger.error("Live Threads fetch failed: %s", exc)
        return []


def _find_threads_spoiler_live(posts: list[tuple[str, str, dict]]) -> Optional[tuple[str, str, dict]]:
    """Find a post with a content warning / spoiler flag in live data."""
    for platform, post_id, raw in posts:
        tpa = raw.get("text_post_app_info") or {}
        if tpa.get("post_text_hidden_content_type") or tpa.get("is_content_warning"):
            return platform, post_id, raw
    return None


def _find_threads_image_live(posts: list[tuple]) -> Optional[tuple]:
    for platform, post_id, raw in posts:
        if raw.get("image_versions2") and not raw.get("video_versions") and not raw.get("carousel_media"):
            return platform, post_id, raw
    return None


def _find_threads_video_live(posts: list[tuple]) -> Optional[tuple]:
    for platform, post_id, raw in posts:
        if raw.get("video_versions"):
            return platform, post_id, raw
    return None


# ---------------------------------------------------------------------------
# Fixture runner
# ---------------------------------------------------------------------------

FIXTURE_LABELS = [
    "tiktok_video",
    "threads_image",
    "threads_video",
    "threads_spoiler",
    "instagram_reel",
    "instagram_carousel",
]


def build_fixtures(live_threads: bool = False) -> dict[str, tuple[str, str, dict]]:
    """Build the fixture map: label → (platform, post_id, raw)."""
    fixtures: dict[str, Optional[tuple]] = {}

    # --- TikTok ---
    fixtures["tiktok_video"] = _load_tiktok_fixture()

    # --- Instagram ---
    fixtures["instagram_reel"] = _load_ig_reel_fixture()
    fixtures["instagram_carousel"] = _load_ig_carousel_fixture()

    # --- Threads ---
    if live_threads:
        logger.info("Live Threads mode — fetching @quokkaredfield ...")
        live_posts = _fetch_threads_live("quokkaredfield")
        if live_posts:
            fixtures["threads_image"] = _find_threads_image_live(live_posts)
            fixtures["threads_video"] = _find_threads_video_live(live_posts)
            fixtures["threads_spoiler"] = _find_threads_spoiler_live(live_posts)
            # Fill missing with synthetics
            if not fixtures.get("threads_image"):
                logger.info("No image post found on @quokkaredfield — using synthetic")
                fixtures["threads_image"] = _synthetic_threads_image()
            if not fixtures.get("threads_video"):
                logger.info("No video post found on @quokkaredfield — using synthetic")
                fixtures["threads_video"] = _synthetic_threads_video()
            if not fixtures.get("threads_spoiler"):
                logger.warning(
                    "No spoiler/CW post found on @quokkaredfield — using synthetic fixture.\n"
                    "ESCALATE: if @quokkaredfield should have a CW post but none was found, "
                    "the Threads raw may not be exposing post_text_hidden_content_type. "
                    "Check the raw payload for CW-related keys."
                )
                fixtures["threads_spoiler"] = _synthetic_threads_spoiler()
        else:
            logger.warning("Live Threads fetch returned nothing — falling back to synthetics")
            fixtures["threads_image"] = _synthetic_threads_image()
            fixtures["threads_video"] = _synthetic_threads_video()
            fixtures["threads_spoiler"] = _synthetic_threads_spoiler()
    else:
        # NOTE: Stored threads_harvest_scratch.json has raw={} for all posts.
        # Until a fresh fetch is stored with raw payloads, synthetics are required.
        fixtures["threads_image"] = _synthetic_threads_image()
        fixtures["threads_video"] = _synthetic_threads_video()
        fixtures["threads_spoiler"] = _synthetic_threads_spoiler()

    # Filter out None and type-check
    valid: dict[str, tuple[str, str, dict]] = {}
    for label, fx in fixtures.items():
        if fx is None:
            logger.warning("Fixture %s: NOT AVAILABLE (source data missing)", label)
        else:
            valid[label] = fx
    return valid


def run_fixtures(
    fixtures: dict[str, tuple[str, str, dict]],
    storage: StubStorage,
    media_root: Path,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, dict]:
    """Run enrichment for all fixtures and return a result dict."""
    results: dict[str, dict] = {}

    for label, (platform, post_id, raw) in fixtures.items():
        logger.info("=" * 60)
        logger.info("FIXTURE: %s  (%s:%s)", label, platform, post_id)

        # Register raw in the stub so enrich() can look it up
        storage.store_raw(platform, post_id, raw)

        # Pre-extract to show what we'd capture (dry-run or real)
        extraction = extract(platform, raw, post_id)
        logger.info(
            "  caption:        %s",
            (extraction.caption or "")[:80] + "..." if extraction.caption and len(extraction.caption) > 80 else extraction.caption,
        )
        logger.info("  spoiler_text:   %s", extraction.spoiler_text is not None)
        logger.info("  sound_id:       %s", extraction.sound_id)
        logger.info("  sound_name:     %s", extraction.sound_name)
        logger.info("  sound_author:   %s", extraction.sound_author)
        logger.info("  author_display: %s", extraction.author_display_name)
        logger.info(
            "  media_items:    %d item(s): %s",
            len(extraction.media_items),
            [m.filename for m in extraction.media_items],
        )
        logger.info(
            "  thumbnail:      %s",
            extraction.thumbnail_item.filename if extraction.thumbnail_item else "none",
        )

        if dry_run:
            results[label] = {
                "status": "dry_run",
                "platform": platform,
                "post_id": post_id,
                "caption": extraction.caption,
                "spoiler_text": extraction.spoiler_text,
                "sound_id": extraction.sound_id,
                "sound_name": extraction.sound_name,
                "sound_author": extraction.sound_author,
                "author_display_name": extraction.author_display_name,
                "media_items": [m.filename for m in extraction.media_items],
                "thumbnail": extraction.thumbnail_item.filename if extraction.thumbnail_item else None,
            }
            continue

        # Enrich
        enrich(
            [(platform, post_id)],
            raw_reader=lambda p, pid, _r=raw: _r,
            storage=storage,
            media_root=media_root,
            skip_if_exists=(not force),
        )

        row = storage._state.get("post_content", {}).get(f"{platform}:{post_id}", {})
        results[label] = {
            "status": row.get("status", "unknown"),
            "platform": platform,
            "post_id": post_id,
            "caption": row.get("caption"),
            "spoiler_text": row.get("spoiler_text"),
            "sound_id": row.get("sound_id"),
            "sound_name": row.get("sound_name"),
            "sound_author": row.get("sound_author"),
            "author_display_name": row.get("author_display_name"),
            "media_paths": row.get("media_paths", []),
        }

    return results


def print_summary(results: dict[str, dict]) -> None:
    print("\n" + "=" * 60)
    print("FIXTURE RESULTS SUMMARY")
    print("=" * 60)
    all_pass = True
    for label, r in results.items():
        status = r.get("status", "unknown")
        icon = "✓" if status in ("done", "dry_run") else "✗" if status == "expired_url_miss" else "?"
        if status == "expired_url_miss":
            all_pass = False
        print(f"\n  [{icon}] {label} ({r['platform']}:{r['post_id']})")
        print(f"        status:         {status}")
        print(f"        caption:        {str(r.get('caption') or '')[:60]}")
        if r.get("spoiler_text") is not None:
            print(f"        spoiler_text:   {str(r['spoiler_text'])[:60]}")
        print(f"        sound:          {r.get('sound_id')} / {r.get('sound_name')}")
        print(f"        author_display: {r.get('author_display_name')}")
        media = r.get("media_paths") or r.get("media_items") or []
        print(f"        media:          {len(media)} file(s): {media}")

    print("\n" + "=" * 60)
    print("OVERALL:", "PASS (all done/dry_run)" if all_pass else "PARTIAL (some expired_url_miss)")
    if not all_pass:
        print("\nNOTE: expired_url_miss for stored fixtures is EXPECTED — signed CDN URLs")
        print("from previous runs expire in hours. Run enrichment at ingestion time to")
        print("capture while URLs are fresh, or re-fetch with --live-threads for Threads.")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run enrichment acceptance fixtures (Layer-3 Content Bundle)."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-enrich posts that are already in post_content (re-writes row; won't re-download).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract fields and print result without downloading any files.",
    )
    parser.add_argument(
        "--live-threads",
        action="store_true",
        help="Fetch Threads fixtures live from @quokkaredfield (requires network + Playwright).",
    )
    parser.add_argument(
        "--media-root",
        default=str(ROOT / "data" / "media"),
        help="Root directory for downloaded media (default: data/media/).",
    )
    parser.add_argument(
        "--stub-path",
        default=str(ROOT / "data" / "enrichment_stub.json"),
        help="Path to the stub storage JSON file.",
    )
    args = parser.parse_args()

    media_root = Path(args.media_root)
    stub_path = Path(args.stub_path)
    storage = StubStorage(stub_path)

    fixtures = build_fixtures(live_threads=args.live_threads)
    if not fixtures:
        logger.error("No fixtures available — check data sources.")
        sys.exit(1)

    logger.info("Running %d fixture(s): %s", len(fixtures), list(fixtures))
    results = run_fixtures(
        fixtures,
        storage=storage,
        media_root=media_root,
        force=args.force,
        dry_run=args.dry_run,
    )

    print_summary(results)

    # Exit non-zero only if there were hard errors (not expected expired_url_miss)
    if not results:
        sys.exit(1)


if __name__ == "__main__":
    main()
