"""Persona-isolation browser harness — the $0 anti-detect browser.

Shared infra (docs/handoffs/stats-instagram.md, Job 1). TikTok-persona and
Threads import this; X doesn't need it (pure HTTP). Nobody re-implements isolation.

What it gives you
-----------------
One **disposable, persistent profile per persona**, fully isolated from the
operator's real machine:

- **Persistent ``userDataDir``** at ``profiles/<persona_id>/`` so real cookies and
  storage survive between runs and the account "recognizes its device" each time.
  NEVER the operator's real Chrome profile (enforced — see ``_assert_disposable``).
- **playwright-stealth** evasions (kills ``navigator.webdriver``, fixes the
  headless tells) plus a **stable, per-persona fingerprint** derived
  deterministically from ``persona_id``: WebGL vendor/renderer, viewport/screen,
  ``hardwareConcurrency``, ``deviceMemory``, and a canvas-noise seed. Same persona
  ⇒ identical "device" every run; different personas ⇒ different devices.
- **KZ defaults**: ``ru-RU`` locale, ``Asia/Almaty`` timezone, Almaty geolocation.
- **Optional per-persona proxy** (residential/mobile IP). For the prototype the
  default is the operator's KZ home IP — fine logged-out; scaling the IP story is
  OPEN-QUESTIONS Q-3.

Why this is enough at $0
------------------------
A "device ban" on the web is just a flagged fingerprint + flagged cookies. Because
each persona is an isolated, disposable profile, recovering from a flag costs
nothing: :func:`reset_persona` nukes the profile dir and the next launch
regenerates a *different* stable fingerprint — no new hardware, no new machine.

Fingerprint choice (read this before "fixing" the UA)
-----------------------------------------------------
We deliberately keep the browser's **real User-Agent** (engine-consistent) rather
than spoofing a different Chrome version. A UA claiming Chrome 131 on top of a
Chrome 148 engine is a contradiction modern anti-bot trivially catches. Instead we
vary the **engine-safe** fingerprint dimensions per persona (GPU strings, screen,
core/memory counts, canvas noise). stealth also scrubs the ``HeadlessChrome``
token from the UA.

Humanization is the CALLER's job (this harness only isolates)
-------------------------------------------------------------
Isolation buys you nothing if you behave like a bot inside it. Callers MUST:

- **Pace like a human.** No tight loops. Use :func:`human_pause` between actions;
  randomize dwell/watch time (don't watch every reel for exactly N seconds).
- **Vary watch-time** and occasionally, lightly engage (a like now and then) so the
  session isn't 100% passive scrolling — but stay low and irregular.
- **Cap sessions.** Short, irregular sessions beat one long marathon. Don't pull
  hundreds of items per run; spread harvesting across runs/days.
- **Back off on friction.** A captcha / checkpoint / "try again later" means STOP
  for this persona, not retry-harder. Rest it (and consider :func:`reset_persona`
  only as a last resort — it also discards the warmed cookies).
- **Warm before hammering.** A fresh profile/account should browse normally for a
  while before any automated harvesting.

The harness needs no logged-in account to run; warm the profile logged-out first.
"""

from __future__ import annotations

import hashlib
import random
import shutil
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.sync_api import BrowserContext, sync_playwright
from playwright_stealth import Stealth

#: All persona profiles live under here (gitignored). Never outside it.
PROFILES_ROOT = Path("profiles")

# Per-persona fingerprint pools. Picks are deterministic (seeded by persona_id),
# so a persona presents the SAME device every run. Engine-safe dimensions only —
# we do not spoof the Chrome major version (see module docstring).
_WEBGL_GPUS: list[tuple[str, str]] = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]
# Common real desktop viewports (KZ users skew Windows laptop/desktop).
_VIEWPORTS: list[tuple[int, int]] = [(1920, 1080), (1536, 864), (1366, 768), (1440, 900), (1600, 900)]
_HW_CONCURRENCY: list[int] = [4, 6, 8, 12, 16]
_DEVICE_MEMORY: list[int] = [4, 8, 8, 16]  # 8 weighted (most common)

# Almaty, KZ — used for geolocation so it agrees with the Asia/Almaty timezone.
_ALMATY_GEO = {"latitude": 43.2389, "longitude": 76.8897, "accuracy": 80}


@dataclass(frozen=True)
class PersonaFingerprint:
    """The stable, per-persona device identity. Deterministic from ``persona_id``."""

    persona_id: str
    locale: str
    timezone: str
    webgl_vendor: str
    webgl_renderer: str
    viewport_width: int
    viewport_height: int
    hardware_concurrency: int
    device_memory: int
    canvas_seed: int


def _persona_rng(persona_id: str) -> random.Random:
    """A Random seeded by persona_id — same persona ⇒ same picks, forever."""
    digest = hashlib.sha256(persona_id.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def build_fingerprint(
    persona_id: str, *, locale: str = "ru-RU", timezone: str = "Asia/Almaty"
) -> PersonaFingerprint:
    """Deterministically derive a persona's stable device fingerprint."""
    rng = _persona_rng(persona_id)
    vendor, renderer = rng.choice(_WEBGL_GPUS)
    vw, vh = rng.choice(_VIEWPORTS)
    return PersonaFingerprint(
        persona_id=persona_id,
        locale=locale,
        timezone=timezone,
        webgl_vendor=vendor,
        webgl_renderer=renderer,
        viewport_width=vw,
        viewport_height=vh,
        hardware_concurrency=rng.choice(_HW_CONCURRENCY),
        device_memory=rng.choice(_DEVICE_MEMORY),
        canvas_seed=rng.getrandbits(32),
    )


def _assert_disposable(profile_dir: Path) -> Path:
    """Refuse to ever touch the operator's real browser profile.

    The whole point is isolation. A persistent ``userDataDir`` pointed at a real
    Chrome/Chromium/Firefox profile would taint the operator's machine and leak
    their identity into the persona. So we hard-require the profile to live under
    ``PROFILES_ROOT`` and reject anything that smells like a real profile.
    """
    resolved = profile_dir.resolve()
    root = PROFILES_ROOT.resolve()
    if root not in resolved.parents and resolved != root:
        raise ValueError(
            f"persona profile {resolved} must live under {root}/ — refusing to use "
            f"a path outside the disposable profiles root."
        )
    forbidden = ("google-chrome", "chromium", ".mozilla", "BraveSoftware", "Microsoft/Edge")
    low = str(resolved).lower()
    if any(f.lower() in low for f in forbidden):
        raise ValueError(
            f"persona profile {resolved} looks like a REAL browser profile — refusing."
        )
    return resolved


def _fingerprint_init_script(fp: PersonaFingerprint) -> str:
    """JS injected before page scripts to stabilize engine-safe fingerprint dims.

    Covers what playwright-stealth's overrides don't pin per-persona:
    ``hardwareConcurrency``, ``deviceMemory``, and deterministic canvas noise
    (seeded so the persona's canvas hash is STABLE across runs but distinct from
    other personas). WebGL vendor/renderer are handled by stealth's overrides.
    """
    return f"""
(() => {{
  const defineRO = (obj, prop, val) => {{
    try {{ Object.defineProperty(obj, prop, {{ get: () => val, configurable: true }}); }} catch (e) {{}}
  }};
  defineRO(navigator, 'hardwareConcurrency', {fp.hardware_concurrency});
  defineRO(navigator, 'deviceMemory', {fp.device_memory});

  // Deterministic canvas noise, seeded per-persona. Goal: a STABLE-yet-unique
  // canvas fingerprint, not a randomized-every-call one (that itself is a tell).
  let seed = {fp.canvas_seed} >>> 0;
  const rnd = () => {{ seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; }};
  const tweak = (data) => {{
    for (let i = 0; i < data.length; i += 4) {{
      if (rnd() < 0.02) {{ // touch ~2% of pixels by ±1 — invisible, deterministic
        data[i]   = Math.max(0, Math.min(255, data[i]   + (rnd() < 0.5 ? -1 : 1)));
        data[i+1] = Math.max(0, Math.min(255, data[i+1] + (rnd() < 0.5 ? -1 : 1)));
        data[i+2] = Math.max(0, Math.min(255, data[i+2] + (rnd() < 0.5 ? -1 : 1)));
      }}
    }}
    return data;
  }};
  const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function(...args) {{
    const res = origGetImageData.apply(this, args);
    // reseed per call from the persona seed so output is deterministic
    seed = {fp.canvas_seed} >>> 0;
    tweak(res.data);
    return res;
  }};
}})();
"""


def launch_persona(
    persona_id: str,
    *,
    proxy: Optional[dict] = None,
    locale: str = "ru-RU",
    timezone: str = "Asia/Almaty",
    headless: bool = False,
    channel: str = "chrome",
) -> BrowserContext:
    """Launch (or resume) an isolated, fingerprint-stable persona browser.

    Parameters
    ----------
    persona_id:
        Stable id. Drives both the persistent profile dir and the deterministic
        fingerprint. Reuse the same id to "be the same device/person" again.
    proxy:
        Playwright proxy dict, e.g. ``{"server": "http://host:port", "username":
        ..., "password": ...}``. Default ``None`` = operator's home IP (KZ).
    locale, timezone:
        Default to KZ (``ru-RU`` / ``Asia/Almaty``). Geolocation is set to Almaty
        to agree with the timezone.
    headless:
        Default ``False`` — a visible window is both less detectable and lets the
        operator watch/intervene. Use ``True`` only for unattended runs.
    channel:
        Browser channel. Defaults to real ``"chrome"``; if that isn't installed we
        fall back to Playwright's bundled Chromium and carry on (prototype-fine;
        real Chrome is marginally less detectable).

    Returns
    -------
    BrowserContext
        A persistent context with stealth + the persona fingerprint applied. Call
        :func:`close_persona` (not just ``context.close()``) to also stop the
        Playwright driver. The driver handle is stashed on the context so it
        isn't garbage-collected mid-session.
    """
    profile_dir = _assert_disposable(PROFILES_ROOT / persona_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    fp = build_fingerprint(persona_id, locale=locale, timezone=timezone)

    # Consistency over disguise: we keep the browser's REAL OS/UA (here Linux) and
    # only vary engine-safe dims. So we DISABLE the stealth evasions that would
    # impose Windows assumptions (platform=Win32, synthesized Windows client-hints
    # / userAgentData) on top of a Linux UA — that contradiction is itself a tell.
    # navigator.webdriver scrubbing, language override, and the WebGL overrides stay on.
    with warnings.catch_warnings():
        # stealth warns because its navigator_platform_override default is always
        # non-None; disabling the evasion on purpose makes that warning noise.
        warnings.filterwarnings("ignore", message=".*navigator_platform is False.*")
        stealth = Stealth(
            init_scripts_only=True,  # everything as init scripts -> applies to a persistent context
            navigator_languages_override=(locale, locale.split("-")[0]),
            webgl_vendor_override=fp.webgl_vendor,
            webgl_renderer_override=fp.webgl_renderer,
            navigator_platform=False,        # keep real platform (matches real UA OS)
            navigator_user_agent_data=False,  # keep real userAgentData (no Win spoof)
            sec_ch_ua=False,                  # let the browser send real client hints
        )

    launch_kwargs = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        locale=locale,
        timezone_id=timezone,
        viewport={"width": fp.viewport_width, "height": fp.viewport_height},
        geolocation=_ALMATY_GEO,
        permissions=["geolocation"],
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--no-first-run",
        ],
    )
    if proxy:
        launch_kwargs["proxy"] = proxy

    pw = sync_playwright().start()
    try:
        context = _launch_with_channel(pw, channel, launch_kwargs)
    except Exception:
        pw.stop()
        raise

    stealth.apply_stealth_sync(context)
    context.add_init_script(_fingerprint_init_script(fp))

    # Keep the driver + metadata alive for the context's lifetime.
    context._persona_pw = pw  # type: ignore[attr-defined]
    context._persona_id = persona_id  # type: ignore[attr-defined]
    context._persona_fingerprint = fp  # type: ignore[attr-defined]
    return context


def _launch_with_channel(pw, channel: str, launch_kwargs: dict) -> BrowserContext:
    """Launch the persistent context, falling back to bundled Chromium.

    Prefer the real channel (e.g. Chrome). If it isn't installed, fall back to
    Playwright's bundled Chromium so the prototype still runs on a bare box.
    """
    try:
        return pw.chromium.launch_persistent_context(channel=channel, **launch_kwargs)
    except Exception as err:
        if channel and "is not found" in str(err) or "Executable doesn't exist" in str(err):
            # Real channel missing — bundled Chromium it is.
            return pw.chromium.launch_persistent_context(**launch_kwargs)
        raise


def close_persona(context: BrowserContext) -> None:
    """Close a persona context AND stop its Playwright driver.

    Use this instead of a bare ``context.close()`` so the driver subprocess
    doesn't linger. Safe to call on an already-closed context.
    """
    pw = getattr(context, "_persona_pw", None)
    try:
        context.close()
    finally:
        if pw is not None:
            pw.stop()


def reset_persona(persona_id: str) -> bool:
    """Nuke a tainted persona's profile dir so its fingerprint is regenerated.

    A flagged web fingerprint is recoverable for free: delete the profile and the
    next :func:`launch_persona` builds a fresh stable identity. Returns ``True`` if
    a profile existed and was removed, ``False`` if there was nothing to remove.

    Close any live context for this persona BEFORE calling — removing a profile
    dir out from under a running browser corrupts it.
    """
    profile_dir = _assert_disposable(PROFILES_ROOT / persona_id)
    if not profile_dir.exists():
        return False
    shutil.rmtree(profile_dir)
    return True


def human_pause(min_sec: float = 0.6, max_sec: float = 2.8) -> None:
    """Sleep a randomized, human-ish interval. Use BETWEEN actions, never a tight loop.

    A convenience so callers pace consistently; see the humanization expectations
    in the module docstring for the rest (watch-time variance, session caps,
    back-off on captcha).
    """
    time.sleep(random.uniform(min_sec, max_sec))
