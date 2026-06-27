# Marketing Trends

An internal tool for the marketing team of an educational-technology company. It surfaces what is gaining traction on social media — both the broad zeitgeist and what specific education competitors are doing — so marketers can plan content. Coverage spans Kazakhstan / CIS and the rest of the world.

## Language

**Trend**:
A recurring *pattern* gaining traction on social media — a hashtag, an audio/sound, or a video format/challenge. Answers "what theme should we make content about?" A Trend is not tied to a single post.
_Avoid_: topic (too vague), hashtag (only one kind of Trend)

**Post Record**:
The stored unit of capture: one post pulled from a platform, kept as **raw + normalized** — the complete original platform payload (`raw`) *plus* a normalized common field set. We capture maximal metadata up front and decide later which records qualify as Viral Posts.
_Avoid_: row, item, scrape

**Viral Post**:
A Post Record judged to exhibit abnormal engagement, i.e. one worth imitating. The *detection rule* is deliberately undecided (see OPEN-QUESTIONS.md Q-1) — for now every post is captured as a Post Record and "viral" is a label applied later. Carries a **media type** so video posts (IG/TikTok/YouTube) and text posts (X/Threads) live in one list, filterable by type. Distinct from a Trend: a Viral Post is one artifact; a Trend is a pattern across many.
_Avoid_: viral video (it's just a Viral Post with media type = video), trending post

**Media Type**:
The form of a Viral Post's content: `video`, `image`, or `text`. Lets the unified Viral Post list serve both the "video to imitate" need and the "what's the discourse" need.

**Content Bundle**:
The complete *extracted* content of a top-ranked Viral Post — every media file (video, all carousel images, cover/thumbnail), the full caption and any spoiler-hidden text, the sound/music used, and the author's identity — captured so a marketer can study and recreate the post. It records what the post *contains*, not an automated interpretation of what it *depicts* (bot understanding is deliberately out of scope — see OPEN-QUESTIONS Q-6). Produced only for the top-N of a Digest, never the whole corpus.
_Avoid_: enrichment (that's the *process* that produces a Content Bundle), media dump, scrape

**Song (Sound)**:
A piece of audio used across posts, surfaced as its own rankable entity — "what audio is going viral." Identified **per-platform** by a stable key (`sound_id` where present — TikTok `music.id` / Instagram `audio_cluster_id` — else a `name:<normalized>` fallback); a TikTok sound and an Instagram sound are separate Songs (no fuzzy cross-platform matching). TikTok + Instagram only (the platforms with reliable sound metadata). A Song aggregates the Posts that use it and carries every ranking metric at once — **reuse count** (the headline: how many videos use it), adoption (post count), distinct creators, total reach (views), total engagement, average engagement rate, and **rising** (recent adoption) — the user picks which to rank by (default **Reused most**), with the same period filter and Refresh model (Seen/Pinned/Hidden) as the post Digest.
_Avoid_: track (ambiguous with the post time series), audio file (that's one media item), trend (a Song is one artifact; a Trend is a pattern)

**Reuse count / Sound pivot**:
The platform's *own* count of how many videos use a Song — the real "reused most" signal, which the post aggregate can't see (we only ever sample a few of a sound's videos). It's obtained by a **sound pivot**: hitting the platform's sound page — TikTok `/api/music/detail` (`stats.videoCount`, e.g. 740K) or Instagram's audio page `clips/music/` (`formatted_clips_media_count`, parsed to a magnitude) — which also returns a batch of the videos using the sound (upserted as Posts, source `sound_pivot`). Pivot candidates come from platform trending-sound lists (TikTok FYP sounds / IG `music_top_trends`) and the sounds already in the corpus. A pivoted Song stores an authoritative `Sound` row; an un-pivoted one falls back to our **observed** post-count (a floor, shown with a `+`/`≈`), so the list is never empty.
_Avoid_: play count (that's a per-video view metric), popularity (vague)

**Watchlist**:
The human-curated set of accounts the tool monitors for Viral Posts. The operational definition of "education accounts we care about" — whatever marketing adds is, by definition, in scope.
_Avoid_: feed, sources, follow list

**Watched Account**:
A single account on the Watchlist.

**Discovery**:
The automated surfacing of *not-yet-watched* accounts (via education hashtags/keywords) as candidates for a human to approve onto the Watchlist. Suggests; never auto-adds.
_Avoid_: crawl, scan

**Segment**:
A tag on a Watched Account classifying its kind: `direct_competitor`, `edu_influencer`, `global_edtech`, or `adjacent`. Used to filter the digest and to seed Discovery hashtags. `adjacent` (studygram / productivity / parenting) is the noisiest and is expected to be filtered out by default.
_Avoid_: category, type

**Snapshot**:
One timestamped observation of a Post's metrics (the engagement counts + author follower count), tagged with the Source that surfaced it. The *series* of Snapshots over time is what makes velocity and cross-persona breadth computable — a single scrape can't.
_Avoid_: reading, sample, scrape

**Source**:
The provenance of a Snapshot — which persona / seed / Watched Account surfaced this post on this run. Basis for cross-persona breadth (the same post seen by many Sources = broadly pushed).
_Avoid_: origin, channel

**Digest**:
The ranked, filterable view of Posts the marketing team consumes, sliced by Geo Tier / period / platform and ordered by a chosen Ranking Strategy.
_Avoid_: feed, report, dashboard (the dashboard is how a Digest is shown)

**Ranking Strategy**:
A user-selectable lens for ordering the Digest (e.g. save-rate, share-rate, velocity, relative-to-baseline, cross-persona breadth). No single one is canonical — the user picks (see OPEN-QUESTIONS Q-1).
_Avoid_: viral score, algorithm

**Geo Tier**:
The geographic bucket a Trend or Viral Post is presented under: `KZ` (Kazakhstan), `CIS` (other CIS countries, lumped), or `World`. Finer per-country data is stored underneath for later drill-down. For the Watchlist, Geo Tier is a manual tag per account; for Trends it comes from a native region parameter where the platform supports it, otherwise inferred from language.
_Avoid_: region, country, market

**Refresh**:
A user-triggered, on-demand rebuild of the Digest, distinct from the scheduled daily ingestion. Three modes:
- **Soft refresh** — re-rank and re-enrich the *current* set in place (no rotation). The cheapest mode.
- **Hard refresh** — rotate the current set out (mark it Seen) and pull the next **unseen** working set. The source is the user's choice: `corpus` (rank the next-best unseen posts already stored — fast, default) or `live` (harvest brand-new posts from the platform adapters first — slow, the expensive path).
- **Selective refresh** — the same rotation, but applied only to specific cards the user selects, leaving the rest in place.
_Avoid_: sync, reload

**Seen**:
Per-post state recording that a Post has been shown in the Digest at least once (`last_served_at`). Hard/selective Refresh marks the outgoing posts Seen so the next pull returns posts the user hasn't encountered — making each hard refresh unique. When the unseen pool is exhausted, the least-recently-Seen posts are **recycled** (Seen state cleared) so refresh always has content.
_Avoid_: viewed, read, visited

**Working Set**:
The rotating subset the Digest shows after a hard/selective Refresh: never-Seen posts plus any Pinned posts, ranked by the chosen Strategy. The home Digest shows the full ranked corpus by default and switches to the Working Set only after a hard refresh (with a "show all" escape).

**Collection**:
A user-created, named group of saved Posts (title + optional description). A Post may live in many Collections; Collections never alter ranking or the corpus — deleting a Collection leaves its Posts untouched. The user's own curation layer on top of the Digest.
_Avoid_: folder, playlist, board

**Note**:
A single editable free-text note attached to a Post **per User** — it shows everywhere that user sees the Post (home Digest, every Collection, the post viewer). One Post, one Note per user.
_Avoid_: comment (that's platform-side), tag, annotation

**Pinned / Hidden**:
Two per-User, per-Post flags that steer Refresh. **Pinned** posts survive a hard refresh (they stay in the Working Set). **Hidden** posts are removed from the Digest entirely ("don't show me this") — excluded from every list except an explicit `include_hidden` request. Both apply to the Post for that user, independent of any Collection.
_Avoid_: starred/blocked, favorite/mute

**Corpus**:
The central, shared source of truth — the union of everything every Node has harvested. Two stores: **Postgres** for canonical metadata (PostRecords, append-only Snapshots) and **S3** object storage for media bundles. The Corpus never scrapes (so it can live on a datacenter IP); Nodes contribute to it. See [ADR-0006](docs/adr/0006-distributed-local-nodes-central-corpus.md).
_Avoid_: database (ambiguous — the local Replica is also a database), backend

**Node**:
One marketing-team member's local install. A full app that harvests + enriches on its own **residential IP**, contributes results to the Corpus, and serves a local Replica for browsing. There is no central scraper — the Nodes' consumer connections are the egress.
_Avoid_: client (it both reads and writes), server

**Replica**:
The Node's local SQLite copy of the Corpus, synced forward by a **watermark** cursor. Used for fast/offline reading and ranking; never the source of truth. Harvested data is write-through to the Corpus, not authoritative on the Node.
_Avoid_: cache (it's a queryable replica, not just media bytes), mirror

**User**:
An identity within a Node, established name-only: type a name to create, or pick an existing name to log in (no passwords — internal tool). Personal state (Collections, Notes, Pinned/Hidden/Seen) is namespaced by `user_id` and synced through the Corpus so the team can share it.
_Avoid_: account (overloaded with platform login accounts), profile
