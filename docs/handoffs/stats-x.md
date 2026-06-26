# Handoff — X: full stats capture

Read `docs/SIGNALS.md` and `docs/INGESTION-CONTRACT.md` first. Goal: capture X's available Tier-1
signals for every post. **No isolation needed** — X uses pure HTTP (syndication + guest-token
GraphQL), no browser, no account. Skip the harness entirely.

## Capture X's available subset (per docs/SIGNALS.md)
- `like_count` ← `favorite_count`
- `comment_count` ← `reply_count`
- **`share_count` ← `retweet_count`** (reposts); keep `quote_count` in `raw`
- `view_count` ← `views.count` **only via the opt-in guest-token GraphQL path** (`prefer_graphql=True`);
  `None` on the default syndication path. This is the only way to get X views.
- **`save_count` → `None`** (bookmark counts are private — not available)
- `hashtags`, `posted_at` ← `created_at`
- **`author_follower_count`** ← user object (ADD if missing)
- no sound concept → `sound_id`/`sound_name` = `None`
- complete tweet dict → `raw`

`media_type` mostly `text`; some `video`/`image`/`carousel`. Do NOT compute ratios (Tier 2 = `core`).

## Note for ranking
X has no saves and views are opt-in/fragile, so X ranking leans on **share rate (retweets) +
engagement rate**. Flag this in the README so `core`'s ranker degrades gracefully for X.

## Definition of done
PostRecords carry like/comment/share counts + `author_follower_count` + full `raw`; `view_count`
populated when `prefer_graphql=True`. README field table updated.
