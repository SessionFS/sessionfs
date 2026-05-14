"""Bedrock Action Group reference handler for SessionFS.

Bedrock invokes a Lambda for every action group call with an event of the form:

    {
        "messageVersion": "1.0",
        "agent": {...},
        "actionGroup": "sessionfs",
        "apiPath": "/api/v1/projects/{project_id}/tickets",
        "httpMethod": "GET",
        "parameters": [
            {"name": "project_id", "value": "proj_..."}
        ],
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": [
                        {"name": "title", "type": "string", "value": "..."}
                    ]
                }
            }
        }
    }

This handler dispatches by `apiPath` + `httpMethod`, calls the matching
SessionFS HTTP endpoint with Bearer auth, and returns a Bedrock-shaped
response.

Environment variables:
    SESSIONFS_API_URL  — base URL, e.g. https://api.sessionfs.dev
    SESSIONFS_API_KEY  — Bearer token; in production load from AWS Secrets
                         Manager rather than env, but the env path is
                         supported for quick testing.

This file is a reference. It deliberately uses only the stdlib +
`urllib.request` so it works in a vanilla AWS Lambda Python 3.12
runtime without packaging dependencies. Copy and adapt as needed for
your action group.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib import error, parse, request

logger = logging.getLogger("bedrock_sessionfs")
logger.setLevel(logging.INFO)


def _api_base() -> str:
    base = os.environ.get("SESSIONFS_API_URL", "https://api.sessionfs.dev")
    return base.rstrip("/")


def _api_key() -> str:
    key = os.environ.get("SESSIONFS_API_KEY")
    if not key:
        raise RuntimeError(
            "SESSIONFS_API_KEY is not set. In production, fetch it from "
            "AWS Secrets Manager before invoking this handler."
        )
    return key


def _params_to_dict(parameters: list[dict[str, Any]] | None) -> dict[str, str]:
    """Bedrock passes path + query params as a list of {name, value, type}."""
    if not parameters:
        return {}
    return {p["name"]: str(p.get("value", "")) for p in parameters if "name" in p}


def _body_to_dict(request_body: dict[str, Any] | None) -> dict[str, Any]:
    """Bedrock passes JSON bodies as {content: {application/json: {properties: [...]}}}.

    Each property is `{name, type, value}`. We coerce based on `type`:
    - boolean → bool
    - integer / number → int / float
    - array → JSON-decoded list (Bedrock encodes arrays as JSON strings)
    - everything else → string
    """
    if not request_body:
        return {}
    content = request_body.get("content", {})
    json_block = content.get("application/json", {})
    props = json_block.get("properties", [])
    out: dict[str, Any] = {}
    for prop in props:
        name = prop.get("name")
        if not name:
            continue
        raw = prop.get("value")
        ptype = (prop.get("type") or "string").lower()
        if ptype == "boolean":
            out[name] = str(raw).strip().lower() in {"true", "1", "yes"}
        elif ptype == "integer":
            try:
                out[name] = int(raw)
            except (TypeError, ValueError):
                out[name] = raw
        elif ptype == "number":
            try:
                out[name] = float(raw)
            except (TypeError, ValueError):
                out[name] = raw
        elif ptype == "array":
            try:
                out[name] = json.loads(raw) if isinstance(raw, str) else list(raw or [])
            except (TypeError, ValueError, json.JSONDecodeError):
                out[name] = []
        else:
            out[name] = raw
    return out


def _http_request(
    method: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any] | list[Any] | str]:
    url = _api_base() + path
    if query:
        # Drop empty/None values — Bedrock often sends optional params with empty strings.
        q = {k: v for k, v in query.items() if v not in (None, "")}
        if q:
            url = f"{url}?{parse.urlencode(q)}"
    data: bytes | None = None
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — trusted URL from env
            raw = resp.read().decode("utf-8")
            status = resp.status
    except error.HTTPError as e:  # 4xx/5xx
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except error.URLError as e:
        logger.exception("SessionFS API transport error")
        return 599, f"transport error: {e.reason}"
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, raw


def _bedrock_response(
    event: dict[str, Any],
    *,
    status: int,
    body: Any,
) -> dict[str, Any]:
    """Return a Bedrock-compatible action group response."""
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", "sessionfs"),
            "apiPath": event.get("apiPath", ""),
            "httpMethod": event.get("httpMethod", "GET"),
            "httpStatusCode": status,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body) if not isinstance(body, str) else body,
                }
            },
        },
    }


# ── operationId-style dispatch table ────────────────────────────────────────
#
# Maps (METHOD, path-pattern) → callable(event_params, event_body) → (status, body).
# Path patterns use `{name}` for placeholders that Bedrock fills from
# `parameters`. We match by exact pattern after substitution; query strings
# come in via `parameters` too, not the apiPath.


def _path_template_to_url(template: str, params: dict[str, str]) -> str:
    """Substitute {name} placeholders with corresponding params (and drop them)."""
    out = template
    consumed: list[str] = []
    for name, value in params.items():
        token = "{" + name + "}"
        if token in out:
            out = out.replace(token, parse.quote(str(value), safe=""))
            consumed.append(name)
    return out


def _split_path_and_query(template: str, params: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Return (resolved_path, leftover_params_as_query)."""
    placeholders = {n for n in params if "{" + n + "}" in template}
    resolved = _path_template_to_url(template, params)
    query = {k: v for k, v in params.items() if k not in placeholders}
    return resolved, query


# Operation table — keep in sync with bedrock-action-group.yaml.
OPERATIONS: dict[tuple[str, str], dict[str, Any]] = {
    ("GET", "/api/v1/projects/{project_id}/personas"): {
        "operationId": "listPersonas",
        "has_body": False,
    },
    ("GET", "/api/v1/projects/{project_id}/personas/{name}"): {
        "operationId": "getPersona",
        "has_body": False,
    },
    ("GET", "/api/v1/projects/{project_id}/tickets"): {
        "operationId": "listTickets",
        "has_body": False,
    },
    ("POST", "/api/v1/projects/{project_id}/tickets"): {
        "operationId": "createTicket",
        "has_body": True,
    },
    ("POST", "/api/v1/projects/{project_id}/tickets/{ticket_id}/start"): {
        "operationId": "startTicket",
        "has_body": False,
    },
    ("POST", "/api/v1/projects/{project_id}/tickets/{ticket_id}/complete"): {
        "operationId": "completeTicket",
        "has_body": True,
    },
    ("POST", "/api/v1/projects/{project_id}/entries/add"): {
        "operationId": "addKnowledge",
        "has_body": True,
    },
    ("GET", "/api/v1/projects/{project_id}/entries"): {
        "operationId": "searchKnowledge",
        "has_body": False,
    },
    ("GET", "/api/v1/projects/{project_id}/rules"): {
        "operationId": "getRules",
        "has_body": False,
    },
    ("GET", "/api/v1/projects/{git_remote_normalized}"): {
        "operationId": "getProjectContext",
        "has_body": False,
    },
}


def dispatch(method: str, api_path: str, params: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
    """Pure function for testability — given Bedrock event fields, return
    (HTTP status, response body) by calling the SessionFS API.

    Exposed separately from `lambda_handler` so unit tests can monkeypatch
    `_http_request` and exercise the mapping without AWS plumbing.
    """
    op = OPERATIONS.get((method.upper(), api_path))
    if op is None:
        return 404, {
            "error": (
                f"Unknown action group operation: {method} {api_path}. "
                "Check bedrock-action-group.yaml for the supported set."
            )
        }
    resolved_path, query = _split_path_and_query(api_path, params)
    payload = body if op["has_body"] else None
    return _http_request(method.upper(), resolved_path, query=query, body=payload)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001 — Lambda contract
    """AWS Lambda entry point. Bedrock invokes this for every action call."""
    method = event.get("httpMethod", "GET")
    api_path = event.get("apiPath", "")
    params = _params_to_dict(event.get("parameters"))
    body = _body_to_dict(event.get("requestBody"))

    logger.info("Bedrock action: %s %s params=%s", method, api_path, list(params.keys()))
    status, response_body = dispatch(method, api_path, params, body)
    return _bedrock_response(event, status=status, body=response_body)
