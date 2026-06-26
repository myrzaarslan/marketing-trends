# TikTok signal inventory — what the Explore feed gives us

Complete catalog of every data field TikTok returns per video, captured from the
**Explore tab** (`https://www.tiktok.com/explore`) logged out. This is the
"capture everything" reference for the deferred viral rule (OPEN-QUESTIONS Q-1):
it documents *every* signal available so any future scoring rule can be computed
retroactively over stored `raw`, without re-scraping.

**Generated from 55 real items, harvested 2026-06-25 from a KZ home IP (Astana /
Kcell residential).** 287 distinct field paths. Counts (`present`) show how many
of the 55 items carried each field — a field at `19/55` is conditional, not rare.

## Source & method

- **Endpoint:** `https://www.tiktok.com/api/explore/item_list/` (the grid behind
  the Explore page), intercepted via headed Chromium + stealth — same harness as
  `fetch_viral_posts` (see [discovery.py](discovery.py)). Items share the exact
  shape of the signed `item_list` used by `fetch_account_posts`, so the same
  `_record_from_api` normalizer applies.
- **Explore beats For-You as a discovery source:** the Explore grid scrolls with
  the mouse wheel and returns **~32–55 unique items per session window** —
  vs FYP's ~15–25 one-at-a-time. Same region shaping (loads as "Интересное" in RU
  on a KZ IP), and it also carries `challenges[]` (associated hashtag objects)
  that the FYP items don't.
- **Logged out.** No account, no ban risk — only IP throttling.
- **Bot-check ceiling (seen 2026-06-26).** Pushed too hard, TikTok serves a slider
  puzzle captcha. The harvester **detects it and backs off** (returns what it has,
  `blocked=True` in the note) — it never tries to solve it and never grinds.
  A captcha means the IP is hot → stop, slow down, and at scale rotate residential
  IPs (OPEN-QUESTIONS Q-3). Treat ~one Explore session window as the polite
  logged-out yield; accumulate across well-spaced runs, don't hammer.

## Modal obstacle taxonomy (auto-dismissed by harvester)

Four types of benign modals are auto-dismissed via stable DOM selectors (never
pixel coordinates). The captcha is the only one that stops the run.

| # | Modal | Detection | Dismiss method | Status |
|---|---|---|---|---|
| 1 | Interest picker ("Что вы хотели бы посмотреть?") | Body text: "хотели бы" / "would you like" | ✕ svg in `[class*="InterestSelector"]`, click parent chain | ✅ Live, verified |
| 2 | Login/signup nudge ("Войти в TikTok") | Body text: "войти в tiktok" / "log in to tiktok" | "Continue as guest" text btn → ✕ svg in login panel selectors → Escape | ✅ Implemented; not yet triggered in sessions |
| 3 | Cookie/GDPR banner | Body text: "we use cookies" / "использует файлы cookie" | "Decline" btn by role+text → "Accept all" → ✕ svg | ✅ Implemented; not yet triggered in sessions |
| 4 | App-install banner ("Get TikTok app") | Body text: "open in app" / "get the app" | ✕ svg in app-banner container selectors | ✅ Implemented; not yet triggered in sessions |
| — | Captcha / slider puzzle | "передвиньте ползунок" / "drag the" / "puzzle" | **STOP** — `blocked=True`, return what's collected | ✅ Live |
| — | Feed exhaustion wall ("Log in to see more") | Body text: "log in to see more" | Cycle reload (move to next cycle) | ✅ Implemented |

Stale-advance improvement: at half the stale limit (`_STALE_LIMIT // 2`), all 4
modal dismissers are called proactively even if no modal text was detected — catches
modals that appear without body-text cues before the stale counter would reset.

## Accumulation toward 500 — verified run log (2026-06-26)

Four cooldown sessions from a KZ home IP (IP had served captcha on 2026-06-26;
cooldown mode used throughout). The IP stayed clean — no captcha hit.

| Session | Geo | Advances | New items | Total | Note |
|---|---|---|---|---|---|
| 1 | KZ | 15 | 16 | 16 | First run, partial scroll |
| 2 | KZ | 20×2 | 16 | 32 | 2 cycles, cycle 2 dry |
| 3 | KZ | 40 | 0 | 32 | Pool exhausted for this window |
| 4 | World | 25 | 0 | 32 | Different locale, same pool |

**Pool size:** ~32 items in this session window. 55 was observed 2026-06-25 in a
single fresh session (different day, no prior sessions). The pool refreshes over
time (estimate: several hours to once per day).

**WHY the cap exists — XHR/server evidence (diagnosed 2026-06-26):**
A focused diagnostic session (one fresh browser, 25 gentle scroll advances) captured
5 XHRs and confirmed the stop mechanism:

| # | URL pattern | status | items | cursor (response) | hasMore |
|---|---|---|---|---|---|
| 1 | `api/explore/item_list/` | 200 | 8 | `"0"` | `true` |
| 2 | `api/explore/item_list/` | 200 | 8 | `"0"` | `true` |
| 3 | `api/explore/item_list/` | 200 | 8 | `"0"` | `true` |
| 4 | `api/explore/item_list/` | 200 | 8 | `"0"` | `true` |
| 5 | `api/prefetch/explore/item_list/` | 200 | **0** | — | — |

Key findings from the XHR log:
- **`cursor: "0"` in every response** despite 32 unique items across 4 XHRs — TikTok's
  logged-out Explore does NOT use client-passed cursor pagination. The server tracks
  state internally (via WebId cookie) and simply issues the next batch each time the
  page fires a request.
- **`hasMore: true` is NOT reliable** as an "items remain" signal for logged-out sessions;
  all 4 real XHRs say `true` even on the last batch. The true stop signal is XHR #5:
  a `prefetch/explore/item_list/` returning `itemList: []`. Once this fires with 0 items,
  the grid halts and scrollHeight stops growing.
- **No captcha, no login wall, no modal** at stall: DOM dump confirmed all modal containers
  are `display:none`, body text contains no login/captcha hints. The "Войти" (Log in)
  visible in the header is the ordinary navigation button, not a blocking overlay.
- **Scroll IS working** up to the stall: scrollHeight grew on the first 3 of 10 advances
  as the grid rendered, then plateaued exactly when the pool was exhausted — confirming
  the stop is a server-side content decision, not a scroll failure.

**Conclusion: GENUINE POOL EXHAUSTION — category (e).** ~32 items is TikTok's server-side
logged-out content quota for this IP/WebId/time-window. Not a silent block, not a missed
modal, not a scroll bug. The logged-out Explore Explore pool varies (32 now vs 55 on a
fresh day) and refreshes over time.

**Path to 500:** ~10–16 well-spaced session windows (run once or twice per day).
At 2 sessions/day × 32 items = ~8 days unattended to reach 500.
See `python -m adapters.tiktok.discovery --help` for the runner.
Accumulator file: `data/tiktok_accumulator.json` (persists across runs, dedup by id).

## Mapping to the normalized `PostRecord`

These columns are filled by `_record_from_api`; everything else lives in `raw`.

| PostRecord column | Signal field |
|---|---|
| `platform_post_id` | `id` |
| `account_handle` | `author.uniqueId` |
| `url` | built from `author.uniqueId` + `id` |
| `posted_at` | `createTime` (Unix epoch) |
| `caption` | `desc` |
| `hashtags` | `textExtra[].hashtagName` (+ `desc` regex) |
| `sound_id` / `sound_name` | `music.id` (`"0"`→None) / `music.title` |
| `duration_sec` | `video.duration` |
| `view_count` | `stats.playCount` |
| `like_count` | `stats.diggCount` |
| `comment_count` | `stats.commentCount` |
| `share_count` | `stats.shareCount` |
| `save_count` | `stats.collectCount` |
| `thumbnail_url` | `video.cover` |
| `media_type` | `imagePost`→image/carousel else `video` |

## High-value signals NOT yet promoted to columns (for the Q-1 viral rule)

The schema has no column for these, so they stay in `raw` — but they're the most
useful "extra" signals a baseline-aware viral rule will likely want:

- **`authorStats.followerCount`** — the denominator for *relative* virality
  (views-per-follower beats raw views; a 100k-view clip from a 1k-follower account
  is the real signal). Also `heartCount`, `videoCount`.
- **`statsV2.repostCount`** — in-app reposts (distinct from `shareCount`); a strong
  intent signal absent from `stats`.
- **`challenges[]`** — the hashtag/challenge objects a post rides (id, title, desc),
  i.e. which *trend* a viral post belongs to — links discovery back to `fetch_trends`.
- **`video.claInfo.originalLanguageInfo.languageCode`** + `video.subtitleInfos[]` —
  the **spoken language** of the video (auto-detected, e.g. `ru`), the most reliable
  KZ/CIS-vs-World geo signal TikTok exposes per post (caption `textLanguage` is
  often `un`).
- **`stickersOnItem[].stickerText`** — **on-screen text overlays** (the hook text),
  and **`desc`** — together the text to mine for education keywords.
- **`music.original`** / **`music.tt2dsp`** — original-vs-licensed sound, plus the
  Apple/Spotify song mapping (`tt_to_dsp_song_infos`) for licensed tracks.
- **`anchors[]`** — CapCut-template / product / link anchors (format & commerce signal).
- **`isAd`**, **`officalItem`**, **`poi`** (location), **`createTime`** age, **`CategoryType`**.

## Caveats

- `stats.*` are ints; `statsV2.*` are strings (same numbers + `repostCount`). Parse
  with the adapter's `_int`.
- Many URL fields (`playUrl`, `playAddr`, `downloadAddr`, caption `url`) are **signed
  and time-limited** — fine to store, but they expire; re-fetch to play.
- `poi` (location) appeared on only 1/55 — opportunistic, not reliable.
- Playback internals (`video.bitrateInfo[]`, `PlayAddrStruct`, codecs, hashes) are
  included below for completeness but are low-value for virality.

---

# Complete field inventory (287 paths)

> `present` = items carrying the field out of 55. Nested list items shown as
> `parent[].child`. Examples truncated to ~46 chars.
<!-- GENERATED from explore item_list; regenerate via the harvest + inventory walk. -->

### Identity & timing

| field path | type | present | meaning | example |
|---|---|---|---|---|
| `AIGCDescription` | str | 55/55 | AI-generated-content label text (empty if none) |  |
| `CategoryType` | int | 55/55 | TikTok content category code | 120 |
| `collected` | bool | 55/55 |  | False |
| `createTime` | int | 55/55 | Posted-at (Unix epoch) | 1781449951 |
| `desc` | str | 55/55 | Caption text | #рек #ош🇰🇬 #москва #казахстан🇰🇿 #атакатитанов  |
| `digged` | bool | 55/55 |  | False |
| `forFriend` | bool | 55/55 |  | False |
| `id` | str | 55/55 | Video id (platform_post_id / url) | 7651269245652798741 |
| `isReviewing` | bool | 55/55 |  | False |
| `privateItem` | bool | 55/55 |  | False |
| `secret` | bool | 55/55 |  | False |
| `textLanguage` | str | 55/55 | Detected caption language ('un'=unknown) | un |
| `diversificationId` | int | 43/55 | Feed diversification bucket | 10003 |

### Engagement (the core viral signals)

| field path | type | present | meaning | example |
|---|---|---|---|---|
| `stats` | dict | 55/55 |  |  |
| `stats.collectCount` | int | 55/55 | SAVES/favorites | 8178 |
| `stats.commentCount` | int | 55/55 | COMMENTS | 1818 |
| `stats.diggCount` | int | 55/55 | LIKES | 111800 |
| `stats.playCount` | int | 55/55 | VIEWS | 2800000 |
| `stats.shareCount` | int | 55/55 | SHARES | 107100 |
| `statsV2` | dict | 55/55 |  |  |
| `statsV2.collectCount` | str | 55/55 | Saves (string) | 8178 |
| `statsV2.commentCount` | str | 55/55 | Comments (string) | 1818 |
| `statsV2.diggCount` | str | 55/55 | Likes (string) | 111800 |
| `statsV2.playCount` | str | 55/55 | Views (string) | 2800000 |
| `statsV2.repostCount` | str | 55/55 | Reposts (string) | 0 |
| `statsV2.shareCount` | str | 55/55 | Shares (string) | 107100 |

### Author & author stats

| field path | type | present | meaning | example |
|---|---|---|---|---|
| `author` | dict | 55/55 |  |  |
| `author.avatarLarger` | str | 55/55 |  | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `author.avatarMedium` | str | 55/55 |  | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `author.avatarThumb` | str | 55/55 |  | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `author.commentSetting` | int | 55/55 |  | 0 |
| `author.downloadSetting` | int | 55/55 |  | 0 |
| `author.duetSetting` | int | 55/55 |  | 0 |
| `author.ftc` | bool | 55/55 |  | False |
| `author.id` | str | 55/55 | Author user id | 7411242582287811589 |
| `author.isADVirtual` | bool | 55/55 |  | False |
| `author.isEmbedBanned` | bool | 55/55 |  | False |
| `author.nickname` | str | 55/55 | Display name | KUBA |
| `author.openFavorite` | bool | 55/55 |  | False |
| `author.privateAccount` | bool | 55/55 | Private account | False |
| `author.relation` | int | 55/55 | Viewer relation | 0 |
| `author.secUid` | str | 55/55 | Stable secUid (for fetch_account_posts) | MS4wLjABAAAAhYSCVnfPGdPI0eLXpD7B8hZxpRGCrbRSeS |
| `author.secret` | bool | 55/55 |  | False |
| `author.shortDramaCreator` | dict | 55/55 |  |  |
| `author.signature` | str | 55/55 | Bio text | Ast  Insta: nauryz.abdikerim |
| `author.stitchSetting` | int | 55/55 |  | 0 |
| `author.uniqueId` | str | 55/55 | @handle | kyb.osh |
| `author.verified` | bool | 55/55 | Verified badge | False |
| `authorStats` | dict | 55/55 |  |  |
| `authorStats.diggCount` | int | 55/55 | Likes the author gave | 24100 |
| `authorStats.followerCount` | int | 55/55 | Author FOLLOWERS (baseline for ratios) | 1230 |
| `authorStats.followingCount` | int | 55/55 | Accounts the author follows | 9536 |
| `authorStats.friendCount` | int | 55/55 |  | 0 |
| `authorStats.heart` | int | 55/55 |  | 120600 |
| `authorStats.heartCount` | int | 55/55 | Author total likes | 120600 |
| `authorStats.videoCount` | int | 55/55 | Author video count | 8 |
| `authorStatsV2` | dict | 55/55 |  |  |
| `authorStatsV2.diggCount` | str | 55/55 |  | 24100 |
| `authorStatsV2.followerCount` | str | 55/55 |  | 1230 |
| `authorStatsV2.followingCount` | str | 55/55 |  | 9536 |
| `authorStatsV2.friendCount` | str | 55/55 |  | 0 |
| `authorStatsV2.heart` | str | 55/55 |  | 120600 |
| `authorStatsV2.heartCount` | str | 55/55 |  | 120600 |
| `authorStatsV2.videoCount` | str | 55/55 |  | 8 |

### Sound / music

| field path | type | present | meaning | example |
|---|---|---|---|---|
| `music` | dict | 55/55 |  |  |
| `music.authorName` | str | 55/55 | Sound author | vse treki telegram kanale |
| `music.coverLarge` | str | 55/55 |  | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `music.coverMedium` | str | 55/55 |  | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `music.coverThumb` | str | 55/55 |  | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `music.duration` | int | 55/55 | Sound length (s) | 32 |
| `music.id` | str | 55/55 | Stable sound_id | 7614509871785970433 |
| `music.isCopyrighted` | bool | 55/55 | Copyrighted | False |
| `music.is_commerce_music` | bool | 55/55 | Commercial-library music | True |
| `music.is_unlimited_music` | bool | 55/55 |  | False |
| `music.original` | bool | 55/55 | Original vs licensed | True |
| `music.playUrl` | str | 55/55 | Audio stream URL | https://v77.tiktokcdn.com/f6dffd2321fa9aa5993b |
| `music.private` | bool | 55/55 |  | False |
| `music.shoot_duration` | int | 55/55 |  | 32 |
| `music.title` | str | 55/55 | Sound name | оригинальный звук |
| `music.tt2dsp` | dict | 55/55 |  |  |
| `music.tt2dsp.tt_to_dsp_song_infos` | list | 19/55 |  |  |
| `music.tt2dsp.tt_to_dsp_song_infos[].meta_song_id` | str | 19/55 |  | 6850394690584315905 |
| `music.tt2dsp.tt_to_dsp_song_infos[].platform` | int | 19/55 | DSP platform (Apple/Spotify) mapping | 1 |
| `music.tt2dsp.tt_to_dsp_song_infos[].song_id` | str | 19/55 |  | 1521277305 |
| `music.tt2dsp.tt_to_dsp_song_infos[].token` | dict | 19/55 |  |  |
| `music.tt2dsp.tt_to_dsp_song_infos[].token.apple_music_token` | dict | 19/55 |  |  |
| `music.tt2dsp.tt_to_dsp_song_infos[].token.apple_music_token.developer_token` | str | 19/55 |  | eyJhbGciOiJFUzI1NiIsImtpZCI6Ikc2Q0dLMjdWQzMifQ |
| `music.album` | str | 4/55 | Album | Drukqs |

### Hashtags, mentions, challenges

| field path | type | present | meaning | example |
|---|---|---|---|---|
| `challenges` | list | 31/55 |  |  |
| `challenges[].coverLarger` | str | 31/55 |  | https://p16-common-sign.tiktokcdn.com/musicall |
| `challenges[].coverMedium` | str | 31/55 |  | https://p16-common-sign.tiktokcdn.com/musicall |
| `challenges[].coverThumb` | str | 31/55 |  | https://p16-common-sign.tiktokcdn.com/musicall |
| `challenges[].desc` | str | 31/55 | Challenge description | Celebrate all the crunchy, gooey, juicy, and e |
| `challenges[].id` | str | 31/55 | Challenge id | 50321361 |
| `challenges[].profileLarger` | str | 31/55 |  | https://p16-common-sign.tiktokcdn.com/musicall |
| `challenges[].profileMedium` | str | 31/55 |  | https://p16-common-sign.tiktokcdn.com/musicall |
| `challenges[].profileThumb` | str | 31/55 |  | https://p16-common-sign.tiktokcdn.com/musicall |
| `challenges[].title` | str | 31/55 | Associated hashtag/challenge name | рек |
| `contents` | list | 43/55 |  |  |
| `contents[].desc` | str | 43/55 | Caption (alt copy) | #рек #ош🇰🇬 #москва #казахстан🇰🇿 #атакатитанов  |
| `contents[].textExtra` | list | 32/55 |  |  |
| `contents[].textExtra[].awemeId` | str | 32/55 |  |  |
| `contents[].textExtra[].end` | int | 32/55 |  | 4 |
| `contents[].textExtra[].hashtagName` | str | 32/55 | Hashtag (alt copy) | рек |
| `contents[].textExtra[].isCommerce` | bool | 32/55 |  | False |
| `contents[].textExtra[].start` | int | 32/55 |  | 0 |
| `contents[].textExtra[].subType` | int | 32/55 |  | 0 |
| `contents[].textExtra[].type` | int | 32/55 |  | 1 |
| `textExtra` | list | 34/55 |  |  |
| `textExtra[].awemeId` | str | 34/55 |  |  |
| `textExtra[].end` | int | 34/55 |  | 4 |
| `textExtra[].hashtagName` | str | 34/55 | HASHTAG on the post | рек |
| `textExtra[].isCommerce` | bool | 34/55 |  | False |
| `textExtra[].start` | int | 34/55 |  | 0 |
| `textExtra[].subType` | int | 34/55 |  | 0 |
| `textExtra[].type` | int | 34/55 | Entity type (1=hashtag) | 1 |
| `contents[].textExtra[].secUid` | str | 6/55 |  | MS4wLjABAAAA0O6Hnjm5IbCiPGmkLUvjHnG4vXX0gNWIXO |
| `contents[].textExtra[].userId` | str | 6/55 |  | 7012976715492279301 |
| `contents[].textExtra[].userUniqueId` | str | 6/55 |  | erkin_tolen |
| `textExtra[].secUid` | str | 6/55 |  | MS4wLjABAAAA0O6Hnjm5IbCiPGmkLUvjHnG4vXX0gNWIXO |
| `textExtra[].userId` | str | 6/55 |  | 7012976715492279301 |
| `textExtra[].userUniqueId` | str | 6/55 | @mention in caption | erkin_tolen |

### Video, media & content-understanding

| field path | type | present | meaning | example |
|---|---|---|---|---|
| `video` | dict | 55/55 |  |  |
| `video.PlayAddrStruct` | dict | 55/55 |  |  |
| `video.PlayAddrStruct.DataSize` | int | 55/55 |  | 1605080 |
| `video.PlayAddrStruct.FileCs` | str | 55/55 |  | c:0-8667-c193 |
| `video.PlayAddrStruct.FileHash` | str | 55/55 |  | 786adffd086948a7e82c9f6e8349386f |
| `video.PlayAddrStruct.Height` | int | 55/55 |  | 1024 |
| `video.PlayAddrStruct.Uri` | str | 55/55 |  | v1c044g50000d8nc817og65laoe3n0q0 |
| `video.PlayAddrStruct.UrlKey` | str | 55/55 |  | v1c044g50000d8nc817og65laoe3n0q0_h264_540p_151 |
| `video.PlayAddrStruct.UrlList` | list | 55/55 |  |  |
| `video.PlayAddrStruct.Width` | int | 55/55 |  | 576 |
| `video.VQScore` | str | 55/55 |  | 60.95 |
| `video.bitrate` | int | 55/55 |  | 1514941 |
| `video.bitrateInfo` | list | 55/55 |  |  |
| `video.bitrateInfo[].Bitrate` | int | 55/55 |  | 1514941 |
| `video.bitrateInfo[].BitrateFPS` | int | 55/55 |  | 30 |
| `video.bitrateInfo[].CodecType` | str | 55/55 |  | h264 |
| `video.bitrateInfo[].Format` | str | 55/55 |  | mp4 |
| `video.bitrateInfo[].GearName` | str | 55/55 |  | normal_540_0 |
| `video.bitrateInfo[].MVMAF` | str | 55/55 |  | {"v2.0": {"srv1": {"v1080": 94.966, "v960": 96 |
| `video.bitrateInfo[].PlayAddr` | dict | 55/55 |  |  |
| `video.bitrateInfo[].PlayAddr.DataSize` | int | 55/55 |  | 1605080 |
| `video.bitrateInfo[].PlayAddr.FileCs` | str | 55/55 |  | c:0-8667-c193 |
| `video.bitrateInfo[].PlayAddr.FileHash` | str | 55/55 |  | 786adffd086948a7e82c9f6e8349386f |
| `video.bitrateInfo[].PlayAddr.Height` | int | 55/55 |  | 1024 |
| `video.bitrateInfo[].PlayAddr.Uri` | str | 55/55 |  | v1c044g50000d8nc817og65laoe3n0q0 |
| `video.bitrateInfo[].PlayAddr.UrlKey` | str | 55/55 |  | v1c044g50000d8nc817og65laoe3n0q0_h264_540p_151 |
| `video.bitrateInfo[].PlayAddr.UrlList` | list | 55/55 |  |  |
| `video.bitrateInfo[].PlayAddr.Width` | int | 55/55 |  | 576 |
| `video.bitrateInfo[].QualityType` | int | 55/55 |  | 20 |
| `video.bitrateInfo[].VideoExtra` | str | 55/55 |  | {"PktOffsetMap":"[{\"time\": 1, \"offset\": 26 |
| `video.claInfo` | dict | 55/55 |  |  |
| `video.claInfo.enableAutoCaption` | bool | 55/55 | Auto-captions on | True |
| `video.claInfo.hasOriginalAudio` | bool | 55/55 |  | False |
| `video.claInfo.noCaptionReason` | int | 36/55 |  | 3 |
| `video.codecType` | str | 55/55 |  | h264 |
| `video.cover` | str | 55/55 | Thumbnail URL | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `video.definition` | str | 55/55 | Resolution label | 540p |
| `video.downloadAddr` | str | 46/55 | Download URL (when present) | https://v16-webapp.tiktok.com/4912481918940871 |
| `video.duration` | int | 55/55 | Duration (s) | 8 |
| `video.dynamicCover` | str | 55/55 | Animated thumbnail | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `video.encodeUserTag` | str | 55/55 |  |  |
| `video.encodedType` | str | 55/55 |  | normal |
| `video.format` | str | 55/55 |  | mp4 |
| `video.height` | int | 55/55 | Height px | 1024 |
| `video.id` | str | 55/55 |  | 7651269245652798741 |
| `video.originCover` | str | 55/55 | Origin thumbnail | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `video.playAddr` | str | 55/55 | Video stream URL | https://v16-webapp.tiktok.com/d4f3186c2bfe8245 |
| `video.ratio` | str | 55/55 |  | 540p |
| `video.size` | int | 55/55 |  | 1605080 |
| `video.videoID` | str | 55/55 |  | v1c044g50000d8nc817og65laoe3n0q0 |
| `video.videoQuality` | str | 55/55 |  | normal |
| `video.volumeInfo` | dict | 55/55 |  |  |
| `video.volumeInfo.Loudness` | float/int | 55/55 | Audio loudness (LUFS) | -13.5 |
| `video.volumeInfo.Peak` | float/int | 55/55 |  | 0.79433 |
| `video.width` | int | 55/55 | Width px | 576 |
| `video.zoomCover` | dict | 55/55 |  |  |
| `video.zoomCover.240` | str | 55/55 |  | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `video.zoomCover.480` | str | 55/55 |  | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `video.zoomCover.720` | str | 55/55 |  | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `video.zoomCover.960` | str | 55/55 |  | https://p16-common-sign.tiktokcdn.com/tos-alis |
| `stickersOnItem` | list | 21/55 |  |  |
| `stickersOnItem[].stickerText` | list | 21/55 | ON-SCREEN TEXT overlays |  |
| `stickersOnItem[].stickerType` | int | 21/55 |  | 4 |
| `video.claInfo.captionInfos` | list | 19/55 |  |  |
| `video.claInfo.captionInfos[].captionFormat` | str | 19/55 |  | webvtt |
| `video.claInfo.captionInfos[].claSubtitleID` | str | 19/55 |  | 7653910350518110994 |
| `video.claInfo.captionInfos[].expire` | str | 19/55 |  | 1782642640 |
| `video.claInfo.captionInfos[].isAutoGen` | bool | 19/55 |  | True |
| `video.claInfo.captionInfos[].isOriginalCaption` | bool | 19/55 |  | True |
| `video.claInfo.captionInfos[].language` | str | 19/55 | Caption language | rus-RU |
| `video.claInfo.captionInfos[].languageCode` | str | 19/55 |  | ru |
| `video.claInfo.captionInfos[].languageID` | str | 19/55 |  | 6 |
| `video.claInfo.captionInfos[].subID` | str | 19/55 |  | 317547900 |
| `video.claInfo.captionInfos[].subtitleType` | str | 19/55 |  | 1 |
| `video.claInfo.captionInfos[].translationType` | str | 19/55 |  | 0 |
| `video.claInfo.captionInfos[].url` | str | 19/55 |  | https://v16-webapp.tiktok.com/9c136ff326625655 |
| `video.claInfo.captionInfos[].urlList` | list | 19/55 |  |  |
| `video.claInfo.captionInfos[].variant` | str | 19/55 |  | whisper_lid |
| `video.claInfo.captionsType` | int | 19/55 |  | 1 |
| `video.claInfo.originalLanguageInfo` | dict | 19/55 |  |  |
| `video.claInfo.originalLanguageInfo.canTranslateRealTimeNoCheck` | bool | 19/55 |  | True |
| `video.claInfo.originalLanguageInfo.language` | str | 19/55 |  | rus-RU |
| `video.claInfo.originalLanguageInfo.languageCode` | str | 19/55 | SPOKEN language of the video | ru |
| `video.claInfo.originalLanguageInfo.languageID` | str | 19/55 |  | 6 |
| `video.subtitleInfos` | list | 19/55 |  |  |
| `video.subtitleInfos[].Format` | str | 19/55 |  | webvtt |
| `video.subtitleInfos[].LanguageCodeName` | str | 19/55 | Subtitle language | rus-RU |
| `video.subtitleInfos[].LanguageID` | str | 19/55 |  | 6 |
| `video.subtitleInfos[].Size` | int | 19/55 |  | 500 |
| `video.subtitleInfos[].Source` | str | 19/55 |  | ASR |
| `video.subtitleInfos[].Url` | str | 19/55 |  | https://v16-webapp.tiktok.com/9c136ff326625655 |
| `video.subtitleInfos[].UrlExpire` | int | 19/55 |  | 1782642640 |
| `video.subtitleInfos[].Version` | str | 19/55 |  | 1:whisper_lid |
| `anchors` | list | 8/55 |  |  |
| `anchors[].description` | str | 8/55 |  | CapCut · Video Editor |
| `anchors[].extraInfo` | dict | 8/55 |  |  |
| `anchors[].extraInfo.subtype` | str | 8/55 |  |  |
| `anchors[].icon` | dict | 8/55 |  |  |
| `anchors[].icon.urlList` | list | 8/55 |  |  |
| `anchors[].id` | str | 8/55 |  | 0 |
| `anchors[].keyword` | str | 8/55 | Anchor label (CapCut template / product) | CapCut · Попробуйте этот шаблон |
| `anchors[].logExtra` | str | 6/55 |  | {"anchor_id":0,"anchor_name":"CapCut · Попробу |
| `anchors[].schema` | str | 8/55 | Anchor deep link | https://www.capcut.com/template-detail/7260099 |
| `anchors[].thumbnail` | dict | 8/55 |  |  |
| `anchors[].thumbnail.height` | int | 8/55 |  | 64 |
| `anchors[].thumbnail.urlList` | list | 8/55 |  |  |
| `anchors[].thumbnail.width` | int | 8/55 |  | 64 |
| `anchors[].type` | int | 8/55 | Anchor type code | 54 |
| `effectStickers` | list | 16/55 |  |  |
| `effectStickers[].ID` | str | 16/55 |  | 4274108785 |
| `effectStickers[].name` | str | 16/55 | AR effect/filter name | Natural1 |
| `effectStickers[].stickerStats` | dict | 16/55 |  |  |
| `effectStickers[].stickerStats.useCount` | int | 16/55 |  | 0 |
| `video.bitrateAudioInfo` | list | 1/55 |  |  |
| `video.bitrateAudioInfo[].AudioDataSize` | int | 1/55 |  | 654282 |
| `video.bitrateAudioInfo[].AudioFPS` | int | 1/55 |  | 0 |
| `video.bitrateAudioInfo[].AudioFormat` | str | 1/55 |  | dash |
| `video.bitrateAudioInfo[].AudioQuality` | int | 1/55 |  | 4 |
| `video.bitrateAudioInfo[].AudioQualityString` | str | 1/55 |  | lower |
| `video.bitrateAudioInfo[].Bitrate` | int | 1/55 |  | 33076 |
| `video.bitrateAudioInfo[].CodecType` | str | 1/55 |  | h265_hvc1 |
| `video.bitrateAudioInfo[].EncodedType` | str | 1/55 |  | normal |
| `video.bitrateAudioInfo[].FileHash` | str | 1/55 |  | 6981c654d555f2dfa3c19cddc4848038 |
| `video.bitrateAudioInfo[].FileId` | str | 1/55 |  | 6981c654d555f2dfa3c19cddc4848038 |
| `video.bitrateAudioInfo[].Format` | str | 1/55 |  | dash |
| `video.bitrateAudioInfo[].MediaType` | str | 1/55 |  | audio |
| `video.bitrateAudioInfo[].SubInfoString` | str | 1/55 |  | {"base_range_info":{"init_range":"0-998","inde |
| `video.bitrateAudioInfo[].UrlList` | dict | 1/55 |  |  |
| `video.bitrateAudioInfo[].UrlList.BackupUrl` | str | 1/55 |  | https://v19-webapp-prime.tiktok.com/video/tos/ |
| `video.bitrateAudioInfo[].UrlList.FallbackUrl` | str | 1/55 |  | https://www.tiktok.com/aweme/v1/play/?faid=198 |
| `video.bitrateAudioInfo[].UrlList.MainUrl` | str | 1/55 |  | https://v16-webapp-prime.tiktok.com/video/tos/ |

### Interaction flags & permissions

| field path | type | present | meaning | example |
|---|---|---|---|---|
| `duetDisplay` | int | 55/55 | Duet allowed (0/1) | 0 |
| `duetEnabled` | bool | 52/55 | Duet enabled | True |
| `isAd` | bool | 55/55 | Paid/branded content flag | False |
| `itemCommentStatus` | int | 55/55 |  | 0 |
| `item_control` | dict | 55/55 |  |  |
| `item_control.can_repost` | bool | 55/55 | Repost permitted | True |
| `officalItem` | bool | 55/55 | Official/curated item | False |
| `originalItem` | bool | 55/55 | Original (not duet/stitch) | False |
| `shareEnabled` | bool | 55/55 |  | True |
| `stitchDisplay` | int | 55/55 | Stitch allowed (0/1) | 0 |
| `stitchEnabled` | bool | 52/55 | Stitch enabled | True |

### Location (POI, when present)

| field path | type | present | meaning | example |
|---|---|---|---|---|
| `playlistId` | str | 1/55 |  | 7565760911143717639 |
| `poi` | dict | 1/55 |  |  |
| `poi.address` | str | 1/55 | Address | Украина |
| `poi.category` | str | 1/55 |  | Место и адрес |
| `poi.city` | str | 1/55 | City |  |
| `poi.cityCode` | str | 1/55 |  | 709930 |
| `poi.country` | str | 1/55 | Country |  |
| `poi.countryCode` | str | 1/55 |  | 690791 |
| `poi.fatherPoiId` | str | 1/55 |  |  |
| `poi.fatherPoiName` | str | 1/55 |  |  |
| `poi.id` | str | 1/55 |  | 22535865205104799 |
| `poi.name` | str | 1/55 | Location name | Днепр |
| `poi.province` | str | 1/55 |  |  |
| `poi.ttTypeCode` | str | 1/55 |  | 19a3a0 |
| `poi.ttTypeNameMedium` | str | 1/55 |  | Места |
| `poi.ttTypeNameSuper` | str | 1/55 |  | Место и адрес |
| `poi.ttTypeNameTiny` | str | 1/55 |  | Город |
| `poi.type` | int | 1/55 |  | 0 |
| `poi.typeCode` | str | 1/55 |  |  |

### Other / misc

| field path | type | present | meaning | example |
|---|---|---|---|---|
| `IsHDBitrate` | bool | 55/55 |  | False |
| `backendSourceEventTracking` | str | 55/55 |  |  |
| `textTranslatable` | bool | 55/55 |  | False |
| `aigcLabelType` | int | 1/55 |  | 2 |