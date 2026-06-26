# Core Spine

The layer that turns adapter output into a ranked, browsable Digest. Sits between the adapters
(which only fetch + normalize) and the UI.

```
INGESTION  (one separate, headed-browser process — runs the whole chain while CDN URLs are fresh)
  adapters ─► STORAGE ─► RANK ──────► ENRICH ───────────────► writes posts + snapshots + post_content
  scrape +    SQLite     core ranker   thumbnail for ALL posts  (+ media files on the filesystem)
  normalize   full hist  (per          + full Content Bundle
  + tag       (raw every  platform×    for the NEW top-N
  source       snapshot)  geo, default  (per platform×geo)
                          sort)

         ┌──────────────────────────── reads, never writes ────────────────────────────┐
(read-only) FastAPI  ──►  React SPA          JSON: GET /digest?geo=&period=&platform=&sort=
            over SQLite   Digest UI           POST /refresh = background-trigger the ingestion process

Enrichment is a STEP INSIDE ingestion (signed media URLs expire in hours → download at capture, not
later). The API/web tier is wholly separate and read-only; a UI click NEVER scrapes.
```

## Storage — SQLite, FULL history (decided 2026-06-26)

Keep the **complete `raw` payload per snapshot** (not latest-only). Maximal retention now; storage
optimization (retention windows, compression, Postgres) is deferred — see OPEN-QUESTIONS Q-5. You
can always discard history; you can never re-fetch a count from three weeks ago.

**`posts`** — one row per unique post (identity + slow-changing fields):
`platform`, `platform_post_id`, `account_handle`, `url`, `posted_at`, `media_type`, `caption`,
`sound_id`, `sound_name`, `hashtags` (JSON), `thumbnail_path` (local path — a thumbnail is downloaded
for EVERY post at ingestion so no card is ever dead), `first_seen_at`, `last_seen_at`.
PK = (platform, platform_post_id).

**`post_snapshots`** — one row per observation (the time series):
`id`, `platform`, `platform_post_id` (FK), `fetched_at`, `view_count`, `like_count`,
`comment_count`, `share_count`, `save_count`, `author_follower_count`, `source` (which
persona/seed/watchlist surfaced it), **`raw` (JSON — FULL payload, every time)**.
→ velocity = Δcounts across rows; cross-persona breadth = COUNT(DISTINCT source).

**`accounts`** — Watchlist + discovered:
`handle`, `platform`, `segment`, `geo_tier`, `platform_account_id`, `on_watchlist`.

**`post_content`** — RESERVED for Layer-3 (one row per *enriched* top-N post; written by the
enrichment process, defined here so the spine ships "Layer-3-ready" — see Enrichment below):
`platform`, `platform_post_id` (FK to `posts`), `media_paths` (JSON — relative paths under
`data/media/<platform>/<platform_post_id>/`), `caption`, `spoiler_text`, `sound_id`, `sound_name`,
`sound_author`, `author_display_name`, `extracted_at`, `status` (`pending`/`done`/`expired_url_miss`).
The media *bytes* live on the filesystem, NOT in SQLite. The spine owns this DDL; the Layer-3 track
only writes rows through a `core.storage` function (it never invents its own schema).

## Ingestion / dedup (recommended)
The ingestion process runs the whole chain (scrape → store → rank → enrich) so media is captured
while URLs are fresh:
- Upsert `posts` by (platform, platform_post_id): insert + set `first_seen_at` if new, else bump
  `last_seen_at` and refresh changed static fields.
- **Always append a `post_snapshots` row each run** (even if counts unchanged) — the time series is
  the point. (Skipping identical consecutive snapshots is a later space optimization, not now.)
- Tag every snapshot with its `source`.
- **Download the thumbnail for EVERY captured post** (cheap CDN image GET) → `posts.thumbnail_path`.
- **Call the `core` ranker** (reuse, don't duplicate) to get the current top-N per (platform × geo),
  default sort, then **enrich the newly-surfaced top-N** (download the full Content Bundle while URLs
  are fresh) → `post_content`. Idempotent: skip posts already in `post_content`.

## Ranking — on-demand, multi-strategy (Q-1 resolved: user picks the sort)
Computed at query time over stored rows (no precompute table for the prototype). Each sort is a
function: raw counts · engagement-rate (÷views) · **engagement-rate-followers (÷author follower
count)** · share-rate (where available) · save-rate (TikTok only) · velocity (latest vs earlier
snapshot) · relative-to-baseline (vs the account's median) · cross-persona breadth (distinct sources).
Query params: geo_tier, period_days, platform, sort.
**Degrade/disable** sorts per platform + per data-availability (SIGNALS.md, Q-1 caveats). Default
sort = engagement-rate ÷views (the standard virality read). **Follower-normalized engagement** is a
selectable sort and the only denominator present on **all four** platforms (Threads has no views), so
it doubles as the universal cross-platform normalizer — computable from a single snapshot.
**Period filter (decided 2026-06-26):** the `period_days` filter keys on **`first_seen_at`**
(recency-*to-us*), NOT `posted_at`. Discovery/FYP posts skew evergreen (60–90d old — TikTok README),
so a `posted_at` filter would silently drop the very posts Layer-2 exists to surface. Keying on
first-seen keeps them as "new to us." `posted_at` is exposed as an optional secondary filter for true
post-freshness.

## Daily pipeline / runner (decided 2026-06-26)
A single `run_ingestion()` entrypoint. **Current gap:** its live-adapter harvest (Step 2) is a STUB —
it only seeds from the scratch JSON, ranks, and enriches. **Next build:** wire the 4 adapters into
Step 2 + add per-stage timing so a clean wipe-and-run yields a real duration estimate.

**Per-platform lanes (decided):** each platform is an INDEPENDENT lane —
`scrape platform → store → rank within that platform → enrich its top-N (fresh URLs) → it's renderable`.
Do **not** wait for all four platforms; a finished platform's ranked+enriched posts surface
immediately. This both respects URL expiry and feeds continuous rendering lane-by-lane.

**Trigger (decided):** **manual-only for v1** — the UI "Refresh" button background-triggers
`run_ingestion()` (+ `/refresh/status`). A real scheduler (systemd-timer/cron, $0, home box must be
powered on) is **deferred** until the end-to-end run is proven.

**Volume reality:** "1000/platform/day" is platform-shaped, not flat. IG Explore (~500/run) and X
(paginate across accounts) are reachable; **TikTok FYP (~15–25/session) is the bottleneck** and needs
spaced runs — higher TikTok volume is what the captcha experiment (ADR-0004) + Q-3 IP rotation unlock.
Target each platform's polite ceiling + accumulate across spaced runs; don't hard-chase 1000 (that
invites bans).

## Continuous rendering (decided 2026-06-26)
No blocking spinner. Two parts:
- **Initial load:** paginated `/digest` + infinite scroll + per-card skeletons + lazy media → first
  cards render instantly.
- **During a refresh:** the UI **polls `/digest` every few seconds and appends/re-ranks** as posts
  land (fits the read-only-API + separate-ingestion design — ingestion writes SQLite incrementally,
  per lane). Server-Sent Events / WebSocket streaming is a later upgrade, not v1.

## UI surface — FastAPI + React (ADR-0003)
`api/` = FastAPI exposing the `core` ranker as JSON (`GET /digest?geo=&period=&platform=&sort=`). The
API stays **read-only over SQLite** — it never drives a browser. **`POST /refresh` is a background
trigger** (decided 2026-06-26): it spawns/enqueues the separate headed-browser ingestion process and
returns immediately; the UI polls a `GET /refresh/status` (or similar) for completion. (Synchronous
ingestion-in-a-web-request is rejected — 4 headed browsers would block/timeout and the API box may
have no `DISPLAY`.) `web/` = React SPA: dropdown filters (geo / period / platform / sort) → API calls
→ grid of video cards (thumbnail, stats, the chosen score). Disable/gray sorts per SIGNALS.md + Q-1
caveats. Heavier than HTMX on purpose — we expect frontend needs to grow.
**Post Content Bundle view (decided 2026-06-26):** beyond the card grid, each post opens a full
Instagram-style **specimen viewer** (lightbox) showing the downloaded Content Bundle — playable
video / swipeable carousel / image, caption + hashtags, **spoiler blur-to-reveal** (Threads CW),
a **playable music chip + title**, engagement stats (degraded per SIGNALS — never a fake 0), a
provenance strip, and an **"open original" link**. Non-enriched long-tail posts degrade to
thumbnail + stats + caption + link. The API gains a read-only static mount of `data/media/` and a
`GET /post/{platform}/{platform_post_id}` Content Bundle endpoint.

## Enrichment / Layer-3 (a STEP INSIDE ingestion — produces Content Bundles)
After ranking, the **top-N only** feed Layer-3, which performs **pure content EXTRACTION** (decided
2026-06-26): for each top-N post it reads the stored `raw` for the media CDN URLs (signed +
**expiring** → fetch promptly), downloads every media file to `data/media/<platform>/<platform_post_id>/`,
captures the caption + any spoiler-hidden text (Threads tap-to-reveal is UI-only → already in `raw`),
the sound/music, and the author identity, then writes one `post_content` row via `core.storage`.
- **No automated understanding** (OCR / ASR / multimodal "label trends by bot") in v1 — deferred,
  OPEN-QUESTIONS Q-6.
- **No comments** in v1 (author self-comments dropped — extra per-post fetch = extra ban surface;
  revisit later). Layer-3 therefore does NOT touch the adapter contract.
- **Top-N seam (decided 2026-06-26):** "top-N of what?" → **top-N per (platform × geo-tier) of the
  DEFAULT (engagement-rate) sort, standard period.** NOT global (one platform would crowd others),
  NOT per-sort/per-slice (a Content Bundle is a property of the *post* — its media/caption/sound —
  independent of which sort surfaced it, so per-sort enrichment just re-downloads the same files).
- **Idempotent / deduped by post identity:** enrich each post **once, ever**. The daily job
  *ensures* the current top-N are enriched; most already are. Steady-state new downloads = only the
  few newly-surfaced posts entering the top-N. `N` default **25, tunable**.
- **EAGER, at ingestion — NOT lazy (decided 2026-06-26).** Signed CDN URLs expire in hours, so
  media MUST be downloaded the moment the post is scraped, while the URL is fresh. Enrichment
  therefore runs **inside the ingestion process** (the headed-browser process), right after it
  stores snapshots: it calls the `core` ranker to get the current top-N (per platform×geo, default
  sort) and downloads the new top-N's media before URLs expire. **There is NO re-fetch and NO lazy
  inline scrape** — that would put scraping in the read-only API path, need a `fetch_post(by-id)`
  method we don't have, and be impossible for discovery posts anyway. Rejected.
- **No dead cards, without any lazy scrape:** download the tiny **thumbnail for EVERY captured post**
  at ingestion (a cheap CDN image GET) → every card has a working thumbnail forever. The long tail
  (non-top-N) shows **stats + stored thumbnail + an outbound link** to the live post; only the top-N
  get the full downloadable Content Bundle. Graceful degradation, not a dead end.
- **Accepted $0 constraint — enrich-at-ingestion-or-never for discovery posts.** A `fetch_viral_posts`
  post has no account and no by-id endpoint at $0, so if it wasn't top-N when captured, its media is
  unrecoverable (keep stats + thumbnail + link). An optional async "enrich this" for *Watchlist*
  posts (which have an account to re-pull) is **post-v1**, and even then an **async enqueue to the
  ingestion worker, never inline**. (`status=expired_url_miss` records the miss; no auto re-fetch.)
- **Seam for parallel build:** Layer-3 is a function `enrich(post_identities)` — input is today's
  top-N per (platform × geo) on the default sort, **minus those already in `post_content`**. It reads
  each post's freshly-captured `raw` for the media URLs, downloads files, and writes `post_content`
  via `core.storage`. Buildable/testable against a **stub list of post identities** before the ranker
  is wired; the ingestion process calls it for real once the ranker exists. (No on-demand path in v1.)
- YouTube is excluded (consistent with prior trend-source decisions).

## Build / stack decisions (2026-06-26)
- **Storage access:** **SQLAlchemy** (ORM) over the SQLite file — chosen over raw `sqlite3` to make
  the eventual Postgres migration (Q-5) cheap. Adapters still never touch the DB; only `core` does.
- **DB schema ownership:** the **spine** track owns ALL DDL (`posts` incl. `thumbnail_path`,
  `post_snapshots`, `accounts`, `post_content`). Other tracks read/write only via `core.storage`.
- **Gated sorts — three INDEPENDENT gates (refined 2026-06-26):** only ONE sort is truly
  time-dependent. Each grays out in the UI on its *own* gate:
  - **velocity** — TIME gate: needs **≥2 snapshots separated in time** ("rising" is undefinable from
    one point). The only genuinely history-dependent sort.
  - **relative-to-baseline** — CORPUS gate: needs **≥3 other posts of the same account**, NOT calendar
    time. A single scrape of an account's recent posts (`fetch_account_posts` returns ~30 + follower
    count) makes the median computable on **day one**. (Cheap for Watchlist accounts we scrape anyway;
    a *discovered* creator needs one extra profile scrape to populate their corpus.)
  - **cross-persona breadth** — SOURCE gate: needs **≥2 distinct sources**; breadth accrues across
    persona/seed harvests, not calendar days.
  - **engagement-rate-followers** — gated only on a non-null follower count (one field, one snapshot).
- **Media download (Layer-3):** direct CDN GET using the URL+headers from `raw`; **fall back to
  yt-dlp** for TikTok video files that need signing. Thumbnails are always a plain image GET.
- **Media retention:** keep all downloaded media **forever** for the prototype (mirror Q-5's
  "don't pre-optimize"); a retention/cap decision is deferred to when it hurts.
- **Frontend:** **Vite + React + TypeScript** SPA (read-only digest, no SSR needed).
- **Track path ownership (so 3 parallel agents don't collide):** spine → `core/` (schema, storage,
  ranker) + `api/` + `web/`; Layer-3 enrichment → `enrichment/` (imports `core` read-only, writes via
  `core.storage`); captcha experiment → `core/harness/captcha_solver.py` + `docs/captcha/` +
  `data/captcha_registry.json`. Disjoint paths; only the spine edits `core/schema.py` & `core/storage.py`.
