# Refresh as seen-state rotation, not re-scrape

**Context.** "Refresh" was a single button that re-ran ingestion. Users wanted more: a *hard*
refresh that returns a **new, unique set each time** (posts they haven't seen), the ability to
**keep** specific posts across a refresh, to **remove** ones they don't want, and to refresh only a
**selection** of cards. The corpus is large and re-scraping 1000+ posts to rotate a handful is
wasteful — and the team explicitly flagged live harvesting as the expensive path
([CONTEXT.md → Refresh](../../CONTEXT.md)).

**Decision.** Model refresh as **rotation over per-post Seen state**, decoupled from scraping.
A `post_flags` row carries `last_served_at` (Seen), `pinned`, and `hidden`:

- **Soft refresh** re-ranks/re-enriches the current set in place; touches no Seen state.
- **Hard refresh** marks the outgoing posts Seen and serves the next **unseen** Working Set
  (unseen ∪ pinned, minus hidden), ranked normally. Source is the user's choice:
  `corpus` (rank the next-best already-stored posts — fast, default) or `live`
  (`harvest_live()` pulls brand-new posts from the adapters first — slow).
- **Selective refresh** is the same rotation scoped to user-selected cards.
- When the unseen pool can't fill a page, the **least-recently-Seen** posts are recycled
  (Seen cleared) so refresh never starves.

Hidden posts are excluded from every Digest by default; pinned posts always survive rotation.

**Why (the trade-off).** Seen-state rotation makes "unique each time" a cheap O(corpus) ranking
filter instead of a fresh harvest, so the common case (corpus) is instant and live scraping is
opt-in per the cost concern. Recycling accepts that "unseen" is eventually a lie on a finite corpus
— we'd rather resurface the oldest-seen post than show an empty list. Seen/pinned/hidden are
**global per post** (not per Collection), keeping the mental model "one post, one state" — the same
reason Notes are global.

**Consequences.**
- New table `post_flags` (+ `collections`, `collection_items`, `post_notes`); `init_db` creates
  them additively, no migration. `foreign_keys=ON` makes collection-item cascade deletes work.
- `GET /digest` gains `unseen_only` (Working Set) and `include_hidden`; `POST /refresh/hard` runs
  the rotation as a background job, polled like soft refresh.
- The UI home Digest defaults to the full ranked corpus and flips to the Working Set after a hard
  refresh, with a "show all" escape — so rotation is visible without hiding the corpus.
- `harvest_live()` is best-effort and bounded per platform; one platform failing (browser/session)
  never sinks the others. X (auth-free syndication) is the reliable path; the rest are heavier.
- Seen state is **not** time-series history — it never feeds ranking signals (velocity etc.),
  avoiding any contamination of the Snapshot series.
