"""Test for `sfs resident health` (R5)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_resident import resident_app
from sessionfs.resident.config import ResidentConfig, LLMConfig

runner = CliRunner()


def _cfg() -> ResidentConfig:
    return ResidentConfig(
        name="rev", queue_id="wq_1", project="proj_1", org_profile="org",
        resident_id="res_abc", mode="review", persona="codex-reviewer",
        poll_interval_seconds=30,
        daily_token_budget=500000, per_wake_token_budget=60000,
        llm=LLMConfig(base_url="https://api.test/v1", model="gpt-x", api_key="k"),
    )


def test_health_reads_config_and_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Seed today's budget state where the runner writes it.
    state_dir = tmp_path / ".sessionfs" / "residents"
    state_dir.mkdir(parents=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (state_dir / "res_abc-budget.json").write_text(
        json.dumps({"date": today, "spent": 123456})
    )

    with patch.object(ResidentConfig, "from_toml", return_value=_cfg()):
        result = runner.invoke(resident_app, ["health", "--config", "rev"])

    assert result.exit_code == 0, result.output
    assert "review" in result.output          # mode
    assert "codex-reviewer" in result.output  # persona
    assert "res_abc" in result.output         # resident id
    assert "123456" in result.output          # spent today
    assert "500000" in result.output          # daily budget
    # Reminds the merger about the resident-reviewed marker.
    assert "resident_trusted" in result.output


def test_health_handles_missing_budget_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # no state file
    with patch.object(ResidentConfig, "from_toml", return_value=_cfg()):
        result = runner.invoke(resident_app, ["health", "--config", "rev"])
    assert result.exit_code == 0, result.output
    assert "no state yet" in result.output


def test_health_zeroes_stale_previous_day_spend(tmp_path: Path, monkeypatch):
    """A budget file from a PREVIOUS UTC day is stale — health reports 0 (the
    runner resets at the date rollover), not the old spend."""
    monkeypatch.setenv("HOME", str(tmp_path))
    state_dir = tmp_path / ".sessionfs" / "residents"
    state_dir.mkdir(parents=True)
    (state_dir / "res_abc-budget.json").write_text(
        json.dumps({"date": "2000-01-01", "spent": 999999})
    )
    with patch.object(ResidentConfig, "from_toml", return_value=_cfg()):
        result = runner.invoke(resident_app, ["health", "--config", "rev"])
    assert result.exit_code == 0, result.output
    assert "999999" not in result.output          # stale spend NOT shown
    assert "0 / 500000" in result.output           # reset to 0 for today


def test_health_handles_malformed_state(tmp_path: Path, monkeypatch):
    """Valid JSON that isn't an object (e.g. a list) doesn't crash health — it's
    reported as unreadable (the runner fails closed)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    state_dir = tmp_path / ".sessionfs" / "residents"
    state_dir.mkdir(parents=True)
    (state_dir / "res_abc-budget.json").write_text("[1, 2, 3]")  # valid JSON, not a dict
    with patch.object(ResidentConfig, "from_toml", return_value=_cfg()):
        result = runner.invoke(resident_app, ["health", "--config", "rev"])
    assert result.exit_code == 0, result.output
    assert "unreadable" in result.output


def test_health_missing_keys_is_unreadable(tmp_path: Path, monkeypatch):
    """A state dict missing date/spent keys is 'unreadable' (matches the runner's
    fail-closed _load), not silently 0."""
    monkeypatch.setenv("HOME", str(tmp_path))
    state_dir = tmp_path / ".sessionfs" / "residents"
    state_dir.mkdir(parents=True)
    (state_dir / "res_abc-budget.json").write_text('{"unrelated": 1}')
    with patch.object(ResidentConfig, "from_toml", return_value=_cfg()):
        result = runner.invoke(resident_app, ["health", "--config", "rev"])
    assert result.exit_code == 0, result.output
    assert "unreadable" in result.output
