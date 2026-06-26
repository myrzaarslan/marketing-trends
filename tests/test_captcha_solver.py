"""Offline deterministic tests for core.harness.captcha_solver (ADR-0004).

All tests run without hitting any live platform.  They cover:

1. Gap detection     — fixture PNGs with known gap positions → algorithm finds them.
2. Move trajectory   — make_slider_moves() produces the right shape; no Playwright needed.
3. Classification    — classify_page() correctly identifies slider vs. unknown.
4. Default-OFF gate  — back-off path is provably unchanged when disabled.
5. Egress guard      — back-off when egress_is_disposable=False.
6. Unknown type      — back-off (record + no solve) for non-slider captchas.
7. Circuit breaker   — stops at MAX_ATTEMPTS; terminal on simulated re-challenge.
8. Registry shape    — record_puzzle() writes the correct JSON structure.

WHAT IS NOT TESTED HERE (and why):
- Live platform acceptance of the drag trajectory — no offline proxy exists
  (see ADR-0004 and PLAYBOOK.md §9).  Only an operator-run live session can
  measure solve-rate / re-challenge rate / ban escalation.
- Screenshots / network calls — completely out of scope.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Make the repo root importable without an installed package.
sys.path.insert(0, str(Path(__file__).parent.parent))

import core.harness.captcha_solver as cs
from core.harness.captcha_solver import (
    MAX_ATTEMPTS,
    Move,
    PuzzleRecord,
    SolveAttempt,
    SolveResult,
    apply_captcha_moves,
    attempt_solve,
    classify_page,
    detect_gap_offset,
    make_slider_moves,
    record_puzzle,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "captcha"


def _read_fixture(name: str) -> bytes:
    path = FIXTURES_DIR / name
    if not path.exists():
        pytest.skip(f"fixture {name} missing — run tests/fixtures/captcha/generate.py first")
    return path.read_bytes()


def _run(coro):
    """Run an async coroutine synchronously (no pytest-asyncio needed)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Gap detection (offline, no Playwright)
# ---------------------------------------------------------------------------

class TestGapDetection:
    """detect_gap_offset() finds the dark column in fixture images."""

    def test_gap_at_40(self):
        img = _read_fixture("slider_gap40.png")
        offset = detect_gap_offset(img)
        # The gap shadow spans columns 40–49; the darkest single column should
        # be within that window (min of a uniform shadow → leftmost dark col).
        assert 40 <= offset <= 49, f"expected 40–49, got {offset}"

    def test_gap_at_120(self):
        img = _read_fixture("slider_gap120.png")
        offset = detect_gap_offset(img)
        assert 120 <= offset <= 129, f"expected 120–129, got {offset}"

    def test_gap_at_200(self):
        img = _read_fixture("slider_gap200.png")
        offset = detect_gap_offset(img)
        assert 200 <= offset <= 209, f"expected 200–209, got {offset}"

    def test_deterministic(self):
        """Same image bytes → same offset every call."""
        img = _read_fixture("slider_gap120.png")
        assert detect_gap_offset(img) == detect_gap_offset(img)


# ---------------------------------------------------------------------------
# 2. Move trajectory (no Playwright, no live page)
# ---------------------------------------------------------------------------

class TestMoveTrajectory:
    """make_slider_moves() produces the correct structure and shape."""

    def _make(self, thumb_x=10.0, gap_x=150.0, n_steps=28) -> list[Move]:
        import random
        return make_slider_moves(
            thumb_x=thumb_x,
            thumb_y=50.0,
            gap_x=gap_x,
            n_steps=n_steps,
            _rng=random.Random(42),  # seeded for determinism
        )

    def test_returns_moves(self):
        moves = self._make()
        assert len(moves) == 29  # n_steps + 1 (0 through n_steps inclusive)

    def test_all_drag_type(self):
        for mv in self._make():
            assert mv.type == "drag"

    def test_starts_at_thumb(self):
        moves = self._make(thumb_x=10.0, gap_x=150.0)
        first = moves[0]
        assert first.x_start == 10.0

    def test_ends_near_gap(self):
        moves = self._make(thumb_x=10.0, gap_x=150.0)
        last = moves[-1]
        # Bezier ease-in-out: eased(1.0) == 1.0 exactly → x = gap_x + jitter
        # Jitter at t=1 is 0 (jitter_scale = 4*1*0 = 0), so x should equal gap_x.
        assert abs(last.x_start - 150.0) < 0.5, f"expected ~150.0, got {last.x_start}"

    def test_monotonically_progresses(self):
        """X coordinates should advance from left to right overall."""
        moves = self._make(thumb_x=10.0, gap_x=150.0)
        xs = [m.x_start for m in moves]
        # Not strictly monotone (ease curve is smooth but doesn't overshoot),
        # so just check that start < midpoint < end.
        mid = xs[len(xs) // 2]
        assert xs[0] < mid < xs[-1]

    def test_y_jitter_bounded(self):
        """Y coordinates should stay within ±3 px of the thumb_y."""
        moves = self._make()
        for mv in moves:
            assert abs(mv.y_start - 50.0) <= 3.0, f"jitter out of range: {mv.y_start}"

    def test_duration_positive(self):
        for mv in self._make():
            assert mv.duration_ms > 0

    def test_seeded_rng_deterministic(self):
        import random
        m1 = make_slider_moves(
            thumb_x=10.0, thumb_y=50.0, gap_x=150.0, _rng=random.Random(99)
        )
        m2 = make_slider_moves(
            thumb_x=10.0, thumb_y=50.0, gap_x=150.0, _rng=random.Random(99)
        )
        assert [m.x_start for m in m1] == [m.x_start for m in m2]

    def test_unseeded_differs(self):
        m1 = make_slider_moves(thumb_x=10.0, thumb_y=50.0, gap_x=150.0)
        m2 = make_slider_moves(thumb_x=10.0, thumb_y=50.0, gap_x=150.0)
        # With high probability the random durations differ (1 in ~10^30 chance they match).
        durations1 = [m.duration_ms for m in m1]
        durations2 = [m.duration_ms for m in m2]
        assert durations1 != durations2


# ---------------------------------------------------------------------------
# 3. Classification (no Playwright)
# ---------------------------------------------------------------------------

class TestClassification:
    """classify_page() distinguishes slider captcha from benign content."""

    def test_slider_ru(self):
        assert classify_page("передвиньте ползунок, чтобы совместить пазл") == "slider"

    def test_slider_en(self):
        assert classify_page("drag the slider to verify") == "slider"

    def test_slider_puzzle_keyword(self):
        assert classify_page("please solve the puzzle") == "slider"

    def test_captcha_verify_class(self):
        assert classify_page("captcha-verify required") == "slider"

    def test_benign_interest_modal(self):
        assert classify_page("что вы хотели бы посмотреть в тiktok?") == "unknown"

    def test_benign_login_nudge(self):
        assert classify_page("войти в тiktok, чтобы видеть больше") == "unknown"

    def test_empty_string(self):
        assert classify_page("") == "unknown"

    def test_case_insensitive(self):
        # classify_page receives already-lowercased text; confirm it handles it.
        assert classify_page("DRAG THE SLIDER".lower()) == "slider"


# ---------------------------------------------------------------------------
# 4. Default-OFF gate (back-off path provably unchanged when disabled)
# ---------------------------------------------------------------------------

class TestDefaultOffGate:
    """When CAPTCHA_SOLVING_ENABLED=False (default), attempt_solve backs off
    immediately without any Playwright calls — indistinguishable from ADR-0001."""

    def setup_method(self):
        # Ensure the kill switch is OFF for these tests.
        cs.CAPTCHA_SOLVING_ENABLED = False

    def teardown_method(self):
        cs.CAPTCHA_SOLVING_ENABLED = False

    def test_returns_backoff_when_disabled(self, tmp_path):
        reg = str(tmp_path / "registry.json")
        result = _run(
            attempt_solve(
                None,  # page=None — must NOT be called
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="передвиньте ползунок",
            )
        )
        assert result.passed is False
        assert result.terminal is False
        assert result.blocked is False
        assert "CAPTCHA_SOLVING_ENABLED=False" in result.note

    def test_backoff_result_has_no_solve_attempt(self, tmp_path):
        reg = str(tmp_path / "registry.json")
        result = _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="передвиньте ползунок",
            )
        )
        assert result.record.attempts[0].outcome == "backed_off"
        assert result.record.attempts[0].moves == []

    def test_backoff_recorded_in_registry(self, tmp_path):
        reg = str(tmp_path / "registry.json")
        _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="передвиньте ползунок",
            )
        )
        data = json.loads(Path(reg).read_text())
        assert data["total_encounters"] == 1
        assert data["puzzles"][0]["attempts"][0]["outcome"] == "backed_off"

    def test_works_on_unknown_type_too(self, tmp_path):
        """Even for unknown type, the disabled gate fires first."""
        reg = str(tmp_path / "registry.json")
        result = _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="some benign page content",
            )
        )
        assert result.passed is False
        assert result.terminal is False
        assert "CAPTCHA_SOLVING_ENABLED=False" in result.note


# ---------------------------------------------------------------------------
# 5. Egress guard (never the home IP)
# ---------------------------------------------------------------------------

class TestEgressGuard:
    """Even when enabled, passing egress_is_disposable=False forces back-off."""

    def setup_method(self):
        cs.CAPTCHA_SOLVING_ENABLED = True

    def teardown_method(self):
        cs.CAPTCHA_SOLVING_ENABLED = False

    def test_home_ip_is_backed_off(self, tmp_path):
        reg = str(tmp_path / "registry.json")
        result = _run(
            attempt_solve(
                None,
                egress_is_disposable=False,  # ← the home-IP guard
                registry_path=reg,
                _body_override="передвиньте ползунок",
            )
        )
        assert result.passed is False
        assert result.terminal is False
        assert result.blocked is False
        assert "home-IP guard" in result.note or "egress_is_disposable=False" in result.note

    def test_home_ip_outcome_is_backed_off(self, tmp_path):
        reg = str(tmp_path / "registry.json")
        result = _run(
            attempt_solve(
                None,
                egress_is_disposable=False,
                registry_path=reg,
                _body_override="передвиньте ползунок",
            )
        )
        assert result.record.attempts[0].outcome == "backed_off"


# ---------------------------------------------------------------------------
# 6. Unknown puzzle type → record + back off
# ---------------------------------------------------------------------------

class TestUnknownType:
    """Non-slider captcha types are recorded and backed off, never attempted."""

    def setup_method(self):
        cs.CAPTCHA_SOLVING_ENABLED = True

    def teardown_method(self):
        cs.CAPTCHA_SOLVING_ENABLED = False

    def test_unknown_type_is_backed_off(self, tmp_path):
        reg = str(tmp_path / "registry.json")
        result = _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="please rotate the image to align the arrow",
            )
        )
        assert result.passed is False
        assert result.terminal is False
        assert result.record.puzzle_type == "unknown"
        assert result.record.attempts[0].outcome == "backed_off"

    def test_unknown_type_flagged_in_registry(self, tmp_path):
        reg = str(tmp_path / "registry.json")
        _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="click on all images containing bicycles",
            )
        )
        data = json.loads(Path(reg).read_text())
        puzzle = data["puzzles"][0]
        assert puzzle["puzzle_type"] == "unknown"
        assert "human/Opus review" in puzzle["note"]


# ---------------------------------------------------------------------------
# 7. Circuit breaker (simulated re-challenge and attempt exhaustion)
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    """Circuit breaker stops at MAX_ATTEMPTS and goes terminal on re-challenge."""

    def setup_method(self):
        cs.CAPTCHA_SOLVING_ENABLED = True

    def teardown_method(self):
        cs.CAPTCHA_SOLVING_ENABLED = False

    # --- re-challenge terminal ---

    def test_rechallenge_is_terminal(self, tmp_path, monkeypatch):
        """A single re-challenge after a solve attempt marks the result terminal."""
        reg = str(tmp_path / "registry.json")

        async def fake_apply(page, moves):
            return "re_challenge"

        monkeypatch.setattr(cs, "apply_captcha_moves", fake_apply)

        result = _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="передвиньте ползунок",
                _thumb_box_override={"x": 10, "y": 50, "width": 20, "height": 20},
                _gap_offset_override=40,
            )
        )
        assert result.terminal is True
        assert result.blocked is True
        assert result.passed is False
        assert "rotate" in result.note.lower() or "terminal" in result.note.lower()

    def test_rechallenge_stops_after_first(self, tmp_path, monkeypatch):
        """Re-challenge terminates immediately — no more attempts are made."""
        reg = str(tmp_path / "registry.json")
        call_count = 0

        async def fake_apply(page, moves):
            nonlocal call_count
            call_count += 1
            return "re_challenge"

        monkeypatch.setattr(cs, "apply_captcha_moves", fake_apply)
        _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="передвиньте ползунок",
                _thumb_box_override={"x": 10, "y": 50, "width": 20, "height": 20},
                _gap_offset_override=40,
            )
        )
        assert call_count == 1, f"expected exactly 1 call, got {call_count}"

    # --- exhaustion terminal (≤ MAX_ATTEMPTS) ---

    def test_stops_at_max_attempts(self, tmp_path, monkeypatch):
        """Circuit breaker: exactly MAX_ATTEMPTS calls before terminal."""
        reg = str(tmp_path / "registry.json")
        call_count = 0

        # Patch asyncio.sleep to avoid real waits in the loop.
        async def fast_sleep(_t):
            pass

        async def fake_apply(page, moves):
            nonlocal call_count
            call_count += 1
            return "fail"

        monkeypatch.setattr(cs, "apply_captcha_moves", fake_apply)
        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        result = _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="передвиньте ползунок",
                _thumb_box_override={"x": 10, "y": 50, "width": 20, "height": 20},
                _gap_offset_override=40,
            )
        )
        assert call_count == MAX_ATTEMPTS, f"expected {MAX_ATTEMPTS} calls, got {call_count}"
        assert result.terminal is True
        assert result.blocked is True
        assert result.passed is False
        assert "circuit breaker" in result.note.lower()

    def test_pass_on_third_attempt(self, tmp_path, monkeypatch):
        """A pass on the Nth attempt short-circuits the loop."""
        reg = str(tmp_path / "registry.json")
        call_count = 0

        async def fast_sleep(_t):
            pass

        async def fake_apply(page, moves):
            nonlocal call_count
            call_count += 1
            return "pass" if call_count == 3 else "fail"

        monkeypatch.setattr(cs, "apply_captcha_moves", fake_apply)
        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        result = _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="передвиньте ползунок",
                _thumb_box_override={"x": 10, "y": 50, "width": 20, "height": 20},
                _gap_offset_override=40,
            )
        )
        assert call_count == 3
        assert result.passed is True
        assert result.terminal is False

    def test_no_solve_on_disabled(self, tmp_path, monkeypatch):
        """When kill switch is OFF, apply_captcha_moves is never called."""
        cs.CAPTCHA_SOLVING_ENABLED = False
        reg = str(tmp_path / "registry.json")
        call_count = 0

        async def should_not_be_called(page, moves):
            nonlocal call_count
            call_count += 1
            return "fail"

        monkeypatch.setattr(cs, "apply_captcha_moves", should_not_be_called)
        _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="передвиньте ползунок",
            )
        )
        assert call_count == 0, "apply_captcha_moves must NOT be called when disabled"


# ---------------------------------------------------------------------------
# 8. Registry shape (record_puzzle + attempt_solve both write correct JSON)
# ---------------------------------------------------------------------------

class TestRegistryShape:
    """record_puzzle() and attempt_solve() write well-formed registry JSON."""

    def test_record_puzzle_creates_file(self, tmp_path):
        reg = str(tmp_path / "registry.json")
        rec = PuzzleRecord(
            puzzle_id="test-id-001",
            puzzle_type="slider",
            detected_at="2026-06-26T12:00:00+00:00",
            dom_signature="передвиньте ползунок",
            screenshot_path=None,
            attempts=[SolveAttempt(moves=[], outcome="backed_off")],
            terminal=False,
            blocked=False,
            note="test record",
        )
        record_puzzle(rec, registry_path=reg)
        assert Path(reg).exists()

    def test_registry_schema(self, tmp_path):
        reg = str(tmp_path / "registry.json")
        rec = PuzzleRecord(
            puzzle_id="test-id-002",
            puzzle_type="slider",
            detected_at="2026-06-26T12:00:00+00:00",
            dom_signature="drag the slider",
            screenshot_path=None,
            attempts=[SolveAttempt(moves=[], outcome="backed_off")],
        )
        record_puzzle(rec, registry_path=reg)
        data = json.loads(Path(reg).read_text())

        assert data["version"] == 1
        assert isinstance(data["total_encounters"], int)
        assert isinstance(data["type_counts"], dict)
        assert isinstance(data["puzzles"], list)
        assert "last_updated" in data

    def test_puzzle_schema_fields(self, tmp_path):
        reg = str(tmp_path / "registry.json")
        rec = PuzzleRecord(
            puzzle_id="test-id-003",
            puzzle_type="slider",
            detected_at="2026-06-26T12:00:00+00:00",
            dom_signature="передвиньте ползунок",
            screenshot_path=None,
            attempts=[
                SolveAttempt(
                    moves=[{"type": "drag", "x_start": 10.0}],
                    outcome="fail",
                    gap_offset_px=40,
                    timestamp="2026-06-26T12:00:01+00:00",
                )
            ],
            terminal=True,
            blocked=True,
            note="circuit breaker test",
        )
        record_puzzle(rec, registry_path=reg)
        puzzle = json.loads(Path(reg).read_text())["puzzles"][0]

        assert puzzle["puzzle_id"] == "test-id-003"
        assert puzzle["puzzle_type"] == "slider"
        assert puzzle["terminal"] is True
        assert puzzle["blocked"] is True
        assert puzzle["attempts"][0]["outcome"] == "fail"
        assert puzzle["attempts"][0]["gap_offset_px"] == 40

    def test_dedup_by_puzzle_id(self, tmp_path):
        """Writing the same puzzle_id twice replaces rather than appends."""
        reg = str(tmp_path / "registry.json")
        for note in ("first write", "updated write"):
            rec = PuzzleRecord(
                puzzle_id="dedup-test",
                puzzle_type="slider",
                detected_at="2026-06-26T12:00:00+00:00",
                dom_signature="test",
                screenshot_path=None,
                note=note,
            )
            record_puzzle(rec, registry_path=reg)
        data = json.loads(Path(reg).read_text())
        assert data["total_encounters"] == 1
        assert data["puzzles"][0]["note"] == "updated write"

    def test_type_counts_accumulate(self, tmp_path):
        """Multiple records with different types are counted correctly."""
        reg = str(tmp_path / "registry.json")
        for i, pt in enumerate(["slider", "slider", "unknown"]):
            rec = PuzzleRecord(
                puzzle_id=f"count-test-{i}",
                puzzle_type=pt,
                detected_at="2026-06-26T12:00:00+00:00",
                dom_signature="test",
                screenshot_path=None,
            )
            record_puzzle(rec, registry_path=reg)
        data = json.loads(Path(reg).read_text())
        assert data["type_counts"]["slider"] == 2
        assert data["type_counts"]["unknown"] == 1
        assert data["total_encounters"] == 3

    def test_attempt_solve_disabled_writes_registry(self, tmp_path):
        """Even in the disabled (back-off) path, the encounter is recorded."""
        cs.CAPTCHA_SOLVING_ENABLED = False
        reg = str(tmp_path / "registry.json")
        _run(
            attempt_solve(
                None,
                egress_is_disposable=True,
                registry_path=reg,
                _body_override="puzzle",
            )
        )
        data = json.loads(Path(reg).read_text())
        assert data["total_encounters"] == 1
        assert data["puzzles"][0]["attempts"][0]["outcome"] == "backed_off"
        cs.CAPTCHA_SOLVING_ENABLED = False
