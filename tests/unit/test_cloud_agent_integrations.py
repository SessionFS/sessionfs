"""Validation for the Cloud Agent Control Plane integration artifacts.

Three goals:
1. The Bedrock OpenAPI spec is parseable YAML and structurally sane.
2. Every operationId in the spec matches an entry in the Bedrock Lambda
   dispatch table, and the (method, path) pairs agree.
3. Every operationId is also present in the Vertex DISPATCH table, and
   path templates / methods agree across all three artifacts.

We deliberately import the reference handlers as plain Python files via
importlib.util so they live under docs/integrations/ (not src/) and
remain copy-pasteable into AWS Lambda / Cloud Functions.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "docs" / "integrations" / "bedrock-action-group.yaml"
LAMBDA_PATH = REPO_ROOT / "docs" / "integrations" / "bedrock_lambda.py"
VERTEX_PATH = REPO_ROOT / "docs" / "integrations" / "vertex_tools.py"


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text())


def _load_module(name: str, path: Path):
    spec_ = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def bedrock():
    return _load_module("bedrock_lambda_ref", LAMBDA_PATH)


@pytest.fixture(scope="module")
def vertex():
    return _load_module("vertex_tools_ref", VERTEX_PATH)


# ── OpenAPI spec sanity ─────────────────────────────────────────────────────


def test_spec_is_openapi_3(spec: dict):
    assert spec["openapi"].startswith("3.0")
    assert "paths" in spec
    assert spec["paths"], "Spec must declare at least one path"


def test_every_operation_has_operationid(spec: dict):
    for path, item in spec["paths"].items():
        for method in ("get", "post", "put", "delete"):
            if method in item:
                op = item[method]
                assert "operationId" in op, f"{method.upper()} {path} missing operationId"
                assert "summary" in op, f"{method.upper()} {path} missing summary"


def test_bedrock_has_global_bearer_security(spec: dict):
    assert spec.get("security") == [{"BearerAuth": []}]
    assert "BearerAuth" in spec["components"]["securitySchemes"]


def test_paths_are_real_sessionfs_routes(spec: dict):
    """Every path in the spec must start with /api/v1/projects/{...}.

    Catches typos (/api/v1/project/, missing /api prefix, etc).
    """
    for path in spec["paths"]:
        assert path.startswith("/api/v1/projects/"), f"Unexpected path prefix: {path}"


# ── Bedrock dispatch table parity with spec ─────────────────────────────────


def _spec_operations(spec: dict) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for path, item in spec["paths"].items():
        for method in ("get", "post", "put", "delete"):
            if method in item:
                out.add((method.upper(), path))
    return out


def test_bedrock_dispatch_covers_every_spec_operation(spec: dict, bedrock):
    spec_ops = _spec_operations(spec)
    dispatch_ops = set(bedrock.OPERATIONS.keys())
    missing_in_dispatch = spec_ops - dispatch_ops
    missing_in_spec = dispatch_ops - spec_ops
    assert not missing_in_dispatch, (
        f"Operations in OpenAPI but not in Bedrock OPERATIONS: {missing_in_dispatch}"
    )
    assert not missing_in_spec, (
        f"Operations in Bedrock OPERATIONS but not in OpenAPI: {missing_in_spec}"
    )


def test_bedrock_operation_ids_match_spec(spec: dict, bedrock):
    spec_op_ids = {
        (method.upper(), path): item[method]["operationId"]
        for path, item in spec["paths"].items()
        for method in ("get", "post", "put", "delete")
        if method in item
    }
    for key, entry in bedrock.OPERATIONS.items():
        assert spec_op_ids[key] == entry["operationId"], (
            f"operationId mismatch at {key}: "
            f"spec={spec_op_ids[key]} vs lambda={entry['operationId']}"
        )


# ── Bedrock handler unit tests (no network) ─────────────────────────────────


def test_bedrock_dispatch_unknown_op_returns_404(bedrock, monkeypatch):
    """Unknown apiPath must surface a 404 + explanatory error, not crash."""
    monkeypatch.setattr(
        bedrock,
        "_http_request",
        lambda *a, **kw: (200, {}),  # would-be success — proves we never called it
    )
    status, body = bedrock.dispatch("GET", "/api/v1/unknown", {}, {})
    assert status == 404
    assert "Unknown action group operation" in body["error"]


def test_bedrock_dispatch_substitutes_path_params(bedrock, monkeypatch):
    captured: dict = {}

    def fake_http(method, path, **kw):
        captured["method"] = method
        captured["path"] = path
        captured["query"] = kw.get("query")
        captured["body"] = kw.get("body")
        return 200, {"id": "tk_x", "status": "open"}

    monkeypatch.setattr(bedrock, "_http_request", fake_http)
    bedrock.dispatch(
        "GET",
        "/api/v1/projects/{project_id}/personas/{name}",
        {"project_id": "proj_abc", "name": "atlas"},
        {},
    )
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/projects/proj_abc/personas/atlas"


def test_bedrock_dispatch_routes_query_params(bedrock, monkeypatch):
    """List endpoints pass non-path params through as query string."""
    captured: dict = {}

    def fake_http(method, path, **kw):
        captured["query"] = kw.get("query")
        return 200, []

    monkeypatch.setattr(bedrock, "_http_request", fake_http)
    bedrock.dispatch(
        "GET",
        "/api/v1/projects/{project_id}/tickets",
        {"project_id": "proj_abc", "assigned_to": "atlas", "status": "open"},
        {},
    )
    assert captured["query"] == {"assigned_to": "atlas", "status": "open"}


def test_bedrock_dispatch_sends_body_only_when_op_has_body(bedrock, monkeypatch):
    seen_bodies: list = []
    monkeypatch.setattr(
        bedrock,
        "_http_request",
        lambda method, path, **kw: (seen_bodies.append(kw.get("body")), (200, {}))[1],
    )
    # POST createTicket — has_body=True
    bedrock.dispatch(
        "POST",
        "/api/v1/projects/{project_id}/tickets",
        {"project_id": "proj_abc"},
        {"title": "x"},
    )
    # GET listPersonas — has_body=False
    bedrock.dispatch(
        "GET",
        "/api/v1/projects/{project_id}/personas",
        {"project_id": "proj_abc"},
        {"should_be_dropped": "yes"},
    )
    assert seen_bodies == [{"title": "x"}, None]


def test_bedrock_param_value_coercion(bedrock):
    """Bedrock encodes boolean / integer / array params with a `type` hint."""
    body = bedrock._body_to_dict({
        "content": {
            "application/json": {
                "properties": [
                    {"name": "force", "type": "boolean", "value": "true"},
                    {"name": "limit", "type": "integer", "value": "25"},
                    {"name": "confidence", "type": "number", "value": "0.85"},
                    {"name": "tags", "type": "array", "value": '["a","b"]'},
                    {"name": "title", "type": "string", "value": "Fix the thing"},
                ]
            }
        }
    })
    assert body == {
        "force": True,
        "limit": 25,
        "confidence": 0.85,
        "tags": ["a", "b"],
        "title": "Fix the thing",
    }


# ── Vertex parity ───────────────────────────────────────────────────────────


def test_vertex_declarations_match_dispatch(vertex):
    decl_names = {d["name"] for d in vertex.FUNCTION_DECLARATIONS}
    dispatch_names = set(vertex.DISPATCH.keys())
    assert decl_names == dispatch_names, (
        f"Mismatch between FUNCTION_DECLARATIONS and DISPATCH: "
        f"only-in-decl={decl_names - dispatch_names}, "
        f"only-in-dispatch={dispatch_names - decl_names}"
    )


def test_vertex_dispatch_covers_every_spec_operation(spec: dict, vertex):
    """operationId from OpenAPI must also be a Vertex function name."""
    spec_op_ids = {
        item[method]["operationId"]
        for path, item in spec["paths"].items()
        for method in ("get", "post", "put", "delete")
        if method in item
    }
    dispatch_names = set(vertex.DISPATCH.keys())
    assert spec_op_ids == dispatch_names


def test_vertex_path_templates_match_spec(spec: dict, vertex):
    spec_method_path: dict[str, tuple[str, str]] = {}
    for path, item in spec["paths"].items():
        for method in ("get", "post", "put", "delete"):
            if method in item:
                spec_method_path[item[method]["operationId"]] = (method.upper(), path)
    for op_id, (method, template, _path_keys, _body_keys, _query_keys) in vertex.DISPATCH.items():
        spec_method, spec_path = spec_method_path[op_id]
        assert method == spec_method, f"{op_id}: method drift {method} vs {spec_method}"
        assert template == spec_path, f"{op_id}: path drift {template} vs {spec_path}"


def test_vertex_dispatch_path_keys_match_template_placeholders(vertex):
    """If the path template has `{name}`, name must appear in path_keys.

    Catches the bug where someone adds a new operation but forgets to
    list a path placeholder in the dispatch tuple.
    """
    placeholder_re = re.compile(r"\{([^}]+)\}")
    for op_id, (_method, template, path_keys, _body_keys, _query_keys) in vertex.DISPATCH.items():
        from_template = set(placeholder_re.findall(template))
        assert from_template == path_keys, (
            f"{op_id}: template placeholders {from_template} "
            f"vs dispatch path_keys {path_keys}"
        )


def test_vertex_call_returns_error_for_unknown_function(vertex):
    result = vertex.call_sessionfs_function("nonexistent_op", {})
    assert "error" in result
    assert "Unknown SessionFS function" in result["error"]


def test_vertex_call_rejects_missing_path_param(vertex):
    # listPersonas requires project_id; pass none.
    result = vertex.call_sessionfs_function("listPersonas", {})
    assert "error" in result
    assert "project_id" in result["error"]


def test_server_recognizes_bedrock_and_vertex_tool_aliases():
    """Codex Round 2 MEDIUM: the cloud-agents docs document `tool=bedrock`
    for a Claude-class budget and `tool=vertex` for a Gemini-class budget.
    The server's _TOOL_TOKEN_LIMITS must actually contain those keys so
    callers don't silently fall back to the 8k generic budget.
    """
    from sessionfs.server.routes.tickets import _TOOL_TOKEN_LIMITS
    assert _TOOL_TOKEN_LIMITS.get("bedrock") == 16000, (
        "bedrock alias must map to the same budget as claude-code (16k)"
    )
    assert _TOOL_TOKEN_LIMITS.get("vertex") == 8000, (
        "vertex alias must map to the same budget as gemini (8k)"
    )


def test_vertex_call_builds_correct_request(vertex, monkeypatch):
    """End-to-end function-call → HTTP request shape, no network."""
    captured: dict = {}

    def fake_http(method, path, *, query=None, body=None, timeout=30):
        captured.update({"method": method, "path": path, "query": query, "body": body})
        return 200, {"id": 42, "content": "ok"}

    monkeypatch.setattr(vertex, "_http", fake_http)
    result = vertex.call_sessionfs_function(
        "addKnowledge",
        {
            "project_id": "proj_abc",
            "content": "Decided to ship cloud agent control plane in v0.10.1.",
            "entry_type": "decision",
            "confidence": 0.9,
        },
    )
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/projects/proj_abc/entries/add"
    assert captured["body"] == {
        "content": "Decided to ship cloud agent control plane in v0.10.1.",
        "entry_type": "decision",
        "confidence": 0.9,
    }
    assert result == {"status": 200, "data": {"id": 42, "content": "ok"}}
