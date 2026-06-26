# Handoff — TikTok discovery (Layer 2)

Read `docs/DISCOVERY.md`, `docs/INGESTION-CONTRACT.md`, `docs/REVERSE-ENGINEERING.md` first.
You're the **primary discovery engine**. Implement `fetch_viral_posts` in `adapters/tiktok/`.

## Goal
Given a geo tier + seed hashtags, return ranked recent **PostRecords** (videos with stats) from
accounts we don't follow. Build the reference implementation; IG mirrors the structure.

## Approach (in priority order)
1. **Seed hashtags → `TikTok-Api` `hashtag(name).videos()`.** This hits the signed
   `challenge/detail` + item-list surface you already cleared in round two (TikTok's own JS signer
   in a headed Playwright browser). Collect each video's full item object (stats incl. views,
   `music`, `media_type`) into a `PostRecord` with complete `raw`.
2. **Seed sources:**
   - global: the top hashtags from your existing `fetch_trends` (Creative Center top-3),
   - vertical/KZ: a curated seed list of education + Kazakh/Russian hashtags (accept `hashtags=`
     arg; keep a sensible default list in the adapter).
3. **Probe (research task): public `/discover` and `/trending/detail/<slug>` pages.** They embed
   `__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON, no auth. Document whether they yield a usable
   ranked-video list as a lighter alternative/supplement to the signed path.

## Ranking
Provisional per `docs/DISCOVERY.md`: `view_count` if present else sum of engagement counts; filter
to last `period_days`. Mark provisional. Do NOT implement the final viral rule (Q-1).

## Constraints / gotchas
- **Headed browser required** (headless trips detection); `EmptyResponseException` = bot-block →
  back off, don't grind; sustained volume needs a residential IP (OPEN-QUESTIONS Q-3).
- Low volume: a handful of seed hashtags × N videos, once/day. Don't hammer.
- Dedupe videos that appear under multiple seed hashtags.

## Definition of done
`fetch_viral_posts("World", hashtags=[...])` returns real ranked PostRecords from a home IP, with
full `raw`; `fetch_viral_posts("KZ", ...)` attempts the local-language-hashtag path and documents
coverage. Update `adapters/tiktok/README.md` with what worked, the `/discover` probe result, and
fragility notes.
