"""Unit tests for the shared active-ticket provenance bundle."""

from __future__ import annotations

from sessionfs.active_ticket import workspace_bundle_path as _wbp
import json
from pathlib import Path

import pytest

from sessionfs import active_ticket as at


@pytest.fixture
def tmp_bundle(tmp_path: Path, monkeypatch) -> Path:
    """Redirect bundle_path() to a tmp file."""
    p = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: p)
    return p


def test_read_bundle_returns_none_when_missing(tmp_bundle: Path):
    assert at.read_bundle() is None


def test_write_then_read_roundtrip(tmp_bundle: Path):
    ok = at.write_bundle(ticket_id="tk_x", persona_name="atlas", project_id="proj_a")
    assert ok is True
    bundle = at.read_bundle()
    assert bundle is not None
    assert bundle["ticket_id"] == "tk_x"
    assert bundle["persona_name"] == "atlas"
    assert bundle["project_id"] == "proj_a"
    assert "started_at" in bundle


def test_write_bundle_returns_false_on_oserror(tmp_path: Path, monkeypatch):
    """KB 339 LOW — write_bundle must signal failure so callers warn."""
    bogus = tmp_path / "dir-conflicts" / "active.json"
    # Create a *file* where the parent dir is expected, which makes
    # mkdir(parents=True) raise NotADirectoryError (a subclass of OSError).
    (tmp_path / "dir-conflicts").write_text("blocking file")
    monkeypatch.setattr(at, "bundle_path", lambda: bogus)
    ok = at.write_bundle(ticket_id="tk_x", persona_name=None, project_id="proj_a")
    assert ok is False


def test_write_creates_parent_dir(tmp_path: Path, monkeypatch):
    nested = tmp_path / "sub" / "deeper" / "active.json"
    monkeypatch.setattr(at, "bundle_path", lambda: nested)
    at.write_bundle(ticket_id="tk_x", persona_name=None, project_id="proj_a")
    assert nested.exists()


def test_read_bundle_returns_none_on_invalid_json(tmp_bundle: Path):
    tmp_bundle.write_text("{not valid json")
    assert at.read_bundle() is None


def test_read_bundle_returns_none_on_non_dict(tmp_bundle: Path):
    tmp_bundle.write_text(json.dumps(["list", "not", "dict"]))
    assert at.read_bundle() is None


def test_clear_bundle_removes_matching(tmp_bundle: Path):
    at.write_bundle(ticket_id="tk_x", persona_name="atlas", project_id="proj_a")
    removed = at.clear_bundle_if_owned(ticket_id="tk_x", project_id="proj_a")
    assert removed is True
    assert not tmp_bundle.exists()


def test_clear_bundle_preserves_other_ticket(tmp_bundle: Path):
    at.write_bundle(ticket_id="tk_OTHER", persona_name="atlas", project_id="proj_a")
    removed = at.clear_bundle_if_owned(ticket_id="tk_ME", project_id="proj_a")
    assert removed is False
    assert tmp_bundle.exists()


def test_clear_bundle_preserves_other_project(tmp_bundle: Path):
    at.write_bundle(ticket_id="tk_ME", persona_name="atlas", project_id="proj_OTHER")
    removed = at.clear_bundle_if_owned(ticket_id="tk_ME", project_id="proj_a")
    assert removed is False
    assert tmp_bundle.exists()


def test_clear_bundle_no_file_returns_false(tmp_bundle: Path):
    removed = at.clear_bundle_if_owned(ticket_id="tk_x", project_id="proj_a")
    assert removed is False


# ── v0.10.1 Phase 8 — persona-only bundles ──


def test_write_bundle_persona_only(tmp_bundle: Path):
    """assume_persona writes ticket_id=None + persona_name set."""
    ok = at.write_bundle(ticket_id=None, persona_name="atlas", project_id="proj_a")
    assert ok is True
    bundle = at.read_bundle()
    assert bundle is not None
    assert bundle["ticket_id"] is None
    assert bundle["persona_name"] == "atlas"


def test_write_bundle_rejects_empty_payload(tmp_bundle: Path):
    """Bundle needs either ticket_id or persona_name to be meaningful."""
    with pytest.raises(ValueError):
        at.write_bundle(ticket_id=None, persona_name=None, project_id="proj_a")


def test_clear_bundle_removes_unconditionally(tmp_bundle: Path):
    """forget_persona clears without ownership check."""
    at.write_bundle(ticket_id=None, persona_name="atlas", project_id="proj_a")
    assert at.clear_bundle() is True
    assert not tmp_bundle.exists()


def test_clear_bundle_returns_false_when_missing(tmp_bundle: Path):
    assert at.clear_bundle() is False


# ── G1 — per-worker bundle resolution ──


def test_write_bundle_stores_git_remote(tmp_bundle: Path):
    at.write_bundle(
        ticket_id="tk_x", persona_name="atlas", project_id="proj_a",
        git_remote="github.com/acme/repo",
    )
    bundle = at.read_bundle()
    assert bundle is not None
    assert bundle["git_remote"] == "github.com/acme/repo"


def test_write_bundle_omits_git_remote_when_none(tmp_bundle: Path):
    at.write_bundle(ticket_id="tk_x", persona_name="atlas", project_id="proj_a")
    assert "git_remote" not in at.read_bundle()


def test_bundle_path_env_override(tmp_path, monkeypatch):
    """SFS_ACTIVE_TICKET_PATH redirects both reads and writes."""
    target = tmp_path / "custom" / "active.json"
    monkeypatch.setenv("SFS_ACTIVE_TICKET_PATH", str(target))
    assert at.bundle_path() == target
    at.write_bundle(ticket_id="tk_x", persona_name="atlas", project_id="proj_a")
    assert target.exists()
    assert at.read_bundle()["ticket_id"] == "tk_x"


def test_bundle_path_agent_run_id(tmp_path, monkeypatch):
    """SFS_AGENT_RUN_ID scopes the bundle to a per-worker file."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SFS_AGENT_RUN_ID", "run_1")
    expected = tmp_path / ".sessionfs" / "agents" / "run_1" / "active_ticket.json"
    assert at.bundle_path() == expected


def test_agent_run_id_scopes_bundles_apart(tmp_path, monkeypatch):
    """Two workers with different SFS_AGENT_RUN_ID don't overwrite each other."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SFS_AGENT_RUN_ID", "run_a")
    at.write_bundle(ticket_id="tk_a", persona_name="atlas", project_id="proj_a")
    monkeypatch.setenv("SFS_AGENT_RUN_ID", "run_b")
    at.write_bundle(ticket_id="tk_b", persona_name="atlas", project_id="proj_b")

    monkeypatch.setenv("SFS_AGENT_RUN_ID", "run_a")
    assert at.read_bundle()["ticket_id"] == "tk_a"
    monkeypatch.setenv("SFS_AGENT_RUN_ID", "run_b")
    assert at.read_bundle()["ticket_id"] == "tk_b"


def test_read_bundle_prefers_workspace_local(tmp_path, monkeypatch):
    """A workspace-local bundle wins over the per-user home bundle."""
    home_bundle = tmp_path / "home" / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: home_bundle)
    at.write_bundle(ticket_id="tk_home", persona_name="atlas", project_id="proj_home")

    ws = tmp_path / "ws"
    (ws / ".sessionfs").mkdir(parents=True)
    _wbp(ws).parent.mkdir(parents=True, exist_ok=True)
    _wbp(ws).write_text(json.dumps({
        "ticket_id": "tk_ws",
        "persona_name": "atlas",
        "project_id": "proj_ws",
    }))

    assert at.read_bundle(workspace=ws)["ticket_id"] == "tk_ws"


def test_bundle_shipped_inside_the_checkout_is_ignored(tmp_path, monkeypatch):
    """A cloned repo's own .sessionfs/active_ticket.json never tags sessions:
    the per-workspace mirror lives under the user's home, not the checkout."""
    monkeypatch.setattr(at, "bundle_path", lambda: tmp_path / "home" / "none.json")
    ws = tmp_path / "ws"
    (ws / ".sessionfs").mkdir(parents=True)
    (ws / ".sessionfs" / "active_ticket.json").write_text(json.dumps({
        "ticket_id": "tk_planted",
        "persona_name": "atlas",
        "project_id": "proj_x",
    }))

    assert at.read_bundle(workspace=ws) is None
    assert not str(_wbp(ws)).startswith(str(ws))


def test_read_bundle_falls_back_when_workspace_bundle_missing(tmp_path, monkeypatch):
    """No workspace-local bundle → the home bundle is still read."""
    home_bundle = tmp_path / "home" / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: home_bundle)
    at.write_bundle(ticket_id="tk_home", persona_name="atlas", project_id="proj_home")

    assert at.read_bundle(workspace=tmp_path / "empty_ws")["ticket_id"] == "tk_home"


# ── G1 — workspace-local mirror when an env-selected path is in use ──


def test_write_bundle_mirrors_workspace_when_run_id_set(tmp_path, monkeypatch):
    """With SFS_AGENT_RUN_ID set, write_bundle ALSO writes the workspace-
    local bundle so the daemon (no worker env) can find it."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SFS_AGENT_RUN_ID", "run_1")
    ws = tmp_path / "ws"
    at.write_bundle(
        ticket_id="tk_x", persona_name="atlas", project_id="proj_a", workspace=ws
    )

    env_bundle = tmp_path / ".sessionfs" / "agents" / "run_1" / "active_ticket.json"
    ws_bundle = _wbp(ws)
    assert env_bundle.exists()
    assert ws_bundle.exists()
    assert json.loads(ws_bundle.read_text())["ticket_id"] == "tk_x"


def test_write_bundle_no_workspace_mirror_without_env(tmp_path, monkeypatch):
    """Without env selection the single home file is enough — no workspace
    file is written even when workspace is passed."""
    home = tmp_path / "home" / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: home)
    monkeypatch.delenv("SFS_ACTIVE_TICKET_PATH", raising=False)
    monkeypatch.delenv("SFS_AGENT_RUN_ID", raising=False)
    ws = tmp_path / "ws"
    at.write_bundle(
        ticket_id="tk_x", persona_name="atlas", project_id="proj_a", workspace=ws
    )
    assert home.exists()
    assert not (_wbp(ws)).exists()


def test_clear_bundle_if_owned_removes_workspace_mirror(tmp_path, monkeypatch):
    """complete_ticket clears the run-scoped home bundle AND the workspace
    mirror, so a stale workspace bundle doesn't tag later sessions."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SFS_AGENT_RUN_ID", "run_1")
    ws = tmp_path / "ws"
    at.write_bundle(
        ticket_id="tk_x", persona_name="atlas", project_id="proj_a", workspace=ws
    )
    env_bundle = tmp_path / ".sessionfs" / "agents" / "run_1" / "active_ticket.json"
    ws_bundle = _wbp(ws)
    assert env_bundle.exists() and ws_bundle.exists()

    removed = at.clear_bundle_if_owned(
        ticket_id="tk_x", project_id="proj_a", workspace=ws
    )
    assert removed is True
    assert not env_bundle.exists()
    assert not ws_bundle.exists()


# ── Bundle hardening — SFS_AGENT_RUN_ID validation ──


def test_env_bundle_path_rejects_invalid_run_ids(tmp_path, monkeypatch):
    """A run id is joined into a path, so traversal/unsafe values must never
    produce a path — they fall back to no env selection."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    for bad in (".", "..", "a/b", "../escape", "back\\slash", "x" * 129):
        monkeypatch.setenv("SFS_AGENT_RUN_ID", bad)
        assert at._env_bundle_path() is None, bad


def test_bundle_path_accepts_valid_run_id_chars(tmp_path, monkeypatch):
    """Dots, dashes and underscores are legal run-id characters."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SFS_AGENT_RUN_ID", "worker.7-ab_c")
    assert at.bundle_path() == (
        tmp_path / ".sessionfs" / "agents" / "worker.7-ab_c" / "active_ticket.json"
    )
