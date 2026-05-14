"""Vertex/Gemini function declarations for SessionFS Cloud Agent Control Plane.

This file is a reference. The Gemini SDK (`google-generativeai` >= 0.7) expects
function declarations as `genai.protos.FunctionDeclaration`. We define them as
plain dicts so this file works without the SDK installed; convert with
`genai.protos.FunctionDeclaration(**decl)` in your agent runtime.

The companion handler `call_sessionfs_function(name, args)` accepts the
function-call request the model emits and translates it into a SessionFS
HTTP request with Bearer auth. Deploy the handler as a Cloud Function or
Cloud Run service that proxies tool calls from your Vertex/Gemini agent.

Environment variables:
    SESSIONFS_API_URL  — base URL, e.g. https://api.sessionfs.dev
    SESSIONFS_API_KEY  — Bearer token; in production load from
                         Google Secret Manager.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib import error, parse, request


# ── Function declarations ───────────────────────────────────────────────────
#
# Convert to genai.protos.FunctionDeclaration in your runtime. Each entry has:
# - name: the function call name the model emits
# - description: model-facing summary
# - parameters: OpenAPI-subset JSON schema (Vertex supports a constrained set)

FUNCTION_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "listPersonas",
        "description": (
            "List active agent personas defined in the SessionFS project. "
            "Use to discover which agent roles (atlas/prism/scribe/...) "
            "are available before assigning work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "SessionFS project id (proj_...)"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "getPersona",
        "description": (
            "Fetch a persona's full markdown content (role + content + "
            "specializations). Use to load a role before doing ad-hoc work "
            "or to inspect what a ticket's assignee represents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "name": {"type": "string", "description": "Persona name (ASCII 1-50 chars)"},
            },
            "required": ["project_id", "name"],
        },
    },
    {
        "name": "listTickets",
        "description": "List tickets with optional filters (assigned_to, status, priority).",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "assigned_to": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["suggested", "open", "in_progress", "blocked", "review", "done", "cancelled"],
                },
                "priority": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "createTicket",
        "description": (
            "Create a new ticket. Agent-created tickets (source='agent') "
            "require >=1 acceptance criterion and a >=20-char description, "
            "max 3 per session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                },
                "assigned_to": {"type": "string"},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "file_refs": {"type": "array", "items": {"type": "string"}},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string", "enum": ["human", "agent"]},
                "created_by_session_id": {"type": "string"},
                "created_by_persona": {"type": "string"},
            },
            "required": ["project_id", "title"],
        },
    },
    {
        "name": "startTicket",
        "description": (
            "Start a ticket (atomic open → in_progress). Returns compiled "
            "persona + ticket context as markdown. Pass tool='gemini' for "
            "an 8k-token context budget."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "ticket_id": {"type": "string"},
                "tool": {
                    "type": "string",
                    "description": "Token-budget hint (gemini=8k, generic=8k)",
                },
                "force": {"type": "boolean"},
            },
            "required": ["project_id", "ticket_id"],
        },
    },
    {
        "name": "completeTicket",
        "description": "Complete a ticket (in_progress → review) with notes and changed files.",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "ticket_id": {"type": "string"},
                "notes": {"type": "string"},
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "knowledge_entry_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["project_id", "ticket_id", "notes"],
        },
    },
    {
        "name": "addKnowledge",
        "description": (
            "Persist a single knowledge entry. Minimum 20 chars; auto-"
            "promotes to 'claim' at confidence >= 0.8 and content >= 50 chars."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "content": {"type": "string"},
                "entry_type": {
                    "type": "string",
                    "enum": ["decision", "pattern", "discovery", "convention", "bug", "dependency"],
                },
                "confidence": {"type": "number"},
                "session_id": {"type": "string"},
                "source_context": {"type": "string"},
                "entity_ref": {"type": "string"},
                "entity_type": {"type": "string"},
                "force_claim": {"type": "boolean"},
            },
            "required": ["project_id", "content", "entry_type"],
        },
    },
    {
        "name": "searchKnowledge",
        "description": (
            "Case-insensitive substring search across knowledge entries with "
            "optional type / claim_class / freshness filters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "search": {"type": "string"},
                "type": {
                    "type": "string",
                    "enum": ["decision", "pattern", "discovery", "convention", "bug", "dependency"],
                },
                "claim_class": {
                    "type": "string",
                    "enum": ["evidence", "claim", "note"],
                },
                "freshness_class": {
                    "type": "string",
                    "enum": ["current", "aging", "stale", "superseded"],
                },
                "limit": {"type": "integer"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "getRules",
        "description": (
            "Fetch canonical project rules + compilation config. Cloud agents "
            "should read this before drafting code to respect project-wide "
            "rules (architecture decisions, do-not-violate constraints)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "getProjectContext",
        "description": (
            "Resolve project by normalized git remote (host/owner/repo, no "
            ".git). Returns the full project record including the compiled "
            "context document."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "git_remote_normalized": {
                    "type": "string",
                    "description": "e.g. github.com/acme/repo",
                },
            },
            "required": ["git_remote_normalized"],
        },
    },
]


# ── Reference handler ───────────────────────────────────────────────────────


def _api_base() -> str:
    base = os.environ.get("SESSIONFS_API_URL", "https://api.sessionfs.dev")
    return base.rstrip("/")


def _api_key() -> str:
    key = os.environ.get("SESSIONFS_API_KEY")
    if not key:
        raise RuntimeError(
            "SESSIONFS_API_KEY is not set. In production, fetch it from "
            "Google Secret Manager before invoking this handler."
        )
    return key


def _http(
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, Any]:
    url = _api_base() + path
    if query:
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
        with request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — env-derived
            raw = resp.read().decode("utf-8")
            status = resp.status
    except error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except error.URLError as e:
        return 599, {"error": f"transport error: {e.reason}"}
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, raw


# Map function name → (http_method, path_template, path_param_keys, body_param_keys, query_param_keys).
# Anything not in path/body keys is dropped (Vertex sometimes passes extras).
DISPATCH: dict[str, tuple[str, str, set[str], set[str], set[str]]] = {
    "listPersonas":     ("GET",  "/api/v1/projects/{project_id}/personas",                 {"project_id"}, set(), set()),
    "getPersona":       ("GET",  "/api/v1/projects/{project_id}/personas/{name}",          {"project_id", "name"}, set(), set()),
    "listTickets":      ("GET",  "/api/v1/projects/{project_id}/tickets",                  {"project_id"}, set(), {"assigned_to", "status", "priority"}),
    "createTicket":     ("POST", "/api/v1/projects/{project_id}/tickets",                  {"project_id"}, {"title", "description", "priority", "assigned_to", "acceptance_criteria", "file_refs", "depends_on", "source", "created_by_session_id", "created_by_persona"}, set()),
    "startTicket":      ("POST", "/api/v1/projects/{project_id}/tickets/{ticket_id}/start", {"project_id", "ticket_id"}, set(), {"tool", "force"}),
    "completeTicket":   ("POST", "/api/v1/projects/{project_id}/tickets/{ticket_id}/complete", {"project_id", "ticket_id"}, {"notes", "changed_files", "knowledge_entry_ids", "resolver_session_id"}, set()),
    "addKnowledge":     ("POST", "/api/v1/projects/{project_id}/entries/add",               {"project_id"}, {"content", "entry_type", "confidence", "session_id", "source_context", "entity_ref", "entity_type", "force_claim"}, set()),
    "searchKnowledge":  ("GET",  "/api/v1/projects/{project_id}/entries",                   {"project_id"}, set(), {"search", "type", "claim_class", "freshness_class", "limit"}),
    "getRules":         ("GET",  "/api/v1/projects/{project_id}/rules",                    {"project_id"}, set(), set()),
    "getProjectContext":("GET",  "/api/v1/projects/{git_remote_normalized}",               {"git_remote_normalized"}, set(), set()),
}


def call_sessionfs_function(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Translate a Vertex/Gemini function call into a SessionFS HTTP request.

    Returns the parsed JSON response (or a {"error": ...} dict on transport
    failure / unknown function). Use this in your Cloud Function or Cloud
    Run service that fronts the Vertex agent.
    """
    spec = DISPATCH.get(name)
    if spec is None:
        return {"error": f"Unknown SessionFS function: {name!r}"}
    method, template, path_keys, body_keys, query_keys = spec

    path = template
    for k in path_keys:
        v = args.get(k)
        if v is None or v == "":
            return {"error": f"Missing required path parameter: {k}"}
        path = path.replace("{" + k + "}", parse.quote(str(v), safe=""))

    body = {k: args[k] for k in body_keys if k in args and args[k] is not None}
    query = {k: args[k] for k in query_keys if k in args and args[k] not in (None, "")}

    status, payload = _http(
        method,
        path,
        query=query if query else None,
        body=body if (method in {"POST", "PUT"}) else None,
    )
    if isinstance(payload, dict):
        return {"status": status, **({"data": payload} if status < 400 else {"error": payload})}
    return {"status": status, "data": payload}


# ── Cloud Function entry point ──────────────────────────────────────────────
#
# Wire this to a Cloud Function with HTTP trigger. The Vertex agent's
# function-calling runtime posts {"name": "...", "args": {...}} to the URL.

def cloud_function_entrypoint(request_obj: Any) -> tuple[str, int, dict[str, str]]:
    """Generic HTTP handler — accepts a Flask-style request and returns
    (body, status, headers). Adapt to your Cloud Function flavor."""
    try:
        payload = request_obj.get_json(silent=True) or {}
    except AttributeError:
        payload = {}
    name = payload.get("name", "")
    args = payload.get("args", {}) or {}
    result = call_sessionfs_function(name, args)
    return json.dumps(result), 200, {"Content-Type": "application/json"}
