"""Authenticated-session fallback for the Threads adapter.

Round one established that public Threads profiles read **unauthenticated** today.
But Threads reuses Instagram's backend/auth, and IG already forces a login wall on
public reads — Threads could follow at any time (see docs/REVERSE-ENGINEERING.md
§Threads). When it does, the durable $0 answer is the same one the IG round-two
adapter uses: a warmed **burner** session (never a real/company account).

This module models that session as a bag of cookies — the universal artifact both
Playwright (``context.add_cookies``) and ``requests`` understand. The key cookie is
``sessionid``; ``ds_user_id`` / ``csrftoken`` / ``mid`` round it out. Because the
backend is shared, an Instagram ``sessionid`` authenticates Threads too.

The ``instagrapi`` bridge is **lazy** — instagrapi is the IG adapter's dependency,
not ours. If it's present in the shared venv (the IG round-two session installs it)
this works; otherwise the cookie/storage_state constructors still do.

GUARDRAILS (ADR-0001 / playbook): burner accounts only, polite/low-volume, treat a
401/403/429/challenge as "back off," never "retry harder."
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

# Cookies are valid on the IG backend that Threads shares; set them on every
# domain the web client touches so either transport (browser or requests) sends them.
_COOKIE_DOMAINS = (".threads.com", ".threads.net", ".instagram.com")

# Cookies that actually matter for an authenticated read; others are noise.
_MEANINGFUL = ("sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "rur")


@dataclass
class ThreadsSession:
    """A burner login expressed as cookies. ``sessionid`` is the one that counts."""

    cookies: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Keep only non-empty string cookies; tolerate junk from upstream tools.
        self.cookies = {
            str(k): str(v)
            for k, v in (self.cookies or {}).items()
            if v not in (None, "")
        }

    @property
    def is_usable(self) -> bool:
        return bool(self.cookies.get("sessionid"))

    # ---- constructors ----------------------------------------------------

    @classmethod
    def from_cookies(cls, **cookies: str) -> "ThreadsSession":
        """e.g. ``ThreadsSession.from_cookies(sessionid=..., ds_user_id=...)``."""
        return cls(cookies=dict(cookies))

    @classmethod
    def from_storage_state(cls, path: str) -> "ThreadsSession":
        """Load cookies from a Playwright ``storage_state`` JSON file."""
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        jar = {
            c.get("name"): c.get("value")
            for c in state.get("cookies", [])
            if c.get("name") in _MEANINGFUL
        }
        return cls(cookies=jar)

    @classmethod
    def from_instagrapi(cls, settings_path: str) -> "ThreadsSession":
        """Mint a session from a warmed instagrapi burner (IG adapter's tool).

        Lazy import on purpose: instagrapi belongs to the Instagram adapter. This
        only resolves if that dependency is installed in the shared venv.
        """
        try:
            from instagrapi import Client  # type: ignore
        except Exception as e:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "instagrapi is not installed in this venv. It is the Instagram "
                "adapter's dependency; install it there, or use "
                "ThreadsSession.from_cookies / from_storage_state instead."
            ) from e
        cl = Client()
        cl.load_settings(settings_path)
        # instagrapi's private API client is a requests.Session under the hood.
        jar = {
            k: v
            for k, v in cl.private.cookies.get_dict().items()
            if k in _MEANINGFUL
        }
        return cls(cookies=jar)

    # ---- transport adapters ---------------------------------------------

    def playwright_cookies(self) -> list[dict[str, Any]]:
        """Cookie list shaped for ``BrowserContext.add_cookies``."""
        out: list[dict[str, Any]] = []
        for domain in _COOKIE_DOMAINS:
            for name, value in self.cookies.items():
                out.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "httpOnly": False,
                        "sameSite": "Lax",
                    }
                )
        return out

    def requests_cookies(self) -> dict[str, str]:
        return dict(self.cookies)
