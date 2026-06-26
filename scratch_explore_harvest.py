"""Harvest the logged-in, personalized Explore feed via response interception.

Drives the logged-in persona browser through /explore/, intercepts the
`discover/web/explore_grid/` JSON as it scrolls, normalizes each media object the
same way the adapter does, and tags every post with the persona + geo (Discovery
candidates per CONTEXT.md). Saves the COMPLETE raw payloads too.

NOTE: bulk pass does NOT hydrate per-video view counts (that'd be one call per
post = ban bait). Views come through only where the feed inlines them; hydrate the
top candidates afterward via the adapter if needed.
"""
import json
from collections import Counter

from instagrapi.extractors import extract_media_v1

from adapters.instagram.adapter import (
    _MEDIA_TYPE_TO_NORM,
    InstagramAdapter,
)
from core.harness import launch_persona, close_persona, human_pause

PERSONA = "ig-burner-main"
GEO = "KZ"
RAW_OUT = "secrets/explore_raw.json"
ROWS_OUT = "secrets/explore_rows.json"

raw_media = {}  # pk -> raw media dict (dedup)


def collect(obj):
    if isinstance(obj, dict):
        if "pk" in obj and "media_type" in obj and ("taken_at" in obj or "code" in obj):
            raw_media[str(obj["pk"])] = obj
        for v in obj.values():
            collect(v)
    elif isinstance(obj, list):
        for v in obj:
            collect(v)


def on_response(resp):
    url = resp.url
    if "/api/v1/" not in url and "/graphql" not in url:
        return
    if "json" not in (resp.headers or {}).get("content-type", ""):
        return
    try:
        collect(resp.json())
    except Exception:
        pass


ctx = launch_persona(PERSONA, headless=True)
page = ctx.new_page()
page.on("response", on_response)

page.goto("https://www.instagram.com/explore/", wait_until="domcontentloaded", timeout=40000)
for _ in range(10):  # scroll to load many grid pages
    human_pause(2.5, 4.0)
    page.mouse.wheel(0, 7000)
human_pause(2.5, 3.5)
close_persona(ctx)

print(f"captured {len(raw_media)} unique media from Explore")

rows = []
for pk, raw in raw_media.items():
    try:
        m = extract_media_v1(dict(raw))
    except Exception:
        continue
    mt = _MEDIA_TYPE_TO_NORM.get(int(m.media_type or 0), "image")
    sid, sname = InstagramAdapter._extract_audio(raw)
    rows.append({
        "id": str(m.pk),
        "author": m.user.username if m.user else raw.get("user", {}).get("username"),
        "media_type": mt,
        "is_reel": (m.product_type == "clips"),
        "likes": m.like_count,
        "comments": m.comment_count,
        "views_inline": raw.get("play_count") or raw.get("ig_play_count") or raw.get("view_count"),
        "caption": (m.caption_text or "")[:80],
        "hashtags": InstagramAdapter._hashtags(m.caption_text),
        "sound_id": sid,
        "sound_name": sname,
        "url": f"https://www.instagram.com/p/{m.code}/" if m.code else None,
        "persona_id": PERSONA,
        "geo_tier": GEO,
    })

json.dump(list(raw_media.values()), open(RAW_OUT, "w"), ensure_ascii=False)
json.dump(rows, open(ROWS_OUT, "w"), ensure_ascii=False, indent=1)

by_type = Counter(r["media_type"] for r in rows)
reels = sum(1 for r in rows if r["is_reel"])
authors = Counter(r["author"] for r in rows if r["author"])
print(f"normalized {len(rows)} posts | by_type={dict(by_type)} reels={reels}")
print(f"distinct authors (Discovery candidates): {len(authors)}")
print("\ntop 10 by likes:")
for r in sorted(rows, key=lambda x: x["likes"] or 0, reverse=True)[:10]:
    tag = "reel" if r["is_reel"] else r["media_type"]
    print(f"  @{str(r['author'])[:20]:20} {tag:7} likes={ (r['likes'] or 0):>8} "
          f"comments={(r['comments'] or 0):>6} views={r['views_inline']} {r['hashtags'][:3]}")
print("\nmost-seen authors:")
for a, c in authors.most_common(8):
    print(f"  @{a}: {c}")
print(f"\nsaved raw -> {RAW_OUT}, rows -> {ROWS_OUT}")
