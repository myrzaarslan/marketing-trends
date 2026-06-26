"""Threads platform adapter.

Feasibility-spike adapter for Meta's Threads. See README.md for the go/no-go
recommendation and the observed reachability / fragility notes.

Threads has no usable free public API (the official Graph API is owned-account
only, like Instagram), so this drives the public web UI with Playwright and
harvests the GraphQL response the page itself fetches. See INGESTION-CONTRACT.md.
"""

from __future__ import annotations

from adapters.threads.adapter import ThreadsAdapter, ThreadsLoginWall, ThreadsRateLimited
from adapters.threads.session import ThreadsSession

__all__ = ["ThreadsAdapter", "ThreadsLoginWall", "ThreadsRateLimited", "ThreadsSession"]
