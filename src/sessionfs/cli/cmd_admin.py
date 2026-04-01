"""Admin commands: sfs admin reindex, license management."""

from __future__ import annotations

import asyncio
import typer
from rich.table import Table

from sessionfs.cli.common import console, err_console

admin_app = typer.Typer(name="admin", help="Server administration commands.")


def _get_sync_client():
    """Create a SyncClient from stored config."""
    from sessionfs.cli.cmd_cloud import _get_sync_client
    return _get_sync_client()


@admin_app.command("reindex")
def reindex() -> None:
    """Re-extract metadata from all session archives on the server.

    Backfills title, source_tool, model, stats, and tags for sessions
    that were pushed before metadata extraction was deployed.
    """
    from sessionfs.sync.client import SyncAuthError

    client = _get_sync_client()

    async def _reindex():
        try:
            http = await client._get_client()
            resp = await http.post(
                f"{client.api_url}/api/v1/sessions/admin/reindex",
                timeout=120.0,
            )
            if resp.status_code in (401, 403):
                raise SyncAuthError(f"Authentication failed: {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        finally:
            await client.close()

    console.print("Reindexing all sessions on the server...")
    try:
        result = asyncio.run(_reindex())
    except SyncAuthError:
        err_console.print("[red]Authentication failed. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)
    except Exception as exc:
        err_console.print(f"[red]Reindex failed: {exc}[/red]")
        raise SystemExit(1)

    console.print(
        f"[green]Reindex complete:[/green] "
        f"{result['reindexed']} sessions, "
        f"{result['updated']} updated, "
        f"{result['errors']} errors"
    )


# ---------------------------------------------------------------------------
# License management commands
# ---------------------------------------------------------------------------


def _admin_request(method: str, path: str, json_body: dict | None = None) -> dict:
    """Make an authenticated admin API request."""
    from sessionfs.sync.client import SyncAuthError

    client = _get_sync_client()

    async def _call():
        try:
            http = await client._get_client()
            url = f"{client.api_url}{path}"
            if method == "GET":
                resp = await http.get(url, timeout=30.0)
            elif method == "POST":
                resp = await http.post(url, json=json_body, timeout=30.0)
            elif method == "PUT":
                resp = await http.put(url, json=json_body, timeout=30.0)
            else:
                raise ValueError(f"Unsupported method: {method}")
            if resp.status_code in (401, 403):
                raise SyncAuthError(f"Authentication failed: {resp.status_code}")
            resp.raise_for_status()
            return resp.json()
        finally:
            await client.close()

    return asyncio.run(_call())


@admin_app.command("create-trial")
def create_trial(
    org: str = typer.Argument(..., help="Organization name"),
    email: str = typer.Argument(..., help="Contact email"),
    days: int = typer.Option(14, help="Trial duration in days"),
    seats: int = typer.Option(25, help="Seat limit"),
) -> None:
    """Create a trial license key."""
    try:
        result = _admin_request("POST", "/api/v1/admin/licenses/", {
            "org_name": org,
            "contact_email": email,
            "license_type": "trial",
            "tier": "enterprise",
            "seats_limit": seats,
            "days": days,
        })
    except Exception as exc:
        err_console.print(f"[red]Failed to create trial: {exc}[/red]")
        raise SystemExit(1)

    console.print(f"[green]Trial license created:[/green] {result['key']}")
    console.print(f"  Org: {result['org_name']}")
    console.print(f"  Expires: {result['expires_at']}")
    console.print(f"  Seats: {result['seats_limit']}")


@admin_app.command("create-license")
def create_license(
    org: str = typer.Argument(..., help="Organization name"),
    email: str = typer.Argument(..., help="Contact email"),
    tier: str = typer.Option("enterprise", help="License tier"),
    seats: int = typer.Option(25, help="Seat limit"),
) -> None:
    """Create a paid license key (no expiry)."""
    try:
        result = _admin_request("POST", "/api/v1/admin/licenses/", {
            "org_name": org,
            "contact_email": email,
            "license_type": "paid",
            "tier": tier,
            "seats_limit": seats,
        })
    except Exception as exc:
        err_console.print(f"[red]Failed to create license: {exc}[/red]")
        raise SystemExit(1)

    console.print(f"[green]License created:[/green] {result['key']}")
    console.print(f"  Org: {result['org_name']}")
    console.print(f"  Tier: {result['tier']}")
    console.print(f"  Seats: {result['seats_limit']}")


@admin_app.command("list-licenses")
def list_licenses(
    status: str = typer.Option("all", help="Filter: all, active, trial, expired, revoked"),
) -> None:
    """List all license keys."""
    try:
        result = _admin_request("GET", f"/api/v1/admin/licenses/?status={status}")
    except Exception as exc:
        err_console.print(f"[red]Failed to list licenses: {exc}[/red]")
        raise SystemExit(1)

    table = Table(title="Licenses")
    table.add_column("Key", style="cyan", no_wrap=True, max_width=36)
    table.add_column("Org")
    table.add_column("Type")
    table.add_column("Tier")
    table.add_column("Status")
    table.add_column("Seats", justify="right")
    table.add_column("Expires")
    table.add_column("Validations", justify="right")

    for lic in result.get("licenses", []):
        eff = lic.get("effective_status", "")
        style = {
            "active": "green",
            "trial": "blue",
            "expiring": "yellow",
            "expired": "red",
            "revoked": "dim red",
        }.get(eff, "")

        table.add_row(
            lic["key"][:36] + "...",
            lic["org_name"],
            lic["license_type"],
            lic["tier"],
            f"[{style}]{eff}[/{style}]" if style else eff,
            str(lic["seats_limit"] or ""),
            (lic["expires_at"] or "never")[:19],
            str(lic["validation_count"]),
        )

    console.print(table)
    console.print(f"Total: {result['total']}")


@admin_app.command("extend-trial")
def extend_trial(
    key: str = typer.Argument(..., help="License key to extend"),
    days: int = typer.Option(14, help="Days to extend from now"),
) -> None:
    """Extend a trial license by N days from now."""
    try:
        result = _admin_request("PUT", f"/api/v1/admin/licenses/{key}/extend", {
            "days": days,
        })
    except Exception as exc:
        err_console.print(f"[red]Failed to extend license: {exc}[/red]")
        raise SystemExit(1)

    console.print(f"[green]License extended:[/green] {result['key']}")
    console.print(f"  New expiry: {result['expires_at']}")


@admin_app.command("revoke-license")
def revoke_license(
    key: str = typer.Argument(..., help="License key to revoke"),
    reason: str = typer.Option("", help="Reason for revocation"),
) -> None:
    """Revoke a license key (immediate 403 on next validation)."""
    confirm = typer.confirm(f"Revoke license {key[:36]}...?")
    if not confirm:
        console.print("Cancelled.")
        raise SystemExit(0)

    try:
        body: dict = {"status": "revoked"}
        if reason:
            body["notes"] = reason
        result = _admin_request("PUT", f"/api/v1/admin/licenses/{key}", body)
    except Exception as exc:
        err_console.print(f"[red]Failed to revoke license: {exc}[/red]")
        raise SystemExit(1)

    console.print(f"[red]License revoked:[/red] {result['key']}")
