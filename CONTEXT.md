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
A user-triggered, on-demand re-pull of source data, distinct from the scheduled daily ingestion that produces the baseline digest.
_Avoid_: sync, reload
