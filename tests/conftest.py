"""Shared test fixtures.

Isolation safeguard: no test may ever read the developer's real SessionFS
credentials or reach a real server.

* Before anything from `sessionfs` is imported, HOME points at a throwaway
  directory (module-level constants such as the daemon's config dir are
  computed from it at import time), and every test then gets its OWN fresh
  HOME (`~/.sessionfs`, `active_profile`, `profiles/` all live under it).
* SESSIONFS_API_URL / SESSIONFS_API_KEY are set to an unroutable address and
  a dummy key unless a test sets them itself; SESSIONFS_PROFILE is cleared.
* Outbound socket connections to anything but loopback raise immediately
  (local test servers and a localhost PostgreSQL stay reachable).
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import tempfile
from pathlib import Path

_SESSION_HOME = tempfile.mkdtemp(prefix="sfs-test-home-")
os.environ["HOME"] = _SESSION_HOME
os.environ.pop("USERPROFILE", None)

import pytest  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CC_FIXTURES = FIXTURES_DIR / "cc_sessions"

UNROUTABLE_API_URL = "http://127.0.0.1:9"  # discard port on loopback: refused at once
DUMMY_API_KEY = "sk_sfs_test_dummy_key_not_real"


class ExternalNetworkBlocked(RuntimeError):
    """Raised when a test tries to open a connection to a non-local host."""


def _is_local(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX paths and other non-IP families
    host = str(address[0]).strip("[]")
    if host in {"localhost", "localhost.localdomain", ""}:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False  # a hostname that is not localhost


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guarded_connect(self: socket.socket, address: object) -> None:
    if not _is_local(address):
        raise ExternalNetworkBlocked(
            f"Tests must not open network connections to non-local hosts (tried {address!r})"
        )
    return _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    if not _is_local(address):
        raise ExternalNetworkBlocked(
            f"Tests must not open network connections to non-local hosts (tried {address!r})"
        )
    return _real_connect_ex(self, address)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _isolated_credentials(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """A fresh HOME per test, and no real credentials or server."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("SESSIONFS_PROFILE", raising=False)
    monkeypatch.setenv("SESSIONFS_API_URL", UNROUTABLE_API_URL)
    monkeypatch.setenv("SESSIONFS_API_KEY", DUMMY_API_KEY)
    # Marketplace install counts are off unless a test turns them on, so
    # request-counting tests never see the extra best-effort POSTs.
    monkeypatch.setenv("SFS_SKILL_USAGE_REPORTING", "0")
    yield


@pytest.fixture
def tmp_store(tmp_path: Path) -> Path:
    """Create a temporary store directory."""
    store_dir = tmp_path / ".sessionfs"
    store_dir.mkdir()
    (store_dir / "sessions").mkdir()
    return store_dir


@pytest.fixture
def tmp_claude_home(tmp_path: Path) -> Path:
    """Create a fake ~/.claude directory with test session fixtures."""
    claude_home = tmp_path / ".claude"
    projects_dir = claude_home / "projects"

    # Create a fake project directory
    project_dir = projects_dir / "-Users-test-myproject"
    project_dir.mkdir(parents=True)

    # Copy all fixture sessions
    for jsonl in CC_FIXTURES.glob("*.jsonl"):
        shutil.copy(jsonl, project_dir / jsonl.name)

    # Copy subagent directory structure
    subagent_dir = CC_FIXTURES / "test-subagent-9999"
    if subagent_dir.is_dir():
        dest = project_dir / "test-subagent-9999"
        shutil.copytree(subagent_dir, dest)

    return claude_home


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Group tests for parallel runs (`pytest -n auto --dist loadgroup`).

    Each file stays on one worker, as with `--dist loadfile`. Every file that
    uses the shared SFS_TEST_PG_URL database joins a single "postgres" group,
    because they create, drop and migrate the same schema.
    """
    pg_files: dict[str, bool] = {}
    for item in items:
        path = str(item.path)
        if path not in pg_files:
            try:
                pg_files[path] = "SFS_TEST_PG_URL" in Path(path).read_text(encoding="utf-8")
            except OSError:
                pg_files[path] = False
        group = "postgres" if pg_files[path] else path
        item.add_marker(pytest.mark.xdist_group(group))
