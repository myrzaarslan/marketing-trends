# Reverse-Engineering Playbook (Round Two)

Shared technique reference for rebuilding the platform adapters on the **private APIs**
that managed providers (Apify, Ensemble, etc.) use — at $0, by standing on the
open-source libraries that already cracked the hard parts.

> Round-two sessions: read this **plus** your original handoff
> (`/tmp/marketing-trends-handoffs/<platform>.md`) **plus** `docs/INGESTION-CONTRACT.md`.
> Nothing here changes the contract: you still implement `core.adapter.PlatformAdapter`,
> still return `PostRecord`/`Trend` dataclasses, still dump the COMPLETE payload into `.raw`,
> still never persist or judge virality.

---

## The core idea

You are **not** reverse-engineering Apify. You are reverse-engineering the **platforms** —
and the OSS community already did ~90% of it and *maintains* it. Apify's whole business is
paying people to re-crack request signing every time a platform ships a change. The durable
$0 strategy:

1. **Wrap the mature OSS lib** that already decoded the platform's private API + signing.
   This amortizes the maintenance treadmill (ADR-0001) across a community instead of one person.
2. **Capture live traffic** (mitmproxy/HTTP Toolkit) only to fill gaps the lib doesn't cover.
3. **Never hand-roll a signer you can't maintain.** Prefer running the platform's *own* JS
   signer over reimplementing its algorithm.

The valuable target on every platform is the **private mobile/internal API endpoint + its
request signing/gating** — that's the difference between flaky web-scraping and provider-grade data.

---

## Per-platform map

### Instagram — the priority upgrade
- **Current adapter uses `instaloader` (web scraping).** That's why anonymous now 403s and you
  fight the burner treadmill. **Switch to [`instagrapi`](https://github.com/subzeroid/instagrapi).**
- **Why:** `instagrapi` speaks the **mobile private API** (`i.instagram.com/api/v1/...`) — the
  same surface Apify uses. It emulates an Android device, manages the session/`Authorization`
  bearer, `X-IG-App-ID`, `X-IG-WWW-Claim`, and CSRF for you. Richer data, fewer challenges.
- **Still needs a logged-in account** (a burner — never a real/company account). But device
  emulation + proper mobile headers make a warmed session far more durable than web scraping.
- **Round-two DoB:** `fetch_account_posts` returning real Post Records via `instagrapi` from a
  burner session; document session-warming + ban behavior vs. the old instaloader path.

### TikTok — add the signed endpoint
- **Current adapter uses `yt-dlp` flat-playlist** (works, but single-video extraction is broken
  and first-class fields like duet-source/is-ad/sounds are missing).
- **Add [`TikTok-Api`](https://github.com/davidteather/TikTok-Api)** as the primary path, yt-dlp as
  fallback. The real endpoint is `https://www.tiktok.com/api/post/item_list/`, gated by
  **`X-Bogus`/`X-Gnarly` + `msToken` + `secUid` + `device_id`** signatures.
- **Signing the durable way:** `TikTok-Api` runs **TikTok's own signing JS in a Playwright
  context** to mint valid signatures — survives algorithm changes because you're using *their*
  code, not a reimplementation. This is exactly the "run their JS" strategy below.
- **Carry forward** the secUid fallback finding from round one (store secUid on
  `WatchedAccount.platform_account_id`).
- **Round-two DoD:** signed `item_list` returning full per-video objects incl. the first-class
  fields flat-playlist couldn't reach.

### Threads — harden what works
- Round one already cracked it: unauthenticated `/api/graphql` with the page `LSD` token +
  `X-IG-App-ID`, intercepting the GraphQL JSON via Playwright to dodge the rotating `doc_id`.
- **Round-two focus:** make `doc_id` acquisition resilient (intercept-by-friendly-name, no
  hardcoding), and confirm `username → user_id` via `i.instagram.com/.../web_profile_info`.
  Watch for Threads adopting IG's login wall — if it does, fall back to the `instagrapi`-style
  session path.

### X — mostly done, optional deepening
- The free **syndication endpoint** works no-auth and is the recommended path; keep it.
- *Optional* deepening for `view_count` (the one gap): the GraphQL `UserTweets` path needs a
  **guest token** (`POST /1.1/guest/activate.json` with the public bearer) + `x-guest-token`
  header. Higher fragility/ban risk — only if views prove necessary for the viral rule (Q-1).

---

## The toolkit (for gaps the libs don't cover)

- **HTTP Toolkit** (easiest) or **mitmproxy** / Proxyman / Charles — intercept and read the
  real requests, headers, and signing params.
- **Android emulator (or rooted device) + `frida` / `objection`** — bypass certificate pinning
  so you can read the *app's* HTTPS. **Mobile-app endpoints are richer and less defended than
  web** — this is where providers get their best data. (`apk-mitm` patches an APK for interception.)
- **Read the OSS source** — `TikTok-Api`'s signer, `instagrapi`'s endpoint defs, `yt-dlp/tiktok.py`,
  `instaloader`. The source *is* the reverse-engineering, already version-tracked.

Workflow: capture in HTTP Toolkit → find the endpoint + required headers/signature → check whether
an OSS lib already implements it (it usually does) → wrap the lib → only hand-roll the residual.

---

## The signing crux

The one genuinely hard wall (TikTok `X-Bogus`/`X-Gnarly`, IG claims). Two strategies, both used
by providers:

1. **Run the platform's own JS signer** in a Node/headless context (what `TikTok-Api` does).
   *Durable* — survives algorithm changes because it's their code. **Prefer this.**
2. **Reimplement the algorithm** in Python. *Faster* per-request, but breaks on every change.
   Last resort.

---

## Rules & guardrails (unchanged from ADR-0001)

- **$0 only:** OSS libs + free endpoints. Paid provider tiers are out (that's the whole point).
- **Public data, internal use.** This is interoperability/competitive-research scraping for an
  internal marketing tool — read-only, low-volume (a curated Watchlist), polite pacing. **No
  high-rate hammering, no DoS, no auth-bypass against private data.** Respect rate limits;
  treat 401/403/429/challenge as "back off," never "retry harder."
- **Burner accounts only**, never real/company accounts, for any logged-in path (IG especially).
- **Residential/datacenter IP** is still OPEN-QUESTIONS Q-3 — prototypes run from a home IP;
  productionizing the IP story is separate.
- **Breakage is expected and acceptable** (ADR-0001). Pin lib versions; fail loud with a clear
  error pointing back here; let the daily digest tolerate gaps.

---

## What round two does NOT change
- The adapter interface, the canonical schema, "capture everything / decide viral later"
  (OPEN-QUESTIONS Q-1), and the no-free-KZ/CIS-trends reality (Q-2) all still hold.
- Adapters still fetch + normalize only. Signing/session/IP concerns live *inside* the adapter;
  they never leak into `core`.
