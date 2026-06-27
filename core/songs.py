"""Song (sound/music) aggregation + ranking.

Groups Posts by the sound they use and ranks the *songs* — "what audio is going
viral" — for the marketing team. Mirrors the post ranker (core/ranker.py) but one
level up: a song is an aggregate over the posts that use it.

Scope: **TikTok + Instagram only** — the only two platforms that expose reliable
sound metadata (TikTok `music.id`, Instagram reel `clips_metadata`). See SIGNALS.md.

Identity (per-platform, decided 2026-06-27)
-------------------------------------------
A "song" is identified within a single platform by a stable key:
  * `sound_id` when present (TikTok music.id / Instagram audio_cluster_id), else
  * `name:<normalized sound_name>` so the yt-dlp TikTok path (name only, no id) and
    IG posts that carry a canonical name still group together.
Posts with neither id nor name carry no song and are skipped. TikTok and Instagram
keys live in separate namespaces (no fuzzy cross-platform matching) — the song
identity is therefore the pair (platform, key).

Metrics (ALL computed; the UI chooses which to rank by)
-------------------------------------------------------
  post_count           — distinct posts using the song (adoption)
  distinct_accounts    — distinct creators using the song
  total_views          — Σ view_count across its posts (reach)
  total_volume         — Σ best-available volume (views, else engagement sum)
  total_engagement     — Σ (likes+comments+shares+saves) across its posts
  avg_engagement_rate  — mean per-post engagement rate (de-biases big accounts)
  rising               — adoption in the recent window (posts first-seen lately)

Reuses the post ranker's per-platform helpers so a song's numbers stay consistent
with the post cards.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from sqlalchemy.orm import Session

from core.ranker import (
    _engagement_numerator,
    _score_engagement_rate,
    _volume_metric,
)
from core.storage import Post, PostSnapshot, sounds_for

# ---------------------------------------------------------------------------
# Types / constants
# ---------------------------------------------------------------------------

SongSortKey = Literal[
    "reuse_count",
    "post_count",
    "total_views",
    "total_engagement",
    "avg_engagement_rate",
    "rising",
]

ALL_SONG_SORTS: tuple[SongSortKey, ...] = (
    "reuse_count",
    "post_count",
    "total_views",
    "total_engagement",
    "avg_engagement_rate",
    "rising",
)

# "Reused most" is the headline question for songs — rank by the platform's own
# count of videos using the sound (TikTok videoCount / IG formatted clips count),
# populated by the sound pivot (core/sound_harvest.py). Falls back to our observed
# post-count for sounds not yet pivoted, so the list is never empty.
DEFAULT_SONG_SORT: SongSortKey = "reuse_count"

# Platforms that expose reliable sound metadata.
SONG_PLATFORMS: frozenset[str] = frozenset({"tiktok", "instagram"})


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_name(name: Optional[str]) -> Optional[str]:
    """Lowercase + collapse whitespace for name-based grouping."""
    if not name:
        return None
    norm = " ".join(name.strip().lower().split())
    return norm or None


def song_key_for(sound_id: Optional[str], sound_name: Optional[str]) -> Optional[str]:
    """Stable per-platform key for a sound. id wins; else a name: fallback; else None."""
    if sound_id:
        sid = str(sound_id).strip()
        if sid and sid != "0":
            return sid
    norm = normalize_name(sound_name)
    if norm:
        return f"name:{norm}"
    return None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class _SongAgg:
    """Mutable accumulator for one (platform, key) song."""

    __slots__ = (
        "platform", "key", "sound_id", "sound_name",
        "post_ids", "accounts", "view_sum", "volume_sum", "eng_sum",
        "rates", "recent_post_count", "top_post_id", "top_score",
        "latest_first_seen", "geo_counts",
        # Authoritative Sound-row fields (from the sound pivot), if present.
        "platform_video_count", "sound_author", "cover_url", "is_original",
        "play_url",
    )

    def __init__(self, platform: str, key: str) -> None:
        self.platform = platform
        self.key = key
        self.sound_id: Optional[str] = None
        self.sound_name: Optional[str] = None
        self.post_ids: list[str] = []
        self.accounts: set[str] = set()
        self.view_sum: int = 0
        self.volume_sum: float = 0.0
        self.eng_sum: float = 0.0
        self.rates: list[float] = []
        self.recent_post_count: int = 0
        self.top_post_id: Optional[str] = None
        self.top_score: float = -1.0
        self.latest_first_seen: Optional[datetime] = None
        self.geo_counts: dict[str, int] = {}
        self.platform_video_count: Optional[int] = None
        self.sound_author: Optional[str] = None
        self.cover_url: Optional[str] = None
        self.is_original: Optional[bool] = None
        self.play_url: Optional[str] = None

    def as_dict(self, sort: SongSortKey) -> dict[str, Any]:
        avg_rate = statistics.fmean(self.rates) if self.rates else None
        post_count = len(self.post_ids)
        distinct_accounts = len(self.accounts)
        geo_tier = (
            max(self.geo_counts, key=self.geo_counts.get) if self.geo_counts else None
        )
        # Reuse count = the platform's own number of videos using this sound when we
        # have it (the pivot filled it), else our observed post-count as a floor.
        if self.platform_video_count is not None:
            reuse_count = self.platform_video_count
            reuse_source = "platform"
        else:
            reuse_count = post_count
            reuse_source = "observed"
        metrics = {
            "reuse_count": reuse_count,
            "post_count": post_count,
            "distinct_accounts": distinct_accounts,
            "total_views": self.view_sum,
            "total_volume": self.volume_sum,
            "total_engagement": self.eng_sum,
            "avg_engagement_rate": avg_rate,
            "rising": self.recent_post_count,
            "recent_post_count": self.recent_post_count,
        }
        return {
            "platform": self.platform,
            "key": self.key,
            "sound_id": self.sound_id,
            "sound_name": self.sound_name,
            "sound_author": self.sound_author,  # may be refined by API from top post
            "cover_url": self.cover_url,
            "is_original": self.is_original,
            # True when we have an audio source (pivoted) — drives the UI download button.
            "downloadable": self.play_url is not None,
            "reuse_count_source": reuse_source,
            "platform_video_count": self.platform_video_count,
            "top_platform_post_id": self.top_post_id,
            "geo_tier": geo_tier,
            "latest_first_seen": (
                self.latest_first_seen.isoformat() if self.latest_first_seen else None
            ),
            "score": _metric_for_sort(metrics, sort),
            "sort_used": sort,
            **metrics,
        }


def _metric_for_sort(metrics: dict[str, Any], sort: SongSortKey) -> Optional[float]:
    if sort == "reuse_count":
        return float(metrics["reuse_count"])
    if sort == "post_count":
        return float(metrics["post_count"])
    if sort == "total_views":
        return float(metrics["total_views"])
    if sort == "total_engagement":
        return float(metrics["total_engagement"])
    if sort == "avg_engagement_rate":
        return metrics["avg_engagement_rate"]
    if sort == "rising":
        return float(metrics["rising"])
    return float(metrics["reuse_count"])


def aggregate_songs(
    session: Session,
    *,
    platform: Optional[str] = None,
    geo_tier: Optional[str] = None,
    period_days: int = 30,
) -> dict[tuple[str, str], _SongAgg]:
    """Build (platform, key) -> aggregate over posts in the window that carry a sound."""
    now = _utcnow_naive()
    cutoff = now - timedelta(days=period_days)
    recent_window = max(1, period_days // 3)
    recent_cutoff = now - timedelta(days=recent_window)

    q = session.query(Post).filter(Post.first_seen_at >= cutoff)
    if platform:
        q = q.filter(Post.platform == platform.lower())
    else:
        q = q.filter(Post.platform.in_(tuple(SONG_PLATFORMS)))
    if geo_tier:
        q = q.filter(Post.geo_tier == geo_tier)

    aggs: dict[tuple[str, str], _SongAgg] = {}
    for post in q.all():
        if post.platform not in SONG_PLATFORMS:
            continue
        key = song_key_for(post.sound_id, post.sound_name)
        if key is None:
            continue

        snap = (
            session.query(PostSnapshot)
            .filter_by(platform=post.platform, platform_post_id=post.platform_post_id)
            .order_by(PostSnapshot.fetched_at.desc())
            .first()
        )
        if snap is None:
            continue

        agg = aggs.get((post.platform, key))
        if agg is None:
            agg = _SongAgg(post.platform, key)
            aggs[(post.platform, key)] = agg

        agg.post_ids.append(post.platform_post_id)
        if post.account_handle:
            agg.accounts.add(post.account_handle)
        if post.sound_id and not agg.sound_id:
            agg.sound_id = post.sound_id
        if post.sound_name and not agg.sound_name:
            agg.sound_name = post.sound_name
        if post.geo_tier:
            agg.geo_counts[post.geo_tier] = agg.geo_counts.get(post.geo_tier, 0) + 1

        if snap.view_count is not None:
            agg.view_sum += snap.view_count
        vol = _volume_metric(snap, post.platform)
        if vol is not None:
            agg.volume_sum += vol
        eng = _engagement_numerator(snap, post.platform)
        if eng is not None:
            agg.eng_sum += eng
        rate = _score_engagement_rate(snap, post.platform)
        if rate is not None:
            agg.rates.append(rate)

        if post.first_seen_at and post.first_seen_at >= recent_cutoff:
            agg.recent_post_count += 1
        if post.first_seen_at and (
            agg.latest_first_seen is None or post.first_seen_at > agg.latest_first_seen
        ):
            agg.latest_first_seen = post.first_seen_at

        # Track the strongest post as the song's cover/representative.
        post_score = rate if rate is not None else (vol or 0.0)
        if post_score > agg.top_score:
            agg.top_score = post_score
            agg.top_post_id = post.platform_post_id

    # Attach authoritative Sound rows (the platform's own reuse count + metadata),
    # populated by the sound pivot. One bulk query for the whole working set.
    if aggs:
        meta = sounds_for(session, list(aggs.keys()))
        for key, agg in aggs.items():
            m = meta.get(key)
            if not m:
                continue
            agg.platform_video_count = m.get("video_count")
            agg.sound_author = m.get("author_name") or agg.sound_author
            agg.cover_url = m.get("cover_url")
            agg.is_original = m.get("is_original")
            agg.play_url = m.get("play_url")
            if m.get("title") and not agg.sound_name:
                agg.sound_name = m.get("title")

    return aggs


def rank_songs(
    session: Session,
    *,
    platform: Optional[str] = None,
    geo_tier: Optional[str] = None,
    period_days: int = 30,
    sort: SongSortKey = DEFAULT_SONG_SORT,
    limit: int = 60,
    exclude_keys: Optional[set[tuple[str, str]]] = None,
    pinned_keys: Optional[set[tuple[str, str]]] = None,
) -> list[dict[str, Any]]:
    """Ranked list of song dicts.

    `exclude_keys` drops (platform, key) pairs (hidden + already-served), except any
    in `pinned_keys` which always survive — the song-level analogue of the post
    hard-refresh working set.
    """
    if sort not in ALL_SONG_SORTS:
        sort = DEFAULT_SONG_SORT
    exclude_keys = exclude_keys or set()
    pinned_keys = pinned_keys or set()

    aggs = aggregate_songs(
        session, platform=platform, geo_tier=geo_tier, period_days=period_days
    )

    rows: list[dict[str, Any]] = []
    for (plat, key), agg in aggs.items():
        if (plat, key) in exclude_keys and (plat, key) not in pinned_keys:
            continue
        rows.append(agg.as_dict(sort))

    rows.sort(key=lambda s: (s["score"] is not None, s["score"] or 0.0), reverse=True)

    # Cross-platform "All" view: interleave so neither platform dominates the head
    # (post ranker does the same — magnitudes aren't comparable across platforms).
    if platform is None:
        rows = _interleave_by_platform(rows)

    return rows[:limit]


def _interleave_by_platform(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["platform"], []).append(r)
    order = sorted(
        groups,
        key=lambda p: (groups[p][0]["score"] is not None, groups[p][0]["score"] or 0.0),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    idx = 0
    while True:
        added = False
        for p in order:
            if idx < len(groups[p]):
                out.append(groups[p][idx])
                added = True
        if not added:
            break
        idx += 1
    return out


def song_post_ids(
    session: Session,
    platform: str,
    key: str,
    *,
    period_days: int = 30,
    geo_tier: Optional[str] = None,
) -> list[tuple[str, str, Optional[float]]]:
    """(platform, post_id, engagement_rate) for one song's posts, best first."""
    cutoff = _utcnow_naive() - timedelta(days=period_days)
    q = (
        session.query(Post)
        .filter(Post.platform == platform.lower())
        .filter(Post.first_seen_at >= cutoff)
    )
    if geo_tier:
        q = q.filter(Post.geo_tier == geo_tier)

    scored: list[tuple[str, str, Optional[float]]] = []
    for post in q.all():
        if song_key_for(post.sound_id, post.sound_name) != key:
            continue
        snap = (
            session.query(PostSnapshot)
            .filter_by(platform=post.platform, platform_post_id=post.platform_post_id)
            .order_by(PostSnapshot.fetched_at.desc())
            .first()
        )
        if snap is None:
            continue
        rate = _score_engagement_rate(snap, post.platform)
        sort_val = rate if rate is not None else (
            _volume_metric(snap, post.platform) or 0.0
        )
        scored.append((post.platform, post.platform_post_id, rate, sort_val))  # type: ignore[arg-type]

    scored.sort(key=lambda t: t[3], reverse=True)
    return [(p, i, r) for (p, i, r, _) in scored]


def song_meta(
    session: Session,
    platform: str,
    key: str,
    *,
    period_days: int = 30,
    geo_tier: Optional[str] = None,
    sort: SongSortKey = DEFAULT_SONG_SORT,
) -> Optional[dict[str, Any]]:
    """The aggregate dict for a single song, or None if it has no posts in window."""
    aggs = aggregate_songs(
        session, platform=platform, geo_tier=geo_tier, period_days=period_days
    )
    agg = aggs.get((platform.lower(), key))
    if agg is None:
        return None
    return agg.as_dict(sort)
