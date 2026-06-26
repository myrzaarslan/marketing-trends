# Build handoff — Digest post UI (full Content Bundle display)

You are a SONNET build agent. Extend the digest into a rich, Instagram-style post viewer that displays
each post's full **Content Bundle** (media + stats + text + music + provenance + source link).

## FIRST: read and FOLLOW the frontend-design skill
Read `/home/myrzaarslan/.claude/plugins/cache/claude-plugins-official/frontend-design/unknown/skills/frontend-design/SKILL.md`
and follow its process: brainstorm a compact token system (palette / type / layout / signature),
critique it against this brief (avoid the 3 default AI looks it warns about), THEN build, then
self-critique (take screenshots if you can). Derive every color/type choice from your plan.

### Subject grounding (don't design a generic consumer IG clone)
This is an internal **trend-intelligence console** for an **EdTech marketing team in Kazakhstan/CIS +
world**, whose job is to **reverse-engineer viral posts** to recreate them. Audience = marketers/
analysts, not consumers. Page job = scan ranked viral posts, study each specimen's full content, jump
to the source. The signature is a **"specimen viewer"**: media is the hero; engagement is treated as
*data* (tabular/mono numerals, a rank+score chip); a **provenance strip** (platform · geo tier ·
source · posted-vs-first-seen · open-original link) encodes something true about each post.

## Read also
`docs/CORE-SPINE.md` (storage/ranker/API/enrichment), `core/storage.py`, `core/ranker.py`,
`api/main.py`, the existing `web/`, `enrichment/README.md`, `CONTEXT.md` (Content Bundle, SIGNALS).

## Own ONLY these paths
`web/`, `api/`, and read-helpers in `core/storage.py`. **Do NOT edit** `enrichment/`, `adapters/`, or
`core/harness/` — another agent is editing those right now. You may *call* `enrichment.enrich(...)`
(read-only use), but never modify it.

## What to build
### API (`api/main.py`)
- **Static-mount `data/media/` read-only** so the SPA can load downloaded media (video.mp4, slide_*.jpg,
  cover.jpg). Serve by a stable URL like `/media/<platform>/<platform_post_id>/<file>`.
- **`GET /post/{platform}/{platform_post_id}`** → the full Content Bundle: media file URLs (ordered),
  media_type, caption, `spoiler_text` (+ a boolean has_spoiler), sound_id/name/author, author display
  name, all engagement counts (from latest snapshot), posted_at, first_seen_at, the source link (`url`),
  platform, geo_tier, rank/score context if available, and `enriched` (bool).
- Extend **`GET /digest`** cards with `has_content_bundle` + `thumbnail` URL so the grid can show
  thumbnails immediately and mark which open a full bundle.
- Keep the API **read-only** over SQLite (plus the static media mount). No browser/scraping in the API.

### Populate real data so the UI isn't empty
- The `post_content` table is currently EMPTY; 1,224 posts are seeded; 3 real bundles exist in
  `data/enrichment_stub.json` + `data/media/` (TikTok video, IG reel, IG 14-slide carousel).
- **Import those 3 stub bundles into `post_content`** (insert their parent `posts` row if missing) so
  they render, AND **run `enrichment.enrich(...)` on the current digest top-N** (small N, e.g. 8–12 per
  platform×geo) to produce real bundles tied to digest posts. Respect anti-ban pacing; if enrichment
  fails/partial, the UI must still work on whatever bundles exist (graceful).

### Web (`web/`, Vite + React + TS)
- Keep the **filter/sort bar** (geo / period / platform / sort) — it's the core value; gray out
  unavailable sorts per SIGNALS (as today).
- Render results as **Instagram-style post cards**; clicking opens a **full-post lightbox/modal**:
  - **Media:** inline `<video controls>` for video; **swipeable carousel** for multi-image; image for
    single image. Text posts (X/Threads) → the **text is the hero** (typographic treatment).
  - **Stats:** likes, comments, and shares/saves/views **only where the platform exposes them**
    (SIGNALS.md — e.g. no saves/shares on IG, no views off TikTok); never render a fake 0.
  - **Caption/text**, hashtags.
  - **Spoiler:** if `has_spoiler`, render the text/media behind a **frosted blur the user clicks to
    reveal** (mirrors Threads' content-warning affordance).
  - **Music:** if a sound exists, a small **playable audio chip** + the track title/author (play the
    audio if a media file exists; otherwise show title only).
  - **Provenance strip** + an **"Open original" link** to the live post (`url`, opens new tab).
  - **Graceful degradation:** non-enriched posts show thumbnail + stats + caption + link only.
- Quality floor (per skill): responsive to mobile, visible keyboard focus, reduced-motion respected.

## Definition of done
`GET /digest` + `GET /post/...` + `/media/...` working; the SPA renders Instagram-style cards and a
full-post lightbox with playable video/carousel/audio, spoiler reveal, stats degraded per platform,
and source links; at least the 3 existing bundles + the freshly-enriched top-N display real media.
README updates in `api/` + `web/`. Report your design plan (palette/type/layout/signature + what you
revised away from defaults), screenshots if possible, and how to run it.
