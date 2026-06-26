# Open Questions (To Be Discussed)

Decisions deliberately deferred. Revisit once we have real data to look at.

## Q-1 — What makes a Post "viral"? (the detection rule)
**Status:** RESOLVED as a product decision — **don't hardcode one rule; expose ALL ranking
strategies as user-selectable sorts in the UI.** The marketing user picks the lens (raw views,
likes, save-rate, share-rate, engagement-rate, velocity, relative-to-baseline, cross-persona
breadth). The spine stores all raw signals so any strategy is computable.
**Caveats the UI must handle:**
- History-dependent sorts (**velocity, relative-to-baseline, cross-persona breadth**) are unavailable
  until snapshots accumulate over time — gray them out / "needs N days of data".
- Platform-limited sorts: **save-rate = TikTok only**; **share-rate = not Instagram**; **views**
  unreliable off TikTok — disable those sorts for platforms that can't produce them (per SIGNALS.md).
- A sensible default sort still needs picking (recommend engagement-rate — the only universal one).

## Q-2 — How is a *Trend* detected/ranked? (vs a Viral Post)
**Status:** partially resolved → see `docs/DISCOVERY.md`.
**Finding:** there is **no free standalone "most viral videos globally" feed** off YouTube
(YouTube excluded by decision, twice). So discovery is reframed as **seeded discovery** — rank
viral posts collected from seeds (trending + education + KZ/RU-language hashtags, IG city
locations). This is also the only $0 path that reaches KZ/CIS. New optional adapter method
`fetch_viral_posts` added to the contract. **Still open:** the curated seed-hashtag list, KZ
location PKs, and whether to also surface abstract Trend *topics* (Google Trends) alongside ranked
*videos*. Provisional ranking only until Q-1 (the real viral rule) is decided.

## Q-3 — Where/how does ingestion run to avoid datacenter-IP blocks?
**Status:** open for SCALE; **solved for the prototype.** The operator is in Kazakhstan, so their
**home connection is an ideal free residential KZ IP** — perfect for prototyping FYP-harvest and the
KZ-persona discovery. The unresolved part is *scale*: sustained/parallel harvesting needs **rotating
residential or mobile IPs**, which cost money — this is where "free browser automation" hits a real
cost, and where a small IP-only budget (not a managed provider) becomes the cheapest unlock. Decide
before productionizing beyond one home-IP prototype. Tied to FYP-harvest (`docs/handoffs/discovery-fyp-harvest.md`).
**Update (2026-06-26):** the captcha-solving experiment (ADR-0004) introduces a $0 *disposable*-egress
story — a VPN (datacenter captcha-farm) for puzzle collection, then a phone 4G/5G tether
(residential-grade, airplane-mode rotation) for measurement — kept strictly OFF the home ingestion IP.
This partially exercises the cheap end of Q-3; revisit the paid residential/mobile-proxy decision with
the measured data.

## Q-6 — Automated content *understanding* (deferred 2026-06-26)
Layer-3 v1 is **extraction only** — it downloads media + text + sound + author into a Content Bundle,
but does NOT interpret what the media *depicts*. The "label trends by bot" understanding layer —
OCR (on-screen text), ASR/Whisper (transcript), and/or a multimodal description pass — is **deferred**.
When revisited, the open sub-decision is **model hosting under the $0 mandate** (ADR-0001): a local
open-weight model ($0, heavy compute) vs. a small paid multimodal API (cheap *because* it runs on the
top-N only, but breaks the strict $0 stance). Also deferred with it: capturing the author's *own*
comments (dropped from v1 as extra per-post ban surface).

## Q-5 — Storage growth from full-history raw snapshots
**Status:** deferred by decision (2026-06-26). We store the COMPLETE `raw` payload on EVERY
snapshot (not latest-only) to never lose data. This grows fast — a tracked post re-observed daily
stores its full payload each time. Acceptable on SQLite for the prototype. **Revisit when it hurts:**
options are skip-identical-consecutive-snapshots, store raw only on change / first-seen, JSON
compression, a retention window, or migrate to Postgres. Don't pre-optimize; just know it's the
first thing to fix at scale.
