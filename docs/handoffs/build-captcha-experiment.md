# Build handoff — Captcha-solving experiment (ADR-0004)

You are a SONNET build agent. Build the **contained, default-OFF** captcha-solving experiment scaffold
per ADR-0004. This REVERSES the project's back-off-only discipline, so the guardrails are not optional
— they are the deliverable. **Escalate any non-trivial design fork to the Opus orchestrator.**

## Read first (source of truth — obey it exactly)
`docs/adr/0004-captcha-solving-experiment.md` (your spec) · `docs/handoffs/robust-harvest.md`
(obstacle taxonomy; note the ADR-0004 exception block) · `core/harness/persona_browser.py` ·
`adapters/tiktok/discovery.py` (where slider-captcha detection + back-off currently lives).

## Own ONLY these paths
`core/harness/captcha_solver.py`, `docs/captcha/PLAYBOOK.md`, `data/captcha_registry.json` (generated;
gitignored under `data/`). **Do not touch** `core/storage`, `enrichment/`, `api/`, `web/`, or the
adapters' fetch logic beyond reading their captcha-detection markers.

## What to build
1. **`captcha_solver.py` — default OFF.** A gate (`enabled=False` unless explicitly turned on) so
   existing harvesters keep backing off. When enabled, only on a **disposable egress** (VPN/mobile),
   **never the home IP**. Slider puzzle ONLY (the one observed); other types → record + back off.
2. **Agent tools:**
   - `apply_captcha_moves(page, moves)` — execute a move-set (drags with a humanized velocity curve,
     clicks, holds) against the live slider via Playwright.
   - `record_puzzle(...)` — persist puzzle type + DOM/screenshot + attempted move-sets + outcome to
     the registry (the dataset a future cheap model is distilled from).
3. **Puzzle registry + playbook:** `data/captcha_registry.json` (types, occurrence counts, move-sets,
   outcomes — coverage + stuck-detection) and a human-readable `docs/captcha/PLAYBOOK.md` (bypass
   notes per type).
4. **Circuit breaker:** ≤ **10 attempts per obstacle** (only on the disposable egress); a re-challenge
   or 429 after a solve = **terminal** → stop, mark `blocked=True`, signal "rotate the disposable IP".
   Never grind. On the home IP, the existing back-off path is unchanged (no solving).
5. **Stuck detection:** when attempts exhaust or an unseen puzzle type appears, flag it in the registry
   for human/Opus review.

## Phase framing (ADR-0004)
Phase 1 builds the tools + registry + detection + guardrails so a strong agent (operator-run) can
collect and understand puzzles. Phase 2 (later) distills to a cheap model. You build the scaffold and
the tools — NOT an autonomous solve loop that runs unattended.

## How to test — and how NOT to (read carefully)
**DO NOT test by hitting a live platform until it serves a captcha.** That is the exact ban-risky
behavior the ADR contains, and it's non-deterministic. Live solve-rate / re-challenge / ban-rate is an
**operator-run measurement on the disposable egress — OUT OF SCOPE for this build.** Note also that
TikTok's **behavioral acceptance** (drag velocity/jerk profile) has **no offline proxy** — only the
operator's live run can validate it.

**IN SCOPE — offline, deterministic tests against saved fixtures** (capture a few real slider
challenges = gap image + DOM, replay from a local HTML page / saved snapshots):
- Gap detection: given a fixture slider image with a known gap, the CV finds the offset within tolerance.
- Move execution: given a target offset, `apply_captcha_moves` produces a humanized velocity curve and
  drives Playwright correctly — assert the **trajectory**, not platform acceptance.
- Classification: correctly distinguishes a slider captcha from a benign modal.
- Circuit breaker: stops at 10 attempts and goes terminal on a **simulated** re-challenge.
- Default-OFF gate: back-off behavior is **provably unchanged** when disabled.
- Registry/playbook: puzzles recorded with the correct shape.
Commit 2-3 sanitized slider fixtures under a test fixtures dir (no secrets).

## Definition of done
Default-OFF solver module + the two agent tools + registry/playbook + a working circuit breaker, with
a clear opt-in flag and egress guard. **Offline fixture/replay tests pass** for the mechanics above.
Existing back-off behavior provably unchanged when OFF. The kill switch + disposable-egress requirement
+ "live measurement is operator-run, not automated" are documented in `docs/captcha/PLAYBOOK.md`.
