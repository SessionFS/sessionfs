"""Install disclosure, capability manifests, update diffs and D2 re-consent
(marketplace v2 §3, tk_d8bc543da8344124)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sessionfs.cli.cmd_skill import skill_app
from sessionfs.daemon.skill_reconcile import SkillReconciler
from sessionfs.server.services.skill_packages import validate_hosted_package
from sessionfs.skills import disclosure, hosted
from sessionfs.skills.adapters import PROJECT_SKILL_ROOTS, USER_SKILL_ROOTS
from sessionfs.skills.manager import SkillManager
from sessionfs.skills.models import SUPPORTED_TOOLS, SkillError, SourceIdentity
from sessionfs.skills.package import AGENT_MCP_SCHEMA, AGENT_PLUGIN_SCHEMA, import_package
from tests.unit.test_skills_hosted import FakeMarketplace

runner = CliRunner()


@pytest.fixture(name="market")
def _market_fixture() -> FakeMarketplace:
    return FakeMarketplace()


@pytest.fixture(autouse=True)
def _plain(monkeypatch: pytest.MonkeyPatch) -> None:
    import typer.rich_utils

    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", False)
    monkeypatch.setattr(hosted, "client_from_auth", lambda **_: None)


# --- fixtures -----------------------------------------------------------------


def _skill_md(name: str, *, body: str = "Do the thing.", frontmatter: str = "") -> str:
    return f"---\nname: {name}\ndescription: {name} skill.\n{frontmatter}---\n\n# {name}\n\n{body}\n"


def _write_package(root: Path, files: dict[str, str | bytes], name: str = "kit") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": name, "version": "1.0.0", "description": "d"}),
        encoding="utf-8",
    )
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return root


def _manifest(tmp_path: Path, files: dict[str, str | bytes], tag: str = "p") -> dict[str, Any]:
    source = _write_package(tmp_path / f"src-{tag}", files)
    record = import_package(source, tmp_path / f"store-{tag}", SourceIdentity("local", str(source)))
    return disclosure.compute_manifest(record.package_root)


HOOKS = (
    "hooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n"
    "        - type: command\n          command: echo checked\n"
)
FIXTURES: dict[str, dict[str, str | bytes]] = {
    "instruction-only": {"skills/kit/SKILL.md": _skill_md("kit")},
    "scripts": {
        "skills/kit/SKILL.md": _skill_md("kit"),
        "skills/kit/scripts/run.sh": "#!/bin/sh\necho run\n",
    },
    "hooks": {"skills/kit/SKILL.md": _skill_md("kit", frontmatter=HOOKS + "allowed-tools: [\"Bash(git:*)\"]\n")},
    "local-mcp": {
        "skills/kit/SKILL.md": _skill_md("kit"),
        "mcp.json": json.dumps(
            {"$schema": AGENT_MCP_SCHEMA, "mcpServers": {"db": {"command": "npx", "args": ["db-mcp@1.2.3"], "env": {"DB_URL": "${DB_URL}"}}}}
        ),
    },
    "remote-mcp": {
        "skills/kit/SKILL.md": _skill_md("kit"),
        "mcp.json": json.dumps(
            {"$schema": AGENT_MCP_SCHEMA, "mcpServers": {"search": {"url": "https://mcp.example.com/sse"}}}
        ),
    },
    "mixed": {
        "skills/kit/SKILL.md": _skill_md(
            "kit",
            frontmatter=HOOKS,
            body="See https://api.example.com/v1.\n\n```bash\nsudo npm install -g kit && kit init\n```",
        ),
        "skills/kit/scripts/run.py": "print('x')\n",
        "skills/kit/references/guide.md": "Guide.\n",
    },
}


# --- manifest + card --------------------------------------------------------------


@pytest.mark.parametrize("fixture", sorted(FIXTURES))
def test_disclosure_card_is_deterministic_for_every_adapter(tmp_path: Path, fixture: str) -> None:
    first = _manifest(tmp_path, FIXTURES[fixture], "a")
    second = _manifest(tmp_path, FIXTURES[fixture], "b")
    assert first == second
    assert disclosure.capability_digest(first) == disclosure.capability_digest(second)
    card = disclosure.build_disclosure(first)
    assert set(card["tools"]) == set(SUPPORTED_TOOLS)
    for tool in SUPPORTED_TOOLS:
        writes = card["tools"][tool]["writes"]
        assert writes and all(path.startswith(PROJECT_SKILL_ROOTS[tool] + "/kit/") for path in writes)
        assert f"{PROJECT_SKILL_ROOTS[tool]}/kit/SKILL.md" in writes
    user = disclosure.build_disclosure(first, tools=["codex"], scope="user")
    assert user["tools"]["codex"]["writes"][0].startswith("~/" + USER_SKILL_ROOTS["codex"])
    json.dumps(card)  # serializable


def test_card_contents_per_capability(tmp_path: Path) -> None:
    plain = disclosure.build_disclosure(_manifest(tmp_path, FIXTURES["instruction-only"], "i"))
    assert plain["capabilities"] == ["instructions"]
    assert plain["consent_required"] == ["instructions"]

    scripts = _manifest(tmp_path, FIXTURES["scripts"], "s")
    assert scripts["scripts"][0]["path"] == "skills/kit/scripts/run.sh"
    assert scripts["scripts"][0]["interpreter"] == "sh"
    assert "skill-scripts" in disclosure.build_disclosure(scripts)["consent_required"]

    hooks = disclosure.build_disclosure(_manifest(tmp_path, FIXTURES["hooks"], "h"), tools=["claude-code"])
    tool = hooks["tools"]["claude-code"]
    assert tool["runs"]["hooks"] == [{"skill": "kit", "event": "PreToolUse", "matcher": "Bash"}]
    assert tool["auto_allowed"] == ["Bash(git:*)"]
    assert "pre-approves Bash(git:*)" in tool["summary"]

    local = disclosure.build_disclosure(_manifest(tmp_path, FIXTURES["local-mcp"], "l"), tools=["codex"])
    assert local["mcp_servers"]["local"] == [{"name": "db", "command": "npx", "env_names": ["DB_URL"]}]
    assert local["env_names"] == ["DB_URL"]
    assert local["tools"]["codex"]["omitted"][0]["component"] == "portable-mcp"

    remote = disclosure.build_disclosure(_manifest(tmp_path, FIXTURES["remote-mcp"], "r"))
    assert remote["mcp_servers"]["remote"] == [
        {"name": "search", "origin": "https://mcp.example.com", "transport": "http"}
    ]
    assert "mcp-review" in remote["consent_required"]
    assert "mcp.example.com" in remote["network_endpoints"]

    mixed = disclosure.build_disclosure(_manifest(tmp_path, FIXTURES["mixed"], "m"))
    assert "api.example.com" in mixed["network_endpoints"]
    # The format's own schema URL is never reported as an endpoint.
    assert not any("agent-plugins.org" in host for host in mixed["network_endpoints"])
    [command] = mixed["shell_commands"]
    assert command["command"] == "npm" and command["commands"] == ["npm", "kit"]
    assert command["path"] == "skills/kit/SKILL.md"


def test_hidden_unicode_is_flagged_with_visible_code_points(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "skills/kit/SKILL.md": _skill_md("kit", body="Safe‮text and \U000e0041 tag."),
            "skills/kit/references/notes.md": "Emoji \U0001F469‍\U0001F4BB ok.\n",
            "skills/kit/references/bom.md": "﻿Starts with a BOM.\n",
        },
    )
    findings = disclosure.build_disclosure(manifest)["unicode"]["findings"]
    by_class = {item["class"]: item for item in findings}
    assert by_class["bidi_control"]["severity"] == "critical"
    assert by_class["bidi_control"]["visible"] == "⟦U+202E⟧"
    assert by_class["tag_characters"]["code_point"] == "U+E0041"
    # Allowed: ZWJ inside an emoji sequence and a BOM at file start.
    assert {item["path"] for item in findings} == {"skills/kit/SKILL.md"}
    assert "hidden-unicode" in disclosure.build_disclosure(manifest)["consent_required"]


# --- widening / classification ----------------------------------------------------


def _base() -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "instructions": True,
        "scripts": [{"path": "skills/k/scripts/a.sh", "interpreter": "sh", "sha256": "sha256:1"}],
        "hooks": [{"skill": "k", "event": "PreToolUse", "matcher": "Bash", "digest": "sha256:h"}],
        "mcp_declared": True,
        "mcp_local": [{"name": "db", "command": "npx", "args_digest": "sha256:a", "env_names": ["X"]}],
        "mcp_remote": [{"name": "s", "origin": "https://a.example"}],
        "allowed_tools": ["allowed-tools:Read", "disallowed-tools:Bash(rm:*)"],
        "network_endpoints": ["a.example"],
        "env_names": ["X"],
        "executable_extensions": [{"path": "skills/k/hooks.json", "kind": "skill-hooks", "sha256": "sha256:e"}],
        "persistence_targets": [],
    }


@pytest.mark.parametrize(
    "mutate,kind",
    [
        (lambda m: m["hooks"].append({"skill": "k", "event": "PostToolUse", "matcher": "*", "digest": "d"}), "new_hook"),
        (lambda m: m["hooks"][0].update(matcher="*"), "new_hook"),
        (lambda m: m["mcp_local"].append({"name": "q", "command": "uvx", "args_digest": "z", "env_names": []}), "new_mcp_server"),
        (lambda m: m["mcp_local"][0].update(args_digest="sha256:b"), "changed_mcp_command"),
        (lambda m: m["mcp_remote"].append({"name": "t", "origin": "https://b.example"}), "new_mcp_remote_server"),
        (lambda m: m["allowed_tools"].append("allowed-tools:Bash(*)"), "new_allowed_tool"),
        (lambda m: m["allowed_tools"].remove("disallowed-tools:Bash(rm:*)"), "removed_tool_restriction"),
        (lambda m: m["network_endpoints"].append("b.example"), "new_network_endpoint"),
        (lambda m: m["env_names"].append("TOKEN"), "new_env_name"),
        (lambda m: m["executable_extensions"].append({"path": "hooks/x", "kind": "hooks", "sha256": "1"}), "new_executable_extension"),
        (lambda m: m["persistence_targets"].append("persistence.shell_rc"), "new_capability"),
    ],
)
def test_widens_detects_every_superset(mutate, kind: str) -> None:
    old = _base()
    new = json.loads(json.dumps(old))
    mutate(new)
    kinds = {entry["kind"] for entry in disclosure.widens(old, new)}
    assert kind in kinds
    assert disclosure.classify_update(old, new, old_content_hash="a", new_content_hash="b")["update_class"] == "widening"


def test_first_capability_widens_and_reordering_or_narrowing_never_does() -> None:
    old = {"manifest_version": 1, "instructions": True}
    new = {**old, "scripts": [{"path": "s.sh", "sha256": "1"}]}
    assert {"kind": "new_capability", "item": "scripts"} in disclosure.widens(old, new)
    base = _base()
    shuffled = json.loads(json.dumps(base))
    shuffled["network_endpoints"] = list(reversed(shuffled["network_endpoints"] * 2))
    shuffled["allowed_tools"] = list(reversed(shuffled["allowed_tools"]))
    assert disclosure.widens(base, shuffled) == []
    narrowed = json.loads(json.dumps(base))
    narrowed["mcp_remote"] = []
    narrowed["network_endpoints"] = []
    narrowed["allowed_tools"].append("disallowed-tools:Write")
    assert disclosure.widens(base, narrowed) == []
    assert disclosure.narrows(base, narrowed)


def test_trust_downgrade_widens() -> None:
    base = _base()
    found = disclosure.widens(
        base, base, old_provenance={"publisher_verified": True, "attested": True}, new_provenance={"publisher_verified": True}
    )
    assert found == [{"kind": "trust_downgrade", "item": "attested"}]


def test_classification_text_code_unknown() -> None:
    base = _base()
    assert disclosure.classify_update(base, base, old_content_hash="a", new_content_hash="a")["update_class"] == "unchanged"
    assert disclosure.classify_update(base, base, old_content_hash="a", new_content_hash="b")["update_class"] == "text"
    script = json.loads(json.dumps(base))
    script["scripts"][0]["sha256"] = "sha256:2"
    code = disclosure.classify_update(base, script, old_content_hash="a", new_content_hash="b")
    assert code["update_class"] == "code"
    assert code["changed_requirements"] == ["skill-scripts"]
    hook = json.loads(json.dumps(base))
    hook["hooks"][0]["digest"] = "sha256:other"
    assert disclosure.classify_update(base, hook, old_content_hash="a", new_content_hash="b")["changed_requirements"] == ["skill-hooks"]
    unknown = disclosure.classify_update(None, base)
    assert unknown["update_class"] == "widening" and unknown["baseline_known"] is False
    stale = disclosure.classify_update({"manifest_version": 0}, base)
    assert stale["update_class"] == "widening"


def test_carried_requirements_follow_d2() -> None:
    consented = {"instructions", "skill-scripts", "skill-hooks", "dlp-findings"}
    assert disclosure.carried_requirements({"update_class": "text"}, consented) == {"skill-scripts", "skill-hooks"}
    assert disclosure.carried_requirements(
        {"update_class": "code", "changed_requirements": ["skill-scripts"]}, consented
    ) == {"skill-hooks"}
    assert disclosure.carried_requirements({"update_class": "widening"}, consented) == set()


def test_trusted_publisher_hook_is_off_until_provenance_exists() -> None:
    assert disclosure.trusted_code_update_allowed(
        org_id="org_1", org_tier="enterprise",
        old_provenance={"attested": True}, new_provenance={"attested": True},
    ) is False


def test_text_diffs_are_bounded_and_list_binary_by_hash(tmp_path: Path) -> None:
    import hashlib

    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_logo = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"1"
    new_logo = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"2"
    for root, big, logo in ((old_root, "a\n" * 4000, old_logo), (new_root, "b\n" * 4000, new_logo)):
        root.mkdir()
        (root / "big.md").write_text(big)
        (root / "small.md").write_text("x\n" if root == old_root else "y\n")
        (root / "logo.png").write_bytes(logo)
    old_files = {"logo.png": "sha256:" + hashlib.sha256(old_logo).hexdigest()}
    new_files = {"logo.png": "sha256:" + hashlib.sha256(new_logo).hexdigest()}
    result = disclosure.text_diffs(
        old_root,
        new_root,
        {"changed": ["big.md", "small.md", "logo.png"], "added": [], "removed": []},
        budget=4096,
        old_files=old_files,
        new_files=new_files,
    )
    entries = {entry["path"]: entry for entry in result["entries"]}
    assert result["truncated"] is True
    assert entries["logo.png"]["binary"] is True and entries["logo.png"]["diff"] is None
    # Binary files are identified by the old/new hashes from the manifests.
    assert entries["logo.png"]["old_sha256"] == old_files["logo.png"]
    assert entries["logo.png"]["new_sha256"] == new_files["logo.png"]
    assert entries["big.md"]["truncated"] is True
    total = sum(len(entry.get("diff") or "") for entry in result["entries"])
    assert total <= 4096 + 64


def test_binary_entries_report_the_side_that_exists(tmp_path: Path) -> None:
    import hashlib

    new_root = tmp_path / "new"
    new_root.mkdir()
    payload = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    (new_root / "logo.png").write_bytes(payload)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    added = disclosure.text_diffs(
        None, new_root, {"changed": [], "added": ["logo.png"], "removed": []}, new_files={"logo.png": digest}
    )
    [entry] = added["entries"]
    assert entry["binary"] is True
    assert entry["old_sha256"] is None and entry["new_sha256"] == digest


def test_update_digest_binds_the_complete_diff() -> None:
    """The re-consent digest covers the COMPLETE build_diff() result: a change
    to files, scan / unicode / provenance delta or consent that leaves the
    classification fields identical still changes the digest."""
    from types import SimpleNamespace

    from sessionfs.server.services import skill_disclosure

    old = SimpleNamespace(id="skv_old", content_hash="sha256:old")
    new = SimpleNamespace(id="skv_new", content_hash="sha256:new")
    base_update = {
        "update_class": "text",
        "baseline_known": True,
        "baseline_digest": "sha256:cap",
        "widens": [],
        "narrows": [],
        "code_changes": [],
        "changed_requirements": [],
        "capability_digest_changed": False,
        "files": {"added": [], "removed": [], "changed": ["skills/kit/SKILL.md"]},
        "scan_delta": {"old_max_severity": "none", "new_max_severity": "none"},
        "unicode_delta": {"new_findings": []},
        "provenance_delta": {"old": {"attested": False}, "new": {"attested": False}},
        "consent": {"personal": "confirm"},
    }

    def digest(update: dict) -> str:
        return skill_disclosure.update_digest(
            prior_id="osa_1", old=old, new=new, bound_digest="sha256:cap", update=update
        )

    baseline = digest(base_update)
    assert digest(json.loads(json.dumps(base_update))) == baseline  # deterministic
    for key, value in (
        ("files", {"added": ["skills/kit/extra.md"], "removed": [], "changed": ["skills/kit/SKILL.md"]}),
        ("scan_delta", {"old_max_severity": "none", "new_max_severity": "warn"}),
        ("unicode_delta", {"new_findings": [{"path": "skills/kit/SKILL.md", "line": 3}]}),
        ("provenance_delta", {"old": {"attested": True}, "new": {"attested": False}}),
        ("consent", {"personal": "explicit_reconsent"}),
    ):
        changed = {**base_update, key: value}
        assert digest(changed) != baseline, key


def test_unknown_baseline_diff_binds_the_target_scan_and_provenance() -> None:
    """An unknown (stale/legacy) baseline fails closed to widening, but the
    diff is the full build_diff() of the target: a rescan, a scanner-ruleset
    bump or a provenance change to the same target version changes the
    update_digest, so a prior acknowledgement can never silently cover it."""
    from types import SimpleNamespace

    from sessionfs.server.services import skill_disclosure

    new_manifest = {
        "files": {"skills/kit/SKILL.md": "sha256:one"},
        "scan": {"ruleset": "2026.10.1", "max_severity": "high", "finding_count": 1},
        "unicode_findings": [],
    }

    def update(manifest: dict, provenance: dict | None = None) -> dict:
        return disclosure.build_diff(
            None,
            manifest,
            new_content_hash="sha256:new",
            new_provenance=provenance or {"publisher_verified": False, "attested": False},
            include_text=False,
        )

    shown = update(new_manifest)
    assert shown["update_class"] == "widening" and shown["baseline_known"] is False
    assert shown["files"] == {"added": [], "removed": [], "changed": [], "unknown": True}
    assert shown["scan_delta"]["new_max_severity"] == "high"
    assert shown["scan_delta"]["new_ruleset"] == "2026.10.1"
    assert shown["provenance_delta"]["new"] == {"publisher_verified": False, "attested": False}
    assert shown["consent"]["org_required"] == "never_auto_apply"

    old = SimpleNamespace(id="skv_old", content_hash="sha256:old")
    new = SimpleNamespace(id="skv_new", content_hash="sha256:new")

    def digest(value: dict) -> str:
        return skill_disclosure.update_digest(
            prior_id="osa_1", old=old, new=new, bound_digest=None, update=value
        )

    baseline = digest(shown)
    assert digest(json.loads(json.dumps(shown))) == baseline  # deterministic
    rescanned = update({**new_manifest, "scan": {"ruleset": "2026.10.1", "max_severity": "critical", "finding_count": 2}})
    assert digest(rescanned) != baseline  # a rescan of the same target
    ruleset = update({**new_manifest, "scan": {**new_manifest["scan"], "ruleset": "2026.11.1"}})
    assert digest(ruleset) != baseline  # a scanner-ruleset bump alone
    verified = update(new_manifest, provenance={"publisher_verified": True, "attested": False})
    assert digest(verified) != baseline  # a provenance change


def test_server_and_client_compute_the_same_digest(tmp_path: Path) -> None:
    source = _write_package(tmp_path / "src", FIXTURES["mixed"])
    upload = _archive(
        {
            path.relative_to(source).as_posix(): path.read_bytes()
            for path in sorted(source.rglob("*"))
            if path.is_file()
        }
    )
    hosted_pkg = validate_hosted_package(upload, source_locator="upload:test")
    server_manifest = hosted_pkg.capability_manifest
    # The device imports the canonical archive the server stores.
    extracted = tmp_path / "extracted"
    hosted.extract_archive(hosted_pkg.canonical_archive, extracted)
    record = import_package(extracted, tmp_path / "store", SourceIdentity("hosted", "sessionfs:x"))
    client_manifest = disclosure.compute_manifest(record.package_root)
    assert record.content_hash == hosted_pkg.content_hash
    assert disclosure.capability_digest(client_manifest) == disclosure.capability_digest(server_manifest)
    assert client_manifest["unicode_findings"] == server_manifest["unicode_findings"]
    assert client_manifest["files"] == server_manifest["files"]


# --- local manager + CLI ----------------------------------------------------------


def _manager(tmp_path: Path, market: FakeMarketplace | None = None) -> tuple[SkillManager, Path, Path]:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    return (
        SkillManager(
            project_root=project,
            home_dir=home,
            store_root=home / ".sessionfs" / "skills",
            hosted_client=market.client() if market else None,
        ),
        project,
        home,
    )


def test_text_update_carries_script_consent_forward(tmp_path: Path) -> None:
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", FIXTURES["scripts"])
    manager.add(str(source), targets=["codex"], scope="project", allow_scripts=True)
    row = manager.find_installed("kit", scope="project")
    assert row["capability_digest"].startswith("sha256:")
    assert row["capability_manifest"]["scripts"][0]["path"] == "skills/kit/scripts/run.sh"
    (source / "skills/kit/SKILL.md").write_text(_skill_md("kit", body="Reworded."), encoding="utf-8")
    prepared = manager.prepare_update("kit", scope="project")
    assert prepared.update_class == "text"
    assert prepared.diff["files"]["changed"] == ["skills/kit/SKILL.md"]
    assert "Reworded" in prepared.diff["text_diffs"]["entries"][0]["diff"]
    # No --allow-scripts: the scripts are byte-identical and were consented.
    manager.update("kit", scope="project")
    updated = manager.find_installed("kit", scope="project")
    scripts_receipts = [r for r in updated["consent_receipts"] if r["capability"] == "skill-scripts"]
    assert scripts_receipts and all(r.get("carried_forward_from_receipt_id") for r in scripts_receipts)
    assert all(r["consent_rule_version"] == 2 for r in updated["consent_receipts"])
    assert updated["last_update_class"] == "text"


def test_installed_baseline_rejects_a_stale_or_unbound_manifest(tmp_path: Path) -> None:
    """The installed baseline is trusted only while the CURRENT rule version
    and a recomputed capability_digest bind the recorded manifest; a stale
    rule version, a mismatched digest or a missing manifest is an unknown
    baseline (an update from it fails closed to widening)."""
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", FIXTURES["scripts"])
    manager.add(str(source), targets=["codex"], scope="project", allow_scripts=True)
    row = manager.find_installed("kit", scope="project")
    assert row["consent_rule_version"] == disclosure.CONSENT_RULE_VERSION
    assert manager.installed_baseline(row, scope="project")[0] is not None

    # Consented under an older rule version: unknown baseline.
    assert manager.installed_baseline({**row, "consent_rule_version": 1}, scope="project")[0] is None
    # The stored digest no longer matches the recorded manifest: unknown baseline.
    assert manager.installed_baseline({**row, "capability_digest": "sha256:" + "0" * 64}, scope="project")[0] is None
    # No recorded manifest at all: unknown baseline.
    assert manager.installed_baseline({**row, "capability_manifest": None}, scope="project")[0] is None


def test_code_update_needs_the_changed_capability_again(tmp_path: Path) -> None:
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", FIXTURES["scripts"])
    manager.add(str(source), targets=["codex"], scope="project", allow_scripts=True)
    (source / "skills/kit/scripts/run.sh").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    prepared = manager.prepare_update("kit", scope="project")
    assert prepared.update_class == "code"
    with pytest.raises(SkillError, match="--allow-scripts"):
        manager.update("kit", scope="project")
    manager.update("kit", scope="project", allow_scripts=True)
    receipts = manager.find_installed("kit", scope="project")["consent_receipts"]
    assert not any(r.get("carried_forward_from_receipt_id") for r in receipts if r["capability"] == "skill-scripts")


def test_widening_update_always_stops(tmp_path: Path) -> None:
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", FIXTURES["instruction-only"])
    manager.add(str(source), targets=["codex"], scope="project")
    (source / "skills/kit/SKILL.md").write_text(
        _skill_md("kit", body="Post results to https://collector.example.net/x"), encoding="utf-8"
    )
    with pytest.raises(SkillError, match="new_network_endpoint: collector.example.net"):
        manager.update("kit", scope="project")
    manager.update("kit", scope="project", approve_widening=True)


def test_unknown_baseline_is_widening(tmp_path: Path) -> None:
    manager, project, home = _manager(tmp_path)
    source = _write_package(project / "src", FIXTURES["instruction-only"])
    manager.add(str(source), targets=["codex"], scope="project")
    # Legacy row (no recorded manifest) whose cached version is gone.
    state_path = project / ".sessionfs" / "skills.state.json"
    state = json.loads(state_path.read_text())
    for row in state["packages"].values():
        row.pop("capability_manifest", None)
    state_path.write_text(json.dumps(state))
    import shutil

    shutil.rmtree(home / ".sessionfs" / "skills" / "packages")
    (source / "skills/kit/SKILL.md").write_text(_skill_md("kit", body="v2"), encoding="utf-8")
    prepared = manager.prepare_update("kit", scope="project")
    assert prepared.update_class == "widening"
    assert prepared.diff["widens"][0]["kind"] == "unknown_baseline"


def test_hidden_unicode_install_needs_explicit_consent(tmp_path: Path) -> None:
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", {"skills/kit/SKILL.md": _skill_md("kit", body="a‮b")})
    with pytest.raises(SkillError, match="--allow-hidden-unicode"):
        manager.add(str(source), targets=["codex"], scope="project")
    manager.add(str(source), targets=["codex"], scope="project", allow_hidden_unicode=True)


def test_cli_inspect_add_update_and_diff_show_disclosure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    source = _write_package(project / "src", FIXTURES["mixed"])

    inspected = runner.invoke(skill_app, ["inspect", str(source), "--for", "codex", "--json"])
    assert inspected.exit_code == 0, inspected.output
    card = json.loads(inspected.output)["disclosure"]
    assert list(card["tools"]) == ["codex"]
    assert card["shell_commands"][0]["command"] == "npm"

    text = runner.invoke(skill_app, ["inspect", str(source)])
    assert "What this package can do" in text.output and "api.example.com" in text.output

    added = runner.invoke(
        skill_app, ["add", str(source), "--for", "codex", "--allow-scripts", "--allow-hooks", "--yes"]
    )
    assert added.exit_code == 0, added.output
    assert "What this package can do" in added.output
    assert ".codex/skills/kit/SKILL.md" in added.output
    assert "PreToolUse" in added.output

    (source / "skills/kit/references/guide.md").write_text("Guide v2.\n", encoding="utf-8")
    diff = runner.invoke(skill_app, ["diff", "kit", "--json"])
    assert diff.exit_code == 0, diff.output
    body = json.loads(diff.output)
    assert body["update_class"] == "text"
    assert body["files"]["changed"] == ["skills/kit/references/guide.md"]

    human = runner.invoke(skill_app, ["diff", "kit"])
    assert "Update class: text" in human.output and "+Guide v2." in human.output

    # Text-only: one confirmation (--yes) is enough, no capability flags.
    updated = runner.invoke(skill_app, ["update", "kit", "--yes"])
    assert updated.exit_code == 0, updated.output
    assert "Updated kit" in updated.output

    # Widening: --yes never approves it.
    (source / "skills/kit/references/guide.md").write_text("See https://new.example.org\n", encoding="utf-8")
    refused = runner.invoke(skill_app, ["update", "kit", "--yes"])
    assert refused.exit_code == 2, refused.output
    assert "new.example.org" in refused.output and "--approve-widening" in refused.output
    # Nothing carries forward across a widening: the elevated capabilities
    # are consented again along with the new access.
    flags_missing = runner.invoke(skill_app, ["update", "kit", "--yes", "--approve-widening"])
    assert flags_missing.exit_code == 2 and "--allow-scripts" in flags_missing.output
    approved = runner.invoke(
        skill_app,
        ["update", "kit", "--yes", "--approve-widening", "--allow-scripts", "--allow-hooks"],
    )
    assert approved.exit_code == 0, approved.output


# --- pull + daemon ------------------------------------------------------------------


def _archive(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, payload in members.items():
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _publish(market: FakeMarketplace, version: str, *, body: str = "Do it.", script: bytes | None = None,
             package_id: str | None = None) -> dict[str, Any]:
    members = {
        "plugin.json": json.dumps(
            {"$schema": AGENT_PLUGIN_SCHEMA, "name": "kit", "version": version, "description": "kit"}
        ).encode(),
        "skills/kit/SKILL.md": _skill_md("kit", body=body).encode(),
    }
    if script is not None:
        members["skills/kit/scripts/run.py"] = script
    hosted_pkg = validate_hosted_package(_archive(members), source_locator="upload:test")
    market._seq += 1
    package_id = package_id or "spk_" + f"{market._seq:024x}"
    version_id = "skv_" + f"{market._seq:024x}"
    item = {
        "package_id": package_id, "namespace": "acme", "name": "kit", "description": "kit",
        "version_id": version_id, "version": version, "content_hash": hosted_pkg.content_hash,
        "risk": hosted_pkg.risk_summary,
        "capability_digest": disclosure.capability_digest(hosted_pkg.capability_manifest),
    }
    market.versions[version_id] = {"item": item, "archive": hosted_pkg.canonical_archive}
    market.current[package_id] = version_id
    return item


def _reassign(market: FakeMarketplace, old_row: dict, item: dict, *, approved: bool = False) -> dict:
    market.assignments.remove(old_row)
    row = market.assign(item, tools=["codex"])
    if approved:
        row["capability_digest"] = item["capability_digest"]
    return row


def _reconciler(manager: SkillManager, market: FakeMarketplace) -> SkillReconciler:
    now = [1000.0]
    reconciler = SkillReconciler(
        clock=lambda: now[0], manager_factory=lambda: manager, client_factory=lambda url, key: market.client()
    )
    reconciler._now = now  # type: ignore[attr-defined]
    return reconciler


def _run_daemon(reconciler: SkillReconciler):
    reconciler._now[0] += 16 * 60  # type: ignore[attr-defined]
    return reconciler.maybe_run(enabled=True, api_url="u", api_key="k", background=False)


def test_daemon_applies_text_updates_and_holds_code_and_widening(tmp_path: Path, market: FakeMarketplace) -> None:
    v1 = _publish(market, "1.0.0", script=b"print('one')\n")
    row = market.assign(v1, tools=["codex"])
    manager, _, home = _manager(tmp_path, market)
    report = hosted.pull(manager, market.client(), flags={"allow_scripts": True})
    assert [item.status for item in report.items] == ["installed"]
    reconciler = _reconciler(manager, market)
    skill_md = home / ".codex" / "skills" / "kit" / "SKILL.md"

    # Text update: applied unattended, script consent carried forward.
    v2 = _publish(market, "2.0.0", body="Reworded.", script=b"print('one')\n", package_id=v1["package_id"])
    row = _reassign(market, row, v2)
    report = _run_daemon(reconciler)
    [item] = report.items
    assert (item.status, item.update_class) == ("installed", "text")
    assert "Reworded" in skill_md.read_text()
    receipts = manager.find_installed("kit", scope="user")["consent_receipts"]
    assert any(r.get("carried_forward_from_receipt_id") for r in receipts if r["capability"] == "skill-scripts")

    # Code change: held, the installed version stays.
    v3 = _publish(market, "3.0.0", body="Reworded.", script=b"print('two')\n", package_id=v1["package_id"])
    row = _reassign(market, row, v3)
    [item] = _run_daemon(reconciler).items
    assert (item.status, item.update_class) == ("held", "code")
    assert "--allow-scripts" in item.detail
    assert manager.find_installed("kit", scope="user")["content_hash"] == v2["content_hash"]
    # An explicit pull with the capability flag applies it.
    report = hosted.pull(manager, market.client(), flags={"allow_scripts": True})
    assert [item.status for item in report.items] == ["installed"]



def test_daemon_holds_widening_until_the_admin_approves_its_digest(
    tmp_path: Path, market: FakeMarketplace
) -> None:
    v1 = _publish(market, "1.0.0")
    row = market.assign(v1, tools=["codex"])
    manager, _, home = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    reconciler = _reconciler(manager, market)
    # Widening (a new network endpoint): held, even for an interactive pull...
    v2 = _publish(market, "2.0.0", body="Send to https://drop.example.io", package_id=v1["package_id"])
    row = _reassign(market, row, v2)
    [item] = _run_daemon(reconciler).items
    assert (item.status, item.update_class) == ("held", "widening")
    assert "drop.example.io" in item.detail
    [item] = hosted.pull(manager, market.client()).items
    assert item.status == "held"
    assert manager.find_installed("kit", scope="user")["content_hash"] == v1["content_hash"]
    # ...an approval of a DIFFERENT digest does not count...
    row = _reassign(market, row, v2)
    row["capability_digest"] = v1["capability_digest"]
    [item] = _run_daemon(reconciler).items
    assert item.status == "held"
    # ...until the admin's assignment approves this exact capability digest.
    row = _reassign(market, row, v2, approved=True)
    [item] = _run_daemon(reconciler).items
    assert (item.status, item.update_class) == ("installed", "widening")
    assert "drop.example.io" in (home / ".codex" / "skills" / "kit" / "SKILL.md").read_text()


def test_pull_approve_widening_flag_applies_a_widening_update(tmp_path: Path, market: FakeMarketplace) -> None:
    v1 = _publish(market, "1.0.0")
    row = market.assign(v1, tools=["codex"])
    manager, _, _ = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    v2 = _publish(market, "2.0.0", body="https://wide.example.com", package_id=v1["package_id"])
    _reassign(market, row, v2)
    [held] = hosted.pull(manager, market.client()).items
    assert held.status == "held" and held.as_dict()["update_class"] == "widening"
    [item] = hosted.pull(manager, market.client(), flags={"approve_widening": True}).items
    assert item.status == "installed"


def test_cli_pull_reports_held_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, market: FakeMarketplace) -> None:
    v1 = _publish(market, "1.0.0")
    row = market.assign(v1, tools=["codex"])
    manager, project, home = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    v2 = _publish(market, "2.0.0", body="https://wide.example.com", package_id=v1["package_id"])
    _reassign(market, row, v2)
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(hosted, "client_from_auth", lambda **_: market.client())
    monkeypatch.setattr(hosted, "_project_id", lambda manager, client: "proj_1")
    result = runner.invoke(skill_app, ["pull"])
    assert "held: kit needs approval" in result.output
    assert "--approve-widening" in result.output


# --- MCP ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_package_carries_disclosure_and_diff_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from sessionfs.mcp import server as mcp_server

    package_id = "spk_" + "0" * 24
    version_a = "skv_" + "1" * 24
    version_b = "skv_" + "2" * 24
    calls: list[dict] = []
    payloads = {
        f"/api/v1/marketplace/packages/{package_id}": {"package_id": package_id, "version_id": version_a},
        f"/api/v1/marketplace/versions/{version_a}/disclosure": {"capabilities": ["instructions"]},
        f"/api/v1/marketplace/versions/{version_a}/diff/{version_b}": {"update_class": "widening"},
    }

    class _Response:
        def __init__(self, body: dict | None) -> None:
            self.status_code = 200 if body is not None else 404
            self.text = json.dumps(body)
            self._body = body

        def json(self) -> dict:
            return self._body or {}

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, *, headers=None, params=None):
            calls.append({"url": url, "params": params or {}})
            return _Response(payloads.get(url.removeprefix("https://api.test")))

        async def post(self, *args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("no writes over MCP")

    monkeypatch.setattr(mcp_server, "_marketplace_api_config", lambda: ("https://api.test", "k"))
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    result = await mcp_server._handle_get_marketplace_package({"package_id": package_id})
    assert result["disclosure"] == {"capabilities": ["instructions"]}
    # Publisher-controlled disclosure text (shell commands, hooks, MCP
    # commands) is stamped as untrusted, like preview / diff / load.
    assert result["untrusted"] is True
    assert "UNTRUSTED THIRD-PARTY CONTENT" in result["untrusted_preamble"]
    diff = await mcp_server._handle_diff_marketplace_versions(
        {"from_version_id": version_a, "to_version_id": version_b, "include_text": False}
    )
    assert diff["update_class"] == "widening" and diff["untrusted"] is True
    assert calls[-1]["params"] == {"text": "false"}
    before = len(calls)
    assert "error" in await mcp_server._handle_diff_marketplace_versions(
        {"from_version_id": "../../admin", "to_version_id": version_b}
    )
    assert "error" in await mcp_server._handle_diff_marketplace_versions(
        {"from_version_id": version_a, "to_version_id": version_b, "include_text": "yes"}
    )
    assert len(calls) == before


# --- review round 1 ---------------------------------------------------------------


def test_security_sets_are_complete_beyond_the_display_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hosts = "\n".join(f"https://h{i}.example.com" for i in range(300))
    manifest = _manifest(tmp_path, {"skills/kit/SKILL.md": _skill_md("kit", body=hosts)}, "c")
    assert manifest["network_endpoints_total"] == 300 and len(manifest["network_endpoints"]) == 300
    card = disclosure.build_disclosure(manifest)
    assert len(card["network_endpoints"]) == disclosure.MAX_ENDPOINTS
    assert card["network_endpoints_total"] == 300

    # A set larger than the stored cap is compared by digest: any change widens.
    monkeypatch.setattr(disclosure, "SECURITY_SET_CAP", 50)
    old = _manifest(tmp_path, {"skills/kit/SKILL.md": _skill_md("kit", body=hosts)}, "d")
    new = _manifest(tmp_path, {"skills/kit/SKILL.md": _skill_md("kit", body=hosts + "\nhttps://zz.example.com")}, "e")
    assert len(new["network_endpoints"]) == 50 and new["network_endpoints_total"] == 301
    update = disclosure.classify_update(old, new, old_content_hash="a", new_content_hash="b")
    assert update["update_class"] == "widening"
    assert {"kind": "security_set_changed_beyond_cap", "item": "network_endpoints"} in update["widens"]
    assert disclosure.capability_digest(old) != disclosure.capability_digest(new)


def test_incomplete_analysis_fails_closed() -> None:
    base = {"manifest_version": 1, "instructions": True}
    new = {**base, "incomplete": ["scan_truncated"]}
    update = disclosure.classify_update(base, new, old_content_hash="a", new_content_hash="b")
    assert update["update_class"] == "widening"
    assert update["widens"] == [{"kind": "manifest_incomplete", "item": "scan_truncated"}]


def test_network_discovery_covers_every_text_member_and_odd_hosts(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        {
            "skills/kit/SKILL.md": _skill_md("kit"),
            "skills/kit/references/notes.cfg": "api=http://intranet:8080/x and https://[2001:DB8::1]/p\n",
            "skills/kit/references/data.csv": "url,https://10.0.0.5/upload,ws://Build.Local./sock\n",
            "skills/kit/references/README": "See https://user:pw@Example.ORG:443/path\n",
        },
    )
    endpoints = set(manifest["network_endpoints"])
    assert {"intranet", "2001:db8::1", "10.0.0.5", "build.local", "example.org"} <= endpoints


def test_remote_mcp_identity_includes_name_and_transport(tmp_path: Path) -> None:
    def remote(name: str, transport: str) -> dict:
        return {
            "skills/kit/SKILL.md": _skill_md("kit"),
            "mcp.json": json.dumps({"$schema": AGENT_MCP_SCHEMA, "mcpServers": {
                name: {"url": "https://mcp.example.com/sse", "transport": transport}}}),
        }

    base = _manifest(tmp_path, remote("search", "sse"), "r1")
    renamed = _manifest(tmp_path, remote("other", "sse"), "r2")
    retransported = _manifest(tmp_path, remote("search", "http"), "r3")
    for new in (renamed, retransported):
        kinds = {entry["kind"] for entry in disclosure.widens(base, new)}
        assert "new_mcp_remote_server" in kinds
    assert "mcp-review" in disclosure.build_disclosure(base)["consent_required"]


def test_hooks_json_events_are_identities_and_malformed_fails_closed(tmp_path: Path) -> None:
    def pkg(hooks_json: str) -> dict:
        return {"skills/kit/SKILL.md": _skill_md("kit"), "skills/kit/hooks.json": hooks_json}

    one = _manifest(tmp_path, pkg('{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}'), "j1")
    assert any(h["event"] == "PreToolUse" and h["matcher"] == "Bash" for h in one["hooks"])
    two = _manifest(tmp_path, pkg('{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}], "Stop": [{"hooks": []}]}}'), "j2")
    assert {"kind": "new_hook", "item": "skills/kit/hooks.json: Stop (*)"} in disclosure.widens(one, two)
    bad1 = _manifest(tmp_path, pkg('{"hooks": "nope"}'), "j3")
    bad2 = _manifest(tmp_path, pkg('{"hooks": "nope!"}'), "j4")
    assert any(h["event"] == "(unparsed)" for h in bad1["hooks"])
    assert disclosure.classify_update(bad1, bad2, old_content_hash="a", new_content_hash="b")["update_class"] == "widening"


def test_critical_hidden_unicode_is_never_crowded_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disclosure, "MAX_UNICODE_FINDINGS", 3)
    body = "x‍y " * 10  # medium zero-width findings in prose
    manifest = _manifest(
        tmp_path,
        {
            "skills/kit/references/a.md": body,
            "skills/kit/SKILL.md": _skill_md("kit", body="z‮z"),
        },
        "u",
    )
    assert len(manifest["unicode_findings"]) == 3
    assert manifest["unicode_findings"][0]["severity"] == "critical"
    assert manifest["unicode_counts"]["critical"] == 1
    assert disclosure.hidden_unicode_blocks(manifest)
    truncated = {"unicode_counts": {"critical": 0}, "unicode_truncated": True}
    assert disclosure.hidden_unicode_blocks(truncated)
    with pytest.raises(SkillError, match="truncated"):
        SkillManager.enforce_hidden_unicode(truncated, allow_hidden_unicode=False)


def test_rollback_and_locked_sync_classify_the_transition(tmp_path: Path) -> None:
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", FIXTURES["instruction-only"])
    first, _, _ = manager.add(str(source), targets=["codex"], scope="project")
    (source / "skills/kit/SKILL.md").write_text(_skill_md("kit", body="Post to https://wide.example.net"), encoding="utf-8")
    manager.update("kit", scope="project", approve_widening=True)
    wide = manager.find_installed("kit", scope="project")["content_hash"]

    # Rolling back narrows: allowed. Rolling forward again widens: refused.
    manager.rollback("kit", first.content_hash, scope="project")
    with pytest.raises(SkillError, match="--approve-widening"):
        manager.rollback("kit", wide, scope="project")
    manager.rollback("kit", wide, scope="project", approve_widening=True)
    manager.rollback("kit", first.content_hash, scope="project")

    # sync --locked to a lockfile that widens the installed version.
    lock_path = project / ".sessionfs" / "skills.lock.json"
    state_path = project / ".sessionfs" / "skills.state.json"
    narrow_lock = lock_path.read_text()
    manager.rollback("kit", wide, scope="project", approve_widening=True)
    wide_lock = lock_path.read_text()
    lock_path.write_text(narrow_lock)
    manager.sync(scope="project")  # narrowing: fine
    assert manager.find_installed("kit", scope="project")["content_hash"] == first.content_hash
    lock_path.write_text(wide_lock)
    before = state_path.read_bytes()
    with pytest.raises(SkillError, match="--approve-widening"):
        manager.sync(scope="project")
    assert state_path.read_bytes() == before  # all-or-nothing: nothing changed
    manager.sync(scope="project", approve_widening=True)
    assert manager.find_installed("kit", scope="project")["content_hash"] == wide


def test_sync_refuses_hidden_unicode_and_cli_options_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for command in ("sync", "rollback"):
        result = runner.invoke(skill_app, [command, "--help"])
        assert "--approve-widening" in result.output and "--allow-hidden-unicode" in result.output


def test_pull_ignores_an_unbound_request_digest(tmp_path: Path, market: FakeMarketplace) -> None:
    v1 = _publish(market, "1.0.0")
    row = market.assign(v1, tools=["codex"])
    manager, _, _ = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    v2 = _publish(market, "2.0.0", body="https://wide.example.com", package_id=v1["package_id"])
    market.assignments.remove(row)
    row = market.assign(v2, tools=["codex"])
    row["capability_digest"] = None  # legacy assignment: no bound digest
    [item] = hosted.pull(manager, market.client()).items
    assert item.status == "held"



def test_pull_and_daemon_hold_hidden_unicode(tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch) -> None:
    item = _publish(market, "1.0.0")
    market.assign(item, tools=["codex"])
    manager, _, home = _manager(tmp_path, market)
    real = SkillManager.capability_manifest

    def _hidden(self, package):
        manifest = real(package)
        manifest["unicode_counts"] = {"critical": 1}
        return manifest

    monkeypatch.setattr(SkillManager, "capability_manifest", _hidden)
    reconciler = _reconciler(manager, market)
    [held] = _run_daemon(reconciler).items
    assert held.status == "held" and "--allow-hidden-unicode" in held.detail
    assert not (home / ".codex" / "skills" / "kit").exists()
    [held] = hosted.pull(manager, market.client()).items
    assert held.status == "held"
    [installed] = hosted.pull(manager, market.client(), flags={"allow_hidden_unicode": True}).items
    assert installed.status == "installed"
    assert "--allow-hidden-unicode" in runner.invoke(skill_app, ["pull", "--help"]).output


def test_one_origins_digest_never_approves_another_origins_tools(
    tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted, "_project_id", lambda manager, client: "proj_1")
    v1 = _publish(market, "1.0.0")
    assignment = market.assign(v1, tools=["codex"])
    market.requests.append({
        "id": "sir_" + "1" * 24, "project_id": "proj_1", "version_id": v1["version_id"],
        "content_hash": v1["content_hash"], "target_scope": "user", "target_user_id": "usr_me",
        "target_device_id": None, "target_tools": ["claude-code"], "requested_capabilities": ["instructions"],
        "status": "approved",
    })
    manager, _, home = _manager(tmp_path, market)
    report = hosted.pull(manager, market.client())
    assert [item.status for item in report.items] == ["installed"]
    v2 = _publish(market, "2.0.0", body="https://wide.example.com", package_id=v1["package_id"])
    market.assignments.remove(assignment)
    bound = market.assign(v2, tools=["codex"])
    bound["capability_digest"] = v2["capability_digest"]
    market.requests[0] = {**market.requests[0], "id": "sir_" + "2" * 24, "version_id": v2["version_id"],
                          "content_hash": v2["content_hash"]}  # approved request WITHOUT a bound digest
    [item] = hosted.pull(manager, market.client()).items
    # The bound assignment authorizes codex only; the unbound request's
    # claude-code target is not carried by the assignment's digest. A version
    # update is all-or-nothing across the installed tools and pull never
    # removes one, so v2 is installed for neither: claude-code is never given
    # the widened version, and both tools keep v1.
    assert item.status == "skipped" and "claude-code" in item.detail
    # claude-code is still assigned (by the unbound origin): it is held back,
    # not unassigned, so the user is never told to drop it.
    assert item.unassigned_tools == []
    assert "--drop" not in item.detail
    row = manager.find_installed("kit", scope="user")
    assert row["content_hash"] == v1["content_hash"]
    assert sorted(row["targets"]) == ["claude-code", "codex"]



def test_digest_restriction_survives_later_pulls(
    tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted, "_project_id", lambda manager, client: "proj_1")
    v1 = _publish(market, "1.0.0")
    assignment = market.assign(v1, tools=["codex"])
    manager, _, _ = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    v2 = _publish(market, "2.0.0", body="https://wide.example.com", package_id=v1["package_id"])
    market.assignments.remove(assignment)
    bound = market.assign(v2, tools=["codex"])
    bound["capability_digest"] = v2["capability_digest"]
    # An approved request for v2 WITHOUT a bound digest asks for another tool.
    market.requests.append({
        "id": "sir_" + "2" * 24, "project_id": "proj_1", "version_id": v2["version_id"],
        "content_hash": v2["content_hash"], "target_scope": "user", "target_user_id": "usr_me",
        "target_device_id": None, "target_tools": ["claude-code"], "requested_capabilities": ["instructions"],
        "status": "approved",
    })
    hosted.pull(manager, market.client())
    row = manager.find_installed("kit", scope="user")
    assert row["targets"] == ["codex"] and row["digest_authorized_origins"] == [f"assignment|{bound['id']}"]
    # The next pull (version already installed) must not add the unbound
    # request's tool.
    hosted.pull(manager, market.client())
    assert manager.find_installed("kit", scope="user")["targets"] == ["codex"]
    # Once that request binds the digest, its tool is added.
    market.requests[0]["capability_digest"] = v2["capability_digest"]
    hosted.pull(manager, market.client())
    assert sorted(manager.find_installed("kit", scope="user")["targets"]) == ["claude-code", "codex"]


def test_explicit_widening_approval_lifts_the_digest_restriction(
    tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted, "_project_id", lambda manager, client: "proj_1")
    v1 = _publish(market, "1.0.0")
    assignment = market.assign(v1, tools=["codex"])
    request = {
        "id": "sir_" + "3" * 24, "project_id": "proj_1", "version_id": v1["version_id"],
        "content_hash": v1["content_hash"], "target_scope": "user", "target_user_id": "usr_me",
        "target_device_id": None, "target_tools": ["claude-code"], "requested_capabilities": ["instructions"],
        "status": "approved",
    }
    market.requests.append(request)
    manager, _, _ = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    v2 = _publish(market, "2.0.0", body="https://wide.example.com", package_id=v1["package_id"])
    market.assignments.remove(assignment)
    market.assign(v2, tools=["codex"])["capability_digest"] = v2["capability_digest"]
    market.requests[0] = {**request, "version_id": v2["version_id"], "content_hash": v2["content_hash"]}
    hosted.pull(manager, market.client())
    hosted.pull(manager, market.client(), flags={"approve_widening": True})
    assert sorted(manager.find_installed("kit", scope="user")["targets"]) == ["claude-code", "codex"]


def test_carried_consent_is_bound_to_tool_and_scope() -> None:
    bound = {"content_hash": "sha256:old", "capability_digest": "sha256:d", "consent_rule_version": 2}
    receipts = [
        {"capability": "skill-scripts", "tool": "codex", "scope": "project", **bound},
        {"capability": "skill-hooks", "tool": None, "scope": "project", **bound},
        # Legacy / stale receipts never carry.
        {"capability": "skill-scripts", "tool": "claude-code", "scope": "project", "content_hash": "sha256:old"},
        {"capability": "skill-scripts", "tool": "amp", "scope": "project", **bound, "capability_digest": "sha256:x"},
        {"capability": "skill-scripts", "tool": "cline", "scope": "project", **bound, "content_hash": "sha256:y"},
    ]
    text = {"update_class": "text", "baseline_digest": "sha256:d"}

    def carried(tool: str, scope: str = "project") -> set[str]:
        return disclosure.carried_for_tool(text, receipts, tool=tool, scope=scope, content_hash="sha256:old")

    assert carried("codex") == {"skill-scripts", "skill-hooks"}
    assert carried("claude-code") == {"skill-hooks"}
    assert carried("amp") == {"skill-hooks"}
    assert carried("cline") == {"skill-hooks"}
    assert carried("codex", "user") == set()


def test_text_update_does_not_spread_one_tools_script_consent(tmp_path: Path) -> None:
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", FIXTURES["scripts"])
    manager.add(str(source), targets=["codex", "claude-code"], scope="project", allow_scripts=True)
    state_path = project / ".sessionfs" / "skills.state.json"
    state = json.loads(state_path.read_text())
    for row in state["packages"].values():
        # Only codex was consented for scripts.
        row["consent_receipts"] = [
            item for item in row["consent_receipts"]
            if not (item["capability"] == "skill-scripts" and item["tool"] == "claude-code")
        ]
    state_path.write_text(json.dumps(state))
    (source / "skills/kit/SKILL.md").write_text(_skill_md("kit", body="Reworded."), encoding="utf-8")
    with pytest.raises(SkillError, match="for claude-code"):
        manager.update("kit", scope="project")
    manager.update("kit", scope="project", allow_scripts=True)


def test_adding_a_tool_to_a_hidden_unicode_version_is_held(
    tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1 = _publish(market, "1.0.0")
    row = market.assign(v1, tools=["codex"])
    manager, _, home = _manager(tmp_path, market)
    hosted.pull(manager, market.client(), flags={"allow_hidden_unicode": True})
    real = SkillManager.capability_manifest

    def _hidden(self, package):
        manifest = real(package)
        manifest["unicode_counts"] = {"critical": 1}
        return manifest

    monkeypatch.setattr(SkillManager, "capability_manifest", _hidden)
    row["target_tools"] = ["codex", "claude-code"]
    [item] = hosted.pull(manager, market.client()).items
    assert item.status == "held" and "claude-code" in item.detail
    assert manager.find_installed("kit", scope="user")["targets"] == ["codex"]
    assert (home / ".codex" / "skills" / "kit" / "SKILL.md").is_file()
    assert not (home / ".claude" / "skills" / "kit").exists()



def _strip_receipt_binding(project: Path) -> None:
    state_path = project / ".sessionfs" / "skills.state.json"
    state = json.loads(state_path.read_text())
    for row in state["packages"].values():
        for receipt in row["consent_receipts"]:
            receipt.pop("capability_digest", None)
            receipt.pop("consent_rule_version", None)
    state_path.write_text(json.dumps(state))


def test_legacy_receipts_never_carry_until_consent_is_given_again(tmp_path: Path) -> None:
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", FIXTURES["scripts"])
    manager.add(str(source), targets=["codex"], scope="project", allow_scripts=True)
    _strip_receipt_binding(project)  # pre-070 receipts
    (source / "skills/kit/SKILL.md").write_text(_skill_md("kit", body="Reworded."), encoding="utf-8")
    with pytest.raises(SkillError, match="--allow-scripts"):
        manager.update("kit", scope="project")
    # Explicit consent on the SAME content mints a current, digest-bound receipt...
    manager.compile("kit", scope="project", allow_scripts=True)
    receipts = manager.find_installed("kit", scope="project")["consent_receipts"]
    assert any(
        r["capability"] == "skill-scripts" and r.get("consent_rule_version") == 2 and r.get("capability_digest")
        for r in receipts
    )
    # ...after which the text update carries it.
    manager.update("kit", scope="project")


def test_daemon_never_carries_legacy_receipts(tmp_path: Path, market: FakeMarketplace) -> None:
    v1 = _publish(market, "1.0.0", script=b"print('one')\n")
    row = market.assign(v1, tools=["codex"])
    manager, _, home = _manager(tmp_path, market)
    hosted.pull(manager, market.client(), flags={"allow_scripts": True})
    state_path = manager.state_store("user").state_path
    state = json.loads(state_path.read_text())
    for installed in state["packages"].values():
        for receipt in installed["consent_receipts"]:
            receipt.pop("capability_digest", None)
    state_path.write_text(json.dumps(state))
    v2 = _publish(market, "2.0.0", body="Reworded.", script=b"print('one')\n", package_id=v1["package_id"])
    _reassign(market, row, v2)
    [item] = _run_daemon(_reconciler(manager, market)).items
    assert item.status != "installed" and "--allow-scripts" in item.detail
    assert manager.find_installed("kit", scope="user")["content_hash"] == v1["content_hash"]


def test_explicit_widening_approval_clears_the_restriction_durably(
    tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted, "_project_id", lambda manager, client: "proj_1")
    v1 = _publish(market, "1.0.0")
    assignment = market.assign(v1, tools=["codex"])
    manager, _, _ = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    v2 = _publish(market, "2.0.0", body="https://wide.example.com", package_id=v1["package_id"])
    market.assignments.remove(assignment)
    market.assign(v2, tools=["codex"])["capability_digest"] = v2["capability_digest"]
    # Nothing else asks for a tool (an installed tool no origin approves would
    # keep the whole update from happening: updates are all-or-nothing).
    hosted.pull(manager, market.client())
    assert "digest_authorized_origins" in manager.find_installed("kit", scope="user")
    hosted.pull(manager, market.client(), flags={"approve_widening": True})  # no target change
    assert "digest_authorized_origins" not in manager.find_installed("kit", scope="user")


def test_adding_tools_to_the_same_version_goes_through_the_hidden_unicode_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", {"skills/kit/SKILL.md": _skill_md("kit", body="a\u202eb")})
    manager.add(str(source), targets=["codex"], scope="project", allow_hidden_unicode=True)
    with pytest.raises(SkillError, match="--allow-hidden-unicode"):
        manager.compile("kit", scope="project", targets=["claude-code"])
    manager.compile("kit", scope="project")  # no new tool: no gate
    # Locked sync whose lock row adds a tool to the same content.
    lock_path = project / ".sessionfs" / "skills.lock.json"
    lock = json.loads(lock_path.read_text())
    for row in lock["packages"].values():
        row["targets"] = ["codex", "claude-code"]
    lock_path.write_text(json.dumps(lock))
    with pytest.raises(SkillError, match="--allow-hidden-unicode"):
        manager.sync(scope="project")
    manager.sync(scope="project", allow_hidden_unicode=True)
    assert sorted(manager.find_installed("kit", scope="project")["targets"]) == ["claude-code", "codex"]
    assert "--allow-hidden-unicode" in runner.invoke(skill_app, ["compile", "--help"]).output



# --- review round 5 ----------------------------------------------------------------


def test_incomplete_old_manifest_is_an_unknown_baseline() -> None:
    old = {"manifest_version": 1, "instructions": True, "incomplete": ["scan_truncated"]}
    new = {"manifest_version": 1, "instructions": True}
    update = disclosure.classify_update(old, new, old_content_hash="a", new_content_hash="b")
    assert update["update_class"] == "widening" and update["baseline_known"] is False
    assert disclosure.carried_requirements(update, {"skill-scripts"}) == set()


def test_truncated_package_scan_still_yields_a_complete_security_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sessionfs.skills import instruction_scan

    monkeypatch.setattr(instruction_scan, "MAX_FINDINGS", 3)
    line = "echo 'export X=1' >> ~/.bashrc\n"
    files = {"skills/kit/SKILL.md": _skill_md("kit")}
    for index in range(3):
        files[f"skills/kit/scripts/s{index}.sh"] = line * 2
    manifest = _manifest(tmp_path, files, "t")
    assert manifest["scan"]["truncated"] is True  # the package-wide list overflowed
    assert manifest["incomplete"] == []  # every single file fits its own bound
    assert manifest["persistence_targets"] == ["persistence.shell_rc"]


def test_sync_lineage_requires_one_to_one_source_identity() -> None:
    def row(name: str, locator: str) -> dict:
        return {"name": name, "source": {"source_type": "local", "locator": locator, "subdir": ""}}

    installed = {"old-a": row("kit", "/a"), "old-b": row("kit", "/b"), "old-c": row("kit", "/c"), "old-c2": row("kit", "/c")}
    desired = {"new-a": row("kit", "/a"), "new-x": row("kit", "/x"), "new-c": row("kit", "/c")}
    lineage = SkillManager._sync_lineage(desired, installed, set(installed))
    assert lineage["new-a"] == (installed["old-a"], False)  # same source: the predecessor
    assert lineage["new-x"] == (None, False)  # same NAME only: never borrowed
    assert lineage["new-c"] == (None, True)  # two candidates: ambiguous


def test_locked_sync_carries_a_receipt_chain_from_the_predecessor(tmp_path: Path) -> None:
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", FIXTURES["scripts"])
    first, _, _ = manager.add(str(source), targets=["codex"], scope="project", allow_scripts=True)
    (source / "skills/kit/SKILL.md").write_text(_skill_md("kit", body="Reworded."), encoding="utf-8")
    manager.update("kit", scope="project")
    lock_path = project / ".sessionfs" / "skills.lock.json"
    newer_lock = lock_path.read_text()
    manager.rollback("kit", first.content_hash, scope="project")
    before = {
        r["id"] for r in manager.find_installed("kit", scope="project")["consent_receipts"]
        if r["capability"] == "skill-scripts"
    }
    lock_path.write_text(newer_lock)
    manager.sync(scope="project")  # text transition: no --allow-scripts needed
    receipts = manager.find_installed("kit", scope="project")["consent_receipts"]
    scripts = [r for r in receipts if r["capability"] == "skill-scripts"]
    assert scripts and all(r.get("carried_forward_from_receipt_id") in before for r in scripts)


def test_receipt_with_a_stale_content_hash_does_not_count(tmp_path: Path) -> None:
    manager, project, _ = _manager(tmp_path)
    source = _write_package(project / "src", FIXTURES["scripts"])
    manager.add(str(source), targets=["codex"], scope="project", allow_scripts=True)
    state_path = project / ".sessionfs" / "skills.state.json"
    state = json.loads(state_path.read_text())
    for row in state["packages"].values():
        for receipt in row["consent_receipts"]:
            receipt["content_hash"] = "sha256:" + "0" * 64
    state_path.write_text(json.dumps(state))
    manager.compile("kit", scope="project", allow_scripts=True)
    row = manager.find_installed("kit", scope="project")
    assert any(
        r["capability"] == "skill-scripts" and r["content_hash"] == row["content_hash"]
        for r in row["consent_receipts"]
    )
    (source / "skills/kit/SKILL.md").write_text(_skill_md("kit", body="Reworded."), encoding="utf-8")
    manager.update("kit", scope="project")  # carries from the valid receipt only



# --- review round 7 ----------------------------------------------------------------


def test_disclosure_digest_covers_everything_shown(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, FIXTURES["mixed"], "dd")
    base = disclosure.build_disclosure(manifest, tools=["codex"], scope="project")
    assert base["card_version"] == disclosure.CARD_VERSION and base["disclosure_digest"].startswith("sha256:")
    assert disclosure.build_disclosure(manifest, tools=["codex"], scope="project")["disclosure_digest"] == base["disclosure_digest"]
    changed = [
        disclosure.build_disclosure(manifest, tools=["claude-code"], scope="project"),
        disclosure.build_disclosure(manifest, tools=["codex"], scope="user"),
        disclosure.build_disclosure(manifest, tools=["codex"], provenance={"publisher_verified": True}),
        disclosure.build_disclosure({**manifest, "scan": {**manifest["scan"], "ruleset": "2099.1.1"}}, tools=["codex"]),
        disclosure.build_disclosure(
            {**manifest, "unicode_findings": [{"path": "a", "line": 1, "column": 1, "rule_id": "unicode.bidi_mark",
                                               "class": "bidi_mark", "severity": "low", "code_point": "U+200E",
                                               "visible": None}]},
            tools=["codex"],
        ),
    ]
    for card in changed:
        assert card["disclosure_digest"] != base["disclosure_digest"]
    # The access digest is unchanged by presentation-only differences.
    assert {card["capability_digest"] for card in changed[:3]} == {base["capability_digest"]}


def _native(tmp_path: Path, kind: str, command: str, tag: str) -> dict:
    src = tmp_path / f"native-{kind}-{tag}"
    (src / "skills" / "kit").mkdir(parents=True)
    (src / "skills" / "kit" / "SKILL.md").write_text(_skill_md("kit"), encoding="utf-8")
    hooks = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}]}}
    if kind == "gemini":
        (src / "gemini-extension.json").write_text(
            json.dumps({"name": "kit", "version": "1.0.0", "description": "d", **hooks}), encoding="utf-8"
        )
    else:
        (src / f".{kind}-plugin").mkdir()
        (src / f".{kind}-plugin" / "plugin.json").write_text(
            json.dumps({"name": "kit", "version": "1.0.0", "description": "d"}), encoding="utf-8"
        )
        (src / "hooks").mkdir()
        (src / "hooks" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
    record = import_package(src, tmp_path / f"store-{kind}-{tag}", SourceIdentity("local", str(src)))
    return disclosure.compute_manifest(record.package_root)


@pytest.mark.parametrize("kind", ["claude", "codex", "cursor", "gemini"])
def test_native_plugin_hook_changes_are_hashed_code_changes(tmp_path: Path, kind: str) -> None:
    old = _native(tmp_path, kind, "echo one", "a")
    new = _native(tmp_path, kind, "curl -s https://x.example/run", "b")
    extensions = old["executable_extensions"]
    assert extensions and all(item["sha256"] and item["path"] != "(declared)" for item in extensions)
    assert any(item["event"] == "PreToolUse" and item["matcher"] == "Bash" for item in old["hooks"])
    changes = disclosure.code_changes(old, new)
    assert {item["kind"] for item in changes} >= {"executable_extension", "hook"}
    update = disclosure.classify_update(old, new, old_content_hash="a", new_content_hash="b")
    assert update["update_class"] in {"code", "widening"}
    assert "executable-extensions" in update["changed_requirements"] or update["update_class"] == "widening"


def test_pull_never_downgrades_when_two_versions_are_granted(
    tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted, "_project_id", lambda manager, client: "proj_1")
    v1 = _publish(market, "1.0.0")
    v2 = _publish(market, "2.0.0", body="Two.", package_id=v1["package_id"])
    row = market.assign(v2, tools=["codex"])
    row["package_id"] = v1["package_id"]
    manager, _, home = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    assert manager.find_installed("kit", scope="user")["content_hash"] == v2["content_hash"]
    # An older version is ALSO granted (e.g. by a request): nothing flips.
    older = market.assign(v1, tools=["codex"])
    older["package_id"] = v1["package_id"]
    report = hosted.pull(manager, market.client())
    statuses = {item.version_id: item.status for item in report.items}
    assert statuses == {v2["version_id"]: "up-to-date", v1["version_id"]: "skipped"}
    assert manager.find_installed("kit", scope="user")["content_hash"] == v2["content_hash"]
    assert any("Conflicting grants" in note for note in report.notes)



def test_conflicts_are_found_even_when_grants_omit_the_package_id(
    tmp_path: Path, market: FakeMarketplace
) -> None:
    v1 = _publish(market, "1.0.0")
    v2 = _publish(market, "2.0.0", body="Two.", package_id=v1["package_id"])
    market.assign(v2, tools=["codex"])  # an older server: no package_id on the row
    manager, _, _ = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    market.assign(v1, tools=["codex"])
    report = hosted.pull(manager, market.client())
    statuses = {item.version_id: item.status for item in report.items}
    assert statuses == {v2["version_id"]: "up-to-date", v1["version_id"]: "skipped"}
    assert manager.find_installed("kit", scope="user")["content_hash"] == v2["content_hash"]


def test_unresolvable_packages_with_several_grants_fail_closed(
    tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1 = _publish(market, "1.0.0")
    v2 = _publish(market, "2.0.0", body="Two.", package_id=v1["package_id"])
    market.assign(v1, tools=["codex"])
    market.assign(v2, tools=["codex"])
    manager, _, _ = _manager(tmp_path, market)

    def _unavailable(self, version_id):
        raise SkillError("metadata unavailable")

    monkeypatch.setattr(hosted.HostedClient, "version_metadata", _unavailable)
    report = hosted.pull(manager, market.client())
    assert {item.status for item in report.items} == {"skipped"}
    assert manager.list_installed("user") == []



def test_partially_resolved_grants_quarantine_the_whole_scope(
    tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1 = _publish(market, "1.0.0")
    v2 = _publish(market, "2.0.0", body="Two.", package_id=v1["package_id"])
    resolved = market.assign(v1, tools=["codex"])
    resolved["package_id"] = v1["package_id"]  # this grant's identity is known
    market.assign(v2, tools=["codex"])  # this one's must be looked up ...
    manager, _, _ = _manager(tmp_path, market)
    original = hosted.HostedClient.version_metadata

    def _partial(self, version_id):
        if version_id == v2["version_id"]:
            raise SkillError("metadata unavailable")  # ... and the lookup fails
        return original(self, version_id)

    monkeypatch.setattr(hosted.HostedClient, "version_metadata", _partial)
    report = hosted.pull(manager, market.client())
    # v2 might be the same package as v1, so neither may install or replace.
    assert all(item.conflict for item in report.items)
    assert {item.status for item in report.items} == {"skipped"}
    assert manager.list_installed("user") == []
    assert any("could not resolve the package" in note.lower() for note in report.notes)


def test_cli_approval_surfaces_render_every_card_leaf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    source = _write_package(project / "src", FIXTURES["mixed"])
    card = json.loads(
        runner.invoke(skill_app, ["inspect", str(source), "--for", "codex", "--json"]).output
    )["disclosure"]
    leaves = disclosure.card_leaves(card)
    assert len(leaves) > 20 and any(path.startswith("tools.codex") for path, _ in leaves)
    shown = runner.invoke(skill_app, ["inspect", str(source), "--for", "codex"])
    added = runner.invoke(
        skill_app, ["add", str(source), "--for", "codex", "--allow-scripts", "--allow-hooks", "--yes"]
    )
    assert added.exit_code == 0, added.output
    for output in (shown.output, added.output):
        for path, value in leaves:
            assert f"{path} = {value}" in output, (path, value)


def test_card_leaves_cover_nested_values_and_empty_containers() -> None:
    leaves = dict(disclosure.card_leaves({"a": {"b": [1, "x"], "c": []}, "d": {}, "e": None, "f": True}))
    assert leaves == {"a.b[0]": "1", "a.b[1]": "x", "a.c": "[]", "d": "{}", "e": "null", "f": "true"}


def test_recovery_restoring_the_prior_version_keeps_its_consent_baseline(
    tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash recovery that reverts an update puts back the prior version's
    consented capability baseline AND its digest restriction exactly - it
    never leaves the reverted (widened) content unrestricted."""
    v1 = _publish(market, "1.0.0")
    assignment = market.assign(v1, tools=["codex"])
    manager, _, _ = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    v2 = _publish(market, "2.0.0", body="https://wide.example.com", package_id=v1["package_id"])
    market.assignments.remove(assignment)
    bound = market.assign(v2, tools=["codex"])
    bound["capability_digest"] = v2["capability_digest"]
    hosted.pull(manager, market.client())
    before = manager.find_installed("kit", scope="user")
    assert before["content_hash"] == v2["content_hash"]
    assert before["digest_authorized_origins"] == [f"assignment|{bound['id']}"]
    # A text update of v2 accepted as an OPTIONAL assignment, interrupted
    # before its acceptance was recorded: recovery reverts to v2.
    v3 = _publish(market, "3.0.0", body="https://wide.example.com again", package_id=v1["package_id"])
    market.assignments.remove(bound)
    market.assign(v3, mode="optional", tools=["codex"])
    real = hosted._record_origins
    calls = {"n": 0}

    def _crash(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash")
        return real(*args, **kwargs)

    monkeypatch.setattr(hosted, "_record_origins", _crash)
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert manager.find_installed("kit", scope="user")["content_hash"] == v3["content_hash"]
    hosted._recover_locally(manager, market.client(), ("user",))
    row = manager.find_installed("kit", scope="user")
    assert row["content_hash"] == v2["content_hash"]
    assert row["digest_authorized_origins"] == before["digest_authorized_origins"]
    assert row["capability_digest"] == before["capability_digest"]
    assert row["capability_manifest"] == before["capability_manifest"]
