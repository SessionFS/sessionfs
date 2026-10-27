"""Project groups — org-scoped flat groups of projects (GitHub #101, phase 2 CLI).

``sfs project group`` manages the groups themselves (list/create/show/set/
delete, add/remove projects, members, grant/revoke access), and surfaces the
phase 2 knowledge reads (search/context/compile) plus the caller's own access.
Every id interpolated into a URL goes through ``_path_segment``; every API
error becomes a clear message (``_fail``) rather than a traceback.

Group identity is the org-scoped slug: the knowledge routes address groups by
slug, while the phase 1 CRUD routes address them by id, so the CLI resolves a
slug to its id through the org's group listing. Org resolution is ``--org``
else the server-side default org (``sfs config default-org``) else an error.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from urllib.parse import urlencode

import typer
from rich.table import Table

from sessionfs.cli.common import console, err_console, format_api_error, handle_errors
from sessionfs.url_path_safety import _path_segment

group_app = typer.Typer(
    name="group",
    help="Manage project groups (org-scoped collections of projects).",
    no_args_is_help=True,
)

_ROLES = ("reader", "editor", "admin")
_ENTRY_TYPES = ("decision", "pattern", "discovery", "convention", "bug", "dependency")


def _client() -> tuple[str, str]:
    # Lazy import keeps this module free of a cmd_project <-> cmd_project_group
    # cycle (cmd_project registers group_app at the bottom of its module).
    from sessionfs.cli.cmd_project import _get_project_client

    return _get_project_client()


def _json(data: object) -> None:
    console.print_json(json.dumps(data, sort_keys=True, default=str))


def _seg(value: str, label: str) -> str:
    """Percent-encode one URL path segment, refusing values (``..``, control
    characters, backslashes) that could change which endpoint is called."""
    try:
        return _path_segment(value)
    except ValueError:
        err_console.print(f"[red]Invalid {label}: {value!r}[/red]")
        raise typer.Exit(2)


def _fail(result: Any, *, not_found: str, conflict: str | None = None) -> None:
    """Exit with a clear message when ``result`` is an error envelope from
    ``_request``; a no-op for a successful response."""
    if not (isinstance(result, dict) and isinstance(result.get("_status"), int)):
        return
    status = result["_status"]
    if status < 400:
        return
    body = result.get("_body")
    if status == 404:
        message = not_found
    elif status == 409 and conflict is not None:
        message = conflict
    else:
        message = f"API error ({status}): {format_api_error(body, status)}"
    err_console.print(f"[red]{message}[/red]")
    raise typer.Exit(1)


def _resolve_org_id(org: str | None, api_url: str, api_key: str) -> str:
    """``--org`` wins; otherwise the server's default org; otherwise error."""
    if org:
        return org
    me = asyncio.run(_request("GET", "/api/v1/auth/me", api_url, api_key))
    _fail(me, not_found="Could not load your account.")
    default = me.get("default_org_id") if isinstance(me, dict) else None
    if not default:
        err_console.print(
            "[red]No org specified. Pass --org, or set a default with "
            "'sfs config default-org'.[/red]"
        )
        raise typer.Exit(1)
    return default


async def _request(
    method: str,
    path: str,
    api_url: str,
    api_key: str,
    json_data: dict | None = None,
) -> Any:
    """Authenticated request. A 2xx returns the parsed body (``{"_status":
    204}`` for an empty one); any error status returns ``{"_status": code,
    "_body": body}`` for the caller to turn into a message via ``_fail``.
    A network failure prints a clear message and exits."""
    import httpx

    url = f"{api_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, json=json_data)
    except httpx.RequestError as exc:
        err_console.print(f"[red]Could not reach the SessionFS API at {api_url}: {exc}[/red]")
        raise typer.Exit(1)

    if resp.status_code == 204:
        return {"_status": 204}
    if resp.status_code >= 400:
        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return {"_status": resp.status_code, "_body": body}
    return resp.json()


def _resolve_group_id(org_id: str, slug: str, api_url: str, api_key: str) -> str:
    """Resolve a group slug to its id inside ``org_id`` (exits if missing)."""
    groups = asyncio.run(
        _request("GET", f"/api/v1/orgs/{_seg(org_id, 'org id')}/groups", api_url, api_key)
    )
    _fail(groups, not_found=f"Org {org_id} not found (or you are not a member).")
    for g in groups if isinstance(groups, list) else []:
        if g.get("slug") == slug:
            return g["id"]
    err_console.print(
        f"[red]No project group with slug '{slug}' in org {org_id}.[/red]"
    )
    raise typer.Exit(1)


def _group_path(org_id: str, group_id: str) -> str:
    return f"/api/v1/orgs/{_seg(org_id, 'org id')}/groups/{_seg(group_id, 'group id')}"


def _slug_path(org_id: str, slug: str) -> str:
    return f"/api/v1/orgs/{_seg(org_id, 'org id')}/groups/{_seg(slug, 'group slug')}"


def _resolve_user_id(org_id: str, user: str, api_url: str, api_key: str) -> str:
    """An org member's user id from ``user``: an id, or an email looked up in
    the org's member list. An exact email match wins; otherwise a single
    case-insensitive match is used, and several are refused as ambiguous."""
    if "@" not in user:
        return user
    roster = asyncio.run(
        _request("GET", f"/api/v1/orgs/{_seg(org_id, 'org id')}/members", api_url, api_key)
    )
    _fail(roster, not_found=f"Org {org_id} not found (or you are not a member).")
    wanted = user.strip()
    members = roster.get("members", []) if isinstance(roster, dict) else []
    exact = [m for m in members if (m.get("email") or "") == wanted]
    if len(exact) == 1:
        return exact[0]["user_id"]
    folded = [m for m in members if (m.get("email") or "").lower() == wanted.lower()]
    if len(exact) > 1 or len(folded) > 1:
        candidates = ", ".join(
            f"{m.get('email')} ({m.get('user_id')})" for m in (exact or folded)
        )
        err_console.print(
            f"[red]{user} matches more than one member of org {org_id}: "
            f"{candidates}. Pass the exact email or the user id.[/red]"
        )
        raise typer.Exit(1)
    if folded:
        return folded[0]["user_id"]
    err_console.print(f"[red]No member of org {org_id} has the email {user}.[/red]")
    raise typer.Exit(1)


_ORG_OPT_HELP = "Org id (default org if omitted)."


def _org_404(org_id: str) -> str:
    return f"Org {org_id} not found (or you are not a member)."


@group_app.command("list")
@handle_errors
def group_list(
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
    json_output: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List the org's project groups."""
    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)

    groups = asyncio.run(
        _request("GET", f"/api/v1/orgs/{_seg(org_id, 'org id')}/groups", api_url, api_key)
    )
    _fail(groups, not_found=_org_404(org_id))

    if json_output:
        _json(groups)
        return
    if not groups:
        console.print("[dim]No project groups in this org.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Slug", style="bold")
    table.add_column("Name")
    table.add_column("Projects", justify="right")
    table.add_column("Members", justify="right")
    table.add_column("My role")
    for g in groups:
        table.add_row(
            g.get("slug", ""),
            g.get("name", ""),
            str(g.get("project_count", 0)),
            str(g.get("member_count", 0)),
            g.get("my_role") or "-",
        )
    console.print(table)


@group_app.command("create")
@handle_errors
def group_create(
    name: str = typer.Argument(..., help="Group name (1–255 chars)."),
    slug: str | None = typer.Option(
        None, "--slug", help="URL-safe slug (derived from NAME if omitted)."
    ),
    description: str | None = typer.Option(None, "--description", help="Group description."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
    json_output: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Create a project group named NAME (org admin/owner)."""
    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)

    body: dict = {"name": name}
    if slug is not None:
        body["slug"] = slug
    if description is not None:
        body["description"] = description

    result = asyncio.run(
        _request(
            "POST",
            f"/api/v1/orgs/{_seg(org_id, 'org id')}/groups",
            api_url,
            api_key,
            json_data=body,
        )
    )
    _fail(
        result,
        not_found=_org_404(org_id),
        conflict="A project group with that slug already exists in this org.",
    )

    if json_output:
        _json(result)
    else:
        console.print(
            f"[green]Created group[/green] [bold]{result['name']}[/bold] "
            f"([dim]{result['slug']}[/dim])"
        )


@group_app.command("show")
@handle_errors
def group_show(
    group: str = typer.Argument(..., help="Group slug."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
    json_output: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Show a group's members and projects."""
    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)
    group_id = _resolve_group_id(org_id, group, api_url, api_key)

    detail = asyncio.run(_request("GET", _group_path(org_id, group_id), api_url, api_key))
    _fail(detail, not_found="Project group not found.")

    if json_output:
        _json(detail)
        return

    console.print(f"[bold]{detail['name']}[/bold]  [dim]{detail['slug']}[/dim]")
    if detail.get("description"):
        console.print(detail["description"])
    console.print(f"[dim]Your role: {detail.get('my_role') or '-'}[/dim]")
    compiled = detail.get("context_compiled_at") or "never"
    console.print(
        f"[dim]Knowledge compile: {'on' if detail.get('compile_enabled') else 'off'}"
        f" (last compiled: {compiled})[/dim]"
    )

    members = detail.get("members", [])
    if members:
        console.print()
        table = Table(title="Members", show_header=True, header_style="bold")
        table.add_column("Email")
        table.add_column("Role")
        for m in members:
            table.add_row(m.get("email", ""), m.get("role", ""))
        console.print(table)

    projects = detail.get("projects", [])
    if projects:
        console.print(f"[bold]Projects ({len(projects)}):[/bold]")
        for pid in projects:
            console.print(f"  {pid}")
    else:
        console.print("[dim]No projects in this group.[/dim]")


@group_app.command("set")
@handle_errors
def group_set(
    group: str = typer.Argument(..., help="Group slug."),
    name: str | None = typer.Option(None, "--name", help="New group name."),
    new_slug: str | None = typer.Option(None, "--slug", help="New group slug."),
    description: str | None = typer.Option(
        None, "--description", help="New description ('' clears it)."
    ),
    compile_enabled: bool | None = typer.Option(
        None,
        "--compile/--no-compile",
        help="Turn the group knowledge-context compile on or off.",
    ),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
    json_output: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Update a group's name, slug, description or compile setting (group admin)."""
    body: dict = {}
    if name is not None:
        body["name"] = name
    if new_slug is not None:
        body["slug"] = new_slug
    if description is not None:
        body["description"] = description or None
    if compile_enabled is not None:
        body["compile_enabled"] = compile_enabled
    if not body:
        err_console.print(
            "[red]Nothing to update: pass --name, --slug, --description, "
            "or --compile/--no-compile.[/red]"
        )
        raise typer.Exit(2)

    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)
    group_id = _resolve_group_id(org_id, group, api_url, api_key)

    result = asyncio.run(
        _request("PATCH", _group_path(org_id, group_id), api_url, api_key, json_data=body)
    )
    _fail(
        result,
        not_found="Project group not found.",
        conflict="A project group with that slug already exists in this org.",
    )
    if json_output:
        _json(result)
        return
    state = "on" if result.get("compile_enabled") else "off"
    console.print(
        f"[green]Updated group[/green] [bold]{result.get('slug', group)}[/bold] "
        f"(knowledge compile {state})."
    )


@group_app.command("delete")
@handle_errors
def group_delete(
    group: str = typer.Argument(..., help="Group slug."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
) -> None:
    """Soft-delete a project group (org admin/owner)."""
    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)
    group_id = _resolve_group_id(org_id, group, api_url, api_key)

    result = asyncio.run(_request("DELETE", _group_path(org_id, group_id), api_url, api_key))
    _fail(result, not_found="Project group not found.")
    console.print(f"[green]Deleted group[/green] [bold]{group}[/bold]")


@group_app.command("add")
@handle_errors
def group_add(
    group: str = typer.Argument(..., help="Group slug."),
    project_ids: list[str] = typer.Argument(..., help="One or more project ids to add."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
) -> None:
    """Add projects to a group (group admin with edit standing on each)."""
    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)
    group_id = _resolve_group_id(org_id, group, api_url, api_key)

    for project_id in project_ids:
        result = asyncio.run(
            _request(
                "PUT",
                f"{_group_path(org_id, group_id)}/projects/{_seg(project_id, 'project id')}",
                api_url,
                api_key,
            )
        )
        _fail(
            result,
            not_found=f"Project {project_id} not found (or you cannot read it).",
            conflict=f"Project {project_id} was added concurrently; retry.",
        )
        console.print(f"[green]Added project[/green] [bold]{project_id}[/bold] to {group}.")


@group_app.command("remove")
@handle_errors
def group_remove(
    group: str = typer.Argument(..., help="Group slug."),
    project_id: str = typer.Argument(..., help="Project id to remove."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
) -> None:
    """Remove a project from a group (group admin)."""
    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)
    group_id = _resolve_group_id(org_id, group, api_url, api_key)

    result = asyncio.run(
        _request(
            "DELETE",
            f"{_group_path(org_id, group_id)}/projects/{_seg(project_id, 'project id')}",
            api_url,
            api_key,
        )
    )
    _fail(result, not_found=f"Project {project_id} is not in group {group}.")
    console.print(f"[green]Removed project[/green] [bold]{project_id}[/bold] from {group}.")


@group_app.command("members")
@handle_errors
def group_members(
    group: str = typer.Argument(..., help="Group slug."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
    json_output: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List a group's members and their roles."""
    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)
    group_id = _resolve_group_id(org_id, group, api_url, api_key)

    detail = asyncio.run(_request("GET", _group_path(org_id, group_id), api_url, api_key))
    _fail(detail, not_found="Project group not found.")

    members = detail.get("members", [])
    if json_output:
        _json(members)
        return
    if not members:
        console.print("[dim]No explicit member grants.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Email")
    table.add_column("Role")
    table.add_column("Granted by")
    for m in members:
        table.add_row(
            m.get("email", ""),
            m.get("role", ""),
            m.get("granted_by_user_id") or "-",
        )
    console.print(table)


@group_app.command("grant")
@handle_errors
def group_grant(
    group: str = typer.Argument(..., help="Group slug."),
    user: str = typer.Argument(..., help="Org member's email or user id."),
    role: str = typer.Option(..., "--role", help="reader, editor, or admin."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
) -> None:
    """Grant (or update) a member's role in a group (group admin)."""
    if role not in _ROLES:
        err_console.print(f"[red]--role must be one of: {', '.join(_ROLES)}[/red]")
        raise typer.Exit(2)

    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)
    group_id = _resolve_group_id(org_id, group, api_url, api_key)
    user_id = _resolve_user_id(org_id, user, api_url, api_key)

    result = asyncio.run(
        _request(
            "PUT",
            f"{_group_path(org_id, group_id)}/members/{_seg(user_id, 'user id')}",
            api_url,
            api_key,
            json_data={"role": role},
        )
    )
    _fail(
        result,
        not_found="Project group not found.",
        conflict="This member's grant changed concurrently; retry.",
    )
    console.print(f"[green]Granted[/green] {user} [bold]{role}[/bold] on {group}.")


@group_app.command("revoke")
@handle_errors
def group_revoke(
    group: str = typer.Argument(..., help="Group slug."),
    user: str = typer.Argument(..., help="Org member's email or user id."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
) -> None:
    """Revoke a member's grant from a group (group admin)."""
    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)
    group_id = _resolve_group_id(org_id, group, api_url, api_key)
    user_id = _resolve_user_id(org_id, user, api_url, api_key)

    result = asyncio.run(
        _request(
            "DELETE",
            f"{_group_path(org_id, group_id)}/members/{_seg(user_id, 'user id')}",
            api_url,
            api_key,
        )
    )
    _fail(result, not_found=f"{user} holds no grant on group {group}.")
    console.print(f"[green]Revoked[/green] {user} from {group}.")


@group_app.command("access")
@handle_errors
def group_access(
    json_output: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List every group you have standing on across your orgs."""
    api_url, api_key = _client()
    groups = asyncio.run(_request("GET", "/api/v1/me/groups", api_url, api_key))
    _fail(groups, not_found="Could not load your groups.")

    if json_output:
        _json(groups)
        return
    if not groups:
        console.print("[dim]You hold no group access.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Slug", style="bold")
    table.add_column("Name")
    table.add_column("Org")
    table.add_column("My role")
    for g in groups:
        table.add_row(
            g.get("slug", ""),
            g.get("name", ""),
            g.get("org_id", ""),
            g.get("my_role") or "-",
        )
    console.print(table)


@group_app.command("search")
@handle_errors
def group_search(
    group: str = typer.Argument(..., help="Group slug."),
    query: str | None = typer.Argument(
        None, help="Text to find in claim content (3–200 chars). Omit to list top claims."
    ),
    entry_type: str | None = typer.Option(
        None, "--entry-type", help="Filter by claim type (decision, pattern, …)."
    ),
    limit: int = typer.Option(100, "--limit", min=1, max=100, help="Max hits (≤100)."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
    json_output: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Search active KB claims across the group's readable member projects."""
    if entry_type is not None and entry_type not in _ENTRY_TYPES:
        err_console.print(f"[red]entry-type must be one of: {', '.join(_ENTRY_TYPES)}[/red]")
        raise typer.Exit(2)
    q = (query or "").strip()
    if q and not 3 <= len(q) <= 200:
        err_console.print("[red]The search query must be 3–200 characters.[/red]")
        raise typer.Exit(2)

    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)

    params: dict[str, str] = {"limit": str(limit)}
    if q:
        params["q"] = q
    if entry_type:
        params["entry_type"] = entry_type

    hits = asyncio.run(
        _request(
            "GET",
            f"{_slug_path(org_id, group)}/knowledge/search?{urlencode(params)}",
            api_url,
            api_key,
        )
    )
    _fail(hits, not_found=f"No project group '{group}' you can see in org {org_id}.")

    if json_output:
        _json(hits)
        return
    if not hits:
        console.print("[dim]No matching claims.[/dim]")
        return

    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("Entry", justify="right", width=6)
    table.add_column("Type", width=12)
    table.add_column("Project", width=16)
    table.add_column("Conf", justify="right", width=6)
    table.add_column("Content", ratio=3)
    for h in hits:
        content = h.get("content", "")
        if len(content) > 100:
            content = content[:97] + "..."
        table.add_row(
            str(h.get("entry_id", "")),
            h.get("entry_type", ""),
            h.get("project_name", ""),
            f"{h.get('confidence', 0):.0%}",
            content,
        )
    console.print(table)
    console.print(f"[dim]{len(hits)} hits[/dim]")


@group_app.command("context")
@handle_errors
def group_context(
    group: str = typer.Argument(..., help="Group slug."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
    json_output: bool = typer.Option(False, "--json", help="Print the full JSON response."),
) -> None:
    """Show the group's compiled knowledge overview."""
    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)

    result = asyncio.run(
        _request("GET", f"{_slug_path(org_id, group)}/context", api_url, api_key)
    )
    _fail(
        result,
        not_found=(
            f"No compiled context for group '{group}' "
            "(or no such group you can see)."
        ),
    )

    if json_output:
        _json(result)
        return
    sys.stdout.write(result.get("document", ""))


@group_app.command("compile")
@handle_errors
def group_compile(
    group: str = typer.Argument(..., help="Group slug."),
    org: str | None = typer.Option(None, "--org", help=_ORG_OPT_HELP),
    json_output: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Compile the group's deterministic knowledge overview (editor/admin)."""
    api_url, api_key = _client()
    org_id = _resolve_org_id(org, api_url, api_key)

    result = asyncio.run(
        _request("POST", f"{_slug_path(org_id, group)}/compile", api_url, api_key)
    )
    _fail(
        result,
        not_found=f"No project group '{group}' you can see in org {org_id}.",
        conflict=(
            "This group has not enabled knowledge context compilation. "
            f"Turn it on with: sfs project group set {group} --compile"
        ),
    )

    if json_output:
        _json(result)
        return
    sources = result.get("sources") or []
    console.print(
        f"[green]Compiled group overview[/green] [bold]{result['slug']}[/bold] "
        f"from {len(sources)} project(s)."
    )
