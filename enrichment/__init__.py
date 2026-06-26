"""Layer-3 enrichment — Content Bundle extraction.

Pure extraction of media files and text content from already-captured ``raw``
payloads. No understanding (no OCR/ASR/multimodal). No comments. No scraping.

Public API
----------
``enrich(post_identities, ...)``   — download full Content Bundle for top-N posts.
``download_thumbnail(post, ...)``  — download thumbnail for every captured post.

See ``enrichment/extractor.py`` for full signatures.
"""

from enrichment.extractor import enrich, download_thumbnail

__all__ = ["enrich", "download_thumbnail"]
