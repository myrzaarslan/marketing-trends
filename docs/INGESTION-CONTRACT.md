# Ingestion Contract

The shared agreement every per-platform adapter session MUST build against, so parallel
work stays compatible. If you're starting a platform prototype, read this first.

## Principle: raw + normalized ("capture everything")

Each adapter returns objects that carry BOTH:
- a **normalized** common field set (same names on every platform), AND
- a **`raw`** dict holding the complete original platform payload, untouched.

Rationale: the "what is viral" rule is undecided (OPEN-QUESTIONS Q-1). Keeping the raw payload
means any future rule can be computed retroactively without re-scraping. Never drop fields you
don't currently use — dump them into `raw`.

## Storage (prototype default)

SQLite, single file `data/trends.db` ($0, zero-setup, good enough for v1). Tables mirror the
dataclasses in `core/schema.py`: `post_records`, `trends`, `accounts`. Normalized fields become
columns; the full payload goes in a `raw_json` TEXT column. Swap to Postgres later if needed —
adapters never touch storage directly, they return dataclasses and `core` persists them.

## Adapter interface

Every platform implements `core.adapter.PlatformAdapter`:

- `platform: str` — e.g. `"tiktok"`
- `fetch_account_posts(account, limit=30) -> list[PostRecord]` — recent posts for one Watched Account
- `fetch_trends(geo_tier) -> list[Trend]` — current trends for a Geo Tier (may return [] if the platform has no trend source)

Rules:
- An adapter imports ONLY from `core`. It NEVER imports another adapter.
- An adapter owns exactly its `adapters/<platform>/` folder.
- An adapter does not write to the DB or decide virality. It fetches and normalizes. Period.

## Normalized PostRecord fields (the common set)

platform · platform_post_id · account_handle · url · posted_at · fetched_at · media_type
(video|image|text|carousel) · caption · hashtags[] · sound_id · sound_name · duration_sec ·
view_count · like_count · comment_count · share_count · save_count · thumbnail_url · geo_tier · raw

Any field a platform doesn't provide → `None` (and it'll still be in `raw` if it exists at all).

## Normalized Trend fields

platform · trend_type (hashtag|sound|format|topic) · name · geo_tier · rank · score · volume ·
sampled_at · raw
