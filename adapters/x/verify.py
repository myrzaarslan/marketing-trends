"""End-to-end check: run XAdapter against real public accounts and assert the
normalized fields + full raw payload come back sanely. Run from the repo root:

    .venv/bin/python -m adapters.x.verify
"""

from __future__ import annotations

from core.schema import WatchedAccount
from adapters.x.adapter import XAdapter

TEST_ACCOUNTS = [
    WatchedAccount(handle="Duolingo", platform="x", segment="adjacent", geo_tier="World"),
    WatchedAccount(handle="khanacademy", platform="x", segment="global_edtech", geo_tier="World"),
    WatchedAccount(handle="NASA", platform="x", segment="adjacent", geo_tier="World"),
]


def main() -> None:
    adapter = XAdapter()
    assert adapter.fetch_trends("World") == [], "fetch_trends should be [] for X"
    print("fetch_trends('World') -> []  (no free trend source — OK)\n")

    for account in TEST_ACCOUNTS:
        posts = adapter.fetch_account_posts(account, limit=5)
        assert posts, f"no posts for @{account.handle}"
        print(f"@{account.handle}: {len(posts)} posts")
        for p in posts[:3]:
            # Contract sanity: required fields present, raw is the complete payload.
            assert p.platform == "x"
            assert p.platform_post_id and p.url.startswith("https://x.com/")
            assert p.media_type in ("video", "image", "text", "carousel")
            assert isinstance(p.raw, dict) and "id_str" in p.raw
            assert p.geo_tier == account.geo_tier
            assert p.author_follower_count is not None and p.author_follower_count > 0, (
                f"author_follower_count missing for @{account.handle}"
            )
            assert p.save_count is None, "save_count must be None (X bookmarks are private)"
            assert p.sound_id is None and p.sound_name is None, "X has no sound concept"
            dur = f" dur={p.duration_sec:.0f}s" if p.duration_sec else ""
            print(
                f"  [{p.media_type:8}] like={p.like_count} reply={p.comment_count} "
                f"rt={p.share_count} view={p.view_count} followers={p.author_follower_count} "
                f"tags={p.hashtags}{dur}"
            )
            print(f"             posted={p.posted_at}  {p.url}")
            print(f"             caption={ (p.caption or '')[:70]!r}")
            print(f"             raw keys={len(p.raw)}")
        print()

    # -- optional GraphQL deepening: view_count recovery --------------------
    print("=" * 50)
    print("GraphQL path (prefer_graphql=True) — view_count recovery")
    gql = XAdapter(prefer_graphql=True)
    nasa = TEST_ACCOUNTS[-1]
    posts = gql.fetch_account_posts(nasa, limit=5)
    assert posts, "GraphQL path returned no posts"
    with_views = [p for p in posts if p.view_count is not None]
    print(f"@{nasa.handle}: {len(posts)} posts, {len(with_views)} carry view_count")
    for p in posts[:5]:
        # raw is the full GraphQL result node; id_str lives under raw["legacy"].
        assert p.platform == "x" and p.platform_post_id
        assert "id_str" in p.raw or "id_str" in p.raw.get("legacy", {})
        assert p.author_follower_count is not None and p.author_follower_count > 0, (
            "author_follower_count missing on GraphQL path"
        )
        print(f"  [{p.media_type:8}] views={p.view_count} like={p.like_count} "
              f"reply={p.comment_count} rt={p.share_count} followers={p.author_follower_count}")
    assert with_views, "expected GraphQL to populate view_count on at least some posts"
    print("\nOK — all assertions passed (syndication + GraphQL).")


if __name__ == "__main__":
    main()
