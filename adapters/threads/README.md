# Threads adapter

**Status:** working (round three — unattended accumulation). **Recommendation: GO.**
**Last verified:** 2026-06-26. Findings are point-in-time; Threads changes often.

**Accumulation result (2026-06-26):** 308 canonical PostRecords from 27 active accounts
in ~17 min (508 raw nodes harvested; 200 sub-item stubs dropped — see §Sub-item stubs
below). No login, no paid provider, no captcha.

Round one was a feasibility spike; round two hardened it per
`docs/REVERSE-ENGINEERING.md`. The adapter pulls real, normalized Post Records from
public Threads profiles **unauthenticated** at $0, and is wired for an authenticated
fallback if Threads adopts Instagram's login wall.

---

## Recommendation (feeds the v1 scope decision)

**Technically: GO.** Unauthenticated public-profile ingestion works at $0, reaches
500 posts unattended in ~17 min across ~27 seed accounts.

**For v1 scope: CONDITIONAL — defer unless marketing wants Threads.** The blocker was
never "can we scrape it"; it's effort-to-value. Recommend cutting from v1 by default
(consistent with ADR-0001) *unless* there's demand for Threads discourse coverage —
the cost to include it is now low, so it's a product call, not an engineering blocker.

`fetch_trends` is a `[]` stub by design: **there is no free Threads trend source**
(no public trending surface, no Creative-Center equivalent; the official Graph API is
owned-account only). See OPEN-QUESTIONS Q-2.

---

## Accumulation (≥500 posts unattended)

**Script:** `adapters/threads/harvest_accumulate.py`  
**Run:** `.venv/bin/python -m adapters.threads.harvest_accumulate`  
**State:** `data/threads_harvest_scratch.json` (idempotent — interrupt and resume safely)

### How we reach 500

**Threads is HTTP/per-request (not a scroll-feed):** "500 posts" = accumulate across many
seed accounts + depth via Relay scroll-pagination within each.

**Confirmed findings (2026-06-26 live run):**

| Metric | Value |
|--------|-------|
| Total canonical PostRecords | 308 |
| Raw nodes harvested (before stub filter) | 508 |
| Sub-item stubs dropped | 200 (see §Sub-item stubs below) |
| Seed accounts tried | 56 handles |
| Active public accounts | 27 |
| Private/gated accounts (skipped) | 4 (@bbcnews, @theverge, @vogue, @glamour) |
| Average posts per account | ~11–18 |
| Max posts per account | 50 (cosmopolitan, nfl — scroll pagination) |
| One-run time | ~17 min |

### Pagination cursor (confirmed)

Every profile-feed GraphQL response carries a **Relay `page_info`** object with
`has_next_page: true` and `end_cursor`. The adapter tracks these via
`adapter.last_has_next_page` and `adapter.last_end_cursor`.

Scroll-triggered follow-up requests use the cursor automatically (the browser fires a new
signed GraphQL request with `after: <cursor>` when you scroll to the bottom). This is why
accounts like `@cosmopolitan` and `@nfl` yielded 50 posts (not just 20) with
`scroll_stall_limit=6` and `limit=50`.

We cannot replay the signed request outside the browser (see §Why the browser stays in the
loop below), so pagination is scroll-driven within the same browser session.

### Logged-out explore/search — probe results (2026-06-26)

| Surface | Verdict | Posts found |
|---------|---------|-------------|
| `threads.com/explore` | **LOGIN_WALL** | 0 |
| `threads.com/search?q=marketing` | EMPTY_NO_WALL | 0 |
| `threads.com/search?q=education` | EMPTY_NO_WALL | 0 |
| `threads.com/search?q=trending` | EMPTY_NO_WALL | 0 |

**Conclusion:** There is **no logged-out feed/search surface.** `/explore` is behind login.
The search pages load without a wall but fire no GraphQL feed requests (the search UX
likely requires auth to load results). Accumulation must be breadth-first across known accounts.

### Blockers and handling

| Blocker | Detection | Handling |
|---------|-----------|----------|
| **Account-level login wall** (private / not-on-Threads account) | `ThreadsLoginWall` exception | Skip account, continue. Only stop if 3 consecutive walls (IP-level signal). |
| **IP-level login wall** (Threads gates all reads) | 3 consecutive `ThreadsLoginWall` exceptions | Stop harvest; persist state; re-run after 30+ min. |
| **HTTP 429 rate limit** | `response.status == 429` → `ThreadsRateLimited` exception | 90 s backoff; skip account; continue. Stop if 3 consecutive 429s. |
| **Rotating `doc_id`** | Intercepted from live request via `fb_api_req_friendly_name` | Already handled since round two — no hardcoded id. |
| **Navigation timeout / network error** | Python exception | Log to `accounts_failed`; mark done; continue. |

**Observed in live run:** One 429 on @espn (recovered with 90 s backoff). Four accounts
hit login walls (all account-specific — private or not on Threads). No IP-level block.

---

## How it works

Engine: **Playwright (headless Chromium)** — open-source, $0.

The profile HTML is a shell (`"ssrEnabled":false`); posts arrive via a GraphQL POST
to `/api/graphql` that needs a *current* `doc_id` (a rotating persisted-query hash
fetched at runtime via Meta's Bootloader — there is no static URL to scrape it from).
So we let a real browser make the call and **intercept the GraphQL response**, then
harvest post nodes from the JSON **by shape, not by path**.

- **doc_id is acquired, never hardcoded.** Interception is scoped to the profile-feed
  query by `fb_api_req_friendly_name` (loose substring match — survives minor
  renames). The live `doc_id` and observed Threads `userID` are exposed for
  observability as `adapter.last_doc_id` / `adapter.last_user_id`.
- Reposts/quotes by the watched account **are** included (they're part of the profile
  feed) and attributed to the **original author** in `account_handle`. Recommendation
  / related-profile payloads are filtered out by the friendly-name scope.
- Each post's **complete** payload is preserved in `PostRecord.raw`
  (INGESTION-CONTRACT.md: "capture everything, decide viral later").

### Why the browser stays in the loop (the requests-replay we did *not* ship)
We investigated a cheaper path: capture the `doc_id` via the browser once, then
replay the GraphQL via plain `requests` for the rest of the Watchlist. **Deliberately
not adopted** — three concrete walls, all confirmed live:

1. **Integrity-bound request.** Replaying the browser's full instrumented body
   (`__csr`/`__hs`/`jazoest`/`__spin_*`) via `requests` returns Meta error `1357054`
   ("Your Request Couldn't be Processed"). That token bundle is session-bound to the
   browser document.
2. **Browser-minted cursor.** The *only* profile-feed request is a Relay
   *refetchable* query (`BarcelonaProfileThreadsTabRefetchableDirectQuery`) that
   already carries an `after` pagination cursor minted from page state. A clean
   cookieless replay executes (`{"data":{"mediaData":null}}`) but returns no posts
   without reproducing that cursor.
3. **Threads userID is its own namespace.** The IG `web_profile_info` id is **not**
   the Threads `userID` (e.g. zuck: IG `314216` vs Threads `63055343223`), and the
   Threads id is not in the profile HTML — it's resolved at runtime. So even the id
   for the replay can't be obtained cheaply.

Reproducing all three outside the browser is exactly the signing/gating wall ADR-0001
says not to hand-roll at $0. For a low-volume Watchlist, the browser path is the right
call; the `doc_id`/`userID` capture is kept purely for observability. If a managed
provider budget ever appears (ADR-0001), that's the swap to reconsider — not a
hand-rolled signer.

### Field mapping (Threads → normalized PostRecord)
| Normalized             | Threads source                                                          | Status |
|------------------------|-------------------------------------------------------------------------|--------|
| `platform_post_id`     | `pk` (fallback `code`)                                                  | ✅ |
| `url`                  | `…/@{user}/post/{code}`                                                 | ✅ |
| `media_type`           | `text` default; `video`/`image`/`carousel` from media flags             | ✅ |
| `caption`              | `caption.text`                                                          | ✅ |
| `hashtags`             | `text_post_app_info.text_fragments` (struct) → regex fallback (Cyrillic-aware) | ✅ |
| `like_count`           | `like_count`                                                            | ✅ |
| `comment_count`        | `text_post_app_info.direct_reply_count` (replies)                       | ✅ |
| `share_count`          | `text_post_app_info.repost_count` (reposts)                             | ✅ |
| `view_count`           | `play_count` **on video posts only** (opportunistic); `None` otherwise  | ⚠️ video-only |
| `save_count`           | not exposed — always `None`                                             | ❌ None |
| `posted_at`            | `taken_at` (epoch → UTC)                                                | ✅ |
| `author_follower_count`| `user.follower_count` (from embedded user object)                       | ✅ |
| `sound_id/name`        | `clips_metadata.music_info` (rare on Threads; usually `None`)           | ⚠️ rare |
| `duration_sec`         | `video_duration` (video posts only)                                     | ⚠️ video-only |
| `thumbnail_url`        | `image_versions2.candidates[0].url`                                     | ✅ |

**Ranking note:** Threads has no views and no saves → `core`'s ranker must degrade
gracefully to **engagement rate = (likes + comments + reposts)** for this platform.
Save rate and view-normalized rates are not available. See docs/SIGNALS.md.

### Fields Threads can't provide (left `None`)
- **`view_count`** — not exposed for text/image/carousel posts. Captured from
  `play_count` on video posts only, else `None`.
- **`save_count`** — no public bookmark/save count. Always `None`.
- `sound_id` / `sound_name` / `duration_sec` — `None` for the common text post.

---

## Login-wall fallback (`session.py`) — wired, unverified

Threads reuses Instagram's auth and could adopt IG's login wall at any time. The
adapter detects a wall (`ThreadsLoginWall`) and, if given a burner `ThreadsSession`,
runs the same browser path **authenticated** (cookies injected into the context):

```python
from adapters.threads import ThreadsAdapter, ThreadsSession

session = ThreadsSession.from_cookies(sessionid="…", ds_user_id="…")     # or
session = ThreadsSession.from_storage_state("burner_state.json")          # Playwright
session = ThreadsSession.from_instagrapi("burner_settings.json")          # lazy bridge
adapter = ThreadsAdapter(session=session)
```

Because the backend is shared, an Instagram `sessionid` authenticates Threads too —
so this consumes the **same warmed burner session the Instagram round-two adapter
produces** (`from_instagrapi` lazily imports `instagrapi`, which is the IG adapter's
dependency, not ours). **Status: implemented and offline-tested, but not verified
end-to-end** — there is no login wall today and no burner credentials in this spike.
**Burner accounts only, never a real/company account** (ADR-0001 / playbook).

---

## Auth / IP / guardrails
- **Auth:** none required today for public reads. Optional burner session for the wall.
- **IP:** runs from a residential home IP. Datacenter IPs will be blocked on sight
  (ADR-0001); the production residential-IP strategy is **OPEN-QUESTIONS Q-3**, out of
  scope for the prototype.
- **Polite by design:** low-volume (a curated Watchlist), gentle scroll-paced loads.
  Treat 401/403/429/challenge as "back off," never "retry harder."
- **Cost:** $0. Playwright + Chromium are open-source; no paid providers.

## Rate limits

`ThreadsRateLimited` is raised when any response returns HTTP 429. The accumulation script
backs off 90 s then continues with the next account. Three consecutive 429s → stop and
persist state (re-run after cooling off). **Never retry aggressively** — that deepens the ban.

---

## Sub-item stubs — the ~40% None-engagement investigation (2026-06-26)

**Root cause:** The original `_looks_like_post` filter accepted any node with `pk`
(OR `code`) + 1 other marker field. This admitted **carousel sub-media nodes** and
**thread-chain continuation stubs** — embedded child nodes from the Threads GraphQL
payload. These stubs look enough like post nodes to pass the old heuristic but carry no:
- `code` (shortcode) → URL fell back to `/t/{pk}` instead of `/@user/post/{code}`
- `caption` → None for all 200
- `taken_at` → posted_at was None for all 200
- `like_count` / `direct_reply_count` / `repost_count` → ALL engagement None

The pattern was most pronounced on accounts posting multi-part threads or carousel
content (@cosmopolitan 80%, @nfl 80%, @mosseri 74%, @spotify 73%, @openai 70%).

**Fix:** `_looks_like_post` now requires `"code"` to be present in the node dict.
Root-level Threads posts always carry a shortcode; sub-items and stubs do not.

**Recovery:** The 200 stubs were dropped from `data/threads_harvest_scratch.json`.
Re-harvesting with the fixed filter will collect only root-level posts going forward.
The 308 remaining records in the file are confirmed root-level posts with full
engagement data (like_count: 100%, share_count: 100%; comment_count: 72.7% — some
accounts' post nodes omit `text_post_app_info.direct_reply_count`).

**What they were NOT:** These were not reposts of foreign content (reposts of another
account's post carry the reposter's own `code` in the profile-feed response). They
were exclusively child nodes embedded inside the response for posts from the watched
account itself.

---

## Known fragility (likely break-order)
1. **Query rename** — if Threads renames the profile-feed query away from the
   `_PROFILE_FEED_HINTS` substrings, the scope filter needs a one-line update (loose
   match buys slack, not immunity).
2. **IP-level login wall** — Threads could force login for all public reads (IG already
   does). Detected as 3 consecutive `ThreadsLoginWall` exceptions. Fallback: supply a
   burner `ThreadsSession` (see §Login-wall fallback below). Account-specific walls
   (private profiles) are expected and simply skipped.
3. **Bot detection** — heavier Playwright traffic from one IP may trip challenges.
   Observed: one 429 from @espn during a 17-account run. Pacing (3.5–7 s jitter) kept
   this isolated.
4. **DOM/selector drift** — minimal exposure: we read GraphQL JSON, not the DOM.

## Run it
```bash
# Smoke test (3 accounts, quick sanity check):
.venv/bin/python -m adapters.threads.smoke_test

# Unattended accumulation (≥500 posts, resume-safe):
.venv/bin/python -m adapters.threads.harvest_accumulate
# State is saved to data/threads_harvest_scratch.json after each account.
# Re-run to resume from where it left off.
```
Both hit the live site, so they're inherently flaky. The accumulation script handles
account-level login walls (skip) and 429s (90 s backoff) automatically.

## Files
- `adapter.py` — `ThreadsAdapter` (Playwright engine + normalizer), `ThreadsLoginWall`,
  `ThreadsRateLimited`. `_looks_like_post` now requires `code` to filter sub-item stubs.
- `session.py` — `ThreadsSession` (cookies / storage_state / lazy instagrapi bridge).
- `smoke_test.py` — live end-to-end check across real accounts.
- `harvest_accumulate.py` — **unattended breadth accumulator**: 56 seed handles, dedup,
  persist to `data/threads_harvest_scratch.json`, login-wall skip (account-level) +
  stop (IP-level), 429 backoff, explore/search surface probe, resume-safe.
  `_post_to_row` now serializes all PostRecord fields with canonical names (including `raw`).

## Dependencies
- `playwright` (+ `playwright install chromium`). Open-source, $0.
- `instagrapi` — **optional**, lazy-imported only for `ThreadsSession.from_instagrapi`;
  owned by the Instagram adapter, not required here.
