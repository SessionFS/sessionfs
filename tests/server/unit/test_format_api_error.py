"""Unit tests for the CLI's format_api_error helper.

v0.10.24 tk_e7da4c4508d94bac — the helper parses the v0.10.x error
envelope shape so CLI error messages surface `error.message` instead of
the raw dict serialisation. najitestech (GH #51 ask #2) was the
trigger; this helper is the read-side companion to the server-side
IntegrityError handler.
"""

from __future__ import annotations

from sessionfs.cli.common import format_api_error


def test_envelope_with_code_and_message():
    body = {
        "error": {
            "code": "duplicate_resource",
            "message": "A resource with that value already exists.",
            "details": {"status": 409},
        }
    }
    assert (
        format_api_error(body, 409)
        == "duplicate_resource: A resource with that value already exists."
    )


def test_envelope_with_only_message():
    body = {"error": {"code": "", "message": "Something went wrong."}}
    assert format_api_error(body, 500) == "Something went wrong."


def test_envelope_with_only_code():
    body = {"error": {"code": "integrity_error", "message": ""}}
    assert format_api_error(body, 500) == "integrity_error"


def test_legacy_detail_string_shape():
    body = {"detail": "Persona 'atlas' not found"}
    assert format_api_error(body, 404) == "Persona 'atlas' not found"


def test_legacy_detail_dict_with_message():
    body = {"detail": {"message": "Boom", "code": "BOOM"}}
    assert format_api_error(body, 400) == "Boom"


def test_legacy_detail_dict_with_error():
    body = {"detail": {"error": "upgrade_required", "required_tier": "team"}}
    assert format_api_error(body, 403) == "upgrade_required"


def test_string_body_returns_as_is():
    assert format_api_error("Internal Server Error", 500) == "Internal Server Error"


def test_empty_dict_falls_back_to_str():
    # Defensive — no error key, no detail key. Don't crash, return
    # something printable.
    assert format_api_error({}, 500) == "{}"


def test_none_body_falls_back_to_str():
    assert format_api_error(None, 500) == "None"
