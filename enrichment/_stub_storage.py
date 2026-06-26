"""Thin local stub for core.storage — used while the spine is being built in parallel.

The spine agent owns the REAL ``core/storage.py`` and the SQLite schema.
Until that lands, this stub:
- Writes ``post_content`` rows to ``data/enrichment_stub.json``
- Writes thumbnail paths to the same file
- Provides ``is_in_post_content`` for idempotency

**Integration path:** once the spine's ``core.storage`` exists, replace the
``storage=`` argument in ``enrich()`` calls with a ``CoreStorage`` instance.
The enrichment module never imports core.storage at the top level so there is
no hard dependency until you wire it in.

The stub file schema mirrors the documented ``post_content`` table exactly
(docs/CORE-SPINE.md) so the real storage integration is a drop-in swap.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


_DEFAULT_STUB_PATH = Path("data/enrichment_stub.json")


class StubStorage:
    """Duck-type compatible with the ``StorageProtocol`` expected by ``enrich()``.

    All writes go to a local JSON file. Thread-unsafe — single-process only.
    """

    def __init__(self, path: Path = _DEFAULT_STUB_PATH) -> None:
        self._path = path
        self._state: dict[str, Any] = self._load()

    # --- post_content --------------------------------------------------------

    def is_in_post_content(self, platform: str, post_id: str) -> bool:
        key = f"{platform}:{post_id}"
        return key in self._state.get("post_content", {})

    def write_post_content(
        self,
        *,
        platform: str,
        platform_post_id: str,
        media_paths: list[str],
        caption: Optional[str],
        spoiler_text: Optional[str],
        sound_id: Optional[str],
        sound_name: Optional[str],
        sound_author: Optional[str],
        author_display_name: Optional[str],
        extracted_at: datetime,
        status: str,  # "pending" | "done" | "expired_url_miss"
    ) -> None:
        key = f"{platform}:{platform_post_id}"
        self._state.setdefault("post_content", {})[key] = {
            "platform": platform,
            "platform_post_id": platform_post_id,
            "media_paths": media_paths,
            "caption": caption,
            "spoiler_text": spoiler_text,
            "sound_id": sound_id,
            "sound_name": sound_name,
            "sound_author": sound_author,
            "author_display_name": author_display_name,
            "extracted_at": extracted_at.isoformat(),
            "status": status,
        }
        self._save()

    def update_post_content_status(self, platform: str, post_id: str, status: str) -> None:
        key = f"{platform}:{post_id}"
        row = self._state.get("post_content", {}).get(key)
        if row:
            row["status"] = status
            self._save()

    # --- thumbnails ----------------------------------------------------------

    def set_thumbnail_path(self, platform: str, post_id: str, path: str) -> None:
        key = f"{platform}:{post_id}"
        self._state.setdefault("thumbnail_paths", {})[key] = path
        self._save()

    def get_thumbnail_path(self, platform: str, post_id: str) -> Optional[str]:
        key = f"{platform}:{post_id}"
        return self._state.get("thumbnail_paths", {}).get(key)

    # --- raw payload store (stub-only convenience) ---------------------------

    def store_raw(self, platform: str, post_id: str, raw: dict) -> None:
        """Store a raw payload so the fixture runner can look it up by identity."""
        key = f"{platform}:{post_id}"
        self._state.setdefault("raw_cache", {})[key] = raw
        self._save()

    def get_raw(self, platform: str, post_id: str) -> Optional[dict]:
        key = f"{platform}:{post_id}"
        return self._state.get("raw_cache", {}).get(key)

    # --- persistence ---------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
        return {"post_content": {}, "thumbnail_paths": {}, "raw_cache": {}}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._state, fh, indent=2, default=str)
        tmp.replace(self._path)

    def dump_post_content(self) -> list[dict]:
        """Return all post_content rows as a list (for display/testing)."""
        return list(self._state.get("post_content", {}).values())


def make_stub(path: Optional[Path] = None) -> StubStorage:
    """Factory: create a StubStorage, defaulting to data/enrichment_stub.json."""
    return StubStorage(path or _DEFAULT_STUB_PATH)
