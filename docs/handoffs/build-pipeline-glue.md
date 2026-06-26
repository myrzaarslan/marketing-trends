# Build handoff — Daily pipeline glue (wire live adapters + per-platform lanes + timing)

You are a SONNET build agent. Turn `run_ingestion()` from a scratch-seeding stub into the real daily
pipeline that scrapes live, ranks + enriches **per platform**, and renders lane-by-lane. **Obey
anti-ban discipline** (ADR-0001, `docs/handoffs/robust-harvest.md`): jittered pacing, back off on
captcha/429/login-wall, never grind. Escalate genuine design forks to the Opus orchestrator.

## Read first
`docs/CORE-SPINE.md` → "Daily pipeline / runner" + "Continuous rendering" (your spec) · `core/ingest.py`
(the stub to replace — Step 2 is `"live adapter harvest: skipped"`) · `core/ranker.py`, `core/storage.py` ·
each `adapters/*/adapter.py` + README (entry methods + realistic yields) · `enrichment/` (call only).

## Own ONLY these paths
`core/ingest.py`, and a thin runner/config (e.g. `core/pipeline.py`, a watchlist/seed config). You may
add read/write helpers to `core/storage.py`. **Call** adapters + `enrichment.enrich(...)` — do not
rewrite them. Don't touch `api/`, `web/`, `core/harness/`.

## What to build
1. **Wire the 4 adapters into Step 2 as PER-PLATFORM LANES.** For each platform independently:
   `scrape (fetch_account_posts over the watchlist AND/OR fetch_viral_posts discovery) → upsert posts
   + append snapshots (full raw!) → download thumbnails → rank that platform's top-N → enrich its
   top-N (fresh URLs) → mark the lane done`. A finished lane's data must be queryable/renderable
   immediately — **do NOT block on the slowest platform.** Lanes may run sequentially or concurrently,
   but each commits incrementally so the polling UI sees a lane's posts as soon as they land.
2. **Per-platform volume config** (don't hard-chase 1000): a configurable target/limit per platform;
   default to each platform's polite ceiling (IG ~500/run, X across accounts, TikTok ~15–25/session →
   accept low, accumulate across spaced runs). Honor the existing harvesters' spaced/idempotent state.
3. **Per-stage timing instrumentation.** Log + return durations: per-platform scrape, rank, enrich
   (and per-post enrich avg), thumbnail download, and totals. This is how we ESTIMATE how long a daily
   run takes — it's a primary deliverable.
4. **Fresh-run mode** (`--fresh`): wipe `data/trends.db` + `data/media/` + `data/thumbnails/` and run
   a clean live scrape→rank→enrich→(renderable) end-to-end, printing the timing report. This is the
   "start from scratch and estimate" run the operator asked for.
5. Keep the **manual trigger** (`POST /refresh` already calls `run_ingestion`); **no scheduler** (v1
   is manual; a systemd-timer/cron is deferred).

## Definition of done
`run_ingestion()` scrapes live per-platform-lane, commits incrementally, ranks + enriches each lane's
top-N as it finishes, and emits a per-stage timing report. `--fresh` does a clean timed end-to-end.
Anti-ban discipline respected; partial/blocked platforms degrade gracefully (a blocked lane doesn't
fail the run). Report: the timing numbers from a real run, per-platform yields, any blocks hit, and
escalations. Update `core/` READMEs / `docs/CORE-SPINE.md` runner notes as needed.
