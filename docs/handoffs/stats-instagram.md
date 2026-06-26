# Handoff — Instagram: isolation harness + full stats capture

Read `docs/SIGNALS.md`, `docs/INGESTION-CONTRACT.md`, `docs/REVERSE-ENGINEERING.md` first.
**You own two jobs:** (1) build the SHARED persona-isolation harness in `core/` that other
browser/persona platforms reuse, and (2) make the IG adapter capture the full Tier-1 signal set.

## Job 1 — shared isolation harness (build in `core/`, not in the IG adapter)

Put it at `core/harness/persona_browser.py` so every platform can import it. It is the $0 version
of an anti-detect browser: **one disposable, persistent profile per persona, fully isolated from the
operator's real machine.** Required surface:

- `launch_persona(persona_id, *, proxy=None, locale="ru-RU", timezone="Asia/Almaty") -> BrowserContext`
  - **Persistent `userDataDir` per persona** under `profiles/<persona_id>/` (real cookies/storage so
    the account "recognizes its device" each run). NEVER the operator's real Chrome profile.
  - Real Chrome channel + `playwright-stealth`; kill `navigator.webdriver`; **stable** fingerprint
    per persona (canvas/WebGL/fonts/screen), KZ locale + `Asia/Almaty` timezone.
  - Optional per-persona `proxy` (residential/mobile IP). For the prototype, default = operator's KZ
    home IP (fine logged-out; see OPEN-QUESTIONS Q-3 for scale).
- `reset_persona(persona_id)` — nuke a tainted profile dir so a flagged fingerprint is regenerated
  for free (a "device ban" on web is just a flagged fingerprint — recoverable, no new hardware).
- Document the humanization expectations callers must follow (watch-time variance, human pacing,
  occasional engagement, session limits, back-off on captcha) — see the ban analysis in chat.

This harness is shared infra: **TikTok-persona and Threads can import it; X does not need it**
(pure HTTP). Other agents do NOT re-implement isolation.

## Job 2 — full IG stats capture

### Test burner account (provided)
A throwaway IG burner is provided for scraping. **Credentials are in the gitignored
`secrets/ig_burner.env`** (`IG_USERNAME` / `IG_PASSWORD` / `IG_SETTINGS_FILE`) — load them via the
adapter's existing session entry points (`load_session` / `login`), never hardcode. Do NOT print
the password or copy it into any committed file (README, code, logs).

- **Warm-before-hammer:** this account is freshly created. Do NOT run automated private-API pulls
  against it until it's been warmed (a few days of normal use from the KZ home IP). The harness
  (Job 1) needs no account and can be built/tested first.
- **Prefer a session file:** once warmed, log in once and `dump_settings("secrets/ig_burner.json")`,
  then load that session on every run instead of the raw password (fewer challenges).
- Burner only — never escalate to a real/company account.

### Signals
Your round-two `instagrapi` adapter already pulls most of Tier 1. Confirm/complete per
`docs/SIGNALS.md`:
- ✅ available: `view_count` (reels), `like_count`, `comment_count`, `sound_id/name` (reels),
  `hashtags`, `posted_at`, **`author_follower_count`** (add it — from `user_info`).
- ❌ **NOT public on IG: `share_count`, `save_count`** → leave `None`. Do not fake them. Note this
  in the README; it means save-rate/share-rate ranking is unavailable for IG (expected).
- Keep the COMPLETE payload in `raw`.

Do NOT compute ratios/velocity/baseline (those are `core`'s Tier 2).

## Definition of done
`core/harness/persona_browser.py` works (launch + reset, isolated profile, KZ locale), with a tiny
demo. IG adapter returns PostRecords with every IG-available Tier-1 signal + `author_follower_count`
+ full `raw`. README documents the unavailable signals and how to run a persona in isolation.
