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
    ForeignKey,
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


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Collection(Base):
    """A user-curated, named set of posts (title + description)."""

    __tablename__ = "collections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow_naive)
    updated_at = Column(DateTime, nullable=False, default=_utcnow_naive, onupdate=_utcnow_naive)


class CollectionItem(Base):
    """Membership of a post in a Collection (M:N posts <-> collections)."""

    __tablename__ = "collection_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(
        Integer,
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform = Column(String(32), nullable=False)
    platform_post_id = Column(String(128), nullable=False)
    added_at = Column(DateTime, nullable=False, default=_utcnow_naive)

    __table_args__ = (
        UniqueConstraint(
            "collection_id", "platform", "platform_post_id",
            name="uq_collection_item",
        ),
        ForeignKeyConstraint(
            ["platform", "platform_post_id"],
            ["posts.platform", "posts.platform_post_id"],
            ondelete="CASCADE",
        ),
        Index("ix_collection_items_post", "platform", "platform_post_id"),
    )


class PostNote(Base):
    """A single editable free-text note per post, shown everywhere the post appears."""

    __tablename__ = "post_notes"

    platform = Column(String(32), primary_key=True, nullable=False)
    platform_post_id = Column(String(128), primary_key=True, nullable=False)
    body = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, nullable=False, default=_utcnow_naive, onupdate=_utcnow_naive)

    __table_args__ = (
        ForeignKeyConstraint(
            ["platform", "platform_post_id"],
            ["posts.platform", "posts.platform_post_id"],
            ondelete="CASCADE",
        ),
    )


class PostFlag(Base):
    """Per-post user state that drives refresh: hidden, pinned, and last-served time.

    - hidden:        never show this post again (global, all lists).
    - pinned:        keep this post across a hard refresh (it survives the swap).
    - last_served_at: when this post was last shown in a digest — lets hard refresh
                      return previously-unseen posts and recycle least-recently-seen
                      ones once the unseen pool is exhausted.
    """

    __tablename__ = "post_flags"

    platform = Column(String(32), primary_key=True, nullable=False)
    platform_post_id = Column(String(128), primary_key=True, nullable=False)
    hidden = Column(Boolean, nullable=False, default=False)
    pinned = Column(Boolean, nullable=False, default=False)
    last_served_at = Column(DateTime, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["platform", "platform_post_id"],
            ["posts.platform", "posts.platform_post_id"],
            ondelete="CASCADE",
        ),
        Index("ix_post_flags_hidden", "hidden"),
        Index("ix_post_flags_pinned", "pinned"),
        Index("ix_post_flags_served", "last_served_at"),
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


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


def create_collection(session: Session, title: str, description: str | None = None) -> Collection:
    coll = Collection(title=title.strip() or "Untitled", description=description)
    session.add(coll)
    session.flush()
    return coll


def list_collections(session: Session) -> list[tuple[Collection, int]]:
    """All collections with their item counts, newest first."""
    colls = session.query(Collection).order_by(Collection.created_at.desc()).all()
    out: list[tuple[Collection, int]] = []
    for c in colls:
        count = (
            session.query(CollectionItem)
            .filter_by(collection_id=c.id)
            .count()
        )
        out.append((c, count))
    return out


def update_collection(
    session: Session, collection_id: int, *, title: str | None = None, description: str | None = None
) -> Collection | None:
    coll = session.get(Collection, collection_id)
    if coll is None:
        return None
    if title is not None:
        coll.title = title.strip() or coll.title
    if description is not None:
        coll.description = description
    return coll


def delete_collection(session: Session, collection_id: int) -> bool:
    coll = session.get(Collection, collection_id)
    if coll is None:
        return False
    # Items are removed via ON DELETE CASCADE, but SQLite needs the rows gone
    # explicitly when the FK action isn't honored for ORM-loaded objects.
    session.query(CollectionItem).filter_by(collection_id=collection_id).delete()
    session.delete(coll)
    return True


def add_to_collection(
    session: Session, collection_id: int, platform: str, platform_post_id: str
) -> bool:
    """Add a post to a collection. Idempotent — returns True if newly added."""
    existing = (
        session.query(CollectionItem)
        .filter_by(collection_id=collection_id, platform=platform, platform_post_id=platform_post_id)
        .first()
    )
    if existing is not None:
        return False
    session.add(
        CollectionItem(
            collection_id=collection_id,
            platform=platform,
            platform_post_id=platform_post_id,
        )
    )
    return True


def remove_from_collection(
    session: Session, collection_id: int, platform: str, platform_post_id: str
) -> bool:
    n = (
        session.query(CollectionItem)
        .filter_by(collection_id=collection_id, platform=platform, platform_post_id=platform_post_id)
        .delete()
    )
    return n > 0


def collection_post_ids(session: Session, collection_id: int) -> list[tuple[str, str]]:
    """(platform, post_id) for a collection, most-recently-added first."""
    rows = (
        session.query(CollectionItem.platform, CollectionItem.platform_post_id)
        .filter_by(collection_id=collection_id)
        .order_by(CollectionItem.added_at.desc())
        .all()
    )
    return [(r[0], r[1]) for r in rows]


def collection_ids_for_post(session: Session, platform: str, platform_post_id: str) -> list[int]:
    rows = (
        session.query(CollectionItem.collection_id)
        .filter_by(platform=platform, platform_post_id=platform_post_id)
        .all()
    )
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Notes (one editable note per post)
# ---------------------------------------------------------------------------


def set_note(session: Session, platform: str, platform_post_id: str, body: str) -> None:
    """Upsert a post's note. An empty/blank body deletes the note."""
    existing = session.get(PostNote, (platform, platform_post_id))
    if not body or not body.strip():
        if existing is not None:
            session.delete(existing)
        return
    if existing is None:
        session.add(PostNote(platform=platform, platform_post_id=platform_post_id, body=body))
    else:
        existing.body = body


def get_note(session: Session, platform: str, platform_post_id: str) -> str | None:
    note = session.get(PostNote, (platform, platform_post_id))
    return note.body if note else None


def notes_for(session: Session, ids: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Bulk-fetch notes for a set of (platform, post_id) pairs."""
    if not ids:
        return {}
    platforms = {p for p, _ in ids}
    rows = session.query(PostNote).filter(PostNote.platform.in_(platforms)).all()
    wanted = set(ids)
    return {
        (r.platform, r.platform_post_id): r.body
        for r in rows
        if (r.platform, r.platform_post_id) in wanted
    }


# ---------------------------------------------------------------------------
# Flags (hidden / pinned / seen) — drive refresh
# ---------------------------------------------------------------------------


def _get_or_create_flag(session: Session, platform: str, platform_post_id: str) -> PostFlag:
    flag = session.get(PostFlag, (platform, platform_post_id))
    if flag is None:
        flag = PostFlag(platform=platform, platform_post_id=platform_post_id)
        session.add(flag)
        session.flush()
    return flag


def set_hidden(session: Session, platform: str, platform_post_id: str, hidden: bool) -> None:
    _get_or_create_flag(session, platform, platform_post_id).hidden = hidden


def set_pinned(session: Session, platform: str, platform_post_id: str, pinned: bool) -> None:
    _get_or_create_flag(session, platform, platform_post_id).pinned = pinned


def mark_served(session: Session, ids: list[tuple[str, str]]) -> None:
    """Stamp last_served_at=now for each post (used by hard refresh seen-tracking)."""
    now = _utcnow_naive()
    for platform, post_id in ids:
        _get_or_create_flag(session, platform, post_id).last_served_at = now


def flag_ids(session: Session, *, hidden: bool | None = None, pinned: bool | None = None) -> set[tuple[str, str]]:
    """Set of (platform, post_id) matching the given hidden/pinned filters."""
    q = session.query(PostFlag.platform, PostFlag.platform_post_id)
    if hidden is not None:
        q = q.filter(PostFlag.hidden == hidden)
    if pinned is not None:
        q = q.filter(PostFlag.pinned == pinned)
    return {(r[0], r[1]) for r in q.all()}


def served_ids(session: Session) -> set[tuple[str, str]]:
    """Posts that have been shown at least once (last_served_at set)."""
    rows = (
        session.query(PostFlag.platform, PostFlag.platform_post_id)
        .filter(PostFlag.last_served_at.isnot(None))
        .all()
    )
    return {(r[0], r[1]) for r in rows}


def count_unseen_eligible(
    session: Session, *, exclude_hidden: bool = True
) -> int:
    """How many posts have never been served (and aren't hidden)."""
    served = served_ids(session)
    hidden = flag_ids(session, hidden=True) if exclude_hidden else set()
    blocked = served | hidden
    total = session.query(Post.platform, Post.platform_post_id).all()
    return sum(1 for r in total if (r[0], r[1]) not in blocked)


def recycle_oldest_served(session: Session, count: int) -> int:
    """Reset last_served_at=NULL for the `count` least-recently-served posts.

    Lets hard refresh keep producing content once the unseen pool is exhausted —
    the oldest-seen posts become 'unseen' again. Returns how many were recycled.
    """
    if count <= 0:
        return 0
    rows = (
        session.query(PostFlag)
        .filter(PostFlag.last_served_at.isnot(None))
        .order_by(PostFlag.last_served_at.asc())
        .limit(count)
        .all()
    )
    for r in rows:
        r.last_served_at = None
    return len(rows)


def flags_for(session: Session, ids: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """Bulk-fetch {hidden, pinned, last_served_at} for a set of posts."""
    if not ids:
        return {}
    platforms = {p for p, _ in ids}
    rows = session.query(PostFlag).filter(PostFlag.platform.in_(platforms)).all()
    wanted = set(ids)
    return {
        (r.platform, r.platform_post_id): {
            "hidden": bool(r.hidden),
            "pinned": bool(r.pinned),
            "last_served_at": r.last_served_at.isoformat() if r.last_served_at else None,
        }
        for r in rows
        if (r.platform, r.platform_post_id) in wanted
    }
