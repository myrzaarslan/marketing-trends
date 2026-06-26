# Captcha-Solving Experiment Playbook (ADR-0004)

> **Status:** Phase 1 — scaffold + tools built; live measurement pending operator run.
> **ADR:** [docs/adr/0004-captcha-solving-experiment.md](../adr/0004-captcha-solving-experiment.md)

---

## 1. Kill switch and defaults

**The solver is DEFAULT OFF.** The module flag is:

```python
# core/harness/captcha_solver.py
CAPTCHA_SOLVING_ENABLED: bool = False
```

When `False` — the ONLY default — existing harvesters back off exactly as
before ADR-0004.  Nothing changes.  Back-off is the honest ceiling.

**To enable for one operator session:**

```python
import core.harness.captcha_solver as cs
cs.CAPTCHA_SOLVING_ENABLED = True
```

**To flip it back off (immediately in-process):**

```python
cs.CAPTCHA_SOLVING_ENABLED = False
```

Alternatively, set environment variable `CAPTCHA_SOLVING_ENABLED=1` before
importing the module.

---

## 2. Disposable-egress requirement — NEVER the home IP

The egress isolation is the **core safety mechanism** (ADR-0004
§Guardrails).  The home broadband is shared across all four platform adapters
running the daily spine ingestion.  A hard ban there would poison the entire
data pipeline.

**Rule:** solving is attempted ONLY when the caller explicitly passes
`egress_is_disposable=True`.  Passing `False` (or omitting it) forces the
back-off path regardless of the kill switch.

```python
result = await cs.attempt_solve(page, egress_is_disposable=True)   # OK on VPN/tether
result = await cs.attempt_solve(page, egress_is_disposable=False)  # back-off (home IP)
```

**Two-phase egress (ADR-0004 §Guardrails):**

| Phase | Egress | Purpose |
|-------|--------|---------|
| Phase 1 | VPN (datacenter) | Puzzle collection — likely pre-flagged, captchas appear often |
| Phase 2 | Phone 4G/5G tether (`airplane-mode` toggle to rotate IP) | Solve-rate measurement that generalises to real users |

A paid residential/mobile proxy (OPEN-QUESTIONS Q-3) is the clean-but-paid
version of the phone tether.

---

## 3. Puzzle types and handling

| Type | Detection | Action |
|------|-----------|--------|
| **Slider** (`"slider"`) | Text markers: "Передвиньте ползунок", "puzzle", "drag the slider", etc. | Attempt to solve (≤10 tries, disposable egress only) |
| **Unknown** (`"unknown"`) | Any page with captcha markers that do NOT match the slider set | Record in registry → back off → flag for human/Opus review |

Only the slider puzzle type observed in production (2026-06-26) is solved.
Every other type is captured in the registry and escalated.

---

## 4. Circuit breaker

```
≤ 10 attempts per obstacle
  └── attempt fails ("fail") → try again with a fresh trajectory (jittered)
  └── re-challenge after solve ("re_challenge") → TERMINAL immediately
  └── 429 after solve → TERMINAL immediately
  └── 10th attempt without pass → TERMINAL
```

**TERMINAL means:**
1. Stop immediately — do not attempt further.
2. Set `blocked=True` on the result.
3. **Rotate the disposable IP** before resuming (VPN server hop or
   airplane-mode cycle on the phone tether).
4. The accumulator checkpoint is already saved — no data is lost.
5. Resume on the new IP with `cooldown=True` for the first session.

Never grind on a hot IP.  A re-challenge is a clear escalation signal.

---

## 5. How to run a Phase-1 puzzle-collection session

```python
from adapters.tiktok.discovery import harvest
import core.harness.captcha_solver as cs

# 1. Enable solver for this session
cs.CAPTCHA_SOLVING_ENABLED = True

# 2. Run harvest — the harvester will call attempt_solve when it detects a captcha.
#    Pass egress_is_disposable=True only when on VPN / phone tether.
result = harvest("KZ", surface="explore", accumulator_path="data/tiktok_accumulator.json")

# 3. Check the registry for what was seen.
import json
with open("data/captcha_registry.json") as f:
    registry = json.load(f)
print(registry["type_counts"])
print(registry["total_encounters"])
```

**Before running:** confirm VPN / phone tether is active, NOT the home broadband.

---

## 6. Slider puzzle — bypass notes (Phase 1 findings)

> **This section is filled in by the operator after live Phase-1 runs.**
> The information below is the pre-run scaffold; update it with real data.

### Observed selector patterns (2026-06-26, not yet validated live on disposable egress)

TikTok's slider captcha has been observed to contain:
- Background image: `[class*="captcha"] img` or `[class*="slider"] img`
- Drag thumb: `[class*="slider-btn"]`, `[class*="drag-btn"]`, `[class*="captcha-btn"]`
- Slider track: `[class*="slider-track"]`, `[class*="captcha-track"]`, `[class*="sliderbg"]`

> **Note:** TikTok's class names are obfuscated and can change.  The solver uses
> fragment-match (`[class*="..."]`) rather than exact class names.  Update these
> selectors if they stop matching.

### Gap-detection algorithm

The module uses a column-brightness approach (PIL):
1. Convert the background image to grayscale.
2. Compute per-column mean pixel brightness.
3. The column with the minimum mean is the gap (the gap shadow is consistently
   the darkest column).

Known limitation: if the background image has decorative dark elements that are
darker than the gap shadow, this will misidentify the gap.  If solve success
rate is low, check the gap detection first with a saved fixture.

### Trajectory shape

Move generation uses a cubic ease-in-out curve (slow start → fast middle →
slow end) with ±2 px Y jitter peaking at the midpoint.  Total drag duration:
650–1050 ms (random, per attempt).

TikTok's behavioral acceptance threshold is **not knowable offline** — only
live runs with outcome measurement can determine whether this trajectory shape
is accepted.

---

## 7. Registry (`data/captcha_registry.json`)

The registry is **gitignored** (`data/` is excluded).  It is the artifact of
Phase-1 operator sessions.  Schema:

```json
{
  "version": 1,
  "total_encounters": 0,
  "type_counts": {"slider": 0, "unknown": 0},
  "last_updated": "<ISO timestamp>",
  "puzzles": [
    {
      "puzzle_id": "<uuid4>",
      "puzzle_type": "slider",
      "detected_at": "<ISO>",
      "dom_signature": "<first 120 chars of page body, normalised>",
      "screenshot_path": null,
      "attempts": [
        {
          "moves": [...],
          "outcome": "pass | re_challenge | fail | backed_off",
          "gap_offset_px": 47,
          "timestamp": "<ISO>"
        }
      ],
      "terminal": false,
      "blocked": false,
      "note": "<human-readable summary>"
    }
  ]
}
```

**Fields to watch:**

| Field | Meaning |
|-------|---------|
| `type_counts` | Coverage map — are there types we haven't seen yet? |
| `total_encounters` | Session volume |
| Outcomes `"backed_off"` | Solver was disabled or wrong egress |
| Outcomes `"re_challenge"` | Platform detected us; rotate IP |
| `terminal=true, blocked=true` | IP was burned; rotate before next run |

---

## 8. Stuck detection and escalation

A puzzle encounter is flagged for human/Opus review when:
- `puzzle_type == "unknown"` — a type the module doesn't handle.
- `attempts[-1].outcome == "backed_off"` AND `note` contains "not found" —
  the DOM selectors couldn't locate the slider thumb (class names changed?).
- `terminal=True` — either re-challenge or circuit-breaker exhaustion.

**Escalation path:** stop the session, check the registry entry, and raise the
issue with the Opus orchestrator before the next run.  Do NOT change selectors
speculatively — DOM changes may be intentional anti-bot measures.

---

## 9. Live measurement is operator-run — NOT automated

> **This is a hard constraint, not a guideline.**

Solve-success rate, re-challenge rate, and ban escalation can ONLY be measured
in **operator-supervised** live sessions on the disposable egress.  These
metrics are:

- Non-deterministic (TikTok's acceptance threshold changes server-side).
- Risk-bearing (each attempt may escalate a soft block toward a hard ban on
  the disposable egress).
- Out of scope for automated CI/CD.

The offline tests in `tests/test_captcha_solver.py` cover the mechanics
(trajectory shape, gap detection algorithm, circuit-breaker counting,
default-OFF gate, registry JSON shape) but they are **not a substitute** for
live measurement.

**ADR-0004 success is judged by live measurement:** solve-success rate,
re-challenge rate, and whether enabling the solver correlates with hard bans on
disposable egresses.

---

## 10. Reversibility

ADR-0004 is explicitly reversible.  If Phase-1 data shows that solving
escalates bans even on disposable egresses, the path is:

1. Set `CAPTCHA_SOLVING_ENABLED = False` (the module default).
2. Revert to back-off-only on all platforms.
3. Invest in the Q-3 IP-rotation path (rotating residential/mobile proxies
   that make captchas less likely in the first place).

The existing back-off contract in `adapters/*/README.md` and
`docs/handoffs/robust-harvest.md` stays the **default**; solving is a
contained, opt-in experimental override.
