# Handoff — Instagram discovery (Layer 2)

Read `docs/DISCOVERY.md`, `docs/INGESTION-CONTRACT.md`, `docs/REVERSE-ENGINEERING.md` first.
Implement `fetch_viral_posts` in `adapters/instagram/`. This is the **heavy / ban-prone** one —
get a working prototype and document the operational cost honestly.

## Goal
Given seed hashtags + KZ locations, return ranked recent **PostRecords** (top posts) from accounts
we don't follow.

## Approach
1. **Hashtag discovery → `hashtag_medias_top(tag)`** — Instagram's own algorithmic "best of" for a
   tag (engagement-ranked). Accept a `hashtags=` arg; keep an education + KZ/RU default seed list.
2. **Geo discovery → `location_medias_top(location_pk)`** — top posts at a place. Resolve KZ city
   location PKs (Almaty, Astana) once and accept `locations=`. This is the $0 KZ angle.
3. Normalize each result with the same `extract_media_v1` path your round-two adapter already uses,
   so PostRecords + `raw` match Layer 1 exactly.

## Ranking
Provisional per `docs/DISCOVERY.md` (`view_count` for reels else engagement sum; last
`period_days`). Mark provisional. Not the final viral rule (Q-1).

## Constraints / gotchas (read these)
- **`hashtag_medias_top` is IG's most-throttled endpoint.** Expect aggressive rate-limiting; a
  warmed **burner** session is mandatory and a **residential proxy** is effectively required for any
  real volume (OPEN-QUESTIONS Q-3). Never a real/company account.
- Pace slowly (reuse the adapter's `delay_range`), treat challenge/throttle as `SoftBlockError` →
  stop, don't retry harder. Keep seed counts tiny for the prototype.
- Dedupe across hashtags/locations.

## Definition of done
`fetch_viral_posts("World", hashtags=[...])` and `fetch_viral_posts("KZ", locations=[...])` return
real ranked PostRecords from a warmed burner on a home IP, with full `raw`. Update
`adapters/instagram/README.md` with throttle/ban behavior observed and an honest read on whether
discovery (not just watchlist) is sustainable at $0 — this is heavier than Layer 1 and may be the
thing that forces the residential-proxy spend.
