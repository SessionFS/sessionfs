"""Unit tests for v0.9.9.8: sfs doctor stale-install detection.

Pius bug shape: pip-installed sessionfs 0.9.9.7 into
~/Library/Python/3.14/bin, but PATH still resolves `sfs` to a binary
tied to a DIFFERENT Python (system 3.12). New `pull-handoff` errors as
"No such command" because the user is running an OLDER `sfs`.

Codex round 1 caught that the v0.9.9.8 detection only enumerated the
current interpreter, which misses the cross-Python case entirely.
These tests exercise the cross-Python shebang-based detection that
actually catches Pius's bug.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from sessionfs.cli.cmd_doctor import (
    _check_install_consistency,
    _resolve_sfs_binary_python,
)


def _dist(version: str, path: str = "/fake/site-packages"):
    """Fake importlib.metadata Distribution."""
    d = MagicMock()
    d.version = version
    d.metadata = {"Name": "sessionfs"}
    d._path = path
    return d


# ── _resolve_sfs_binary_python ────────────────────────────────────


def test_resolve_python_returns_none_when_sfs_not_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert _resolve_sfs_binary_python() is None


def test_resolve_python_reads_plain_shebang(monkeypatch, tmp_path):
    script = tmp_path / "sfs"
    script.write_text("#!/usr/local/bin/python3.12\nimport sys\n")
    script.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda _name: str(script))
    assert _resolve_sfs_binary_python() == "/usr/local/bin/python3.12"


def test_resolve_python_reads_env_shebang(monkeypatch, tmp_path):
    """`#!/usr/bin/env python3.14` form — common on macOS installs."""
    script = tmp_path / "sfs"
    script.write_text("#!/usr/bin/env python3.14\nimport sys\n")
    script.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda _name: str(script))
    assert _resolve_sfs_binary_python() == "python3.14"


def test_resolve_python_returns_none_on_unreadable_script(monkeypatch, tmp_path):
    """Best-effort: missing/unreadable file → None, never an exception."""
    monkeypatch.setattr("shutil.which", lambda _name: str(tmp_path / "missing"))
    assert _resolve_sfs_binary_python() is None


# ── _check_install_consistency end-to-end ────────────────────────


def test_consistency_ok_when_same_interpreter_same_version(monkeypatch):
    """No drift, sfs on PATH belongs to this Python."""
    monkeypatch.setattr("sessionfs.__version__", "0.9.9.7", raising=False)
    monkeypatch.setattr(
        "importlib.metadata.distributions",
        lambda: [_dist("0.9.9.7", "/venv/lib")],
    )
    monkeypatch.setattr("shutil.which", lambda _name: sys.executable + "-sfs")
    monkeypatch.setattr(
        "sessionfs.cli.cmd_doctor._resolve_sfs_binary_python",
        lambda: sys.executable,
    )

    ok, detail = _check_install_consistency()
    assert ok is True, detail
    assert "0.9.9.7" in detail
    assert "drift" not in detail.lower()


def test_consistency_flags_cross_python_drift(monkeypatch):
    """The Pius bug: running interpreter has 0.9.9.7, but the sfs on
    PATH belongs to a DIFFERENT Python that sees 0.9.9.5."""
    monkeypatch.setattr("sessionfs.__version__", "0.9.9.7", raising=False)
    monkeypatch.setattr(
        "importlib.metadata.distributions",
        lambda: [_dist("0.9.9.7", "/venv/lib")],
    )
    monkeypatch.setattr(
        "shutil.which", lambda _name: "/usr/local/bin/sfs"
    )
    # sfs on PATH is shebang'd at /usr/local/bin/python3.12 (a different
    # interpreter than sys.executable).
    monkeypatch.setattr(
        "sessionfs.cli.cmd_doctor._resolve_sfs_binary_python",
        lambda: "/usr/local/bin/python3.12",
    )
    # And that peer Python sees an older sessionfs.
    monkeypatch.setattr(
        "sessionfs.cli.cmd_doctor._peer_sessionfs_version",
        lambda _py: "0.9.9.5",
    )

    ok, detail = _check_install_consistency()
    assert ok is False
    assert "drift" in detail.lower()
    assert "0.9.9.5" in detail
    assert "0.9.9.7" in detail
    # Workaround advice mentions the python -m fallback that we made
    # reachable via the __main__ guard in cli/main.py.
    assert "python -m sessionfs.cli.main" in detail


def test_consistency_flags_when_peer_python_missing_sessionfs(monkeypatch):
    """If the sfs binary's Python can't import sessionfs at all, that's
    still drift — flag it."""
    monkeypatch.setattr("sessionfs.__version__", "0.9.9.7", raising=False)
    monkeypatch.setattr(
        "importlib.metadata.distributions",
        lambda: [_dist("0.9.9.7", "/venv/lib")],
    )
    monkeypatch.setattr(
        "shutil.which", lambda _name: "/usr/local/bin/sfs"
    )
    monkeypatch.setattr(
        "sessionfs.cli.cmd_doctor._resolve_sfs_binary_python",
        lambda: "/usr/local/bin/python3.12",
    )
    monkeypatch.setattr(
        "sessionfs.cli.cmd_doctor._peer_sessionfs_version",
        lambda _py: None,  # subprocess failed
    )

    ok, detail = _check_install_consistency()
    assert ok is False
    assert "could not query" in detail.lower()


def test_consistency_flags_same_interpreter_dist_drift(monkeypatch):
    """Secondary check: same interpreter has a newer dist-info than
    the imported module reports. Catches the in-process editable
    install + cached metadata case."""
    monkeypatch.setattr("sessionfs.__version__", "0.9.9.5", raising=False)
    monkeypatch.setattr(
        "importlib.metadata.distributions",
        lambda: [
            _dist("0.9.9.7", "/user-site"),
            _dist("0.9.9.5", "/old-venv"),
        ],
    )
    monkeypatch.setattr("shutil.which", lambda _name: sys.executable + "-sfs")
    monkeypatch.setattr(
        "sessionfs.cli.cmd_doctor._resolve_sfs_binary_python",
        lambda: sys.executable,
    )

    ok, detail = _check_install_consistency()
    assert ok is False
    assert "0.9.9.7" in detail
    assert "same interpreter" in detail.lower()


def test_consistency_ok_when_sfs_not_on_path(monkeypatch):
    """No sfs on PATH at all → not a drift bug, just a packaging note.
    The user is invoking via a different mechanism (e.g. `python -m`
    or an IDE)."""
    monkeypatch.setattr("sessionfs.__version__", "0.9.9.7", raising=False)
    monkeypatch.setattr(
        "importlib.metadata.distributions",
        lambda: [_dist("0.9.9.7", "/venv/lib")],
    )
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "sessionfs.cli.cmd_doctor._resolve_sfs_binary_python",
        lambda: None,
    )

    ok, detail = _check_install_consistency()
    assert ok is True
    assert "not on PATH" in detail
