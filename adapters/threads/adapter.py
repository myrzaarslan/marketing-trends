"""Threads adapter — public web-UI scraper driven by Playwright.

Round-one finding (still true 2026-06): public Threads profiles load
**unauthenticated** from a home IP. The profile HTML is a shell
(``"ssrEnabled":false``); posts arrive via a GraphQL POST to ``/api/graphql`` that
needs a *current* ``doc_id`` — a rotating persisted-query hash fetched at runtime
via Meta's Bootloader (no static URL to scrape it from).

Round-two hardening (this file):

* **doc_id is acquired, never hardcoded.** A headless browser loads the profile and
  we intercept the profile-feed GraphQL request *by ``fb_api_req_friendly_name``*
  (loose substring match, not a pinned id), reading the live ``doc_id`` and the
  Threads ``userID`` off it for observability (``last_doc_id`` / ``last_user_id``).
  Posts are harvested from the response **by shape, not by path**, so the adapter
  survives query-shape changes.
* **Login-wall fallback.** Threads reuses Instagram's auth and could adopt IG's
  login wall at any time. Pass a burner ``ThreadsSession`` (cookies / storage_state
  / instagrapi bridge — see session.py) and the load runs authenticated; otherwise
  a wall is detected and raised loudly (``ThreadsLoginWall``).

Why not a lighter ``requests``-only replay of the captured ``doc_id``? Investigated
and deliberately not adopted — see README.md §"Why the browser stays in the loop".
Short version: the one profile-feed request is a Relay *refetchable* query that
already carries a browser-minted pagination cursor, and the web GraphQL endpoint is
integrity-bound (``lsd``/``jazoest``/``__spin``/csr). Reproducing that outside the
browser is the exact signing/gating wall ADR-0001 says not to hand-roll at $0.

This adapter FETCHES and NORMALIZES only — no persistence, scheduling, or virality.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import requests

from core.adapter import PlatformAdapter
from core.schema import GeoTier, MediaType, PostRecord, Trend, WatchedAccount

from adapters.threads.session import ThreadsSession

_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)

# Meta's web app id for the Instagram backend; public, sent by the web client.
# Used only by the optional Threads-presence pre-check (NOT the feed path).
_IG_APP_ID = "936619743392459"

# Friendly-name fragments for the profile post-feed GraphQL queries (initial load
# + pagination). We scope by these so reposts/quotes by the watched account come
# through, but recommendation / related-profile payloads do not. Loose substring
# match rides out minor renames without hardcoding a doc_id.
_PROFILE_FEED_HINTS = ("profilethreads", "profiletimeline", "profilereplies")

# Keys that together mark a dict as a Threads/Barcelona "post" node inside an
# arbitrary GraphQL payload. We harvest by shape, not path.
_POST_MARKER_KEYS = ("pk", "code", "caption", "taken_at", "like_count")


class ThreadsLoginWall(RuntimeError):
    """Raised when Threads gates a public profile behind login and no usable
    ThreadsSession was supplied. See adapters/threads/README.md."""


class ThreadsRateLimited(RuntimeError):
    """Raised when Threads returns HTTP 429 (rate limit). The IP is hot; back off
    before retrying. Never solve this with aggressive retries — that deepens the ban.
    See adapters/threads/README.md §Rate limits."""


class ThreadsAdapter(PlatformAdapter):
    """Adapter for Meta's Threads (text-first social platform)."""

    platform = "threads"

    #: public web origin (Threads migrated .net -> .com; both resolve here)
    BASE_URL = "https://www.threads.com"

    def __init__(
        self,
        *,
        session: Optional[ThreadsSession] = None,
        headless: bool = True,
        nav_timeout_ms: int = 45_000,
        user_agent: Optional[str] = None,
        scroll_stall_limit: int = 6,
    ) -> None:
        self.session = session
        self.headless = headless
        self.nav_timeout_ms = nav_timeout_ms
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        )
        # Number of consecutive stall-scrolls (no new posts) before giving up.
        # Higher values try harder for depth; 6 is the default (was 4 before).
        self.scroll_stall_limit = scroll_stall_limit
        # Observability for the last fetch — the rotating doc_id we actually used,
        # the Threads userID we observed (its own namespace, != the IG id), and the
        # Relay pagination state (has_next_page / end_cursor) from the last response.
        self.last_doc_id: Optional[str] = None
        self.last_user_id: Optional[str] = None
        self.last_has_next_page: Optional[bool] = None
        self.last_end_cursor: Optional[str] = None

    # ------------------------------------------------------------------ public

    def fetch_account_posts(
        self, account: WatchedAccount, limit: int = 30
    ) -> list[PostRecord]:
        """Recent posts for one Watched Account, newest first."""
        handle = account.handle.lstrip("@")

        # Pre-fetch the account's follower count from the IG web profile API.
        # The embedded user stub in post nodes doesn't carry follower_count, but
        # the same IG backend exposes it via web_profile_info (one GET per account,
        # no browser required). None on any error — adapters are defensive.
        follower_count = self._fetch_profile_follower_count(handle)

        raw_posts = self._harvest_via_browser(handle, limit)

        records: list[PostRecord] = []
        for raw in raw_posts:
            rec = self._normalize_post(raw, account, follower_count=follower_count)
            if rec is not None:
                records.append(rec)
            if len(records) >= limit:
                break
        return records

    def fetch_trends(self, geo_tier: GeoTier) -> list[Trend]:
        # Threads exposes no free trend source: no public trending surface, no
        # Creative-Center equivalent, official API is owned-account only. Returning
        # [] is the contract-correct outcome (see README / OPEN-QUESTIONS Q-2).
        return []

    def _fetch_profile_follower_count(self, handle: str) -> Optional[int]:
        """Return the account's follower count from the IG web_profile_info endpoint.

        The embedded ``user`` stub inside each post node is a minimal identity dict
        that doesn't carry ``follower_count``. This single GET per account fills the
        gap using the same IG backend path that ``account_is_on_threads`` uses.
        Returns ``None`` on any error so callers can proceed without blocking.
        """
        headers = {
            "User-Agent": self.user_agent,
            "X-IG-App-ID": _IG_APP_ID,
            "Accept": "application/json",
        }
        cookies = self.session.requests_cookies() if self.session else None
        try:
            resp = requests.get(
                "https://i.instagram.com/api/v1/users/web_profile_info/",
                params={"username": handle},
                headers=headers,
                cookies=cookies,
                timeout=20,
            )
            if resp.status_code != 200:
                return None
            user = (resp.json().get("data") or {}).get("user") or {}
            # IG GraphQL v1 shape: edge_followed_by.count
            edge = user.get("edge_followed_by")
            if isinstance(edge, dict):
                cnt = edge.get("count")
                if isinstance(cnt, int):
                    return cnt
            # Fallback: newer API variants may flatten to follower_count
            cnt = user.get("follower_count")
            if isinstance(cnt, int):
                return cnt
            return None
        except (requests.RequestException, ValueError):
            return None

    def account_is_on_threads(self, handle: str) -> Optional[bool]:
        """Cheap pre-check: is this account on Threads at all?

        Uses Instagram's ``web_profile_info`` (Threads reuses IG's backend), which
        exposes ``has_onboarded_to_text_post_app``. NOTE: the ``id`` this endpoint
        returns is the *Instagram* user id, which is a DIFFERENT namespace from the
        Threads ``userID`` the feed query needs — so it cannot drive the feed, only
        gate whether a browser fetch is worth attempting. Returns ``None`` on error.
        """
        handle = handle.lstrip("@")
        headers = {
            "User-Agent": self.user_agent,
            "X-IG-App-ID": _IG_APP_ID,
            "Accept": "application/json",
        }
        cookies = self.session.requests_cookies() if self.session else None
        try:
            resp = requests.get(
                "https://i.instagram.com/api/v1/users/web_profile_info/",
                params={"username": handle},
                headers=headers,
                cookies=cookies,
                timeout=20,
            )
            if resp.status_code != 200:
                return None
            user = (resp.json().get("data") or {}).get("user") or {}
            return bool(user.get("has_onboarded_to_text_post_app"))
        except (requests.RequestException, ValueError):
            return None

    # --------------------------------------------------------------- collection

    def _harvest_via_browser(self, handle: str, limit: int) -> list[dict[str, Any]]:
        """Load the profile in headless Chromium; collect post nodes from the
        profile-feed GraphQL responses and record the live doc_id / userID."""
        # Lazy import so the module/normalizer stay importable without Playwright.
        from playwright.sync_api import sync_playwright

        collected: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        self.last_doc_id = None
        self.last_user_id = None
        self.last_has_next_page = None
        self.last_end_cursor = None
        rate_limited = [False]  # mutable flag for the closure

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(
                user_agent=self.user_agent,
                locale="en-US",
                viewport={"width": 1280, "height": 2400},
            )
            if self.session and self.session.is_usable:
                context.add_cookies(self.session.playwright_cookies())
            page = context.new_page()

            def on_request(request: Any) -> None:
                # Record the live doc_id + Threads userID off the signed feed
                # request, by friendly-name (no hardcoded doc_id).
                if "/graphql" not in request.url:
                    return
                if not _is_profile_feed_request(request):
                    return
                doc_id, user_id = _read_doc_and_user(request)
                if doc_id and not self.last_doc_id:
                    self.last_doc_id = doc_id
                if user_id and not self.last_user_id:
                    self.last_user_id = user_id

            def on_response(response: Any) -> None:
                # Rate-limit signal — catch 429 on any request, not just /graphql.
                if response.status == 429:
                    rate_limited[0] = True
                    return
                if "/graphql" not in response.url:
                    return
                if not _is_profile_feed_request(response.request):
                    # Skip recommendation / related-profile payloads.
                    return
                try:
                    if "application/json" in response.headers.get("content-type", ""):
                        payload = response.json()
                        _absorb(payload, collected, seen_ids)
                        # Track Relay pagination state so callers know if more pages exist.
                        pi = _extract_page_info(payload)
                        if pi is not None:
                            self.last_has_next_page = bool(pi.get("has_next_page"))
                            ec = pi.get("end_cursor")
                            if ec:
                                self.last_end_cursor = str(ec)
                except Exception:
                    return

            page.on("request", on_request)
            page.on("response", on_response)

            try:
                page.goto(
                    f"{self.BASE_URL}/@{handle}",
                    wait_until="domcontentloaded",
                    timeout=self.nav_timeout_ms,
                )
                page.wait_for_timeout(2500)
                if not collected and _looks_like_login_wall(page):
                    if not (self.session and self.session.is_usable):
                        raise ThreadsLoginWall(
                            "Threads gated @%s behind login and no usable burner "
                            "ThreadsSession was provided. See "
                            "adapters/threads/README.md (login-wall fallback)."
                            % handle
                        )
                stalls = 0
                while len(collected) < limit and stalls < self.scroll_stall_limit:
                    if rate_limited[0]:
                        break
                    before = len(collected)
                    page.mouse.wheel(0, 6000)
                    page.wait_for_timeout(2000)
                    stalls = stalls + 1 if len(collected) <= before else 0
            finally:
                context.close()
                browser.close()

        if rate_limited[0]:
            raise ThreadsRateLimited(
                f"Threads returned HTTP 429 while fetching @{handle}. "
                "Back off — IP may be hot. See adapters/threads/README.md §Rate limits."
            )

        return collected

    # --------------------------------------------------------------- normalize

    def _normalize_post(
        self,
        raw: dict[str, Any],
        account: WatchedAccount,
        *,
        follower_count: Optional[int] = None,
    ) -> Optional[PostRecord]:
        """Map one Threads post node to a normalized PostRecord. Defensive by
        design; the COMPLETE original node is preserved in ``raw``."""
        code = raw.get("code")
        pk = raw.get("pk") or raw.get("id")
        if not (code or pk):
            return None

        user = raw.get("user") or {}
        username = user.get("username") or account.handle.lstrip("@")
        post_id = str(pk or code)
        url = (
            f"{self.BASE_URL}/@{username}/post/{code}"
            if code
            else f"{self.BASE_URL}/t/{post_id}"
        )

        caption_obj = raw.get("caption") or {}
        caption = caption_obj.get("text") if isinstance(caption_obj, dict) else None
        tpa = raw.get("text_post_app_info") or {}

        return PostRecord(
            platform=self.platform,
            platform_post_id=post_id,
            account_handle=username,
            url=url,
            fetched_at=datetime.now(timezone.utc),
            media_type=_media_type(raw),
            raw=raw,
            posted_at=_epoch_to_dt(raw.get("taken_at")),
            caption=caption,
            hashtags=_hashtags(caption, tpa),
            sound_id=_sound_id(raw),
            sound_name=_sound_name(raw),
            duration_sec=_duration_sec(raw),
            # view_count not exposed for most Threads posts; capture play_count
            # opportunistically on video posts, else None (per SIGNALS.md).
            view_count=_first_int(raw, ("play_count",)) if _is_video(raw) else None,
            like_count=_first_int(raw, ("like_count",)),
            comment_count=_first_int(tpa, ("direct_reply_count",))
            or _first_int(raw, ("reply_count", "comment_count")),
            share_count=_first_int(tpa, ("repost_count", "reshare_count")),
            save_count=None,  # Threads exposes no public save/bookmark count
            thumbnail_url=_thumbnail(raw),
            geo_tier=account.geo_tier,
            # follower count — pre-fetched from web_profile_info (the post-node
            # user stub is minimal and never carries follower_count). Falls back
            # to the user stub in case a future Threads schema change embeds it.
            author_follower_count=follower_count
            if follower_count is not None
            else _first_int(user, ("follower_count", "edge_followed_by_count")),
        )


# ----------------------------------------------------- request introspection

def _is_profile_feed_request(request: Any) -> bool:
    try:
        body = request.post_data or ""
    except Exception:
        return False
    if not body:
        return False
    name = ""
    for part in body.split("&"):
        if part.startswith("fb_api_req_friendly_name="):
            name = part.split("=", 1)[1].lower()
            break
    if not name:
        # No friendly name (unusual): allow, so we don't silently drop the only
        # feed payload if Meta drops the field.
        return True
    return any(hint in name for hint in _PROFILE_FEED_HINTS)


def _read_doc_and_user(request: Any) -> tuple[Optional[str], Optional[str]]:
    """Extract (doc_id, Threads userID) off a signed feed request, for logging."""
    try:
        body = request.post_data or ""
    except Exception:
        return None, None
    doc_id: Optional[str] = None
    variables: Optional[str] = None
    for part in body.split("&"):
        if part.startswith("doc_id="):
            doc_id = part.split("=", 1)[1] or None
        elif part.startswith("variables="):
            from urllib.parse import unquote
            variables = unquote(part.split("=", 1)[1])
    user_id = None
    if variables:
        try:
            v = json.loads(variables)
            for key in ("userID", "user_id", "id"):
                if v.get(key):
                    user_id = str(v[key])
                    break
        except (ValueError, TypeError):
            pass
    return doc_id, user_id


def _looks_like_login_wall(page: Any) -> bool:
    try:
        if "/login" in page.url:
            return True
        html = page.content()
    except Exception:
        return False
    lowered = html.lower()
    markers = ("log in to threads", "/login/?", "loginform", "view in app to continue")
    return any(m in lowered for m in markers)


# ---------------------------------------------------------------- harvesting

def _absorb(payload: Any, collected: list[dict[str, Any]], seen: set[str]) -> None:
    for node in _iter_post_nodes(payload):
        key = str(node.get("pk") or node.get("code") or id(node))
        if key in seen:
            continue
        seen.add(key)
        collected.append(node)


def _extract_page_info(payload: Any) -> Optional[dict[str, Any]]:
    """Walk the payload looking for a Relay page_info node (has_next_page field).

    Threads uses Relay connections; paginated responses carry a ``page_info`` object
    with ``has_next_page`` (bool) and ``end_cursor`` (opaque string). We expose these
    on the adapter for observability — see ``last_has_next_page`` / ``last_end_cursor``.
    Scroll-triggered follow-up requests use the cursor automatically; we can't replay
    the signed request outside the browser (see README §Why the browser stays in the loop).
    """
    stack: list[Any] = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "has_next_page" in cur:
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _iter_post_nodes(payload: Any) -> Iterable[dict[str, Any]]:
    """Walk an arbitrary GraphQL payload, yield dicts that look like post nodes."""
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if _looks_like_post(cur):
                yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _looks_like_post(d: dict[str, Any]) -> bool:
    # Require ``code`` — the Threads/Instagram shortcode used in canonical public URLs.
    # Nodes without it are carousel sub-items or thread-chain continuation stubs: they
    # carry no engagement stats, no caption, and no timestamp of their own (those all
    # live on the parent node). Accepting them was the root cause of the ~40% None-
    # engagement records in the first harvest.  Requiring code ensures we only yield
    # root-level post nodes that carry a full payload.
    if "code" not in d:
        return False
    present = sum(1 for k in _POST_MARKER_KEYS if k in d)
    return present >= 2


# ----------------------------------------------------------------- field maps

def _is_video(raw: dict[str, Any]) -> bool:
    """True when the node is a video post (used to gate opportunistic play_count)."""
    if raw.get("video_versions") or raw.get("has_video_versions"):
        return True
    mt = raw.get("media_type")
    return mt == 2


def _media_type(raw: dict[str, Any]) -> MediaType:
    # Threads reuses Instagram media-type ints: 1=image, 2=video, 8=carousel.
    # Anything else (text-only, link cards) -> "text" (Threads is text-first).
    if raw.get("carousel_media") or raw.get("carousel_media_count"):
        return "carousel"
    if _is_video(raw):
        return "video"
    mt = raw.get("media_type")
    if mt == 8:
        return "carousel"
    if mt == 1 and raw.get("image_versions2"):
        return "image"
    return "text"


def _hashtags(caption: Optional[str], tpa: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    frags_obj = tpa.get("text_fragments")
    frags = frags_obj.get("fragments") if isinstance(frags_obj, dict) else None
    if isinstance(frags, list):
        for fr in frags:
            if isinstance(fr, dict) and fr.get("fragment_type") == "hashtag":
                name = (fr.get("value") or "").lstrip("#")
                if name:
                    tags.append(name)
    if not tags and caption:
        tags = _HASHTAG_RE.findall(caption)
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _music(raw: dict[str, Any]) -> dict[str, Any]:
    clips = raw.get("clips_metadata") or {}
    info = clips.get("music_info") or {}
    return (info.get("music_asset_info") or {}) if isinstance(info, dict) else {}


def _sound_id(raw: dict[str, Any]) -> Optional[str]:
    asset = _music(raw)
    val = asset.get("audio_cluster_id") or asset.get("id")
    return str(val) if val is not None else None


def _sound_name(raw: dict[str, Any]) -> Optional[str]:
    asset = _music(raw)
    title = asset.get("title")
    artist = asset.get("display_artist")
    if title and artist:
        return f"{title} — {artist}"
    return title or None


def _duration_sec(raw: dict[str, Any]) -> Optional[float]:
    for key in ("video_duration", "duration"):
        v = raw.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _thumbnail(raw: dict[str, Any]) -> Optional[str]:
    iv2 = raw.get("image_versions2") or {}
    candidates = iv2.get("candidates") if isinstance(iv2, dict) else None
    if isinstance(candidates, list) and candidates:
        first = candidates[0]
        if isinstance(first, dict):
            return first.get("url")
    return None


def _epoch_to_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _first_int(d: dict[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None
