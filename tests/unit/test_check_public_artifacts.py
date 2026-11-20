"""Unit tests for the public-artifact checker.

``.github/scripts/check_public_artifacts.py`` is the release gate that fails a
build if a wheel, sdist, or directory bundle carries any Enterprise-only path
or the ``SESSIONFS-ENTERPRISE-ONLY`` marker. These tests build tiny synthetic
artifacts in a temp dir and assert each rule fires (and does not fire on
clean Community content).
"""

from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / ".github" / "scripts" / "check_public_artifacts.py"

MARKER = b"SESSIONFS-ENTERPRISE-ONLY"


def _load_checker():
    if not CHECKER.is_file():
        pytest.skip("check_public_artifacts.py not present")
    spec = importlib.util.spec_from_file_location("check_public_artifacts", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def make_wheel(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def make_sdist(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


# --- path rules ---------------------------------------------------------


@pytest.mark.parametrize(
    "rel_path",
    [
        "sessionfs_ee/__init__.py",
        "ee/__init__.py",
        "ee/sessionfs_ee/__init__.py",
        "dashboard/src/ee/App.tsx",
        "pkg-0.32.0/ee/__init__.py",  # sdist wrapper puts ee/ at depth 1
        "pkg-0.32.0/dashboard/src/ee/App.tsx",
    ],
)
def test_path_is_enterprise_detects_enterprise_paths(rel_path):
    assert checker.path_is_enterprise(rel_path) is not None


@pytest.mark.parametrize(
    "rel_path",
    [
        "src/sessionfs/features.py",
        "src/sessionfs/server/helper/docs_index.json",
        "pkg-0.32.0/src/sessionfs/features.py",
        "usr/local/lib/python3.11/site-packages/sessionfs/features.py",
        "assets/index.js",
        "node_modules/foo/ee/__init__.py",  # ee/ nested deeper than repo root
        "spec/schemas/config.schema.json",
    ],
)
def test_path_is_enterprise_ignores_community_paths(rel_path):
    assert checker.path_is_enterprise(rel_path) is None


# --- content rule -------------------------------------------------------


def test_wheel_content_marker_detected(tmp_path: Path):
    wheel = make_wheel(
        tmp_path / "leak.whl",
        {"src/sessionfs/features.py": b"# " + MARKER + b"\ndef x(): pass\n"},
    )
    problems = checker.inspect_zip(wheel)
    assert any("content contains" in p for p in problems)


def test_wheel_clean_passes(tmp_path: Path):
    wheel = make_wheel(
        tmp_path / "clean.whl",
        {
            "src/sessionfs/__init__.py": b"",
            "src/sessionfs/features.py": b"def x(): pass\n",
        },
    )
    assert checker.inspect_zip(wheel) == []


def test_wheel_enterprise_package_path_detected(tmp_path: Path):
    wheel = make_wheel(tmp_path / "leak.whl", {"sessionfs_ee/__init__.py": b""})
    problems = checker.inspect_zip(wheel)
    assert any("sessionfs_ee" in p for p in problems)


# --- artifact dispatchers -------------------------------------------------


def test_inspect_tar_gz_detects_sdist_wrapper_ee(tmp_path: Path):
    sdist = make_sdist(
        tmp_path / "sessionfs-0.32.0.tar.gz",
        {"sessionfs-0.32.0/ee/__init__.py": b"# enterprise\n"},
    )
    problems = checker.inspect_tar_gz(sdist)
    assert any("repo-root ee/" in p for p in problems)


def test_inspect_tar_gz_clean_sdist_passes(tmp_path: Path):
    sdist = make_sdist(
        tmp_path / "sessionfs-0.32.0.tar.gz",
        {
            "sessionfs-0.32.0/src/sessionfs/__init__.py": b"",
            "sessionfs-0.32.0/README.md": b"community only\n",
        },
    )
    assert checker.inspect_tar_gz(sdist) == []


def test_inspect_directory_detects_dashboard_ee(tmp_path: Path):
    root = tmp_path / "dist"
    (root / "dashboard" / "src" / "ee").mkdir(parents=True)
    (root / "dashboard" / "src" / "ee" / "App.tsx").write_bytes(b"// x\n")
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "index.js").write_bytes(b"// y\n")
    problems = checker.inspect_directory(root)
    assert any("dashboard/src/ee" in p for p in problems)


def test_inspect_directory_detects_marker_content(tmp_path: Path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_bytes(b"<html>" + MARKER + b"</html>")
    problems = checker.inspect_directory(root)
    assert any("content contains" in p for p in problems)


def test_inspect_directory_clean_passes(tmp_path: Path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "index.js").write_bytes(b"console.log('hi');\n")
    (root / "index.html").write_bytes(b"<html></html>\n")
    assert checker.inspect_directory(root) == []


# --- CLI ---------------------------------------------------------------


def test_main_returns_zero_for_clean_artifact(tmp_path: Path, capsys):
    wheel = make_wheel(tmp_path / "clean.whl", {"src/sessionfs/__init__.py": b""})
    assert checker.main([str(wheel)]) == 0
    assert "ok" in capsys.readouterr().out


def test_main_returns_one_for_leaking_artifact(tmp_path: Path):
    wheel = make_wheel(tmp_path / "leak.whl", {"sessionfs_ee/__init__.py": b""})
    assert checker.main([str(wheel)]) == 1


def test_main_reports_missing_path(tmp_path: Path):
    assert checker.main([str(tmp_path / "nope.whl")]) == 1
