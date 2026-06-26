# web/ — Trend Intelligence SPA ("specimen viewer")

Vite + React + TypeScript SPA that consumes `api/` to display the ranked social digest **and** the
full **Content Bundle** of each top-ranked post. Built as an internal trend-intelligence console for
an EdTech marketing team (Kazakhstan / CIS + world) reverse-engineering viral posts — not a consumer
Instagram clone. The hero is the *specimen*: media front-and-centre, engagement treated as data.

## Design language

- **Palette**: graphite/ink surfaces (`#0C0C0D` → `#1C1C1E`) with a single warm **amber** data-accent
  (`#D4882A`). Deliberately *not* the cream+serif+terracotta, neon-on-black, or hairline-broadsheet
  defaults.
- **Type**: `Inter` for prose, `JetBrains Mono` for all data (rank chips, engagement counts, scores,
  IDs, timestamps) — numbers read like instrument readouts.
- **Signature**: every card wears a monospace amber **rank chip** (`#001`, `#002`, …); each post is a
  catalogued specimen. The lightbox closes with a **provenance strip** (platform · geo · posted-vs-
  first-seen · followers) + an **Open original** link.

## Dev (requires the FastAPI server running on port 8001)

```bash
# Terminal 1 — FastAPI
cd /path/to/marketing-trends
.venv/bin/uvicorn api.main:app --port 8001 --reload

# Terminal 2 — React dev server (proxies /digest, /refresh to :8001)
cd web
npm run dev
# → http://localhost:5173
```

## Build for production

```bash
npm run build
# Output: web/dist/
```

Serve `dist/` from any static host (Nginx, Caddy, etc.). Point it at the FastAPI server via
`VITE_API_URL=https://api.example.com` env var at build time, or a reverse-proxy rule at runtime.

## Features

- **Filter bar**: Platform / Geo / Period / Sort / Limit dropdowns
- **Sort grayed out**: Unavailable sorts (e.g. `save_rate` on Instagram, `velocity` without ≥3 snapshot days) are disabled in the dropdown — the API explains why
- **Specimen card grid**: rank chip, thumbnail (cover/first slide), media-type marker, content-bundle
  marker, platform-honest stats, mono score
- **Full-post lightbox** (click any card with a Content Bundle):
  - Inline `<video controls>` for video; **navigable carousel** (`‹ / ›` + `n / total`) for multi-image;
    single image; **text-as-hero** typographic treatment for X/Threads text posts
  - **Stats shown only where the platform exposes them** (SIGNALS.md) — no saves/shares on IG, no
    views off TikTok; a missing signal is omitted, **never rendered as a fake `0`**
  - Caption + hashtags
  - **Spoiler**: content-warning posts render behind a **frosted blur the user clicks to reveal**
  - **Music chip**: track title + author; plays inline if an audio file was downloaded, else title-only
  - **Provenance strip** + **Open original** (new tab)
  - **Graceful degradation**: non-enriched posts fall back to thumbnail + stats + caption + link
- **Quality floor**: responsive to mobile (single-column grid + bottom-sheet lightbox), visible
  keyboard focus (`:focus-visible`), `prefers-reduced-motion` honoured
- **Refresh button**: Triggers `POST /refresh` and polls for completion

## Sort labels

| Key | When grayed out |
|-----|-----------------|
| `engagement_rate` | Never (default) |
| `raw_counts` | Never |
| `share_rate` | Instagram; no-view platforms |
| `save_rate` | Non-TikTok platforms |
| `velocity` | < 3 distinct snapshot days |
| `relative_baseline` | < 3 distinct snapshot days |
| `cross_persona` | < 3 distinct snapshot days |
