"""Unit tests for instruction_provenance.discover()."""

from __future__ import annotations

from pathlib import Path


from sessionfs.spec.instruction_provenance import (
    Provenance,
    discover,
)


MANAGED_MARKER = (
    "<!--\nsessionfs-managed: version=7 hash=deadbeefdeadbeef\n"
    "Canonical source: sfs rules edit\n-->\n\n# project rules\n"
)


def test_empty_dir_returns_none_source(tmp_path: Path):
    prov = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")
    assert prov.rules_source == "none"
    assert prov.rules_hash is None
    assert prov.instruction_artifacts == []


def test_unmanaged_claude_file_classifies_as_manual(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# plain unmanaged rules\n")
    prov = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")
    assert prov.rules_source == "manual"
    assert prov.rules_hash is not None
    assert any(a.path == "CLAUDE.md" for a in prov.instruction_artifacts)
    assert all(a.source != "sessionfs" for a in prov.instruction_artifacts)


def test_managed_claude_file_classifies_as_sessionfs(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text(MANAGED_MARKER)
    prov = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")
    assert prov.rules_source == "sessionfs"
    assert prov.rules_version == 7
    assert prov.rules_hash is not None
    first = prov.instruction_artifacts[0]
    assert first.source == "sessionfs"
    assert first.version == 7


def test_mixed_when_both_managed_and_unmanaged_present(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text(MANAGED_MARKER)
    (tmp_path / "GEMINI.md").write_text("# plain\n")
    prov = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")
    assert prov.rules_source == "mixed"


def test_captures_all_tool_files(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("# a\n")
    (tmp_path / "codex.md").write_text("# b\n")
    (tmp_path / ".cursorrules").write_text("# c\n")
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/copilot-instructions.md").write_text("# d\n")
    (tmp_path / "GEMINI.md").write_text("# e\n")
    prov = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")
    paths = {a.path for a in prov.instruction_artifacts if a.scope == "project"}
    assert {"CLAUDE.md", "codex.md", ".cursorrules",
            ".github/copilot-instructions.md", "GEMINI.md"} <= paths


def test_global_env_off_suppresses_global_hashes(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/CLAUDE.md").write_text("# global\n")
    monkeypatch.setenv("SFS_CAPTURE_GLOBAL_RULES", "off")
    (tmp_path / "CLAUDE.md").write_text("# local\n")
    prov = discover(tmp_path, tool="claude-code", home_dir=home)
    assert all(a.scope != "global" for a in prov.instruction_artifacts)


def test_global_captured_when_enabled(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/CLAUDE.md").write_text("# global\n")
    monkeypatch.delenv("SFS_CAPTURE_GLOBAL_RULES", raising=False)
    prov = discover(tmp_path, tool="claude-code", home_dir=home)
    globals_ = [a for a in prov.instruction_artifacts if a.scope == "global"]
    assert len(globals_) >= 1
    assert all(a.path is None for a in globals_), "global artifacts must not leak path"


def test_discover_is_nonfatal_on_broken_project(tmp_path: Path):
    # Pass a path that doesn't exist — must return empty provenance, not raise.
    prov = discover(tmp_path / "does-not-exist", tool="claude-code")
    assert isinstance(prov, Provenance)
    assert prov.rules_source == "none"


def test_auxiliary_files_recorded(tmp_path: Path):
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir()
    (agents_dir / "atlas.md").write_text("# agent\n")
    prov = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")
    kinds = {a.artifact_type for a in prov.instruction_artifacts}
    assert "agent" in kinds


def test_nested_auxiliary_skill_files_recorded(tmp_path: Path):
    skills_dir = tmp_path / ".claude/skills/python-review"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("# nested skill\n")
    prov = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")
    paths = {a.path for a in prov.instruction_artifacts if a.artifact_type == "skill"}
    assert ".claude/skills/python-review/SKILL.md" in paths


def test_auxiliary_dir_scan_is_recursive_but_bounded(tmp_path: Path):
    agents_dir = tmp_path / ".agents"
    for idx in range(35):
        branch = agents_dir / f"group-{idx:02d}"
        branch.mkdir(parents=True, exist_ok=True)
        (branch / f"agent-{idx:02d}.md").write_text(f"# agent {idx}\n")

    prov = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")
    agent_paths = sorted(
        a.path for a in prov.instruction_artifacts
        if a.artifact_type == "agent" and a.scope == "project"
    )

    assert len(agent_paths) == 30
    assert ".agents/group-00/agent-00.md" in agent_paths
    assert ".agents/group-29/agent-29.md" in agent_paths
    assert ".agents/group-30/agent-30.md" not in agent_paths


def test_every_supported_tools_project_skill_root_is_scanned(tmp_path: Path):
    """P2-4: unmanaged skills under any adapter's project root (Gemini, Roo,
    Kilo, Copilot, Cline, ...) are reported, not only Claude Code's."""
    from sessionfs.skills.adapters import PROJECT_SKILL_ROOTS
    from sessionfs.spec.instruction_provenance import PROJECT_AUX_DIRS

    assert {root for root, kind in PROJECT_AUX_DIRS if kind == "skill"} >= set(PROJECT_SKILL_ROOTS.values())
    for tool, root in PROJECT_SKILL_ROOTS.items():
        skill_dir = tmp_path / root / f"{tool}-manual"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# manual {tool}\n")
    prov = discover(tmp_path, tool="gemini", home_dir=tmp_path / "home")
    skills = [a for a in prov.instruction_artifacts if a.artifact_type == "skill" and a.scope == "project"]
    paths = {a.path for a in skills}
    assert ".gemini/skills/gemini-manual/SKILL.md" in paths
    assert ".roo/skills/roo-code-manual/SKILL.md" in paths
    assert ".kilo/skills/kilo-code-manual/SKILL.md" in paths
    assert ".github/skills/copilot-manual/SKILL.md" in paths
    assert ".cline/skills/cline-manual/SKILL.md" in paths
    assert ".agents/skills/cursor-manual/SKILL.md" in paths
    assert all(a.source == "manual" for a in skills)
    # `.agents/skills/...` is recorded once, as a skill - not again as an agent.
    agents = [a.path for a in prov.instruction_artifacts if a.artifact_type == "agent"]
    assert not any(path.startswith(".agents/skills/") for path in agents)
    assert len([a for a in prov.instruction_artifacts if a.path == ".agents/skills/cursor-manual/SKILL.md"]) == 1


def test_managed_files_do_not_consume_the_aux_scan_budget(tmp_path, monkeypatch):
    """R16 P2-2: 30 managed outputs in a skill root must not hide the one
    manual skill that sorts after them."""
    import os

    from sessionfs.spec import instruction_provenance

    root = tmp_path / ".claude" / "skills"
    managed = []
    for idx in range(30):
        skill_dir = root / f"managed-{idx:02d}"
        skill_dir.mkdir(parents=True)
        path = skill_dir / "SKILL.md"
        path.write_text(f"# managed {idx}\n")
        managed.append(Path(os.path.abspath(path)))
    manual = root / "zz-manual" / "SKILL.md"
    manual.parent.mkdir()
    manual.write_text("# manual\n")
    monkeypatch.setattr(instruction_provenance, "_managed_output_paths", lambda *a, **k: set(managed))
    prov = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")
    skills = {a.path for a in prov.instruction_artifacts if a.artifact_type == "skill" and a.source == "manual"}
    assert ".claude/skills/zz-manual/SKILL.md" in skills
    assert not any("managed-" in path for path in skills)
    # The iterator itself: excluded files never count against the limit.
    listed = instruction_provenance._iter_aux_files(root, limit=1, exclude=lambda p: "managed-" in str(p))
    assert [p.name for p in listed] == ["SKILL.md"] and listed[0].parent.name == "zz-manual"


def test_hostile_state_with_thousands_of_duplicate_outputs_is_bounded(tmp_path):
    """S MEDIUM-1: 5,000 duplicate output entries complete fast and the
    package is reported `unverified` (entry budget tripped), never re-hashed."""
    import json
    import time

    from sessionfs.spec import instruction_provenance

    assert instruction_provenance.MANAGED_OUTPUT_ENTRY_CAP == 2_000
    assert instruction_provenance.MANAGED_OUTPUT_HASH_BYTES_CAP == 64 * 1024 * 1024
    output = tmp_path / ".codex" / "skills" / "x" / "SKILL.md"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"# x\n" * 100_000)  # ~400 KiB, hashed at most once
    digest = "sha256:" + __import__("hashlib").sha256(output.read_bytes()).hexdigest()
    state_dir = tmp_path / ".sessionfs"
    state_dir.mkdir()
    entry = {"path": str(output), "output_hash": digest, "tool": "codex", "tools": ["codex"]}
    (state_dir / "skills.state.json").write_text(json.dumps({"version": 1, "packages": {
        "skp_hostile": {"package_id": "skp_hostile", "name": "x", "content_hash": digest, "targets": ["codex"],
                        "source": {"source_type": "git", "locator": "https://x/y"}, "outputs": [entry] * 5_000},
        "skp_ok": {"package_id": "skp_ok", "name": "ok", "content_hash": digest, "targets": ["codex"],
                   "source": {"source_type": "local-project", "locator": "s"}, "outputs": [entry]},
    }}), encoding="utf-8")
    reads = []
    real_read = instruction_provenance._safe_read

    def counting_read(path, *args, **kwargs):
        if str(path) == str(output):
            reads.append(1)
        return real_read(path, *args, **kwargs)

    instruction_provenance._safe_read = counting_read
    try:
        started = time.monotonic()
        prov = discover(tmp_path, tool="codex", home_dir=tmp_path / "home")
        elapsed = time.monotonic() - started
    finally:
        instruction_provenance._safe_read = real_read
    assert elapsed < 15  # wall clock, so leave room for a loaded machine
    status = {item["package_id"]: item["integrity_status"] for item in prov.managed_packages}
    assert status["skp_hostile"] == "unverified"
    assert len(reads) <= 2  # the shared output is hashed once, never per entry


def test_rules_source_stays_rules_only_and_skills_source_is_separate(tmp_path):
    """R20 P3-1: managed skill packages never flip `rules_source`; the new
    `skills_source` field reports managed | manual | mixed | none."""
    import json

    from sessionfs.spec import instruction_provenance

    empty = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home").as_dict()
    assert empty["rules_source"] == "none" and empty["skills_source"] == "none"

    manual = tmp_path / ".claude" / "skills" / "manual-one" / "SKILL.md"
    manual.parent.mkdir(parents=True)
    manual.write_text("# manual\n")
    only_manual = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home").as_dict()
    assert only_manual["skills_source"] == "manual"
    assert only_manual["rules_source"] == "none"  # R21 P3-3: skills never touch rules_source
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "atlas.md").write_text("# agent\n")
    with_agent = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home").as_dict()
    assert with_agent["rules_source"] == "manual"  # agents/settings still count as manual instruction content
    (tmp_path / ".agents" / "atlas.md").unlink()
    (tmp_path / ".agents").rmdir()

    (tmp_path / "CLAUDE.md").write_text("<!-- sessionfs:managed v3 -->\n# rules\n")
    with_rules = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")
    rules_only = with_rules.rules_source

    managed = tmp_path / ".claude" / "skills" / "managed-one" / "SKILL.md"
    managed.parent.mkdir(parents=True)
    managed.write_text("# managed\n")
    digest = "sha256:" + __import__("hashlib").sha256(managed.read_bytes()).hexdigest()
    state_dir = tmp_path / ".sessionfs"
    state_dir.mkdir()
    (state_dir / "skills.state.json").write_text(json.dumps({"version": 1, "packages": {
        "skp_m": {"package_id": "skp_m", "name": "managed-one", "content_hash": digest, "targets": ["claude-code"],
                  "source": {"source_type": "local-project", "locator": "s"},
                  "outputs": [{"path": str(managed), "output_hash": digest, "tool": "claude-code", "tools": ["claude-code"]}]},
    }}), encoding="utf-8")
    mixed = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home").as_dict()
    assert mixed["skills_source"] == "mixed"
    assert mixed["rules_source"] == rules_only  # unchanged by the managed package
    assert mixed["managed_packages"][0]["package_id"] == "skp_m"
    manual.unlink()
    manual.parent.rmdir()
    managed_only = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home").as_dict()
    assert managed_only["skills_source"] == "managed"
    assert managed_only["rules_source"] == rules_only
    assert "skills_source" in instruction_provenance.Provenance().as_dict()


def test_aux_scan_prunes_vcs_metadata_and_dot_directories(tmp_path: Path):
    """R26 P3-3: a manual skill checked out as a git repo must not spend the
    30-file budget on `.git/objects` and hide its sibling skills."""
    objects = tmp_path / ".claude" / "skills" / "aaa" / ".git" / "objects"
    objects.mkdir(parents=True)
    for idx in range(40):
        (objects / f"{idx:02d}").write_text("blob\n")
    (tmp_path / ".claude" / "skills" / "aaa" / "SKILL.md").write_text("# aaa\n")
    hidden = tmp_path / ".claude" / "skills" / "aaa" / ".cache"
    hidden.mkdir()
    (hidden / "index").write_text("cache\n")
    (tmp_path / ".claude" / "skills" / "bbb").mkdir()
    (tmp_path / ".claude" / "skills" / "bbb" / "SKILL.md").write_text("# bbb\n")

    prov = discover(tmp_path, tool="claude-code", home_dir=tmp_path / "home")

    paths = {a.path for a in prov.instruction_artifacts if a.artifact_type == "skill"}
    assert paths == {".claude/skills/aaa/SKILL.md", ".claude/skills/bbb/SKILL.md"}
    assert prov.skills_source == "manual"
