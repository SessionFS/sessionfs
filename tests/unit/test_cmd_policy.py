"""`sfs policy` CLI (marketplace v2 §8.6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sessionfs.cli import cmd_policy
from sessionfs.cli.main import app
from tests.utils.ansi import strip_ansi

runner = CliRunner()

POLICY_ITEM = {
    "org_id": "org_a",
    "org_name": "Acme",
    "revision": 2,
    "policy_hash": "sha256:x",
    "policy": {"skills": {"mode": "allowlist", "max_scan_severity": "medium", "allow_remote_mcp": True,
                          "allowlist": [{"publisher": "pub_acme"}]}},
    "tools": {
        "claude-code": {
            "enforcement": "managed",
            "enforceable": ["allow_hooks", "block_unmanaged_marketplaces"],
            "report_only": ["mode"],
            "unsupported": ["auto_update"],
            "artifacts": [{"kind": "file", "content_hash": "sha256:none",
                           "paths": {"linux": "/nonexistent/managed-settings.json",
                                     "macos": "/nonexistent/managed-settings.json"}}],
        },
        "antigravity": {"enforcement": "unsupported", "enforceable": [], "report_only": [], "unsupported": []},
    },
}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list:
    sent: list = []

    def fake(method, path, payload=None, *, headers=None, allow=()):
        sent.append((method, path, payload, headers))
        if path == "/devices/me/policy":
            return 200, {"items": [POLICY_ITEM]}, {}
        if path.endswith("/skills/policy/compiled?tool=claude-code"):
            return 200, {"compiled": {"claude-code": {
                "enforcement": "managed",
                "artifacts": [{"kind": "file", "content": '{\n  "allowManagedHooksOnly": true\n}\n',
                               "paths": {"linux": "/etc/claude-code/managed-settings.json"}}],
            }}}, {}
        if path.endswith("/skills/policy/compiled?tool=amp"):
            return 200, {"compiled": {"amp": {"enforcement": "report_only", "artifacts": []}}}, {}
        if path.endswith("/skills/policy/preview"):
            return 200, {"current_revision": 2, "changed_tools": ["claude-code"], "warnings": [
                {"tool": "claude-code", "code": "claude-code.managed_hooks_only", "message": "stops hooks"}
            ]}, {}
        if method == "GET" and path.endswith("/skills/policy"):
            return 200, {"revision": 2}, {"etag": '"osp-2"'}
        if method == "PUT" or path.endswith("/skills/policy/disable"):
            return 200, {"revision": 3, "policy_hash": "sha256:y"}, {}
        raise AssertionError(path)

    monkeypatch.setattr(cmd_policy, "_request", fake)
    return sent


def test_show_lists_policy_and_per_tool_enforcement(calls: list) -> None:
    result = runner.invoke(app, ["policy", "show"])
    out = strip_ansi(result.output)
    assert result.exit_code == 0, out
    assert "Acme (org_a) - revision 2" in out
    assert "mode: allowlist" in out
    assert "enforced by the tool: allow_hooks, block_unmanaged_marketplaces" in out
    assert "checked by SessionFS only: mode" in out


def test_compile_prints_the_raw_artifact(calls: list) -> None:
    result = runner.invoke(app, ["policy", "compile", "--tool", "claude-code"])
    assert result.exit_code == 0, result.output
    assert '"allowManagedHooksOnly": true' in strip_ansi(result.output)
    # The org came from the one policy that applies.
    assert any(path.startswith("/orgs/org_a/") for _, path, _, _ in calls)
    refused = runner.invoke(app, ["policy", "compile", "--tool", "amp"])
    assert refused.exit_code == 1


def test_status_reports_advisory_when_nothing_is_deployed(calls: list) -> None:
    result = runner.invoke(app, ["policy", "status", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(strip_ansi(result.output))
    assert [(r["tool"], r["delivery"]) for r in rows] == [("claude-code", "advisory")]


def test_set_previews_acknowledges_and_publishes_with_if_match(calls: list, tmp_path: Path) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"skills": {"mode": "open"}}))
    declined = runner.invoke(app, ["policy", "set", "--org", "org_a", "--file", str(policy)], input="n\n")
    assert declined.exit_code == 1
    assert not any(method == "PUT" for method, *_ in calls)
    result = runner.invoke(
        app, ["policy", "set", "--org", "org_a", "--file", str(policy), "--acknowledge-warnings"]
    )
    out = strip_ansi(result.output)
    assert result.exit_code == 0, out
    assert "Published revision 3" in out
    [(method, path, payload, headers)] = [c for c in calls if c[0] == "PUT"]
    assert headers == {"If-Match": '"osp-2"'}
    assert payload["acknowledge_warnings"] == ["claude-code.managed_hooks_only"]
    # The ETag comes from the preview: no separate GET of the policy.
    assert not any(method == "GET" and p.endswith("/skills/policy") for method, p, *_ in calls)


def test_set_refuses_to_overwrite_a_revision_published_after_the_preview(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Someone publishes revision 3 while `sfs policy set` waits for the
    confirmation: the PUT carries the preview's revision 2 and gets 409."""
    server = {"revision": 2}
    sent: list = []

    def fake(method, path, payload=None, *, headers=None, allow=()):
        sent.append((method, path, headers))
        if path.endswith("/skills/policy/preview"):
            preview = {"current_revision": server["revision"], "changed_tools": [], "warnings": [
                {"tool": "codex", "code": "codex.managed_hooks_only", "message": "stops hooks"}
            ]}
            server["revision"] = 3  # a concurrent admin publishes
            return 200, preview, {}
        if method == "GET" and path.endswith("/skills/policy"):
            return 200, {"revision": server["revision"]}, {"etag": f'"osp-{server["revision"]}"'}
        if method == "PUT":
            if headers.get("If-Match") != f'"osp-{server["revision"]}"':
                cmd_policy.err_console.print("The policy changed since it was read; reload and retry.")
                raise cmd_policy.typer.Exit(1)
            return 200, {"revision": server["revision"] + 1, "policy_hash": "sha256:y"}, {}
        raise AssertionError(path)

    monkeypatch.setattr(cmd_policy, "_request", fake)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"skills": {"mode": "open"}}))
    result = runner.invoke(app, ["policy", "set", "--org", "org_a", "--file", str(policy)], input="y\n")
    assert result.exit_code == 1, strip_ansi(result.output)
    assert "Published" not in strip_ansi(result.output)
    [(_, _, headers)] = [c for c in sent if c[0] == "PUT"]
    assert headers == {"If-Match": '"osp-2"'}


def test_disable_publishes_against_the_current_revision(calls: list) -> None:
    declined = runner.invoke(app, ["policy", "disable", "--org", "org_a"], input="n\n")
    assert declined.exit_code == 1
    assert not any(path.endswith("/disable") for _, path, *_ in calls)
    result = runner.invoke(app, ["policy", "disable", "--org", "org_a", "--yes"])
    assert result.exit_code == 0, strip_ansi(result.output)
    [(method, path, _, headers)] = [c for c in calls if c[1].endswith("/disable")]
    assert method == "POST" and headers == {"If-Match": '"osp-2"'}
