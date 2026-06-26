"""Manual end-to-end smoke test for the Threads adapter (round two).

Run from the repo root:

    .venv/bin/python -m adapters.threads.smoke_test

Loads each profile in a headless browser and intercepts the profile-feed GraphQL.
Reports the live (rotating) `doc_id` and observed Threads `userID` per fetch — both
acquired dynamically, never hardcoded. Hits the live site, so it is inherently
flaky — that fragility is the point of the spike (see README.md).

Not a unit test. `fetch_trends` returns [] by design.
"""

from __future__ import annotations

import json

from adapters.threads import ThreadsAdapter
from core.schema import WatchedAccount

# A few real, public, text-active Threads accounts across segments.
ACCOUNTS = [
    WatchedAccount(handle="zuck", platform="threads",
                   segment="adjacent", geo_tier="World"),
    WatchedAccount(handle="mosseri", platform="threads",
                   segment="adjacent", geo_tier="World"),
    WatchedAccount(handle="duolingo", platform="threads",
                   segment="global_edtech", geo_tier="World"),
]


def main() -> None:
    adapter = ThreadsAdapter(headless=True)

    # Cheap pre-check: is the account on Threads at all? (IG backend signal.)
    print(f"account_is_on_threads(zuck) = {adapter.account_is_on_threads('zuck')}")

    for acct in ACCOUNTS:
        print(f"\n===== @{acct.handle} =====")
        try:
            posts = adapter.fetch_account_posts(acct, limit=5)
        except Exception as e:  # noqa: BLE001 - smoke test surfaces anything
            print(f"  ERROR: {type(e).__name__}: {str(e)[:300]}")
            continue
        print(f"  got {len(posts)} PostRecord(s)  "
              f"doc_id={adapter.last_doc_id} threads_user_id={adapter.last_user_id}")
        for r in posts[:5]:
            cap = (r.caption or "").replace("\n", " ")
            print(
                f"  - [{r.media_type:8}] {r.platform_post_id} "
                f"likes={r.like_count} replies={r.comment_count} "
                f"reposts={r.share_count} views={r.view_count} tags={r.hashtags}"
            )
            print(f"      {r.url}")
            print(
                f"      posted_at={r.posted_at}  "
                f"followers={r.author_follower_count}  raw_keys={len(r.raw)}"
            )
            print(f"      caption: {cap[:90]}")
        if posts:
            print(f"  raw sample bytes: {len(json.dumps(posts[0].raw, default=str))}")

    print("\n===== fetch_trends (expected []) =====")
    print(" ", adapter.fetch_trends("World"))


if __name__ == "__main__":
    main()
