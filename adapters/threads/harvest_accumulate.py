"""Unattended Threads accumulation harvester — breadth-first across seed accounts.

Goal: accumulate >= 500 posts unattended, persisted to data/threads_harvest_scratch.json.
      Idempotent: interrupt and re-run safely; already-done accounts are skipped.

Strategy (Threads is HTTP/per-request, not a scroll-feed):
  - Fetch up to LIMIT_PER_ACCOUNT posts from each seed account (the adapter
    internally scrolls the browser to request paginated Relay batches).
  - Dedup by platform_post_id across all accounts.
  - ~20–50 posts per active public account → need ~15–25 accounts for 500.
  - This script seeds 50 handles; ~30–40 are typically Threads-active.

Blockers handled:
  - ThreadsLoginWall  → STOP all fetching (Threads gated reads); set blocked flag.
  - ThreadsRateLimited → BACK OFF (90 s sleep); count 3× consecutive → stop.
  - Navigation timeout / other errors → log, skip account, continue.

Run from repo root:
  .venv/bin/python -m adapters.threads.harvest_accumulate

Also performs a one-shot probe of logged-out explore/search surfaces and reports findings.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from adapters.threads.adapter import (
    ThreadsLoginWall,
    ThreadsRateLimited,
    _absorb,
    _looks_like_login_wall,
)
from adapters.threads import ThreadsAdapter
from core.schema import WatchedAccount

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRATCH_PATH = Path("data/threads_harvest_scratch.json")
TARGET = 500
LIMIT_PER_ACCOUNT = 50       # try up to 50 posts per account (scroll pagination)
SLEEP_MIN_S = 3.5            # min jittered sleep between accounts
SLEEP_MAX_S = 7.0            # max
RATE_LIMIT_SLEEP_S = 90.0   # back-off on 429
MAX_CONSECUTIVE_RATE_LIMITS = 3
# Login walls: some accounts are private / not on Threads (account-level wall).
# Only treat as an IP/session-level block if N consecutive accounts all wall — that
# means Threads has started gating reads entirely.
MAX_CONSECUTIVE_LOGIN_WALLS = 3

# ---------------------------------------------------------------------------
# Seed handles — ordered roughly by expected Threads activity.
# Includes 50 candidates; ~30–40 are typically active on Threads.
# ---------------------------------------------------------------------------

_SEED_RAW = [
    # Meta / platform-native (guaranteed active, high volume)
    "zuck", "mosseri",
    # Big consumer brands
    "netflix", "spotify", "duolingo", "nasa", "google", "openai",
    # News / media
    "nytimes", "bbcnews", "guardian", "washingtonpost",
    "wired", "techcrunch", "theverge", "time",
    # Science / education
    "natgeo", "harvard", "mit", "khanacademy",
    # Entertainment / lifestyle / fashion
    "vogue", "cosmopolitan", "glamour", "esquire",
    # Sports
    "nfl", "nba", "espn",
    # More tech
    "microsoft", "adobe", "canva", "figma", "vercel",
    # Creator economy / entrepreneurs
    "garyvee",
    # More brands
    "starbucks", "nike", "patagonia",
    # Digital media
    "buzzfeed", "vice", "newyorker", "theatlantic", "economist",
    # Startup / b2b
    "ycombinator", "hubspot",
    # More AI / tech
    "anthropic",
    # More news
    "cnn", "nbcnews", "bloomberg", "fortune", "fastcompany",
    # Extra to pad past 500 if earlier accounts return few posts
    "mashable", "engadget", "techradar", "pcmag",
    "nationalgeo", "nationalgeographic",
    "bbc",
]

# Deduplicate preserving order
_seen_h: set[str] = set()
SEED_HANDLES: list[str] = []
for _h in _SEED_RAW:
    if _h not in _seen_h:
        _seen_h.add(_h)
        SEED_HANDLES.append(_h)
del _SEED_RAW, _seen_h, _h


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {
        "posts": {},
        "accounts_done": [],
        "accounts_failed": [],
        "blocked_at": None,
        "blocked_reason": None,
        "probe_results": {},
        "summary": {},
    }


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
    tmp.replace(path)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def _guard_nonempty_raw(post: Any) -> bool:
    """INGESTION-CONTRACT guard: return True only when ``raw`` is populated.

    An empty ``raw`` means the adapter failed to preserve the original platform
    payload. Persisting such a row silently would produce a data-quality hole that
    is impossible to fix without re-fetching. Rows that fail this guard are
    logged and skipped; they will be re-fetched on the next run (idempotent).
    """
    if not post.raw:
        import logging as _logging
        _logging.warning(
            "Threads post %s (%s) has empty raw — INGESTION-CONTRACT violation; "
            "skipping so the hole is never persisted. Will re-fetch next run.",
            post.platform_post_id,
            post.account_handle,
        )
        return False
    return True


def _post_to_row(post: Any) -> dict[str, Any]:
    """Serialize a PostRecord to a canonical JSON row.

    Field names match PostRecord EXACTLY — no invented aliases.
    ``raw`` is included in full so downstream code can re-normalize without
    re-fetching (INGESTION-CONTRACT: capture everything, decide viral later).
    """
    return {
        # --- identity ---
        "platform": post.platform,
        "platform_post_id": post.platform_post_id,
        "account_handle": post.account_handle,
        "url": post.url,
        "fetched_at": str(post.fetched_at),
        "media_type": post.media_type,
        # --- content ---
        "posted_at": str(post.posted_at) if post.posted_at is not None else None,
        "caption": post.caption,
        "hashtags": post.hashtags,
        "sound_id": post.sound_id,
        "sound_name": post.sound_name,
        "duration_sec": post.duration_sec,
        # --- engagement counts (canonical SIGNALS.md names) ---
        "view_count": post.view_count,       # None for most Threads posts (no view surface)
        "like_count": post.like_count,
        "comment_count": post.comment_count,
        "share_count": post.share_count,
        "save_count": post.save_count,       # always None — Threads exposes no save count
        # --- author / geo ---
        "author_follower_count": post.author_follower_count,
        "geo_tier": post.geo_tier,
        # --- media ---
        "thumbnail_url": post.thumbnail_url,
        # --- complete original payload (required for re-normalization) ---
        "raw": post.raw,
    }


# ---------------------------------------------------------------------------
# Explore / search surface probe
# ---------------------------------------------------------------------------

def probe_logged_out_surfaces(headless: bool = True) -> dict[str, Any]:
    """One-shot probe: does Threads serve a logged-out explore/search feed?

    Navigates each surface unauthenticated, intercepts GraphQL responses, and
    checks whether post nodes arrive before any login redirect. Returns findings
    keyed by surface name.
    """
    from playwright.sync_api import sync_playwright

    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    surfaces = [
        ("explore", "https://www.threads.com/explore"),
        ("search_marketing", "https://www.threads.com/search?q=marketing"),
        ("search_education", "https://www.threads.com/search?q=education"),
        ("search_trending", "https://www.threads.com/search?q=trending"),
    ]

    results: dict[str, Any] = {}

    for name, url in surfaces:
        collected: list[dict] = []
        seen_ids: set[str] = set()
        login_wall = False
        final_url = url
        error: Optional[str] = None

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent=UA,
                locale="en-US",
                viewport={"width": 1280, "height": 2400},
            )
            page = context.new_page()

            def on_resp(response: Any) -> None:
                if "/graphql" not in response.url:
                    return
                try:
                    ct = response.headers.get("content-type", "")
                    if "application/json" in ct:
                        _absorb(response.json(), collected, seen_ids)
                except Exception:
                    pass

            page.on("response", on_resp)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(3000)
                final_url = page.url
                login_wall = _looks_like_login_wall(page)

                if not login_wall and len(collected) == 0:
                    # Light scroll to trigger lazy-load
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(2000)

                final_posts = len(collected)
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:200]}"
                final_posts = len(collected)
            finally:
                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass

        if error:
            verdict = "ERROR"
        elif login_wall:
            verdict = "LOGIN_WALL"
        elif final_posts > 0:
            verdict = "FEED_FOUND"
        else:
            verdict = "EMPTY_NO_WALL"

        results[name] = {
            "url": url,
            "final_url": final_url,
            "login_wall": login_wall,
            "posts_found": final_posts,
            "verdict": verdict,
            "error": error,
        }
        print(
            f"  [{_ts()}] probe {name:25}: verdict={verdict:12} "
            f"posts={final_posts} login_wall={login_wall}"
        )
        time.sleep(3)

    return results


# ---------------------------------------------------------------------------
# Main harvest loop
# ---------------------------------------------------------------------------

def main() -> None:
    state = load_state(SCRATCH_PATH)
    posts_by_id: dict[str, Any] = state.get("posts", {})
    accounts_done: set[str] = set(state.get("accounts_done", []))
    accounts_failed: list[dict] = state.get("accounts_failed", [])

    total_start = len(posts_by_id)
    pending = [h for h in SEED_HANDLES if h not in accounts_done]

    print(
        f"[{_ts()}] Threads unattended harvester\n"
        f"  target={TARGET}  already_have={total_start}  "
        f"accounts_done={len(accounts_done)}  pending={len(pending)}"
    )

    adapter = ThreadsAdapter(headless=True, scroll_stall_limit=6)
    consecutive_rate_limits = 0
    consecutive_login_walls = 0
    ip_level_wall = False    # True only if N consecutive accounts all wall (IP-level)
    per_account_log: list[dict] = []

    for handle in pending:
        if len(posts_by_id) >= TARGET:
            print(f"[{_ts()}] TARGET {TARGET} reached — stopping.")
            break

        print(f"[{_ts()}] @{handle} ({len(posts_by_id)}/{TARGET}) ...")

        try:
            acct = WatchedAccount(
                handle=handle,
                platform="threads",
                segment="adjacent",
                geo_tier="World",
            )
            posts = adapter.fetch_account_posts(acct, limit=LIMIT_PER_ACCOUNT)

            new_count = 0
            for p in posts:
                if p.platform_post_id not in posts_by_id:
                    if not _guard_nonempty_raw(p):
                        print(
                            f"  WARNING: post {p.platform_post_id} empty raw "
                            "— skipping (INGESTION-CONTRACT violation)"
                        )
                        continue
                    posts_by_id[p.platform_post_id] = _post_to_row(p)
                    new_count += 1

            accounts_done.add(handle)
            consecutive_rate_limits = 0
            consecutive_login_walls = 0

            row = {
                "handle": handle,
                "fetched": len(posts),
                "new": new_count,
                "has_next_page": adapter.last_has_next_page,
                "cursor_present": bool(adapter.last_end_cursor),
                "doc_id": adapter.last_doc_id,
                "total_after": len(posts_by_id),
            }
            per_account_log.append(row)

            print(
                f"  fetched={len(posts):3}  new={new_count:3}  "
                f"has_next={str(adapter.last_has_next_page):5}  "
                f"cursor={'yes' if adapter.last_end_cursor else 'no '}  "
                f"total={len(posts_by_id)}"
            )

        except ThreadsLoginWall as exc:
            consecutive_login_walls += 1
            print(
                f"  LOGIN WALL (consecutive={consecutive_login_walls}): "
                f"@{handle} appears private/not-on-Threads. Skipping."
            )
            accounts_done.add(handle)  # skip this account permanently
            accounts_failed.append({"handle": handle, "error": "login_wall"})
            if consecutive_login_walls >= MAX_CONSECUTIVE_LOGIN_WALLS:
                ip_level_wall = True
                state["blocked_at"] = _ts()
                state["blocked_reason"] = f"ip_level_login_wall_{MAX_CONSECUTIVE_LOGIN_WALLS}x"
                print(
                    f"  {MAX_CONSECUTIVE_LOGIN_WALLS} consecutive login walls — "
                    "likely IP-level gate. Stopping to back off."
                )
                save_state(SCRATCH_PATH, state)
                break
            # account-level wall: persist and continue to next account
            state["posts"] = posts_by_id
            state["accounts_done"] = sorted(accounts_done)
            state["accounts_failed"] = accounts_failed
            save_state(SCRATCH_PATH, state)
            sleep_s = random.uniform(SLEEP_MIN_S, SLEEP_MAX_S)
            print(f"  sleeping {sleep_s:.1f}s ...")
            time.sleep(sleep_s)
            continue

        except ThreadsRateLimited as exc:
            consecutive_rate_limits += 1
            print(
                f"  RATE LIMITED (consecutive={consecutive_rate_limits}): "
                f"{str(exc)[:150]}"
            )
            if consecutive_rate_limits >= MAX_CONSECUTIVE_RATE_LIMITS:
                state["blocked_at"] = _ts()
                state["blocked_reason"] = f"rate_limit_{MAX_CONSECUTIVE_RATE_LIMITS}x"
                print(
                    f"  {MAX_CONSECUTIVE_RATE_LIMITS} consecutive rate limits — "
                    "stopping to protect IP."
                )
                save_state(SCRATCH_PATH, state)
                break
            print(f"  backing off {RATE_LIMIT_SLEEP_S:.0f}s...")
            time.sleep(RATE_LIMIT_SLEEP_S)
            # Skip this account and continue with the next
            accounts_done.add(handle)
            continue

        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:300]}"
            print(f"  ERROR: {err}")
            accounts_failed.append({"handle": handle, "error": err})
            accounts_done.add(handle)

        # Persist after every account (crash-safe)
        state["posts"] = posts_by_id
        state["accounts_done"] = sorted(accounts_done)
        state["accounts_failed"] = accounts_failed
        save_state(SCRATCH_PATH, state)

        sleep_s = random.uniform(SLEEP_MIN_S, SLEEP_MAX_S)
        print(f"  sleeping {sleep_s:.1f}s ...")
        time.sleep(sleep_s)

    # ---- one-shot explore/search probe (skip if already done or IP-blocked) ----
    if not ip_level_wall and not state.get("probe_results"):
        print(f"\n[{_ts()}] Probing logged-out explore/search surfaces ...")
        try:
            probe_results = probe_logged_out_surfaces(headless=True)
            state["probe_results"] = probe_results
        except Exception as exc:
            state["probe_results"] = {"error": str(exc)[:300]}
            print(f"  probe error: {exc}")
    elif state.get("probe_results"):
        print(f"[{_ts()}] Explore/search probe already done — skipping.")

    # ---- final persist ----
    state["posts"] = posts_by_id
    state["accounts_done"] = sorted(accounts_done)
    state["accounts_failed"] = accounts_failed
    state["summary"] = {
        "total_posts": len(posts_by_id),
        "new_this_run": len(posts_by_id) - total_start,
        "accounts_processed_this_run": len(per_account_log),
        "accounts_done_total": len(accounts_done),
        "target": TARGET,
        "target_reached": len(posts_by_id) >= TARGET,
        "ip_level_wall_hit": ip_level_wall,
        "consecutive_login_walls_at_stop": consecutive_login_walls,
        "consecutive_rate_limits_at_stop": consecutive_rate_limits,
        "per_account": per_account_log,
    }
    save_state(SCRATCH_PATH, state)

    # ---- final report ----
    print(f"\n{'=' * 62}")
    print(f"THREADS HARVEST REPORT  {_ts()}")
    print(f"{'=' * 62}")
    print(f"Total posts accumulated : {len(posts_by_id)}")
    print(f"New posts this run      : {len(posts_by_id) - total_start}")
    print(f"Accounts done (total)   : {len(accounts_done)}")
    print(f"Accounts tried this run : {len(per_account_log)}")
    print(f"Target ({TARGET}) reached   : {len(posts_by_id) >= TARGET}")
    print(f"IP-level wall hit       : {ip_level_wall}")
    print(f"Login walls (skipped)   : {consecutive_login_walls}")
    print(f"Rate limits (count)     : {consecutive_rate_limits}")
    print()

    if per_account_log:
        print("Per-account breakdown:")
        for row in per_account_log:
            cursor_tag = "cursor:yes" if row["cursor_present"] else "cursor:no "
            print(
                f"  @{row['handle']:28} fetched={row['fetched']:3}  "
                f"new={row['new']:3}  "
                f"has_next={str(row['has_next_page']):5}  {cursor_tag}"
            )
        print()

    probe = state.get("probe_results") or {}
    if probe and not probe.get("error"):
        print("Logged-out explore/search probe:")
        for k, v in probe.items():
            if isinstance(v, dict):
                print(
                    f"  {k:30}: verdict={v.get('verdict'):15} "
                    f"posts={v.get('posts_found'):3}  "
                    f"login_wall={v.get('login_wall')}"
                )
        print()

    if accounts_failed:
        print(f"Failed accounts ({len(accounts_failed)}):")
        for row in accounts_failed[:10]:
            print(f"  @{row['handle']}: {row['error'][:100]}")
        print()

    print(f"State saved to: {SCRATCH_PATH}")

    if len(posts_by_id) >= TARGET:
        print(f"\nSUCCESS: {len(posts_by_id)} posts >= {TARGET} target.")
        _print_how()
    elif ip_level_wall:
        print(
            "\nBLOCKED: IP-level login wall — Threads is gating all reads.\n"
            "Wait 30+ min then re-run (state is persisted), or supply a\n"
            "burner ThreadsSession. See README §Login-wall fallback."
        )
    elif consecutive_rate_limits >= MAX_CONSECUTIVE_RATE_LIMITS:
        print(
            f"\nBACKED OFF: {MAX_CONSECUTIVE_RATE_LIMITS}x rate limits. "
            "Re-run after 10+ minutes to resume (state is persisted)."
        )
    else:
        remaining = TARGET - len(posts_by_id)
        print(
            f"\nINCOMPLETE: {len(posts_by_id)}/{TARGET} posts. "
            f"Need ~{max(1, remaining // 20)} more active accounts. "
            "Add handles to SEED_HANDLES and re-run."
        )


def _print_how() -> None:
    """Print the 'how did we reach 500' summary."""
    state = load_state(SCRATCH_PATH)
    per_acct = (state.get("summary") or {}).get("per_account") or []
    if not per_acct:
        return
    total = sum(r["fetched"] for r in per_acct)
    n = len(per_acct)
    avg = total / n if n else 0
    max_depth = max((r["fetched"] for r in per_acct), default=0)
    has_cursor = sum(1 for r in per_acct if r.get("cursor_present"))
    print(
        f"\nHow we reached {state['summary']['total_posts']} posts:\n"
        f"  Accounts fetched: {n}\n"
        f"  Avg posts/account: {avg:.1f}\n"
        f"  Max posts/account: {max_depth}\n"
        f"  Accounts with pagination cursor: {has_cursor}/{n}\n"
        f"  Strategy: breadth across {n} public profiles; "
        f"scroll-pagination within each."
    )


if __name__ == "__main__":
    main()
