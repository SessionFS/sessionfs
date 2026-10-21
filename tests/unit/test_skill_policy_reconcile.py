"""Advisory delivery of the org skills policy on the device
(marketplace v2 §8.4): refusals during pull and the compliance report."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from sessionfs.skills import hosted, policy_reconcile
from sessionfs.skills.hosted import HostedClient, PullReport
from sessionfs.skills.manager import SkillManager

V1 = "skv_" + "1" * 24
V2 = "skv_" + "2" * 24
HASH = "sha256:" + "a" * 64
REFUSAL = {
    "allowed": False,
    "violations": [{"code": "scripts_not_allowed", "field": "allow_skill_scripts", "message": "m"}],
    "org_id": "org_a",
    "policy_revision": 3,
    "message": "Blocked by the Acme skills policy: ... Ask an owner or admin of Acme.",
}


def _manager(tmp_path: Path) -> SkillManager:
    (tmp_path / "project").mkdir()
    (tmp_path / "home").mkdir()
    return SkillManager(
        project_root=tmp_path / "project",
        home_dir=tmp_path / "home",
        store_root=tmp_path / "home" / ".sessionfs" / "skills",
    )


def _client(routes: dict[str, httpx.Response], posted: list | None = None) -> HostedClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and posted is not None:
            posted.append(json.loads(request.content))
        return routes.get(request.url.path, httpx.Response(404, json={"detail": "Not Found"}))

    return HostedClient("https://sfs.test", "sk_test", transport=httpx.MockTransport(handler))


def _page(items: list, **extra) -> httpx.Response:
    return httpx.Response(200, json={"items": items, "has_more": False, "incomplete": False, **extra})


def test_pull_never_merges_refused_grants_and_says_why(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    request_row = {
        "id": "sir_1", "version_id": V2, "content_hash": HASH, "target_tools": [], "status": "approved",
        "target_scope": "project", "requested_capabilities": ["scripts"], "policy": REFUSAL,
    }
    client = _client({
        "/api/v1/auth/me": httpx.Response(200, json={"user_id": "u1"}),
        "/api/v1/users/me/skills/assignments": _page(
            [], policy_refused=[{"assignment_id": "osa_1", "org_id": "org_a", "version_id": V1,
                                 "package_id": "skp_1", "policy": REFUSAL}],
        ),
        "/api/v1/projects/proj_1/skill-install-requests": _page([request_row]),
    })
    report = PullReport()
    items = hosted.collect_pull_items(manager, client, device_id=None, project_id="proj_1", report=report)
    assert items == []
    assert [(r["origin"], r["version_id"]) for r in report.policy_refused] == [
        ("assignment", V1), ("request", V2)
    ]
    assert all(r["violations"] == ["scripts_not_allowed"] for r in report.policy_refused)
    assert any("Ask an owner or admin of Acme" in note for note in report.notes)
    # Withholding a grant never marks a source incomplete.
    assert report.complete_sources == {"assignment": True, "request": True}
    assert report.as_dict()["policy_refused"] == report.policy_refused


def test_device_policy_is_none_on_an_older_server(tmp_path: Path) -> None:
    assert _client({}).device_policy() is None


def test_tool_report_detects_deployed_managed_settings(tmp_path: Path) -> None:
    content = b'{"allowManagedHooksOnly": true}\n'
    path = tmp_path / "managed-settings.json"
    summary = {
        "enforcement": "managed",
        "artifacts": [{
            "kind": "file",
            "content_hash": "sha256:" + hashlib.sha256(content).hexdigest(),
            "paths": {"linux": str(path)},
        }],
    }
    assert policy_reconcile.tool_report(summary, "linux")["enforced_mode"] == "advisory"
    path.write_bytes(content)
    deployed = policy_reconcile.tool_report(summary, "linux")
    assert deployed["enforced_mode"] == "system" and deployed["violations"] == []
    path.write_bytes(b"{}\n")
    outdated = policy_reconcile.tool_report(summary, "linux")
    assert outdated["enforced_mode"] == "system"
    assert outdated["violations"] == ["managed_settings_outdated"]
    assert policy_reconcile.tool_report({"enforcement": "report_only"}, "linux")["enforced_mode"] == "advisory"


def test_report_after_pull_posts_names_and_codes_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(tmp_path)
    posted: list = []
    body = {
        "items": [{
            "org_id": "org_a", "org_name": "Acme", "revision": 3,
            "tools": {
                "claude-code": {"enforcement": "managed", "artifacts": []},
                "antigravity": {"enforcement": "unsupported", "artifacts": []},
            },
        }]
    }
    client = _client(
        {
            "/api/v1/devices/me/policy": httpx.Response(200, json=body),
            "/api/v1/devices/me/policy-report": httpx.Response(
                202, json={"device_id": "dev_1", "accepted": [{"org_id": "org_a", "compliant": False}]}
            ),
        },
        posted,
    )
    report = PullReport()
    report.policy_refused.append({"origin": "assignment", "org_id": "org_a", "version_id": V1,
                                  "package_id": "skp_1", "violations": ["scripts_not_allowed"]})
    monkeypatch.setattr(policy_reconcile, "_installed_versions", lambda manager: {V1})
    sent = policy_reconcile.report_after_pull(manager, client, report, None)
    assert sent == [{"org_id": "org_a", "compliant": False}]
    [payload] = posted
    assert payload["device_id"].startswith("local-")
    # The generated id is stable across runs.
    assert policy_reconcile.local_device_id(manager.store_root) == payload["device_id"]
    [org_report] = payload["reports"]
    assert org_report["policy_revision"] == 3
    assert set(org_report["per_tool"]) == {"claude-code"}
    assert org_report["noncompliant_installs"] == [
        {"version_id": V1, "package_id": "skp_1", "violations": "scripts_not_allowed"}
    ]
    assert org_report["refused"][0]["origin"] == "assignment"


def test_no_policy_means_no_report(tmp_path: Path) -> None:
    posted: list = []
    client = _client({"/api/v1/devices/me/policy": httpx.Response(200, json={"items": []})}, posted)
    assert policy_reconcile.report_after_pull(_manager(tmp_path), client, PullReport(), "d1") == []
    assert posted == []
