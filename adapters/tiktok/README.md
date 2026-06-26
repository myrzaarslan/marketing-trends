# TikTok adapter

Implements `core.adapter.PlatformAdapter` for TikTok. Two independent free,
no-auth paths (per [ADR-0001](../../docs/adr/0001-diy-scraping-under-zero-budget.md)
— no paid providers). Verified working from a home IP on **2026-06-25**.

```python
from adapters.tiktok import TikTokAdapter
from core.schema import WatchedAccount

a = TikTokAdapter()
posts  = a.fetch_account_posts(WatchedAccount("khanacademy", "tiktok", "global_edtech", "World"), limit=30)
trends = a.fetch_trends("World")   # top trending hashtags; KZ/CIS -> []
```

Quick smoke test (hits the live site):

```bash
pip install -r adapters/tiktok/requirements.txt
playwright install chromium          # for the signed primary path
python -m adapters.tiktok.adapter [handle]   # default handle: khanacademy
# prints which path served each batch: "TikTok-Api (signed)" or "yt-dlp (fallback)"
```

## `fetch_account_posts` — two-tier (round two)

A signed private-API path with an unsigned web-scrape fallback. See
[docs/REVERSE-ENGINEERING.md](../../docs/REVERSE-ENGINEERING.md) for the strategy.

### PRIMARY — [`TikTok-Api`](https://github.com/davidteather/TikTok-Api) (signed `item_list`)

- Hits TikTok's private `https://www.tiktok.com/api/post/item_list/` — the
  provider-grade surface. The wall is the `X-Bogus`/`X-Gnarly` + `msToken`
  request **signing**; TikTok-Api clears it the durable way — by running TikTok's
  *own* signer JS inside a Playwright browser, so it survives algorithm changes
  (vs. a Python reimplementation that breaks on every tweak).
- **Why we added it:** the full item object carries the first-class fields the
  yt-dlp path couldn't reach. These now populate normalized columns and live in
  `raw`:

  ### Full Tier-1 signal coverage (signed `item_list` path)

  | normalized column | TikTok source field | yt-dlp fallback? |
  |---|---|---|
  | `view_count` | `statsV2.playCount` / `stats.playCount` | ✅ `view_count` |
  | `like_count` | `statsV2.diggCount` / `stats.diggCount` | ✅ `like_count` |
  | `comment_count` | `statsV2.commentCount` / `stats.commentCount` | ✅ `comment_count` |
  | `share_count` | `statsV2.shareCount` / `stats.shareCount` | ✅ `repost_count` |
  | `save_count` | `statsV2.collectCount` / `stats.collectCount` | ⚠️ sometimes (`save_count`) |
  | `sound_id` | `music.id` (stable; `"0"` → `None`) | ❌ no stable id |
  | `sound_name` | `music.title` | ✅ `track` |
  | `hashtags` | `textExtra[*].hashtagName` + `desc` regex | ✅ caption regex |
  | `duration_sec` | `video.duration` → `music.duration` | ✅ `duration` |
  | `media_type` | `imagePost` → `image`/`carousel`, else `video` | ❌ always `"video"` |
  | `caption` | `desc` | ✅ `description` / `title` |
  | `posted_at` | `createTime` (Unix epoch) | ✅ `timestamp` |
  | `thumbnail_url` | `video.cover` / `originCover` / `dynamicCover` | ✅ `thumbnails[-1]` |
  | `account_handle` | `author.uniqueId` | ✅ `uploader` |
  | `author_follower_count` | `authorStats.followerCount` | ⚠️ `channel_follower_count` if present |
  | `geo_tier` | caller-supplied (no per-post geo in TikTok) | same |
  | `raw` | complete item_list object, untouched | complete yt-dlp entry |

  `save_count` / `author_follower_count` from `authorStats.followerCount` are the two
  signals unique or most reliable on TikTok — the signed path captures both consistently.

  Additional fields captured in `raw` for future virality rules: `isAd`,
  `music.original` (original-vs-licensed), `music.isCopyrighted`,
  `duetDisplay`/`stitchDisplay`/`originalItem`, `locationCreated`/`poi`.
- **Requires a HEADED browser.** From a home IP, headless Chromium trips TikTok's
  bot detection and `item_list` comes back empty. The adapter defaults to
  `headless=False` and needs a real or virtual display (`DISPLAY` set, or run
  under `xvfb-run`). On a headless box install `xvfb`; productionizing this +
  the IP story is [OPEN-QUESTIONS Q-3](../../docs/OPEN-QUESTIONS.md).
- **Cost:** ~10–20s of browser startup **per account** (one session per call).
  Fine for a once/day low-volume Watchlist; cross-account session batching is a
  productionization concern, deliberately kept out of the adapter.
- **secUid carry-forward:** if `WatchedAccount.platform_account_id` holds the
  `MS4w…` secUid, the adapter calls `api.user(sec_uid=…)` and skips fragile
  handle resolution.

### FALLBACK — [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) flat-playlist

Used automatically when TikTok-Api raises (session/signing failure) **or** returns
empty (bot detection). Lighter (no browser) but thinner data and **no `sound_id`**.
Flat-playlist mode against `https://www.tiktok.com/@<handle>` passes through
TikTok's user-feed item objects; `curl_cffi` (in requirements) gives it browser-TLS
impersonation. Construct `TikTokAdapter(use_tiktokapi=False)` to force this path.

### Known fragility (expect breakage — ADR-0001 accepts it)

- **Both libs are version-pinned** in `requirements.txt` — TikTok-Api's signer and
  yt-dlp's extractor both change often. After a bump, re-run the smoke test and
  confirm `sound_id`/`save_count` still populate from the signed path.
- **TikTok-Api session creation is flaky/slow** (a 30s mint timeout fires
  intermittently). That's not fatal — it just trips the yt-dlp fallback for that
  account, which the daily digest tolerates by design.
- **yt-dlp's secUid resolution fails for some accounts** deterministically
  (e.g. `@nasa`, `@ted`; `@khanacademy`, `@natgeo` work) — *"Unable to extract
  secondary user ID."* Same `platform_account_id` secUid fix applies (yt-dlp then
  uses its `tiktokuser:<secUid>` input). yt-dlp's *single-video* extractor is also
  broken from a home IP; we never use it.
- `last_tiktokapi_error` on the adapter records why a batch fell back to yt-dlp.

### Normalized fields still left `None` (signed path)

| Field | Why |
|---|---|
| `sound_id` | `None` only on the **yt-dlp fallback** (flat data has the name, not a stable id) or when the signed `music.id` is the `"0"` placeholder. The signed path fills it. |
| `geo_tier` | Carried from the Watched Account's manual tag (TikTok exposes no per-post geo). |
| `author_follower_count` | Always populated on the signed path (`authorStats.followerCount`). On the yt-dlp fallback, populated only if yt-dlp includes `channel_follower_count` in its flat entry (not guaranteed). |

The raw payload shape **differs between the two paths** (signed `item_list` object
vs. yt-dlp entry) — consumers should read normalized columns, not `raw` keys.

## `fetch_trends` — TikTok Creative Center

- **Source:** the Creative Center "Creative Suite" SPA. Its trend pages are
  server-rendered via Next.js loader endpoints that return clean JSON with **no
  request signing** — just a browser User-Agent:
  ```
  https://ads.tiktok.com/creative/creativeCenter/trends/hashtag
      ?countryCode=US&period=7
      &__loader=creativeCenter%2Ftrends%2F(tab)%2Fpage&__ssrDirect=true
  ```
  We read it with stdlib `urllib` (the trends path has **zero** pip deps).
- The older `creative_radar_api/v1/popular_trend/...` XHR still exists but now
  returns `40101 no permission` without a reverse-engineered `user-sign`
  signature. We deliberately avoid it — a signature we can't verify is exactly
  the brittle dependency ADR-0001 warns against. The SSR loader is the durable
  path.

### Coverage reality (verified 2026-06-25 — this was the handoff's first task)

The logged-out free tier is **much narrower than the handoff assumed**:

- ❌ **KZ is not a supported market**, and **neither is any CIS country.**
  Creative Center advertises exactly 27 markets (US, GB, DE, FR, JP, KR, TR, AE,
  …) and none are CIS. On top of that, **the logged-out loader ignores
  `countryCode` entirely** and always serves one global/US default list — country
  filtering is itself login-gated. → `fetch_trends("KZ")` and
  `fetch_trends("CIS")` return **`[]`** (the contracted answer; there is no free
  per-country source to fill them honestly).
- ⚠️ **Only the top 3 hashtags** are free. The rest of the ranking is behind a
  *"Log in or sign up"* wall. The $0 mandate rules out the login/account
  treadmill (ADR-0001), so we take the free top-3.
- ⚠️ **Only `trend_type="hashtag"`.** The Songs/`music`, Creator and Video tabs
  return no logged-out data (Creator & Video literally show "Coming soon"). So
  **no `sound` trends** are available right now, despite the handoff expecting
  them. Re-check after a TikTok change.
- ✅ `fetch_trends("World")` returns the global top-3 trending hashtags, ranked by
  Creative Center's own `rankIndex`, with `score` ← view volume (`vv`) and
  `volume` ← post count (`publishCnt`). Full item (incl. `popularityCurve`,
  `topCreators`, `industryIDs`) is in `Trend.raw`.

If a budget ever appears, the cleanest upgrade is a logged-in Creative Center
session (full per-country rankings + sounds) or a managed provider behind this
same `fetch_trends` interface — see ADR-0001.

## `fetch_viral_posts` — logged-out For-You harvest (discovery, Layer 2)

The discovery spine from [docs/DISCOVERY.md](../../docs/DISCOVERY.md): instead of
named accounts, harvest TikTok's **own** algorithmic feed and rank it. Implemented
in [`discovery.py`](discovery.py); the adapter normalizes + ranks.

> **Surfaces:** the harvester supports two — **`explore`** (the `/explore` grid,
> the **default**: ~32–55 items per session window, richer, carries `challenges[]`)
> and **`foryou`** (the full-screen player, ArrowDown, ~15–25/session). Choose via
> `TikTokAdapter(discovery_surface="foryou")`. The complete per-video field catalog
> both surfaces return is in [`signal.md`](signal.md).
>
> **Bot-check discipline:** if pushed, TikTok serves a slider captcha. The harvester
> **detects it and backs off** (`blocked=True`) — it never solves it and never
> grinds. (Triggers found & fixed 2026-06-26: a focus-click was opening grid tiles
> as videos — now FYP-only; and pacing is now slower/jittered.) Treat ~one Explore
> session window as the polite logged-out yield per time period; accumulate across
> well-spaced runs over multiple days. Rotate residential IPs at scale (Q-3).

- **How:** headed Chromium + `playwright-stealth` loads `tiktok.com/explore`
  **logged out**, wheel-scrolls the grid, and intercepts the
  `https://www.tiktok.com/api/explore/item_list/` XHR (never the DOM). Items share
  the signed `item_list` shape, so they reuse `_record_from_api`. **No login → no
  account to ban**, only IP throttling.
- **Modal handling — 4 types auto-dismissed (obstacle #1):**
  1. **Interest picker** ("Что вы хотели бы посмотреть?") — ✕ svg in
     `[class*="InterestSelector"]`, clicked live, no hardcoded coordinates.
  2. **Login/signup nudge** ("Войти в TikTok") — tries "Continue as guest" button
     text first, then ✕ svg in login panel containers, then Escape key.
  3. **Cookie/GDPR banner** — tries "Decline"/"Reject all" button by role+text,
     then "Accept all" fallback, then ✕ svg in cookie panel containers.
  4. **App-install banner** ("Get TikTok app") — ✕ svg in app-banner container.
  All selectors are class-fragment / role-based (never pixel coordinates).
  Feed exhaustion wall ("Log in to see more") is NOT dismissed — it triggers a
  cycle reload. Captcha → stop immediately, `blocked=True`.
- **Stale-advance improvement:** at half the stale limit, the harvester proactively
  tries all 4 modal dismissers even if no modal text was detected (catches modals
  that appear without body-text cues).
- **Output:** `PostRecord`s ordered by a **provisional** score
  (`view_count`, else like+comment+share+save — docs/DISCOVERY.md), with the
  score/rank/`provisional: True` stamped into `raw['_discovery']`. Dedup is by
  video id across feed reloads. **Not** the final viral rule (Q-1).
- `last_discovery_note` reports new items this run, total accumulated, modals
  dismissed (by type), and any block/exhaustion signal.

## Unattended accumulation toward 500 — runner + persistence

`discovery.py` ships a `__main__` runner for unattended accumulation:

```bash
# First run (starts from 0, or continues existing accumulator):
python -m adapters.tiktok.discovery

# Cooldown mode — always use when IP recently served a captcha:
python -m adapters.tiktok.discovery --cooldown

# Check progress without opening a browser:
python -m adapters.tiktok.discovery --status

# Custom accumulator path (default: data/tiktok_accumulator.json):
python -m adapters.tiktok.discovery --out /path/to/accumulator.json
```

The accumulator file (`data/tiktok_accumulator.json`) persists items across runs
(dedup by video id). Re-running always **continues** — it never resets. If a
captcha hits mid-session, the run stops immediately and whatever was collected is
saved; the next operator run resumes from that count.

### Parameters for unattended operation

| param | default | notes |
|---|---|---|
| `--cycles` | 2 | Page reloads per run. Set 1 for gentlest; 2 lets the feed serve a second batch. |
| `--advances` | 30 | Scroll steps per cycle. Explore plateaus after ~10–15 meaningful scrolls; higher = safer cap. |
| `--cooldown` | off | Doubles delays (3–5.5s vs 1.8–3.2s). Use whenever IP was recently hot. |
| `--geo` | KZ | `KZ` for ru-RU/Asia/Almaty (natural CIS shaping), `World` for en-US. |

### Verified 2026-06-26 from a KZ home IP (Astana / Kcell residential)

Four cooldown sessions run back-to-back. Results:

| Session | Geo | Cycles | Advances | New items | Total | Blocked? |
|---|---|---|---|---|---|---|
| 1 | KZ | 1 | 15 | **16** | 16 | No |
| 2 | KZ | 2 | 20 | **16** | 32 | No |
| 3 | KZ | 1 | 40 | **0** | 32 | No |
| 4 | World | 1 | 25 | **0** | 32 | No |

Key findings:
- ✅ **Cooldown mode kept the IP clean** — no captcha in 4 sessions (the IP had
  served one on 2026-06-26; cooldown pacing let it recover).
- ✅ **Cross-run dedup works**: sessions 3 and 4 correctly returned 0 new, confirming
  the accumulator is preventing re-collection.
- ✅ **Full rich API payload** captured (views, likes, saves, sound_id, follower
  count, etc.) — identical to signal.md's inventory.
- ⚠️ **Pool size ~32 items per session window.** The logged-out Explore feed for a
  given IP has a relatively small content pool at any point in time (~32–55 items;
  55 was seen 2026-06-25 in a single deep session). After 2 sessions exhaust it,
  subsequent runs yield 0 until the pool refreshes (several hours / next day).
- ⚠️ **Different geo tier (KZ vs World) doesn't expand the pool** within the same
  IP/hour — session 4 (World) found 0 new after sessions 1–3 (KZ).
- 🟡 **No modals appeared** in any session this run (no interest-picker, no login
  wall, no cookie banner). The new modal dismissers haven't been stress-tested yet;
  they'll activate if/when those modals appear.

### Is 500 reachable logged-out unattended? How?

**Yes — with well-spaced runs across multiple days.** The math:

- Pool per session window: **~32–55 unique items** (varies by time of day)
- Pool refresh: several hours (possibly once per day from TikTok's side)
- To reach 500: **~10–16 session windows** = ~5–10 days of once/twice-daily runs
- Recommended cadence: **one run per 2–6 hours** (no more than 2–3 per day)
- At 2 sessions/day × 32 items = 8 days; at 2 sessions/day × 55 items = 5 days

**Implementation for fully unattended operation:**

```bash
# Add to crontab (twice daily — 09:00 and 18:00):
0 9,18 * * * cd /path/to/marketing-trends && DISPLAY=:0 \
    .venv/bin/python -m adapters.tiktok.discovery --cooldown \
    >> logs/tiktok_harvest.log 2>&1

# Check progress any time:
.venv/bin/python -m adapters.tiktok.discovery --status
```

**If 500 is needed faster:** a warmed KZ burner session (logged-in) can yield
several hundred items per session via the full paginated FYP. See below.

- ✅ **Logged-out suffices for a KZ-shaped sample.** The feed loads in Russian
  ("Смотрите трендовые видео") and returns KZ/CIS creators mixed with global virals.
  Region shaping comes free from the KZ IP + `ru-RU`/`Asia/Almaty` locale.
- ⚠️ **FYP skews EVERGREEN-viral (60–90 days old).** The algorithm surfaces
  proven hits, not just last-week's. A tight `period_days` filters most out
  (`period_days=7` → ~0). **Use `period_days≈90+` for this source**, or treat
  feed-presence (not post date) as the recency signal. The note reports drops.
- 🟡 **Education shaping IS available logged-out (not wired here).** The interest
  modal offers "Наука и образование" (Science & Education) — selecting it +
  Continue shapes the session toward education with no login. We currently just
  **close** the modal (simpler/robust); flipping to select-education is a one-call
  change in `discovery.py` if a future digest wants edu-biased discovery.
  (Logged-out hashtag `/tag/…` and search item_lists are still EMPTY/gated, so
  `hashtags`/`locations` args remain unservable here — accepted but ignored,
  flagged in `last_discovery_note`.)
- ❗ **Headed browser required.** Headless Chromium gets an empty feed (bot
  detection); needs a real/virtual display (`DISPLAY` or `xvfb`), same as the
  signed account-posts path. Default `tiktokapi_headless=False`.

### When a KZ burner becomes necessary (escalation, not done here)

Switch to a **warmed, throwaway** KZ burner (never a real account) when you need:
volume well beyond a few reloads (paginate the feed with a session), **stronger
education-persona shaping** (warm the interest graph over days, beyond the one-shot
interest modal), or the seed paths (hashtag/search/location). Anti-ban discipline
(burners only, headed + stealth, human pacing, geo-matched residential IP, back off
on blocks) is in
[docs/handoffs/discovery-fyp-harvest.md](../../docs/handoffs/discovery-fyp-harvest.md);
rotating residential IPs at scale are [OPEN-QUESTIONS Q-3](../../docs/OPEN-QUESTIONS.md).
