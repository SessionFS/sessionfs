"""Tests for the resident LLM-spend BudgetTracker (R4)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sessionfs.resident.budget import BudgetTracker


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_unlimited_budget_never_parks(tmp_path: Path):
    b = BudgetTracker("r", daily_token_budget=0, state_dir=tmp_path)
    b.record(1_000_000)
    ok, _ = b.can_wake()
    assert ok is True  # 0 = unlimited


def test_daily_budget_parks_when_exhausted(tmp_path: Path):
    b = BudgetTracker("r", daily_token_budget=1000, state_dir=tmp_path)
    assert b.can_wake()[0] is True
    b.record(1000)
    ok, reason = b.can_wake()
    assert ok is False
    assert "daily" in reason


def test_full_call_reserve_parks_before_starting_unaffordable_call(tmp_path: Path):
    """A full call's estimated cost is reserved before continuing — don't START
    a call the remaining budget can't cover."""
    b = BudgetTracker(
        "r", daily_token_budget=1000, per_wake_token_budget=900,
        per_call_estimate=300, state_dir=tmp_path,
    )
    b.record(800)  # spent today — only 200 left, less than a 300-token call
    b.begin_wake()  # a fresh heartbeat: wake_spent resets to 0
    ok, reason = b.can_wake()
    assert ok is False
    assert "insufficient" in reason


def test_per_wake_cap_tracks_cumulative_heartbeat_spend(tmp_path: Path):
    """The per-wake budget caps the WHOLE heartbeat, not a single call."""
    b = BudgetTracker(
        "r", daily_token_budget=100000, per_wake_token_budget=500, state_dir=tmp_path
    )
    b.begin_wake()
    assert b.can_wake()[0] is True
    b.record(300)  # first directive's LLM call
    assert b.can_wake()[0] is True  # 300 < 500, another directive is fine
    b.record(300)  # second directive → 600 cumulative this wake
    ok, reason = b.can_wake()
    assert ok is False
    assert "per-wake" in reason
    # A NEW heartbeat resets the per-wake counter.
    b.begin_wake()
    assert b.can_wake()[0] is True


def test_record_accumulates_and_persists(tmp_path: Path):
    b = BudgetTracker("r", daily_token_budget=5000, state_dir=tmp_path)
    b.record(1200)
    b.record(800)
    assert b.spent_today == 2000
    # A fresh tracker (restart) reloads today's spend from disk.
    b2 = BudgetTracker("r", daily_token_budget=5000, state_dir=tmp_path)
    assert b2.spent_today == 2000
    assert b2.can_wake()[0] is True  # 3000 left


def test_stale_state_from_a_previous_day_resets(tmp_path: Path):
    # Persist spend under a PAST date.
    state = tmp_path / "r-budget.json"
    state.write_text(json.dumps({"date": "2000-01-01", "spent": 99999}))
    b = BudgetTracker("r", daily_token_budget=1000, state_dir=tmp_path)
    assert b.spent_today == 0  # yesterday's spend does not carry over
    assert b.can_wake()[0] is True


def test_negative_tokens_are_clamped(tmp_path: Path):
    b = BudgetTracker("r", daily_token_budget=1000, state_dir=tmp_path)
    b.record(-500)
    assert b.spent_today == 0


def test_name_is_sanitized_into_the_filename(tmp_path: Path):
    b = BudgetTracker("../evil/name", daily_token_budget=100, state_dir=tmp_path)
    b.record(10)
    # The state file stays inside state_dir (no path traversal from the name).
    files = list(tmp_path.iterdir())
    assert files
    for f in files:
        assert f.parent == tmp_path


def test_daily_budget_requires_per_wake_cap():
    """A daily budget without a per-wake cap is rejected — the cap is what keeps
    one unrestricted call from overshooting the daily ceiling."""
    from sessionfs.resident.config import ResidentConfig, LLMConfig
    cfg = ResidentConfig(
        name="r", queue_id="wq", project="proj_x", org_profile="o",
        daily_token_budget=100000, per_wake_token_budget=0,
        llm=LLMConfig(base_url="https://x/v1", model="m", api_key="k"),
    )
    cfg._resolved_llm_key = "k"
    assert any("per_wake_token_budget is required" in e for e in cfg.validate())
    # A per-wake cap that covers a full call and stays under the daily ceiling
    # is valid.
    cfg.per_wake_token_budget = cfg.estimated_call_tokens() + 1000
    assert not any(
        "per_wake_token_budget" in e for e in cfg.validate()
    ), cfg.validate()


def test_per_wake_must_cover_a_full_call():
    """A per-wake cap smaller than a single call's cost is rejected."""
    from sessionfs.resident.config import ResidentConfig, LLMConfig
    cfg = ResidentConfig(
        name="r", queue_id="wq", project="proj_x", org_profile="o",
        daily_token_budget=100000, per_wake_token_budget=100,  # far too small
        mind_token_budget=8000,
        llm=LLMConfig(base_url="https://x/v1", model="m", api_key="k", max_tokens=4096),
    )
    cfg._resolved_llm_key = "k"
    assert any("must be at least" in e for e in cfg.validate())


def test_per_wake_reserves_full_call_headroom(tmp_path: Path):
    """Within a heartbeat, don't START a call unless the per-wake budget still
    has a full call's headroom (round-8: reserve before continuing)."""
    b = BudgetTracker(
        "r", daily_token_budget=100000, per_wake_token_budget=1000,
        per_call_estimate=400, state_dir=tmp_path,
    )
    b.begin_wake()
    assert b.can_wake()[0] is True          # 0 + 400 <= 1000
    b.record(400)
    assert b.can_wake()[0] is True          # 400 + 400 <= 1000
    b.record(400)                            # 800 this heartbeat
    ok, reason = b.can_wake()
    assert ok is False                       # 800 + 400 > 1000 → not enough for another
    assert "per-wake" in reason


def test_persistence_failure_fails_closed(tmp_path: Path):
    """If daily spend can't be persisted, the ceiling isn't restart-safe — park
    (fail closed) rather than silently stop enforcing it."""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")  # mkdir under it will fail
    b = BudgetTracker(
        "r", daily_token_budget=1000, per_call_estimate=100,
        state_dir=blocker / "sub",
    )
    # No record() call — the INIT probe already detected the unwritable dir, so
    # the very first can_wake() fails closed.
    ok, reason = b.can_wake()
    assert ok is False
    assert "persist" in reason


def test_negative_budget_rejected():
    """A negative budget is rejected (not silently treated as 0 = unlimited)."""
    from sessionfs.resident.config import ResidentConfig, LLMConfig
    cfg = ResidentConfig(
        name="r", queue_id="wq", project="p", org_profile="o",
        daily_token_budget=-5,
        llm=LLMConfig(base_url="https://x/v1", model="m", api_key="k"),
    )
    cfg._resolved_llm_key = "k"
    assert any(">= 0" in e for e in cfg.validate())


def test_zero_usage_charges_the_estimate(tmp_path: Path):
    """A call reporting 0 tokens (provider omitted usage) is charged the
    per-call estimate — missing usage must not mean free, unbounded calls."""
    b = BudgetTracker(
        "r", daily_token_budget=10000, per_wake_token_budget=5000,
        per_call_estimate=2000, state_dir=tmp_path,
    )
    b.record(0)  # a call happened but usage was unavailable
    assert b.spent_today == 2000  # charged the estimate, not 0


def test_zero_usage_without_budget_stays_free(tmp_path: Path):
    """With no budget configured (per_call=0), a 0-token record stays 0."""
    b = BudgetTracker("r", daily_token_budget=0, state_dir=tmp_path)
    b.record(0)
    assert b.spent_today == 0


def test_corrupt_current_state_fails_closed(tmp_path: Path):
    """A truncated/malformed state file must NOT silently reset spend to 0 — it
    fails closed (parks) so a same-day restart can't forget prior spend."""
    (tmp_path / "r-budget.json").write_text("{ this is not valid json")
    b = BudgetTracker(
        "r", daily_token_budget=1000, per_call_estimate=100, state_dir=tmp_path
    )
    ok, reason = b.can_wake()
    assert ok is False
    assert "unreadable" in reason
    # It also treated today's daily budget as already exhausted.
    assert b.spent_today == 1000


def test_corrupt_flag_self_heals_on_date_rollover(tmp_path: Path):
    """A corrupt state file from a PREVIOUS day must not block today."""
    (tmp_path / "r-budget.json").write_text("{corrupt")
    b = BudgetTracker(
        "r", daily_token_budget=1000, per_call_estimate=100, state_dir=tmp_path
    )
    assert b.can_wake()[0] is False  # corrupt → parked today
    # Simulate the UTC date advancing.
    b._date = "1999-01-01"
    ok, _ = b.can_wake()  # _roll_date() runs → self-heals
    assert ok is True


def test_per_wake_only_budget_ignores_persistence(tmp_path: Path):
    """A per-wake-ONLY budget doesn't persist anything, so an unwritable dir must
    NOT fail it closed (only daily spend is persisted)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file")
    b = BudgetTracker(
        "r", daily_token_budget=0, per_wake_token_budget=5000,
        per_call_estimate=1000, state_dir=blocker / "sub",
    )
    # No daily budget → no persistence needed → not failed closed.
    assert b.can_wake()[0] is True
