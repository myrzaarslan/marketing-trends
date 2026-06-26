# Handoff — Threads: full stats capture

Read `docs/SIGNALS.md` and `docs/INGESTION-CONTRACT.md` first. Goal: capture Threads' available
Tier-1 signals for every post. **Isolation:** Threads uses an unauthenticated Playwright GraphQL
intercept (no login, no persona today), so you do NOT set up isolation. If Threads later adopts a
login wall (it reuses IG's backend), import the shared harness `core/harness/persona_browser.py`
from the Instagram agent — don't build your own.

## Capture Threads' available subset (per docs/SIGNALS.md)
- `like_count` ← `like_count`
- `comment_count` ← `text_post_app_info.direct_reply_count`
- **`share_count` ← `text_post_app_info.repost_count`** (reposts)
- **`view_count` → `None`** (not exposed for most posts; capture `play_count` opportunistically on
  videos, else `None`)
- **`save_count` → `None`** (no public bookmark count)
- `hashtags`, `posted_at` ← `taken_at`, `media_type`
- `sound_id`/`sound_name` ← `clips_metadata.music_info` (rare on Threads, usually `None`)
- **`author_follower_count`** ← user object (ADD if missing)
- complete node → `raw`

Do NOT compute ratios/velocity (Tier 2 = `core`).

## Note for ranking
Threads has no views and no saves → ranking leans on **engagement rate (likes+comments+reposts)**.
Flag in README so `core`'s ranker degrades gracefully.

## Definition of done
PostRecords carry like/comment/share counts + `author_follower_count` + full `raw`, with the
unavailable signals honestly left `None`. README field table updated.
