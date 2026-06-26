# Handoff — Robust unattended harvest (≥500 posts/platform, no agent in the loop)

GOAL: make each platform's harvester self-sufficient enough to accumulate **≥500 posts**
**unattended** — robust to UI blockers. Encode the operator's instinct: when the feed stops
yielding NEW items, figure out WHY in code and handle it. No human/agent babysitting.

## Obstacle taxonomy — handle each DIFFERENTLY (this is the core of the task)

1. **Dismissible UI modal** (interest picker, cookie/consent, login/signup nudge, app-install
   banner, "see more" wall): locate in the DOM by a **STABLE selector** (class fragment / role /
   visible text — **NEVER pixel coordinates**), close it, continue. Bake it into the harvester so
   every future run auto-handles it. **This is the main thing to build.**
2. **Captcha / bot-check** (slider puzzle, "verify you're human", "Передвиньте ползунок…"): **DO
   NOT solve, DO NOT grind.** Detect via text/DOM markers, **back off**, return what's collected
   with `blocked=True`. This means the session/IP is HOT — it's the honest ceiling, not a modal to
   dismiss. Document it; never try to beat it.
   > **Exception (ADR-0004, 2026-06-26):** back-off-only remains the DEFAULT here. A *separate,
   > opt-in, default-off* captcha-solving experiment exists, but it runs **only on a disposable egress
   > (VPN/mobile), never the home IP**, and on the home IP this back-off rule is unchanged. See
   > [docs/adr/0004-captcha-solving-experiment.md](../adr/0004-captcha-solving-experiment.md).
3. **Feed exhaustion / pagination end**: implement real pagination — scroll triggers, cursor token,
   reload+dedup, or session-based deeper paging — to keep going until genuinely dry.

## The harvest loop to build
```
advance/scroll → collect new items (dedup by id) → if NO new items for N tries → inspect:
    benign modal     → dismiss → continue
    captcha/bot-check → STOP, back off, blocked=True
    exhaustion        → paginate (next cursor / reload+dedup) or stop if truly dry
accumulate toward 500 across whatever the platform allows (one session, deeper pagination,
or N WELL-SPACED runs with persisted dedup).
```

## Reaching 500 differs by platform MECHANISM — be honest about which you are
- **TikTok / Instagram = SCROLL-FEED** → the modal/scroll instinct applies directly. Reach 500 via
  deeper scroll + pagination + spaced accumulation; auto-dismiss benign modals; back off on captcha.
- **X / Threads = HTTP/PER-REQUEST (no scroll, no modals)** → "500 posts" = **paginate/accumulate
  across many seed accounts/queries** (SearchTimeline cursors / many handles) with **rate-limit
  (429) backoff**. Do NOT hunt for DOM modals that don't exist here.

## Anti-ban (mandatory, unchanged)
Gentle JITTERED pacing, low speed, no grinding. The KZ home IP is **SHARED across all 4 platforms
running at once** → cross-platform heat; pace conservatively and assume the IP may already be warm
(TikTok served a captcha on 2026-06-26). A captcha/429 = IP hot → **stop that platform**, don't
push. Scale-out via rotating residential IPs is OPEN-QUESTIONS Q-3, out of scope.

## Deliverable per platform
- A harvester that, run **unattended**, accumulates toward 500 and **auto-handles benign modals +
  backs off on captcha/429** — no agent required.
- A report: max posts in one run; **every blocker hit, with its DOM/text signature + how handled**;
  whether 500 is reachable unattended and HOW (1 run / N spaced runs / N accounts/queries); any
  residual manual step that still needs a human.
- Update the adapter README + signal docs with the obstacle handling and the realistic yield.

## Escalation
If you hit a NON-trivial design fork, a new blocker you can't classify, or a case where reaching
500 would require grinding through a captcha (forbidden), STOP and return the question — the Opus
main thread will advise. Do NOT solve captchas to hit the number.
