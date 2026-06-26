# Fix handoff — the 3 Layer-3 escalations

You are a SONNET agent. Resolve the three escalations the Layer-3 build raised. **Obey the project's
anti-ban discipline at all times** (ADR-0001, `docs/handoffs/robust-harvest.md`): gentle jittered
pacing, and on any captcha / 429 / login-wall → **back off, never grind**. Escalate genuine design
forks back to the Opus orchestrator.

## #1 — Threads `raw={}` (data-quality contract violation) — PRIMARY
- **Diagnosis (already done by Opus):** the Threads *code* is correct — `adapters/threads/adapter.py`
  `_normalize_post` sets `raw=raw`, and `harvest_accumulate.py` `_post_to_row` writes `"raw": post.raw`.
  The stored `data/threads_harvest_scratch.json` nonetheless has `raw={}` for **all 308** rows — it's
  **stale data** from an earlier harvest that predated the `raw` wiring.
- **Do:**
  1. **Reproduce:** fetch 1–2 live Threads accounts via `ThreadsAdapter` and confirm the current code
     produces a NON-empty `raw` per post. (If it does NOT, you've found a real live bug — fix it.)
  2. **Add a guard** in `harvest_accumulate.py` so a row with empty/falsy `raw` is **never silently
     persisted** — skip it + log a loud warning (and a one-line test). This is the durable fix that
     stops a recurrence. INGESTION-CONTRACT mandates the full `raw`.
  3. **Re-harvest** to repopulate the scratch with real `raw` (respect pacing; partial is fine — the
     harvester is idempotent/crash-safe). It's acceptable to delete the stale `data/threads_harvest_scratch.json`
     first so old raw-less rows don't linger, OR re-fetch over them.
  4. **Verify the spoiler/CW field** against the real `@quokkaredfield` fixture post: confirm where the
     content-warning / hidden text lives in the live `raw` and that `enrichment/field_maps.py` reads it
     correctly (the code currently assumes `is_content_warning` / `post_text_hidden_content_type`).

## #2 — TikTok imagePost URL path (enrichment)
- `enrichment/field_maps.py` reads `imagePost.images[*].imageURL.urlList[0]` per the documented shape,
  but no image/carousel TikTok post existed in the accumulator to verify it.
- **Do:** find a photo-mode TikTok post (or capture one via the existing TikTok discovery/adapter path)
  and confirm the image URLs extract + download. If you can't source one safely, add a **defensive unit
  test** against a saved fixture of the documented `imagePost` shape and document the residual unknown.

## #3 — TikTok direct-GET → yt-dlp fallback (enrichment)
- The direct CDN GET works while `downloadAddr` URLs are fresh; the yt-dlp fallback is unverified.
- **Do:** DON'T wait for natural expiry — **simulate it**: force the direct GET to fail (e.g. corrupt/
  expire the URL or stub the downloader to raise) and confirm `enrichment/downloader.py` cleanly falls
  back to yt-dlp and still produces the video file. Add a test for the fallback trigger.

## Definition of done
Threads scratch re-populated with real `raw` (+ the empty-raw guard + test); spoiler field verified or
its residual unknown documented; TikTok imagePost verified or covered by a defensive test; yt-dlp
fallback proven via a simulated direct-GET failure. Report what you found, what you ran, and any
escalations. Touch only `adapters/threads/`, `enrichment/`, and tests — do not modify `core/`, `api/`,
`web/`, or `core/harness/`.
