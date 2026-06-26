"""One-shot re-normalization of secrets/explore_harvest.json.

Re-normalizes every item in raw_by_id through the fixed _normalize_row so
persisted rows pick up the fields that were missing in prior runs:
  - posted_at      (from raw["taken_at"], present on all 589 items)
  - status         ("ok" for rich items | "needs_hydration" for carousel fillers)
  - author_follower_count (always None from explore; marks Layer-3 need)
  - share_count / save_count (always None; IG never exposes them)

Prints a coverage tally split by group, then saves the updated state.

Run:
    .venv/bin/python -m adapters.instagram.renormalize_explore
or
    .venv/bin/python adapters/instagram/renormalize_explore.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from adapters.instagram.explore_harvest import PERSIST_FILE, _normalize_row

# ---------------------------------------------------------------------------
# Load raw state
# ---------------------------------------------------------------------------

state = json.loads(PERSIST_FILE.read_text())
raw_by_id: dict = state.get("raw_by_id", {})
print(f"Loaded {len(raw_by_id)} raw items from {PERSIST_FILE}")

# ---------------------------------------------------------------------------
# Re-normalize ALL raw items (full replacement — not incremental)
# ---------------------------------------------------------------------------

rows: list[dict] = []
skipped = 0
for pk, raw in raw_by_id.items():
    row = _normalize_row(pk, raw)
    if row:
        rows.append(row)
    else:
        skipped += 1

print(f"Re-normalized: {len(rows)} rows  (skipped {skipped} items with no usable pk)")

# ---------------------------------------------------------------------------
# Split into rich vs filler for the tally
# ---------------------------------------------------------------------------

rich_rows   = [r for r in rows if r.get("status") == "ok"]
filler_rows = [r for r in rows if r.get("status") == "needs_hydration"]
other_rows  = [r for r in rows if r.get("status") not in ("ok", "needs_hydration")]

print(f"\nClassification: rich(ok)={len(rich_rows)}  filler(needs_hydration)={len(filler_rows)}  other={len(other_rows)}")

# ---------------------------------------------------------------------------
# Coverage tally helper
# ---------------------------------------------------------------------------

FIELDS = [
    "id", "status", "author", "media_type", "is_reel",
    "posted_at",
    "like_count", "comment_count", "view_count",
    "caption", "hashtags",
    "sound_id", "sound_name",
    "url",
    "author_follower_count",
    "share_count", "save_count",
]


def tally(label: str, group: list[dict]) -> None:
    total = len(group)
    if total == 0:
        print(f"\n{label}: (empty)")
        return
    print(f"\n{'─'*60}")
    print(f"{label}  (n={total})")
    print(f"{'─'*60}")
    print(f"  {'field':<26}  {'populated':>10}  {'total':>6}  {'%':>6}")
    print(f"  {'─'*26}  {'─'*10}  {'─'*6}  {'─'*6}")
    for field in FIELDS:
        populated = sum(
            1 for r in group
            if r.get(field) is not None and r.get(field) != [] and r.get(field) != ""
        )
        pct = 100 * populated // total if total else 0
        flag = ""
        if field in ("author_follower_count", "share_count", "save_count"):
            flag = "  [always None — IG doesn't expose]"
        elif field == "view_count" and label.startswith("filler"):
            flag = "  [filler: no stats in payload]"
        print(f"  {field:<26}  {populated:>10}  {total:>6}  {pct:>5}%{flag}")


tally("rich items  (status=ok, had stats in payload)", rich_rows)
tally("grid-filler (status=needs_hydration, carousel children)", filler_rows)

# ---------------------------------------------------------------------------
# Media-type breakdown
# ---------------------------------------------------------------------------

print(f"\n{'─'*60}")
print("Media-type breakdown")
print(f"{'─'*60}")
for label, group in [("rich", rich_rows), ("filler", filler_rows)]:
    ct = Counter(r.get("media_type") for r in group)
    reels = sum(1 for r in group if r.get("is_reel"))
    print(f"  {label}: {dict(ct)}  reels={reels}")

# ---------------------------------------------------------------------------
# Save updated state
# ---------------------------------------------------------------------------

updated_state = {
    "rows": rows,
    "raw_by_id": raw_by_id,
    "_count": len(rows),
    "_rich": len(rich_rows),
    "_needs_hydration": len(filler_rows),
}
PERSIST_FILE.write_text(json.dumps(updated_state, ensure_ascii=False))
print(f"\nSaved {len(rows)} rows → {PERSIST_FILE}")
print(f"  {len(rich_rows)} ok (full stats)  +  {len(filler_rows)} needs_hydration")
