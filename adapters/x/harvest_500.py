"""X harvest-500: accumulate ≥500 unique posts UNATTENDED.

Strategy (per docs/handoffs/robust-harvest.md — X is HTTP/PER-REQUEST):

  Phase 1 — GraphQL UserTweets cursor pagination (depth):
    For each seed account, follow bottom-cursor pages until:
    - no cursor returned (timeline exhausted)
    - per-account page cap reached
    - persistent 429 (backoff exhausted)
    Each page adds ~15-20 unique tweets; 3-4 pages/account = 50-80 tweets/account.
    Target reachable with ~7-10 accounts.

  Phase 2 — Syndication fallback (breadth, robustness):
    If Phase 1 falls short, pull syndication timelines across many handles.
    Pure stdlib, no auth, ~20 tweets per account.

  Dedup: by tweet id_str across both phases.

SearchTimeline status (as of 2026-06-26):
  SearchTimeline is login-gated on X — returns HTTP 404 for guest-token
  requests even with the current correct query ID (Bcw3RzK-PatNAmbnw54hFw,
  confirmed from X's main.a1a43c5a.js bundle). Every known candidate query
  ID also returns 404. This is not a stale-ID problem; X enforces user-auth
  for search. See adapters/x/README.md for escalation context.

Rate-limit handling:
  - XGraphQLClient._gql_with_backoff: exponential 5→10→20→40→80s + guest-token refresh
  - Inter-page jitter: 1.5–3.0 s between pages
  - On persistent 429: move to next account, record blocker

Run from repo root:
    .venv/bin/python -m adapters.x.harvest_500
"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from core.schema import PostRecord, WatchedAccount
from adapters.x.graphql import XGraphQLClient, XGraphQLError
from adapters.x.adapter import XAdapter

# ---------------------------------------------------------------------------
# Seed accounts — used by both GraphQL (Phase 1) and syndication (Phase 2)
# ---------------------------------------------------------------------------

SEED_ACCOUNTS = [
    # Phase 1 (GraphQL cursor) — high-volume accounts for depth
    WatchedAccount(handle="NASA",          platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="OpenAI",        platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="elonmusk",      platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="nytimes",       platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="Forbes",        platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="Google",        platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="MIT",           platform="x", segment="global_edtech",   geo_tier="World"),
    WatchedAccount(handle="Stanford",      platform="x", segment="global_edtech",   geo_tier="World"),
    WatchedAccount(handle="khanacademy",   platform="x", segment="global_edtech",   geo_tier="World"),
    WatchedAccount(handle="Duolingo",      platform="x", segment="adjacent",        geo_tier="World"),
    # Extra accounts for syndication fallback
    WatchedAccount(handle="TED",           platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="HarvardBiz",    platform="x", segment="global_edtech",   geo_tier="World"),
    WatchedAccount(handle="edutopia",      platform="x", segment="global_edtech",   geo_tier="World"),
    WatchedAccount(handle="Coursera",      platform="x", segment="global_edtech",   geo_tier="World"),
    WatchedAccount(handle="BarackObama",   platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="BBCBreaking",   platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="natgeo",        platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="Wired",         platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="FastCompany",   platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="Microsoft",     platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="verge",         platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="techcrunch",    platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="github",        platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="Wikipedia",     platform="x", segment="adjacent",        geo_tier="World"),
    WatchedAccount(handle="discoverymag",  platform="x", segment="adjacent",        geo_tier="World"),
]

# GraphQL cursor pagination settings
GQL_ACCOUNTS = SEED_ACCOUNTS[:10]   # high-value accounts for Phase 1
MAX_PAGES_PER_ACCOUNT = 5            # ~20 tweets/page → up to 100/account
PAGE_SLEEP_MIN = 1.5
PAGE_SLEEP_MAX = 3.0
ACCOUNT_SLEEP_MIN = 2.0
ACCOUNT_SLEEP_MAX = 4.0

TARGET = 500

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_DATA_DIR.mkdir(exist_ok=True)
_SCRATCH_PATH = _DATA_DIR / "x_harvest_500_scratch.json"


def _save_scratch(posts: list[dict], stats: dict) -> None:
    with open(_SCRATCH_PATH, "w") as f:
        json.dump({"stats": stats, "posts": posts}, f, default=str, indent=2)


# ---------------------------------------------------------------------------
# Canonical serializer — PostRecord → dict (all fields per SIGNALS.md)
# ---------------------------------------------------------------------------

def _post_record_to_dict(p: PostRecord, source: str) -> dict:
    """Serialize a PostRecord into the full canonical dict shape for scratch storage.

    Every field in SIGNALS.md Tier 1 is captured here.  Nothing is truncated.
    ``raw`` carries the COMPLETE original platform payload (untouched).
    """
    return {
        "id": p.platform_post_id,
        "source": source,
        "handle": p.account_handle,
        "url": p.url,
        "caption": p.caption,
        "posted_at": p.posted_at.isoformat() if p.posted_at else None,
        "fetched_at": p.fetched_at.isoformat(),
        "media_type": p.media_type,
        "hashtags": p.hashtags,
        "like_count": p.like_count,
        "comment_count": p.comment_count,
        "share_count": p.share_count,
        "view_count": p.view_count,
        "save_count": p.save_count,  # always None on X — bookmarks are not public
        "author_follower_count": p.author_follower_count,
        "duration_sec": p.duration_sec,
        "thumbnail_url": p.thumbnail_url,
        "geo_tier": p.geo_tier,
        "raw": p.raw,          # COMPLETE original payload — nothing dropped
    }


# ---------------------------------------------------------------------------
# Main harvest
# ---------------------------------------------------------------------------

def harvest() -> None:
    seen_ids: set[str] = set()
    posts: list[dict] = []
    stats = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        # Phase 1: GraphQL UserTweets cursor pagination
        "gql_accounts_attempted": 0,
        "gql_pages_fetched": 0,
        "gql_posts_collected": 0,
        "gql_cursor_follows": 0,
        "gql_cursor_exhaustions": 0,
        "rate_limit_hits": 0,
        "backoff_seconds_total": 0.0,
        "gql_errors": [],
        # Phase 2: Syndication
        "syndication_accounts_attempted": 0,
        "syndication_posts_collected": 0,
        "syndication_errors": [],
        # Search (documented as blocked)
        "search_timeline_status": "BLOCKED — login-gated (HTTP 404 for guest tokens; see README)",
        # Summary
        "total_unique": 0,
        "reached_target": False,
    }

    gql = XGraphQLClient(timeout=25.0)
    # Shared adapter instance — used by Phase 1 for canonical GQL normalization
    # and by Phase 2 for syndication.  prefer_graphql=False because Phase 1 drives
    # GQL directly via the XGraphQLClient; the adapter only normalizes here.
    adapter = XAdapter(prefer_graphql=False)

    # ------------------------------------------------------------------
    # Phase 1: UserTweets cursor pagination (depth per account)
    # ------------------------------------------------------------------
    print(f"[harvest] Phase 1: UserTweets cursor pagination across {len(GQL_ACCOUNTS)} accounts")
    print(f"[harvest] Target: {TARGET} unique posts\n")

    for acct_idx, account in enumerate(GQL_ACCOUNTS):
        if len(seen_ids) >= TARGET:
            break

        stats["gql_accounts_attempted"] += 1
        handle = account.handle.lstrip("@")
        acct_posts = 0
        fetched_at = datetime.now(timezone.utc)

        print(f"  [{acct_idx+1}/{len(GQL_ACCOUNTS)}] @{handle}  (running total={len(seen_ids)})")

        try:
            rest_id = gql.get_user_rest_id(handle)
        except XGraphQLError as e:
            err = f"UserByScreenName @{handle}: {e}"
            print(f"    [SKIP] {err}")
            stats["gql_errors"].append(err)
            continue

        cursor: str | None = None
        for page in range(MAX_PAGES_PER_ACCOUNT):
            try:
                results, next_cursor = gql.get_user_tweets_page(
                    rest_id, count=20, cursor=cursor
                )
            except XGraphQLError as e:
                err = f"UserTweets @{handle} page {page+1}: {e}"
                print(f"    [BLOCKED] {err}")
                stats["gql_errors"].append(err)
                break

            stats["gql_pages_fetched"] += 1
            new_this_page = 0
            for result in results:
                # Route through the CANONICAL normalizer — produces a full PostRecord
                # with all SIGNALS.md Tier 1 fields populated from the complete GQL node.
                try:
                    p = adapter._graphql_to_post_record(result, account, fetched_at)
                except (KeyError, TypeError, ValueError):
                    continue
                if p.platform_post_id not in seen_ids:
                    seen_ids.add(p.platform_post_id)
                    posts.append(_post_record_to_dict(p, f"gql:@{handle}:p{page+1}"))
                    new_this_page += 1
                    acct_posts += 1

            has_cursor = next_cursor and next_cursor != cursor
            print(f"    page {page+1}: {len(results)} raw → {new_this_page} new  "
                  f"(cursor={'yes' if has_cursor else 'NONE'})  total={len(seen_ids)}")

            if has_cursor:
                cursor = next_cursor
                stats["gql_cursor_follows"] += 1
            else:
                stats["gql_cursor_exhaustions"] += 1
                print(f"    timeline exhausted at page {page+1}")
                break

            if len(seen_ids) >= TARGET:
                break

            time.sleep(random.uniform(PAGE_SLEEP_MIN, PAGE_SLEEP_MAX))

        stats["gql_posts_collected"] += acct_posts
        stats["rate_limit_hits"] = gql.rate_limit_hits
        stats["backoff_seconds_total"] = round(gql.backoff_seconds_total, 1)
        _save_scratch(posts, stats)

        if len(seen_ids) < TARGET:
            time.sleep(random.uniform(ACCOUNT_SLEEP_MIN, ACCOUNT_SLEEP_MAX))

    # ------------------------------------------------------------------
    # Phase 2: Syndication fallback (fills the gap if GraphQL falls short)
    # ------------------------------------------------------------------
    remaining_accounts = [a for a in SEED_ACCOUNTS if a not in GQL_ACCOUNTS]
    # also include GQL accounts not yet tried via syndication for gap-fill

    if len(seen_ids) < TARGET:
        print(f"\n[harvest] Phase 2: Syndication fallback ({len(remaining_accounts)} handles)  "
              f"(running total={len(seen_ids)})")
        # adapter already created above (Phase 1)

        for acct_idx, account in enumerate(remaining_accounts):
            if len(seen_ids) >= TARGET:
                break

            stats["syndication_accounts_attempted"] += 1
            handle = account.handle.lstrip("@")
            try:
                acc_posts = adapter.fetch_account_posts(account, limit=30)
            except Exception as e:
                err_msg = f"@{handle}: {e}"
                print(f"  [{acct_idx+1}] ERROR {err_msg}")
                stats["syndication_errors"].append(err_msg)
                continue

            new_count = 0
            for p in acc_posts:
                tid = p.platform_post_id
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    # Route through canonical serializer — full fields, nothing truncated
                    posts.append(_post_record_to_dict(p, f"syndication:@{handle}"))
                    new_count += 1

            stats["syndication_posts_collected"] += new_count
            print(f"  [{acct_idx+1}] @{handle}: {len(acc_posts)} fetched → "
                  f"{new_count} new  (total={len(seen_ids)})")
            _save_scratch(posts, stats)

            if len(seen_ids) < TARGET:
                time.sleep(random.uniform(0.8, 1.5))

    # ------------------------------------------------------------------
    # Final report
    # ------------------------------------------------------------------
    stats["total_unique"] = len(seen_ids)
    stats["reached_target"] = len(seen_ids) >= TARGET
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    stats["rate_limit_hits"] = gql.rate_limit_hits
    stats["backoff_seconds_total"] = round(gql.backoff_seconds_total, 1)
    _save_scratch(posts, stats)

    print("\n" + "=" * 60)
    print("HARVEST SUMMARY")
    print("=" * 60)
    print(f"  Total unique posts accumulated : {stats['total_unique']}")
    print(f"  Target ({TARGET}) reached       : {stats['reached_target']}")
    print()
    print("  Phase 1 — GraphQL UserTweets cursor pagination:")
    print(f"    Accounts attempted            : {stats['gql_accounts_attempted']}")
    print(f"    Pages fetched                 : {stats['gql_pages_fetched']}")
    print(f"    Posts from GraphQL            : {stats['gql_posts_collected']}")
    print(f"    Cursor follows (successful)   : {stats['gql_cursor_follows']}")
    print(f"    Cursor exhaustions            : {stats['gql_cursor_exhaustions']}")
    print(f"    Rate-limit hits (429)         : {stats['rate_limit_hits']}")
    print(f"    Total backoff seconds         : {stats['backoff_seconds_total']}")
    if stats["gql_errors"]:
        print(f"    GQL errors                    : {len(stats['gql_errors'])}")
        for e in stats["gql_errors"][:3]:
            print(f"      {e[:100]}")
    print()
    print("  Phase 2 — Syndication fallback:")
    print(f"    Accounts attempted            : {stats['syndication_accounts_attempted']}")
    print(f"    Posts from syndication        : {stats['syndication_posts_collected']}")
    if stats["syndication_errors"]:
        print(f"    Errors                        : {len(stats['syndication_errors'])}")
    print()
    print(f"  SearchTimeline: {stats['search_timeline_status']}")
    print()
    print(f"  Scratch output: {_SCRATCH_PATH}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # Coverage tally — print populated/total/% for every SIGNALS.md field
    # ------------------------------------------------------------------
    _print_coverage_tally(posts)

    if not stats["reached_target"]:
        print(f"\n[WARNING] Only {stats['total_unique']} / {TARGET} reached.")
        print("  See adapters/x/README.md section 'Unattended 500 — blockers and paths'.")
        sys.exit(1)
    else:
        print(f"\n[OK] {stats['total_unique']} unique posts accumulated.")


def _print_coverage_tally(posts: list[dict]) -> None:
    """Print a field-coverage tally over all persisted records."""
    total = len(posts)
    if not total:
        print("\n[coverage] No posts to tally.")
        return

    # Canonical Tier-1 fields per SIGNALS.md (save_count intentionally None on X)
    FIELDS = [
        "id",
        "url",
        "handle",
        "caption",
        "posted_at",
        "fetched_at",
        "media_type",
        "hashtags",
        "like_count",
        "comment_count",
        "share_count",
        "view_count",
        "save_count",
        "author_follower_count",
        "duration_sec",
        "thumbnail_url",
        "geo_tier",
        "raw",
    ]

    def _is_populated(v: object) -> bool:
        if v is None:
            return False
        if isinstance(v, (list, dict)):
            return len(v) > 0
        if isinstance(v, str):
            return len(v) > 0
        return True  # int, float, bool — presence is enough (0 is a valid count)

    print("\n" + "=" * 60)
    print("FIELD COVERAGE TALLY")
    print("=" * 60)
    print(f"  {'Field':<28} {'Populated':>9}  {'Total':>5}  {'%':>5}")
    print(f"  {'-'*28}  {'-'*9}  {'-'*5}  {'-'*5}")
    for f in FIELDS:
        populated = sum(1 for p in posts if _is_populated(p.get(f)))
        pct = 100 * populated / total
        note = ""
        if f == "save_count":
            note = "  ← always None on X (bookmarks private)"
        elif f == "view_count":
            note = "  ← None on syndication path; populated via GraphQL"
        elif f == "hashtags":
            note = "  ← [] when tweet has no hashtags"
        elif f == "duration_sec":
            note = "  ← None for non-video posts"
        print(f"  {f:<28} {populated:>9}  {total:>5}  {pct:>5.1f}%{note}")
    print("=" * 60)


if __name__ == "__main__":
    harvest()
