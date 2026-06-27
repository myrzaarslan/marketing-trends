# Distributed local nodes over a central corpus

**Context.** The product ships to a non-engineering **marketing team**, and the question was how to
deploy it. The naive answer — host it on a cloud server — breaks the core premise: harvesting must
come from **residential/mobile IPs** ([ADR-0001](0001-diy-scraping-under-zero-budget.md),
[robust-harvest.md](../handoffs/robust-harvest.md)). A datacenter IP hitting TikTok/IG/Threads/X is
banned within hours. So the **write path (harvest + enrich) cannot be centralized onto a server**.
The **read path (browse the Digest)**, on the other hand, wants to be fast and offline-tolerant.
These two halves have opposite constraints.

The team also wants a single shared knowledge base ("the general corpus"), every member's machine to
contribute harvest (spread the IP load), and to share collections/notes across the team.

**Decision.** Run the app as **distributed local nodes over a central corpus**.

- **Every laptop is a full node.** It runs the adapters + enrichment on its own residential IP and
  **contributes** what it harvests back to the corpus. There is no central scraper — the team's
  consumer connections *are* the egress, which maximises ban resilience (one IP burning never sinks
  the others, per the blast-radius reasoning in [ADR-0004](0004-captcha-solving-experiment.md)).
- **The corpus is central and is the source of truth.** Two stores:
  - **Postgres** — canonical metadata: `PostRecord`s (upsert by `(platform, platform_post_id)`) and
    `PostSnapshot`s (append-only time series). The prototype's SQLite schema migrates here largely
    as-is.
  - **S3** (object storage) — the media bundles. Whichever node harvested a video/image **uploads it
    once**; every other node streams/caches it by URL and **never re-fetches it from the platform**.
- **Local SQLite is a read replica, not the source of truth.** On launch a node pulls the corpus
  **delta since its last watermark** into local SQLite, so browsing/ranking is all-local and instant.
  Append-only snapshots + idempotent upserts make the merge conflict-free. The background pre-download
  worker fills the local media cache from S3 (fast, zero ban risk).
- **Identity is name-only.** On onboarding a user types a name to create an account, or selects an
  existing name to log in. No passwords (internal-team tool). A `user_id` is keyed to that name.
- **Personal state is per-user and synced.** Collections, Notes, and hide/pin/seen flags are
  namespaced by `user_id` in the corpus so the team can see and share them. **This supersedes the
  "global per post" decision in [ADR-0005](0005-refresh-seen-state-model.md)** — those tables gain a
  `user_id` column; "one post, one state" becomes "one post, one state *per user*."
- **Watchlist coordination is loose for now.** A few shared platform sessions; no strict per-account
  ownership. Redundant scraping of the same account from multiple nodes is accepted — upsert/dedup
  keeps the corpus correct, and snapshot de-duplication keeps the series clean. Partitioning the
  watchlist per node is a known, deferred optimisation if platform contact volume becomes a problem.

**Why (the trade-off).** Centralising the corpus but distributing the harvest is the only shape that
satisfies both constraints at once: the DB/media can live on a cheap box with a datacenter IP because
**the corpus never scrapes**, while scraping stays on residential IPs because **the nodes do**. The
local SQLite replica buys snappy, offline browsing without making each laptop authoritative — the
expensive/risky work (harvest, enrich, upload) is write-through to the corpus, the cheap work (read,
rank, browse) is local. We accept the operational weight of putting the full scraper +
Playwright/sessions on non-engineer laptops (the price of distributed IPs) and the redundancy of
uncoordinated harvest (the price of skipping a scheduler) as deliberate, reversible simplifications.

**Consequences.**
- The corpus needs a small **sync API** (`GET /sync?since=<watermark>` → posts/snapshots/flags delta;
  `POST /contribute` → push harvested records + presigned S3 upload). Local SQLite tracks a per-table
  watermark (`max(updated_at)` / monotonic id).
- `post_flags`, `collections`, `collection_items`, `post_notes` all gain a **`user_id`** column;
  every personal-state query filters by the logged-in user. Migration is additive.
- Media URLs in `ContentBundleResponse` become **S3 URLs** (presigned or via a thin proxy) instead of
  local `/media/...` paths; the local cache sits in front for speed.
- The captcha experiment ([ADR-0004](0004-captcha-solving-experiment.md)) now runs **per node**, so
  its default-off circuit breaker and disposable-session isolation matter even more — a node must
  never escalate its own residential IP into a hard ban that poisons its contribution stream.
- A node going offline degrades gracefully: it stops contributing and serves its last local replica;
  on reconnect it resumes from its watermark.
- Open knobs for later: watchlist partitioning/claim, conflict policy if personal state is ever
  edited on two nodes before sync (last-writer-wins is the assumed default), and whether S3 is real
  AWS S3 vs an S3-compatible cheaper backend (R2/B2/MinIO) — the code targets the S3 API either way.
