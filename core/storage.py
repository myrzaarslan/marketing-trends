"""SQLAlchemy storage layer for the marketing-trends core spine.

All DB schema is owned here — the spine owns all DDL including post_content
(Layer-3 surface) even though the enrichment agent writes those rows.

Adapters NEVER import this module; they return dataclasses and core persists them.

See docs/CORE-SPINE.md for the full schema rationale.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from core.schema import GeoTier, PostRecord, WatchedAccount

# ---------------------------------------------------------------------------
# Engine + session factory
# ---------------------------------------------------------------------------

_DB_PATH = Path(__file__).parent.parent / "data" / "trends.db"


def _db_url(path: Path = _DB_PATH) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path}"


def make_engine(db_path: Path = _DB_PATH):
    engine = create_engine(_db_url(db_path), echo=False, future=True)
    # Enable WAL mode for concurrent readers while the ingestion process writes
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    return engine


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal()


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class Post(Base):
    """One row per unique post. PK = (platform, platform_post_id)."""

    __tablename__ = "posts"

    platform = Column(String(32), primary_key=True, nullable=False)
    platform_post_id = Column(String(128), primary_key=True, nullable=False)
    account_handle = Column(String(128), nullable=False, index=True)
    url = Column(Text, nullable=False)
    media_type = Column(String(32), nullable=False)

    # Slow-changing content fields
    caption = Column(Text, nullable=True)
    hashtags = Column(Text, nullable=True)  # JSON array
    sound_id = Column(String(128), nullable=True)
    sound_name = Column(Text, nullable=True)

    # Thumbnail for EVERY post (downloaded at ingestion — no dead cards ever)
    thumbnail_path = Column(Text, nullable=True)

    # Timestamps
    posted_at = Column(DateTime, nullable=True)
    first_seen_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False)

    # Geo tier (from the account watchlist or discovery context)
    geo_tier = Column(String(16), nullable=True, index=True)

    __table_args__ = (
        Index("ix_posts_geo_first_seen", "geo_tier", "first_seen_at"),
        Index("ix_posts_platform_handle", "platform", "account_handle"),
    )


class PostSnapshot(Base):
    """One row per observation — the time series. Appended every run."""

    __tablename__ = "post_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(32), nullable=False, index=True)
    platform_post_id = Column(String(128), nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False)

    # Engagement counts (None where the platform doesn't expose the signal)
    view_count = Column(Integer, nullable=True)
    like_count = Column(Integer, nullable=True)
    comment_count = Column(Integer, nullable=True)
    share_count = Column(Integer, nullable=True)
    save_count = Column(Integer, nullable=True)
    author_follower_count = Column(Integer, nullable=True)

    # Which persona / seed / watchlist session surfaced this post
    source = Column(String(128), nullable=True)

    # FULL original platform payload — never drop fields
    raw = Column(Text, nullable=False)  # JSON

    __table_args__ = (
        ForeignKeyConstraint(
            ["platform", "platform_post_id"],
            ["posts.platform", "posts.platform_post_id"],
            ondelete="CASCADE",
        ),
        Index("ix_snapshots_post", "platform", "platform_post_id"),
        Index("ix_snapshots_fetched", "platform", "fetched_at"),
    )


class Account(Base):
    """Watchlist + discovered accounts."""

    __tablename__ = "accounts"

    handle = Column(String(128), primary_key=True, nullable=False)
    platform = Column(String(32), primary_key=True, nullable=False)
    segment = Column(String(64), nullable=True)
    geo_tier = Column(String(16), nullable=True)
    platform_account_id = Column(String(128), nullable=True)
    on_watchlist = Column(Boolean, nullable=False, default=False)

    __table_args__ = (Index("ix_accounts_platform", "platform"),)


class PostContent(Base):
    """Layer-3 surface — RESERVED for the enrichment track.

    The spine owns this DDL; the enrichment agent writes rows only via
    write_post_content() below. Defined here so the spine ships Layer-3-ready.
    """

    __tablename__ = "post_content"

    platform = Column(String(32), primary_key=True, nullable=False)
    platform_post_id = Column(String(128), primary_key=True, nullable=False)

    # JSON list of relative paths under data/media/<platform>/<platform_post_id>/
    media_paths = Column(Text, nullable=True)
    caption = Column(Text, nullable=True)
    spoiler_text = Column(Text, nullable=True)
    sound_id = Column(String(128), nullable=True)
    sound_name = Column(Text, nullable=True)
    sound_author = Column(Text, nullable=True)
    author_display_name = Column(Text, nullable=True)

    extracted_at = Column(DateTime, nullable=True)
    # pending | done | expired_url_miss
    status = Column(String(32), nullable=False, default="pending")

    __table_args__ = (
        ForeignKeyConstraint(
            ["platform", "platform_post_id"],
            ["posts.platform", "posts.platform_post_id"],
            ondelete="CASCADE",
        ),
    )


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------


def init_db(db_path: Path = _DB_PATH) -> None:
    """Create all tables (idempotent — safe to call on every startup)."""
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)


# ---------------------------------------------------------------------------
# Write helpers (called by ingest.py and enrichment)
# ---------------------------------------------------------------------------


def upsert_post(session: Session, record: PostRecord, source: str) -> None:
    """Upsert posts row + append a post_snapshots row.

    Insert a new post (setting first_seen_at) or update last_seen_at and any
    changed static fields on an existing one. Always appends a snapshot row.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fetched = record.fetched_at.replace(tzinfo=None) if record.fetched_at.tzinfo else record.fetched_at

    existing = session.get(Post, (record.platform, record.platform_post_id))
    if existing is None:
        post = Post(
            platform=record.platform,
            platform_post_id=record.platform_post_id,
            account_handle=record.account_handle,
            url=record.url,
            media_type=record.media_type,
            caption=record.caption,
            hashtags=json.dumps(record.hashtags or []),
            sound_id=record.sound_id,
            sound_name=record.sound_name,
            thumbnail_path=None,  # set later by download step
            posted_at=record.posted_at.replace(tzinfo=None) if record.posted_at and record.posted_at.tzinfo else record.posted_at,
            first_seen_at=fetched,
            last_seen_at=fetched,
            geo_tier=record.geo_tier,
        )
        session.add(post)
    else:
        # Refresh mutable fields
        existing.last_seen_at = fetched
        existing.account_handle = record.account_handle or existing.account_handle
        existing.caption = record.caption or existing.caption
        existing.url = record.url or existing.url
        existing.geo_tier = record.geo_tier or existing.geo_tier
        if record.hashtags:
            existing.hashtags = json.dumps(record.hashtags)
        if record.sound_id:
            existing.sound_id = record.sound_id
        if record.sound_name:
            existing.sound_name = record.sound_name

    # Always append a snapshot (the time series is the point)
    snap = PostSnapshot(
        platform=record.platform,
        platform_post_id=record.platform_post_id,
        fetched_at=fetched,
        view_count=record.view_count,
        like_count=record.like_count,
        comment_count=record.comment_count,
        share_count=record.share_count,
        save_count=record.save_count,
        author_follower_count=record.author_follower_count,
        source=source,
        raw=json.dumps(record.raw),
    )
    session.add(snap)


def set_thumbnail_path(
    session: Session, platform: str, platform_post_id: str, path: str
) -> None:
    """Set posts.thumbnail_path after thumbnail download.

    Called by the ingest download step; also callable by the enrichment track.
    """
    post = session.get(Post, (platform, platform_post_id))
    if post is not None:
        post.thumbnail_path = path


def upsert_account(session: Session, account: WatchedAccount) -> None:
    """Upsert an account into the accounts table."""
    existing = session.get(Account, (account.handle, account.platform))
    if existing is None:
        session.add(
            Account(
                handle=account.handle,
                platform=account.platform,
                segment=account.segment,
                geo_tier=account.geo_tier,
                platform_account_id=account.platform_account_id,
                on_watchlist=True,
            )
        )
    else:
        existing.segment = account.segment or existing.segment
        existing.geo_tier = account.geo_tier or existing.geo_tier
        existing.on_watchlist = True


def write_post_content(
    session: Session,
    platform: str,
    platform_post_id: str,
    *,
    media_paths: list[str] | None = None,
    caption: str | None = None,
    spoiler_text: str | None = None,
    sound_id: str | None = None,
    sound_name: str | None = None,
    sound_author: str | None = None,
    author_display_name: str | None = None,
    status: str = "done",
) -> None:
    """Write or update a post_content row (called by the enrichment track).

    Idempotent: upserts on (platform, platform_post_id). The enrichment agent
    MUST use this function — it must never invent its own schema writes.
    """
    extracted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    existing = session.get(PostContent, (platform, platform_post_id))
    if existing is None:
        session.add(
            PostContent(
                platform=platform,
                platform_post_id=platform_post_id,
                media_paths=json.dumps(media_paths or []),
                caption=caption,
                spoiler_text=spoiler_text,
                sound_id=sound_id,
                sound_name=sound_name,
                sound_author=sound_author,
                author_display_name=author_display_name,
                extracted_at=extracted_at,
                status=status,
            )
        )
    else:
        if media_paths is not None:
            existing.media_paths = json.dumps(media_paths)
        if caption is not None:
            existing.caption = caption
        if spoiler_text is not None:
            existing.spoiler_text = spoiler_text
        if sound_id is not None:
            existing.sound_id = sound_id
        if sound_name is not None:
            existing.sound_name = sound_name
        if sound_author is not None:
            existing.sound_author = sound_author
        if author_display_name is not None:
            existing.author_display_name = author_display_name
        existing.extracted_at = extracted_at
        existing.status = status


# ---------------------------------------------------------------------------
# Read helpers (used by ranker + API)
# ---------------------------------------------------------------------------


def get_post_with_latest_snapshot(
    session: Session, platform: str, platform_post_id: str
) -> tuple[Post, PostSnapshot] | None:
    """Return the post row and its most recent snapshot, or None."""
    post = session.get(Post, (platform, platform_post_id))
    if post is None:
        return None
    snap = (
        session.query(PostSnapshot)
        .filter_by(platform=platform, platform_post_id=platform_post_id)
        .order_by(PostSnapshot.fetched_at.desc())
        .first()
    )
    if snap is None:
        return None
    return post, snap


def count_distinct_snapshot_days(
    session: Session, platform: str, platform_post_id: str
) -> int:
    """Count distinct calendar days with at least one snapshot (for history gate)."""
    rows = (
        session.query(PostSnapshot.fetched_at)
        .filter_by(platform=platform, platform_post_id=platform_post_id)
        .all()
    )
    days = {r[0].date() if isinstance(r[0], datetime) else r[0] for r in rows}
    return len(days)
