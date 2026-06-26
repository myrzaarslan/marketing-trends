# X (Twitter) adapter — feasibility spike

**Recommendation: conditional GO for v1 (Viral Posts only; no Trends).**

The handoff framed X as a likely cut — "low effort-to-value at $0, hostile to free
scraping." That's true of the paths the handoff named (snscrape, Nitter). But a
different free path — X's own **syndication / embed timeline endpoint** — works
reliably from a home IP today with **no auth, no login, and no paid API**, and
returns rich per-tweet engagement data. The effort-to-value math flips: this
adapter is ~200 lines of stdlib, zero pip dependencies, and pulls real,
correctly-normalized Post Records. It is worth keeping in v1 for Watchlist
monitoring. Trends remain a no-go (no free source). See caveats before relying on it.

> **Round-two update (2026-06-25): `view_count` is recoverable after all.** The one
> gap the syndication path can't see — impressions — *is* reachable via X's
> **guest-token GraphQL `UserTweets`** endpoint, which (surprisingly) still works
> in 2026 with **no login and no account**, only an anonymous guest token. It's
> implemented as an **opt-in** path (`XAdapter(prefer_graphql=True)`), OFF by
> default because it's markedly more fragile. Syndication stays the default. See
> "[Optional: view_count via guest-token GraphQL](#optional-view_count-via-guest-token-graphql)".

> **Round-three update (2026-06-26): ≥500 posts unattended is achievable.**
> `harvest_500.py` accumulates 500+ unique posts from 7 accounts across 23
> cursor pages in one unattended run, zero rate-limit hits. See
> "[Unattended 500 — blockers and paths](#unattended-500--blockers-and-paths)".

> **Round-four update (2026-06-26): persisted records are now full canonical PostRecords.**
> `harvest_500.py` previously saved a lossy hand-rolled projection (only `like_count` and
> `view_count` reliably populated; `caption`, `url`, `posted_at`, `comment_count`, `share_count`,
> `author_follower_count`, `media_type`, `hashtags`, `raw` all absent). Fixed: every harvested
> GraphQL node now routes through `XAdapter._graphql_to_post_record` (the same verified normalizer
> used by the regular adapter), then serializes via `_post_record_to_dict` (all SIGNALS.md Tier 1
> fields). Coverage after re-run: `id` / `url` / `handle` / `caption` / `posted_at` / `fetched_at`
> / `media_type` / `like_count` / `comment_count` / `share_count` / `author_follower_count` /
> `geo_tier` / `raw` — all **100%**. `view_count` **61.8%** (GraphQL path only; `None` on
> syndication — by design). `hashtags` 12.3% (most tweets have no hashtags). `save_count` 0%
> (X bookmarks are not public — correct, not faked). `duration_sec` / `thumbnail_url` present for
> video/image posts only.

## What works

**Data path:** `GET https://syndication.twitter.com/srv/timeline-profile/screen-name/<handle>?showReplies=false`

This is the endpoint that powers X's embeddable profile timelines. It returns an
HTML page with the ~20 most recent tweets as JSON inside a Next.js
`<script id="__NEXT_DATA__">` island. We parse that island and normalize each tweet.

Observed on 2026-06-25 from a home IP against `@Duolingo`, `@khanacademy`, `@NASA`:
- **HTTP 200, ~1.0–1.3 s/request.** A 6-request burst stayed 200 throughout — no
  rate-limiting at the low volume a Watchlist poll needs.
- **19–20 tweets per account**, each with full engagement counts and entities.
- All media variants normalize: `text`, `image`, `carousel` (multi-photo),
  `video` (with `duration_sec`). Hashtags extract from `entities.hashtags`.

Run the live check yourself:

```bash
.venv/bin/python -m adapters.x.verify      # from repo root
```

## Unattended 500 — blockers and paths

Run the harvest:

```bash
.venv/bin/python -m adapters.x.harvest_500
```

**Verified 2026-06-26 from a home IP:**
- **503 unique posts** in one unattended run
- 7 accounts × 23 cursor pages (Phase 1: GraphQL UserTweets)
- 0 rate-limit hits, 0 human intervention
- Phase 2 (syndication fallback) was not needed

### The two working paths

**Path A — GraphQL `UserTweets` cursor pagination (Phase 1, preferred)**

Each call to `get_user_tweets_page(rest_id, cursor=…)` returns a page of
tweets + a `next_cursor` token. Following the cursor produces as many tweets as
the account has posted. Observed yields per-account: 50–100 unique tweets across
5 cursor pages (varies by account posting rate and dedup overlap).

Rate-limit (429) handling:
- `_gql_with_backoff`: exponential backoff 5 → 10 → 20 → 40 → 80 s + guest-token
  refresh after each 429. The client tracks `rate_limit_hits` and
  `backoff_seconds_total` for reporting.
- Inter-page jitter sleep: 1.5–3.0 s. Inter-account sleep: 2–4 s.
- In practice (7 accounts, 23 pages): 0 rate-limit events.

**Path B — Syndication breadth (Phase 2, fallback)**

When GraphQL falls short, the syndication endpoint pulls ~20 tweets per account,
no auth, no cursor needed. 25 accounts = ~500 posts. Used as a gap-filler; in the
2026-06-26 run it contributed 0 (GraphQL covered all 500).

### SearchTimeline: login-gated — escalation question for Opus

`SearchTimeline` is the GraphQL operation that would allow search-by-hashtag/keyword
with cursor pagination across topics (not just known account handles). We confirmed:

1. The **correct current query ID** is `Bcw3RzK-PatNAmbnw54hFw`, extracted from
   X's live JS bundle (`main.a1a43c5a.js`). All previously known IDs are stale.
2. **The endpoint returns HTTP 404 for guest-token requests** — same empty 404
   regardless of query ID, API base URL, or header set.
3. Confirmed `UserTweets` works (HTTP 200) with the same guest token at the same
   time — so the token is valid; SearchTimeline specifically requires user-auth.

**Conclusion:** SearchTimeline is login-gated. No amount of query-ID rotation or
header tricks will fix it; the server rejects all non-user-auth requests with 404.

**Escalation question for Opus:** Should we add user-bearer-token support (login
cookies / auth_token) to unlock SearchTimeline for topic-based discovery? The
trade-offs:
- Pro: enables hashtag/keyword search → topic-level harvest across thousands of
  accounts we don't know about, not just the Watchlist.
- Con: ties the harvester to an authenticated X account (burner or real). If that
  account gets suspended, search harvest stops. Higher operational complexity.
- Alternative (no-login): stick with account-timeline cursor pagination across a
  larger Watchlist (~25 accounts) — achieves 500+ unattended but is account-list
  bounded, not topic-bounded.

Current implementation chooses the no-login alternative and reaches 500 cleanly.

## Library & dependencies

- **None.** Pure Python standard library (`urllib`, `json`, `re`, `datetime`, `time`).
  No snscrape, no `requests`, no API client.

## Auth / IP requirements

- **No auth, no login, no cookies, no API key.** Anonymous GET / guest token.
- Needs a **browser-like `User-Agent`** or the syndication endpoint returns an empty
  shell.
- Worked from a plain **home IP**. Datacenter-IP behavior is untested — see
  OPEN-QUESTIONS Q-3.

## Field mapping (normalized ← raw)

| Normalized field | Source | Notes |
|---|---|---|
| `platform_post_id` | `id_str` | |
| `account_handle` | `user.screen_name` | author of the rendered tweet |
| `url` | `permalink` → `https://x.com…` | |
| `posted_at` | `created_at` | parsed `%a %b %d %H:%M:%S %z %Y` |
| `media_type` | `extended_entities.media[].type` | photo→`image`, >1 photo→`carousel`, video/gif→`video`, none→`text` |
| `caption` | `full_text` (fallback `text`) | |
| `hashtags` | `entities.hashtags[].text` | |
| `duration_sec` | `video_info.duration_millis / 1000` | video only |
| `like_count` | `favorite_count` | |
| `comment_count` | `reply_count` | |
| `share_count` | `retweet_count` | reposts; `quote_count` stays in `raw.quote_count` |
| `author_follower_count` | `user.followers_count` (syndication) / `core.legacy.followers_count` (GraphQL) | author's follower count at fetch time; used for follower-normalized ranking |
| `thumbnail_url` | `media[0].media_url_https` | poster frame for video |
| `raw` | the **complete** tweet dict | nothing dropped (capture everything) |

### Fields the platform can't provide (left `None`)

- **`view_count`** — not exposed by the *syndication* endpoint. **Recoverable via
  the opt-in GraphQL path** (see below); `None` on the default path. Without it
  you still have like/reply/repost, which suffice for a relative-to-baseline rule
  (OPEN-QUESTIONS Q-1).
- **`save_count`** — X bookmark counts are not public anywhere.
- **`sound_id` / `sound_name`** — X has no first-class sound/audio concept.
- **`geo_tier`** — not on the post; taken from the `WatchedAccount` (per contract).

### Ranking note (for `core`)

X lacks `save_count` and `view_count` is opt-in/fragile. The ranker should
**degrade gracefully** for X, leaning on **share rate (retweets / followers) +
engagement rate ((likes + replies + retweets) / followers)** in the absence of
views and saves. Do NOT compare X's save-rate (always `None`) against TikTok's.

### First-class signals preserved in `.raw` (no normalized column exists yet)

`is_quote_status`, `quoted_status` / `quoted_status_id_str` (quoted-author),
`retweeted_status` (is-repost), `in_reply_to_screen_name` (is-reply),
`extended_entities` (has-media + alt text), `quote_count`, `lang`. Per the
contract these stay in `raw` for retroactive use.

## Trends: no-go

`fetch_trends()` returns `[]`. X's "What's happening" trends are location-based
but every access path is login-walled or behind the ~$200/mo+ paid API. There is
no free, no-auth trend source, so per the ingestion contract `[]` is the correct
result.

## Optional: view_count via guest-token GraphQL

Enable with `XAdapter(prefer_graphql=True)`. Implemented in `graphql.py`. When on,
the GraphQL path is tried first (it returns `view_count`) and **falls back to
syndication on any failure**, so turning it on never makes things worse than the
default.

**How it works** (all $0, no login, no account):
1. `POST /1.1/guest/activate.json` with X's public web bearer → a **guest token**.
2. GraphQL `UserByScreenName` (guest token) → the account's `rest_id`.
3. GraphQL `UserTweets` (guest token) → full tweet nodes including `views.count`.

**Verified 2026-06-25 from a home IP:** guest activation 200; `UserByScreenName`
200; `UserTweets` 200; recovered real view counts for `@NASA` (e.g. 2,104,129 /
698,995 / 1,045,040 views). Run `.venv/bin/python -m adapters.x.verify` — its
second section exercises this path.

**Cursor pagination (new in round-three):** `get_user_tweets_page(rest_id, cursor=…)`
follows the UserTweets bottom-cursor page-by-page, enabling 100+ tweets per account
in one unattended session.

**Why it's OFF by default — the fragility/ban trade-off:**
- **Ban risk is *low*** — there is **no account to ban** (guest token only). The
  realistic failure is the guest token getting **rate-limited (429)** under volume,
  not a ban. `_gql_with_backoff` handles this with exponential backoff + token refresh.
- **Fragility is *high*, higher than syndication** — two things rotate on X's
  release schedule:
  - **GraphQL query-ids** (the hash in the URL). We keep a list and try each;
    when all break, add the current id to the front of the lists in `graphql.py`.
    The current ID can be found in X's main JS bundle — grep for
    `queryId:"…",operationName:"UserTweets"`.
  - **The `features` blob.** X 400s and names the missing flags; the client
    **auto-heals** by reading those names and retrying. Self-correcting within limits.

**When to turn it on:** only if the virality rule (OPEN-QUESTIONS Q-1) ends up
*needing* impressions. Until then, default syndication + `view_count=None` is the
lower-maintenance choice.

## Known fragility

- **Undocumented endpoint.** It exists to serve embeds, not us. X can change the
  `__NEXT_DATA__` shape or pull it with no notice; the parser would break. The
  adapter raises a clear `RuntimeError` pointing back here when the island is
  missing, so breakage is loud, not silent.
- **`showReplies=true` returns an empty timeline** (endpoint quirk) — we pin
  `showReplies=false`. Replies to others therefore won't appear; original posts,
  quotes, and retweets do.
- **Syndication path: ~20 tweets, no cursor.** Fine for daily Watchlist polling.
  For bulk harvest use the GraphQL cursor path.
- **GraphQL path: rotating query-ids + `features` blob** — more moving parts than
  syndication. Auto-heals the features blob; query-ids need manual update when they
  rotate (check the JS bundle). See `_USERTWEETS_QIDS` in `graphql.py`.
- **SearchTimeline: login-gated.** Returns HTTP 404 for all guest-token requests —
  not a stale-ID problem. Requires user-bearer auth. See escalation above.
- **Datacenter IPs untested** — see OPEN-QUESTIONS Q-3.

## Bottom line

Keep X in v1 **for Viral Posts via the Watchlist**. Default (syndication) gives
recent posts with like/reply/repost counts and media type — robust, zero-dep, no
auth. GraphQL cursor pagination reaches 500+ posts unattended across 7 accounts.
If the viral rule later needs impressions, flip on the opt-in guest-token GraphQL
path (`prefer_graphql=True`) to add `view_count`. Treat all of it as best-effort
(ADR-0001) and expect to fix query-ids when X shifts. Drop X **Trends** from v1
scope entirely. SearchTimeline (topic search) requires login — escalate to Opus if
topic-based discovery becomes a priority.
