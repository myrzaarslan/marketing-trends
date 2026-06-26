"""On-demand, multi-strategy ranker.

Computed at query time over stored SQLite rows — no precompute table needed for
the prototype. Degrades gracefully per platform signal availability (SIGNALS.md).

Sorts
-----
  raw_counts                — total engagement count (likes+comments+shares+saves) — always
  engagement_rate           — (likes+comments+shares+saves) / views  [DEFAULT]
  engagement_rate_followers — (likes+comments+shares+saves) / author_follower_count
  share_rate                — shares / views  (TikTok / X / Threads only)
  save_rate                 — saves / views   (TikTok only)
  velocity                  — Δviews across two snapshots / hours      [TIME gate]
  relative_baseline         — views vs median views of the same account [CORPUS gate]
  cross_persona             — COUNT(DISTINCT source) across snapshots    [SOURCE gate]

Gating (refined 2026-06-26) — only ONE sort is truly time-dependent:
  * velocity          needs ≥2 snapshots *separated in time* (TIME_GATE) — "rising"
                      is undefinable from a single point.
  * relative_baseline needs ≥MIN_BASELINE_POSTS *other posts of the same account*
                      (CORPUS gate) — NOT calendar time. A single scrape of an
                      account's recent posts makes the median computable on day one.
  * cross_persona     needs ≥MIN_BREADTH_SOURCES *distinct sources* (SOURCE gate) —
                      breadth accrues across persona/seed harvests, not calendar days.
  * engagement_rate_followers needs a non-null author_follower_count (always present
                      where the platform exposes follower counts; the only universal
                      denominator — works on Threads, which has no view count).
Each is grayed out in the UI independently when its own gate isn't met.

Period filter: `period_days` keys on `first_seen_at` (recency-to-us), NOT
`posted_at` — discovery posts skew evergreen (60-90 d old) so posted_at would
silently drop them. Decided 2026-06-26; see CORE-SPINE.md.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from core.storage import Post, PostContent, PostSnapshot

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

SortKey = Literal[
    "raw_counts",
    "engagement_rate",
    "engagement_rate_followers",
    "share_rate",
    "save_rate",
    "velocity",
    "relative_baseline",
    "cross_persona",
]

ALL_SORTS: tuple[SortKey, ...] = (
    "raw_counts",
    "engagement_rate",
    "engagement_rate_followers",
    "share_rate",
    "save_rate",
    "velocity",
    "relative_baseline",
    "cross_persona",
)

DEFAULT_SORT: SortKey = "engagement_rate"

# --- Independent gates (refined 2026-06-26) ---------------------------------
# Each gated sort has its OWN dependency; they are NOT the same "history" flag.
MIN_VELOCITY_SNAPSHOTS = 2  # velocity needs a 2nd point in time
MIN_BASELINE_POSTS = 3      # relative_baseline needs N other posts of the account
MIN_BREADTH_SOURCES = 2     # cross_persona needs N distinct vantage points

# Kept for the coarse /digest/meta matrix + docs (distinct snapshot DAYS).
HISTORY_GATE_DAYS = 3

TIME_GATED: frozenset[SortKey] = frozenset({"velocity"})
CORPUS_GATED: frozenset[SortKey] = frozenset({"relative_baseline"})
SOURCE_GATED: frozenset[SortKey] = frozenset({"cross_persona"})
# Union of everything that depends on accumulated data (used by the coarse meta).
HISTORY_GATED: frozenset[SortKey] = TIME_GATED | CORPUS_GATED | SOURCE_GATED

# Sorts unavailable on each platform (missing signals)
PLATFORM_UNAVAILABLE: dict[str, frozenset[SortKey]] = {
    "instagram": frozenset({"share_rate", "save_rate"}),
    "threads": frozenset({"save_rate"}),
    "x": frozenset({"save_rate"}),
    "tiktok": frozenset(),
}

# Platforms where engagement_rate is unreliable due to missing views
# (Threads has no view count at all)
NO_VIEW_PLATFORMS: frozenset[str] = frozenset({"threads"})


# ---------------------------------------------------------------------------
# Availability metadata (sent to the UI so it can gray out unavailable sorts)
# ---------------------------------------------------------------------------


def _availability_for_post(
    platform: Optional[str],
    *,
    snapshot_count: int,
    account_post_count: int,
    distinct_source_count: int,
    has_follower_count: bool,
) -> dict[SortKey, bool]:
    """Precise per-post availability: each gated sort checks its OWN dependency."""
    unavail = PLATFORM_UNAVAILABLE.get(platform or "", frozenset())
    result: dict[SortKey, bool] = {}
    for key in ALL_SORTS:
        if key in unavail:
            result[key] = False
        elif key in TIME_GATED:
            result[key] = snapshot_count >= MIN_VELOCITY_SNAPSHOTS
        elif key in CORPUS_GATED:
            result[key] = account_post_count >= MIN_BASELINE_POSTS
        elif key in SOURCE_GATED:
            result[key] = distinct_source_count >= MIN_BREADTH_SOURCES
        elif key == "engagement_rate_followers":
            result[key] = has_follower_count
        else:
            result[key] = True
    return result


def sort_availability(
    platform: Optional[str],
    has_history: bool = False,
) -> dict[SortKey, bool]:
    """Coarse, platform-level availability matrix (for GET /digest/meta).

    `has_history` is a single rough flag: True means "enough data has accumulated"
    and unlocks all three accumulated-data sorts at once. Per-post graying uses the
    precise `_availability_for_post` instead (different gate per sort). Follower-rate
    is treated as available at the platform level (gated per-post on follower_count).
    """
    return _availability_for_post(
        platform,
        snapshot_count=MIN_VELOCITY_SNAPSHOTS if has_history else 1,
        account_post_count=MIN_BASELINE_POSTS if has_history else 1,
        distinct_source_count=MIN_BREADTH_SOURCES if has_history else 1,
        has_follower_count=True,
    )


def real_sort_availability(
    session: Session, platform: Optional[str] = None
) -> dict[SortKey, bool]:
    """DB-backed availability for GET /digest/meta — reflects what the corpus supports.

    Unlike the coarse `sort_availability`, this inspects actual data so a gated sort
    is enabled when *any* qualifying post/account exists (per-post graying in rank()
    still applies to individual cards):
      * velocity          — any post with >= MIN_VELOCITY_SNAPSHOTS snapshots
      * relative_baseline — any account with >= MIN_BASELINE_POSTS posts
      * cross_persona     — any post with >= MIN_BREADTH_SOURCES distinct sources
      * engagement_rate_followers — any snapshot with a non-null follower count
    """
    from sqlalchemy import distinct, func

    def _pfilter(q, col):
        return q.filter(col == platform) if platform else q

    # velocity: max snapshots for a single post
    vel_q = _pfilter(
        session.query(func.count(PostSnapshot.id)).group_by(
            PostSnapshot.platform, PostSnapshot.platform_post_id
        ),
        PostSnapshot.platform,
    )
    has_velocity = any(r[0] >= MIN_VELOCITY_SNAPSHOTS for r in vel_q.all())

    # relative_baseline: any account with enough posts
    base_q = _pfilter(
        session.query(func.count(Post.platform_post_id)).group_by(
            Post.platform, Post.account_handle
        ),
        Post.platform,
    )
    has_baseline = any(r[0] >= MIN_BASELINE_POSTS for r in base_q.all())

    # cross_persona: any post observed from enough distinct sources
    src_q = _pfilter(
        session.query(func.count(distinct(PostSnapshot.source))).group_by(
            PostSnapshot.platform, PostSnapshot.platform_post_id
        ),
        PostSnapshot.platform,
    )
    has_breadth = any(r[0] >= MIN_BREADTH_SOURCES for r in src_q.all())

    has_followers = (
        _pfilter(
            session.query(PostSnapshot.id).filter(
                PostSnapshot.author_follower_count.isnot(None)
            ),
            PostSnapshot.platform,
        ).first()
        is not None
    )

    unavail = PLATFORM_UNAVAILABLE.get(platform or "", frozenset())
    result: dict[SortKey, bool] = {}
    for key in ALL_SORTS:
        if key in unavail:
            result[key] = False
        elif key in TIME_GATED:
            result[key] = has_velocity
        elif key in CORPUS_GATED:
            result[key] = has_baseline
        elif key in SOURCE_GATED:
            result[key] = has_breadth
        elif key == "engagement_rate_followers":
            result[key] = has_followers
        else:
            result[key] = True
    return result


# ---------------------------------------------------------------------------
# Score computation helpers
# ---------------------------------------------------------------------------


def _safe_div(num: Optional[float | int], denom: Optional[float | int]) -> Optional[float]:
    if num is None or denom is None or denom == 0:
        return None
    return num / denom


def _engagement_numerator(snap: PostSnapshot, platform: str) -> Optional[float]:
    """Sum of available engagement signals for a platform."""
    total = 0
    has = False
    for col in (snap.like_count, snap.comment_count):
        if col is not None:
            total += col
            has = True
    # shares: TikTok / X / Threads
    if platform not in ("instagram",) and snap.share_count is not None:
        total += snap.share_count
        has = True
    # saves: TikTok only
    if platform == "tiktok" and snap.save_count is not None:
        total += snap.save_count
        has = True
    return float(total) if has else None


def _score_engagement_rate(snap: PostSnapshot, platform: str) -> Optional[float]:
    num = _engagement_numerator(snap, platform)
    if platform in NO_VIEW_PLATFORMS:
        # No view count → just use raw numerator as proxy
        return num
    return _safe_div(num, snap.view_count)


def _score_engagement_rate_followers(snap: PostSnapshot, platform: str) -> Optional[float]:
    """Engagement ÷ author follower count — the classic influencer-marketing rate.

    The only denominator present on all four platforms (Threads has no views), so it
    works as a universal cross-platform normalizer. Computable from a SINGLE snapshot —
    no history needed.
    """
    num = _engagement_numerator(snap, platform)
    return _safe_div(num, snap.author_follower_count)


def _score_raw_counts(snap: PostSnapshot, platform: str) -> Optional[float]:
    return _engagement_numerator(snap, platform)


def _score_share_rate(snap: PostSnapshot, platform: str) -> Optional[float]:
    if platform in ("instagram",):
        return None
    return _safe_div(snap.share_count, snap.view_count)


def _score_save_rate(snap: PostSnapshot, platform: str) -> Optional[float]:
    if platform != "tiktok":
        return None
    return _safe_div(snap.save_count, snap.view_count)


def _score_velocity(
    session: Session, platform: str, post_id: str
) -> Optional[float]:
    """Δviews / hours between latest two snapshots."""
    snaps = (
        session.query(PostSnapshot)
        .filter_by(platform=platform, platform_post_id=post_id)
        .order_by(PostSnapshot.fetched_at.desc())
        .limit(2)
        .all()
    )
    if len(snaps) < 2:
        return None
    newer, older = snaps[0], snaps[1]
    if newer.view_count is None or older.view_count is None:
        # Fall back to likes delta if no views
        if newer.like_count is None or older.like_count is None:
            return None
        delta = newer.like_count - older.like_count
    else:
        delta = newer.view_count - older.view_count
    hours = (newer.fetched_at - older.fetched_at).total_seconds() / 3600
    if hours < 0.01:
        return None
    return delta / hours


def _score_relative_baseline(
    session: Session, platform: str, post_id: str, account_handle: str
) -> Optional[float]:
    """Latest views / median views for this account's posts."""
    # Latest snapshot for this post
    latest = (
        session.query(PostSnapshot)
        .filter_by(platform=platform, platform_post_id=post_id)
        .order_by(PostSnapshot.fetched_at.desc())
        .first()
    )
    if latest is None or latest.view_count is None:
        return None

    # Median view_count across all snapshots for this account's posts (latest snap per post)
    # Simple approach: latest snapshot per post for the same account
    account_posts = (
        session.query(Post.platform_post_id)
        .filter_by(platform=platform, account_handle=account_handle)
        .all()
    )
    account_post_ids = [r[0] for r in account_posts]
    if not account_post_ids:
        return None

    views_list = []
    for pid in account_post_ids:
        snap = (
            session.query(PostSnapshot.view_count)
            .filter_by(platform=platform, platform_post_id=pid)
            .order_by(PostSnapshot.fetched_at.desc())
            .first()
        )
        if snap and snap[0] is not None:
            views_list.append(snap[0])

    if len(views_list) < 2:
        return None
    median = statistics.median(views_list)
    if median == 0:
        return None
    return latest.view_count / median


def _score_cross_persona(
    session: Session, platform: str, post_id: str
) -> Optional[float]:
    """COUNT(DISTINCT source) across all snapshots for this post."""
    result = (
        session.query(func.count(func.distinct(PostSnapshot.source)))
        .filter_by(platform=platform, platform_post_id=post_id)
        .scalar()
    )
    return float(result) if result is not None else None


def _snapshot_day_count(session: Session, platform: str, post_id: str) -> int:
    """Count of distinct calendar days with at least one snapshot."""
    rows = (
        session.query(PostSnapshot.fetched_at)
        .filter_by(platform=platform, platform_post_id=post_id)
        .all()
    )
    days = {r[0].date() for r in rows}
    return len(days)


def _snapshot_count(session: Session, platform: str, post_id: str) -> int:
    """Total snapshots for this post (velocity gate — needs a 2nd point in time)."""
    return (
        session.query(func.count(PostSnapshot.id))
        .filter_by(platform=platform, platform_post_id=post_id)
        .scalar()
        or 0
    )


def _account_post_count(session: Session, platform: str, account_handle: str) -> int:
    """How many posts we hold for this account (relative_baseline corpus gate)."""
    if not account_handle:
        return 0
    return (
        session.query(func.count(Post.platform_post_id))
        .filter_by(platform=platform, account_handle=account_handle)
        .scalar()
        or 0
    )


def _distinct_source_count(session: Session, platform: str, post_id: str) -> int:
    """Distinct vantage points that surfaced this post (cross_persona gate)."""
    return (
        session.query(func.count(func.distinct(PostSnapshot.source)))
        .filter_by(platform=platform, platform_post_id=post_id)
        .scalar()
        or 0
    )


# ---------------------------------------------------------------------------
# Main ranking function
# ---------------------------------------------------------------------------


def rank(
    session: Session,
    *,
    platform: Optional[str] = None,
    geo_tier: Optional[str] = None,
    period_days: int = 30,
    sort: SortKey = DEFAULT_SORT,
    limit: int = 50,
    top_n_only: bool = False,
    top_n: int = 25,
) -> list[dict[str, Any]]:
    """Return ranked digest cards.

    Parameters
    ----------
    platform    Filter to a single platform (None = all).
    geo_tier    Filter to a geo tier (None = all).
    period_days Posts whose `first_seen_at` is within this many days from now.
    sort        Ranking strategy.
    limit       Max rows to return.
    top_n_only  If True return only top-N (used internally by ingest).
    top_n       How many posts to return when top_n_only is True.
    """
    # Period window based on first_seen_at (recency-to-us)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=period_days)

    q = session.query(Post).filter(Post.first_seen_at >= cutoff)
    if platform:
        q = q.filter(Post.platform == platform.lower())
    if geo_tier:
        q = q.filter(Post.geo_tier == geo_tier)

    posts = q.all()

    cards = []
    for post in posts:
        # Latest snapshot
        snap = (
            session.query(PostSnapshot)
            .filter_by(platform=post.platform, platform_post_id=post.platform_post_id)
            .order_by(PostSnapshot.fetched_at.desc())
            .first()
        )
        if snap is None:
            continue

        plat = post.platform
        pid = post.platform_post_id

        # Per-post gate inputs (each gated sort depends on a DIFFERENT one)
        snap_days = _snapshot_day_count(session, plat, pid)
        snap_count = _snapshot_count(session, plat, pid)
        acct_posts = _account_post_count(session, plat, post.account_handle)
        src_count = _distinct_source_count(session, plat, pid)
        has_history = snap_days >= HISTORY_GATE_DAYS  # kept for the card field

        avail = _availability_for_post(
            plat,
            snapshot_count=snap_count,
            account_post_count=acct_posts,
            distinct_source_count=src_count,
            has_follower_count=snap.author_follower_count is not None,
        )

        # Compute the requested score; degrade to the default if its gate isn't met
        score: Optional[float]
        effective_sort = sort if avail.get(sort, False) else DEFAULT_SORT

        if effective_sort == "engagement_rate":
            score = _score_engagement_rate(snap, plat)
        elif effective_sort == "engagement_rate_followers":
            score = _score_engagement_rate_followers(snap, plat)
        elif effective_sort == "raw_counts":
            score = _score_raw_counts(snap, plat)
        elif effective_sort == "share_rate":
            score = _score_share_rate(snap, plat)
        elif effective_sort == "save_rate":
            score = _score_save_rate(snap, plat)
        elif effective_sort == "velocity":
            score = _score_velocity(session, plat, pid)
        elif effective_sort == "relative_baseline":
            score = _score_relative_baseline(session, plat, pid, post.account_handle)
        elif effective_sort == "cross_persona":
            score = _score_cross_persona(session, plat, pid)
        else:
            score = _score_engagement_rate(snap, plat)

        # Check if enriched (Layer-3)
        has_content = (
            session.get(PostContent, (plat, post.platform_post_id)) is not None
        )

        cards.append(
            {
                "platform": plat,
                "platform_post_id": post.platform_post_id,
                "account_handle": post.account_handle,
                "url": post.url,
                "caption": post.caption,
                "hashtags": json.loads(post.hashtags or "[]"),
                "sound_id": post.sound_id,
                "sound_name": post.sound_name,
                "media_type": post.media_type,
                "geo_tier": post.geo_tier,
                "thumbnail_path": post.thumbnail_path,
                "posted_at": post.posted_at.isoformat() if post.posted_at else None,
                "first_seen_at": post.first_seen_at.isoformat(),
                "last_seen_at": post.last_seen_at.isoformat(),
                # Latest stats
                "view_count": snap.view_count,
                "like_count": snap.like_count,
                "comment_count": snap.comment_count,
                "share_count": snap.share_count,
                "save_count": snap.save_count,
                "author_follower_count": snap.author_follower_count,
                # Ranking metadata
                "score": score,
                "sort_used": effective_sort,
                "sort_requested": sort,
                "snapshot_days": snap_days,
                "snapshot_count": snap_count,
                "account_post_count": acct_posts,
                "distinct_source_count": src_count,
                "has_history": has_history,
                "has_content": has_content,
                # Precise per-post availability (each gated sort checks its own gate)
                "sort_availability": avail,
            }
        )

    # Sort descending by score (None scores sink to bottom)
    cards.sort(key=lambda c: (c["score"] is not None, c["score"] or 0), reverse=True)

    n = top_n if top_n_only else limit

    # When no single platform is selected, scores are NOT comparable across
    # platforms (view-based rates are 0–1 while no-view platforms like Threads
    # fall back to raw counts in the thousands). A single global sort lets one
    # platform crowd out the others (CORE-SPINE.md: "top-N per platform×geo, NOT
    # global"). So interleave the per-platform rankings round-robin instead.
    if platform is None:
        cards = _interleave_by_platform(cards)

    return cards[:n]


def _interleave_by_platform(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-robin the already-sorted cards across platforms.

    Each platform keeps its own descending order; we take the #1 of each
    platform, then the #2 of each, etc. — so no single platform can dominate
    the head of an unfiltered digest.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in cards:
        groups.setdefault(c["platform"], []).append(c)

    # Stable platform order = platforms that currently have the strongest #1.
    order = sorted(
        groups,
        key=lambda p: (
            groups[p][0]["score"] is not None,
            groups[p][0]["score"] or 0,
        ),
        reverse=True,
    )

    interleaved: list[dict[str, Any]] = []
    idx = 0
    while True:
        added = False
        for p in order:
            if idx < len(groups[p]):
                interleaved.append(groups[p][idx])
                added = True
        if not added:
            break
        idx += 1
    return interleaved


def top_n_ids(
    session: Session,
    *,
    platform: Optional[str] = None,
    geo_tier: Optional[str] = None,
    period_days: int = 30,
    n: int = 25,
) -> list[tuple[str, str]]:
    """Return top-N (platform, platform_post_id) tuples for the enrichment step.

    Uses the default engagement-rate sort, top-N per (platform × geo) as decided
    in CORE-SPINE.md.
    """
    cards = rank(
        session,
        platform=platform,
        geo_tier=geo_tier,
        period_days=period_days,
        sort=DEFAULT_SORT,
        top_n_only=True,
        top_n=n,
    )
    return [(c["platform"], c["platform_post_id"]) for c in cards]
