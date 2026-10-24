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
# Rich fixes a console's width at construction when COLUMNS is set (CI
# runners export COLUMNS=80), so the CLI's module-level console would ignore
# the per-invoke COLUMNS that table tests pass to CliRunner.
os.environ.pop("COLUMNS", None)
os.environ.pop("LINES", None)

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


@pytest.fixture(autouse=True)
def _reset_llm_base_url_guard():
    """create_app installs a process-wide SSRF guard into the LLM provider
    module; start every test without it so CLI-side provider tests are not
    affected by an app built in an earlier test (an app fixture re-installs
    it after this runs)."""
    from sessionfs.judge import providers

    providers.set_base_url_guard(None)
    yield
    providers.set_base_url_guard(None)


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


def _uses_shared_pg(path: object) -> bool:
    try:
        return "SFS_TEST_PG_URL" in Path(str(path)).read_text(encoding="utf-8")
    except OSError:
        return False


@pytest.fixture(autouse=True, scope="module")
def _pg_schema_isolation(request: pytest.FixtureRequest):
    """Reset the shared PostgreSQL database around every file that uses it.

    Each such file starts from an empty `public` schema and leaves one behind,
    so no file inherits another's tables or `alembic_version` stamp.
    """
    if not os.environ.get("SFS_TEST_PG_URL") or not _uses_shared_pg(request.path):
        yield
        return
    from tests.pg_helpers import reset_pg_schema

    reset_pg_schema()
    yield
    reset_pg_schema()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Group tests for parallel runs (`pytest -n auto --dist loadgroup`).

    Each file stays on one worker, as with `--dist loadfile`. Every file that
    uses the shared SFS_TEST_PG_URL database joins a single "postgres" group,
    because they create, drop and migrate the same schema.

    `tryfirst`: xdist's worker turns the `xdist_group` marker into the node id
    suffix in its own modifyitems hook, which otherwise runs before this one
    and sees no marker, leaving every test free to land on any worker.
    """
    pg_files: dict[str, bool] = {}
    for item in items:
        path = str(item.path)
        if path not in pg_files:
            pg_files[path] = _uses_shared_pg(path)
        group = "postgres" if pg_files[path] else path
        item.add_marker(pytest.mark.xdist_group(group))
