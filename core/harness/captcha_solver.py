"""Default-OFF captcha-solving experiment scaffold (ADR-0004).

KILL SWITCH
-----------
``CAPTCHA_SOLVING_ENABLED = False``  ← the default.  Existing back-off path is
completely unchanged when this is False.  Flip it True ONLY on a disposable
egress (VPN or mobile tether) and ONLY when the operator is present to monitor.
NEVER on the home broadband that the spine's daily ingestion depends on.

EGRESS GUARD
------------
Even when enabled, every entry point requires the caller to explicitly pass
``egress_is_disposable=True``.  If False (or omitted), the function immediately
returns a backed-off SolveResult — the home-IP code path stays clean and the
back-off contract from robust-harvest.md is preserved.

SLIDER ONLY
-----------
This module understands ONE puzzle type: TikTok's slider puzzle
("Передвиньте ползунок, чтобы совместить пазл").  Unknown or unseen types are
recorded in the registry and backed off — never attempted.

CIRCUIT BREAKER
---------------
≤ ``MAX_ATTEMPTS`` (10) attempts per obstacle.  A re-challenge or 429 response
after a solve attempt = terminal → ``blocked=True``, signal: rotate the
disposable IP before resuming.  Never grind.

PHASE FRAMING (ADR-0004)
------------------------
Phase 1 = this scaffold + the two agent tools.  A human/strong-agent operator
runs harvest sessions; this module RECORDS every captcha encounter (DOM
signature + attempted move-sets + outcomes) to build the dataset a future cheap
Phase-2 model is distilled from.  We build tools and guardrails — NOT an
autonomous unattended grinder.

LIVE MEASUREMENT IS OPERATOR-RUN
---------------------------------
Drag velocity / jerk acceptance is TikTok's server-side behavioral signal.
There is NO offline proxy for it.  Solve-rate, re-challenge rate, and ban
escalation can ONLY be measured in operator-run live sessions on the disposable
egress.  Offline tests cover the mechanics (trajectory shape, gap detection,
circuit-breaker counting, default-OFF gate, registry shape) — they cannot
validate platform acceptance.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import random
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

# ---------------------------------------------------------------------------
# Kill switch — DEFAULT OFF (ADR-0004 §Guardrails)
# ---------------------------------------------------------------------------

CAPTCHA_SOLVING_ENABLED: bool = False
"""Module-level kill switch.  Default False preserves existing back-off.

To enable for a disposable-egress operator session::

    import core.harness.captcha_solver as cs
    cs.CAPTCHA_SOLVING_ENABLED = True

Or set env var ``CAPTCHA_SOLVING_ENABLED=1`` before the module is imported.

Turn it back off::

    cs.CAPTCHA_SOLVING_ENABLED = False
"""

if os.environ.get("CAPTCHA_SOLVING_ENABLED", "").strip() == "1":
    CAPTCHA_SOLVING_ENABLED = True

MAX_ATTEMPTS: int = 10
"""Circuit-breaker ceiling.  ≤ 10 solve attempts per obstacle; disposable
egress only.  On the home IP the count is irrelevant — we never attempt."""

_DEFAULT_REGISTRY_PATH: str = "data/captcha_registry.json"

# ---------------------------------------------------------------------------
# Puzzle detection markers (slider subset of discovery._BLOCK_MARKERS)
# ---------------------------------------------------------------------------

SLIDER_MARKERS: tuple[str, ...] = (
    "передвиньте ползунок",   # "slide the slider" — primary TikTok signal
    "совместить пазл",         # "align the puzzle piece"
    "перетащите",              # "drag" (generic drag verb)
    "drag the slider",
    "slide to verify",
    "puzzle piece",
    "drag the puzzle",
    "verify to continue",
    "captcha-verify",
    "slide to",
    "puzzle",
)

_RECHALLENGE_MARKERS: tuple[str, ...] = (
    "передвиньте ползунок",   # new challenge appeared right after a solve attempt
    "совместить пазл",
    "slide again",
    "try again",
    "incorrect",
    "неверно",
    "попробуйте ещё раз",
    "verification failed",
    "please try again",
)

_RATELIMIT_MARKERS: tuple[str, ...] = (
    "too many requests",
    "rate limit",
    "429",
    "try again later",
    "слишком много запросов",
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Move:
    """A single browser input action within a solve attempt."""

    type: Literal["drag", "click", "hold"]
    x_start: float
    y_start: float
    x_end: float = 0.0
    y_end: float = 0.0
    duration_ms: float = 800.0
    note: str = ""


@dataclass
class SolveAttempt:
    """One attempt at solving a puzzle — recorded regardless of outcome."""

    moves: list[dict]         # Move objects serialised to dicts
    outcome: str              # "pass" | "re_challenge" | "fail" | "backed_off"
    gap_offset_px: int = 0   # estimated gap pixel offset used for this attempt
    timestamp: str = ""


@dataclass
class PuzzleRecord:
    """Everything captured about one captcha obstacle encounter."""

    puzzle_id: str                      # uuid4, assigned at first detection
    puzzle_type: str                    # "slider" | "unknown"
    detected_at: str                    # ISO timestamp
    dom_signature: str                  # first ~120 chars of page body (normalised)
    screenshot_path: Optional[str]      # relative path to saved screenshot (if any)
    attempts: list[SolveAttempt] = field(default_factory=list)
    terminal: bool = False
    blocked: bool = False               # True = rotate disposable IP
    note: str = ""


@dataclass
class SolveResult:
    """Return value of :func:`attempt_solve`."""

    passed: bool
    terminal: bool     # True → stop this session, rotate IP
    blocked: bool
    note: str
    record: PuzzleRecord


# ---------------------------------------------------------------------------
# Puzzle classification (text-based, offline-testable)
# ---------------------------------------------------------------------------

def classify_page(body_lower: str) -> str:
    """Classify the captcha type visible on the page from its lower-cased body.

    Returns ``"slider"`` when any slider marker is found, otherwise
    ``"unknown"``.  Classification is purely text-based — no DOM queries or
    screenshots needed for the initial dispatch.

    Parameters
    ----------
    body_lower:
        Lower-cased inner text of the page body.

    Examples
    --------
    >>> classify_page("передвиньте ползунок, чтобы совместить пазл")
    'slider'
    >>> classify_page("drag the slider to verify")
    'slider'
    >>> classify_page("some other page content")
    'unknown'
    """
    if any(m in body_lower for m in SLIDER_MARKERS):
        return "slider"
    return "unknown"


# ---------------------------------------------------------------------------
# Gap detection (offline-testable, PIL only — no numpy/OpenCV dependency)
# ---------------------------------------------------------------------------

def detect_gap_offset(image_bytes: bytes) -> int:
    """Find the X pixel offset of the gap in a slider captcha background image.

    Algorithm: convert to grayscale, compute per-column mean pixel brightness,
    return the X coordinate of the column with the minimum mean.  The gap's
    shadow region is consistently the darkest column in TikTok's slider puzzle.

    This function is deterministic and fully offline-testable against saved PNG
    fixture images.

    Parameters
    ----------
    image_bytes:
        Raw PNG/JPEG bytes of the slider background image (NOT the piece).

    Returns
    -------
    int
        Pixel X coordinate (0-indexed) of the estimated gap centre column.

    Note
    ----
    Platform acceptance (drag velocity/jerk profile matching) has NO offline
    proxy — only an operator-run live session can measure it.
    """
    from PIL import Image  # noqa: PLC0415 — optional import (Pillow always present)

    img = Image.open(io.BytesIO(image_bytes)).convert("L")  # grayscale
    w, h = img.size
    if w == 0 or h == 0:
        return 0

    # Use tobytes() (raw pixel bytes, L-mode = 1 byte per pixel) rather than
    # getdata() which is deprecated in Pillow 12+ and removed in Pillow 14.
    raw = img.tobytes()
    col_means: list[float] = [
        sum(raw[y * w + x] for y in range(h)) / h
        for x in range(w)
    ]
    return col_means.index(min(col_means))


# ---------------------------------------------------------------------------
# Humanized move generation (offline-testable — no Playwright needed)
# ---------------------------------------------------------------------------

def _bezier_ease(t: float) -> float:
    """Cubic ease-in-out: slow start, fast middle, slow end (t ∈ [0, 1])."""
    return t * t * (3.0 - 2.0 * t)


def make_slider_moves(
    *,
    thumb_x: float,
    thumb_y: float,
    gap_x: float,
    track_y: Optional[float] = None,
    n_steps: int = 28,
    duration_ms: Optional[float] = None,
    _rng: Optional[random.Random] = None,
) -> list[Move]:
    """Generate a humanized drag move-set for a TikTok slider captcha.

    The drag path runs from the slider thumb (``thumb_x``, ``thumb_y``) to the
    gap target (``gap_x``) using a cubic ease-in-out curve with small random Y
    jitter to mimic natural hand tremor.

    Parameters
    ----------
    thumb_x, thumb_y:
        Current centre of the drag handle (bounding-box centre).
    gap_x:
        Absolute X coordinate of the gap target (track left edge + gap offset).
    track_y:
        If given, constrains the drag endpoint to this Y value.
    n_steps:
        Number of intermediate mouse positions (25-32 is human-like).
    duration_ms:
        Total drag duration in milliseconds.  Defaults to a random 650–1050 ms.
    _rng:
        Optional seeded :class:`random.Random` for deterministic test output.
        Production always leaves this ``None`` (fresh randomness each call).

    Returns
    -------
    list[Move]
        A sequence of ``Move(type="drag", ...)`` steps; the first is the
        mouse-down position, the last is the release position.
    """
    rng = _rng if _rng is not None else random.Random()
    if duration_ms is None:
        duration_ms = rng.uniform(650, 1050)

    end_y = track_y if track_y is not None else thumb_y
    step_ms = duration_ms / max(n_steps, 1)
    moves: list[Move] = []

    for i in range(n_steps + 1):
        t = i / n_steps
        eased = _bezier_ease(t)
        x = thumb_x + eased * (gap_x - thumb_x)
        # Jitter peaks at t=0.5 (±2 px), zero at start/end
        jitter_scale = 4.0 * t * (1.0 - t)
        y = thumb_y + eased * (end_y - thumb_y) + rng.uniform(-2, 2) * jitter_scale
        moves.append(
            Move(
                type="drag",
                x_start=round(x, 1),
                y_start=round(y, 1),
                x_end=round(x, 1),
                y_end=round(y, 1),
                duration_ms=round(step_ms, 1),
                note=f"step {i}/{n_steps}",
            )
        )
    return moves


# ---------------------------------------------------------------------------
# Registry persistence
# ---------------------------------------------------------------------------

def record_puzzle(
    record: PuzzleRecord,
    registry_path: str = _DEFAULT_REGISTRY_PATH,
) -> None:
    """Persist a :class:`PuzzleRecord` to the JSON puzzle registry.

    The registry accumulates ALL encounters — solved, backed off, and unknown
    types — forming the dataset a future cheap Phase-2 model will be distilled
    from.  Records are deduplicated by ``puzzle_id``; an existing entry is
    replaced, new entries are appended.

    The file lives under ``data/`` (gitignored).  It is created on first call.

    Parameters
    ----------
    record:
        The :class:`PuzzleRecord` to persist.
    registry_path:
        Override the default path (useful in tests).
    """
    path = Path(registry_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if path.exists():
        try:
            with open(path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing.setdefault("version", 1)
    existing.setdefault("puzzles", [])
    existing.setdefault("type_counts", {})
    existing.setdefault("last_updated", "")

    puzzles: list[dict] = existing["puzzles"]
    record_dict = asdict(record)

    # Dedup by puzzle_id: replace existing entry, or append.
    idx = next(
        (i for i, p in enumerate(puzzles) if p.get("puzzle_id") == record.puzzle_id),
        None,
    )
    if idx is not None:
        puzzles[idx] = record_dict
    else:
        puzzles.append(record_dict)

    # Recount from persisted records so counts stay accurate across sessions.
    counts: dict[str, int] = {}
    for p in puzzles:
        pt = p.get("puzzle_type", "unknown")
        counts[pt] = counts.get(pt, 0) + 1

    existing["type_counts"] = counts
    existing["last_updated"] = datetime.now(timezone.utc).isoformat()
    existing["total_encounters"] = len(puzzles)

    with open(path, "w") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Playwright execution — live moves (async, requires a real Playwright Page)
# ---------------------------------------------------------------------------

async def apply_captcha_moves(page, moves: list[Move]) -> str:
    """Execute a humanized drag move-set against a live Playwright page.

    Drives the slider through the intermediate positions encoded in ``moves``
    (as produced by :func:`make_slider_moves`).  Returns an outcome string:

    * ``"pass"``         — body no longer contains captcha markers.
    * ``"re_challenge"`` — a fresh captcha appeared immediately after the drag
                           (platform detected the attempt).
    * ``"fail"``         — captcha markers still present (wrong offset or
                           threshold not met).

    This function does NOT check :data:`CAPTCHA_SOLVING_ENABLED` — the caller
    (:func:`attempt_solve`) is responsible for the gate.

    Note: drag velocity/jerk acceptance is TikTok's server-side behavioral
    signal.  There is NO offline proxy for it; only a live operator run can
    measure whether the trajectory is accepted.

    Parameters
    ----------
    page:
        A Playwright ``Page`` object positioned on the captcha page.
    moves:
        Move sequence from :func:`make_slider_moves`.
    """
    if not moves:
        return "fail"

    first = moves[0]
    await page.mouse.move(first.x_start, first.y_start)
    await page.mouse.down()
    await page.wait_for_timeout(int(random.uniform(80, 160)))

    for mv in moves[1:]:
        await page.mouse.move(mv.x_start, mv.y_start)
        await page.wait_for_timeout(max(10, int(mv.duration_ms)))

    await page.mouse.up()
    await page.wait_for_timeout(int(random.uniform(300, 600)))

    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        return "fail"

    if any(m in body for m in _RATELIMIT_MARKERS):
        return "re_challenge"  # treat 429 as terminal (same path as re-challenge)
    if any(m in body for m in _RECHALLENGE_MARKERS):
        return "re_challenge"
    if any(m in body for m in SLIDER_MARKERS):
        return "fail"
    return "pass"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def attempt_solve(
    page,
    *,
    egress_is_disposable: bool,
    registry_path: str = _DEFAULT_REGISTRY_PATH,
    # ---- Test hooks (internal only — not for production use) ----
    _body_override: Optional[str] = None,
    _thumb_box_override: Optional[dict] = None,
    _gap_offset_override: Optional[int] = None,
) -> SolveResult:
    """Try to solve a captcha on ``page``.  DEFAULT OFF.

    This is the **single entry point** callers use.  All ADR-0004 guardrails
    are enforced here so no caller has to replicate them.

    Parameters
    ----------
    page:
        A live Playwright ``Page`` positioned on a page serving a captcha.
        May be ``None`` in tests that only exercise the pre-loop gates.
    egress_is_disposable:
        **Must be ``True`` for any solve to be attempted.**  Pass ``False``
        when running on the home IP — the function returns the back-off path
        immediately, unchanged from ADR-0001 behaviour.
    registry_path:
        Puzzle registry JSON path (override in tests).
    _body_override:
        *Test hook.*  If set, skips the live ``page.inner_text("body")`` call
        and uses this string instead.  Not for production use.
    _thumb_box_override:
        *Test hook.*  Playwright bounding box dict to use as the slider thumb
        location, skipping the live DOM lookup.  Not for production use.
    _gap_offset_override:
        *Test hook.*  Gap pixel offset to use, skipping the image-detection
        step.  Not for production use.

    Returns
    -------
    SolveResult
        Back-off (disabled / wrong egress)  →  ``passed=False, terminal=False``
        Passed                              →  ``passed=True``
        Terminal (re-challenge / exhausted) →  ``terminal=True, blocked=True``

    Back-off contract
    -----------------
    When ``CAPTCHA_SOLVING_ENABLED=False`` OR ``egress_is_disposable=False``,
    this function returns immediately with ``passed=False, terminal=False,
    blocked=False`` — provably identical to the pre-ADR-0004 back-off result.
    No Playwright calls are made in that path.
    """
    puzzle_id = str(uuid.uuid4())

    # Read body text (or use test override).
    if _body_override is not None:
        body_lower = _body_override.lower()
    else:
        try:
            body_lower = (await page.inner_text("body")).lower()
        except Exception:
            body_lower = ""

    dom_signature = body_lower[:120].replace("\n", " ").strip()
    puzzle_type = classify_page(body_lower)
    detected_at = datetime.now(timezone.utc).isoformat()

    record = PuzzleRecord(
        puzzle_id=puzzle_id,
        puzzle_type=puzzle_type,
        detected_at=detected_at,
        dom_signature=dom_signature,
        screenshot_path=None,
    )

    # ------------------------------------------------------------------
    # Gate 1: Kill switch (DEFAULT OFF)
    # ------------------------------------------------------------------
    if not CAPTCHA_SOLVING_ENABLED:
        record.attempts.append(
            SolveAttempt(
                moves=[],
                outcome="backed_off",
                timestamp=detected_at,
            )
        )
        record.note = (
            "solver disabled (CAPTCHA_SOLVING_ENABLED=False) — "
            "back-off path unchanged, no solve attempted"
        )
        record_puzzle(record, registry_path)
        return SolveResult(
            passed=False,
            terminal=False,
            blocked=False,
            note=record.note,
            record=record,
        )

    # ------------------------------------------------------------------
    # Gate 2: Egress guard — never the home IP (ADR-0004 §Guardrails)
    # ------------------------------------------------------------------
    if not egress_is_disposable:
        record.attempts.append(
            SolveAttempt(
                moves=[],
                outcome="backed_off",
                timestamp=detected_at,
            )
        )
        record.note = (
            "egress_is_disposable=False — home-IP guard enforced (ADR-0004); "
            "back-off-only, no solve attempted"
        )
        record_puzzle(record, registry_path)
        return SolveResult(
            passed=False,
            terminal=False,
            blocked=False,
            note=record.note,
            record=record,
        )

    # ------------------------------------------------------------------
    # Gate 3: Unknown puzzle type → record + back off (slider only)
    # ------------------------------------------------------------------
    if puzzle_type != "slider":
        record.attempts.append(
            SolveAttempt(
                moves=[],
                outcome="backed_off",
                timestamp=detected_at,
            )
        )
        record.note = (
            f"unseen puzzle type {puzzle_type!r} — recorded for human/Opus review; "
            "backed off (slider is the only type this module handles)"
        )
        record_puzzle(record, registry_path)
        return SolveResult(
            passed=False,
            terminal=False,
            blocked=False,
            note=record.note,
            record=record,
        )

    # ------------------------------------------------------------------
    # Attempt loop — circuit breaker: ≤ MAX_ATTEMPTS per obstacle
    # ------------------------------------------------------------------
    for attempt_num in range(1, MAX_ATTEMPTS + 1):
        attempt_ts = datetime.now(timezone.utc).isoformat()

        # --- Locate gap offset ---
        gap_offset_px: int
        if _gap_offset_override is not None:
            gap_offset_px = _gap_offset_override
        else:
            gap_offset_px = 0
            try:
                bg_el = page.locator(
                    '[class*="captcha"] img, [class*="slider"] img, '
                    '[id*="captcha"] img, [class*="puzzle"] img'
                ).first
                if await bg_el.count() > 0:
                    img_bytes = await bg_el.screenshot()
                    gap_offset_px = detect_gap_offset(img_bytes)
            except Exception:
                gap_offset_px = 0

        # --- Locate slider thumb ---
        thumb_box: Optional[dict]
        if _thumb_box_override is not None:
            thumb_box = _thumb_box_override
        else:
            thumb_box = None
            try:
                thumb_el = page.locator(
                    '[class*="slider-btn"], [class*="drag-btn"], '
                    '[class*="captcha-btn"], [class*="sliderbtn"], '
                    '[class*="slider_btn"], [class*="drag_btn"]'
                ).first
                if await thumb_el.count() > 0:
                    thumb_box = await thumb_el.bounding_box()
            except Exception:
                thumb_box = None

        if thumb_box is None:
            # Stuck — slider thumb not locatable; flag for human review.
            record.attempts.append(
                SolveAttempt(
                    moves=[],
                    outcome="backed_off",
                    gap_offset_px=gap_offset_px,
                    timestamp=attempt_ts,
                )
            )
            record.note = (
                f"slider thumb not found in DOM on attempt {attempt_num} — "
                "stuck, flagged for human/Opus review"
            )
            break

        thumb_x = thumb_box["x"] + thumb_box["width"] / 2
        thumb_y = thumb_box["y"] + thumb_box["height"] / 2

        # Locate slider track to compute absolute gap target X.
        target_x: float
        if _gap_offset_override is not None:
            # In test mode with no live page, treat gap_offset_px as absolute X.
            target_x = float(thumb_box["x"]) + gap_offset_px
        else:
            track_box: Optional[dict] = None
            try:
                track_el = page.locator(
                    '[class*="slider-track"], [class*="captcha-track"], '
                    '[class*="slider_track"], [class*="sliderbg"], '
                    '[class*="captcha-bg"]'
                ).first
                if await track_el.count() > 0:
                    track_box = await track_el.bounding_box()
            except Exception:
                track_box = None
            target_x = (
                (track_box["x"] + gap_offset_px) if track_box else (thumb_x + gap_offset_px)
            )

        moves = make_slider_moves(
            thumb_x=thumb_x,
            thumb_y=thumb_y,
            gap_x=target_x,
        )

        outcome = await apply_captcha_moves(page, moves)
        record.attempts.append(
            SolveAttempt(
                moves=[asdict(m) for m in moves],
                outcome=outcome,
                gap_offset_px=gap_offset_px,
                timestamp=attempt_ts,
            )
        )

        if outcome == "pass":
            record.note = f"passed on attempt {attempt_num}"
            record_puzzle(record, registry_path)
            return SolveResult(
                passed=True,
                terminal=False,
                blocked=False,
                note=record.note,
                record=record,
            )

        if outcome == "re_challenge":
            # Terminal: a fresh challenge after a solve = escalating detection.
            record.terminal = True
            record.blocked = True
            record.note = (
                f"re-challenge detected after attempt {attempt_num} — "
                "terminal; rotate the disposable IP before resuming"
            )
            record_puzzle(record, registry_path)
            return SolveResult(
                passed=False,
                terminal=True,
                blocked=True,
                note=record.note,
                record=record,
            )

        # outcome == "fail" — brief human-ish pause before next attempt.
        if attempt_num < MAX_ATTEMPTS:
            await asyncio.sleep(random.uniform(0.8, 1.8))

    # Exhausted MAX_ATTEMPTS without passing.
    record.terminal = True
    record.blocked = True
    record.note = (
        f"circuit breaker: {MAX_ATTEMPTS} attempts exhausted without passing — "
        "stop, rotate disposable IP before resuming"
    )
    record_puzzle(record, registry_path)
    return SolveResult(
        passed=False,
        terminal=True,
        blocked=True,
        note=record.note,
        record=record,
    )
