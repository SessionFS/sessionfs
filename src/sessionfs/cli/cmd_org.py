"""Organization management commands: sfs org info, sfs org invite, sfs org members."""

from __future__ import annotations

import typer
from rich.table import Table

from sessionfs.cli.common import console, err_console

org_app = typer.Typer(name="org", help="Organization management.")


def _load_api_config() -> tuple[str, str]:
    """Load API URL and key from config."""
    from sessionfs.daemon.config import load_config

    config = load_config()
    api_url = config.sync.api_url
    api_key = config.sync.api_key
    if not api_key:
        err_console.print("[red]Not authenticated. Run 'sfs auth signup' first.[/red]")
        raise SystemExit(1)
    return api_url, api_key


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


@org_app.command("info")
def org_info() -> None:
    """Show organization info and member count."""
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    api_url, api_key = _load_api_config()

    try:
        resp = httpx.get(f"{api_url}/api/v1/org", headers=_headers(api_key), timeout=15.0)
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)

    handle_api_response(resp)
    data = resp.json()
    org = data.get("org")

    if not org:
        console.print("You are not part of an organization.")
        console.print("Create one with: sfs org create <name> <slug>")
        return

    console.print(f"\n[bold]{org['name']}[/bold] ({org['slug']})")
    console.print(f"  Tier: {org['tier']}")
    console.print(f"  Seats: {org['seats_used']} / {org['seats_limit']}")
    console.print(f"  Your role: {data.get('current_user_role', 'unknown')}")

    members = data.get("members", [])
    if members:
        table = Table(title="Members")
        table.add_column("Email")
        table.add_column("Role")
        table.add_column("Joined")
        for m in members:
            table.add_row(m["email"], m["role"], (m.get("joined_at") or "")[:10])
        console.print(table)


@org_app.command("create")
def org_create(
    name: str = typer.Argument(help="Organization display name"),
    slug: str = typer.Argument(help="URL-safe slug (e.g. 'baptist-health')"),
) -> None:
    """Create a new organization. You become the admin."""
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    api_url, api_key = _load_api_config()

    try:
        resp = httpx.post(
            f"{api_url}/api/v1/org",
            headers=_headers(api_key),
            json={"name": name, "slug": slug},
            timeout=15.0,
        )
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)

    handle_api_response(resp)
    data = resp.json()
    console.print(f"[green]Organization created: {data['name']} ({data['slug']})[/green]")
    console.print(f"  Org ID: {data['org_id']}")


@org_app.command("invite")
def org_invite(
    email: str = typer.Argument(help="Email address to invite"),
    role: str = typer.Option("member", help="Role: 'admin' or 'member'"),
) -> None:
    """Invite a user to your organization (admin only)."""
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    api_url, api_key = _load_api_config()

    try:
        resp = httpx.post(
            f"{api_url}/api/v1/org/invite",
            headers=_headers(api_key),
            json={"email": email, "role": role},
            timeout=15.0,
        )
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)

    handle_api_response(resp)
    data = resp.json()
    console.print(f"[green]Invite sent to {data['email']} as {data['role']}[/green]")
    console.print(f"  Invite ID: {data['invite_id']}")
    console.print("  The invite expires in 7 days.")


@org_app.command("members")
def org_members() -> None:
    """List all members in your organization."""
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    api_url, api_key = _load_api_config()

    try:
        resp = httpx.get(f"{api_url}/api/v1/org", headers=_headers(api_key), timeout=15.0)
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)

    handle_api_response(resp)
    data = resp.json()
    org = data.get("org")

    if not org:
        console.print("You are not part of an organization.")
        return

    members = data.get("members", [])
    table = Table(title=f"{org['name']} — Members ({len(members)}/{org['seats_limit']})")
    table.add_column("Email")
    table.add_column("Name")
    table.add_column("Role")
    table.add_column("Joined")

    for m in members:
        table.add_row(
            m["email"],
            m.get("display_name") or "",
            m["role"],
            (m.get("joined_at") or "")[:10],
        )

    console.print(table)


@org_app.command("remove")
def org_remove(
    user_id: str = typer.Argument(help="User ID to remove"),
) -> None:
    """Remove a member from your organization (admin only)."""
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    api_url, api_key = _load_api_config()

    try:
        resp = httpx.delete(
            f"{api_url}/api/v1/org/members/{user_id}",
            headers=_headers(api_key),
            timeout=15.0,
        )
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)

    handle_api_response(resp)
    console.print(f"[green]Member {user_id} removed.[/green]")
