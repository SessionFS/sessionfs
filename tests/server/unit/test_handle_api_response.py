"""Unit tests for the CLI's handle_api_response helper.

v0.10.24 tk_e7da4c4508d94bac Codex R1 MED #1 — the customer-facing
`sfs org create` (GH #51) goes through handle_api_response, and the
generic-error fallback (status >= 400 that doesn't match
upgrade_required/storage_limit/insufficient_role/seat_limit) must
parse the new v0.10.x envelope shape so users see
"foreign_key_violation: Database integrity error..." instead of bare
JSON.
"""

from __future__ import annotations

import json

import pytest

from sessionfs.cli.api_errors import handle_api_response


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | str, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if not isinstance(body, str) else body
        self.headers = headers or {"content-type": "application/json"}

    def json(self) -> dict:
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


def test_5xx_envelope_surfaces_code_and_message(capsys):
    """Codex R1 MED #1 regression — the GH #51 customer-facing path."""
    resp = _FakeResponse(
        500,
        {
            "error": {
                "code": "foreign_key_violation",
                "message": "Database integrity error: a referenced row was missing.",
                "details": {"status": 500},
            }
        },
    )
    with pytest.raises(SystemExit) as exc_info:
        handle_api_response(resp)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    # The user must see the structured envelope's code + message, NOT
    # the raw JSON dict or bare "Internal Server Error".
    assert "foreign_key_violation" in err
    assert "Database integrity error" in err
    # No raw JSON braces — the helper extracted the message cleanly.
    assert '{"error"' not in err


def test_4xx_envelope_surfaces_code_and_message(capsys):
    """409 unique-violation envelope (the IntegrityError handler's
    duplicate_resource path) renders cleanly too."""
    resp = _FakeResponse(
        409,
        {
            "error": {
                "code": "duplicate_resource",
                "message": "A resource with that value already exists.",
            }
        },
    )
    with pytest.raises(SystemExit):
        handle_api_response(resp)
    err = capsys.readouterr().err
    assert "duplicate_resource" in err
    assert "already exists" in err


def test_legacy_detail_string_still_works(capsys):
    """Legacy {"detail": "..."} shape (older FastAPI defaults) renders
    correctly via the same format_api_error fallback."""
    resp = _FakeResponse(400, {"detail": "Persona 'atlas' not found"})
    with pytest.raises(SystemExit):
        handle_api_response(resp)
    err = capsys.readouterr().err
    assert "Persona 'atlas' not found" in err


def test_plain_text_5xx_falls_back_to_body(capsys):
    """Bare 'Internal Server Error' plain-text response (the original
    failure mode that this whole ticket fixes) still prints something
    rather than crashing."""

    class _PlainTextResp:
        status_code = 500
        text = "Internal Server Error"
        headers: dict = {}

        def json(self) -> dict:
            raise ValueError("not json")

    with pytest.raises(SystemExit):
        handle_api_response(_PlainTextResp())
    err = capsys.readouterr().err
    assert "Internal Server Error" in err


def test_2xx_does_not_exit(capsys):
    """Sanity check — handle_api_response only fires on >= 400."""
    resp = _FakeResponse(200, {"ok": True})
    # No raise expected.
    handle_api_response(resp)
    # No stderr output either.
    assert capsys.readouterr().err == ""


def test_403_upgrade_required_still_special_cased(capsys):
    """Pre-existing tier-aware special case must keep working — the
    new envelope parsing is for the fallback branch only, not the
    handle_api_response's specialised upgrade_required path."""
    resp = _FakeResponse(
        403,
        {"detail": {"error": "upgrade_required", "required_tier": "team", "current_tier": "free"}},
    )
    with pytest.raises(SystemExit) as exc_info:
        handle_api_response(resp)
    # upgrade_required exits 0 (it's a "please upgrade" prompt, not a
    # crash) — pre-existing contract that this fix must not break.
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "team" in out
