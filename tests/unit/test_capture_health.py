"""Unit tests for CaptureHealthTracker — per-watcher capture-health state machine."""

from __future__ import annotations

from unittest import mock

import pytest

from sessionfs.watchers.base import CaptureHealthTracker, WatcherHealth


class TestCaptureHealthTracker:
    """Tests for the shared CaptureHealthTracker state machine."""

    def test_initial_state_is_healthy(self):
        """A freshly-created tracker reports healthy with no error."""
        t = CaptureHealthTracker(tool_name="claude-code")
        assert t.health == WatcherHealth.HEALTHY
        assert t.last_error is None
        assert t.degraded_since is None

    def test_idle_tool_stays_healthy(self):
        """A tracker that never records a failure stays healthy forever.

        This is the false-positive guard: an idle tool (no native activity,
        no capture attempts) is NEVER degraded.  Degradation requires
        observed failure, and failure only happens when the watcher tries
        to parse a changed session.
        """
        t = CaptureHealthTracker(tool_name="claude-code")
        # Simulate time passing with zero activity.
        assert t.health == WatcherHealth.HEALTHY
        assert t.degraded_since is None

    def test_single_failure_does_not_degrade(self):
        """One failure is not enough — threshold is 3."""
        t = CaptureHealthTracker(tool_name="claude-code")
        t.record_failure("parse error: unexpected format")
        assert t.health == WatcherHealth.HEALTHY
        assert t.last_error == "parse error: unexpected format"

    def test_two_failures_do_not_degrade(self):
        """Two consecutive failures still below threshold."""
        t = CaptureHealthTracker(tool_name="claude-code")
        t.record_failure("error 1")
        t.record_failure("error 2")
        assert t.health == WatcherHealth.HEALTHY

    def test_three_consecutive_failures_marks_degraded(self):
        """3 consecutive parse failures → health='degraded' + degraded_since set."""
        t = CaptureHealthTracker(tool_name="claude-code")
        t.record_failure("bad json on line 5")
        t.record_failure("bad json on line 12")
        t.record_failure("missing required field 'messages'")

        assert t.health == WatcherHealth.DEGRADED
        assert t.degraded_since is not None
        assert "missing required field" in (t.last_error or "")

    def test_fourth_failure_stays_degraded_no_reemit(self):
        """After degraded, further failures keep it degraded but don't
        change degraded_since or re-emit telemetry."""
        t = CaptureHealthTracker(tool_name="claude-code")
        t.record_failure("e1")
        t.record_failure("e2")
        t.record_failure("e3")
        assert t.health == WatcherHealth.DEGRADED
        first_since = t.degraded_since

        t.record_failure("e4")
        assert t.health == WatcherHealth.DEGRADED
        assert t.degraded_since == first_since  # unchanged

    def test_success_resets_counter_and_recovers_from_degraded(self):
        """A successful capture resets the failure counter and clears degraded."""
        t = CaptureHealthTracker(tool_name="claude-code")
        # Degrade first
        t.record_failure("e1")
        t.record_failure("e2")
        t.record_failure("e3")
        assert t.health == WatcherHealth.DEGRADED

        # Success recovers
        t.record_success()
        assert t.health == WatcherHealth.HEALTHY
        assert t.degraded_since is None
        assert t.last_error is None

    def test_success_resets_counter_before_threshold(self):
        """A success before 3 failures resets the counter (stays healthy)."""
        t = CaptureHealthTracker(tool_name="claude-code")
        t.record_failure("e1")
        t.record_failure("e2")
        t.record_success()

        # Counter reset — need 3 NEW consecutive failures to degrade
        t.record_failure("new e1")
        t.record_failure("new e2")
        assert t.health == WatcherHealth.HEALTHY  # still below threshold

        t.record_failure("new e3")
        assert t.health == WatcherHealth.DEGRADED

    def test_interleaved_success_and_failure(self):
        """Success between failures keeps resetting the counter."""
        t = CaptureHealthTracker(tool_name="claude-code")
        for _ in range(10):
            t.record_failure("fail")
            t.record_success()
        assert t.health == WatcherHealth.HEALTHY

    def test_degraded_then_recover_then_degrade_again(self):
        """Full cycle: healthy → degraded → healthy → degraded."""
        t = CaptureHealthTracker(tool_name="claude-code")

        # First degradation
        for _ in range(3):
            t.record_failure("format v1 broken")
        assert t.health == WatcherHealth.DEGRADED
        first_since = t.degraded_since

        # Recover
        t.record_success()
        assert t.health == WatcherHealth.HEALTHY

        # Second degradation
        for _ in range(3):
            t.record_failure("format v2 broken")
        assert t.health == WatcherHealth.DEGRADED
        assert t.degraded_since != first_since  # new timestamp

    def test_set_degraded_structural_failure(self):
        """Structural failures (dir not found, etc.) set degraded immediately
        without counting toward the parse threshold."""
        t = CaptureHealthTracker(tool_name="claude-code")
        t.set_degraded("Projects dir not found: /nonexistent")
        assert t.health == WatcherHealth.DEGRADED
        assert t.degraded_since is not None
        assert "Projects dir not found" in (t.last_error or "")

    def test_set_healthy_clears_structural_degradation(self):
        """set_healthy recovers from structural degradation."""
        t = CaptureHealthTracker(tool_name="claude-code")
        t.set_degraded("dir missing")
        assert t.health == WatcherHealth.DEGRADED

        t.set_healthy()
        assert t.health == WatcherHealth.HEALTHY
        assert t.degraded_since is None
        assert t.last_error is None

    def test_record_success_also_recovers_structural_degradation(self):
        """record_success recovers regardless of how degradation was set."""
        t = CaptureHealthTracker(tool_name="claude-code")
        t.set_degraded("dir missing")
        t.record_success()
        assert t.health == WatcherHealth.HEALTHY


class TestCaptureHealthTelemetry:
    """Tests for telemetry emission on degraded transition."""

    @pytest.fixture(autouse=True)
    def _isolate_telemetry(self, monkeypatch):
        """Capture emit_once calls in a list so tests can inspect them."""
        self._emitted: list[tuple] = []

        def _fake_emit_once(event, marker_name, *, tool=None):
            self._emitted.append((event, marker_name, tool))

        # _emit_degraded does `from sessionfs.telemetry import emit_once`
        # so patching sessionfs.telemetry.emit_once is sufficient.
        monkeypatch.setattr(
            "sessionfs.telemetry.emit_once",
            _fake_emit_once,
        )

    def test_transition_to_degraded_emits_once(self):
        """First degradation emits capture_degraded with the tool name."""
        t = CaptureHealthTracker(tool_name="claude-code")
        t.record_failure("e1")
        t.record_failure("e2")
        t.record_failure("e3")

        assert len(self._emitted) == 1
        event, marker, tool = self._emitted[0]
        assert event == "capture_degraded"
        assert marker == "capture-degraded-claude-code"
        assert tool == "claude-code"

    def test_repeat_ticks_do_not_emit(self):
        """Only the transition emits — repeated ticks while degraded do not."""
        t = CaptureHealthTracker(tool_name="gemini-cli")
        for _ in range(3):
            t.record_failure("fail")
        assert len(self._emitted) == 1

        # More failures while already degraded — no additional emit
        t.record_failure("another fail")
        t.record_failure("yet another")
        assert len(self._emitted) == 1

    def test_recover_and_redegrade_emits_again(self):
        """Recovery + re-degradation emits again (new transition)."""
        t = CaptureHealthTracker(tool_name="codex")
        for _ in range(3):
            t.record_failure("fail")
        assert len(self._emitted) == 1

        t.record_success()  # recover
        assert t.health == WatcherHealth.HEALTHY

        for _ in range(3):
            t.record_failure("fail again")
        assert len(self._emitted) == 2  # new transition

    def test_structural_degraded_also_emits(self):
        """set_degraded for structural failures also emits telemetry."""
        t = CaptureHealthTracker(tool_name="cursor")
        t.set_degraded("global DB not found")
        assert len(self._emitted) == 1
        event, marker, tool = self._emitted[0]
        assert event == "capture_degraded"
        assert marker == "capture-degraded-cursor"
        assert tool == "cursor"

    def test_telemetry_failure_is_silent(self):
        """If emit_once raises, the tracker does not propagate."""
        def _explode(*args, **kwargs):
            raise RuntimeError("network dead")

        with mock.patch("sessionfs.telemetry.emit_once", side_effect=_explode):
            t = CaptureHealthTracker(tool_name="amp")
            t.record_failure("e1")
            t.record_failure("e2")
            t.record_failure("e3")
            # Should not raise
            assert t.health == WatcherHealth.DEGRADED


class TestWatcherStatusDegradedSince:
    """Tests for degraded_since persistence in WatcherStatus."""

    def test_watcher_status_accepts_degraded_since(self):
        """WatcherStatus model carries degraded_since through to JSON."""
        from sessionfs.daemon.status import WatcherStatus

        ws = WatcherStatus(
            name="claude-code",
            enabled=True,
            health="degraded",
            degraded_since="2026-08-11T10:30:00+00:00",
            last_error="Capture failed: bad json",
        )
        d = ws.model_dump()
        assert d["degraded_since"] == "2026-08-11T10:30:00+00:00"
        assert d["health"] == "degraded"

    def test_watcher_status_defaults_degraded_since_to_none(self):
        """Healthy watchers omit degraded_since (None)."""
        from sessionfs.daemon.status import WatcherStatus

        ws = WatcherStatus(name="claude-code", enabled=True, health="healthy")
        d = ws.model_dump()
        assert d["degraded_since"] is None


class TestScanEndDoesNotClearDegradation:
    def test_set_healthy_preserves_capture_degradation(self):
        """End-of-scan set_healthy() (structural) must NOT wipe per-capture
        degradation — only record_success() recovers it. Without this, a
        format break is hidden before daemon.json / doctor ever report it."""
        from sessionfs.watchers.base import CaptureHealthTracker, WatcherHealth
        t = CaptureHealthTracker(tool_name="codex")
        for _ in range(t.FAILURE_THRESHOLD):
            t.record_failure("parse boom")
        assert t.health == WatcherHealth.DEGRADED
        t.set_healthy()  # the unconditional end-of-scan call
        assert t.health == WatcherHealth.DEGRADED  # preserved
        assert t.degraded_since is not None
        t.record_success()  # ONLY this recovers
        assert t.health == WatcherHealth.HEALTHY

    def test_set_healthy_still_clears_structural_state(self):
        from sessionfs.watchers.base import CaptureHealthTracker, WatcherHealth
        t = CaptureHealthTracker(tool_name="codex")
        t.record_failure("one-off blip")  # below threshold
        t.set_healthy()
        assert t.health == WatcherHealth.HEALTHY
        assert t.last_error is None


class TestGuardSkipResetsFailureStreak:
    def test_all_watchers_record_success_on_guard_skip(self):
        """A should_recapture guard-skip follows a SUCCESSFUL parse — every
        watcher must reset the failure streak there (else doctor stays
        degraded through the common compaction-guard path)."""
        from pathlib import Path
        for name in ("claude_code", "codex", "cline", "copilot", "amp",
                     "gemini", "cursor"):
            src = Path(f"src/sessionfs/watchers/{name}.py").read_text()
            i = src.find("if not should_recapture(")
            assert i != -1, name
            window = src[i:i + 600]
            assert "record_success()" in window, f"{name} missing guard-skip reset"


def test_doctor_unreadable_status_is_not_green():
    """P2 (round 10): a malformed daemon.json must not render capture health
    as OK — unknown is a failure state."""
    from pathlib import Path
    src = Path("src/sessionfs/cli/cmd_doctor.py").read_text()
    i = src.find("could not read daemon.json")
    assert i != -1
    window = src[max(0, i - 300):i]
    assert "_check_mark(False)" in window


def test_success_clears_stale_error_below_threshold():
    """P3 (round 11): a sub-threshold failure's error string must not outlive
    the next success."""
    from sessionfs.watchers.base import CaptureHealthTracker
    t = CaptureHealthTracker(tool_name="amp")
    t.record_failure("one-off")
    t.record_success()
    assert t.last_error is None


def test_doctor_rejects_malformed_status_shapes():
    """P2 (round 11): valid-JSON-wrong-shape daemon.json must hit the
    unknown-failure branch, not crash or render green."""
    from pathlib import Path
    src = Path("src/sessionfs/cli/cmd_doctor.py").read_text()
    assert "malformed daemon status shape" in src
    assert "isinstance(w, dict) for w in watchers" in src


class TestDistinctSessionDegradation:
    def test_one_corrupt_file_retried_does_not_degrade(self):
        """P2 (round 12): the SAME session failing repeatedly is one bad file,
        not a format break — no degradation."""
        from sessionfs.watchers.base import CaptureHealthTracker, WatcherHealth
        t = CaptureHealthTracker(tool_name="codex")
        for _ in range(6):
            t.record_failure("boom", session_key="same-session")
        assert t.health == WatcherHealth.HEALTHY

    def test_failures_across_two_sessions_degrade(self):
        from sessionfs.watchers.base import CaptureHealthTracker, WatcherHealth
        t = CaptureHealthTracker(tool_name="codex")
        t.record_failure("boom", session_key="s1")
        t.record_failure("boom", session_key="s2")
        t.record_failure("boom", session_key="s1")
        assert t.health == WatcherHealth.DEGRADED

    def test_keyless_callers_keep_plain_counting(self):
        from sessionfs.watchers.base import CaptureHealthTracker, WatcherHealth
        t = CaptureHealthTracker(tool_name="amp")
        for _ in range(3):
            t.record_failure("boom")
        assert t.health == WatcherHealth.DEGRADED

    def test_all_watchers_pass_session_keys(self):
        from pathlib import Path
        for name in ("claude_code", "codex", "cline", "copilot", "amp",
                     "gemini", "cursor"):
            src = Path(f"src/sessionfs/watchers/{name}.py").read_text()
            assert "session_key=native_id" in src, name
