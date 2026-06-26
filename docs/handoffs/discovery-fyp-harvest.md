# Handoff — FYP / Explore burner-harvest (discovery spine)

Read `docs/DISCOVERY.md`, `docs/REVERSE-ENGINEERING.md`, `docs/INGESTION-CONTRACT.md` first.
This is the **fuller** discovery path: harvest the platform's OWN algorithmic feed (TikTok For
You, IG Explore) via browser automation, shaped to education + KZ, returning ranked PostRecords.
Implement as a discovery source feeding `fetch_viral_posts` (or a dedicated harvester module).

## Why this beats hashtag lenses
The FYP/Explore feed IS the platform's own ranking of "what's viral right now." Letting the
algorithm hand us its stream (persona+region shaped) is a fuller sample than guessing hashtags.
Still a *sample* (a scrolled stream), never a complete index — that stays paywalled.

## Approach
- **Engine:** Playwright **headed** Chromium + stealth (real fingerprint, human-like scroll).
- **TikTok — start LOGGED-OUT.** The web For You feed is partially viewable without login → **no
  account to ban**, only mild IP throttling. Load the page, **intercept the internal For-You
  `item_list` JSON** (don't parse DOM), normalize to PostRecord with full `raw`. Escalate to a
  warmed KZ burner ONLY if persona/region control proves necessary.
- **Instagram — needs login.** Warmed burner (instagrapi session or Playwright), hit the
  **Explore / reels** feed, intercept JSON. This is the higher-risk one.
- **Persona shaping (for KZ/edu):** region = KZ (KZ IP + app region/locale), interest graph warmed
  by viewing education content before harvesting. Document how much shaping actually changes the feed.

## Ranking
Provisional per `docs/DISCOVERY.md`. Dedupe across runs/personas. Not the final viral rule (Q-1).

## Anti-ban discipline (MANDATORY — see also the ban analysis in chat)
- **Burners only, never real/company accounts.** Assume disposable; build for rotation.
- **Warm** a burner (days of normal use) before harvesting; harvest in human hours with session breaks.
- **Headed + stealth**; randomized human-like delays/scroll; **low volume** (sample, don't crawl).
- **Residential IP, consistent per identity, geo-matched.** For the prototype, run from a **KZ home
  connection** (already an ideal free residential KZ IP). Scale → rotating residential/mobile IPs
  ($ — OPEN-QUESTIONS Q-3).
- Block responses (captcha / empty / login-redirect) = **back off + rotate**, never grind.

## Definition of done
`fetch_viral_posts`-compatible output: real ranked PostRecords harvested from the **TikTok FYP
logged-out** path, shaped toward KZ/education, with full `raw`. Document: did logged-out suffice or
was a burner needed; observed throttle/ban behavior; how much persona-shaping moved the feed.
Update the relevant adapter README.

## What the operator must provide
- A KZ home/residential connection to run from (prototype).
- One **throwaway** IG burner (for the Explore path) — never a real account.
