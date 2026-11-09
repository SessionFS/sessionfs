"""MCP tool ↔ REST route contract drift test (tk_3855ef8b11f94ee2).

Every MCP tool that writes through a REST route is listed in
``MCP_WRITE_ROUTES`` with the route(s) it calls. Two checks keep the MCP
surface from drifting away from the API:

1. Every property in the tool's ``inputSchema`` is accepted by one of its
   routes (a body field or alias, a path parameter or a query parameter), is
   renamed onto one (``MCP_RENAMES``), or is a documented client-side-only
   argument (``CLIENT_ONLY`` / ``TOOL_CLIENT_ONLY``).
2. Every top-level key the handler puts into a JSON body (read statically
   from the handler source) is a field the route's body model accepts. This
   is the check that catches the next ``transcript`` vs ``content`` mismatch:
   a key the route would silently drop.

A third test makes sure a new MCP tool that sends a JSON body can't skip the
map.
"""

from __future__ import annotations

import ast
import inspect
from functools import lru_cache

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

from fastapi.routing import APIRoute  # noqa: E402

from sessionfs.mcp import server as mcp_server  # noqa: E402
from sessionfs.server.api_contract import (  # noqa: E402
    model_input_keys,
    raw_body_model,
    route_body_keys,
    route_body_models,
)

P = "/api/v1/projects/{project_id}"
T = f"{P}/tickets/{{ticket_id}}"
MA = "/api/v1/meetings/{meeting_id}/action-items/{item_id}"

# tool -> [(METHOD, route path template)]
MCP_WRITE_ROUTES: dict[str, list[tuple[str, str]]] = {
    "add_handoff_comment": [("POST", "/api/v1/handoffs/{handoff_id}/comments")],
    "add_knowledge": [("POST", f"{P}/entries/add")],
    "add_project_to_group": [("PUT", "/api/v1/orgs/{org_id}/groups/{group_id}/projects/{project_id}")],
    "add_ticket_comment": [("POST", f"{T}/comments")],
    "approve_ticket": [("POST", f"{T}/approve")],
    "ask_session_helper": [("POST", "/api/v1/helper/turns")],
    "assign_persona": [("PUT", T)],
    "claim_handoff": [("POST", "/api/v1/handoffs/{handoff_id}/claim")],
    "compile_group_knowledge": [("POST", "/api/v1/orgs/{org_id}/groups/{slug}/compile")],
    "compile_knowledge_base": [("POST", f"{P}/compile")],
    "complete_agent_run": [("POST", f"{P}/agent-runs/{{run_id}}/complete")],
    "complete_ticket": [("POST", f"{T}/complete")],
    "complete_work_queue_step": [("POST", f"{P}/work-queues/{{queue_id}}/step/complete")],
    "create_agent_run": [
        ("POST", f"{P}/agent-runs"),
        ("POST", f"{P}/agent-runs/{{run_id}}/start"),
    ],
    "create_external_contact": [("POST", "/api/v1/orgs/{org_id}/contacts")],
    "create_handoff": [("POST", "/api/v1/handoffs")],
    "create_persona": [
        ("POST", f"{P}/personas"),
        ("POST", "/api/v1/orgs/{org_id}/personas"),
    ],
    "create_project_group": [("POST", "/api/v1/orgs/{org_id}/groups")],
    "create_reminder": [("POST", "/api/v1/me/reminders")],
    "create_ticket": [("POST", f"{P}/tickets")],
    "create_work_queue": [("POST", f"{P}/work-queues")],
    "decline_handoff": [("POST", "/api/v1/handoffs/{handoff_id}/decline")],
    "dismiss_knowledge_entry": [("PUT", f"{P}/entries/{{entry_id}}")],
    "escalate_ticket": [("PUT", T), ("POST", f"{T}/comments")],
    "extract_meeting_action_items": [("POST", "/api/v1/meetings/{meeting_id}/extract")],
    "finish_worker_run": [("POST", f"{P}/agent-runs/{{run_id}}/finish")],
    "get_compiled_rules": [("POST", f"{P}/rules/compile")],
    "get_ticket_updates": [
        ("GET", "/api/v1/me/ticket-updates"),
        ("POST", "/api/v1/me/ticket-updates/ack"),
    ],
    "grant_group_access": [("PUT", "/api/v1/orgs/{org_id}/groups/{group_id}/members/{user_id}")],
    "link_meeting_action_item": [
        ("POST", f"{MA}/link/preview"),
        ("POST", f"{MA}/link"),
        ("POST", f"{MA}/unlink"),
    ],
    "promote_eligible_entries": [("POST", f"{P}/entries/bulk-promote")],
    "promote_entry": [("PUT", f"{P}/entries/{{entry_id}}/promote")],
    "promote_meeting_action_item": [
        ("POST", f"{MA}/promote/preview"),
        ("POST", f"{MA}/promote"),
    ],
    "propose_checklist_item": [("POST", f"{P}/loop-checklist")],
    "record_agent_feedback": [("POST", f"{P}/agent-feedback")],
    "record_review_round": [("POST", f"{P}/loops/{{loop_id}}/rounds")],
    "rename_session": [("PATCH", "/api/v1/sessions/{session_id}")],
    "replay_agent_run": [("POST", f"{P}/agent-runs/{{run_id}}/replay")],
    "report_agent_run_usage": [("POST", f"{P}/agent-runs/{{run_id}}/usage")],
    "request_skill_install": [("POST", f"{P}/skill-install-requests")],
    "resolve_ticket": [("POST", f"{T}/accept")],
    "revoke_handoff": [("POST", "/api/v1/handoffs/{handoff_id}/revoke")],
    "run_work_queue_step": [("POST", f"{P}/work-queues/{{queue_id}}/step")],
    "set_work_queue_status": [("POST", f"{P}/work-queues/{{queue_id}}/status")],
    "start_review_loop": [("POST", f"{P}/loops")],
    "start_ticket": [("POST", f"{T}/start")],
    "start_worker_run": [("POST", f"{P}/agent-runs/start-worker")],
    "start_workflow": [("POST", f"{P}/workflows")],
    "update_entry_confidence": [("PUT", f"{P}/entries/{{entry_id}}/confidence")],
    "update_external_contact": [
        ("PATCH", "/api/v1/orgs/{org_id}/contacts/{contact_id}"),
        ("POST", "/api/v1/orgs/{org_id}/contacts/{contact_id}/aliases"),
        ("DELETE", "/api/v1/orgs/{org_id}/contacts/{contact_id}/aliases"),
    ],
    "update_persona": [
        ("PUT", f"{P}/personas/{{name}}"),
        ("PUT", "/api/v1/orgs/{org_id}/personas/{name}"),
    ],
    "update_project_group": [("PATCH", "/api/v1/orgs/{org_id}/groups/{group_id}")],
    "update_reminder": [
        ("PATCH", "/api/v1/me/reminders/{reminder_id}"),
        ("POST", "/api/v1/me/reminders/{reminder_id}/snooze"),
        ("POST", "/api/v1/me/reminders/{reminder_id}/notes"),
    ],
    "update_review_loop": [("PATCH", f"{P}/loops/{{loop_id}}")],
    "update_ticket": [("PUT", T)],
    "update_wiki_page": [("PUT", f"{P}/pages/{{slug:path}}")],
    "upload_chat_thread": [
        ("POST", f"{P}/chat-threads"),
        ("POST", "/api/v1/groups/{group_id}/chat-threads"),
    ],
    "upload_meeting_transcript": [
        ("POST", f"{P}/meetings"),
        ("POST", "/api/v1/groups/{group_id}/meetings"),
    ],
    "watch_ticket": [("PUT", f"{T}/watch")],
    # Evals + lineage (dispatched through _EVAL_HANDLERS).
    "start_eval_run": [("POST", f"{P}/eval-runs")],
    "record_eval_results": [
        ("POST", f"{P}/eval-runs/{{run_id}}/results"),
        ("POST", f"{P}/eval-runs/{{run_id}}/finish"),
    ],
    "register_artifact": [("POST", f"{P}/artifacts")],
    "link_artifacts": [("POST", f"{P}/artifact-edges")],
}

# Arguments every tool may take that never reach a route.
CLIENT_ONLY: dict[str, str] = {
    "git_remote": "resolves the project id client-side (path parameter)",
    "audit_session_id": "client-side retrieval-audit attribution",
}

_USAGE = {
    k: f"usage.{k}"
    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd", "cost_basis")
}

# tool -> {mcp argument: route field it is sent as}. ``a.b`` names field ``b``
# of the object field ``a``.
MCP_RENAMES: dict[str, dict[str, str]] = {
    "add_project_to_group": {"slug": "group_id"},
    "ask_session_helper": {
        "question": "message",
        "project": "scope.project_id",
        "group_id": "scope.group_id",
        "session_ids": "scope.session_ids",
    },
    "assign_persona": {"persona_name": "assigned_to"},
    "complete_work_queue_step": {"work_queue_id": "queue_id"},
    "create_ticket": {"assigned_contact": "assigned_contact_id"},
    "dismiss_knowledge_entry": {"id": "entry_id", "undismiss": "dismissed"},
    "escalate_ticket": {"reason": "content"},
    "finish_worker_run": dict(_USAGE),
    "get_compiled_rules": {"tool": "tools"},
    "get_ticket_updates": {"persona_name": "persona"},
    "grant_group_access": {"slug": "group_id"},
    "link_meeting_action_item": {"conversation_kind": "kind"},
    "promote_entry": {"id": "entry_id"},
    "promote_meeting_action_item": {"conversation_kind": "kind"},
    "record_eval_results": {k: f"usage.{k}" for k in ("input_tokens", "output_tokens", "cost_usd")},
    "record_review_round": dict(_USAGE),
    "rename_session": {"new_title": "title", "new_alias": "alias"},
    "request_skill_install": {"session_id": "requested_by_session_id"},
    "run_work_queue_step": {"work_queue_id": "queue_id"},
    "set_work_queue_status": {"work_queue_id": "queue_id"},
    "update_entry_confidence": {"id": "entry_id"},
    "update_external_contact": {"add_aliases": "alias", "remove_aliases": "alias"},
    "update_project_group": {"new_slug": "slug"},
    "update_reminder": {
        "note": "body",
        "snooze": "preset",
        "reschedule": "due_at",
        "close": "status",
    },
    "update_ticket": {"assigned_contact": "assigned_contact_id"},
}

# tool -> {mcp argument: why it never reaches the route}
TOOL_CLIENT_ONLY: dict[str, dict[str, str]] = {
    "create_agent_run": {"start_now": "chooses whether to call the /start route after create"},
    "get_ticket_updates": {"ack": "chooses whether to call the /ack route after the read"},
    "link_meeting_action_item": {
        "confirm": "chooses preview vs link (preview first, then the pinned link)",
        "unlink": "chooses the /unlink route",
    },
    "promote_meeting_action_item": {"confirm": "chooses preview vs promote"},
    "record_eval_results": {"finish": "chooses whether to call the /finish route"},
    "watch_ticket": {"unwatch": "chooses DELETE (unwatch) instead of PUT"},
}


# ---------------------------------------------------------------------------
# Route side
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _routes() -> dict[tuple[str, str], APIRoute]:
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    app = create_app(ServerConfig(dashboard_dir="/nonexistent-sessionfs-dashboard"))
    from sessionfs.server.route_introspection import iter_api_routes

    out: dict[tuple[str, str], APIRoute] = {}
    for entry in iter_api_routes(app):
        for method in entry.methods:
            out.setdefault((method, entry.path), entry.route)  # type: ignore[arg-type]
    return out


def _route(method: str, path: str) -> APIRoute:
    route = _routes().get((method, path))
    assert route is not None, f"MCP_WRITE_ROUTES names a route that does not exist: {method} {path}"
    return route


def _route_body(route: APIRoute) -> set[str]:
    keys = route_body_keys(route)
    if keys is not None:
        return keys
    raw = raw_body_model(route)
    return model_input_keys(raw) if raw is not None else set()


def _route_nested_keys(route: APIRoute, field: str) -> set[str]:
    """Keys of the object-valued body field ``field``."""
    from sessionfs.server.api_contract import union_models

    models = route_body_models(route) or []
    raw = raw_body_model(route)
    if raw is not None:
        models = [*models, raw]
    keys: set[str] = set()
    for model in models:
        info = model.model_fields.get(field)
        if info is None:
            continue
        for sub in union_models(info.annotation) or []:
            keys |= model_input_keys(sub)
    return keys


def _route_accepts_name(route: APIRoute, name: str) -> bool:
    if "." in name:
        parent, child = name.split(".", 1)
        return child in _route_nested_keys(route, parent)
    return name in _route_accepts(route)


def _route_accepts(route: APIRoute) -> set[str]:
    from sessionfs.server.route_introspection import flat_params

    path_params = {p.name for p in flat_params(route.dependant, "path_params")}
    query = {p.alias for p in flat_params(route.dependant, "query_params")}
    return _route_body(route) | path_params | query


# ---------------------------------------------------------------------------
# MCP side
# ---------------------------------------------------------------------------


def _tools() -> dict[str, dict]:
    return {t.name: t.inputSchema for t in mcp_server._TOOLS}


@lru_cache(maxsize=1)
def _module_ast() -> tuple[dict[str, ast.AST], dict[str, str]]:
    tree = ast.parse(inspect.getsource(mcp_server))
    funcs = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    dispatch: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "name"
            and isinstance(node.test.comparators[0], ast.Constant)
        ):
            for sub in ast.walk(node.body[0]):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id.startswith("_handle")
                ):
                    dispatch.setdefault(node.test.comparators[0].value, sub.func.id)
                    break
    for tool, fn in mcp_server._EVAL_HANDLERS.items():
        dispatch[tool] = fn.__name__
    return funcs, dispatch


_BODY_KWARGS = ("json", "body")
_GENERIC_REQUEST_HELPERS = frozenset({"_resolve_project_id", "_eval_call", "_contact_request"})


def _dict_keys(node: ast.AST | None) -> set[str]:
    """Constant top-level keys of a dict expression (dict literal, ``**`` and
    conditional expressions included)."""
    keys: set[str] = set()
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if k is None:
                keys |= _dict_keys(v)
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
    elif isinstance(node, ast.IfExp):
        keys |= _dict_keys(node.body) | _dict_keys(node.orelse)
    elif isinstance(node, ast.BoolOp):
        for v in node.values:
            keys |= _dict_keys(v)
    return keys


def _loop_constants(fn: ast.AST) -> dict[str, set[str]]:
    """``for key in ("a", "b"):`` loop variables -> their constant values."""
    out: dict[str, set[str]] = {}
    for node in ast.walk(fn):
        if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name):
            if isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
                vals = {
                    e.value for e in node.iter.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
                out.setdefault(node.target.id, set()).update(vals)
    return out


def _body_vars(fn: ast.AST, funcs: dict[str, ast.AST]) -> tuple[set[str], set[str]]:
    """(names passed as a JSON body, keys of inline dict bodies) in ``fn``."""
    names: set[str] = set()
    inline: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg in _BODY_KWARGS:
                if isinstance(kw.value, ast.Name):
                    names.add(kw.value.id)
                else:
                    inline |= _dict_keys(kw.value)
        callee = node.func.id if isinstance(node.func, ast.Name) else None
        # _loop_worker_post(args, path, body)
        if callee == "_loop_worker_post" and len(node.args) >= 3:
            arg = node.args[2]
            if isinstance(arg, ast.Name):
                names.add(arg.id)
            else:
                inline |= _dict_keys(arg)
    return names, inline


def _keys_assigned(fn: ast.AST, var_names: set[str]) -> set[str]:
    loops = _loop_constants(fn)
    keys: set[str] = set()

    def sub_key(target: ast.Subscript) -> set[str]:
        s = target.slice
        if isinstance(s, ast.Constant) and isinstance(s.value, str):
            return {s.value}
        if isinstance(s, ast.Name):
            return set(loops.get(s.id, set()))
        return set()

    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for target in targets:
                if isinstance(target, ast.Name) and target.id in var_names:
                    keys |= _dict_keys(value)
                elif (
                    isinstance(target, ast.Tuple)
                    and isinstance(value, ast.Tuple)
                    and len(target.elts) == len(value.elts)
                ):
                    for t, v in zip(target.elts, value.elts):
                        if isinstance(t, ast.Name) and t.id in var_names:
                            keys |= _dict_keys(v)
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in var_names
                ):
                    keys |= sub_key(target)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id in var_names:
                if node.func.attr == "update" and node.args:
                    keys |= _dict_keys(node.args[0])
                    for kw in node.keywords:
                        if kw.arg:
                            keys.add(kw.arg)
                elif node.func.attr == "setdefault" and node.args:
                    a = node.args[0]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        keys.add(a.value)
    return keys


def _handler_body_keys(handler: str) -> set[str]:
    """Top-level JSON body keys ``handler`` (and the helpers it hands the body
    to) can send."""
    funcs, _ = _module_ast()
    seen: set[tuple[str, frozenset[str]]] = set()

    def visit(name: str, extra_vars: frozenset[str]) -> set[str]:
        if (name, extra_vars) in seen or name not in funcs:
            return set()
        seen.add((name, extra_vars))
        fn = funcs[name]
        names, inline = _body_vars(fn, funcs)
        names |= extra_vars
        keys = inline | _keys_assigned(fn, names)
        # Follow the body into helpers it's handed to (e.g. _add_step_id).
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in funcs
                and not node.func.id.startswith("_handle")
                and node.func.id != name
            ):
                callee = funcs[node.func.id]
                params = [a.arg for a in callee.args.args]  # type: ignore[attr-defined]
                passed = {
                    params[i]
                    for i, a in enumerate(node.args)
                    if i < len(params) and isinstance(a, ast.Name) and a.id in names
                }
                if passed:
                    keys |= visit(node.func.id, frozenset(passed))
        return keys

    return visit(handler, frozenset())


def _write_tools_from_source() -> set[str]:
    """Tools whose handler sends a JSON body somewhere (directly or via a
    helper)."""
    funcs, dispatch = _module_ast()
    out: set[str] = set()

    def sends_body(name: str, seen: set[str]) -> bool:
        if name in seen or name not in funcs:
            return False
        seen.add(name)
        fn = funcs[name]
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if any(kw.arg in _BODY_KWARGS for kw in node.keywords):
                return True
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else None
            if attr in ("post", "put", "patch"):
                return True
            if isinstance(func, ast.Name) and func.id in ("_loop_worker_post",):
                return True
            if (
                isinstance(func, ast.Name)
                and func.id in funcs
                and not func.id.startswith("_handle")
                # Generic request helpers: whether they send a body is decided
                # at the call site (a ``json=``/``body=`` argument, caught above).
                and func.id not in _GENERIC_REQUEST_HELPERS
            ):
                if sends_body(func.id, seen):
                    return True
        return False

    for tool, handler in dispatch.items():
        if sends_body(handler, set()):
            out.add(tool)
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_writing_tool_is_mapped():
    tools = _tools()
    writing = _write_tools_from_source() & set(tools)
    missing = sorted(writing - set(MCP_WRITE_ROUTES))
    assert not missing, (
        "These MCP tools send a request body or write to the API but are not in "
        f"MCP_WRITE_ROUTES (tests/unit/test_mcp_route_contract.py): {missing}. "
        "Add each with the route(s) it calls."
    )
    unknown = sorted(set(MCP_WRITE_ROUTES) - set(tools))
    assert not unknown, f"MCP_WRITE_ROUTES names tools that don't exist: {unknown}"


def test_mapped_routes_exist():
    for tool, routes in MCP_WRITE_ROUTES.items():
        for method, path in routes:
            _route(method, path)


def _describe(routes: list[tuple[str, str]]) -> str:
    return ", ".join(f"{m} {p}" for m, p in routes)


def _argument_problems(
    tool: str,
    props: set[str],
    routes: list[tuple[str, str]],
    renames: dict[str, str],
    client_only: dict[str, str],
) -> list[str]:
    accepted: set[str] = set()
    for method, path in routes:
        accepted |= _route_accepts(_route(method, path))
    problems: list[str] = []
    for prop in sorted(props):
        if prop in client_only:
            continue
        if prop in renames:
            target = renames[prop]
            if not any(_route_accepts_name(_route(m, p), target) for m, p in routes):
                problems.append(f"{tool}: '{prop}' is renamed to '{target}', which no mapped route accepts")
            continue
        if prop not in accepted:
            problems.append(
                f"{tool}: inputSchema property '{prop}' is not accepted by {_describe(routes)}"
            )
    for prop in sorted(set(renames) | (set(client_only) - set(CLIENT_ONLY))):
        if prop not in props:
            problems.append(f"{tool}: allowlisted/renamed argument '{prop}' is not in the inputSchema")
    return problems


def _body_problems(tool: str, sent: set[str], routes: list[tuple[str, str]]) -> list[str]:
    body_fields: set[str] = set()
    for method, path in routes:
        body_fields |= _route_body(_route(method, path))
    return [
        f"{tool}: handler sends body key '{key}', which {_describe(routes)} "
        "does not accept (it would be dropped)"
        for key in sorted(sent - body_fields)
    ]


def test_tool_arguments_map_onto_route_fields():
    tools = _tools()
    problems: list[str] = []
    for tool, routes in sorted(MCP_WRITE_ROUTES.items()):
        problems += _argument_problems(
            tool,
            set((tools[tool].get("properties") or {}).keys()),
            routes,
            MCP_RENAMES.get(tool, {}),
            {**CLIENT_ONLY, **TOOL_CLIENT_ONLY.get(tool, {})},
        )
    assert not problems, (
        "MCP ↔ route drift (fix the tool or the route; only allowlist an "
        "argument that never reaches the API):\n  " + "\n  ".join(problems)
    )


def test_handler_body_keys_are_route_fields():
    _, dispatch = _module_ast()
    problems: list[str] = []
    for tool, routes in sorted(MCP_WRITE_ROUTES.items()):
        problems += _body_problems(tool, _handler_body_keys(dispatch[tool]), routes)
    assert not problems, "MCP handler bodies send fields the route ignores:\n  " + "\n  ".join(problems)


def test_drift_checks_catch_a_misnamed_field():
    """The transcript-vs-content class: a tool argument or body key the route
    doesn't read is reported, and so is a rename onto a missing field."""
    routes = MCP_WRITE_ROUTES["upload_meeting_transcript"]
    arg_problems = _argument_problems(
        "upload_meeting_transcript", {"title", "transcript"}, routes, {}, dict(CLIENT_ONLY)
    )
    assert any("'transcript'" in p for p in arg_problems), arg_problems
    assert not any("'title'" in p for p in arg_problems)
    body_problems = _body_problems("upload_meeting_transcript", {"title", "transcript"}, routes)
    assert body_problems == [
        f"upload_meeting_transcript: handler sends body key 'transcript', which {_describe(routes)} "
        "does not accept (it would be dropped)"
    ]
    rename_problems = _argument_problems(
        "finish_worker_run",
        {"input_tokens"},
        MCP_WRITE_ROUTES["finish_worker_run"],
        {"input_tokens": "usage.no_such_field"},
        dict(CLIENT_ONLY),
    )
    assert rename_problems and "usage.no_such_field" in rename_problems[0]


def test_body_key_extraction_sees_known_keys():
    """Guard the static analysis itself: known handlers yield their keys."""
    _, dispatch = _module_ast()
    loop_keys = _handler_body_keys(dispatch["start_review_loop"])
    assert {"tickets", "reviewer_persona", "title", "from_ticket_id", "token_budget"} <= loop_keys
    worker_keys = _handler_body_keys(dispatch["start_worker_run"])
    assert {"ticket_id", "model", "step_id"} <= worker_keys
    reminder_keys = _handler_body_keys(dispatch["update_reminder"])
    assert {"body", "preset", "until", "due_at", "status"} <= reminder_keys
