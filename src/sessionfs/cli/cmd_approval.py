"""`sfs approval …` — approval policies and requests (GitHub #123).

Commands:
- sfs approval policy list [--org ORG] [--all]
- sfs approval policy create --name N --target merge|kb_promote
      --require role:admin:1 [--require users:ID1,ID2:2 ...]
      [--filter JSON | --filter-file PATH] [--advisory] [--description D]
- sfs approval policy update POLICY_ID [--name] [--description] [--require ...]
      [--filter JSON | --filter-file PATH] [--blocking | --advisory]
- sfs approval policy disable POLICY_ID
- sfs approval list
- sfs approval show REQUEST_ID
- sfs approval approve REQUEST_ID --reason "…"
- sfs approval reject REQUEST_ID --reason "…"

A thin client of ``/api/v1/orgs/{org}/approval-policies``,
``/api/v1/me/approvals`` and ``/api/v1/approval-requests``. Who may decide,
self-approval, DLP on reasons and the plan gate are enforced server-side.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import typer
from rich.markup import escape
from rich.table import Table

from sessionfs.cli.cmd_rules import _api_request, _get_api_config
from sessionfs.cli.common import console, err_console, format_api_error, handle_errors

approval_app = typer.Typer(
    name="approval",
    help="Approval policies and the requests waiting on you.",
    no_args_is_help=True,
)
policy_app = typer.Typer(
    name="policy",
    help="Manage an org's approval policies (owners and admins).",
    no_args_is_help=True,
)
approval_app.add_typer(policy_app, name="policy")

_ROLE_RE = re.compile(r"^role:(owner|admin):(\d{1,2})$")
_USERS_RE = re.compile(r"^users:([^:]+):(\d{1,2})$")
_ORG_HELP = "Org id (defaults to your default org)."


def _seg(value: str) -> str:
    """One URL path segment: ids can never change the request target."""
    return quote(value.strip(), safe="")


def _call(
    method: str, path: str, *, json_data: dict | None = None, params: dict | None = None,
    not_found: str = "Not found.",
) -> Any:
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request(method, path, api_url, api_key, json_data=json_data, params=params)
    )
    if status == 404:
        err_console.print(f"[red]{not_found}[/red]")
        raise typer.Exit(1)
    if status >= 400:
        err_console.print(f"[red]API error ({status}): {format_api_error(body, status)}[/red]")
        raise typer.Exit(1)
    return body


def _org_id(org: str | None) -> str:
    if org and org.strip():
        return org.strip()
    me = _call("GET", "/api/v1/auth/me", not_found="Could not load your account.")
    default = me.get("default_org_id") if isinstance(me, dict) else None
    if not default:
        err_console.print(
            "[red]No org specified. Pass --org, or set a default with "
            "'sfs config default-org'.[/red]"
        )
        raise typer.Exit(1)
    return default


def parse_requirement(value: str) -> dict:
    """``role:admin:1`` / ``role:owner:2`` / ``users:ID1,ID2:1``."""
    raw = value.strip()
    m = _ROLE_RE.match(raw)
    if m:
        return {"who": "org_role", "role": m.group(1), "count": int(m.group(2))}
    m = _USERS_RE.match(raw)
    if m:
        ids = [i.strip() for i in m.group(1).split(",") if i.strip()]
        if ids:
            return {"who": "users", "user_ids": ids, "count": int(m.group(2))}
    raise typer.BadParameter(
        f"could not read requirement {value!r}; use role:admin:1, role:owner:1 "
        "or users:USER_ID[,USER_ID]:COUNT"
    )


def parse_filter(filter_json: str | None, filter_file: Path | None) -> dict | None:
    if filter_json is not None and filter_file is not None:
        raise typer.BadParameter("use either --filter or --filter-file")
    text = filter_json
    if filter_file is not None:
        text = filter_file.read_text(encoding="utf-8")
    if text is None:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"filter is not valid JSON: {exc.msg}") from None
    if not isinstance(value, dict):
        raise typer.BadParameter("filter must be a JSON object")
    return value


def _requirements_text(reqs: list[dict]) -> str:
    parts = []
    for r in reqs or []:
        if r.get("who") == "org_role":
            parts.append(f"{r.get('count')} {r.get('role')}")
        else:
            parts.append(f"{r.get('count')} of {len(r.get('user_ids') or [])} named")
    return ", ".join(parts)


def _policies_url(org_id: str, policy_id: str | None = None) -> str:
    base = f"/api/v1/orgs/{_seg(org_id)}/approval-policies"
    return f"{base}/{_seg(policy_id)}" if policy_id else base


# ── policies ───────────────────────────────────────────────────────


@policy_app.command("list")
@handle_errors
def policy_list(
    org: str | None = typer.Option(None, "--org", help=_ORG_HELP),
    all_: bool = typer.Option(False, "--all", help="Every version, including inactive (admins)."),
) -> None:
    """List approval policies."""
    org_id = _org_id(org)
    body = _call("GET", _policies_url(org_id), params={"include_inactive": "true"} if all_ else None,
                 not_found="Organization not found.")
    policies = body.get("policies") or []
    if not policies:
        console.print("[dim]No approval policies.[/dim]")
        return
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Target")
    table.add_column("Mode")
    table.add_column("Needs")
    table.add_column("Version")
    for p in policies:
        mode = "blocking" if p.get("blocking") else "advisory"
        if not p.get("active"):
            mode += " (inactive)"
        table.add_row(
            p.get("id", ""), escape(str(p.get("name", ""))), p.get("target_kind", ""), mode,
            escape(_requirements_text(p.get("requirements") or [])), str(p.get("version", "")),
        )
    console.print(table)
    if not body.get("plan_includes_approvals", True):
        console.print("[yellow]This org's plan does not include approval policies; "
                      "existing ones can still be read and disabled.[/yellow]")


@policy_app.command("create")
@handle_errors
def policy_create(
    name: str = typer.Option(..., "--name", help="Policy name."),
    target: str = typer.Option(..., "--target", help="merge or kb_promote."),
    require: list[str] = typer.Option(..., "--require", help="role:admin:1, role:owner:1 or users:ID[,ID]:COUNT (repeatable)."),
    filter_json: str | None = typer.Option(None, "--filter", help="Filter as a JSON object."),
    filter_file: Path | None = typer.Option(None, "--filter-file", help="Read the filter JSON from a file."),
    advisory: bool = typer.Option(False, "--advisory", help="Warn only; never block."),
    description: str = typer.Option("", "--description"),
    org: str | None = typer.Option(None, "--org", help=_ORG_HELP),
) -> None:
    """Create an approval policy (org owners and admins, Enterprise)."""
    if target not in ("merge", "kb_promote"):
        raise typer.BadParameter("--target must be merge or kb_promote")
    body = {
        "name": name,
        "description": description,
        "target_kind": target,
        "filter": parse_filter(filter_json, filter_file) or {},
        "requirements": [parse_requirement(r) for r in require],
        "blocking": not advisory,
    }
    org_id = _org_id(org)
    p = _call("POST", _policies_url(org_id), json_data=body, not_found="Organization not found.")
    console.print(f"[green]Policy created[/green] {p.get('id', '')} ({escape(str(p.get('name', '')))})")


@policy_app.command("update")
@handle_errors
def policy_update(
    policy_id: str = typer.Argument(..., help="The active version's id (ap_...)."),
    name: str | None = typer.Option(None, "--name"),
    description: str | None = typer.Option(None, "--description"),
    require: list[str] | None = typer.Option(None, "--require", help="Replace the requirements (repeatable)."),
    filter_json: str | None = typer.Option(None, "--filter", help="Replace the filter (JSON object)."),
    filter_file: Path | None = typer.Option(None, "--filter-file"),
    blocking: bool | None = typer.Option(None, "--blocking/--advisory", help="Blocking or advisory."),
    org: str | None = typer.Option(None, "--org", help=_ORG_HELP),
) -> None:
    """Edit a policy. This creates a new version; open requests stay on the old one."""
    flt = parse_filter(filter_json, filter_file)
    reqs = [parse_requirement(r) for r in require] if require else None
    if all(v is None for v in (name, description, reqs, flt, blocking)):
        raise typer.BadParameter("nothing to change")
    org_id = _org_id(org)
    listing = _call("GET", _policies_url(org_id), not_found="Organization not found.")
    current = next((p for p in listing.get("policies") or [] if p.get("id") == policy_id), None)
    if current is None:
        err_console.print("[red]Approval policy not found (or not the active version).[/red]")
        raise typer.Exit(1)
    if "filter" not in current:
        err_console.print("[red]Editing a policy needs the admin or owner role.[/red]")
        raise typer.Exit(1)
    body = {
        "name": name if name is not None else current.get("name"),
        "description": description if description is not None else current.get("description") or "",
        "target_kind": current.get("target_kind"),
        "filter": flt if flt is not None else current.get("filter") or {},
        "requirements": reqs if reqs is not None else current.get("requirements"),
        "blocking": blocking if blocking is not None else bool(current.get("blocking")),
    }
    p = _call("PUT", _policies_url(org_id, policy_id), json_data=body,
              not_found="Approval policy not found.")
    console.print(f"[green]Policy updated[/green] {p.get('id', '')} (version {p.get('version', '')})")


@policy_app.command("disable")
@handle_errors
def policy_disable(
    policy_id: str = typer.Argument(...),
    org: str | None = typer.Option(None, "--org", help=_ORG_HELP),
) -> None:
    """Deactivate a policy. Works on any plan, so a gate can always be cleared."""
    org_id = _org_id(org)
    _call("DELETE", _policies_url(org_id, policy_id), not_found="Approval policy not found.")
    console.print("[green]Policy disabled.[/green]")


# ── requests ───────────────────────────────────────────────────────


@approval_app.command("list")
@handle_errors
def list_() -> None:
    """Pending approval requests you could decide now."""
    body = _call("GET", "/api/v1/me/approvals")
    items = body.get("requests") or []
    if not items:
        console.print("[dim]Nothing waiting on you.[/dim]")
        return
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Policy")
    table.add_column("Target")
    table.add_column("Title")
    table.add_column("Approvals")
    table.add_column("Expires", style="dim")
    for r in items:
        target = f"{r.get('target_kind')}:{r.get('target_id')}"
        table.add_row(
            r.get("id", ""), escape(str(r.get("policy_name") or "")), escape(target),
            escape(str(r.get("target_title") or "")),
            f"{r.get('approved_count', 0)} of {r.get('required_count', 0)}",
            (r.get("expires_at") or "")[:16],
        )
    console.print(table)


@approval_app.command("show")
@handle_errors
def show(request_id: str = typer.Argument(...)) -> None:
    """Show one approval request and its decisions."""
    r = _call("GET", f"/api/v1/approval-requests/{_seg(request_id)}",
              not_found="Approval request not found.")
    policy = r.get("policy") or {}
    console.print(f"[bold]{escape(str(policy.get('name') or 'Approval'))}[/bold]  [cyan]{r.get('id', '')}[/cyan]")
    console.print(f"  Policy:   {policy.get('id', '')} v{policy.get('version', '')}"
                  f" ({'blocking' if policy.get('blocking') else 'advisory'})")
    console.print(f"  Target:   {r.get('target_kind')}:{r.get('target_id')} @ {r.get('target_fingerprint', '')[:12]}")
    console.print(f"  State:    {r.get('state')}  ({r.get('approved_count', 0)} of {r.get('required_count', 0)})")
    console.print(f"  Expires:  {(r.get('expires_at') or '')[:16]}")
    if r.get("can_decide"):
        console.print("  [yellow]You can decide this request.[/yellow]")
    for d in r.get("decisions") or []:
        reason = f" — {escape(str(d.get('reason')))}" if d.get("reason") else ""
        console.print(
            f"   [dim]{(d.get('decided_at') or '')[:16]}[/dim] "
            f"{escape(str(d.get('approver_name') or d.get('approver_user_id') or ''))}: "
            f"{d.get('decision')}{reason}"
        )


def _decide(request_id: str, decision: str, reason: str) -> None:
    r = _call("POST", f"/api/v1/approval-requests/{_seg(request_id)}/decide",
              json_data={"decision": decision, "reason": reason},
              not_found="Approval request not found.")
    console.print(f"[green]Recorded.[/green] Request is now {r.get('state')} "
                  f"({r.get('approved_count', 0)} of {r.get('required_count', 0)}).")


@approval_app.command("approve")
@handle_errors
def approve(
    request_id: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason", help="Why (stored with the decision)."),
) -> None:
    """Approve a request."""
    _decide(request_id, "approve", reason)


@approval_app.command("reject")
@handle_errors
def reject(
    request_id: str = typer.Argument(...),
    reason: str = typer.Option(..., "--reason", help="Why (stored with the decision)."),
) -> None:
    """Reject a request (one reject rejects the whole request)."""
    _decide(request_id, "reject", reason)
