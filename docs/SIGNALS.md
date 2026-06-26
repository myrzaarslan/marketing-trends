# Signals & Ranking Spec

The canonical data every adapter captures, and how `core` turns it into rankings. Goal: capture
the **same signals across all platforms** — within what each platform actually exposes.

## Two tiers (who computes what)

**Tier 1 — captured by ADAPTERS, per snapshot (raw, no math):**
- Engagement counts: `view_count`, `like_count`, `comment_count`, `share_count`, `save_count`
- Content: `sound_id`/`sound_name`, `hashtags`, `caption`, `duration_sec`, `media_type`
- Author: `account_handle`, `author_follower_count`, verified (in `raw`)
- Time: `posted_at`, `fetched_at`
- The COMPLETE original payload in `raw` (capture everything)

**Tier 2 — derived by `core`, needs storage/history (adapters must NOT compute these):**
- **Engagement rate** = (likes+comments+shares+saves)/views
- **Share rate** = shares/views · **Save rate** = saves/views
- **Velocity** = Δcounts across snapshots over time → "rising now" (needs repeated snapshots — the
  ONLY truly time-dependent sort)
- **Relative-to-baseline** = views vs the creator's own median (needs the creator's **other posts**,
  NOT time — one scrape of the account's recent posts makes it computable immediately)
- **Cross-persona breadth** = how many distinct personas' feeds a video appeared in (needs multiple
  distinct sources, accrued across harvests — not calendar days)
- **Engagement-rate-followers** = engagement ÷ follower count (needs only the follower count, present
  in a single snapshot; the one denominator that exists on all four platforms)

Adapters capture Tier 1 + `raw`; `core` computes Tier 2 over stored rows. Only **velocity** truly
needs repeated snapshots over time. **Baseline** needs the account's post *corpus* (cheap — scrape the
account once); **breadth** needs multiple distinct sources; **follower-rate** needs one field. See
CORE-SPINE.md "three independent gates" for the exact per-sort gating.

## Per-platform availability (HONEST — not every signal exists everywhere)

| Signal | TikTok | Instagram | X | Threads |
|---|---|---|---|---|
| views | `playCount` ✅ | reel/video only ✅ | opt-in GraphQL ⚠️ | ❌ |
| likes | `diggCount` ✅ | `like_count` ✅ | `favorite_count` ✅ | `like_count` ✅ |
| comments | `commentCount` ✅ | `comment_count` ✅ | `reply_count` ✅ | `direct_reply_count` ✅ |
| **shares** | `shareCount` ✅ | **❌** | `retweet_count` ✅ | `repost_count` ✅ |
| **saves** | `collectCount` ✅ | **❌** | **❌** (bookmarks private) | **❌** |
| sound/music | `music.id` ✅ | reels ✅ | ❌ | rare |
| hashtags | ✅ | ✅ (caption) | ✅ | ✅ |
| author followers | `authorStats` ✅ | `user` ✅ | `user` ✅ | `user` ✅ |
| posted_at | `createTime` ✅ | `taken_at` ✅ | `created_at` ✅ | `taken_at` ✅ |

**Consequences for ranking:**
- **Save rate exists ONLY on TikTok.** Don't build a ranker that assumes it elsewhere.
- **Shares missing on Instagram** → share rate is TikTok/X/Threads only.
- **Views unreliable off TikTok** (IG reels-only, X opt-in, Threads none).
- The only **universal** signal is **engagement rate over whatever counts that platform exposes**.
  The cross-platform ranker must **degrade gracefully** to each platform's available subset and
  never compare a TikTok save-rate against an IG that can't produce one.

## Cross-persona breadth (an ingestion concern, not an adapter one)

Adapters don't know about personas. The harvest harness tags each captured post with the
**persona/session** that saw it. `core` stores `(video_id, persona_id, snapshot_at, counts)` rows,
dedupes by `video_id`, and `COUNT(DISTINCT persona_id)` = breadth. So "capture across personas" =
persist a row per (post × persona × snapshot); breadth and velocity both fall out of that table.

## Schema note
`author_follower_count` is added to `PostRecord` (optional) for baseline/normalization. Everything
else not normalized stays in `raw`.
