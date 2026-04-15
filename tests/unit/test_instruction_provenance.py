"""Unit tests for instruction_provenance.discover()."""

from __future__ import annotations

from pathlib import Path

import pytest

from sessionfs.spec.instruction_provenance import (
    GLOBAL_CANDIDATES,
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
