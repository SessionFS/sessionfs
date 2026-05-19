"""v0.10.11 — CLI for scoped service keys + personal user keys.

Two typer apps registered into the existing CLI:

  sfs admin service-keys list|create|revoke|rotate   (org-admin, Team+)
  sfs auth keys list|create|revoke                   (any user)

Wraps the v0.10.10 endpoints in src/sessionfs/server/routes/api_keys.py.
No backend / schema / migration changes — pure CLI surface.

Raw key handling: `create` + `rotate` print the raw key to stdout
exactly once with a "save this — you won't see it again" warning to
stderr. `--output-key` flag prints ONLY the raw key (no decorations)
for `KEY=$(sfs admin service-keys create ... --output-key)` CI capture.

Scope validation: the 14-scope vocabulary is imported from
sessionfs.server.schemas.api_keys.VALID_SCOPES so the CLI and server
share one source of truth. The `*` wildcard is reserved for legacy
user keys — service-key creates with `*` are rejected locally for a
fast error rather than the 422 round-trip.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import typer
from rich.table import Table

from sessionfs.cli.common import console, err_console
from sessionfs.cli.cmd_rules import _api_request, _get_api_config

service_keys_app = typer.Typer(
    name="service-keys",
    help="Manage org-scoped service keys for cloud agents / CI / Bedrock / Vertex.",
)
auth_keys_app = typer.Typer(
    name="keys",
    help="Manage your personal API keys.",
)


def _valid_scopes() -> frozenset[str]:
    """Single source of truth — imported from server schemas. Failing
    open here would let invalid scopes slip through to the server's 422
    instead of a fast local error, so we defer the import to call-time
    rather than module import to avoid hard-failing the whole CLI if a
    user's install is missing the server package."""
    from sessionfs.server.schemas.api_keys import VALID_SCOPES
    return VALID_SCOPES


def _print_raw_key(raw: str, output_key: bool) -> None:
    """Surface the raw key per the v0.10.10 secret-handling rules.

    --output-key mode: stdout = raw key only, no trailing newline beyond
    typer's default. Suitable for `KEY=$(sfs ... --output-key)`.

    Default mode: pretty stdout + stderr warning. The warning is on
    stderr so it does NOT pollute pipes, but is still visible in a
    terminal.
    """
    if output_key:
        # Match `sfs ticket create --output-id` semantics — print to
        # stdout via typer.echo so a CI runner can capture without rich
        # ANSI codes muddying the value.
        typer.echo(raw)
        return
    console.print(f"\n[bold cyan]Raw key:[/bold cyan] {raw}\n")
    err_console.print(
        "[yellow]Save this key now — it will not be shown again.[/yellow]"
    )


def _parse_error(status: int, body) -> str:
    """Render a server error body into a single-line message.

    Handles the four shapes the v0.10.10 backend can return:

      1. {"error": {"code": "X", "message": "Y"}}
         Custom structured error envelope used by the auth dependency.

      2. {"detail": {"error": "X", "message": "Y"}}
         FastAPI's wrapper when a route does
         `HTTPException(detail={...})`. Used by tier_gate.py upgrade
         prompts. "error" is the code field here.

      3. {"detail": "Plain string"}
         Most FastAPI HTTPExceptions (e.g. "Organization not found").

      4. {"detail": [{loc, msg, type}, ...]}
         Pydantic validation errors (422). Render the first error's
         loc + msg — the full list would be noisy.

    Codex R1 MEDIUM on tk_53e042ecee7e43ff: the earlier implementation
    fell through to body.get("message") on shape #2/#3/#4, returning
    the literal string "None" because no top-level "message" key
    exists.
    """
    if not isinstance(body, dict):
        return f"HTTP {status}: {body}"

    # Shape 1: top-level structured error envelope.
    top_error = body.get("error")
    if isinstance(top_error, dict):
        code = top_error.get("code") or top_error.get("error")
        msg = top_error.get("message") or ""
        return f"{code}: {msg}" if code else str(msg)

    detail = body.get("detail")

    # Shape 2: detail-wrapped structured envelope.
    if isinstance(detail, dict):
        code = detail.get("code") or detail.get("error")
        msg = detail.get("message") or detail.get("detail") or ""
        return f"{code}: {msg}" if code else str(msg)

    # Shape 4: Pydantic validation errors.
    if isinstance(detail, list) and detail:
        first = detail[0]
        if isinstance(first, dict):
            loc = ".".join(str(p) for p in first.get("loc", []))
            msg = first.get("msg") or first.get("message") or "validation error"
            return f"validation error at {loc}: {msg}" if loc else str(msg)
        return str(first)

    # Shape 3: plain string detail (or anything else).
    if isinstance(detail, str) and detail:
        return detail

    return f"HTTP {status}: {body}"


# ── sfs admin service-keys ───────────────────────────────────────────


@service_keys_app.command("list")
def service_keys_list(
    org_id: str = typer.Option(..., "--org", "-o", help="Organization id"),
) -> None:
    """List service keys in an org. Org admin + Team+ tier required."""
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request(
            "GET",
            f"/api/v1/orgs/{org_id}/service-keys",
            api_url,
            api_key,
        )
    )
    if status >= 400:
        err_console.print(f"[red]Error[/red]: {_parse_error(status, body)}")
        raise typer.Exit(1)

    if not body:
        console.print("[dim]No service keys in this org.[/dim]")
        return

    table = Table(title=f"Service keys — org {org_id}")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Prefix")
    table.add_column("Scopes")
    table.add_column("Project IDs")
    table.add_column("Expires")
    table.add_column("Active")
    for row in body:
        scopes = ", ".join(row.get("scopes") or [])
        projects = ", ".join(row.get("project_ids") or []) or "[dim]all[/dim]"
        expires = row.get("expires_at") or "[dim]never[/dim]"
        active = (
            "[green]yes[/green]" if row.get("is_active") else "[red]revoked[/red]"
        )
        table.add_row(
            row["id"],
            row.get("name") or "",
            row.get("key_prefix") or "",
            scopes,
            projects,
            str(expires),
            active,
        )
    console.print(table)


@service_keys_app.command("create")
def service_keys_create(
    org_id: str = typer.Option(..., "--org", "-o", help="Organization id"),
    name: str = typer.Option(..., "--name", "-n", help="Human label"),
    scopes: list[str] = typer.Option(
        ...,
        "--scope",
        "-s",
        help=(
            "Capability scope (repeatable). e.g. handoffs:write, "
            "tickets:read. Run `sfs admin service-keys scopes` for the "
            "full list."
        ),
    ),
    expires_days: Optional[int] = typer.Option(
        None,
        "--expires-days",
        help="Key lifetime in days (1–730). Omit for no expiry.",
    ),
    project_ids: list[str] = typer.Option(
        [],
        "--project",
        "-p",
        help=(
            "Optional per-key project allowlist (repeatable). Omit to allow "
            "all projects in the org."
        ),
    ),
    output_key: bool = typer.Option(
        False,
        "--output-key",
        help=(
            "Print ONLY the raw key to stdout (no decorations). CI-safe — "
            "use `KEY=$(sfs admin service-keys create ... --output-key)`."
        ),
    ),
) -> None:
    """Mint a new scoped service key. Returns the raw key ONCE."""
    # Local scope validation — fail fast before hitting the network so
    # users see the 14-scope vocab on a typo, not a 422 round-trip.
    valid = _valid_scopes()
    if "*" in scopes:
        err_console.print(
            "[red]Error[/red]: service keys cannot use the '*' wildcard scope. "
            "Enumerate specific scopes (e.g. handoffs:write, tickets:read). "
            "The '*' wildcard is reserved for legacy personal user keys."
        )
        raise typer.Exit(2)
    unknown = sorted(set(scopes) - valid)
    if unknown:
        err_console.print(
            f"[red]Error[/red]: unknown scopes {unknown}. "
            f"Valid scopes: {sorted(valid)}"
        )
        raise typer.Exit(2)

    api_url, api_key = _get_api_config()
    payload: dict = {
        "name": name,
        "scopes": scopes,
    }
    if expires_days is not None:
        payload["expires_in_days"] = expires_days
    if project_ids:
        payload["project_ids"] = project_ids

    status, body, _ = asyncio.run(
        _api_request(
            "POST",
            f"/api/v1/orgs/{org_id}/service-keys",
            api_url,
            api_key,
            json_data=payload,
        )
    )
    if status >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]Error[/red]: {_parse_error(status, body)}")
        raise typer.Exit(1)

    raw = body.get("key")
    if not isinstance(raw, str) or not raw:
        err_console.print(
            "[red]Error[/red]: server did not return a raw key on create. "
            "This is a bug — please report."
        )
        raise typer.Exit(1)

    if not output_key:
        console.print(
            f"[green]Service key created[/green]: "
            f"id={body.get('id')} name={body.get('name')} "
            f"prefix={body.get('key_prefix')}"
        )
    _print_raw_key(raw, output_key)


@service_keys_app.command("revoke")
def service_keys_revoke(
    key_id: str = typer.Argument(..., help="Service key id"),
    org_id: str = typer.Option(..., "--org", "-o", help="Organization id"),
    reason: str = typer.Option(
        ...,
        "--reason",
        "-r",
        help="Why this key is being revoked (audit trail; required).",
    ),
) -> None:
    """Revoke a service key. Soft delete + audited reason."""
    if not reason.strip():
        err_console.print("[red]Error[/red]: --reason cannot be blank.")
        raise typer.Exit(2)
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request(
            "DELETE",
            f"/api/v1/orgs/{org_id}/service-keys/{key_id}",
            api_url,
            api_key,
            json_data={"reason": reason.strip()},
        )
    )
    if status == 404:
        err_console.print(
            f"[red]Error[/red]: service key {key_id} not found in org {org_id}."
        )
        raise typer.Exit(1)
    if status >= 400:
        err_console.print(f"[red]Error[/red]: {_parse_error(status, body)}")
        raise typer.Exit(1)
    console.print(f"[green]Revoked[/green] service key {key_id}.")


@service_keys_app.command("rotate")
def service_keys_rotate(
    key_id: str = typer.Argument(..., help="Service key id"),
    org_id: str = typer.Option(..., "--org", "-o", help="Organization id"),
    output_key: bool = typer.Option(
        False,
        "--output-key",
        help="Print ONLY the new raw key to stdout (no decorations).",
    ),
) -> None:
    """Issue a new raw secret with the same scopes/expiry/project allowlist.
    The old key is revoked atomically with the new one being minted."""
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request(
            "POST",
            f"/api/v1/orgs/{org_id}/service-keys/{key_id}/rotate",
            api_url,
            api_key,
        )
    )
    if status == 404:
        err_console.print(
            f"[red]Error[/red]: service key {key_id} not found in org {org_id}."
        )
        raise typer.Exit(1)
    if status == 409:
        err_console.print(
            f"[red]Error[/red]: cannot rotate revoked key {key_id} — "
            f"create a new one with `sfs admin service-keys create`."
        )
        raise typer.Exit(1)
    if status >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]Error[/red]: {_parse_error(status, body)}")
        raise typer.Exit(1)

    raw = body.get("key")
    if not isinstance(raw, str) or not raw:
        err_console.print(
            "[red]Error[/red]: server did not return a raw key on rotate."
        )
        raise typer.Exit(1)

    if not output_key:
        console.print(
            f"[green]Rotated[/green]: new id={body.get('id')} "
            f"prefix={body.get('key_prefix')} (old key revoked)"
        )
    _print_raw_key(raw, output_key)


@service_keys_app.command("scopes")
def service_keys_scopes() -> None:
    """Print the 14-scope vocabulary."""
    valid = sorted(_valid_scopes())
    for s in valid:
        console.print(s)


# ── sfs auth keys (personal user keys) ───────────────────────────────


@auth_keys_app.command("list")
def auth_keys_list() -> None:
    """List your personal API keys."""
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request("GET", "/api/v1/auth/me/api-keys", api_url, api_key)
    )
    if status >= 400:
        err_console.print(f"[red]Error[/red]: {_parse_error(status, body)}")
        raise typer.Exit(1)

    if not body:
        console.print("[dim]No personal API keys.[/dim]")
        return

    table = Table(title="Personal API keys")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Prefix")
    table.add_column("Expires")
    table.add_column("Last used")
    table.add_column("Active")
    for row in body:
        expires = row.get("expires_at") or "[dim]never[/dim]"
        last_used = row.get("last_used_at") or "[dim]never[/dim]"
        active = (
            "[green]yes[/green]" if row.get("is_active") else "[red]revoked[/red]"
        )
        table.add_row(
            row["id"],
            row.get("name") or "",
            row.get("key_prefix") or "",
            str(expires),
            str(last_used),
            active,
        )
    console.print(table)


@auth_keys_app.command("create")
def auth_keys_create(
    name: str = typer.Option(..., "--name", "-n", help="Human label"),
    expires_days: Optional[int] = typer.Option(
        None,
        "--expires-days",
        help="Key lifetime in days (1–730). Omit for no expiry.",
    ),
    output_key: bool = typer.Option(
        False,
        "--output-key",
        help="Print ONLY the raw key to stdout (no decorations).",
    ),
) -> None:
    """Mint a personal API key. Returns the raw key ONCE."""
    api_url, api_key = _get_api_config()
    payload: dict = {"name": name}
    if expires_days is not None:
        payload["expires_in_days"] = expires_days

    status, body, _ = asyncio.run(
        _api_request(
            "POST",
            "/api/v1/auth/me/api-keys",
            api_url,
            api_key,
            json_data=payload,
        )
    )
    if status >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]Error[/red]: {_parse_error(status, body)}")
        raise typer.Exit(1)

    raw = body.get("key")
    if not isinstance(raw, str) or not raw:
        err_console.print(
            "[red]Error[/red]: server did not return a raw key on create."
        )
        raise typer.Exit(1)

    if not output_key:
        console.print(
            f"[green]Personal API key created[/green]: "
            f"id={body.get('id')} name={body.get('name')} "
            f"prefix={body.get('key_prefix')}"
        )
    _print_raw_key(raw, output_key)


@auth_keys_app.command("revoke")
def auth_keys_revoke(
    key_id: str = typer.Argument(..., help="Personal key id"),
    reason: str = typer.Option(
        ...,
        "--reason",
        "-r",
        help="Why this key is being revoked (required).",
    ),
) -> None:
    """Revoke a personal API key. Soft delete + audited reason."""
    if not reason.strip():
        err_console.print("[red]Error[/red]: --reason cannot be blank.")
        raise typer.Exit(2)
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request(
            "DELETE",
            f"/api/v1/auth/me/api-keys/{key_id}",
            api_url,
            api_key,
            json_data={"reason": reason.strip()},
        )
    )
    if status == 404:
        err_console.print(f"[red]Error[/red]: personal key {key_id} not found.")
        raise typer.Exit(1)
    if status >= 400:
        err_console.print(f"[red]Error[/red]: {_parse_error(status, body)}")
        raise typer.Exit(1)
    console.print(f"[green]Revoked[/green] personal key {key_id}.")
