"""Guest-token GraphQL client for X — the OPTIONAL deepening path.

Why this exists: the default syndication path (see adapter.py) can't see
`view_count`. X's own web GraphQL API *can*, and it's reachable with only a
**guest token** — no login, no account, no API key. That matters for ban risk:
there is no account to ban, only a guest token to rate-limit. Verified working
from a home IP on 2026-06-25 (recovered real view counts for @NASA).

Cost of using it (why it's off by default): far more fragile than syndication —
the GraphQL query-ids and the `features` blob both rotate on X's release
schedule. We mitigate by (a) trying a list of known query-ids and (b)
auto-healing the `features` blob from X's own 400 error messages. When X moves
faster than this, callers fall back to syndication.

Per ADR-0001 / the round-two playbook: low volume, polite, treat 401/403/429 as
"back off." This client is read-only competitive monitoring of public posts.

Round-three update (2026-06-26): added SearchTimeline with cursor pagination for
≥500-post unattended harvest across multiple seed queries. Added exponential
backoff + guest-token refresh on 429.
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# Public web bearer baked into X's web app for years — not a secret credential,
# it's what every anonymous web visitor's browser sends.
_PUBLIC_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Query-ids rotate on X's release schedule; we try each in turn. Verified head
# of list working 2026-06-25. Add newer ids to the front when these break.
_USERBYSCREENNAME_QIDS = ["G3KGOASz96M-Qu0nwmGXNg", "sLVLhk0bGj3MVFEKTdax1w"]
_USERTWEETS_QIDS = [
    "E3opETHurmVJflFsUBVuUQ",
    "V7H0Ap3_Hh2FyS75OCDO3Q",
    "Q6aAvPw7azXZbqXzuqTALA",
]

# SearchTimeline query-ids — verified candidates from X's web bundle.
# Try each in order; add newer ids to the front when all break.
# NOTE (2026-06-26): SearchTimeline returns HTTP 404 for guest-token requests
# even with the correct query ID (Bcw3RzK-PatNAmbnw54hFw, confirmed from the
# main.a1a43c5a.js bundle). This is a login-gate: the endpoint accepts only
# authenticated (user-bearer) tokens, not anonymous guest tokens. All candidates
# below have been tested and return 404. Kept for forward-compatibility in case
# X re-opens the endpoint, or a caller has a user-bearer token.
_SEARCHTIMELINE_QIDS = [
    "Bcw3RzK-PatNAmbnw54hFw",  # extracted 2026-06-26 from main.a1a43c5a.js — still 404
    "nLpSMYOzuEcFVPtFLxJFLA",
    "gkjsKepM6gl_HmFWoWKfgg",
    "AIdc203v_gE9MfgULZrULg",
    "lZ7Q_mHUU6V9Yz4SBrLmhA",
    "TrJWRLFBjcxZVXFMQqSufg",
    "5wxkMSHHWBThLfHBWTlblw",
    "oSboT2K7L4rHb_sERWa_Rg",
]

# Seed feature flags. Missing ones are auto-healed from X's 400 error text, so
# this list only needs to be "close enough" — it self-corrects at runtime.
_BASE_FEATURES = {
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "rweb_video_timestamps_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
}

# Extra feature flags common in SearchTimeline responses
_SEARCH_EXTRA_FEATURES = {
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "interactive_text_enabled": True,
    "responsive_web_text_conversations_enabled": False,
    "vibe_api_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_twitter_article_tweet_consumption_enabled": False,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "articles_preview_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}


class XGraphQLError(RuntimeError):
    """GraphQL path failed — callers should fall back to syndication."""


class XRateLimitError(XGraphQLError):
    """HTTP 429 — rate-limited. Caller should back off."""


class XGraphQLClient:
    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout = timeout
        self._guest_token: str | None = None
        # Running count of 429s and backoffs seen during this session
        self.rate_limit_hits: int = 0
        self.backoff_seconds_total: float = 0.0

    # -- low-level ----------------------------------------------------------

    def _request(self, method: str, url: str, *, with_guest: bool = True) -> tuple[int, str]:
        headers = {"Authorization": f"Bearer {_PUBLIC_BEARER}", "User-Agent": _UA}
        if with_guest:
            headers["x-guest-token"] = self._ensure_guest_token()
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def _ensure_guest_token(self) -> str:
        if self._guest_token:
            return self._guest_token
        req = urllib.request.Request(
            "https://api.twitter.com/1.1/guest/activate.json",
            method="POST",
            headers={"Authorization": f"Bearer {_PUBLIC_BEARER}", "User-Agent": _UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                token = json.loads(resp.read()).get("guest_token")
        except urllib.error.HTTPError as e:
            raise XGraphQLError(f"guest token activation failed: HTTP {e.code}") from e
        if not token:
            raise XGraphQLError("guest token activation returned no token")
        self._guest_token = token
        return token

    def refresh_guest_token(self) -> str:
        """Force-refresh guest token (call after 429 to get a clean token)."""
        self._guest_token = None
        return self._ensure_guest_token()

    def _gql(self, qid: str, op: str, variables: dict, features: dict) -> tuple[int, str]:
        qs = urllib.parse.urlencode(
            {"variables": json.dumps(variables), "features": json.dumps(features)}
        )
        return self._request("GET", f"https://api.twitter.com/graphql/{qid}/{op}?{qs}")

    def _gql_with_backoff(
        self,
        qid: str,
        op: str,
        variables: dict,
        features: dict,
        *,
        max_retries: int = 4,
    ) -> tuple[int, str]:
        """Like _gql but exponential-backoff on 429 + guest-token refresh."""
        delay = 5.0
        for attempt in range(max_retries + 1):
            status, body = self._gql(qid, op, variables, features)
            if status != 429:
                return status, body
            self.rate_limit_hits += 1
            if attempt >= max_retries:
                return status, body  # exhausted retries
            # Jitter: delay × [1.0, 1.5]
            sleep_secs = delay * (1.0 + 0.5 * random.random())
            self.backoff_seconds_total += sleep_secs
            time.sleep(sleep_secs)
            delay = min(delay * 2, 120.0)
            # Refresh guest token — it may be exhausted
            try:
                self.refresh_guest_token()
            except XGraphQLError:
                pass  # will retry with old token; next 429 will try again
        return status, body

    # -- operations ---------------------------------------------------------

    def get_user_rest_id(self, handle: str) -> str:
        handle = handle.lstrip("@")
        variables = {"screen_name": handle, "withSafetyModeUserFields": True}
        last = ""
        for qid in _USERBYSCREENNAME_QIDS:
            status, body = self._gql(qid, "UserByScreenName", variables, _BASE_FEATURES)
            if status == 200 and '"rest_id"' in body:
                return json.loads(body)["data"]["user"]["result"]["rest_id"]
            last = f"HTTP {status}: {body[:120]}"
        raise XGraphQLError(f"UserByScreenName failed for @{handle} ({last})")

    def get_user_tweets(self, rest_id: str, count: int = 20) -> list[dict]:
        """Return the raw `tweet_results.result` nodes from the user timeline."""
        results, _ = self.get_user_tweets_page(rest_id, count=count)
        return results

    def get_user_tweets_page(
        self,
        rest_id: str,
        *,
        count: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """Fetch one page of user tweets and return ``(results, next_cursor)``.

        Pass the returned ``next_cursor`` back in subsequent calls to paginate
        deeper into the user's timeline. Returns ``None`` for next_cursor when
        the timeline is exhausted.

        Rate-limit (429) is handled by ``_gql_with_backoff`` so individual
        transient limits are retried transparently.
        """
        variables: dict = {
            "userId": rest_id,
            "count": count,
            "includePromotedContent": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor is not None:
            variables["cursor"] = cursor

        last = ""
        for qid in _USERTWEETS_QIDS:
            features = dict(_BASE_FEATURES)
            for _ in range(8):  # heal at most 8 rounds of missing-feature 400s
                status, body = self._gql_with_backoff(qid, "UserTweets", variables, features)
                if status == 200:
                    data = json.loads(body)
                    results = self._extract_results(data)
                    next_cursor = self._extract_bottom_cursor(data)
                    return results, next_cursor
                missing = re.findall(r"The following features cannot be null: ([^\"]+)", body)
                if status == 400 and missing:
                    for name in missing[0].split(","):
                        features[name.strip()] = True
                    continue
                last = f"HTTP {status}: {body[:120]}"
                break
        raise XGraphQLError(f"UserTweets failed for rest_id={rest_id} ({last})")

    def search_timeline(
        self,
        query: str,
        *,
        count: int = 20,
        cursor: str | None = None,
        product: str = "Latest",
        max_heal_rounds: int = 8,
    ) -> tuple[list[dict], str | None]:
        """Search X for ``query`` via SearchTimeline.

        Returns ``(results, next_cursor)``.  ``next_cursor`` is ``None`` when
        the feed is exhausted or when no cursor entry was found in the response
        (common without login after 2-3 pages).

        The caller is responsible for rate-limit backoff at the page level;
        this method uses ``_gql_with_backoff`` so individual 429s are retried
        transparently.

        ``product`` is ``"Latest"`` (reverse-chron) or ``"Top"`` (algo-ranked).
        """
        variables: dict = {
            "rawQuery": query,
            "count": count,
            "querySource": "typed_query",
            "product": product,
        }
        if cursor is not None:
            variables["cursor"] = cursor

        last_error = ""
        for qid in _SEARCHTIMELINE_QIDS:
            features = {**_BASE_FEATURES, **_SEARCH_EXTRA_FEATURES}
            for heal_round in range(max_heal_rounds):
                status, body = self._gql_with_backoff(
                    qid, "SearchTimeline", variables, features
                )
                if status == 200:
                    data = json.loads(body)
                    results, next_cursor = self._extract_search_results(data)
                    return results, next_cursor
                # Auto-heal missing features
                missing = re.findall(r"The following features cannot be null: ([^\"]+)", body)
                if status == 400 and missing:
                    for name in missing[0].split(","):
                        features[name.strip()] = True
                    continue
                # 429 already handled by _gql_with_backoff — if we're still here it's terminal
                last_error = f"HTTP {status} (qid={qid}): {body[:200]}"
                break  # try next qid

        raise XGraphQLError(f"SearchTimeline failed for {query!r} ({last_error})")

    # -- extraction helpers -------------------------------------------------

    @staticmethod
    def _instructions_from(data: dict) -> list[dict]:
        """Navigate the nested search / user-timeline response to find instructions."""
        # SearchTimeline path
        search_by_raw = (
            data.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )
        if search_by_raw:
            return search_by_raw
        # UserTweets path (fallback for _extract_results)
        user = data.get("data", {}).get("user", {}).get("result", {})
        timeline = user.get("timeline_v2") or user.get("timeline") or {}
        return timeline.get("timeline", {}).get("instructions", [])

    @classmethod
    def _extract_search_results(cls, data: dict) -> tuple[list[dict], str | None]:
        """Extract tweet result nodes and the bottom cursor from SearchTimeline."""
        instructions = cls._instructions_from(data)
        results: list[dict] = []
        next_cursor: str | None = None
        for instr in instructions:
            entries = instr.get("entries", [])
            if "entry" in instr:
                entries = entries + [instr["entry"]]
            for entry in entries:
                entry_id = entry.get("entryId", "")
                content = entry.get("content", {})
                # Bottom cursor
                if (
                    content.get("cursorType") == "Bottom"
                    or content.get("entryType") == "TimelineTimelineCursor"
                    and content.get("cursorType") == "Bottom"
                ):
                    next_cursor = content.get("value")
                    continue
                # Also check __typename
                if content.get("__typename") == "TimelineTimelineCursor":
                    if content.get("cursorType") == "Bottom":
                        next_cursor = content.get("value")
                    continue
                # Tweet entries: entryId starts with "sq-I-t-" or "tweet-" in search
                if not (
                    entry_id.startswith("sq-I-t-")
                    or entry_id.startswith("tweet-")
                    or "tweet" in entry_id.lower()
                ):
                    continue
                # Flat itemContent
                item = content.get("itemContent", {})
                tr = item.get("tweet_results", {})
                result = tr.get("result")
                if result:
                    if result.get("__typename") == "TweetWithVisibilityResults":
                        result = result.get("tweet", result)
                    if "legacy" in result:
                        results.append(result)
                        continue
                # Module items (TimelineTimelineModule)
                for module_item in content.get("items", []):
                    mitem = module_item.get("item", {}).get("itemContent", {})
                    mtr = mitem.get("tweet_results", {})
                    mresult = mtr.get("result")
                    if mresult:
                        if mresult.get("__typename") == "TweetWithVisibilityResults":
                            mresult = mresult.get("tweet", mresult)
                        if "legacy" in mresult:
                            results.append(mresult)
        return results, next_cursor

    @staticmethod
    def _extract_bottom_cursor(data: dict) -> str | None:
        """Extract the bottom-cursor token from a UserTweets response."""
        user = data.get("data", {}).get("user", {}).get("result", {})
        timeline = user.get("timeline_v2") or user.get("timeline") or {}
        instructions = timeline.get("timeline", {}).get("instructions", [])
        for instr in instructions:
            entries = instr.get("entries", [])
            if "entry" in instr:
                entries = entries + [instr["entry"]]
            for entry in entries:
                eid = entry.get("entryId", "")
                content = entry.get("content", {})
                if (
                    "cursor-bottom" in eid
                    or content.get("cursorType") == "Bottom"
                    or content.get("__typename") == "TimelineTimelineCursor"
                    and content.get("cursorType") == "Bottom"
                ):
                    val = content.get("value")
                    if val:
                        return val
        return None

    @staticmethod
    def _extract_results(data: dict) -> list[dict]:
        """Extract tweet result nodes from UserTweets response."""
        user = data.get("data", {}).get("user", {}).get("result", {})
        timeline = user.get("timeline_v2") or user.get("timeline") or {}
        instructions = timeline.get("timeline", {}).get("instructions", [])
        results: list[dict] = []
        for instr in instructions:
            entries = instr.get("entries", [])
            if "entry" in instr:  # TimelinePinEntry carries a single entry
                entries = entries + [instr["entry"]]
            for entry in entries:
                if not entry.get("entryId", "").startswith("tweet-"):
                    continue
                content = entry.get("content", {})
                tr = content.get("itemContent", {}).get("tweet_results", {})
                result = tr.get("result")
                if not result:
                    continue
                # TweetWithVisibilityResults wraps the real tweet under .tweet
                if result.get("__typename") == "TweetWithVisibilityResults":
                    result = result.get("tweet", result)
                if "legacy" in result:
                    results.append(result)
        return results
