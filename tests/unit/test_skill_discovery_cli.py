"""CLI surfaces for marketplace discovery (marketplace v2 §9): publisher
stats and package settings, org usage, categories, and the admin
freshness sweeper."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from sessionfs.cli import cmd_admin, cmd_skill_publisher
from sessionfs.cli.cmd_admin import admin_app
from sessionfs.cli.cmd_skill import skill_app
from tests.utils.ansi import strip_ansi

runner = CliRunner()


def _floor(count: int | None) -> dict:
    return {"count": count, "display": "<10" if count is None else str(count)}


@pytest.fixture
def requests(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, Any]]:
    calls: list[tuple[str, str, Any]] = []

    def fake(method: str, path: str, payload: dict | None = None) -> Any:
        calls.append((method, path, payload))
        if path.startswith("/publishers/pub_1/analytics"):
            return {
                "namespace": "acme", "window_days": 30,
                "totals": {"installs": _floor(25), "active_installs": _floor(12), "uninstalls": _floor(None),
                           "uninstall_rate": 0.12, "translation_failures": _floor(None),
                           "service_installs": _floor(0)},
                "by_tool": [{"tool": "codex", "installs": _floor(25), "active": _floor(12),
                             "uninstalls": _floor(None), "uninstall_rate": None,
                             "translation_failures": _floor(0)}],
                "packages": [{"namespace": "acme", "name": "lint\x1b[2J\u202e", "current_version": "1.2.0",
                              "installs": _floor(25), "active_installs": _floor(12),
                              "works_in_regressions": ["cursor"], "scan": {"max_severity": "low"},
                              "open_reports": 1, "stale": True}],
            }
        if path.startswith("/publishers/pub_1/packages/"):
            return {"package_id": "spk_1", "namespace": "acme", "name": "lint",
                    "categories": payload.get("categories", []) if payload else [],
                    "deprecated_at": "2026-10-20T00:00:00+00:00" if payload and payload.get("deprecated") else None,
                    "successor_package_id": payload.get("successor_package_id") if payload else None,
                    "readme_path": None}
        if path.startswith("/orgs/org_1/skills/usage"):
            return {"items": [{"namespace": "acme", "name": "lint\x1b]0;pwned\x07", "installs": 4, "active": 3,
                               "uninstalls": 1, "translation_failures": 0,
                               "by_tool": [{"tool": "codex"}], "deprecated": True}]}
        raise AssertionError(path)

    monkeypatch.setattr(cmd_skill_publisher, "_request", fake)
    return calls


def test_publisher_stats_prints_floored_counts(requests) -> None:
    result = runner.invoke(skill_app, ["publisher", "stats", "pub_1", "--days", "7"], env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.output
    assert requests[-1][:2] == ("GET", "/publishers/pub_1/analytics?days=7")
    plain = strip_ansi(result.output)
    assert "Installs 25" in plain and "Uninstalls <10 (12.0%)" in plain
    assert "n/a (<10 installs)" in plain
    assert "cursor" in plain and "stale" in plain
    # Server-supplied text cannot carry terminal escapes or bidi controls.
    assert "\x1b[2J" not in result.output and "‮" not in result.output


def test_publisher_package_settings_payload(requests) -> None:
    result = runner.invoke(
        skill_app,
        ["publisher", "package", "pub_1", "spk_1", "--category", "review", "--category", "testing",
         "--deprecate", "--message", "use v2", "--successor", "spk_2"],
    )
    assert result.exit_code == 0, result.output
    assert requests[-1] == (
        "PATCH", "/publishers/pub_1/packages/spk_1",
        {"categories": ["review", "testing"], "deprecated": True, "deprecation_message": "use v2",
         "successor_package_id": "spk_2"},
    )
    assert "Deprecated since" in strip_ansi(result.output)
    nothing = runner.invoke(skill_app, ["publisher", "package", "pub_1", "spk_1"])
    assert nothing.exit_code != 0
    both = runner.invoke(skill_app, ["publisher", "package", "pub_1", "spk_1", "--deprecate", "--undeprecate"])
    assert both.exit_code != 0


def test_org_usage_table(requests) -> None:
    result = runner.invoke(skill_app, ["usage", "--org", "org_1"], env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.output
    assert requests[-1][:2] == ("GET", "/orgs/org_1/skills/usage?days=30")
    plain = strip_ansi(result.output)
    assert "acme/lint" in plain and "deprecated" in plain
    assert "\x1b]0;" not in result.output and "\x07" not in result.output


def test_admin_freshness_sweep_pages_to_done(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    pages = [
        {"processed": 2, "stale": 1, "unmaintained": 0, "pruned_dedupe_rows": 5, "next_cursor": "spk_b", "done": False},
        {"processed": 1, "stale": 0, "unmaintained": 1, "pruned_dedupe_rows": 0, "next_cursor": None, "done": True},
    ]

    def fake(method: str, path: str, json_body: dict | None = None) -> dict:
        assert (method, path) == ("POST", "/api/v1/admin/marketplace/freshness/sweep")
        calls.append(dict(json_body or {}))
        return pages[len(calls) - 1]

    monkeypatch.setattr(cmd_admin, "_admin_request", fake)
    result = runner.invoke(admin_app, ["skills-freshness-sweep", "--batch", "2"])
    assert result.exit_code == 0, result.output
    assert calls == [{"limit": 2}, {"limit": 2, "cursor": "spk_b"}]
    assert "3 package(s), 1 stale, 1 unmaintained, 5 expired" in strip_ansi(result.output)
