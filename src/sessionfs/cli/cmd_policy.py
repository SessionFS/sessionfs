"""`sfs policy` - the org skills policy and what it becomes in each tool
(marketplace v2 §8). Enterprise.

- ``sfs policy show``: the policies that apply to you (or one org's).
- ``sfs policy compile --tool T``: print a tool's compiled managed settings
  (redirect to a file and deploy it through MDM / IT).
- ``sfs policy status``: per tool, whether the managed settings are deployed
  on this machine (system) or only SessionFS's refusals apply (advisory).
- ``sfs policy set --org O --file F``: publish a new policy revision.
- ``sfs policy disable --org O``: publish a revision that restricts nothing
  (works below Enterprise, where a kept policy can only be relaxed).

`sfs` never writes system paths: deploying compiled settings is an explicit
IT step (signed bundles and ``sfs policy apply --system`` are a follow-up).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from sessionfs.cli.common import console, err_console, format_api_error, handle_errors

policy_app = typer.Typer(
    name="policy",
    help="Org skills policy: show it, compile it per tool, check this device (Enterprise).",
    no_args_is_help=True,
)


def _request(
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    headers: dict[str, str] | None = None,
    allow: tuple[int, ...] = (),
) -> tuple[int, Any, dict[str, str]]:
    import httpx

    from sessionfs.profiles import resolve_auth

    auth = resolve_auth()
    if not auth.api_key:
        err_console.print("[red]Not authenticated.[/red] Run [bold]sfs auth login[/bold] first.")
        raise typer.Exit(1)
    url = f"{auth.api_url.rstrip('/')}/api/v1{path}"
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {auth.api_key}", **(headers or {})},
                json=payload,
            )
    except httpx.RequestError as exc:
        err_console.print(f"[red]Can't reach the SessionFS API at {auth.api_url}.[/red] ({type(exc).__name__})")
        raise typer.Exit(1) from exc
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    if response.status_code >= 400 and response.status_code not in allow:
        err_console.print(Text(format_api_error(body, response.status_code), style="red"))
        raise typer.Exit(1)
    return response.status_code, body, dict(response.headers)


def _segment(value: str) -> str:
    from sessionfs.url_path_safety import path_segment

    try:
        return path_segment(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _my_policies() -> list[dict[str, Any]]:
    _status, body, _ = _request("GET", "/devices/me/policy")
    items = body.get("items") if isinstance(body, dict) else None
    return [item for item in items or [] if isinstance(item, dict)]


def _pick_org(org: str | None) -> str:
    if org:
        return _segment(org)
    items = _my_policies()
    if len(items) == 1:
        return _segment(str(items[0]["org_id"]))
    if not items:
        err_console.print("[yellow]No organization skills policy applies to you.[/yellow]")
    else:
        names = ", ".join(f"{item.get('org_name')} ({item.get('org_id')})" for item in items)
        err_console.print(Text(f"Several policies apply ({names}); pass --org.", style="yellow"))
    raise typer.Exit(1)


def _fields_line(label: str, fields: list[str]) -> Text:
    return Text(f"  {label}: {', '.join(fields) if fields else '-'}")


@policy_app.command("show")
@handle_errors
def show_policy(
    org: str | None = typer.Option(None, "--org", help="Organization id (default: every policy that applies to you)"),
    tool: str | None = typer.Option(None, "--tool", help="Also show what this tool enforces"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Show the org skills policy that applies to you."""
    if org:
        _status, body, _ = _request("GET", f"/orgs/{_segment(org)}/skills/policy")
        items = [
            {
                "org_id": body.get("org_id"),
                "org_name": body.get("org_id"),
                "revision": body.get("revision"),
                "policy_hash": body.get("policy_hash"),
                "policy": body.get("policy"),
            }
        ]
        if tool:
            _s, compiled, _ = _request(
                "GET", f"/orgs/{_segment(org)}/skills/policy/compiled?tool={_segment(tool)}"
            )
            items[0]["tools"] = compiled.get("compiled") or {}
    else:
        items = _my_policies()
    if json_output:
        console.print_json(json.dumps(items, sort_keys=True))
        return
    if not items:
        console.print("[dim]No organization skills policy applies to you.[/dim]")
        return
    for item in items:
        if not item.get("revision"):
            console.print(Text(f"{item.get('org_id')}: no skills policy", style="dim"))
            continue
        skills = ((item.get("policy") or {}).get("skills")) or {}
        console.print(
            Text(f"{item.get('org_name')} ({item.get('org_id')}) - revision {item.get('revision')}", style="bold")
        )
        console.print(Text(f"  mode: {skills.get('mode')}   max scan severity: {skills.get('max_scan_severity')}"))
        allowed = [
            name
            for name in ("allow_hooks", "allow_local_mcp", "allow_remote_mcp", "allow_skill_scripts",
                         "allow_hidden_unicode")
            if skills.get(name)
        ]
        console.print(Text(f"  allowed capabilities: {', '.join(allowed) or 'instructions only'}"))
        if skills.get("allowlist"):
            console.print(Text(f"  allowlist entries: {len(skills['allowlist'])}"))
        tools = item.get("tools") or {}
        for name in sorted(tools):
            if tool and name != tool:
                continue
            summary = tools[name]
            console.print(Text(f"  {name} ({summary.get('enforcement')})", style="cyan"))
            console.print(_fields_line("enforced by the tool", summary.get("enforceable") or []))
            console.print(_fields_line("checked by SessionFS only", summary.get("report_only") or []))
            console.print(_fields_line("not checked", summary.get("unsupported") or []))


@policy_app.command("compile")
@handle_errors
def compile_policy(
    tool: str = typer.Option(..., "--tool", help="claude-code, copilot, vscode, gemini, codex, cursor, ..."),
    org: str | None = typer.Option(None, "--org", help="Organization id (default: the one policy that applies)"),
    json_output: bool = typer.Option(False, "--json", help="Print the full compiled result as JSON"),
) -> None:
    """Print a tool's compiled managed settings (redirect to a file for IT)."""
    org_id = _pick_org(org)
    _status, body, _ = _request(
        "GET", f"/orgs/{org_id}/skills/policy/compiled?tool={_segment(tool)}"
    )
    result = (body.get("compiled") or {}).get(tool) or {}
    if json_output:
        typer.echo(json.dumps(result, indent=2, sort_keys=True))
        return
    artifacts = result.get("artifacts") or []
    if not artifacts:
        err_console.print(
            Text(f"{tool}: no managed settings ({result.get('enforcement')}); "
                 "SessionFS checks the policy during reconcile.", style="yellow")
        )
        raise typer.Exit(1)
    for artifact in artifacts:
        paths = artifact.get("paths") or {}
        if paths:
            where = "; ".join(f"{os_name}: {path}" for os_name, path in sorted(paths.items()))
            err_console.print(Text(f"# deploy to {where}", style="dim"))
        typer.echo(artifact.get("content") or "", nl=False)


@policy_app.command("status")
@handle_errors
def policy_status(
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Show, per tool, how the policy is delivered on this device."""
    from sessionfs.skills.policy_reconcile import tool_report

    rows = []
    for item in _my_policies():
        for name, summary in sorted((item.get("tools") or {}).items()):
            if not isinstance(summary, dict) or summary.get("enforcement") in {"off", "unsupported"}:
                continue
            local = tool_report(summary)
            rows.append(
                {
                    "org_id": item.get("org_id"),
                    "revision": item.get("revision"),
                    "tool": name,
                    "enforcement": summary.get("enforcement"),
                    "delivery": local["enforced_mode"],
                    "violations": local["violations"],
                    "enforceable": summary.get("enforceable") or [],
                }
            )
    if json_output:
        console.print_json(json.dumps(rows, sort_keys=True))
        return
    if not rows:
        console.print("[dim]No organization skills policy applies to you.[/dim]")
        return
    table = Table(title="Skills policy on this device")
    for column in ("Org", "Rev", "Tool", "Output", "Delivery", "Tool-enforced fields", "Issues"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["org_id"]), str(row["revision"]), row["tool"], str(row["enforcement"]),
            row["delivery"], str(len(row["enforceable"])), ", ".join(row["violations"]) or "-",
        )
    console.print(table)
    console.print(
        "[dim]advisory = SessionFS refuses non-compliant installs it manages; a user can bypass it. "
        "Deploy `sfs policy compile` output through MDM for tool enforcement.[/dim]"
    )


@policy_app.command("set")
@handle_errors
def set_policy(
    org: str = typer.Option(..., "--org", help="Organization id"),
    file: Path = typer.Option(..., "--file", "-f", help='Policy JSON ({"skills": {...}})', exists=True,
                              dir_okay=False, readable=True),
    acknowledge: bool = typer.Option(
        False, "--acknowledge-warnings", help="Accept the listed side effects without asking"
    ),
) -> None:
    """Publish a new policy revision (owner / admin)."""
    org_id = _segment(org)
    try:
        document = json.loads(file.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"could not read {file}: {exc}") from exc
    _s, preview, _ = _request("POST", f"/orgs/{org_id}/skills/policy/preview", {"policy": document})
    console.print(Text(f"Changes the output for: {', '.join(preview.get('changed_tools') or []) or 'no tool'}"))
    warnings = preview.get("warnings") or []
    for warning in warnings:
        console.print(Text(f"  ! {warning.get('tool')}: {warning.get('message')} [{warning.get('code')}]",
                           style="yellow"))
    if warnings and not acknowledge:
        if not typer.confirm("Publish with these side effects?", default=False):
            raise typer.Exit(1)
    # If-Match is the revision the PREVIEW was computed against: a revision
    # published while this command waited for confirmation makes the PUT
    # fail with 409 instead of silently overwriting it.
    revision = preview.get("current_revision") if isinstance(preview, dict) else None
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        err_console.print("[red]The server's preview did not include the current revision.[/red]")
        raise typer.Exit(1)
    etag = f'"osp-{revision}"'
    _s, saved, _ = _request(
        "PUT",
        f"/orgs/{org_id}/skills/policy",
        {"policy": document, "acknowledge_warnings": [w.get("code") for w in warnings]},
        headers={"If-Match": etag},
    )
    console.print(Text(f"Published revision {saved.get('revision')} ({saved.get('policy_hash')})", style="green"))


@policy_app.command("disable")
@handle_errors
def disable_policy(
    org: str = typer.Option(..., "--org", help="Organization id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
) -> None:
    """Publish a revision that restricts nothing (owner / admin, any tier)."""
    org_id = _segment(org)
    _s, current, headers = _request("GET", f"/orgs/{org_id}/skills/policy")
    revision = current.get("revision") if isinstance(current, dict) else None
    if not revision:
        err_console.print("[red]This organization has no skills policy.[/red]")
        raise typer.Exit(1)
    if not yes and not typer.confirm(
        f"Lift every restriction of revision {revision} for {org_id}?", default=False
    ):
        raise typer.Exit(1)
    etag = headers.get("etag") or headers.get("ETag") or f'"osp-{revision}"'
    _s, saved, _ = _request(
        "POST", f"/orgs/{org_id}/skills/policy/disable", {}, headers={"If-Match": etag}
    )
    console.print(Text(f"Published revision {saved.get('revision')}: no restrictions", style="green"))
