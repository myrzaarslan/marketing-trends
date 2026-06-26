# Handoff — TikTok: full stats capture

Read `docs/SIGNALS.md` and `docs/INGESTION-CONTRACT.md` first. Goal: make sure the TikTok adapter
captures the **complete Tier-1 signal set** for every post it returns (account-posts AND discovery).
**You do NOT set up isolation** — if/when TikTok goes logged-in-persona, import the shared harness
`core/harness/persona_browser.py` that the Instagram agent builds. For now (logged-out FYP + signed
item_list) no persona isolation is needed.

## TikTok is the richest platform — capture ALL of it
Per `docs/SIGNALS.md`, TikTok exposes every signal (it's the only one that does):
- `view_count` ← `playCount`/`stats.playCount`
- `like_count` ← `diggCount`
- `comment_count` ← `commentCount`
- **`share_count` ← `shareCount`**
- **`save_count` ← `collectCount`**  ← TikTok is the ONLY platform with this; capture it reliably
- `sound_id`/`sound_name` ← `music.id` / `music.title`
- `hashtags`, `duration_sec`, `media_type`, `posted_at` ← `createTime`
- **`author_follower_count`** ← `authorStats.followerCount` (ADD if missing)
- complete item object → `raw`

The signed `item_list` path already carries all of these; confirm each maps to the normalized
column (not left in `raw`) so `core` can rank without re-parsing. Do NOT compute ratios/velocity
(Tier 2 = `core`).

## Definition of done
Both `fetch_account_posts` and `fetch_viral_posts` return PostRecords with all TikTok Tier-1 signals
populated incl. `share_count`, `save_count`, `author_follower_count`, plus full `raw`. README field
table updated to confirm the full set.
