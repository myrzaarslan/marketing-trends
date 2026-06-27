"""TikTok platform adapter — the $0 reference implementation.

Two independent data paths, both free, no-auth, no paid provider (per ADR-0001):

1. ``fetch_account_posts`` — two-tier (round two, see docs/REVERSE-ENGINEERING.md):

   PRIMARY: **TikTok-Api** (davidteather) hitting TikTok's signed private endpoint
   ``/api/post/item_list/``. The hard wall is the ``X-Bogus``/``X-Gnarly`` +
   ``msToken`` request signing; TikTok-Api clears it the durable way — by running
   TikTok's *own* signer JS inside a Playwright browser, so it survives algorithm
   changes. This is the provider-grade surface: it returns the full per-video
   object, which carries the first-class fields the yt-dlp path could NOT reach —
   stable ``music.id`` (sound_id), ``collectCount`` (save_count), ``isAd``,
   original-vs-licensed music (``music.original``), duet/stitch info, location.

   FALLBACK: **yt-dlp** flat-playlist against ``https://www.tiktok.com/@<handle>``
   (the round-one path). Lighter and no browser, but thinner data and no sound_id.
   Used automatically if TikTok-Api is unavailable or its session/signing fails.

   NOTE on fragility: TikTok-Api needs a real browser to mint signatures; from a
   home IP, headless Chromium trips TikTok's bot detection (empty ``item_list``) —
   a HEADED browser (``headless=False`` on a real/virtual display) is required.
   yt-dlp's own single-video extractor is broken and its handle→secUid resolution
   fails for some accounts. See README for both and the secUid escape hatch.

2. ``fetch_trends`` — **TikTok Creative Center**. The Creative Center migrated to
   a new "Creative Suite" SPA whose trend pages are server-rendered via Next.js
   loader endpoints (``...?__loader=...&__ssrDirect=true``) that return clean
   JSON with NO request signing required — just a browser User-Agent. We read the
   logged-out, free tier directly over HTTP. See README for what that tier does
   and (importantly) does NOT cover, verified 2026-06-25.

This module only FETCHES and NORMALIZES. It never persists, schedules, or decides
virality (docs/INGESTION-CONTRACT.md). The COMPLETE original platform payload is
preserved untouched in ``PostRecord.raw`` / ``Trend.raw`` — capture everything,
decide "viral" later (OPEN-QUESTIONS Q-1).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.adapter import PlatformAdapter
from core.schema import GeoTier, PostRecord, SoundRecord, Trend, WatchedAccount

# A browser-ish UA is required by both paths or TikTok serves blocked/empty shells.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --- Creative Center (trends) -------------------------------------------------

# The new Creative Suite SSR loader. ``period`` is a day window (7 | 30 | 120);
# ``countryCode`` selects the market. The ``__loader``/``__ssrDirect`` query is
# what makes TikTok return the page's React-Query state as JSON instead of HTML.
_CC_LOADER_PATH = "creativeCenter/trends/(tab)/page"
_CC_TREND_URL = (
    "https://ads.tiktok.com/creative/creativeCenter/trends/hashtag"
    "?countryCode={country}&period={period}"
    "&__loader={loader}&__ssrDirect=true"
)

# Countries TikTok Creative Center *advertises* (verified 2026-06-25 from the
# page's own ``getTrendsFilterConfig`` query). KZ — and every CIS country — are
# ABSENT, which is one reason fetch_trends("KZ"/"CIS") returns []. NOTE: when
# logged out, the SSR loader IGNORES ``countryCode`` entirely and always serves
# one default (US/global) list — country filtering is itself login-gated. So this
# set documents the gap rather than being something we can exploit for free.
_CC_SUPPORTED_COUNTRIES = frozenset({
    "US", "FR", "DE", "IT", "ES", "GB", "AR", "AU", "BR", "CA", "CO", "EG", "ID",
    "IL", "JP", "KR", "MY", "MX", "PH", "SA", "SG", "ZA", "TW", "TH", "TR", "AE", "VN",
})

# The single market the free/logged-out loader actually resolves to. "World" maps
# here; it's the global default list TikTok returns regardless of countryCode.
_DEFAULT_MARKET = "US"

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)


class TikTokAdapter(PlatformAdapter):
    platform = "tiktok"

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        trend_period_days: int = 7,
        use_tiktokapi: bool = True,
        tiktokapi_headless: bool = False,
        fyp_cycles: int = 3,
        fyp_max_advances: int = 40,
        discovery_surface: str = "explore",
    ) -> None:
        self._timeout = timeout
        self._trend_period = trend_period_days
        # Discovery harvest tuning (see discovery.py). "explore" (the grid) is the
        # default surface — more items, faster, and richer (challenges) than the
        # For-You player; pass discovery_surface="foryou" for the player instead.
        self._fyp_cycles = fyp_cycles
        self._fyp_max_advances = fyp_max_advances
        self._discovery_surface = discovery_surface
        # Diagnostics from the last fetch_viral_posts run (throttle/block note).
        self.last_discovery_note: Optional[str] = None
        # TikTok-Api (signed item_list) is the primary account-posts path; yt-dlp
        # is the fallback. Set use_tiktokapi=False to force the lighter yt-dlp path
        # (no browser). headless=False is the default because headless Chromium
        # trips TikTok's bot detection from a home IP — see module docstring.
        self._use_tiktokapi = use_tiktokapi
        self._tiktokapi_headless = tiktokapi_headless
        # Diagnostic: the last TikTok-Api error that forced a yt-dlp fallback.
        self.last_tiktokapi_error: Optional[Exception] = None

    # == account posts =========================================================

    def fetch_account_posts(
        self, account: WatchedAccount, limit: int = 30
    ) -> list[PostRecord]:
        """Recent videos for one Watched Account, newest first.

        Tries the signed TikTok-Api ``item_list`` path first (rich payload), then
        falls back to yt-dlp flat-playlist. ``limit`` is honored on both paths.
        Each PostRecord.raw holds the COMPLETE original item dict (capture
        everything) — note the two paths have *different* raw shapes.
        """
        fetched_at = datetime.now(timezone.utc)

        if self._use_tiktokapi:
            try:
                items = self._fetch_via_tiktokapi(account, limit)
            except Exception as e:  # signing/session/bot-detection/network
                self.last_tiktokapi_error = e
            else:
                if items:
                    return [
                        self._record_from_api(
                            it, fetched_at,
                            geo_tier=account.geo_tier,
                            fallback_handle=account.handle.lstrip("@"),
                        )
                        for it in items
                    ]
                # Empty (not an exception) usually means bot-detection on a
                # headless browser; fall through to yt-dlp rather than 0 posts.

        entries = self._fetch_entries(account, limit)  # yt-dlp fallback
        return [self._to_post_record(e, account, fetched_at) for e in entries]

    # -- primary path: TikTok-Api (signed item_list) ---------------------------

    def _fetch_via_tiktokapi(self, account: WatchedAccount, limit: int) -> list[dict]:
        """Run the async TikTok-Api fetch from this sync interface.

        Each call spins up its own Playwright session and tears it down. That's
        ~10–20s of browser startup per account — fine for a once/day low-volume
        Watchlist; batching sessions across accounts is a productionization
        concern (OPEN-QUESTIONS Q-3), deliberately kept out of the adapter.
        """
        import asyncio  # noqa: PLC0415

        return asyncio.run(self._fetch_via_tiktokapi_async(account, limit))

    async def _fetch_via_tiktokapi_async(
        self, account: WatchedAccount, limit: int
    ) -> list[dict]:
        try:
            from TikTokApi import TikTokApi  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(
                "TikTokApi is required for the primary account-posts path. "
                "Install it: pip install -r adapters/tiktok/requirements.txt "
                "(or construct TikTokAdapter(use_tiktokapi=False) to use yt-dlp)."
            ) from e

        handle = account.handle.lstrip("@")
        sec_uid = account.platform_account_id
        items: list[dict] = []
        async with TikTokApi() as api:
            await api.create_sessions(
                num_sessions=1,
                sleep_after=3,
                browser="chromium",
                headless=self._tiktokapi_headless,
            )
            # Prefer secUid when we have it (skips fragile handle resolution —
            # the round-one finding, carried forward). Else resolve by username.
            if sec_uid and sec_uid.startswith("MS4"):
                user = api.user(sec_uid=sec_uid)
            else:
                user = api.user(username=handle)
            # TikTok-Api yields whole API pages (~30), so `count` is a floor, not a
            # cap — enforce `limit` ourselves.
            async for video in user.videos(count=limit):
                items.append(video.as_dict)
                if len(items) >= limit:
                    break
        return items

    # == sound pivot (the "reused most" path) =================================

    def fetch_sound(
        self, sound_id: str, *, videos_limit: int = 30
    ) -> "tuple[SoundRecord, list[PostRecord]]":
        """Pivot on one sound: its authoritative reuse count + the videos using it.

        Hits TikTok's ``/api/music/detail`` via TikTok-Api's ``Sound.info()`` — the
        same signed-session path as account posts — to read ``stats.videoCount``
        (how many videos use this sound across all of TikTok, the "reused most"
        signal) and the sound's metadata, then pages ``Sound.videos()`` for a sample
        of the videos using it (returned as normal PostRecords so they enrich the
        corpus too). The SoundRecord.raw keeps the complete music-detail payload.
        """
        import asyncio  # noqa: PLC0415

        return asyncio.run(self._fetch_sound_async(sound_id, videos_limit))

    async def _fetch_sound_async(
        self, sound_id: str, videos_limit: int
    ) -> "tuple[SoundRecord, list[PostRecord]]":
        try:
            from TikTokApi import TikTokApi  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(
                "TikTokApi is required for the sound-pivot path. "
                "Install it: pip install -r adapters/tiktok/requirements.txt"
            ) from e

        fetched_at = datetime.now(timezone.utc)
        async with TikTokApi() as api:
            await api.create_sessions(
                num_sessions=1,
                sleep_after=3,
                browser="chromium",
                headless=self._tiktokapi_headless,
            )
            snd = api.sound(id=str(sound_id))
            info = await snd.info()
            sound_rec = self._sound_record_from_info(snd, info, sound_id, fetched_at)

            records: list[PostRecord] = []
            if videos_limit > 0:
                try:
                    async for video in snd.videos(count=videos_limit):
                        try:
                            records.append(
                                self._record_from_api(video.as_dict, fetched_at)
                            )
                        except (KeyError, TypeError):
                            continue
                        if len(records) >= videos_limit:
                            break
                except Exception:
                    # A sound with restricted/empty video listing still yields its
                    # count + metadata — don't lose those to a videos() failure.
                    pass
        return sound_rec, records

    def _sound_record_from_info(
        self, snd: Any, info: dict, sound_id: str, fetched_at: datetime
    ) -> SoundRecord:
        """Normalize a ``/api/music/detail`` payload into a SoundRecord.

        Tolerant of both shapes TikTok returns: a ``musicInfo`` envelope (the
        logged-in detail) and a bare ``music``/``stats`` pair.
        """
        mi = info.get("musicInfo") if isinstance(info, dict) else None
        mi = mi if isinstance(mi, dict) else {}
        music = mi.get("music") or (info.get("music") if isinstance(info, dict) else None) or {}
        stats = mi.get("stats") or (info.get("stats") if isinstance(info, dict) else None) or {}
        author = mi.get("author") or {}
        author_name = None
        if isinstance(author, dict):
            author_name = author.get("nickname") or author.get("uniqueId")
        elif isinstance(author, str):
            author_name = author
        author_name = author_name or music.get("authorName")

        return SoundRecord(
            platform=self.platform,
            sound_id=str(getattr(snd, "id", None) or music.get("id") or sound_id),
            fetched_at=fetched_at,
            raw=info if isinstance(info, dict) else {"raw": info},
            title=getattr(snd, "title", None) or music.get("title"),
            author_name=author_name,
            video_count=self._int(stats.get("videoCount")),
            is_original=(
                bool(music.get("original")) if music.get("original") is not None else None
            ),
            cover_url=(
                music.get("coverLarge") or music.get("coverMedium")
                or music.get("coverThumb") or getattr(snd, "cover_large", None)
            ),
            play_url=music.get("playUrl") or getattr(snd, "play_url", None),
            duration_sec=self._num(music.get("duration") or getattr(snd, "duration", None)),
        )

    def _fetch_entries(self, account: WatchedAccount, limit: int) -> list[dict]:
        """Pull flat-playlist entries for an account, with a sec_uid fallback.

        yt-dlp is imported lazily so that fetch_trends (which needs none of it)
        works even if yt-dlp isn't installed.
        """
        try:
            import yt_dlp  # noqa: PLC0415  (lazy: trends path needs no yt-dlp)
        except ImportError as e:  # pragma: no cover - environment guard
            raise RuntimeError(
                "yt-dlp is required for TikTok account posts. "
                "Install it: pip install -r adapters/tiktok/requirements.txt"
            ) from e

        handle = account.handle.lstrip("@")
        candidate_urls = [f"https://www.tiktok.com/@{handle}"]
        # yt-dlp resolves @handle -> secUid by scraping the profile page, which
        # deterministically fails for some accounts ("Unable to extract secondary
        # user ID"). The documented escape hatch is yt-dlp's ``tiktokuser:<secUid>``
        # input, which skips that resolution. Store the secUid (the "MS4w..." id,
        # captured as ``channel_id`` in a working entry's raw) on the Watched
        # Account's ``platform_account_id`` to make such accounts reliable.
        sec_uid = account.platform_account_id
        if sec_uid and sec_uid.startswith("MS4"):
            candidate_urls.append(f"tiktokuser:{sec_uid}")

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",  # pass through TikTok's item objects
            "playlistend": limit,
            "extractor_args": {"tiktok": {"app_name": ["tiktok_web"]}},
        }

        last_err: Optional[Exception] = None
        for url in candidate_urls:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as e:  # yt-dlp raises many DownloadError subtypes
                last_err = e
                continue
            entries = [e for e in (info or {}).get("entries", []) if e]
            if entries:
                return entries[:limit]
            last_err = RuntimeError(f"No entries returned for {url}")

        raise RuntimeError(
            f"Could not fetch TikTok posts for @{handle} "
            f"(account may be private/empty, or the extractor broke): {last_err}"
        )

    # -- normalization ---------------------------------------------------------

    def _to_post_record(
        self, entry: dict, account: WatchedAccount, fetched_at: datetime
    ) -> PostRecord:
        caption = entry.get("description") or entry.get("title")
        return PostRecord(
            platform=self.platform,
            platform_post_id=str(entry["id"]),
            account_handle=entry.get("uploader") or account.handle.lstrip("@"),
            url=entry.get("url") or entry.get("webpage_url") or "",
            fetched_at=fetched_at,
            # TikTok photo-mode posts exist but flat data exposes no reliable flag;
            # default to video (the dominant case) — the raw entry settles ties.
            media_type="video",
            raw=entry,  # COMPLETE yt-dlp entry, untouched (capture everything)
            posted_at=self._ts(entry.get("timestamp")),
            caption=caption,
            hashtags=self._hashtags(caption),
            # Flat data carries the sound *name* ("track") but not a stable music id.
            sound_id=None,
            sound_name=entry.get("track"),
            duration_sec=self._num(entry.get("duration")),
            view_count=self._int(entry.get("view_count")),
            like_count=self._int(entry.get("like_count")),
            comment_count=self._int(entry.get("comment_count")),
            share_count=self._int(entry.get("repost_count")),
            save_count=self._int(entry.get("save_count")),
            thumbnail_url=self._thumbnail(entry),
            geo_tier=account.geo_tier,  # Watchlist tier is a manual per-account tag
            # yt-dlp flat entries sometimes expose channel_follower_count; None if absent
            author_follower_count=self._int(entry.get("channel_follower_count")),
        )

    def _record_from_api(
        self,
        item: dict,
        fetched_at: datetime,
        *,
        geo_tier: Optional[GeoTier] = None,
        fallback_handle: Optional[str] = None,
    ) -> PostRecord:
        """Normalize a signed ``item_list`` video object into a PostRecord.

        Shared by ``fetch_account_posts`` (signed path) and ``fetch_viral_posts``
        (For-You harvest) — both receive the same item shape. The author is read
        from the item itself; ``geo_tier`` is supplied by the caller (the Watched
        Account's tag, or the discovery geo tier). Fills the columns yt-dlp left
        ``None`` (sound_id, save_count) and preserves the first-class extras (isAd,
        music.original, duet/stitch, location) untouched in ``raw``.
        """
        stats = item.get("statsV2") or item.get("stats") or {}
        music = item.get("music") or {}
        author = item.get("author") or {}
        author_stats = item.get("authorStats") or {}
        handle = author.get("uniqueId") or fallback_handle or ""
        post_id = str(item["id"])
        desc = item.get("desc")
        return PostRecord(
            platform=self.platform,
            platform_post_id=post_id,
            account_handle=handle,
            url=f"https://www.tiktok.com/@{handle}/video/{post_id}",
            fetched_at=fetched_at,
            media_type=self._media_type_api(item),
            raw=item,  # COMPLETE item_list object, untouched (capture everything)
            posted_at=self._ts(item.get("createTime")),
            caption=desc,
            hashtags=self._hashtags_api(item, desc),
            # music id "0" is TikTok's placeholder for "no distinct sound" -> None.
            sound_id=(
                str(music["id"]) if music.get("id") and str(music["id"]) != "0"
                else None
            ),
            sound_name=music.get("title"),
            duration_sec=self._num(
                (item.get("video") or {}).get("duration") or music.get("duration")
            ),
            view_count=self._int(stats.get("playCount")),
            like_count=self._int(stats.get("diggCount")),
            comment_count=self._int(stats.get("commentCount")),
            share_count=self._int(stats.get("shareCount")),
            save_count=self._int(stats.get("collectCount")),  # yt-dlp couldn't
            thumbnail_url=self._thumbnail_api(item),
            geo_tier=geo_tier,
            # follower count at capture time — for baseline/follower-normalised ranking
            author_follower_count=self._int(author_stats.get("followerCount")),
        )

    @staticmethod
    def _media_type_api(item: dict):
        # Photo-mode posts carry an imagePost block instead of a single video.
        image_post = item.get("imagePost") or {}
        images = image_post.get("images") if isinstance(image_post, dict) else None
        if images:
            return "carousel" if len(images) > 1 else "image"
        return "video"

    @classmethod
    def _hashtags_api(cls, item: dict, desc: Optional[str]) -> list[str]:
        # textExtra entries are hashtags (hashtagName set) OR mentions (userId set);
        # take the hashtags, then union with a desc regex pass, order-preserving.
        tags: list[str] = []
        for extra in item.get("textExtra") or []:
            name = extra.get("hashtagName")
            if name:
                tags.append(name)
        for name in cls._hashtags(desc):
            if name not in tags:
                tags.append(name)
        return tags

    @staticmethod
    def _thumbnail_api(item: dict) -> Optional[str]:
        video = item.get("video") or {}
        for key in ("cover", "originCover", "dynamicCover"):
            url = video.get(key)
            if url:
                return url
        # photo-mode fallback: first image's cover
        images = (item.get("imagePost") or {}).get("images") or []
        if images:
            url_list = (images[0].get("imageURL") or {}).get("urlList") or []
            if url_list:
                return url_list[0]
        return None

    @staticmethod
    def _hashtags(text: Optional[str]) -> list[str]:
        return _HASHTAG_RE.findall(text) if text else []

    @staticmethod
    def _thumbnail(entry: dict) -> Optional[str]:
        thumbs = entry.get("thumbnails") or []
        for t in reversed(thumbs):  # last is typically highest-res
            if t.get("url"):
                return t["url"]
        return entry.get("thumbnail")

    @staticmethod
    def _ts(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return None

    @staticmethod
    def _int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _num(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    # == trends ================================================================

    def fetch_trends(self, geo_tier: GeoTier) -> list[Trend]:
        """Current trending hashtags for a Geo Tier, from TikTok Creative Center.

        Coverage reality (verified 2026-06-25 — see README):
          * Only the **hashtag** tab is free/SSR-rendered; the music, creator and
            video tabs return no logged-out data, so we cannot produce ``sound``
            (or other) trend types right now — only ``hashtag``.
          * The logged-out tier exposes only the **top 3** global hashtags; the
            rest of the ranking — and ALL per-country filtering — is behind a
            login wall ($0 mandate rules out the account treadmill — ADR-0001).
          * **KZ and CIS are not supported markets** (27-country list, none CIS),
            and country filtering is login-gated anyway, so those tiers return []
            — no native source exists to honestly fill them.
          * "World" maps to TikTok's single global/default list — the only thing
            the free tier actually serves.
        """
        if geo_tier in ("KZ", "CIS"):
            # No KZ/CIS market in Creative Center, and the free loader ignores
            # countryCode regardless. No free per-country source exists elsewhere,
            # so [] is the contracted answer (language-inference is OPEN-QUESTIONS
            # territory, not this adapter's job).
            return []

        sampled_at = datetime.now(timezone.utc)
        items = self._fetch_hashtags(_DEFAULT_MARKET)
        trends: list[Trend] = []
        for item in items:
            if not item.get("hashtagName"):
                continue
            item = {**item, "_market": _DEFAULT_MARKET, "_period": self._trend_period}
            # Trust Creative Center's own ranking; fall back to list order.
            rank = self._int(item.get("rankIndex")) or (len(trends) + 1)
            trends.append(self._to_trend(item, rank, geo_tier, sampled_at))
        return trends

    def _fetch_hashtags(self, country: str) -> list[dict]:
        """Fetch the free top-N trending-hashtag items for one market.

        Returns [] (rather than raising) on a fetch/parse failure: a missing
        trends section should degrade the digest, not crash ingestion.
        """
        url = _CC_TREND_URL.format(
            country=country,
            period=self._trend_period,
            loader=urllib.parse.quote(_CC_LOADER_PATH, safe=""),
        )
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            return []
        return self._extract_list(payload)

    @staticmethod
    def _extract_list(payload: dict) -> list[dict]:
        """Pull the hashtag list out of the React-Query dehydrated state.

        Shape: dehydratedState.queries[*].state.data.pages[0].data == [items...].
        Tolerant of the exact query ordering (we scan for the paginated one).
        """
        queries = (payload.get("dehydratedState") or {}).get("queries") or []
        for q in queries:
            data = (q.get("state") or {}).get("data")
            if isinstance(data, dict) and isinstance(data.get("pages"), list):
                pages = data["pages"]
                if pages and isinstance(pages[0], dict):
                    items = pages[0].get("data")
                    if isinstance(items, list):
                        return items
        return []

    def _to_trend(
        self, item: dict, rank: int, geo_tier: GeoTier, sampled_at: datetime
    ) -> Trend:
        return Trend(
            platform=self.platform,
            trend_type="hashtag",  # only type Creative Center exposes free today
            name=item["hashtagName"],
            sampled_at=sampled_at,
            raw=item,  # complete Creative Center item + injected _country/_period
            geo_tier=geo_tier,
            rank=rank,  # merged global rank; per-market rankIndex is in raw
            score=self._num(item.get("vv")),  # view volume
            volume=self._int(item.get("publishCnt")),  # # of posts using the tag
        )

    # == discovery (Layer 2): For-You harvest ==================================

    def fetch_viral_posts(
        self,
        geo_tier: GeoTier,
        period_days: int = 7,
        hashtags: "list[str] | None" = None,
        locations: "list[str] | None" = None,
    ) -> list[PostRecord]:
        """Discover viral posts NOT tied to a Watched Account, by harvesting
        TikTok's logged-out For-You feed (docs/DISCOVERY.md, the FYP spine).

        The feed is region-shaped by the KZ IP + locale/timezone; no login, so no
        account to ban (only IP throttling). Returns PostRecords ordered by a
        PROVISIONAL score (NOT the final viral rule — OPEN-QUESTIONS Q-1); the
        score, rank and a ``provisional: True`` flag are stamped into each
        ``raw['_discovery']``.

        ``hashtags``/``locations`` are accepted for interface parity but are NOT
        used here: verified 2026-06-25 that logged-out hashtag/search item_lists
        come back EMPTY (gated). Seed-based education shaping needs the signed
        TikTok-Api ``hashtag(name).videos()`` path or a warmed burner — left as the
        documented next step, not silently faked.
        """
        from adapters.tiktok import discovery  # noqa: PLC0415  (heavy: Playwright)

        if hashtags or locations:
            self.last_discovery_note = (
                "hashtags/locations ignored: logged-out FYP is algorithmic and "
                "logged-out hashtag/search item_lists are empty (see docstring)."
            )

        result = discovery.harvest(
            geo_tier,
            surface=self._discovery_surface,  # "explore" by default; see discovery.py
            cycles=self._fyp_cycles,
            max_advances=self._fyp_max_advances,
            headless=self._tiktokapi_headless,  # headed required; reuse the flag
        )

        fetched_at = datetime.now(timezone.utc)
        cutoff = fetched_at - timedelta(days=period_days)
        records: list[PostRecord] = []
        dropped_old = 0
        for item in result.items:
            try:
                rec = self._record_from_api(item, fetched_at, geo_tier=geo_tier)
            except (KeyError, TypeError):
                continue  # malformed item; skip rather than crash the harvest
            # Period filter (docs/DISCOVERY.md). NOTE: logged-out FYP surfaces
            # EVERGREEN-viral content (verified 60–90 days old), so a tight
            # period_days drops most of it — raise period_days for this source, or
            # treat feed presence (not post date) as the recency signal. Undated
            # posts are kept rather than silently dropped.
            if rec.posted_at is not None and rec.posted_at < cutoff:
                dropped_old += 1
                continue
            records.append(rec)

        self.last_discovery_note = (
            f"{result.note}; harvested {len(result.items)}, "
            f"{len(records)} within {period_days}d, {dropped_old} older "
            f"(FYP skews evergreen — raise period_days)"
        )

        # Provisional ranking + dedupe is already done by item id in the harvester.
        records.sort(key=self._provisional_score, reverse=True)
        for rank, rec in enumerate(records, start=1):
            rec.raw["_discovery"] = {
                "provisional": True,  # NOT the final viral rule (Q-1)
                "provisional_score": self._provisional_score(rec),
                "rank": rank,
                "source": "tiktok_fyp_logged_out",
                "geo_tier": geo_tier,
                "sampled_at": fetched_at.isoformat(),
            }
        return records

    @staticmethod
    def _provisional_score(rec: PostRecord) -> int:
        """docs/DISCOVERY.md provisional score: views if known, else engagement."""
        if rec.view_count is not None:
            return rec.view_count
        return sum(
            v or 0
            for v in (rec.like_count, rec.comment_count, rec.share_count, rec.save_count)
        )


if __name__ == "__main__":  # pragma: no cover — manual smoke test
    import sys

    adapter = TikTokAdapter()
    print("== fetch_trends('World') ==")
    trends = adapter.fetch_trends("World")
    print(f"{len(trends)} hashtag trends")
    for t in trends[:10]:
        print(f"  #{t.rank:>2} {t.name:<28} views={t.score} posts={t.volume} "
              f"({t.raw.get('_market')})")
    print("  KZ ->", len(adapter.fetch_trends("KZ")), "| CIS ->",
          len(adapter.fetch_trends("CIS")))

    handle = sys.argv[1] if len(sys.argv) > 1 else "khanacademy"
    acct = WatchedAccount(handle=handle, platform="tiktok",
                          segment="global_edtech", geo_tier="World")
    print(f"\n== fetch_account_posts(@{handle}, limit=3) ==")
    posts = adapter.fetch_account_posts(acct, limit=3)
    path = "yt-dlp (fallback)" if adapter.last_tiktokapi_error else "TikTok-Api (signed)"
    print(f"served by: {path}")
    if adapter.last_tiktokapi_error:
        print(f"  (TikTok-Api error: {str(adapter.last_tiktokapi_error)[:80]})")
    for p in posts:
        print(f"  {p.platform_post_id} views={p.view_count} likes={p.like_count} "
              f"saves={p.save_count} sound_id={p.sound_id} sound={p.sound_name!r} "
              f"tags={p.hashtags[:4]}")

    geo = sys.argv[2] if len(sys.argv) > 2 else "KZ"
    print(f"\n== fetch_viral_posts('{geo}') — logged-out For-You harvest ==")
    viral = adapter.fetch_viral_posts(geo, period_days=14)
    print(f"{len(viral)} ranked posts | note: {adapter.last_discovery_note}")
    for p in viral[:10]:
        d = p.raw.get("_discovery", {})
        print(f"  #{d.get('rank'):>2} @{p.account_handle:<20} score={d.get('provisional_score'):>9} "
              f"views={p.view_count} posted={p.posted_at.date() if p.posted_at else '?'} "
              f"desc={(p.caption or '')[:40]!r}")
