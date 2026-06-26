"""Shared ingestion harness utilities (platform-agnostic).

Lives in ``core`` so every browser/persona-based platform adapter can reuse it.
Currently exposes the persona-isolation browser harness — the $0 anti-detect
browser: one disposable, persistent, fingerprint-stable profile per persona,
fully isolated from the operator's real machine.
"""

from core.harness.persona_browser import (
    PersonaFingerprint,
    build_fingerprint,
    close_persona,
    human_pause,
    launch_persona,
    reset_persona,
)

__all__ = [
    "PersonaFingerprint",
    "build_fingerprint",
    "close_persona",
    "human_pause",
    "launch_persona",
    "reset_persona",
]
