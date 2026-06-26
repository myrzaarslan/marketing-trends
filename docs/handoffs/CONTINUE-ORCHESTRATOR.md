# Continue Here — Orchestrator / Grilling Handoff

You are taking over the **architect + grilling + curator** role for `marketing-trends` (an internal
social-trends tool for an EdTech marketing team). Your job is NOT to write all the code yourself —
it's to **grill the user one decision at a time (with a recommendation), record decisions in the
docs, and dispatch SONNET subagents** for build work, keeping everything coherent.

## How to work (the method that's been used)
- **grill-with-docs discipline.** Walk the design tree one decision at a time, give YOUR
  recommendation each time, stress-test against existing decisions. Keep `CONTEXT.md` (glossary
  ONLY — not a spec), `docs/adr/` (sparingly — hard-to-reverse + surprising + real-tradeoff), and
  `docs/OPEN-QUESTIONS.md` current AS decisions land, not batched.
- **Subagents: always `model:"sonnet"`, never Opus** (user rule, in memory). Point each at the
  relevant `docs/handoffs/*.md`. Instruct them to **escalate hard design calls back to you (Opus)**
  rather than guess.
- **Never inline the IG burner credentials** — they live in gitignored `secrets/ig_burner.env`.
- Update the project memory (`project_marketing_trends_tool.md`) at milestones.

## Orientation — read these first (they ARE the source of truth; don't duplicate them)
`CONTEXT.md` (glossary) · `docs/CORE-SPINE.md` · `docs/SIGNALS.md` · `docs/DISCOVERY.md` ·
`docs/REVERSE-ENGINEERING.md` · `docs/OPEN-QUESTIONS.md` · `docs/adr/0001..0003` ·
the per-platform `adapters/*/README.md` · the project memory file.

## State as of this handoff (2026-06-26)
- **Adapters (TikTok / Instagram / X / Threads):** Tier-1 stats capture (incl `author_follower_count`)
  DONE + verified live from the KZ home IP for TikTok/X/Threads; IG works logged-in (Explore harvest
  ~110/run). Per-platform availability is real and uneven — see SIGNALS.md (saves = TikTok only, etc).
- **Discovery (Layer 2):** seeded lenses + FYP/Explore burner-harvest. TikTok Explore is the default
  surface (~55/session); IG Explore via logged-in burner. `fetch_viral_posts` is the optional method.
- **Harness:** `core/harness/persona_browser.py` (per-persona disposable profiles, stealth, KZ
  locale, captcha back-off) — ready, its real customer is TikTok/IG persona harvest.
- **Robustness round (IN FLIGHT at handoff):** 4 Sonnet agents were accumulating toward 500
  posts/platform with auto-modal-dismiss + captcha/429 back-off. **Their agent IDs do NOT survive
  into a new session** — check `adapters/*/README.md` + any scratch json outputs for what they
  produced; don't assume, verify.
- **Core spine:** fully DESIGNED (CORE-SPINE.md, ADR-0003) but **NOT built** — no storage, ranker,
  API, or UI yet. This is the critical path to "four scrapers → a tool."
- **IG burner:** warming (creds in `secrets/ig_burner.env`).

## Remaining work (what the user wants next)

### 1. Layer-3 enrichment — content text + media files
Get the actual CONTENT of posts: caption (have it) + downloaded media (video/images) + understanding
(OCR on-screen text, ASR/Whisper transcript, and/or multimodal description for "label trends by bot").
**Key facts already established (don't re-derive):** public posts need NO user token — the media CDN
URLs are in the captured `raw`; download the bytes (URLs are signed + EXPIRING → fetch promptly). The
agreed architecture is a **two-stage funnel**: cheap stats on MANY → rank → expensive enrichment on
the TOP-N only. **Grill the user on:** which understanding layer (OCR? ASR? which multimodal model —
note compute cost vs the $0 stance), where media/derived text is stored, and confirm top-N-only.

### 2. ⚠️ Captcha-solving agentic system — REVERSES PRIOR DISCIPLINE, GRILL BEFORE BUILDING
The user wants an agentic system (agent tools) to **identify AND SOLVE** captchas. **This contradicts
everything decided so far:** `robust-harvest.md` and ADR-0001 say *never solve, back off* — solving is
an arms race that invites hard bans, captcha-solver services cost money (vs $0), and it raises ToS/legal
exposure. **Do NOT just build it.** Grill the user: is the goal worth the ban/cost/legal risk vs. the
cheaper alternatives (spaced-run cadence, rotating residential IPs — Q-3)? Slider-puzzle solving via a
vision agent is technically possible but fragile. **If the user commits after grilling, record a new
ADR** (it's hard-to-reverse + surprising + a real trade-off) and only then dispatch the build.

### 3. Backend + frontend — build the core spine
Per `docs/CORE-SPINE.md` + ADR-0003. Order (each depends on prior): (1) `core` SQLite storage (3
tables: posts / post_snapshots / accounts) + `run_ingestion()` (upsert + append full-raw snapshot +
tag source); (2) `core` multi-strategy ranker (engagement-rate default + platform/history-gated
sorts — Q-1 is user-selectable); (3) `api/` FastAPI (`GET /digest?…`, `POST /refresh`); (4) `web/`
React SPA. It's adapter-independent — buildable NOW against the 3 working adapters. Build 1→3 first
(a live `/digest` over real data), React after. Dispatch as Sonnet subagent(s).

## Live open questions (docs/OPEN-QUESTIONS.md)
Q-1 resolved (user-selectable ranking). Q-2 (seed hashtag lists + KZ location PKs — still needs the
user's input). Q-3 (residential IPs at scale — the recurring $ unlock). Q-5 (storage growth from
full-raw snapshots). PLUS the new captcha-solving decision (item 2 above).

## Suggested skills
- **grill-with-docs** — your primary operating mode for items 1 & 2 (and any new fork).
- **diagnose** — when a scraper/harvester breaks.
- **prototype** / **tdd** — for the spine build.
- **code-review** / **verify** — gate the backend before wiring React.
- **handoff** — to pass the baton again when context fills.
