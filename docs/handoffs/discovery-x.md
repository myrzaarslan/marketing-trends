# Handoff — X discovery (Layer 2, OPTIONAL spike)

Read `docs/DISCOVERY.md` and `docs/REVERSE-ENGINEERING.md` first. **Time-boxed spike** — X video
virality is weak, so this is low priority. Only pursue if the TikTok/IG discovery is in hand.

## Goal
Given a hashtag/keyword, return ranked recent **PostRecords** under that query (mostly `text`,
some `video`).

## Approach
- Extend the round-two **guest-token GraphQL** path (`adapters/x/graphql.py`) with
  **`SearchTimeline`** (latest/top product). Guest token only — no login, no account to ban. Same
  rotating-query-id + `features`-blob fragility as `UserTweets`; reuse the auto-heal logic.
- Seeds: education / KZ-relevant keywords + hashtags.

## Ranking
Provisional (`view_count` via the GraphQL path if available, else like+reply+repost; last
`period_days`). Mark provisional.

## Constraints
- Guest token rate-limits (429) under volume — poll politely. No burner treadmill (nothing to ban).
- Higher parser fragility than syndication; keep it behind the same opt-in spirit.

## Definition of done
A working `fetch_viral_posts` over `SearchTimeline` for at least one query, OR a clear written
**no-go** explaining why X discovery isn't worth it. Update `adapters/x/README.md`.
