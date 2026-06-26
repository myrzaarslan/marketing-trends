# Build handoff — Layer-3 Enrichment (Content Bundles)

You are a SONNET build agent. Build the enrichment module that, for the top-N ranked posts, downloads
their complete content into a **Content Bundle**. **Pure EXTRACTION — no understanding, no comments.**
**Escalate any non-trivial design fork to the Opus orchestrator.**

## Read first (source of truth)
`docs/CORE-SPINE.md` → "Enrichment / Layer-3" + the `post_content` table + "Build / stack decisions" ·
`CONTEXT.md` → "Content Bundle" · `docs/SIGNALS.md` · the four `adapters/*/README.md` (for the `raw`
payload shapes — where media URLs / sound / author live per platform).

## Own ONLY this path
`enrichment/`. Import from `core` **read-only**; write rows **only** via `core.storage`
(`write_post_content`, `set_thumbnail_path`) — never invent your own schema or touch `core/schema.py`.

## What to build
A function `enrich(post_identities: list[(platform, platform_post_id)]) -> None` that, for each post
**not already in `post_content`**:
1. Reads the post's freshly-captured `raw` (passed in or read via `core.storage`) for the **signed,
   EXPIRING** media CDN URLs — act promptly.
2. Downloads **every** media file → `data/media/<platform>/<platform_post_id>/` (video(s), all
   carousel images, cover). **Direct CDN GET** with the URL+headers from `raw`; **fall back to yt-dlp**
   for TikTok video files that need signing.
3. Captures: caption + any **spoiler-hidden text** (Threads tap-to-reveal is UI-only → already in
   `raw`, verify against the `@quokkaredfield` fixture), sound id/name/author, author display name.
4. Writes one `post_content` row (`media_paths` JSON, text, sound, author, `extracted_at`, `status`).
   On an already-expired URL, record `status=expired_url_miss` (NO re-fetch — see CORE-SPINE).
Also provide a `download_thumbnail(post) -> path` helper the ingestion process calls for EVERY post.

## Parallel-build note
The spine agent owns `core.storage`/`post_content`. Until it lands, build against the **documented
`post_content` schema** in CORE-SPINE with a thin local stub for `core.storage`, and a small fixture
runner. Integrate when the spine's `core.storage` exists.

## Acceptance fixtures (operator-provided; YouTube EXCLUDED)
TikTok video · Threads image · Threads video · **Threads spoiler** · Instagram reel (+music) ·
Instagram image/carousel. For each, produce a complete Content Bundle (all media on disk + the text /
sound / author record). Exclude the two YouTube URLs.

## Definition of done
`enrich([...])` produces correct, complete Content Bundles for the fixture set, idempotent (re-running
skips already-done posts), media bytes on the filesystem, metadata rows via `core.storage`. README in
`enrichment/` documenting the per-platform `raw` field map.
