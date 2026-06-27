"""Instagram adapter — fetch Post Records for Watched Accounts via instagrapi.

Round two (see docs/REVERSE-ENGINEERING.md). We dropped `instaloader` (web
scraping, now 403s anonymously) for `instagrapi`, which speaks Instagram's
**mobile private API** (`i.instagram.com/api/v1/...`) — the same surface managed
providers use. It emulates an Android device and manages the session bearer,
`X-IG-App-ID`, `X-IG-WWW-Claim`, CSRF and signing for us. Richer payloads, and a
warmed device session is far more durable than scraping the web frontend.

What this adapter is and isn't (docs/INGESTION-CONTRACT.md + docs/adr/0001):

- It FETCHES and NORMALIZES. It does not persist, schedule, or judge virality.
- It imports only from ``core``. Signing/session/IP concerns live entirely inside
  this adapter; they never leak into ``core``.
- It needs a **logged-in burner** account — never a real/company account
  (ADR-0001's "account treadmill"). The account you log in with is the one that
  gets banned.

Anti-ban discipline (policy, not vibes):
- instagrapi's ``delay_range`` makes every private request sleep a randomized
  human-ish gap. We set it from ``min_delay``/``max_delay``.
- We do NOT retry into a wall. Challenge / rate-limit / login-wall / feedback
  exceptions are re-raised as :class:`SoftBlockError` — the caller stops, rests
  the burner/IP, and resumes later.

Capture everything: we hit the private ``feed/user/{id}/`` endpoint directly so we
can stash each COMPLETE raw API item in ``PostRecord.raw`` (instagrapi's parsed
``Media`` drops fields), then normalize that same raw item with
``extract_media_v1`` for convenient field access. A future "what is viral" rule
(OPEN-QUESTIONS Q-1) can be computed retroactively over the stored raw.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeError,
    ChallengeRequired,
    ClientForbiddenError,
    ClientThrottledError,
    ClientUnauthorizedError,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    PrivateAccount,
    ProxyAddressIsBlocked,
    RateLimitError,
    RecaptchaChallengeForm,
    ReloginAttemptExceeded,
    SelectContactPointRecoveryForm,
    SentryBlock,
    SubmitPhoneNumberForm,
    UserNotFound,
)
from instagrapi.extractors import extract_media_v1

from core.adapter import PlatformAdapter
from core.schema import GeoTier, MediaType, PostRecord, SoundRecord, Trend, WatchedAccount

# instagrapi media_type (int) -> our normalized MediaType. A reel is media_type 2
# (video) with product_type "clips"; reel-ness is flagged separately in extras.
_MEDIA_TYPE_TO_NORM: dict[int, MediaType] = {1: "image", 2: "video", 8: "carousel"}

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)

# Instagram is telling us to back off. None of these are retryable in a tight
# loop: challenge flows, rate limits, login/permission walls, anti-bot blocks.
_BACKOFF_EXCEPTIONS: tuple[type[Exception], ...] = (
    ChallengeRequired,
    ChallengeError,
    RecaptchaChallengeForm,
    SelectContactPointRecoveryForm,
    SubmitPhoneNumberForm,
    PleaseWaitFewMinutes,
    RateLimitError,
    ClientThrottledError,
    FeedbackRequired,
    SentryBlock,
    ProxyAddressIsBlocked,
    LoginRequired,
    ClientForbiddenError,
    ClientUnauthorizedError,
    ReloginAttemptExceeded,
)


class SoftBlockError(RuntimeError):
    """Instagram is asking us to back off (challenge / rate limit / login wall).

    NOT retryable in a tight loop. The treadmill cost is real (ADR-0001): stop,
    rest the burner account and/or rotate IP, and resume later. Re-raised from
    instagrapi's challenge/rate-limit/permission exceptions so callers don't have
    to know instagrapi's exception zoo.
    """


class InstagramAdapter(PlatformAdapter):
    """Pulls recent Post Records for a Watched Account via the mobile private API.

    Parameters
    ----------
    client:
        A pre-configured, logged-in ``instagrapi.Client`` (e.g. one you've warmed
        a burner into). If omitted, a fresh client is built and you must call
        :meth:`load_session` / :meth:`login` / :meth:`login_by_sessionid` before
        fetching — the private API rejects anonymous callers.
    min_delay, max_delay:
        Bounds (seconds) for instagrapi's automatic per-request delay. Defaults
        are deliberately slow. Lower at your own risk.
    """

    platform = "instagram"

    def __init__(
        self,
        client: Optional[Client] = None,
        *,
        min_delay: float = 3.0,
        max_delay: float = 8.0,
        hydrate_views: bool = True,
    ) -> None:
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError("require 0 <= min_delay <= max_delay")
        self.min_delay = min_delay
        self.max_delay = max_delay
        # Logged-in, IG omits play/view counts from the feed list (a
        # `has_views_fetching` flag instead) and serves them lazily per-media.
        # When True we hydrate a video's view_count with one extra media_info
        # call. Costs one call per video — set False for low-footprint pulls
        # where you don't need views.
        self.hydrate_views = hydrate_views
        self.cl = client or Client()
        # instagrapi sleeps a random gap in this range before each private call.
        self.cl.delay_range = [min_delay, max_delay]

    # ------------------------------------------------------------------ session

    def load_session(self, settings_file: Optional[str] = None) -> "InstagramAdapter":
        """Restore a warmed burner session from an instagrapi settings file.

        Preferred entry point. The settings JSON carries the emulated device,
        UUIDs and the authorization token, so reusing it looks like the same
        phone coming back — far less challenge-prone than logging in fresh.
        Create it once, out of band, with :meth:`login` + ``dump_settings`` (see
        README). Reads ``IG_SETTINGS_FILE`` when the arg is omitted.
        """
        settings_file = settings_file or os.environ.get("IG_SETTINGS_FILE")
        if not settings_file:
            raise ValueError("no settings_file given and IG_SETTINGS_FILE not set")
        if not os.path.exists(settings_file):
            raise FileNotFoundError(
                f"No instagrapi session at {settings_file!r}. Create one once with "
                f"login(...) then dump_settings() — see README."
            )
        self.cl.load_settings(settings_file)
        return self

    def login(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        *,
        settings_file: Optional[str] = None,
    ) -> "InstagramAdapter":
        """Full login for a BURNER account, persisting the session if asked.

        Reuses the device in ``settings_file`` when present (keeps the same
        emulated phone across logins — less suspicious), logs in, and dumps the
        refreshed session back to ``settings_file`` so subsequent runs can
        :meth:`load_session` instead. Reads ``IG_USERNAME``/``IG_PASSWORD``/
        ``IG_SETTINGS_FILE`` from the environment when args are omitted. Never a
        real or company account.
        """
        username = username or os.environ.get("IG_USERNAME")
        password = password or os.environ.get("IG_PASSWORD")
        settings_file = settings_file or os.environ.get("IG_SETTINGS_FILE")
        if not username or not password:
            raise ValueError("username/password required (or set IG_USERNAME/IG_PASSWORD)")
        if settings_file and os.path.exists(settings_file):
            self.cl.load_settings(settings_file)  # reuse device, drop stale auth
        try:
            self.cl.login(username, password)
        except _BACKOFF_EXCEPTIONS as err:
            raise self._as_softblock(err)
        if settings_file:
            self.cl.dump_settings(settings_file)
        return self

    def login_by_sessionid(self, sessionid: Optional[str] = None) -> "InstagramAdapter":
        """Authenticate from a raw burner ``sessionid`` cookie.

        Handy when you've pulled the cookie from a browser/app rather than doing
        a password login. Reads ``IG_SESSIONID`` when the arg is omitted.
        """
        sessionid = sessionid or os.environ.get("IG_SESSIONID")
        if not sessionid:
            raise ValueError("no sessionid given and IG_SESSIONID not set")
        try:
            self.cl.login_by_sessionid(sessionid)
        except _BACKOFF_EXCEPTIONS as err:
            raise self._as_softblock(err)
        return self

    # --------------------------------------------------------------- fetching

    def fetch_account_posts(
        self, account: WatchedAccount, limit: int = 30
    ) -> list[PostRecord]:
        """Recent posts for one Watched Account, newest first.

        Raises :class:`SoftBlockError` on a challenge / rate-limit / login wall —
        stop and rest rather than retrying. A profile that doesn't exist or is
        private-and-unfollowed yields ``[]`` (a Watchlist-curation problem, not a
        scraper failure). On a mid-pull block, posts already collected ride along
        on ``err.partial``.
        """
        try:
            user_id = self._resolve_user_id(account)
        except (UserNotFound, PrivateAccount):
            return []
        except _BACKOFF_EXCEPTIONS as err:
            raise self._as_softblock(err)

        # One author lookup per account (NOT per post) for the Tier-1
        # `author_follower_count` signal (docs/SIGNALS.md). Extra calls are extra
        # ban surface, so we resolve the author once and attach it to every post.
        follower_count, author_raw = self._fetch_author(user_id)

        records: list[PostRecord] = []
        try:
            for raw_item in self._iter_raw_items(user_id, limit):
                rec = self._to_record(
                    raw_item, account, follower_count=follower_count, author_raw=author_raw
                )
                if rec is not None:
                    records.append(rec)
        except (UserNotFound, PrivateAccount):
            return records
        except _BACKOFF_EXCEPTIONS as err:
            raise self._as_softblock(err, partial=records)
        return records

    # ---------------------------------------------------------- sound pivot

    def fetch_trending_sound_ids(self, *, limit: int = 30) -> list[str]:
        """IG's trending audios — the reused-a-lot sounds, straight from the source.

        Reads ``music/top_trends/`` (and ``music/trending/`` as a top-up) and pulls
        out audio_cluster_ids, newest/most-trending first, deduped. These feed the
        sound pivot so we discover heavily-reused sounds without waiting for them to
        appear in a harvested post. Best-effort: a back-off raises SoftBlockError;
        any other miss yields whatever ids we already gathered.
        """
        ids: list[str] = []
        seen: set[str] = set()

        def _collect(payload: Any) -> None:
            for cid in re.findall(r'"audio_cluster_id"\s*:\s*"?(\d{6,})"?', json.dumps(payload)):
                if cid not in seen:
                    seen.add(cid)
                    ids.append(cid)

        try:
            for product in ("music_in_feed", "feed_post"):
                if len(ids) >= limit:
                    break
                _collect(self.cl.music_top_trends(product=product, page_size=15))
            if len(ids) < limit:
                _collect(self.cl.music_trending(product="feed_post"))
        except _BACKOFF_EXCEPTIONS as err:
            raise self._as_softblock(err)
        except Exception:
            pass
        return ids[:limit]

    def fetch_sound(
        self, audio_cluster_id: str, *, videos_limit: int = 30, max_pages: int = 2
    ) -> "tuple[SoundRecord, list[PostRecord]]":
        """Pivot on one IG audio: the reels using it + a reuse count if IG gives one.

        Hits the audio page (``clips/music/``, the same endpoint instagrapi's
        ``track_info_by_id`` uses) and pages a bounded number of reels. Unlike
        TikTok there is no single ``videoCount`` field, so ``video_count`` is taken
        from a platform count in the payload **only if present** (left None
        otherwise — the song ranker then falls back to our observed post-count). The
        real win is breadth: one pivot pulls in many reels for the sound, so the
        derived post-count becomes a meaningful reuse signal.

        BURNER + PACING: needs a logged-in burner (caller's job) and is ban-sensitive
        — view hydration is forced off here (one call per reel would multiply ban
        surface). Raises :class:`SoftBlockError` on a challenge / rate limit.
        """
        fetched_at = datetime.now(timezone.utc)
        items_raw: list[dict[str, Any]] = []
        header: Optional[dict[str, Any]] = None
        max_id = ""
        pages = 0
        try:
            while pages < max_pages and len(items_raw) < videos_limit:
                data: dict[str, Any] = {
                    "audio_cluster_id": str(audio_cluster_id),
                    "original_sound_audio_asset_id": str(audio_cluster_id),
                    "_uuid": self.cl.uuid,
                    "tab_type": "clips",
                }
                if max_id:
                    data["max_id"] = max_id
                self.cl.private_request("clips/music/", data)
                last = self.cl.last_json or {}
                if header is None:
                    header = last
                for it in (last.get("items") or []):
                    media = it.get("media") if isinstance(it, dict) and "media" in it else it
                    if isinstance(media, dict):
                        items_raw.append(media)
                paging = last.get("paging_info") or {}
                max_id = paging.get("max_id") or last.get("next_max_id") or ""
                pages += 1
                if not max_id or not paging.get("more_available", False):
                    break
        except _BACKOFF_EXCEPTIONS as err:
            raise self._as_softblock(err)

        # Normalize reels to PostRecords. Force view hydration OFF for the batch.
        prev_hydrate = self.hydrate_views
        self.hydrate_views = False
        records: list[PostRecord] = []
        try:
            for raw in items_raw[:videos_limit]:
                user = raw.get("user") or {}
                handle = user.get("username") or ""
                acct = WatchedAccount(
                    handle=handle, platform=self.platform,
                    segment="adjacent", geo_tier=None,
                )
                try:
                    rec = self._to_record(
                        raw, acct, follower_count=user.get("follower_count")
                    )
                except Exception:
                    rec = None
                if rec is not None and rec.raw and handle:
                    records.append(rec)
        finally:
            self.hydrate_views = prev_hydrate

        return self._sound_record_from_audio_page(
            audio_cluster_id, header, records, fetched_at
        ), records

    def _sound_record_from_audio_page(
        self,
        audio_cluster_id: str,
        header: Optional[dict[str, Any]],
        records: list[PostRecord],
        fetched_at: datetime,
    ) -> SoundRecord:
        """Build a SoundRecord from a ``clips/music/`` payload + the reels we got."""
        header = header or {}
        meta = header.get("metadata") if isinstance(header.get("metadata"), dict) else {}
        music_info = meta.get("music_info") if isinstance(meta.get("music_info"), dict) else {}
        asset = (
            music_info.get("music_asset_info")
            if isinstance(music_info.get("music_asset_info"), dict) else {}
        )
        consumption = (
            music_info.get("music_consumption_info")
            if isinstance(music_info.get("music_consumption_info"), dict) else {}
        )

        title = asset.get("title")
        author_name = asset.get("display_artist") or asset.get("artist_name")
        cover = (
            asset.get("cover_artwork_thumbnail_uri")
            or asset.get("cover_artwork_uri")
        )
        play_url = asset.get("progressive_download_url") or asset.get("fast_start_progressive_download_url")

        # The reels carry a stable asset audio_id (what our posts group under); the
        # trending list's audio_cluster_id can differ. Prefer the reels' id so the
        # Sound row joins the song aggregate. Title/author fall back to a reel too.
        reel_sound_id = records[0].sound_id if records else None
        if (not title) and records:
            title = records[0].sound_name

        return SoundRecord(
            platform=self.platform,
            sound_id=str(reel_sound_id or asset.get("audio_cluster_id") or audio_cluster_id),
            fetched_at=fetched_at,
            raw=header if isinstance(header, dict) else {},
            title=title,
            author_name=author_name,
            # IG's own reuse count is a FORMATTED string ("1.2M"); parse it to an
            # int magnitude for ranking. None → ranker falls back to observed count.
            video_count=_parse_formatted_count(consumption.get("formatted_clips_media_count")),
            is_original=bool(asset.get("is_original_sound")) if asset.get("is_original_sound") is not None else None,
            cover_url=str(cover) if cover else None,
            play_url=str(play_url) if play_url else None,
            duration_sec=(asset.get("duration_in_ms") / 1000.0) if asset.get("duration_in_ms") else None,
        )

    def fetch_trends(self, geo_tier: GeoTier) -> list[Trend]:
        # No free Instagram trending source exists — switching to the private API
        # doesn't change that. The mobile API exposes no platform-wide trend feed
        # we can read for an arbitrary geo; third-party trend feeds are all paid.
        # Per the handoff + ADR-0001 + OPEN-QUESTIONS Q-2, IG trend signal (if we
        # ever want it) must be *derived* by aggregating Watchlist hashtags in
        # `core`, which is out of scope for this adapter. So: nothing to return.
        return []

    # ------------------------------------------------------------- internals

    def _resolve_user_id(self, account: WatchedAccount) -> str:
        """Numeric user pk. Prefer the cached id to skip a lookup call.

        Each extra private call is extra ban surface, so if the Watchlist already
        stored the pk on ``platform_account_id`` we use it; otherwise we resolve
        the handle once.
        """
        if account.platform_account_id:
            return str(account.platform_account_id)
        return str(self.cl.user_id_from_username(account.handle))

    def _fetch_author(
        self, user_id: str
    ) -> tuple[Optional[int], Optional[dict[str, Any]]]:
        """One author lookup → (follower_count, complete raw author payload).

        Done once per account. We hit ``users/{id}/info/`` with a RAW
        ``private_request`` and read ``follower_count`` straight from the JSON,
        rather than instagrapi's ``user_info_v1`` — its strict pydantic ``User``
        model rejects the stripped author payloads IG returns anonymously (missing
        ``full_name`` etc.), and the follower count is an optional Tier-1 signal we
        don't want to let sink the whole pull. The untouched author JSON is kept so
        un-normalized fields (verified, following_count, bio, category, …) survive
        in ``raw``. Back-off is surfaced; anything else degrades to no follower
        count — we still return whatever posts we can.
        """
        try:
            self.cl.private_request(
                f"users/{user_id}/info/",
                params={"entry_point": "profile", "from_module": "feed_timeline"},
            )
        except _BACKOFF_EXCEPTIONS as err:
            raise self._as_softblock(err)
        except (UserNotFound, PrivateAccount):
            return None, None
        except Exception:
            # Strict-parse / shape drift / transient miss → no follower count,
            # but don't kill the post pull over an optional signal.
            return None, None
        last = self.cl.last_json
        author_raw = last.get("user") if isinstance(last, dict) else None
        if not isinstance(author_raw, dict):
            author_raw = last if isinstance(last, dict) else None
        follower_count = author_raw.get("follower_count") if isinstance(author_raw, dict) else None
        return follower_count, author_raw

    def _hydrate_views(self, media_pk: str) -> Optional[int]:
        """Fetch a video's real play count via ``media/{pk}/info/``.

        Logged-in feed payloads omit play/view counts (served lazily), so for
        videos we make one extra call to recover the number. Back-off is
        surfaced; any other miss degrades to ``None`` rather than sinking the
        pull over a single post's views.
        """
        try:
            self.cl.private_request(f"media/{media_pk}/info/")
        except _BACKOFF_EXCEPTIONS as err:
            raise self._as_softblock(err)
        except Exception:
            return None
        last = self.cl.last_json
        items = last.get("items") if isinstance(last, dict) else None
        if not items:
            return None
        info = items[0]
        return info.get("play_count") or info.get("ig_play_count") or info.get("view_count")

    def _iter_raw_items(self, user_id: str, limit: int) -> Iterator[dict[str, Any]]:
        """Page the private ``feed/user/{id}/`` endpoint, yielding RAW items.

        Mirrors instagrapi's own pagination but keeps the untouched API dicts
        (its ``user_medias_v1`` throws the raw away after parsing). instagrapi
        applies the ``delay_range`` sleep before each ``private_request``, so the
        polite pacing is automatic — no manual sleep here.
        """
        if limit <= 0:
            return
        fetched = 0
        next_max_id = ""
        while fetched < limit:
            count = min(33, limit - fetched)
            self.cl.private_request(
                f"feed/user/{user_id}/",
                params={
                    "max_id": next_max_id,
                    "count": count,
                    "min_timestamp": None,
                    "rank_token": self.cl.rank_token,
                    "ranked_content": "true",
                },
            )
            data = self.cl.last_json or {}
            items = data.get("items") or []
            for item in items:
                yield item
                fetched += 1
                if fetched >= limit:
                    return
            next_max_id = data.get("next_max_id")
            if not next_max_id:
                return

    def _to_record(
        self,
        raw: dict[str, Any],
        account: WatchedAccount,
        *,
        follower_count: Optional[int] = None,
        author_raw: Optional[dict[str, Any]] = None,
    ) -> Optional[PostRecord]:
        """Normalize one RAW private-API item into a PostRecord.

        ``raw`` is the complete untouched API dict — it goes into ``.raw`` whole.
        We parse the same dict with ``extract_media_v1`` for tidy field access;
        if that ever fails on a drifted shape we fall back to reading the raw
        directly so a single odd post can't sink the whole pull. ``follower_count``
        / ``author_raw`` come from one per-account author lookup (see
        :meth:`_fetch_author`).
        """
        try:
            media = extract_media_v1(dict(raw))
        except Exception:
            return self._record_from_raw_only(
                raw, account, follower_count=follower_count, author_raw=author_raw
            )

        code = media.code or raw.get("code")
        media_type = _MEDIA_TYPE_TO_NORM.get(int(media.media_type or 0), "image")
        sound_id, sound_name = self._extract_audio(raw)
        # view/play count is video-only. Anonymously IG inlines it in the feed
        # item; logged-in it's omitted (lazy `has_views_fetching`) so we hydrate
        # it with a per-media call. Images -> None (a "0 views" would mislead).
        view_count = None
        if media_type == "video":
            view_count = (
                raw.get("play_count") or raw.get("ig_play_count") or raw.get("view_count")
            )
            if not view_count and self.hydrate_views:
                view_count = self._hydrate_views(media.pk)

        raw_out = dict(raw)
        # First-class extras the handoff called out, lifted out of raw so
        # consumers don't re-parse. EXTRA (not in the normalized common set);
        # they ride along under a stable key inside raw.
        raw_out["_normalized_extra"] = {
            "is_reel": (media.product_type == "clips") or bool(raw.get("clips_metadata")),
            "is_paid_partnership": bool(getattr(media, "is_paid_partnership", False)),
            "sponsor_users": [
                t.user.username
                for t in (media.sponsor_tags or [])
                if getattr(t, "user", None)
            ],
            "coauthors": [u.username for u in (media.coauthor_producers or [])],
            "location_name": media.location.name if media.location else None,
            "carousel_count": len(media.resources) if media.resources else None,
            "audio_id": sound_id,
            "audio_name": sound_name,
            "has_audio": getattr(media, "has_audio", None),
        }
        # Complete author payload from the per-account lookup (verified, bio,
        # following_count, category, …) — kept whole so nothing is dropped.
        if author_raw is not None:
            raw_out["_account_info"] = author_raw

        return PostRecord(
            platform=self.platform,
            platform_post_id=str(media.pk),
            account_handle=account.handle,
            url=f"https://www.instagram.com/p/{code}/" if code else "",
            fetched_at=datetime.now(timezone.utc),
            media_type=media_type,
            raw=raw_out,
            posted_at=self._as_utc(media.taken_at),
            caption=media.caption_text or None,
            hashtags=self._hashtags(media.caption_text),
            sound_id=sound_id,
            sound_name=sound_name,
            duration_sec=media.video_duration or None,
            view_count=view_count,
            like_count=media.like_count,
            comment_count=media.comment_count,
            share_count=None,  # never public
            save_count=None,  # never public
            thumbnail_url=str(media.thumbnail_url) if media.thumbnail_url else None,
            geo_tier=self._geo_of(account),
            author_follower_count=follower_count,
        )

    def _record_from_raw_only(
        self,
        raw: dict[str, Any],
        account: WatchedAccount,
        *,
        follower_count: Optional[int] = None,
        author_raw: Optional[dict[str, Any]] = None,
    ) -> Optional[PostRecord]:
        """Minimal record straight from raw when extract_media_v1 chokes.

        We still capture everything in ``.raw``; we just normalize less. Returns
        None only if the item has no usable id at all.
        """
        pk = raw.get("pk") or raw.get("id")
        if not pk:
            return None
        code = raw.get("code")
        media_type = _MEDIA_TYPE_TO_NORM.get(int(raw.get("media_type") or 0), "image")
        sound_id, sound_name = self._extract_audio(raw)
        taken = raw.get("taken_at")
        posted_at = (
            datetime.fromtimestamp(taken, tz=timezone.utc)
            if isinstance(taken, (int, float))
            else None
        )
        raw_out = dict(raw)
        raw_out["_normalized_extra"] = {"extract_failed": True}
        if author_raw is not None:
            raw_out["_account_info"] = author_raw
        return PostRecord(
            platform=self.platform,
            platform_post_id=str(pk).split("_")[0],
            account_handle=account.handle,
            url=f"https://www.instagram.com/p/{code}/" if code else "",
            fetched_at=datetime.now(timezone.utc),
            media_type=media_type,
            raw=raw_out,
            posted_at=posted_at,
            caption=raw.get("caption", {}).get("text") if isinstance(raw.get("caption"), dict) else None,
            hashtags=self._hashtags(
                raw.get("caption", {}).get("text") if isinstance(raw.get("caption"), dict) else None
            ),
            sound_id=sound_id,
            sound_name=sound_name,
            like_count=raw.get("like_count"),
            comment_count=raw.get("comment_count"),
            view_count=raw.get("play_count") or raw.get("view_count"),
            geo_tier=self._geo_of(account),
            author_follower_count=follower_count,
        )

    @staticmethod
    def _extract_audio(raw: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        """Best-effort reel audio (sound_id, sound_name) from clips_metadata.

        Reel audio nests under ``clips_metadata`` as either licensed
        ``music_info`` or ``original_sound_info``. Shape drifts, so probe
        defensively; non-reels / unknown shapes → ``(None, None)``.
        """
        clips = raw.get("clips_metadata")
        if not isinstance(clips, dict):
            return None, None

        music = clips.get("music_info")
        if isinstance(music, dict):
            asset = music.get("music_asset_info")
            if isinstance(asset, dict):
                return (
                    _str_or_none(asset.get("audio_id") or asset.get("id")),
                    _str_or_none(asset.get("title")),
                )

        original = clips.get("original_sound_info")
        if isinstance(original, dict):
            return (
                _str_or_none(original.get("audio_asset_id")),
                _str_or_none(original.get("original_audio_title")),
            )

        # Some reels (e.g. original in-video audio) expose no named track but
        # still carry a stable canonical id — useful for grouping a sound across
        # posts (Trend detection, Q-2). Id only; there's no title to give.
        canonical = clips.get("music_canonical_id")
        if canonical:
            return _str_or_none(canonical), None
        return None, None

    @staticmethod
    def _hashtags(caption: Optional[str]) -> list[str]:
        # instagrapi doesn't split hashtags out; pull them from the caption.
        return _HASHTAG_RE.findall(caption) if caption else []

    @staticmethod
    def _as_utc(dt: Any) -> Optional[datetime]:
        if not isinstance(dt, datetime):
            return None
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

    @staticmethod
    def _geo_of(account: WatchedAccount) -> Optional[GeoTier]:
        # For the Watchlist, Geo Tier is a manual per-account tag (CONTEXT.md);
        # IG exposes no reliable native per-post region, so pass the account's
        # tag straight through rather than inferring.
        return account.geo_tier

    def _as_softblock(
        self, err: Exception, partial: Optional[list[PostRecord]] = None
    ) -> SoftBlockError:
        sb = SoftBlockError(
            f"Instagram asked us to back off ({type(err).__name__}: {err}). Stop, "
            f"rest the burner/IP, retry later — do not retry in a loop. A warmed "
            f"session (load_session) is more durable than re-login."
        )
        if partial:
            sb.partial = partial  # type: ignore[attr-defined]
        return sb


def _str_or_none(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _parse_formatted_count(text: Any) -> Optional[int]:
    """Parse IG's formatted reuse count ("1.2M reels", "12.3K", "1,234") to an int.

    IG only exposes the audio-page media count as a human string, so exact
    precision is lost — fine for *ranking* by magnitude (the whole point of
    "reused most"). Returns None when there's nothing parseable.
    """
    if isinstance(text, (int, float)):
        return int(text)
    if not isinstance(text, str):
        return None
    m = re.search(r"([\d.,]+)\s*([KkMmBb])?", text)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        value = float(num)
    except ValueError:
        return None
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(
        (m.group(2) or "").lower(), 1
    )
    return int(value * mult)
