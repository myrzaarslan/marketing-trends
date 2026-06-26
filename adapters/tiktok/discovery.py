"""TikTok For-You harvest — the logged-out discovery spine.

Layer-2 discovery (docs/DISCOVERY.md): instead of pulling named accounts, we let
TikTok's OWN algorithm hand us its stream and harvest it. The web For-You feed is
partially viewable LOGGED OUT, so there is **no account to ban** — only IP-level
throttling. We drive a headed Chromium with stealth, let the page sign and fire
its internal ``/api/recommend/item_list/`` XHR, and intercept that JSON (never the
DOM). The adapter normalizes the items with the same ``_record_from_api`` mapper
used for the signed account-posts path — they share the item shape.

VERIFIED 2026-06-25 from a KZ home IP (Astana / Kcell residential):
  * Logged-out For-You works; the page loads in RU ("Смотрите трендовые видео"),
    so the feed is naturally KZ/CIS region-shaped without any login.
  * The feed advances by **ArrowDown / swipe**, NOT page scroll — ``mouse.wheel``
    is a no-op on the full-screen player (an earlier version of this module used
    it and wrongly concluded the feed "caps at ~10"; it never actually paginated).
    Each ArrowDown that nears the end of the loaded set triggers a fresh
    ``item_list`` XHR.
  * Every ~6–14 advances an **interest-picker modal** ("Что вы хотели бы
    посмотреть в TikTok?") appears and BLOCKS further advancing. We dismiss it via
    its top-right ✕ — the single ``svg`` inside the ``DivInterestSelectorContainer``
    panel, located by class fragment + clicked live (no hardcoded coordinates,
    so it survives different window/screen sizes). NOTE: that same modal offers a
    "Наука и образование" (Science & Education) interest, so logged-out education
    shaping IS possible — we just close the modal here (simpler/robust); selecting
    the interest is a documented future option, not done.
  * Per-session yield is ~15–25 unique videos (noisy); we RELOAD the feed for more
    fresh batches and dedupe by id across loads. Deep volume needs a session.
  * Logged-out hashtag/search item_lists come back EMPTY (gated), so seed-based
    discovery routes through the signed TikTok-Api path or a burner instead.
  * BOT-CHECK CEILING (seen 2026-06-26): pushed too hard, TikTok serves a slider
    puzzle captcha ("Передвиньте ползунок, чтобы совместить пазл"). We DETECT it
    (``_BLOCK_MARKERS``) and BACK OFF — never try to solve it (arms race) and never
    grind. Two things tripped it: (a) on the Explore grid, a center "focus" click
    opens a video page (grid tiles are links) — fixed: we only focus-click the FYP
    player, never the grid; (b) volume/speed — fixed: slower jittered pacing. A
    captcha means the session/IP is hot: stop, return what we have, and wait /
    slow down / (at scale) rotate residential IPs — OPEN-QUESTIONS Q-3.

## Persistence (accumulate toward 500 across runs)

Pass ``accumulator_path`` to ``harvest()`` to load and save progress across runs.
The file is a JSON dict ``{version, total, last_updated, items: {id: raw_item}}``.
Re-running with the same path de-dupes by id and picks up where the last run left
off. On block/captcha the run stops immediately, preserves whatever was collected,
and saves it — the next operator re-run (after IP cooldown) continues from there.

## Cooldown mode

Pass ``cooldown=True`` when the IP may be warm (e.g. after a captcha on 2026-06-26).
This doubles per-advance delays (3–5.5 s vs 1.8–3.2 s), adds a longer post-load
wait, and caps ``max_advances`` at a gentler ceiling. The goal is to appear more
human and give TikTok's anti-bot signals time to decay.

## Modal taxonomy (obstacle #1: auto-dismiss benign, stop on captcha)

1. **Interest picker** — "Что вы хотели бы посмотреть?" — already handled;
   dismissed via ``[class*="InterestSelector"] svg`` click.
2. **Login/signup nudge** — "Войти в TikTok" / "Log in to TikTok" overlay — can
   appear after extensive scrolling. Dismissed via "Continue as guest" button text
   or the ✕ svg in a login panel, with Escape as last resort.
3. **Cookie / GDPR consent banner** — "We use cookies" / "Принять все cookies" —
   appears on first load from some IPs. Dismissed via "Decline" / "Reject all"
   button (preferred over Accept so we don't create a cookie session).
4. **App-install banner** — "Open in app" / "Get TikTok app" — non-blocking top
   banner; dismissed via its ✕ svg inside an app-banner container.
5. **Feed exhaustion wall** — "Log in to see more" — NOT a dismissible modal;
   signals that the logged-out feed is dry. Triggers a cycle reload (new URL load),
   or stops if all cycles are exhausted.
6. **Captcha / slider puzzle** — "Передвиньте ползунок" / "Drag the" — STOP,
   back off, ``blocked=True``. Never solve, never grind.

This module FETCHES only (returns raw item dicts). Normalization, ranking and the
PostRecord contract live in the adapter. No persistence, no virality judgment.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
from datetime import datetime, timezone
from typing import Optional

# Region shaping: the strongest lever we have logged-out is locale + timezone (the
# IP already geolocates to KZ). KZ/CIS browse as RU; World as US-English.
_LOCALE_BY_TIER = {
    "KZ": ("ru-RU", "Asia/Almaty"),
    "CIS": ("ru-RU", "Europe/Moscow"),
    "World": ("en-US", "America/New_York"),
}

# Harvestable surfaces. "explore" is the grid behind /explore — it scrolls with
# the wheel and yields ~55 items in ~10 scrolls (more, faster, and it carries
# `challenges[]`); preferred. "foryou" is the full-screen player, advanced with
# ArrowDown (the wheel is a no-op there), ~15-25/session. Both reuse the same
# item_list intercept, modal handling, and normalizer.
_SURFACES = {
    "explore": {"url": "https://www.tiktok.com/explore", "advance": "wheel"},
    "foryou": {"url": "https://www.tiktok.com/foryou", "advance": "arrow"},
}

# ---------------------------------------------------------------------------
# Modal #1: Interest picker ("Что вы хотели бы посмотреть в TikTok?")
# ---------------------------------------------------------------------------
_MODAL_TITLE_HINTS = ("хотели бы", "would you like", "посмотреть в tiktok")
_INTEREST_PANEL = '[class*="InterestSelector"]'

# ---------------------------------------------------------------------------
# Modal #2: Login / signup nudge ("Войти в TikTok")
# ---------------------------------------------------------------------------
# Text that appears in body when a login overlay is shown.
_LOGIN_HINTS = (
    "войти в tiktok", "log in to tiktok", "sign up for tiktok",
    "зарегистрируйтесь", "create an account", "войдите в tiktok",
)
# Preferred dismiss: "Continue as guest" / "Продолжить без входа" button text.
_LOGIN_GUEST_TEXTS = (
    "продолжить без", "continue as guest", "continue without",
    "не сейчас", "not now", "skip",
)
# CSS selectors for the login panel's ✕ button area (class-fragment, survives renames)
_LOGIN_PANEL_SELECTORS = (
    '[class*="LoginContainer"]',
    '[class*="LoginModal"]',
    '[class*="login-modal"]',
    '[role="dialog"]',
)

# ---------------------------------------------------------------------------
# Modal #3: Cookie / GDPR banner
# ---------------------------------------------------------------------------
_COOKIE_HINTS = (
    "we use cookies", "cookie policy", "принять все cookies",
    "использует файлы cookie", "cookie consent", "accept cookies",
)
_COOKIE_PANEL_SELECTORS = (
    '[class*="CookieBanner"]',
    '[class*="CookieConsent"]',
    '[id*="cookie"]',
    '[class*="cookie-banner"]',
)
_COOKIE_DECLINE_TEXTS = ("decline", "reject all", "отклонить", "не принимать", "отказаться")
_COOKIE_ACCEPT_TEXTS = ("accept all", "принять все", "accept cookies")

# ---------------------------------------------------------------------------
# Modal #4: App-install banner ("Get TikTok app")
# ---------------------------------------------------------------------------
_APP_HINTS = (
    "open in app", "get the app", "get tiktok",
    "скачать приложение", "открыть в приложении",
)
_APP_PANEL_SELECTORS = (
    '[class*="AppBanner"]',
    '[class*="DownloadModal"]',
    '[class*="AppDownload"]',
    '[class*="download-app"]',
    '[class*="open-app"]',
)

# ---------------------------------------------------------------------------
# Feed exhaustion wall (NOT a modal — triggers cycle reload)
# ---------------------------------------------------------------------------
_EXHAUSTION_HINTS = (
    "log in to see more", "войдите чтобы видеть больше",
    "sign up to see more", "create account to see more",
    "зарегистрируйтесь чтобы",
)

# ---------------------------------------------------------------------------
# Block / captcha markers — STOP and back off, never solve
# ---------------------------------------------------------------------------
_BLOCK_MARKERS = (
    "verify to continue", "captcha-verify", "security check",
    "передвиньте ползунок", "совместить пазл", "перетащите",
    "drag the", "slide to", "puzzle", "verify to keep",
)

# Give up a cycle after this many consecutive advances yield no new items
# AND no modal could be dismissed. Raised from 8→12 so spurious stalls get more
# time to resolve (a cookie or login banner may appear after a few stale steps).
_STALE_LIMIT = 12


class HarvestResult:
    """Items plus a short diagnostics note (was it throttled? blocked?)."""

    def __init__(
        self,
        items: list[dict],
        note: str,
        blocked: bool,
        total_accumulated: int = 0,
        new_this_run: int = 0,
    ) -> None:
        self.items = items
        self.note = note
        self.blocked = blocked
        # How many unique items are in the accumulator file (including this run).
        self.total_accumulated = total_accumulated
        # How many NET NEW items were added during this specific run.
        self.new_this_run = new_this_run


def harvest(
    geo_tier: str,
    *,
    surface: str = "explore",
    cycles: int = 3,
    max_advances: int = 40,
    headless: bool = False,
    target: Optional[int] = None,
    accumulator_path: Optional[str] = None,
    cooldown: bool = False,
) -> HarvestResult:
    """Sync entry point: harvest unique discovery item dicts. See module docstring.

    surface          — "explore" (default, grid, more items) or "foryou" (player).
    cycles           — how many times to (re)load the surface; each reload reshuffles.
    max_advances     — scroll/advance steps per cycle (the feed plateaus before this).
    headless         — MUST be False in practice; headless trips bot detection.
    target           — stop early once this many unique items are collected.
    accumulator_path — path to a JSON file that persists items across runs (dedup by id).
                       Created on first run; subsequent runs load it and extend it.
                       Re-running with the same path NEVER resets — always continues.
    cooldown         — True when the IP may be warm (captcha recently served).
                       Doubles per-advance delays and adds longer post-load waits.
    """
    if surface not in _SURFACES:
        raise ValueError(f"unknown surface {surface!r}; use one of {list(_SURFACES)}")
    return asyncio.run(
        _harvest_async(
            geo_tier,
            surface=surface,
            cycles=cycles,
            max_advances=max_advances,
            headless=headless,
            target=target,
            accumulator_path=accumulator_path,
            cooldown=cooldown,
        )
    )


def harvest_foryou(geo_tier: str, **kw) -> HarvestResult:
    """Back-compat alias: harvest the For-You surface specifically."""
    return harvest(geo_tier, surface="foryou", **kw)


def harvest_explore(geo_tier: str, **kw) -> HarvestResult:
    """Harvest the Explore grid (preferred — more items, faster)."""
    return harvest(geo_tier, surface="explore", **kw)


# ---------------------------------------------------------------------------
# Modal detection + dismissal helpers
# ---------------------------------------------------------------------------

async def _body_lower(page) -> str:
    try:
        return (await page.inner_text("body")).lower()
    except Exception:
        return ""


async def _modal_present(page) -> bool:
    body = await _body_lower(page)
    return any(hint in body for hint in _MODAL_TITLE_HINTS)


async def _dismiss_modal(page) -> bool:
    """Close the interest-picker via its ✕ (the lone svg in the panel).

    DOM-located, then clicked live — robust to window/screen size. Falls back to
    the icon's parent wrappers since an svg's click handler often sits on an
    ancestor.
    """
    icon = page.locator(f"{_INTEREST_PANEL} svg").first
    try:
        if await icon.count() == 0:
            return False
    except Exception:
        return False
    for target in (icon, icon.locator("xpath=.."), icon.locator("xpath=../..")):
        try:
            await target.click(timeout=2000)
            await page.wait_for_timeout(900)
            if not await _modal_present(page):
                return True
        except Exception:
            continue
    return False


async def _dismiss_login_wall(page) -> bool:
    """Dismiss a 'Log in to TikTok' overlay (modal #2).

    Strategy:
    1. Click the "Continue as guest" / "Продолжить без входа" button if visible.
    2. Click the ✕ svg inside the login panel container.
    3. Press Escape as the last resort (closes most modal dialogs).
    Returns True if the login text is gone afterward.
    """
    body = await _body_lower(page)
    if not any(h in body for h in _LOGIN_HINTS):
        return False

    # 1. Preferred: "Continue as guest" / equivalent text button
    for text in _LOGIN_GUEST_TEXTS:
        try:
            btn = page.get_by_text(text, exact=False).first
            if await btn.count() > 0:
                await btn.click(timeout=2000)
                await page.wait_for_timeout(900)
                body = await _body_lower(page)
                if not any(h in body for h in _LOGIN_HINTS):
                    return True
        except Exception:
            continue

    # 2. ✕ svg inside any login-panel selector
    for sel in _LOGIN_PANEL_SELECTORS:
        icon = page.locator(f"{sel} svg").first
        try:
            if await icon.count() == 0:
                continue
        except Exception:
            continue
        for target in (icon, icon.locator("xpath=.."), icon.locator("xpath=../..")):
            try:
                await target.click(timeout=2000)
                await page.wait_for_timeout(900)
                body = await _body_lower(page)
                if not any(h in body for h in _LOGIN_HINTS):
                    return True
            except Exception:
                continue

    # 3. Escape key
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(900)
        body = await _body_lower(page)
        return not any(h in body for h in _LOGIN_HINTS)
    except Exception:
        return False


async def _dismiss_cookie_banner(page) -> bool:
    """Dismiss a GDPR/cookie consent banner (modal #3).

    Prefers "Decline" (avoids creating a cookie session); falls back to Accept.
    Returns True if the cookie text is gone afterward.
    """
    body = await _body_lower(page)
    if not any(h in body for h in _COOKIE_HINTS):
        return False

    # Try Decline / Reject first
    for text in _COOKIE_DECLINE_TEXTS:
        try:
            btn = page.get_by_role("button", name=text, exact=False).first
            if await btn.count() > 0:
                await btn.click(timeout=2000)
                await page.wait_for_timeout(600)
                return True
        except Exception:
            continue
    # Try Accept (still gets rid of the banner)
    for text in _COOKIE_ACCEPT_TEXTS:
        try:
            btn = page.get_by_role("button", name=text, exact=False).first
            if await btn.count() > 0:
                await btn.click(timeout=2000)
                await page.wait_for_timeout(600)
                return True
        except Exception:
            continue
    # Try ✕ inside a cookie panel
    for sel in _COOKIE_PANEL_SELECTORS:
        icon = page.locator(f"{sel} svg").first
        try:
            if await icon.count() == 0:
                continue
        except Exception:
            continue
        for target in (icon, icon.locator("xpath=.."), icon.locator("xpath=../..")):
            try:
                await target.click(timeout=2000)
                await page.wait_for_timeout(600)
                return True
            except Exception:
                continue
    return False


async def _dismiss_app_banner(page) -> bool:
    """Dismiss a 'Get TikTok app' install banner (modal #4).

    The banner sits at the top of the page; dismissing it is nice-to-have but
    shouldn't be blocking. Tries to click the ✕ inside an app-banner container.
    Returns True if the banner text is gone afterward.
    """
    body = await _body_lower(page)
    if not any(h in body for h in _APP_HINTS):
        return False

    for sel in _APP_PANEL_SELECTORS:
        icon = page.locator(f"{sel} svg").first
        try:
            if await icon.count() == 0:
                continue
        except Exception:
            continue
        for target in (icon, icon.locator("xpath=.."), icon.locator("xpath=../..")):
            try:
                await target.click(timeout=2000)
                await page.wait_for_timeout(500)
                body = await _body_lower(page)
                if not any(h in body for h in _APP_HINTS):
                    return True
            except Exception:
                continue
    return False


async def _try_dismiss_all_modals(page) -> int:
    """Try ALL known benign modal dismissals in priority order.

    Returns the count of modals successfully dismissed this call (0 if none).
    The caller should ``continue`` the advance loop if count > 0.
    """
    dismissed = 0
    # Interest picker first (most common, fastest selector)
    if await _modal_present(page):
        if await _dismiss_modal(page):
            dismissed += 1
    # Login wall
    if await _dismiss_login_wall(page):
        dismissed += 1
    # Cookie banner
    if await _dismiss_cookie_banner(page):
        dismissed += 1
    # App banner (lowest priority, non-blocking)
    if await _dismiss_app_banner(page):
        dismissed += 1
    return dismissed


def _is_exhaustion(body: str) -> bool:
    """True when the body signals that the logged-out feed is dry (reload-able)."""
    return any(h in body for h in _EXHAUSTION_HINTS)


# ---------------------------------------------------------------------------
# Advance helper
# ---------------------------------------------------------------------------

async def _advance(page, mode: str, delay_factor: float = 1.0) -> None:
    """Move to the next content: wheel-scroll the Explore grid, or ArrowDown the
    For-You player (the wheel is a no-op on the full-screen player).

    ``delay_factor`` scales the jittered dwell time; use 2.0 in cooldown mode to
    appear more human when the IP is warm.
    """
    if mode == "wheel":
        await page.mouse.wheel(0, 2600)
    else:
        await page.keyboard.press("ArrowDown")
    # Human-ish pacing (anti-ban): jittered dwell per step. Kept deliberately slow
    # — hammering the logged-out feed is what trips TikTok's slider captcha.
    base_ms = random.uniform(1800, 3200)
    await page.wait_for_timeout(int(base_ms * delay_factor))


# ---------------------------------------------------------------------------
# Core async harvest loop
# ---------------------------------------------------------------------------

async def _harvest_async(
    geo_tier: str,
    *,
    surface: str,
    cycles: int,
    max_advances: int,
    headless: bool,
    target: Optional[int],
    accumulator_path: Optional[str],
    cooldown: bool,
) -> HarvestResult:
    from playwright.async_api import async_playwright  # noqa: PLC0415

    url = _SURFACES[surface]["url"]
    advance_mode = _SURFACES[surface]["advance"]
    locale, tz = _LOCALE_BY_TIER.get(geo_tier, _LOCALE_BY_TIER["World"])
    delay_factor = 2.0 if cooldown else 1.0
    initial_wait = 8000 if cooldown else 4000

    # Pre-populate seen from accumulator so this run dedupes against all prior runs.
    seen: dict[str, dict] = {}
    pre_loaded = 0
    if accumulator_path:
        try:
            with open(accumulator_path) as f:
                stored = json.load(f)
            loaded_items = stored.get("items", {})
            seen.update(loaded_items)
            pre_loaded = len(seen)
        except (FileNotFoundError, json.JSONDecodeError):
            pass  # first run or corrupted file — start fresh

    blocked = False
    modals_dismissed = 0
    modal_breakdown: dict[str, int] = {
        "interest": 0, "login": 0, "cookie": 0, "app": 0,
    }

    async def on_response(resp) -> None:
        # Both surfaces stream their videos via an `item_list` XHR
        # (recommend/preload for FYP, explore/item_list for Explore). Hashtag/
        # search item_lists are empty logged-out — nothing to gain by widening.
        if "item_list" not in resp.url:
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        for item in payload.get("itemList") or []:
            item_id = item.get("id")
            if item_id:
                seen[item_id] = item

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            locale=locale,
            timezone_id=tz,
            viewport={"width": 1280, "height": 900},
        )
        # Stealth: real-looking fingerprint so the headed browser isn't flagged.
        try:
            from playwright_stealth import Stealth  # noqa: PLC0415

            await Stealth().apply_stealth_async(context)
        except Exception:
            pass  # best-effort; the harvest still works without it

        page = await context.new_page()
        page.on("response", on_response)

        note = "completed"
        try:
            for cycle in range(cycles):
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # Longer initial wait in cooldown mode (let the page fully settle,
                # and let any post-load modal appear before we start advancing).
                await page.wait_for_timeout(initial_wait)

                # Dismiss any modals that may have appeared on load (cookie banner,
                # login nudge) before advancing — don't let them stall the first step.
                await _try_dismiss_all_modals(page)

                # FYP only: focus the player so ArrowDown lands. Do NOT click on the
                # Explore grid — its tiles are links, so a center click opens a video
                # page (and the resulting thrash trips TikTok's slider captcha).
                if advance_mode == "arrow":
                    vp = page.viewport_size or {"width": 1280, "height": 900}
                    await page.mouse.click(vp["width"] // 2, vp["height"] // 2)

                # A bot-check can greet us right after load — bail before advancing.
                body = await _body_lower(page)
                if any(m in body for m in _BLOCK_MARKERS):
                    blocked = True
                    note = f"bot-check on load (cycle {cycle}) — backed off, no grind"
                    break

                cycle_start = len(seen)
                stale = 0
                advances = 0
                while advances < max_advances and stale < _STALE_LIMIT:
                    body = await _body_lower(page)

                    # Check for captcha / bot block FIRST (highest priority).
                    if any(m in body for m in _BLOCK_MARKERS):
                        blocked = True
                        note = f"blocked on cycle {cycle} (back off / rotate IP — Q-3)"
                        break

                    # Check for feed exhaustion wall (triggers cycle reload, not dismiss).
                    if _is_exhaustion(body):
                        note = f"feed exhaustion wall on cycle {cycle} — reloading"
                        break  # break inner loop → outer loop will reload the page

                    # Unified modal check — all 4 types.
                    _before_dismiss_interest = await _modal_present(page)
                    _before_login = any(h in body for h in _LOGIN_HINTS)
                    _before_cookie = any(h in body for h in _COOKIE_HINTS)
                    _before_app = any(h in body for h in _APP_HINTS)

                    if _before_dismiss_interest or _before_login or _before_cookie or _before_app:
                        n_dismissed = await _try_dismiss_all_modals(page)
                        if n_dismissed > 0:
                            modals_dismissed += n_dismissed
                            # Track breakdown for the note
                            body_after = await _body_lower(page)
                            if _before_dismiss_interest and not await _modal_present(page):
                                modal_breakdown["interest"] += 1
                            if _before_login and not any(h in body_after for h in _LOGIN_HINTS):
                                modal_breakdown["login"] += 1
                            if _before_cookie and not any(h in body_after for h in _COOKIE_HINTS):
                                modal_breakdown["cookie"] += 1
                            if _before_app and not any(h in body_after for h in _APP_HINTS):
                                modal_breakdown["app"] += 1
                            continue  # re-run the loop head after any dismissal
                        # Couldn't clear a modal — back off rather than grind.
                        note = f"stuck on undismissable modal (cycle {cycle})"
                        break

                    before = len(seen)
                    await _advance(page, advance_mode, delay_factor)
                    advances += 1
                    if len(seen) > before:
                        stale = 0
                    else:
                        stale += 1
                        # At half the stale limit, proactively try all modals once
                        # more (a modal may have appeared AFTER the last advance
                        # without triggering any of our text hints yet).
                        if stale == _STALE_LIMIT // 2:
                            n = await _try_dismiss_all_modals(page)
                            if n > 0:
                                modals_dismissed += n
                                stale = 0  # modal was the blocker — reset stale counter

                    if target and len(seen) >= target:
                        note = f"reached target {target}"
                        break

                if blocked or (target and len(seen) >= target):
                    break
                if "stuck on" in note:
                    break
                # If a whole reload added nothing new (and it wasn't exhaustion),
                # the logged-out feed is genuinely dry.
                if cycle > 0 and len(seen) == cycle_start and "exhaustion" not in note:
                    note = f"feed dry after {cycle + 1} cycles ({len(seen)} items total)"
                    break
        finally:
            await browser.close()

    # Build diagnostic note
    new_this_run = len(seen) - pre_loaded
    breakdown_str = ", ".join(
        f"{k}={v}" for k, v in modal_breakdown.items() if v > 0
    ) or "none"
    note = (
        f"[{surface}{'|cooldown' if cooldown else ''}] {note}; "
        f"{new_this_run} new this run, {len(seen)} total; "
        f"modals dismissed={modals_dismissed} ({breakdown_str})"
    )

    # Save / update accumulator file
    if accumulator_path and seen:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(accumulator_path)), exist_ok=True)
            with open(accumulator_path, "w") as f:
                json.dump(
                    {
                        "version": 1,
                        "total": len(seen),
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                        "items": seen,
                    },
                    f,
                    ensure_ascii=False,
                )
        except Exception as e:
            note += f"; accumulator save failed: {e}"

    return HarvestResult(
        list(seen.values()),
        note,
        blocked,
        total_accumulated=len(seen),
        new_this_run=new_this_run,
    )


# ---------------------------------------------------------------------------
# Unattended accumulation runner  (python -m adapters.tiktok.discovery)
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    """Run one gentle harvest session and accumulate toward 500 posts.

    Usage::

        # First run (or any run) — auto-continues from saved state:
        python -m adapters.tiktok.discovery

        # Explicit cooldown mode (IP was recently hot):
        python -m adapters.tiktok.discovery --cooldown

        # Custom accumulator location:
        python -m adapters.tiktok.discovery --out /path/to/accumulator.json

        # Check current count without running a session:
        python -m adapters.tiktok.discovery --status

    Anti-ban: always runs with modest cycles/advances. After a captcha (blocked=True)
    the run stops immediately — operator should wait before the next run. The
    accumulator survives captcha stops; the next run continues from the saved count.

    Target: 500 unique posts accumulated across however many well-spaced runs are needed.
    """
    import argparse
    import sys

    TARGET = 500
    DEFAULT_ACCUMULATOR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "tiktok_accumulator.json",
    )

    parser = argparse.ArgumentParser(
        description="Accumulate TikTok posts toward 500 (unattended).",
    )
    parser.add_argument("--out", default=DEFAULT_ACCUMULATOR,
                        help="Path to the JSON accumulator file (default: data/tiktok_accumulator.json)")
    parser.add_argument("--cooldown", action="store_true",
                        help="Gentle / cooldown mode — double delays (use when IP recently served captcha)")
    parser.add_argument("--geo", default="KZ",
                        help="Geo tier: KZ (default), CIS, World")
    parser.add_argument("--cycles", type=int, default=2,
                        help="Reload cycles per run (default 2; each ~55 items on Explore)")
    parser.add_argument("--advances", type=int, default=30,
                        help="Max advances per cycle (default 30; Explore plateaus ~10–15)")
    parser.add_argument("--status", action="store_true",
                        help="Print current accumulator count and exit (no browser)")
    args = parser.parse_args()

    # --status: just print progress
    if args.status:
        try:
            with open(args.out) as f:
                stored = json.load(f)
            total = stored.get("total", 0)
            updated = stored.get("last_updated", "?")
            print(f"Accumulator: {total}/{TARGET} posts  (last_updated={updated})")
            print(f"Path: {args.out}")
        except FileNotFoundError:
            print(f"No accumulator found at {args.out}  (0/{TARGET})")
        sys.exit(0)

    # Check current state before running
    pre_count = 0
    try:
        with open(args.out) as f:
            pre_count = json.load(f).get("total", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    if pre_count >= TARGET:
        print(f"Already at {pre_count}/{TARGET} — target reached. Nothing to do.")
        sys.exit(0)

    print(f"Starting harvest | geo={args.geo} surface=explore "
          f"cooldown={args.cooldown} cycles={args.cycles} advances/cycle={args.advances}")
    print(f"Accumulator: {pre_count}/{TARGET} posts before this run")
    print(f"Saving to: {args.out}")

    result = harvest(
        args.geo,
        surface="explore",
        cycles=args.cycles,
        max_advances=args.advances,
        headless=False,
        target=TARGET,
        accumulator_path=args.out,
        cooldown=args.cooldown,
    )

    print(f"\nResult:")
    print(f"  note: {result.note}")
    print(f"  blocked: {result.blocked}")
    print(f"  new this run: {result.new_this_run}")
    print(f"  total accumulated: {result.total_accumulated}/{TARGET}")

    if result.blocked:
        print("\nCAPTCHA / BOT-CHECK HIT — IP is hot.")
        print("Action: wait at least 30–60 min (preferably hours) before re-running.")
        print("Progress saved. Next run will continue from current count.")
        sys.exit(2)

    remaining = TARGET - result.total_accumulated
    if remaining <= 0:
        print(f"\nTarget reached! {result.total_accumulated} posts accumulated.")
    elif result.new_this_run == 0:
        print("\nFeed pool EXHAUSTED for this session window (0 new items).")
        print("The logged-out Explore pool refreshes over time (several hours / day).")
        print("Action: wait 2–6 hours before re-running — do NOT hammer.")
        print(f"Progress saved at {result.total_accumulated}/{TARGET}.")
    else:
        # Estimate based on this run's actual yield (not a fixed default)
        avg_per_run = result.new_this_run
        runs_needed = max(1, -(-remaining // avg_per_run))  # ceiling div
        print(f"\n{remaining} more needed. ~{runs_needed} more session windows at today's rate.")
        print("Wait 2–6 hours between runs (let the feed pool refresh).")
        print(f"Progress saved at {result.total_accumulated}/{TARGET}.")

    sys.exit(0)
