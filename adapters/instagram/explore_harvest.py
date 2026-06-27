"""Unattended Instagram Explore harvester — accumulates toward ≥500 posts.

Harvest loop (per docs/handoffs/robust-harvest.md):
  scroll → collect new items via response interception (discover/web/explore_grid/)
  → on stall (N scrolls, no new items):
      inspect DOM → dismiss BENIGN modal by stable selector → continue
      captcha/checkpoint → STOP, blocked=True (never solve/grind)
      exhaustion → reload + dedup, try again (up to MAX_RELOADS times)
  → persist harvested ids+rows across runs in PERSIST_FILE

Run unattended:
    cd /home/myrzaarslan/dev/kashgari/marketing-trends
    .venv/bin/python -m adapters.instagram.explore_harvest
or
    .venv/bin/python adapters/instagram/explore_harvest.py

Anti-ban discipline:
- REUSES persisted profile/session under profiles/ig-burner-main/
- Human-ish jittered scrolling; big pauses between reloads
- STOPS on any captcha/checkpoint — does NOT retry or solve
- Does NOT hydrate per-video view counts in bulk (ban bait)
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from instagrapi.extractors import extract_media_v1

from adapters.instagram.adapter import InstagramAdapter, _MEDIA_TYPE_TO_NORM
from core.harness import close_persona, human_pause, launch_persona

# ---------------------------------------------------------------------------
# Config (tune for pace vs. coverage)
# ---------------------------------------------------------------------------
PERSONA = "ig-burner-main"
GEO = "KZ"
TARGET = 500                # Stop when we've accumulated this many unique posts
MAX_SCROLLS_PER_LOAD = 50   # Scroll attempts per page-load before considering reload
STALL_AFTER = 6             # Consecutive no-new-items scrolls before inspecting DOM
MAX_RELOADS = 4             # Total page reloads before giving up
SCROLL_PAUSE_MIN = 2.2      # Human-paced pause between scrolls (seconds)
SCROLL_PAUSE_MAX = 4.5
RELOAD_PAUSE_MIN = 30.0     # Big rest between reloads (more human, less hot)
RELOAD_PAUSE_MAX = 55.0
SCROLL_PX_MIN = 4000        # Pixels per scroll — varies for human feel
SCROLL_PX_MAX = 8000

# Persistent state across runs
PERSIST_FILE = Path("secrets/explore_harvest.json")
# Bootstrap from the prior scratch run (110 posts)
LEGACY_ROWS_FILE = Path("secrets/explore_rows.json")
LEGACY_RAW_FILE = Path("secrets/explore_raw.json")


# ---------------------------------------------------------------------------
# Persistent state helpers
# ---------------------------------------------------------------------------

def _load_state() -> tuple[dict[str, Any], list[dict]]:
    """Load (raw_by_id, rows_list) from PERSIST_FILE, seeded from legacy files.

    Also normalizes any raw items not yet in rows — handles the case where the
    prior scratch run captured more raw items than it successfully normalized.
    """
    raw_by_id: dict[str, Any] = {}
    rows: list[dict] = []

    # 1. Bootstrap from legacy scratch files (one-time migration).
    # Load raw FIRST so we can normalize all of it.
    if LEGACY_RAW_FILE.exists():
        try:
            legacy_raws = json.loads(LEGACY_RAW_FILE.read_text())
            if isinstance(legacy_raws, list):
                for raw in legacy_raws:
                    pk = str(raw.get("pk") or raw.get("id") or "")
                    if pk:
                        raw_by_id[pk] = raw
        except Exception:
            pass

    if LEGACY_ROWS_FILE.exists():
        try:
            legacy_rows = json.loads(LEGACY_ROWS_FILE.read_text())
            seen_ids = set()
            for r in legacy_rows:
                if r.get("id") and r["id"] not in seen_ids:
                    rows.append(r)
                    seen_ids.add(r["id"])
        except Exception:
            pass

    # 2. Load our own persist file (overrides/extends legacy)
    if PERSIST_FILE.exists():
        try:
            state = json.loads(PERSIST_FILE.read_text())
            seen_ids = {r["id"] for r in rows}
            for r in state.get("rows", []):
                if r.get("id") and r["id"] not in seen_ids:
                    rows.append(r)
                    seen_ids.add(r["id"])
            for pk, raw in state.get("raw_by_id", {}).items():
                raw_by_id[pk] = raw
        except Exception:
            pass

    # 3. Normalize any raw items not yet in rows — handles prior runs that
    #    captured raw but failed extract_media_v1 at normalization time.
    row_ids = {r["id"] for r in rows}
    extra_normalized = 0
    for pk, raw in list(raw_by_id.items()):
        if pk not in row_ids:
            row = _normalize_row(pk, raw)
            if row:
                rows.append(row)
                row_ids.add(pk)
                extra_normalized += 1

    if extra_normalized:
        print(f"[state] normalized {extra_normalized} extra raw items from prior run")
    print(f"[state] loaded {len(rows)} rows, {len(raw_by_id)} raw items from prior runs")
    return raw_by_id, rows


def _save_state(raw_by_id: dict[str, Any], rows: list[dict]) -> None:
    """Persist current accumulated state to PERSIST_FILE."""
    PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {"rows": rows, "raw_by_id": raw_by_id, "_count": len(rows)}
    PERSIST_FILE.write_text(json.dumps(state, ensure_ascii=False))
    print(f"[state] saved {len(rows)} rows → {PERSIST_FILE}")


# ---------------------------------------------------------------------------
# Response interception — collect raw media objects from any IG API response
# ---------------------------------------------------------------------------

def _collect_media(obj: Any, raw_by_id: dict[str, Any]) -> int:
    """Walk a JSON value recursively; extract any media-shaped dicts by pk.

    Returns the number of NEW items added.
    """
    added = 0
    if isinstance(obj, dict):
        pk = obj.get("pk") or obj.get("id")
        if pk and "media_type" in obj and ("taken_at" in obj or "code" in obj):
            pk_str = str(pk)
            if pk_str not in raw_by_id:
                raw_by_id[pk_str] = obj
                added += 1
        for v in obj.values():
            added += _collect_media(v, raw_by_id)
    elif isinstance(obj, list):
        for v in obj:
            added += _collect_media(v, raw_by_id)
    return added


def _make_on_response(raw_by_id: dict[str, Any], counters: dict) -> Any:
    """Return a Playwright response listener that collects media from IG API responses."""
    def on_response(resp):
        url = resp.url
        # Only intercept IG API and graphql endpoints
        if "/api/v1/" not in url and "/graphql" not in url:
            return
        ct = (resp.headers or {}).get("content-type", "")
        if "json" not in ct:
            return
        try:
            data = resp.json()
        except Exception:
            return
        added = _collect_media(data, raw_by_id)
        counters["new_since_scroll"] += added
        counters["new_since_reload"] += added
    return on_response


# ---------------------------------------------------------------------------
# Modal dismissal — STABLE SELECTOR only, never coordinates
# ---------------------------------------------------------------------------

# Each entry: (description, locator_spec, click_which)
# locator_spec: a CSS/text/role selector that uniquely identifies the dismiss target
_DISMISS_CANDIDATES = [
    # Cookie / consent banners (appear before or during session)
    ("cookie-allow-all",       'button:has-text("Allow all cookies")',             "first"),
    ("cookie-allow-all-ru",    'button:has-text("Разрешить все cookie")',           "first"),
    ("cookie-essential-only",  'button:has-text("Only allow essential cookies")',   "first"),
    ("cookie-essential-ru",    'button:has-text("Разрешить только необходимые")',   "first"),
    # "Save login info" prompt (post-login)
    ("save-info-notnow",       'button:has-text("Not Now")',                        "first"),
    ("save-info-notnow-ru",    'button:has-text("Не сейчас")',                      "first"),
    # App install / "Open in app" banner
    ("app-banner-close",       '[aria-label="Close"]',                              "first"),
    ("app-banner-notnow",      'button:has-text("Not now")',                        "first"),
    # Notification permission dialog
    ("notif-notnow",           'button:has-text("Not Now")',                        "first"),
    # Login / signup nudge (soft-login wall — the feed is behind a "Log in" overlay)
    # Don't click "Log in" — click the "×" or "Not Now" instead
    ("login-nudge-close",      '[aria-label="Close"]',                              "first"),
    ("login-nudge-notnow",     'button:has-text("Not Now")',                        "first"),
    # "See more" content gate (sometimes appears mid-scroll)
    ("see-more",               'button:has-text("See more")',                       "first"),
    ("see-more-ru",            'button:has-text("Ещё")',                            "first"),
    # "Add to home screen" / PWA install
    ("pwa-dismiss",            'button:has-text("Not Now")',                        "first"),
    # Post-login "Who to follow" / "Explore more" interstitial
    ("follow-dismiss",         'button:has-text("Skip")',                           "first"),
    ("follow-dismiss-ru",      'button:has-text("Пропустить")',                     "first"),
]

# Captcha / checkpoint signals → STOP immediately (never solve)
_CAPTCHA_URL_MARKERS = ("checkpoint", "challenge", "captcha", "suspended", "disabled")
_CAPTCHA_DOM_TEXTS = [
    "Передвиньте ползунок",      # slider CAPTCHA (Russian)
    "Verify you're human",
    "verify you're human",
    "Complete the security check",
    "complete the security check",
    "Your account has been suspended",
    "We suspended your account",
    "We've detected unusual activity",
    "Suspicious login attempt",
    # Suspicious-activity challenge (should stop; these could escalate)
    "This Wasn't Me",
    "Secure Your Account",
]

# Session-expired / login-wall markers → stop and report (don't re-login in loop)
_LOGIN_WALL_URL_MARKERS = ("/accounts/login/", "/login/", "instagram.com/login")


def _is_captcha_page(page) -> str | None:
    """Return a description if we're on a captcha/checkpoint or login-wall page.

    Login wall (session expired) is treated the same as captcha: stop, don't grind.
    """
    url = page.url
    for marker in _LOGIN_WALL_URL_MARKERS:
        if marker in url:
            return f"Session expired or not logged in — redirected to login: {url[:100]}"
    for marker in _CAPTCHA_URL_MARKERS:
        if marker in url:
            return f"URL contains '{marker}': {url[:100]}"
    for text in _CAPTCHA_DOM_TEXTS:
        try:
            loc = page.locator(f'text="{text}"')
            if loc.count() > 0:
                return f"DOM text '{text}' visible"
        except Exception:
            pass
    return None


def _try_dismiss_modals(page) -> list[str]:
    """Try all known modal dismissals; return list of what was dismissed."""
    dismissed = []
    for name, selector, which in _DISMISS_CANDIDATES:
        try:
            loc = page.locator(selector)
            n = loc.count()
            if n > 0:
                target = loc.first if which == "first" else loc.last
                if target.is_visible(timeout=500):
                    target.click()
                    human_pause(0.8, 1.8)
                    dismissed.append(name)
        except Exception:
            pass
    return dismissed


# ---------------------------------------------------------------------------
# Row normalization (same logic as scratch_explore_harvest.py)
# ---------------------------------------------------------------------------

def _normalize_row(pk: str, raw: dict[str, Any]) -> dict | None:
    """Turn a raw API media dict into a lightweight harvest row.

    Fields added vs. the bare harvest row:
    - ``posted_at`` — ISO-8601 UTC string from ``raw["taken_at"]`` (unix ts).
      Present on every raw item (both rich and filler).
    - ``status`` — ``"ok"`` for rich items that carry full stats (like_count,
      comment_count, user) in the explore payload; ``"needs_hydration"`` for
      carousel-child grid fillers whose stats are absent from the explore
      intercept and require a per-post API call (Layer-3, out of scope here).
    - ``author_follower_count`` — always ``None`` for explore-harvested items;
      the explore payload does not include the author's follower count.
      Needs a per-author hydration call.
    - ``share_count`` / ``save_count`` — always ``None``; Instagram never exposes
      them publicly (per docs/SIGNALS.md).
    """
    # --- classify: rich (full stats in payload) vs filler (carousel child, stat-light)
    # Carousel children intercepted as grid pivots carry no like_count, no
    # comment_count, and no user dict.  Every other explore item (standalone
    # image, reel, carousel parent) has at minimum like_count + comment_count.
    is_stat_light = (
        raw.get("like_count") is None
        and raw.get("comment_count") is None
        and not isinstance(raw.get("user"), dict)
    )
    status = "needs_hydration" if is_stat_light else "ok"

    # posted_at from unix timestamp — present on every raw item
    taken_at_ts = raw.get("taken_at")
    posted_at = (
        datetime.fromtimestamp(taken_at_ts, tz=timezone.utc).isoformat()
        if isinstance(taken_at_ts, (int, float))
        else None
    )

    try:
        m = extract_media_v1(dict(raw))
    except Exception:
        # Fallback: direct from raw.  Always used for filler/carousel-child
        # items since extract_media_v1 chokes on their stripped shape.
        code = raw.get("code")
        mt = _MEDIA_TYPE_TO_NORM.get(int(raw.get("media_type") or 0), "image")
        sid, sname = InstagramAdapter._extract_audio(raw)
        caption_obj = raw.get("caption")
        caption_text = caption_obj.get("text") if isinstance(caption_obj, dict) else None
        return {
            "id": pk,
            "status": status,
            "author": (raw.get("user") or {}).get("username"),
            "media_type": mt,
            "is_reel": bool(raw.get("clips_metadata")),
            "posted_at": posted_at,
            "like_count": raw.get("like_count"),
            "comment_count": raw.get("comment_count"),
            "view_count": raw.get("play_count") or raw.get("ig_play_count") or raw.get("view_count"),
            "caption": (caption_text or "")[:80] if caption_text else None,
            "hashtags": InstagramAdapter._hashtags(caption_text),
            "sound_id": sid,
            "sound_name": sname,
            "url": f"https://www.instagram.com/p/{code}/" if code else None,
            "author_follower_count": None,  # not in explore payload; needs Layer-3 hydration
            "share_count": None,            # never public on Instagram
            "save_count": None,             # never public on Instagram
            "persona_id": PERSONA,
            "geo_tier": GEO,
        }

    mt = _MEDIA_TYPE_TO_NORM.get(int(m.media_type or 0), "image")
    sid, sname = InstagramAdapter._extract_audio(raw)
    return {
        "id": str(m.pk),
        "status": status,
        "author": m.user.username if m.user else (raw.get("user") or {}).get("username"),
        "media_type": mt,
        "is_reel": (m.product_type == "clips") or bool(raw.get("clips_metadata")),
        "posted_at": posted_at,
        "like_count": m.like_count,
        "comment_count": m.comment_count,
        "view_count": raw.get("play_count") or raw.get("ig_play_count") or raw.get("view_count"),
        "caption": (m.caption_text or "")[:80] if m.caption_text else None,
        "hashtags": InstagramAdapter._hashtags(m.caption_text),
        "sound_id": sid,
        "sound_name": sname,
        "url": f"https://www.instagram.com/p/{m.code}/" if m.code else None,
        "author_follower_count": None,  # not in explore payload; needs Layer-3 hydration
        "share_count": None,            # never public on Instagram
        "save_count": None,             # never public on Instagram
        "persona_id": PERSONA,
        "geo_tier": GEO,
    }


# ---------------------------------------------------------------------------
# Main harvest function
# ---------------------------------------------------------------------------

def harvest_explore(
    target: int = TARGET,
    max_scrolls_per_load: int = MAX_SCROLLS_PER_LOAD,
    stall_after: int = STALL_AFTER,
    max_reloads: int = MAX_RELOADS,
    headless: bool = True,
    on_new_item=None,
    target_reached=None,
) -> dict:
    """Run an unattended Explore harvest and return a summary dict.

    Accumulates from persisted state; saves on exit.

    Hooks (used by the live-ingest wiring in core.ingest):
    - ``on_new_item(pk, raw)`` is called for each newly-intercepted raw media
      item, letting the caller filter (e.g. drop celebrities), normalize, and
      upsert it into the DB on the fly.
    - ``target_reached()`` overrides the default stop condition (accumulated rows
      >= target). The caller returns True once it has collected enough *kept*
      posts, so celebrity-skips don't count toward the goal.
    """
    def _reached() -> bool:
        if target_reached is not None:
            return bool(target_reached())
        return len(known_ids) >= target
    print(f"\n{'='*60}")
    print(f"Instagram Explore harvest — target={target} max_reloads={max_reloads}")
    print(f"{'='*60}")

    # Load accumulated state from prior runs
    raw_by_id, rows = _load_state()
    known_ids: set[str] = {r["id"] for r in rows}
    run_new = 0           # new items this run
    blocked = False
    blocked_reason = ""
    blockers_hit: list[str] = []

    def _drain() -> int:
        """Normalize any newly-intercepted raw items into rows; notify the caller.

        Returns the count of new items added this call. Shared by the initial-load
        and per-scroll collection points so the on_new_item hook fires uniformly.
        """
        nonlocal run_new
        added = 0
        for pk, raw in list(raw_by_id.items()):
            if pk in known_ids:
                continue
            row = _normalize_row(pk, raw)
            if not row:
                continue
            rows.append(row)
            known_ids.add(pk)
            added += 1
            run_new += 1
            if on_new_item is not None:
                try:
                    on_new_item(pk, raw)
                except Exception as cb_exc:  # a bad item must not sink the harvest
                    print(f"[harvest] on_new_item error for {pk}: {cb_exc}")
        return added

    ctx = launch_persona(PERSONA, headless=headless)
    page = ctx.new_page()

    try:
        for reload_num in range(max_reloads + 1):
            if _reached():
                print(f"[harvest] target reached — stopping")
                break

            # Counters reset per reload
            counters = {"new_since_scroll": 0, "new_since_reload": 0}

            # Attach response listener
            on_response = _make_on_response(raw_by_id, counters)
            page.on("response", on_response)

            print(f"\n[reload {reload_num}] navigating to /explore/ (total so far: {len(known_ids)})")
            try:
                page.goto(
                    "https://www.instagram.com/explore/",
                    wait_until="domcontentloaded",
                    timeout=40_000,
                )
            except Exception as e:
                print(f"[reload {reload_num}] navigation error: {e}")
                break

            # Initial settle
            human_pause(3.0, 5.0)

            # Check for captcha/checkpoint immediately after load
            cap = _is_captcha_page(page)
            if cap:
                blocked = True
                blocked_reason = f"captcha/checkpoint on load (reload {reload_num}): {cap}"
                blockers_hit.append(blocked_reason)
                print(f"[STOP] {blocked_reason}")
                break

            # Dismiss any modals on page load
            dismissed = _try_dismiss_modals(page)
            if dismissed:
                print(f"[modal] dismissed on load: {dismissed}")
                blockers_hit.extend(f"modal:{d}" for d in dismissed)
                human_pause(1.0, 2.0)

            # Collect what loaded immediately
            # (response listener fires during navigation)
            new_after_nav = _drain()
            if new_after_nav:
                print(f"[reload {reload_num}] {new_after_nav} items on initial load → total={len(known_ids)}")

            # Scroll loop
            stall_count = 0
            for scroll_num in range(max_scrolls_per_load):
                if _reached():
                    break

                counters["new_since_scroll"] = 0
                px = random.randint(SCROLL_PX_MIN, SCROLL_PX_MAX)
                page.mouse.wheel(0, px)
                human_pause(SCROLL_PAUSE_MIN, SCROLL_PAUSE_MAX)

                # Collect any new items that arrived
                batch_new = _drain()

                if batch_new:
                    stall_count = 0
                    if scroll_num % 5 == 0 or batch_new > 0:
                        print(f"  [scroll {scroll_num:02d}] +{batch_new} new → total={len(known_ids)}")
                else:
                    stall_count += 1

                # Check for captcha after every scroll
                cap = _is_captcha_page(page)
                if cap:
                    blocked = True
                    blocked_reason = f"captcha/checkpoint during scroll {scroll_num}: {cap}"
                    blockers_hit.append(blocked_reason)
                    print(f"[STOP] {blocked_reason}")
                    break

                if stall_count >= stall_after:
                    print(f"  [stall] {stall_count} scrolls with no new items — inspecting DOM")

                    # Try to dismiss any blocking modal
                    dismissed = _try_dismiss_modals(page)
                    if dismissed:
                        print(f"  [modal] dismissed during stall: {dismissed}")
                        blockers_hit.extend(f"modal:{d}" for d in dismissed)
                        stall_count = 0  # reset stall; something changed
                        human_pause(1.5, 3.0)
                        continue

                    # Re-check for captcha
                    cap = _is_captcha_page(page)
                    if cap:
                        blocked = True
                        blocked_reason = f"captcha/checkpoint detected in stall: {cap}"
                        blockers_hit.append(blocked_reason)
                        print(f"[STOP] {blocked_reason}")
                        break

                    # Genuine feed exhaustion — break scroll, try reload
                    print(f"  [exhaustion] feed dry on reload {reload_num} after {scroll_num+1} scrolls")
                    break

            if blocked:
                break

            # Detach this reload's response listener (avoid double-counting on next reload)
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass

            if _reached():
                break

            if reload_num < max_reloads:
                rest = random.uniform(RELOAD_PAUSE_MIN, RELOAD_PAUSE_MAX)
                print(f"\n[reload {reload_num}] exhausted; resting {rest:.0f}s before reload {reload_num+1}...")
                time.sleep(rest)

    finally:
        try:
            close_persona(ctx)
        except Exception:
            pass
        _save_state(raw_by_id, rows)

    # Summary
    from collections import Counter
    by_type = Counter(r["media_type"] for r in rows)
    reels = sum(1 for r in rows if r.get("is_reel"))
    summary = {
        "total_accumulated": len(rows),
        "new_this_run": run_new,
        "blocked": blocked,
        "blocked_reason": blocked_reason or None,
        "blockers_hit": blockers_hit,
        "by_type": dict(by_type),
        "reels": reels,
    }
    print(f"\n{'='*60}")
    print(f"DONE: total={len(rows)} | new_this_run={run_new} | blocked={blocked}")
    print(f"      by_type={dict(by_type)} reels={reels}")
    if blocked_reason:
        print(f"      BLOCKED: {blocked_reason}")
    if blockers_hit:
        print(f"      blockers_hit: {blockers_hit}")
    print(f"{'='*60}")
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Allow overriding target from argv: python explore_harvest.py 300
    target = int(sys.argv[1]) if len(sys.argv) > 1 else TARGET
    headless_flag = "--headed" not in sys.argv  # --headed shows browser window
    result = harvest_explore(target=target, headless=headless_flag)
    sys.exit(0)
