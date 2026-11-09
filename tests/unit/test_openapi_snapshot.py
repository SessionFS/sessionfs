"""OpenAPI snapshot drift test (tk_3855ef8b11f94ee2).

``docs/api/openapi.json`` is the committed contract of the HTTP API. This
test regenerates it and fails with a classified, readable diff when the
code no longer matches.
"""

from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("fastapi")

from sessionfs.server import openapi_snapshot as snap  # noqa: E402


def test_openapi_snapshot_is_current():
    new_doc = snap.build_openapi()
    new_text = snap.render(new_doc)
    assert snap.SNAPSHOT_PATH.exists(), (
        f"{snap.SNAPSHOT_PATH} is missing. Generate it with "
        "`PYTHONPATH=src python -m sessionfs.server.openapi_snapshot`."
    )
    old_text = snap.SNAPSHOT_PATH.read_text()
    if old_text == new_text:
        return
    diff = snap.classify(json.loads(old_text), new_doc)
    headline = (
        "The API contract has BREAKING changes against docs/api/openapi.json."
        if diff.breaking
        else "docs/api/openapi.json is stale (compatible changes only)."
    )
    pytest.fail(
        f"{headline}\n\n{snap.report(diff)}\n\n{snap.HOW_TO_FIX}\n\n"
        f"{snap.unified_diff(old_text, new_text, limit=80)}",
        pytrace=False,
    )


def test_snapshot_is_deterministic():
    first = snap.render(snap.build_openapi())
    second = snap.render(snap.build_openapi())
    assert first == second
    assert '"version"' not in json.dumps(json.loads(first)["info"])


# ── classifier ───────────────────────────────────────────────────


def _doc() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "t"},
        "paths": {
            "/api/v1/things": {
                "post": {
                    "parameters": [
                        {"in": "query", "name": "dry_run", "required": False, "schema": {"type": "boolean"}}
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ThingIn"}}},
                    },
                    "responses": {
                        "201": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ThingOut"}}}}
                    },
                },
                "get": {"responses": {"200": {"description": "ok"}}},
            }
        },
        "components": {
            "schemas": {
                "ThingIn": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "count": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    },
                    "required": ["title"],
                },
                "ThingOut": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
                },
            }
        },
    }


def test_identical_docs_have_no_changes():
    assert not snap.classify(_doc(), _doc()).changed


def test_removed_method_is_breaking():
    new = _doc()
    del new["paths"]["/api/v1/things"]["get"]
    diff = snap.classify(_doc(), new)
    assert diff.breaking == ["GET /api/v1/things: removed"]


def test_removed_path_is_breaking():
    new = _doc()
    del new["paths"]["/api/v1/things"]
    assert len(snap.classify(_doc(), new).breaking) == 2


def test_renamed_request_field_is_breaking():
    new = _doc()
    props = new["components"]["schemas"]["ThingIn"]["properties"]
    props["name"] = props.pop("title")
    new["components"]["schemas"]["ThingIn"]["required"] = ["name"]
    diff = snap.classify(_doc(), new)
    assert any("'title' removed or renamed" in b for b in diff.breaking)
    assert any("new required request field 'name'" in b for b in diff.breaking)


def test_removed_response_field_is_breaking():
    new = _doc()
    del new["components"]["schemas"]["ThingOut"]["properties"]["title"]
    diff = snap.classify(_doc(), new)
    assert diff.breaking == ["POST /api/v1/things -> 201: response field 'title' removed or renamed"]


def test_type_change_is_breaking():
    new = _doc()
    new["components"]["schemas"]["ThingIn"]["properties"]["title"] = {"type": "integer"}
    diff = snap.classify(_doc(), new)
    assert diff.breaking == ["POST /api/v1/things body.title: type ['string'] -> ['integer']"]


def test_request_field_becoming_required_is_breaking():
    new = _doc()
    new["components"]["schemas"]["ThingIn"]["required"] = ["title", "count"]
    diff = snap.classify(_doc(), new)
    assert diff.breaking == ["POST /api/v1/things body: request field 'count' became required"]


def test_new_required_query_parameter_is_breaking():
    new = _doc()
    new["paths"]["/api/v1/things"]["post"]["parameters"].append(
        {"in": "query", "name": "mode", "required": True, "schema": {"type": "string"}}
    )
    diff = snap.classify(_doc(), new)
    assert diff.breaking == ["POST /api/v1/things: new required query parameter 'mode'"]


def test_additive_changes_are_not_breaking():
    new = copy.deepcopy(_doc())
    new["paths"]["/api/v1/other"] = {"get": {"responses": {"200": {"description": "ok"}}}}
    new["components"]["schemas"]["ThingIn"]["properties"]["note"] = {"type": "string"}
    new["components"]["schemas"]["ThingOut"]["properties"]["created_at"] = {"type": "string"}
    # Widening a request type (int -> int | string) is compatible.
    new["components"]["schemas"]["ThingIn"]["properties"]["count"] = {
        "anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]
    }
    diff = snap.classify(_doc(), new)
    assert diff.breaking == []
    assert "GET /api/v1/other: added" in diff.additive
    assert any("request field 'note' added" in a for a in diff.additive)
    assert any("response field 'created_at' added" in a for a in diff.additive)


def test_check_mode_and_refusal(tmp_path, monkeypatch, capsys):
    """--check fails on a stale file; a breaking write needs --accept-breaking."""
    out = tmp_path / "openapi.json"
    old = _doc()
    out.write_text(snap.render(old))
    new = _doc()
    del new["paths"]["/api/v1/things"]["get"]
    monkeypatch.setattr(snap, "build_openapi", lambda: new)
    monkeypatch.delenv(snap.ACCEPT_BREAKING_ENV, raising=False)

    assert snap.main(["--check", "--output", str(out)]) == 1
    assert snap.main(["--output", str(out)]) == 2
    assert json.loads(out.read_text()) == old
    assert snap.main(["--output", str(out), "--accept-breaking"]) == 0
    assert json.loads(out.read_text()) == new
    assert snap.main(["--check", "--output", str(out)]) == 0
