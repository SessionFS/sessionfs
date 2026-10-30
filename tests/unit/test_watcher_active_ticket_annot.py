"""Unit tests for watchers/active_ticket_annot.py — v0.10.1 Phase 6.

The helper reads ~/.sessionfs/active_ticket.json and tags a captured
.sfs session's manifest with `ticket_id` + `persona_name`. Every test
monkeypatches `sessionfs.active_ticket.bundle_path` so no real home
directory is touched.
"""

from __future__ import annotations

from sessionfs.active_ticket import workspace_bundle_path as _wbp
import json
from pathlib import Path

from sessionfs import active_ticket as at
from sessionfs.watchers.active_ticket_annot import (
    annotate_manifest_with_active_ticket,
)


def _make_session_dir(tmp_path: Path, manifest: dict | None) -> Path:
    session_dir = tmp_path / "ses_x"
    session_dir.mkdir()
    if manifest is not None:
        (session_dir / "manifest.json").write_text(json.dumps(manifest))
    return session_dir


def _make_workspace_json(session_dir: Path, root_path: str, remote_url: str) -> None:
    """Write a workspace.json carrying a root path and git remote."""
    (session_dir / "workspace.json").write_text(json.dumps({
        "root_path": root_path,
        "git": {"remote_url": remote_url, "branch": "main"},
    }))


def test_no_bundle_leaves_manifest_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "bundle_path", lambda: tmp_path / "missing.json")
    base_manifest = {"session_id": "ses_x", "title": "untagged"}
    session_dir = _make_session_dir(tmp_path, base_manifest)

    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert "ticket_id" not in out
    assert "persona_name" not in out
    assert out["title"] == "untagged"


def test_bundle_present_tags_manifest(tmp_path, monkeypatch):
    bundle = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: bundle)
    at.write_bundle(
        ticket_id="tk_42",
        persona_name="atlas",
        project_id="proj_a",
        retrieval_audit_id="ra_123",
    )

    session_dir = _make_session_dir(tmp_path, {"session_id": "ses_x", "title": "tagged"})

    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert out["ticket_id"] == "tk_42"
    assert out["persona_name"] == "atlas"
    assert out["retrieval_audit_id"] == "ra_123"
    # Pre-existing fields preserved.
    assert out["title"] == "tagged"


def test_bundle_without_persona_only_writes_ticket(tmp_path, monkeypatch):
    """Tickets can be unassigned — persona may be absent. Manifest should
    still gain ticket_id but no persona_name key."""
    bundle = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: bundle)
    at.write_bundle(ticket_id="tk_42", persona_name=None, project_id="proj_a")

    session_dir = _make_session_dir(tmp_path, {"session_id": "ses_x"})
    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert out["ticket_id"] == "tk_42"
    assert "persona_name" not in out


def test_missing_manifest_is_noop(tmp_path, monkeypatch):
    """No manifest.json in session_dir → silent no-op."""
    bundle = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: bundle)
    at.write_bundle(ticket_id="tk_42", persona_name="atlas", project_id="proj_a")

    session_dir = tmp_path / "ses_x"
    session_dir.mkdir()

    annotate_manifest_with_active_ticket(session_dir)  # must not raise
    assert not (session_dir / "manifest.json").exists()


def test_corrupt_manifest_is_noop(tmp_path, monkeypatch):
    """Malformed manifest.json must not break capture — leave it alone."""
    bundle = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: bundle)
    at.write_bundle(ticket_id="tk_42", persona_name="atlas", project_id="proj_a")

    session_dir = tmp_path / "ses_x"
    session_dir.mkdir()
    corrupt = "{not valid json"
    (session_dir / "manifest.json").write_text(corrupt)

    annotate_manifest_with_active_ticket(session_dir)

    # Manifest left untouched — better than half-rewriting it.
    assert (session_dir / "manifest.json").read_text() == corrupt


def test_bundle_with_no_ticket_id_tags_persona_only(tmp_path, monkeypatch):
    """v0.10.1 Phase 8: a persona-only bundle (no ticket_id) tags the
    manifest with persona_name. Previously this was a no-op."""
    bundle = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: bundle)
    bundle.write_text(json.dumps({
        "ticket_id": None,
        "persona_name": "atlas",
        "project_id": "proj_a",
        "started_at": "2026-05-13T00:00:00Z",
    }))

    session_dir = _make_session_dir(tmp_path, {"session_id": "ses_x"})
    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert "ticket_id" not in out
    assert out["persona_name"] == "atlas"


def test_bundle_with_neither_field_is_noop(tmp_path, monkeypatch):
    """A bundle with neither ticket_id nor persona_name is a no-op."""
    bundle = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: bundle)
    bundle.write_text(json.dumps({"project_id": "proj_a"}))

    session_dir = _make_session_dir(tmp_path, {"session_id": "ses_x"})
    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert "ticket_id" not in out
    assert "persona_name" not in out


# ── G1 — per-worker / project match ──


def test_matching_remote_tags_manifest(tmp_path, monkeypatch):
    """A bundle tagged with the workspace's remote tags that session."""
    bundle = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: bundle)
    at.write_bundle(
        ticket_id="tk_42", persona_name="atlas", project_id="proj_a",
        git_remote="acme/repo",
    )
    session_dir = _make_session_dir(tmp_path, {"session_id": "ses_x", "title": "tagged"})
    _make_workspace_json(
        session_dir, root_path="/ws", remote_url="https://github.com/acme/repo.git",
    )

    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert out["ticket_id"] == "tk_42"
    assert out["persona_name"] == "atlas"


def test_mismatched_remote_skips_manifest(tmp_path, monkeypatch):
    """A bundle from a different project must not tag this session."""
    bundle = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: bundle)
    at.write_bundle(
        ticket_id="tk_42", persona_name="atlas", project_id="proj_a",
        git_remote="acme/repo",
    )
    session_dir = _make_session_dir(tmp_path, {"session_id": "ses_x", "title": "untagged"})
    _make_workspace_json(
        session_dir, root_path="/ws", remote_url="https://github.com/other/proj.git",
    )

    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert "ticket_id" not in out
    assert "persona_name" not in out
    assert out["title"] == "untagged"


def test_legacy_bundle_without_remote_still_tags(tmp_path, monkeypatch):
    """Pre-G1 bundles carry no git_remote and keep tagging (backwards compat)."""
    bundle = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: bundle)
    at.write_bundle(ticket_id="tk_42", persona_name="atlas", project_id="proj_a")
    session_dir = _make_session_dir(tmp_path, {"session_id": "ses_x"})
    _make_workspace_json(
        session_dir, root_path="/ws", remote_url="https://github.com/acme/repo.git",
    )

    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert out["ticket_id"] == "tk_42"


def test_workspace_local_bundle_used_for_annotation(tmp_path, monkeypatch):
    """The daemon prefers the workspace-local bundle over the home bundle."""
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
        "git_remote": "acme/ws-repo",
    }))

    session_dir = _make_session_dir(tmp_path, {"session_id": "ses_x"})
    _make_workspace_json(
        session_dir, root_path=str(ws),
        remote_url="https://github.com/acme/ws-repo.git",
    )

    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert out["ticket_id"] == "tk_ws"


def test_bundle_with_remote_skips_when_session_has_no_remote(tmp_path, monkeypatch):
    """A bundle carrying git_remote must NOT tag a session with no remote —
    we can't prove the session belongs to that project."""
    bundle = tmp_path / "active_ticket.json"
    monkeypatch.setattr(at, "bundle_path", lambda: bundle)
    at.write_bundle(
        ticket_id="tk_42", persona_name="atlas", project_id="proj_a",
        git_remote="acme/repo",
    )
    session_dir = _make_session_dir(
        tmp_path, {"session_id": "ses_x", "title": "untagged"}
    )
    _make_workspace_json(session_dir, root_path="/ws", remote_url="")

    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert "ticket_id" not in out
    assert "persona_name" not in out
    assert out["title"] == "untagged"


def test_worker_bundle_tags_daemon_without_worker_env(tmp_path, monkeypatch):
    """E2E shape of G1: a worker starts a ticket with SFS_AGENT_RUN_ID set,
    writing both the run-scoped home bundle and the workspace mirror. The
    daemon — running with NO worker env — reads the workspace-local bundle
    and tags the session captured in that workspace."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SFS_AGENT_RUN_ID", "r1")
    ws = tmp_path / "ws"
    at.write_bundle(
        ticket_id="tk_42", persona_name="atlas", project_id="proj_a",
        git_remote="acme/repo", workspace=ws,
    )

    # The daemon has no SFS_AGENT_RUN_ID.
    monkeypatch.delenv("SFS_AGENT_RUN_ID", raising=False)
    session_dir = _make_session_dir(tmp_path, {"session_id": "ses_x"})
    _make_workspace_json(
        session_dir, root_path=str(ws),
        remote_url="https://github.com/acme/repo.git",
    )

    annotate_manifest_with_active_ticket(session_dir)

    out = json.loads((session_dir / "manifest.json").read_text())
    assert out["ticket_id"] == "tk_42"
    assert out["persona_name"] == "atlas"
