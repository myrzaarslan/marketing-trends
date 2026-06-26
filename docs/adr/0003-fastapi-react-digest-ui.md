# FastAPI + React for the digest UI

**Context.** The Digest UI needs interactive filtering/sorting — Q-1 resolved that Ranking Strategy
is *user-selectable*, so static output is out. The repo is Python ([ADR-0002](./0002-python-despite-typescript-shop.md)).
Crucially, the heavy part (headed-browser ingestion) runs as a **separate process**, so the UI
framework adds **no load** to scraping. A lighter option — FastAPI + Jinja2 + **HTMX**, one Python
process, no build step — was considered and would fully cover today's read-only digest.

**Decision.** Build the UI as a **FastAPI JSON API + a React SPA**, despite the heavier tooling (a
second language/toolchain + build pipeline in a Python repo).

**Why (the trade-off).** We expect frontend requirements to grow — richer interactivity, media
previews, persona/Watchlist management, enrichment displays — and chose to pay React's setup cost
up front rather than migrate off HTMX later. We accept the dual-language maintenance burden.

**Consequences.**
- Repo gains `api/` (FastAPI — exposes the `core` ranker over SQLite as JSON) and `web/` (React).
- The ranker stays in Python `core`; React only consumes JSON (e.g. `GET /digest?geo=&period=&platform=&sort=`).
- Ingestion stays a wholly separate process (no coupling to the UI).
- If the UI stays simple, this is over-built — the bet is that it won't, and that swapping HTMX→React
  mid-stream would cost more than starting on React.
