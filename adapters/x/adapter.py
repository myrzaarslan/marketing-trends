"""X (Twitter) platform adapter — feasibility-spike implementation.

Data path: the **syndication / embed timeline endpoint** that powers X's
embeddable profile timelines:

    https://syndication.twitter.com/srv/timeline-profile/screen-name/<handle>

This is the one free, no-auth, no-login path that still works from a home IP in
2026 (snscrape is broken; public Nitter instances are dead — see README). It
returns the ~20 most recent tweets as rich JSON embedded in a Next.js
``__NEXT_DATA__`` script tag, with per-tweet engagement counts, entities, and
media. No paid API, no account cookies. See adapters/x/README.md for the full
go/no-go writeup and known limitations.

This module only FETCHES and NORMALIZES. It never persists, schedules, or
decides virality (per docs/INGESTION-CONTRACT.md). The complete original tweet
payload is preserved untouched in ``PostRecord.raw``.

OPTIONAL deepening (``prefer_graphql=True``): the syndication path cannot see
``view_count``. A guest-token GraphQL path can (see graphql.py) — at the cost of
more fragility. It is OFF by default; when on, GraphQL is tried first and falls
back to syndication on any failure, so enabling it never makes things worse.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

from core.adapter import PlatformAdapter
from core.schema import GeoTier, MediaType, PostRecord, Trend, WatchedAccount
from adapters.x.graphql import XGraphQLClient, XGraphQLError

_TIMELINE_URL = (
    "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
    "?showReplies=false"  # showReplies=true returns an empty timeline (endpoint quirk)
)

# Embed endpoint needs a browser-ish UA or it returns an empty/blocked shell.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# The tweet JSON is embedded in the page's Next.js data island.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

# X's created_at format, e.g. "Wed Jun 24 21:26:09 +0000 2026".
_CREATED_AT_FMT = "%a %b %d %H:%M:%S %z %Y"


class XAdapter(PlatformAdapter):
    platform = "x"

    def __init__(self, *, timeout: float = 15.0, prefer_graphql: bool = False) -> None:
        self._timeout = timeout
        self._prefer_graphql = prefer_graphql
        self._graphql = XGraphQLClient(timeout=timeout) if prefer_graphql else None

    # -- public API ---------------------------------------------------------

    def fetch_account_posts(
        self, account: WatchedAccount, limit: int = 30
    ) -> list[PostRecord]:
        """Recent tweets for one Watched Account, newest first.

        Default path is syndication (robust, no auth). With ``prefer_graphql``
        the guest-token GraphQL path is tried first to additionally recover
        ``view_count``, falling back to syndication on any failure.

        NOTE: the syndication embed endpoint exposes only the most recent ~20
        tweets and has no usable pagination cursor, so ``limit`` is effectively
        capped at what a single page returns. Asking for more is not an error —
        you just get what's available.
        """
        fetched_at = datetime.now(timezone.utc)

        if self._graphql is not None:
            try:
                results = self._graphql.get_user_tweets(
                    self._graphql.get_user_rest_id(account.handle), count=max(limit, 20)
                )
                records = [
                    self._graphql_to_post_record(r, account, fetched_at) for r in results
                ]
                if records:  # empty => fall through to syndication
                    return records[:limit]
            except (XGraphQLError, KeyError, json.JSONDecodeError, urllib.error.URLError):
                pass  # fragile-by-design path failed; fall back to the robust one

        tweets = self._fetch_timeline(account.handle)
        records = [
            self._to_post_record(tweet, account, fetched_at) for tweet in tweets
        ]
        return records[:limit]

    def fetch_trends(self, geo_tier: GeoTier) -> list[Trend]:
        # X's trends ("What's happening") are location-based but the only access
        # paths are login-walled or behind the ~$200/mo+ paid API. There is no
        # free, no-auth trend source, so we return nothing (per the ingestion
        # contract, [] is a valid result). Do NOT spend the spike's time-box here.
        return []

    # -- fetching -----------------------------------------------------------

    def _fetch_timeline(self, handle: str) -> list[dict]:
        """Fetch and parse the embed timeline; returns raw tweet dicts."""
        url = _TIMELINE_URL.format(handle=handle.lstrip("@"))
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                html = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"X embed endpoint returned HTTP {e.code} for @{handle} "
                f"(account may be private, suspended, or rate-limited)"
            ) from e

        match = _NEXT_DATA_RE.search(html)
        if not match:
            raise RuntimeError(
                f"No __NEXT_DATA__ island in X embed response for @{handle} "
                f"(endpoint shape may have changed — see adapters/x/README.md)"
            )
        data = json.loads(match.group(1))
        try:
            entries = data["props"]["pageProps"]["timeline"]["entries"]
        except (KeyError, TypeError):
            return []
        return [
            e["content"]["tweet"]
            for e in entries
            if e.get("type") == "tweet" and "tweet" in e.get("content", {})
        ]

    # -- normalization ------------------------------------------------------

    def _to_post_record(
        self, tweet: dict, account: WatchedAccount, fetched_at: datetime
    ) -> PostRecord:
        """Normalize a syndication tweet dict (no view_count on this path)."""
        user = tweet.get("user") or {}
        handle = user.get("screen_name") or account.handle.lstrip("@")
        author_follower_count = self._to_int(user.get("followers_count"))
        return self._build_record(
            fields=tweet, raw=tweet, handle=handle, view_count=None,
            author_follower_count=author_follower_count,
            account=account, fetched_at=fetched_at,
        )

    def _graphql_to_post_record(
        self, result: dict, account: WatchedAccount, fetched_at: datetime
    ) -> PostRecord:
        """Normalize a GraphQL ``tweet_results.result`` node.

        Its ``legacy`` sub-object is field-for-field compatible with the
        syndication tweet shape, so the same builder handles it; the one extra
        is ``views.count``. The COMPLETE result node goes in ``raw``.
        """
        fields = result.get("legacy", {})
        core = (result.get("core") or {}).get("user_results", {}).get("result", {})
        core_legacy = core.get("legacy") or {}
        handle = (
            core_legacy.get("screen_name")
            or (core.get("core") or {}).get("screen_name")
            or account.handle.lstrip("@")
        )
        view_count = self._to_int((result.get("views") or {}).get("count"))
        author_follower_count = self._to_int(core_legacy.get("followers_count"))
        return self._build_record(
            fields=fields, raw=result, handle=handle, view_count=view_count,
            author_follower_count=author_follower_count,
            account=account, fetched_at=fetched_at,
        )

    def _build_record(
        self, *, fields: dict, raw: dict, handle: str, view_count: int | None,
        author_follower_count: int | None,
        account: WatchedAccount, fetched_at: datetime,
    ) -> PostRecord:
        """Shared normalizer. ``fields`` is a legacy-shaped tweet dict (same on
        both paths); ``raw`` is the complete original payload for that path."""
        post_id = fields["id_str"]
        return PostRecord(
            platform=self.platform,
            platform_post_id=post_id,
            account_handle=handle,
            url=self._permalink(fields, handle, post_id),
            fetched_at=fetched_at,
            media_type=self._media_type(fields),
            raw=raw,  # COMPLETE original payload, untouched (capture everything)
            posted_at=self._parse_created_at(fields.get("created_at")),
            caption=fields.get("full_text") or fields.get("text"),
            hashtags=self._hashtags(fields),
            # X has no "sound" concept; bookmark counts are never public.
            sound_id=None,
            sound_name=None,
            duration_sec=self._duration_sec(fields),
            view_count=view_count,  # None on syndication; populated via GraphQL
            like_count=fields.get("favorite_count"),
            comment_count=fields.get("reply_count"),
            share_count=fields.get("retweet_count"),  # reposts; quote_count stays in raw
            save_count=None,  # X bookmark counts are not public
            thumbnail_url=self._thumbnail_url(fields),
            geo_tier=account.geo_tier,
            author_follower_count=author_follower_count,  # from user object at fetch time
        )

    @staticmethod
    def _to_int(value: object) -> int | None:
        # GraphQL returns views.count as a string ("2104060"); coerce safely.
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _permalink(tweet: dict, handle: str, post_id: str) -> str:
        path = tweet.get("permalink")
        if path:
            return f"https://x.com{path}" if path.startswith("/") else path
        return f"https://x.com/{handle}/status/{post_id}"

    @staticmethod
    def _parse_created_at(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, _CREATED_AT_FMT)
        except ValueError:
            return None

    @staticmethod
    def _media_list(tweet: dict) -> list[dict]:
        # extended_entities carries the full media set (multi-photo, video);
        # entities.media only ever has the first item. Prefer the former.
        ext = (tweet.get("extended_entities") or {}).get("media")
        if ext:
            return ext
        return (tweet.get("entities") or {}).get("media") or []

    @classmethod
    def _media_type(cls, tweet: dict) -> MediaType:
        media = cls._media_list(tweet)
        if not media:
            return "text"
        types = {m.get("type") for m in media}
        if "video" in types or "animated_gif" in types:
            return "video"
        # all photos: one -> image, several -> carousel
        return "carousel" if len(media) > 1 else "image"

    @classmethod
    def _hashtags(cls, tweet: dict) -> list[str]:
        tags = (tweet.get("entities") or {}).get("hashtags") or []
        return [t["text"] for t in tags if t.get("text")]

    @classmethod
    def _duration_sec(cls, tweet: dict) -> float | None:
        for m in cls._media_list(tweet):
            info = m.get("video_info") or {}
            millis = info.get("duration_millis")
            if millis is not None:
                return millis / 1000.0
        return None

    @classmethod
    def _thumbnail_url(cls, tweet: dict) -> str | None:
        media = cls._media_list(tweet)
        if media:
            # For video this is the poster frame; for photos, the image itself.
            return media[0].get("media_url_https")
        return None
