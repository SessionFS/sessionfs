"""Unit tests for the shared active-ticket provenance bundle."""

from __future__ import annotations

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
