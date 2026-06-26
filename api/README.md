# api/ — FastAPI Digest API

Read-only JSON API over the SQLite trends DB. See `docs/CORE-SPINE.md` and `docs/adr/0003-*`.

## Run

```bash
# From repo root — port 8001 (the web dev server proxies to this port)
.venv/bin/uvicorn api.main:app --reload --port 8001
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness probe |
| `GET` | `/digest` | Ranked digest cards (each now carries `has_content_bundle` + `thumbnail`) |
| `GET` | `/digest/meta` | Sort availability for a given platform |
| `GET` | `/post/{platform}/{platform_post_id}` | Full **Content Bundle** for one post (see below) |
| `GET` | `/media/{platform}/{platform_post_id}/{file}` | Static-serve downloaded media (video.mp4, slide_*.jpg, cover.jpg) |
| `POST` | `/refresh` | Background-trigger `run_ingestion()`; returns `job_id` immediately |
| `GET` | `/refresh/status/{job_id}` | Poll ingestion job status |
| `GET` | `/thumbnails/{platform}/{filename}` | Serve downloaded thumbnails |

## `GET /post/{platform}/{platform_post_id}` — Content Bundle

Returns the complete extracted content of a post so a marketer can study and recreate it:

- `enriched` (bool) — whether a finished bundle exists
- `media_type`, `media_items[]` (ordered; each `{url, filename, kind}` where `kind` ∈ `video|image|audio`),
  `thumbnail`
- `caption`, `hashtags[]`, `has_spoiler` (bool) + `spoiler_text`
- `sound_id`, `sound_name`, `sound_author`
- `author_display_name`, `account_handle`
- Engagement from the latest snapshot: `view_count`, `like_count`, `comment_count`, `share_count`,
  `save_count`, `author_follower_count`. **A count is `null` when the platform does not expose that
  signal** (SIGNALS.md) — the UI never renders a fake `0`.
- Provenance: `url` (source link), `geo_tier`, `posted_at`, `first_seen_at`

Returns `404` if the post is unknown. Works for non-enriched posts too (degraded: stats + caption +
link, no media).

## Media files

`data/media/` is mounted read-only at `/media`. Bytes live on the filesystem, never in SQLite. The
API never drives a browser — a UI click only reads the DB + static files.

## Populating Content Bundles

On startup the API imports the 3 real bundles in `data/enrichment_stub.json` (TikTok video, IG reel,
IG 14-slide carousel) + the synthetic Threads fixtures into `post_content` (idempotent — inserts a
parent `posts` row + a minimal snapshot if missing, so the ranker surfaces them). Fresh bundles for
the live digest top-N are produced by the ingestion/enrichment track, not by this read-only API.

## `/digest` query params

| Param | Default | Description |
|-------|---------|-------------|
| `platform` | `null` (all) | Filter: `tiktok`, `instagram`, `x`, `threads` |
| `geo` | `null` (all) | Filter: `KZ`, `CIS`, `World` |
| `period` | `30` | Days since `first_seen_at` |
| `sort` | `engagement_rate` | Sort strategy (see below) |
| `limit` | `50` | Max cards returned |

## Sort strategies

| Key | Available on | Notes |
|-----|--------------|-------|
| `engagement_rate` | All platforms | Default. Degrades gracefully for no-view platforms. |
| `raw_counts` | All platforms | Sum of available engagement signals |
| `share_rate` | TikTok / X / Threads | Instagram has no share count |
| `save_rate` | TikTok only | — |
| `velocity` | All (with history) | Requires ≥3 distinct snapshot days |
| `relative_baseline` | All (with history) | Requires ≥3 distinct snapshot days |
| `cross_persona` | All (with history) | Requires ≥3 distinct snapshot days |

History-gated sorts degrade to `engagement_rate` when the gate isn't met.

## First run — seed the DB

```bash
# Seed from scratch JSON files (no browser needed)
.venv/bin/python -m core.ingest --seed-only

# Then start the API
.venv/bin/uvicorn api.main:app --reload --port 8001
```

Or trigger via the API after it's running:

```bash
curl -X POST "http://localhost:8001/refresh?seed_scratch=true"
```
