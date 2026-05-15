"""Unit tests for watchers/active_ticket_annot.py — v0.10.1 Phase 6.

The helper reads ~/.sessionfs/active_ticket.json and tags a captured
.sfs session's manifest with `ticket_id` + `persona_name`. Every test
monkeypatches `sessionfs.active_ticket.bundle_path` so no real home
directory is touched.
"""

from __future__ import annotations

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
