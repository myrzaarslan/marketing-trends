"""Tiny demo for the persona-isolation harness.

Run it:  python -m core.harness.demo_persona  [persona_id]  [--headed]

Proves the isolation properties without needing any account (logged-out, KZ home
IP): a killed ``navigator.webdriver``, KZ locale/timezone, the stable per-persona
fingerprint, real network egress, and that ``reset_persona`` regenerates a fresh
identity. Default is headless so it runs on a box with no display; pass
``--headed`` to watch the window.
"""

from __future__ import annotations

import sys

from core.harness import (
    build_fingerprint,
    close_persona,
    human_pause,
    launch_persona,
    reset_persona,
)

# JS read back from inside the persona to confirm what a site would actually see.
_PROBE_JS = """() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
  const uaData = navigator.userAgentData || {};
  return {
    webdriver: navigator.webdriver,
    platform: navigator.platform,
    uaDataPlatform: uaData.platform,
    language: navigator.language,
    languages: navigator.languages,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory,
    screen: `${screen.width}x${screen.height}`,
    webglVendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
    webglRenderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
    userAgent: navigator.userAgent,
  };
}"""


def _mask_ip(ip: str) -> str:
    parts = ip.split(".")
    return f"{parts[0]}.{parts[1]}.x.x" if len(parts) == 4 else "x.x.x.x"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    headed = "--headed" in sys.argv
    persona_id = args[0] if args else "demo-edu-kz-01"

    fp = build_fingerprint(persona_id)
    print(f"persona              : {persona_id}")
    print(f"expected fingerprint : screen={fp.viewport_width}x{fp.viewport_height} "
          f"cores={fp.hardware_concurrency} mem={fp.device_memory} "
          f"gpu={fp.webgl_renderer.split(',')[1].strip()}")

    context = launch_persona(persona_id, headless=not headed)
    try:
        page = context.new_page()

        # 1) Fingerprint / isolation probe (about:blank — no network needed).
        page.goto("about:blank")
        seen = page.evaluate(_PROBE_JS)
        print("\n--- what a site sees inside the persona ---")
        print(f"navigator.webdriver  : {seen['webdriver']}   (must be False/undefined)")
        print(f"platform / uaData    : {seen['platform']} / {seen['uaDataPlatform']}   (must agree w/ UA OS)")
        print(f"language / languages : {seen['language']} / {seen['languages']}")
        print(f"timezone             : {seen['timezone']}")
        print(f"hardwareConcurrency  : {seen['hardwareConcurrency']}")
        print(f"deviceMemory         : {seen['deviceMemory']}")
        print(f"screen               : {seen['screen']}")
        print(f"webgl vendor         : {seen['webglVendor']}")
        print(f"webgl renderer       : {seen['webglRenderer']}")
        print(f"user agent           : {seen['userAgent']}")

        # 2) Real egress on the home IP (confirms the persona actually browses).
        print("\n--- network egress (this KZ home IP, logged out) ---")
        try:
            page.goto("https://ipinfo.io/json", timeout=20000)
            import json as _json
            info = _json.loads(page.inner_text("pre"))
            print(f"egress country/city  : {info.get('country')} / {info.get('city')}")
            print(f"egress ip (masked)   : {_mask_ip(info.get('ip', ''))}  org={info.get('org')}")
        except Exception as e:
            print(f"egress check skipped : {type(e).__name__}: {e}")

        human_pause()
    finally:
        close_persona(context)

    # 3) Show the profile persisted, then that reset nukes it.
    from core.harness.persona_browser import PROFILES_ROOT
    prof = PROFILES_ROOT / persona_id
    print(f"\nprofile persisted at : {prof}  exists={prof.exists()}")
    if persona_id.startswith("demo-"):
        removed = reset_persona(persona_id)
        print(f"reset_persona()      : removed={removed}  exists_now={prof.exists()}")
    else:
        print("reset_persona()      : skipped (non-demo persona; won't nuke real profiles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
