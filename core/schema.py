"""Canonical data shapes shared by every platform adapter.

The contract: adapters return these dataclasses. `core` persists them. Nobody invents
their own field names. See docs/INGESTION-CONTRACT.md.

Principle: raw + normalized. Always populate `raw` with the COMPLETE original platform
payload, even fields we don't normalize yet — the "what is viral" rule is undecided
(docs/OPEN-QUESTIONS.md Q-1) and will be computed later over stored data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional

MediaType = Literal["video", "image", "text", "carousel"]
TrendType = Literal["hashtag", "sound", "format", "topic"]
GeoTier = Literal["KZ", "CIS", "World"]
Segment = Literal["direct_competitor", "edu_influencer", "global_edtech", "adjacent"]


@dataclass
class WatchedAccount:
    """An account on the Watchlist that we monitor for Post Records."""
    handle: str
    platform: str
    segment: Segment
    geo_tier: GeoTier
    platform_account_id: Optional[str] = None
    display_name: Optional[str] = None


@dataclass
class PostRecord:
    """One captured post: normalized common fields + the full raw payload.

    A PostRecord may later be labeled a Viral Post; that decision is NOT made here.
    """
    platform: str
    platform_post_id: str
    account_handle: str
    url: str
    fetched_at: datetime
    media_type: MediaType
    raw: dict[str, Any]

    posted_at: Optional[datetime] = None
    caption: Optional[str] = None
    hashtags: list[str] = field(default_factory=list)
    sound_id: Optional[str] = None
    sound_name: Optional[str] = None
    duration_sec: Optional[float] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    share_count: Optional[int] = None
    save_count: Optional[int] = None
    thumbnail_url: Optional[str] = None
    geo_tier: Optional[GeoTier] = None
    # author's follower count at fetch time — for relative-to-baseline / follower-normalized
    # ranking (docs/SIGNALS.md). Available on all four platforms' author objects.
    author_follower_count: Optional[int] = None


@dataclass
class Trend:
    """A pattern (hashtag/sound/format/topic) gaining traction, per platform + geo."""
    platform: str
    trend_type: TrendType
    name: str
    sampled_at: datetime
    raw: dict[str, Any]

    geo_tier: Optional[GeoTier] = None
    rank: Optional[int] = None
    score: Optional[float] = None
    volume: Optional[int] = None
