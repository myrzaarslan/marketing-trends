# Discovery (Layer 2) — surfacing viral posts we DON'T already follow

The Watchlist (Layer 1, built) pulls posts from accounts we *name*. Discovery finds viral
content from accounts we *don't* know about. This doc is the shared brief for the discovery
prototype sessions (see `docs/handoffs/discovery-*.md`).

## The reframe: "global most-viral feed" → "seeded discovery"

There is **no free, reliable, standalone "most viral videos globally" feed** off YouTube
(YouTube is excluded by decision). TikTok's logged-out trending-*video* surfaces are gated;
Instagram/X/Threads have no global viral feed. So discovery at $0 must be **seeded**: collect
posts from sources we *can* reach for free, then rank them. Seeds:

1. **Trending hashtags** — TikTok Creative Center (free global top-3; already in `fetch_trends`).
2. **A curated seed list** — education + KZ/RU-language hashtags (e.g. `#ielts`, `#ентпробный`,
   `#қазақша`, `#онлайнсабақ`, `#repetitor`), maintained like the Watchlist.
3. **Locations** — IG location "top media" for KZ cities (Almaty, Astana).

This is arguably *better targeted* than a global firehose — you seed with your own vertical — and
it is the **only $0 path that reaches KZ/CIS without YouTube** (local-language hashtags + IG
location feeds). That directly answers the requirement that defeated every native trend source.

## New capability: `fetch_viral_posts` (OPTIONAL adapter method)

Distinct from the two existing methods:

| method | input | returns | tied to an account? |
|---|---|---|---|
| `fetch_account_posts` | one Watched Account | that account's recent posts | yes (Layer 1) |
| `fetch_trends` | geo tier | Trend patterns (hashtags) | no |
| **`fetch_viral_posts`** (new) | geo tier + seeds + period | **ranked PostRecords**, not tied to any account | **no** (Layer 2) |

Signature (added to `core/adapter.py` as a NON-abstract, optional method — default raises
`NotImplementedError`, so a platform implements it only if it has a feasible $0 path):

```python
fetch_viral_posts(geo_tier, period_days=7, hashtags=None, locations=None) -> list[PostRecord]
```

Returns the same `PostRecord` shape (full `raw`, capture-everything). It does NOT persist and does
NOT make the final "viral" judgment — see ranking below.

## Provisional ranking (NOT the final viral rule)

The real viral definition is still deferred (OPEN-QUESTIONS Q-1). For discovery, rank the
seed-collected posts by a **provisional** score so the list is useful now:

> `score = view_count` if present, else `like_count + comment_count + share_count + save_count`,
> filtered to posts from the last `period_days`.

Label this clearly as provisional in the output. The final, baseline-aware rule replaces it later.

## Geography at $0 without YouTube

- **Global** → broad / Creative-Center-trending hashtags.
- **KZ / CIS** → Kazakh/Russian-language + local hashtags, **and** IG `location_medias_top` for KZ
  city location PKs. This is the $0 KZ discovery path; treat its coverage as best-effort.

## Fuller source: FYP / Explore burner-harvest (the discovery spine)

The hashtag/sound/location/keyword/graph lenses are *samples*. A fuller sample comes from
harvesting the platform's OWN algorithmic feed — **TikTok For You, Instagram Explore** — via
Playwright browser automation, shaped to education + KZ (persona + region). The algorithm itself
surfaces viral content, so this is closer to the true picture than guessing seeds. Still a scrolled
*sample*, never a complete index (completeness stays paywalled — YouTube API / paid providers).
**Lower-risk start: TikTok FYP works partially LOGGED-OUT (no account to ban).** Full build brief +
ban analysis: `docs/handoffs/discovery-fyp-harvest.md`. Pair with **Reddit API + Google Trends**
(free) as a virality *radar* to auto-generate better seeds.

## Per-platform sourcing (verify-first — see each handoff)

- **TikTok (primary discovery engine):** seed hashtag → `TikTok-Api` `hashtag(name).videos()`
  (signed `challenge/detail` + item list) → rank. Also probe the public `/discover` and
  `/trending/detail/` pages (`__UNIVERSAL_DATA_FOR_REHYDRATION__` embedded JSON, no auth). Needs the
  headed browser; `EmptyResponseException` = bot-block → back off / residential IP (Q-3).
- **Instagram (secondary, heavy):** `hashtag_medias_top(tag)` + `location_medias_top(pk)`. Warmed
  burner + slow pacing; this is IG's *most-throttled* endpoint and wants a residential proxy for
  any volume (Q-3).
- **X (optional spike):** guest-token GraphQL `SearchTimeline` for a hashtag/keyword → posts.
  Video virality on X is weak; low priority.
- **Threads:** no good free discovery surface today — skip until there's demand.

## Rules (unchanged)
Obey the ingestion contract: return `PostRecord`s, full `raw`, no persistence, no final viral
judgment. **Verification-first:** each session's first task is to confirm its seed→videos path
returns real data from a home IP *today*, and document any block (this is volatile).
