# DIY scraping under a zero-budget mandate

**Context.** The tool must surface Trends and Viral Posts from competitors' Instagram, TikTok (and possibly X/Threads). Official APIs (Meta Graph, X) only expose accounts we own, so they cannot see competitors. Managed scraping providers (Apify, Ensemble Data, Bright Data) solve this reliably for ~$30–150/mo — but the project has a hard **$0 budget**.

**Decision.** We build and maintain our own scrapers rather than buy a managed provider or rely on official APIs. This consciously resolves the coverage/reliability/cost "iron triangle" in favor of **coverage + $0**, accepting **reduced reliability** (best-effort ingestion; the digest may have gaps when scrapers break).

**Why this is non-obvious.** A reasonable engineer would assume buying a provider is correct — it is, on pure cost-of-time. We reject it *only* because there is no budget line, not because DIY is technically superior.

**Consequences.**
- Two recurring operational costs survive no matter how good the code is: **residential/mobile IPs** (datacenter IPs get blocked on sight) and an **Instagram logged-in-account treadmill** (accounts get banned and must be re-warmed).
- TikTok at low volume (polling a known Watchlist) is the feasible core; **Instagram is the high-risk part**; **X and Threads are deprioritized/cut** for v1 as poor effort-to-value at $0.
  > **Amended 2026-06-26:** this "cut" is superseded. The round-two adapter work (signed TikTok
  > `item_list`, `instagrapi` for IG, unauthenticated Threads GraphQL, X syndication) made all four
  > viable at $0, all are built + verified live, and **all four are in v1.** The per-platform signal
  > unevenness is handled honestly by `docs/SIGNALS.md` — a thin platform (e.g. Threads) simply
  > exposes fewer ranking sorts; it is not excluded. IG remains the highest-risk (burner treadmill).
- If a budget ever appears, swapping a managed provider in behind the same ingestion interface should be the first thing reconsidered — design the scraper layer behind an interface so this swap is cheap.
