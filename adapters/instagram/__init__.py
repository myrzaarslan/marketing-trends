"""Instagram platform adapter.

Implements ``core.adapter.PlatformAdapter`` for Instagram via instagrapi (the
mobile private API — round two; see docs/REVERSE-ENGINEERING.md). See
``adapters/instagram/README.md`` for the operational story (burner-account
requirement, session warming, which fields are reliable, observed ban behavior).
"""

from adapters.instagram.adapter import InstagramAdapter, SoftBlockError

__all__ = ["InstagramAdapter", "SoftBlockError"]
