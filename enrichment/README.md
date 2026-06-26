# Layer-3 Enrichment — Content Bundles

Pure content **extraction** for the top-N ranked posts. Produces a **Content Bundle** for each: every media file on disk + text/sound/author metadata in `post_content`.

> **No understanding.** No OCR, ASR, or multimodal analysis. No comments. This module extracts what the post *contains*, not what it *depicts*.

---

## Architecture

```
ingestion process
  adapters → core.storage (raw)
                │
                └─► core.ranker → top-N post_identities
                                      │
                                      ▼
                           enrichment.enrich(post_identities)
                              ├─ reads raw from core.storage
                              ├─ field_maps.extract(platform, raw, post_id)
                              ├─ downloader.download_item(media_items)  ← CDN GET / yt-dlp
                              └─ storage.write_post_content(...)
                                 storage.set_thumbnail_path(...)

   (every captured post, not just top-N)
   ingestion.download_thumbnail(post) → enrichment.download_thumbnail(post)
```

**Owned path:** `enrichment/` only. Reads `core` data read-only. Writes via `core.storage` (`write_post_content`, `set_thumbnail_path`). Never touches `core/schema.py`, `core/storage.py`, `api/`, `web/`, or any adapter.

---

## Public API

```python
from enrichment import enrich, download_thumbnail

# Enrich the current top-N (called by run_ingestion() after ranking)
enrich(
    post_identities=[("tiktok", "7651269245652798741"), ...],
    raw_reader=core.storage.get_post_raw,   # wire in when spine lands
    storage=core_storage_instance,          # wire in when spine lands
)

# Download thumbnail for every captured post (called per-post at ingestion)
path = download_thumbnail(post_record, storage=core_storage_instance)
```

Until `core.storage` is built (spine track runs in parallel), both default to the local stub (`enrichment._stub_storage.StubStorage`) which writes to `data/enrichment_stub.json`.

### Idempotency

`enrich()` is idempotent: posts already in `post_content` are silently skipped (`skip_if_exists=True` by default). The daily ingestion job enriches only the newly-surfaced top-N entries — steady-state cost is minimal.

### Expiry handling

CDN URLs embedded in `raw` are **signed and expiring**. Enrichment must run inside the ingestion process while URLs are fresh. If a URL is already expired (HTTP 403/410 or detected via `x-expires`/`oe` timestamp), the post_content row is written with `status=expired_url_miss` and no re-fetch is attempted.

---

## Per-platform raw field map

### TikTok

The `raw` stored in a `PostRecord` from the **TikTok-Api signed path** is the complete `item_list` API item object. From the **yt-dlp fallback**, it is the yt-dlp flat-playlist entry.

| Content Bundle field | TikTok-Api raw path | yt-dlp raw path |
|---|---|---|
| Video URL (main) | `raw['video']['downloadAddr']` | — (no signed URL; use yt-dlp) |
| Video URL (streaming) | `raw['video']['playAddr']` | — |
| Cover / thumbnail | `raw['video']['cover']` or `originCover` | `raw['thumbnail']` |
| Carousel images | `raw['imagePost']['images'][*]['imageURL']['urlList'][0]` | — |
| Carousel cover | `raw['video']['cover']` or first image thumbnail | — |
| `caption` | `raw['desc']` | `raw['description']` / `raw['title']` |
| `sound_id` | `raw['music']['id']` (skip if `"0"`) | — (not available) |
| `sound_name` | `raw['music']['title']` | `raw['track']` |
| `sound_author` | `raw['music']['authorName']` | — |
| `author_display_name` | `raw['author']['nickname']` | `raw['uploader']` |
| Spoiler | — (TikTok has no CW/spoiler feature) | — |
| yt-dlp fallback URL | `https://www.tiktok.com/@{author.uniqueId}/video/{id}` | `raw['webpage_url']` |

**Download headers (TikTok CDN):**
```python
{"Referer": "https://www.tiktok.com/", "User-Agent": "<browser UA>"}
```

**yt-dlp fallback:** TikTok video CDN URLs require browser-signed tokens (`X-Bogus`/`msToken`). Direct GET often returns HTTP 403. `downloader.py` tries direct GET first; on failure, invokes `yt-dlp` to re-sign and download. Cover images are plain GETs (no yt-dlp needed).

---

### Threads

The `raw` is the complete Threads GraphQL post node, intercepted from the profile-feed response. It shares Meta's private-API field naming with Instagram.

| Content Bundle field | Threads raw path |
|---|---|
| Image URL | `raw['image_versions2']['candidates'][0]['url']` |
| Video URL | `raw['video_versions'][0]['url']` |
| Carousel images | `raw['carousel_media'][*]['image_versions2']['candidates'][0]['url']` |
| Carousel videos | `raw['carousel_media'][*]['video_versions'][0]['url']` |
| Cover / thumbnail | Same as image URL (first slide for carousels) |
| `caption` | `raw['caption']['text']` |
| `spoiler_text` | `raw['caption']['text']` **when** `raw['text_post_app_info']['post_text_hidden_content_type']` is non-null or `raw['text_post_app_info']['is_content_warning']` is true. Tap-to-reveal is a UI gate only — the full text is always in `caption.text`. |
| `sound_id` | `raw['clips_metadata']['music_info']['music_asset_info']['audio_cluster_id']` |
| `sound_name` | `raw['clips_metadata']['music_info']['music_asset_info']['title']` |
| `sound_author` | `raw['clips_metadata']['music_info']['music_asset_info']['display_artist']` |
| `author_display_name` | `raw['user']['full_name']` → fallback `raw['user']['username']` |

**Threads CDN:** plain HTTPS GET (Meta/fbcdn.net CDN, no special headers needed for public posts). URLs carry `oe=<hex_unix_ts>` for expiry detection.

**Known gap:** the stored `data/threads_harvest_scratch.json` has `raw={}` for all rows (the accumulator run on 2026-06-26 did not preserve raw payloads in the JSON). Fresh Threads enrichment requires running the adapter at ingestion time while URLs are live. Use `fixture_runner.py --live-threads` to test with a freshly fetched Threads post.

---

### Instagram

The `raw` is the complete `instagrapi` private-API item from `/api/v1/feed/user/{id}/` (~97–122 keys). Post-processing adds:
- `raw['_normalized_extra']`: `audio_id`, `audio_name`, `is_reel`, `carousel_count`, etc.
- `raw['_account_info']`: full author payload (one call per account).

| Content Bundle field | Instagram raw path |
|---|---|
| Image URL | `raw['image_versions2']['candidates'][0]['url']` |
| Video URL | `raw['video_versions'][0]['url']` |
| Carousel images | `raw['carousel_media'][*]['image_versions2']['candidates'][0]['url']` |
| Carousel videos | `raw['carousel_media'][*]['video_versions'][0]['url']` |
| Cover / thumbnail | Same as image URL (first slide for carousels) |
| `caption` | `raw['caption']['text']` |
| `sound_id` | `raw['clips_metadata']['music_info']['music_asset_info']['audio_cluster_id']` → fallback `raw['_normalized_extra']['audio_id']` |
| `sound_name` | `raw['clips_metadata']['music_info']['music_asset_info']['title']` → fallback `raw['_normalized_extra']['audio_name']` |
| `sound_author` | `raw['clips_metadata']['music_info']['music_asset_info']['display_artist']` |
| `author_display_name` | `raw['user']['full_name']` → `raw['_account_info']['user']['full_name']` → `raw['user']['username']` |

**Instagram CDN:** plain HTTPS GET (fbcdn.net). URLs carry `oe=<hex_unix_ts>` for expiry. No special headers needed for public posts via instagrapi.

---

## File layout

```
enrichment/
├── __init__.py           # exports: enrich, download_thumbnail
├── extractor.py          # enrich() + download_thumbnail() — main API
├── field_maps.py         # per-platform raw field extraction
├── downloader.py         # CDN GET + yt-dlp fallback
├── _stub_storage.py      # core.storage stub (parallel build shim)
├── fixture_runner.py     # acceptance fixture runner
├── requirements.txt
└── README.md (this file)
```

---

## Running the fixture runner

```bash
# Dry-run: extract fields, print bundle, no downloads
.venv/bin/python -m enrichment.fixture_runner --dry-run

# Full run: extract + download media
.venv/bin/python -m enrichment.fixture_runner

# Force re-enrichment of already-done posts
.venv/bin/python -m enrichment.fixture_runner --force

# Threads spoiler live test (fetches @quokkaredfield via Playwright):
.venv/bin/python -m enrichment.fixture_runner --live-threads
```

### Expected fixture results

| Fixture | Source | Expected status | Notes |
|---|---|---|---|
| TikTok video | `data/tiktok_accumulator.json` (item 7651…741) | `done` | CDN URLs valid ~46h from scrape |
| Instagram reel | `secrets/explore_harvest.json` raw_by_id (has music) | `done` | CDN valid ~4h; run at ingestion |
| Instagram carousel | `secrets/explore_harvest.json` raw_by_id | `done` | 14 slides; CDN valid ~4h |
| Threads image | Synthetic | `expired_url_miss` | Placeholder URLs; real test via `--live-threads` |
| Threads video | Synthetic | `expired_url_miss` | Same |
| Threads spoiler | Synthetic | `expired_url_miss` (but extraction ✓) | `spoiler_text` correctly populated from `is_content_warning=True` |

---

## Integration with core.storage (when spine lands)

Replace the stub with real storage:

```python
from core import storage as cs
from enrichment import enrich

enrich(
    top_n_identities,
    raw_reader=cs.get_post_raw,          # core.storage function
    storage=cs,                          # core.storage module (duck-typed)
    media_root=Path("data/media"),
)
```

`core.storage` must expose:
- `is_in_post_content(platform, post_id) -> bool`
- `write_post_content(*, platform, platform_post_id, media_paths, caption, spoiler_text, sound_id, sound_name, sound_author, author_display_name, extracted_at, status) -> None`
- `set_thumbnail_path(platform, post_id, path) -> None`
- `get_post_raw(platform, post_id) -> dict | None`

The stub (`_stub_storage.py`) has matching signatures — the swap is a one-liner at the call site.

---

## Escalations to Opus

1. **Threads spoiler raw field** — `post_text_hidden_content_type` / `is_content_warning` are documented based on Meta's API shape, but NOT verified live against `@quokkaredfield` (the stored `threads_harvest_scratch.json` has empty `raw`). Run `--live-threads` and confirm the CW post node contains the expected fields. If Threads uses a different field (e.g., `text_post_app_info.share_url` or a nested `hidden_text`), update `field_maps.extract_threads`.

2. **TikTok imagePost URL structure** — the `imagePost.images[*].imageURL.urlList[0]` path is documented from TikTok's API schema but not observed in the current accumulator (all 413 stored items are video type). If a TikTok photo/carousel post is encountered and the extraction fails, the raw structure needs to be inspected and `_extract_tiktok_api` updated.

3. **TikTok direct GET headers** — `downloadAddr` CDN GET succeeds with `Referer: tiktok.com` in the current accumulated items (URLs still fresh ~46h). Once those URLs expire, direct GET will fail and yt-dlp becomes the only path. Test with `--force` after URL expiry (~2026-06-28) to confirm yt-dlp fallback activates cleanly.
