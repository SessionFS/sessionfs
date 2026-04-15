"""Unit tests for `sfs rules` CLI helpers (no API / no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sessionfs.cli import cmd_rules


def test_canonicalize_tool_common_forms():
    assert cmd_rules._canonicalize_tool("claude-code") == "claude-code"
    assert cmd_rules._canonicalize_tool("cc") == "claude-code"
    assert cmd_rules._canonicalize_tool("gemini-cli") == "gemini"
    assert cmd_rules._canonicalize_tool("copilot-cli") == "copilot"
    assert cmd_rules._canonicalize_tool("amp") is None
    assert cmd_rules._canonicalize_tool("") is None


def test_find_existing_rule_files(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text("a")
    (tmp_path / "AGENTS.md").write_text("b")  # seeds codex
    (tmp_path / ".cursorrules").write_text("c")
    files = cmd_rules._find_existing_rule_files(tmp_path)
    assert set(files.keys()) == {"claude-code", "codex", "cursor"}


def test_preselect_tools_honours_precedence(tmp_path: Path):
    existing = {"claude-code": tmp_path / "CLAUDE.md"}
    recent = {"cursor": 12, "codex": 0}
    selected = cmd_rules._preselect_tools(existing, recent)
    tools = {t for t, _ in selected}
    # claude-code present via existing file; cursor via recent usage.
    assert "claude-code" in tools
    assert "cursor" in tools
    # codex with count=0 is not selected
    assert "codex" not in tools


def test_is_managed_detects_marker(tmp_path: Path):
    managed = tmp_path / "CLAUDE.md"
    managed.write_text(
        "<!--\nsessionfs-managed: version=3 hash=abcdef12\n"
        "Canonical source: sfs rules edit\n-->\n\n# rules\n"
    )
    assert cmd_rules._is_managed(managed) is True

    plain = tmp_path / "plain.md"
    plain.write_text("# plain file\nNo marker.\n")
    assert cmd_rules._is_managed(plain) is False


def test_is_managed_only_scans_first_512_bytes(tmp_path: Path):
    """A hand-written file that mentions the marker phrase deep in the body
    must NOT be treated as managed — the regex window guards against that
    false-positive.
    """
    decoy = tmp_path / "CLAUDE.md"
    body = "# My hand-written rules\n\n" + ("filler content. " * 200) + (
        "\n\nNote: the marker format is `sessionfs-managed: version=1 hash=abcdef12`."
    )
    decoy.write_text(body)
    # Sanity: the marker phrase IS present in the file...
    assert "sessionfs-managed" in decoy.read_text()
    # ...but past the 512-byte window, so it's not treated as managed.
    assert cmd_rules._is_managed(decoy) is False


def test_rules_show_in_sync_uses_full_content(tmp_path: Path):
    """Regression: `sfs rules show` must compare full file content (with
    marker) against the compile output's `content`, not against the
    body-only `hash`. Earlier mismatch made every freshly written managed
    file look out-of-sync.
    """
    # Simulate: write a managed file via the same compiler the API uses,
    # then verify the byte-equality check that the show command runs.
    from sessionfs.server.services.rules_compiler import COMPILERS
    from sessionfs.server.services.rules_compiler.base import (
        CompileContext,
    )

    ctx = CompileContext(
        static_rules="static body",
        knowledge_claims=[],
        context_sections={},
        tool_overrides={},
        version=4,
    )
    result = COMPILERS["claude-code"].compile(ctx)
    target = tmp_path / "CLAUDE.md"
    target.write_text(result.content, encoding="utf-8")

    # `out["content"]` is what the API would have returned.
    out = {"filename": "CLAUDE.md", "content": result.content, "hash": result.content_hash}
    in_sync = target.read_bytes() == out["content"].encode("utf-8")
    assert in_sync, "Byte equality should hold for a freshly written managed file"
    # The body hash alone is intentionally NOT equal to a SHA256 of the file —
    # that comparison is the bug we're guarding against.
    import hashlib
    full_file_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    assert full_file_sha != out["hash"], (
        "If body hash equals full-file hash, the regression test is meaningless"
    )


def test_write_gitignore_entry_appends(tmp_path: Path):
    cmd_rules._write_gitignore_entry(tmp_path, "CLAUDE.md")
    assert (tmp_path / ".gitignore").read_text().strip() == "CLAUDE.md"
    # Idempotent — no duplicate
    cmd_rules._write_gitignore_entry(tmp_path, "CLAUDE.md")
    assert (tmp_path / ".gitignore").read_text().count("CLAUDE.md") == 1
    # Additional entries appended on new line
    cmd_rules._write_gitignore_entry(tmp_path, ".cursorrules")
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert "CLAUDE.md" in lines
    assert ".cursorrules" in lines
