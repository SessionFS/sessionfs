"""Unit tests for enterprise feature loading.

`sessionfs.features` is the single seam between the Community package and the
Enterprise package: it reports whether `sessionfs_ee` is importable and loads
a feature module from it when it is, returning None otherwise. These tests
force both states through `sys.modules` / `sys.path` so they pass in the dev
repo (with `ee/` editable-installed) and in a Community-only export (without
it) alike.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from sessionfs.features import get_feature, is_ee_available


def _ee_path_entries() -> list[str]:
    """sys.path entries that could resolve a `sessionfs_ee` package."""
    return [p for p in sys.path if p == "ee" or p.rstrip("/").endswith("/ee")]


@pytest.fixture
def no_ee_package(monkeypatch):
    """Drop any real or editable `sessionfs_ee` from the import machinery."""
    for name in list(sys.modules):
        if name == "sessionfs_ee" or name.startswith("sessionfs_ee."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in _ee_path_entries()])


def _fake_ee_package() -> ModuleType:
    pkg = ModuleType("sessionfs_ee")
    pkg.__path__ = []
    return pkg


def test_is_ee_available_false_without_ee(no_ee_package):
    assert is_ee_available() is False


def test_get_feature_none_without_ee(no_ee_package):
    assert get_feature("pr_comments") is None


def test_get_feature_nonexistent_returns_none(no_ee_package):
    assert get_feature("nonexistent_module_xyz") is None


def test_is_ee_available_true_when_package_importable(monkeypatch):
    monkeypatch.setitem(sys.modules, "sessionfs_ee", _fake_ee_package())
    assert is_ee_available() is True


def test_get_feature_loads_module_when_present(monkeypatch):
    feature = ModuleType("sessionfs_ee.pr_comments")
    monkeypatch.setitem(sys.modules, "sessionfs_ee", _fake_ee_package())
    monkeypatch.setitem(sys.modules, "sessionfs_ee.pr_comments", feature)
    assert get_feature("pr_comments") is feature


def test_get_feature_missing_submodule_returns_none(monkeypatch):
    monkeypatch.setitem(sys.modules, "sessionfs_ee", _fake_ee_package())
    assert get_feature("pr_comments") is None


def test_is_ee_available_missing_dependency_raises(tmp_path, monkeypatch):
    """A ``sessionfs_ee`` whose own import is broken must propagate, not read
    as "not installed" — that would silently downgrade to Community."""
    pkg = tmp_path / "sessionfs_ee"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "import sessionfs_ee_missing_dependency  # noqa: F401\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "sessionfs_ee", raising=False)
    with pytest.raises(ModuleNotFoundError):
        is_ee_available()


def test_get_feature_missing_dependency_raises(tmp_path, monkeypatch):
    """A feature module whose own dependency is missing must propagate, not
    read as "feature absent"."""
    pkg = tmp_path / "sessionfs_ee"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "pr_comments.py").write_text(
        "import sessionfs_ee_missing_dependency  # noqa: F401\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "sessionfs_ee", raising=False)
    try:
        with pytest.raises(ModuleNotFoundError):
            get_feature("pr_comments")
    finally:
        sys.modules.pop("sessionfs_ee", None)
