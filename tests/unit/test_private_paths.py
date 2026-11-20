"""Unit tests for the shared .release/private_paths.py matcher.

`.release/` is stripped from public main, so this imports the module lazily
and skips cleanly when it is absent (a Community-only export).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_HELPER_PATH = Path(__file__).resolve().parents[2] / ".release" / "private_paths.py"


def _load_matcher():
    if not _HELPER_PATH.exists():
        pytest.skip(".release/private_paths.py not present (likely on main)")
    spec = importlib.util.spec_from_file_location("private_paths", _HELPER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def matcher():
    return _load_matcher()


def test_validate_entry_rejects_absolute_and_parent_traversal(matcher):
    assert matcher.validate_entry("/etc/passwd") is not None
    assert matcher.validate_entry("ee/../secret.py") is not None
    assert matcher.validate_entry("../.agents") is not None


def test_validate_entry_accepts_relative_entries(matcher):
    assert matcher.validate_entry("ee/") is None
    assert matcher.validate_entry("docs/positioning.md") is None
    assert matcher.validate_entry("v*-implementation-brief") is None


def test_is_private_dir_prefix_vs_glob(matcher):
    entries = ["ee/", "v*-implementation-brief"]
    assert matcher.is_private("ee/sessionfs_ee/__init__.py", entries) is True
    assert matcher.is_private("ee/", entries) is True
    assert matcher.is_private("v0.10.0-implementation-brief", entries) is True
    assert matcher.is_private("README.md", entries) is False


def test_paths_to_delete_dir_and_glob(matcher, tmp_path):
    (tmp_path / "ee").mkdir()
    (tmp_path / "ee" / "secret.py").write_text("x")
    (tmp_path / "v0.10.0-implementation-brief").write_text("x")
    (tmp_path / "v0.11.0-resume-atlas-brief").write_text("x")
    (tmp_path / "README.md").write_text("x")

    deleted = matcher.paths_to_delete(
        tmp_path,
        ["ee/", "v*-implementation-brief", "v*-resume-*-brief"],
    )
    # Directory entries yield the directory itself so `rm -rf` removes the tree.
    assert "ee" in deleted
    assert "v0.10.0-implementation-brief" in deleted
    assert "v0.11.0-resume-atlas-brief" in deleted
    assert "README.md" not in deleted


def test_main_cli_refuses_unsafe_entries(matcher, tmp_path, capsys):
    manifest = tmp_path / "private-files.txt"
    manifest.write_text("/etc/passwd\n")
    assert matcher.main(["--root", str(tmp_path), "--list", str(manifest)]) == 2
    assert "unsafe private entry" in capsys.readouterr().err


def test_main_cli_lists_paths(matcher, tmp_path, capsys):
    (tmp_path / ".release").mkdir()
    manifest = tmp_path / ".release" / "private-files.txt"
    manifest.write_text("ee/\n")
    (tmp_path / "ee").mkdir()
    (tmp_path / "ee" / "secret.py").write_text("x")
    assert matcher.main(["--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.split() == ["ee"]
