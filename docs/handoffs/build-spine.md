# Build handoff — Core Spine (storage + ranker + API + React)

You are a SONNET build agent. Build the core spine that turns the existing adapters' output into a
ranked, browsable Digest. **Escalate any non-trivial design fork back to the Opus orchestrator — do
not guess on architecture.**

## Read first (source of truth — do not contradict)
`docs/CORE-SPINE.md` (your spec) · `docs/SIGNALS.md` (per-platform signal availability + ranking) ·
`docs/INGESTION-CONTRACT.md` · `core/schema.py` · `core/adapter.py` · `docs/adr/0003-*` ·
`docs/OPEN-QUESTIONS.md` (Q-1 ranking, Q-5 storage).

## Own ONLY these paths (3 agents run in parallel — stay in your lane)
`core/storage.py`, `core/ranker.py`, `core/ingest.py` (the `run_ingestion()` entrypoint), `api/`,
`web/`. You may EXTEND `core/schema.py`. **Do not touch** `core/harness/`, `enrichment/`, or the
`adapters/`.

## What to build (in this order)
1. **Storage — SQLAlchemy over SQLite (`data/trends.db`).** You own ALL DDL: `posts` (incl.
   `thumbnail_path`), `post_snapshots` (full `raw` every snapshot), `accounts`, and **`post_content`**
   (the Layer-3 surface — define it per CORE-SPINE even though another agent writes its rows). Expose
   clean `core.storage` functions, incl. `write_post_content(...)` and `set_thumbnail_path(...)` that
   the enrichment track will call.
2. **`run_ingestion()` (`core/ingest.py`)** — pulls configured adapters/watchlist, upserts `posts`,
   appends a `post_snapshots` row tagged with `source`, downloads each post's thumbnail
   (`posts.thumbnail_path`), then **calls the ranker** to get the top-N per (platform × geo_tier) on
   the default sort and **calls `enrichment.enrich(top_n_ids)`** for the newly-surfaced ones. Import
   enrichment lazily and **no-op gracefully if the `enrichment/` module isn't present yet** (it's
   built in parallel).
3. **Ranker (`core/ranker.py`)** — on-demand, multi-strategy, computed at query time. Sorts: raw
   counts · engagement-rate (default) · share-rate · save-rate · velocity · relative-to-baseline ·
   cross-persona breadth. **Degrade/disable per platform** (SIGNALS.md) and **gate history sorts to
   ≥3 distinct snapshot days**. The **`period_days` filter keys on `first_seen_at`** (recency-to-us);
   `posted_at` is an optional secondary filter.
4. **API — FastAPI, READ-ONLY over SQLite.** `GET /digest?geo=&period=&platform=&sort=` returns ranked
   cards (identity, stats, thumbnail_path, chosen score, content-bundle presence). **`POST /refresh`
   = background trigger** (spawn the separate `run_ingestion` process, return immediately) +
   `GET /refresh/status`. The API must NEVER drive a browser.
5. **Web — Vite + React + TypeScript SPA.** Dropdown filters (geo/period/platform/sort) → grid of
   cards (thumbnail, stats, score, link). Gray out unavailable sorts per SIGNALS + the ≥3-day gate.

## Definition of done
A live `GET /digest` over real data (seed the DB by running `run_ingestion` against the working
adapters, or import the `data/*_accumulator.json` scratch), default sort = engagement-rate, sorts
correctly degraded per platform; React grid renders it. Pin deps; add a short README in `api/` + `web/`.
