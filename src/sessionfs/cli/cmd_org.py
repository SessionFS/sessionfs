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


def _fetch_members(api_url: str, api_key: str) -> list[dict]:
    """Return the org's member list (empty when not in an org)."""
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    try:
        resp = httpx.get(f"{api_url}/api/v1/org", headers=_headers(api_key), timeout=15.0)
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)
    handle_api_response(resp)
    return (resp.json().get("members") or [])


def _resolve_member_id_or_error(
    api_url: str, api_key: str, token: str
) -> tuple[str | None, str | None]:
    """Resolve a user ID or email to a member user_id.

    Returns ``(user_id, None)`` on success or ``(None, reason)`` on failure
    so bulk callers can collect every failure before acting.
    """
    if "@" not in token:
        return token, None
    members = _fetch_members(api_url, api_key)
    needle = token.strip().lower()
    matches = [m for m in members if (m.get("email") or "").strip().lower() == needle]
    if not matches:
        return None, f"no member with email '{token}'"
    if len(matches) > 1:
        return None, f"ambiguous email '{token}'"
    return matches[0]["user_id"], None


def _resolve_member_id(api_url: str, api_key: str, token: str) -> str:
    user_id, err = _resolve_member_id_or_error(api_url, api_key, token)
    if err:
        err_console.print(f"[red]{err}[/red]")
        raise SystemExit(1)
    return user_id


def _describe_error(resp) -> str:
    """Format an httpx error response as a one-line message (no exit)."""
    from sessionfs.cli.common import format_api_error

    try:
        body = resp.json()
    except Exception:
        body = resp.text
    return format_api_error(body, resp.status_code)


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


@org_app.command("set-role")
def org_set_role(
    user_id_or_email: str = typer.Argument(help="User ID or email of the member"),
    role: str = typer.Argument(help="New role: 'admin' or 'member'"),
) -> None:
    """Change a member's role (admin only). Ownership transfer stays separate."""
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    if role not in ("admin", "member"):
        err_console.print(
            "[red]Role must be 'admin' or 'member'. "
            "Ownership transfer uses a separate command.[/red]"
        )
        raise SystemExit(2)

    api_url, api_key = _load_api_config()
    target_id = _resolve_member_id(api_url, api_key, user_id_or_email)

    try:
        resp = httpx.put(
            f"{api_url}/api/v1/org/members/{target_id}/role",
            headers=_headers(api_key),
            json={"role": role},
            timeout=15.0,
        )
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)

    handle_api_response(resp)
    console.print(f"[green]Role for {user_id_or_email} changed to {role}.[/green]")


@org_app.command("remove")
def org_remove(
    user_id_or_email: str = typer.Argument(None, help="User ID or email to remove"),
    bulk: str | None = typer.Option(
        None,
        "--bulk",
        help="File with one user ID/email per line (# comments and blank lines ignored)",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation (bulk only)"),
) -> None:
    """Remove a member from your organization (admin only)."""
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    api_url, api_key = _load_api_config()

    if bulk:
        _remove_bulk(api_url, api_key, bulk, yes)
        return

    if not user_id_or_email:
        err_console.print(
            "[red]Provide a user ID/email to remove, or use --bulk <file>.[/red]"
        )
        raise SystemExit(2)

    target_id = _resolve_member_id(api_url, api_key, user_id_or_email)
    try:
        resp = httpx.delete(
            f"{api_url}/api/v1/org/members/{target_id}",
            headers=_headers(api_key),
            timeout=15.0,
        )
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)

    handle_api_response(resp)
    console.print(f"[green]Member {user_id_or_email} removed.[/green]")


def _remove_bulk(api_url: str, api_key: str, path: str, yes: bool) -> None:
    """Remove many members listed in a file, one per line.

    Resolves ALL entries first — if any fail to resolve, print them and
    abort before removing anything. Otherwise show the list, confirm
    (skippable with --yes), then remove one at a time, continuing past
    individual failures. Exit non-zero if any removal failed.
    """
    import httpx
    from pathlib import Path
    from rich.prompt import Confirm

    try:
        lines = Path(path).read_text().splitlines()
    except OSError as exc:
        err_console.print(f"[red]Could not read file: {exc}[/red]")
        raise SystemExit(1)

    tokens = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not tokens:
        err_console.print("[red]No entries found in file.[/red]")
        raise SystemExit(1)

    # Resolve ALL first against one roster fetch — emails AND user IDs must
    # be current members, and no member may be listed twice. Abort before
    # removing anything on any failure.
    members = _fetch_members(api_url, api_key)
    by_id = {m.get("user_id"): m for m in members if m.get("user_id")}
    resolved: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for token in tokens:
        if "@" in token:
            needle = token.lower()
            matches = [
                m for m in members if (m.get("email") or "").strip().lower() == needle
            ]
            if not matches:
                failures.append((token, f"no member with email '{token}'"))
                continue
            if len(matches) > 1:
                failures.append((token, f"ambiguous email '{token}'"))
                continue
            user_id = matches[0]["user_id"]
        elif token in by_id:
            user_id = token
        else:
            failures.append((token, f"no member with user ID '{token}'"))
            continue
        if user_id in seen:
            failures.append((token, f"same member as '{seen[user_id]}'"))
            continue
        seen[user_id] = token
        resolved.append((token, user_id))

    if failures:
        err_console.print("[red]Could not resolve some entries — nothing removed:[/red]")
        for token, err in failures:
            err_console.print(f"  {token}: {err}")
        raise SystemExit(1)

    console.print("[bold]About to remove:[/bold]")
    for token, _user_id in resolved:
        console.print(f"  - {token}")

    if not yes and not Confirm.ask("Remove these members?"):
        console.print("Aborted.")
        return

    removed = 0
    remove_failures: list[tuple[str, str]] = []
    for token, user_id in resolved:
        try:
            resp = httpx.delete(
                f"{api_url}/api/v1/org/members/{user_id}",
                headers=_headers(api_key),
                timeout=15.0,
            )
        except httpx.ConnectError:
            remove_failures.append((token, f"Could not reach server at {api_url}"))
            continue
        if resp.status_code >= 400:
            remove_failures.append((token, _describe_error(resp)))
        else:
            removed += 1

    console.print(f"[green]Removed {removed} of {len(resolved)} member(s).[/green]")
    if remove_failures:
        for token, reason in remove_failures:
            err_console.print(f"[red]Failed: {token}: {reason}[/red]")
        raise SystemExit(1)


# ── sfs org auto-join ───────────────────────────────────────────────────


auto_join_app = typer.Typer(help="Auto-join by verified email domain.")


def _org_id(api_url: str, api_key: str) -> str:
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    try:
        resp = httpx.get(f"{api_url}/api/v1/org", headers=_headers(api_key), timeout=15.0)
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)
    handle_api_response(resp)
    org = (resp.json() or {}).get("org")
    if not org:
        err_console.print("[red]You are not part of an organization.[/red]")
        raise SystemExit(1)
    return org["id"]


def _get_settings(api_url: str, api_key: str, org_id: str) -> dict:
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    try:
        resp = httpx.get(
            f"{api_url}/api/v1/orgs/{org_id}/settings",
            headers=_headers(api_key),
            timeout=15.0,
        )
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)
    handle_api_response(resp)
    return resp.json()


def _put_settings(api_url: str, api_key: str, org_id: str, payload: dict) -> None:
    import httpx
    from sessionfs.cli.api_errors import handle_api_response

    try:
        resp = httpx.put(
            f"{api_url}/api/v1/orgs/{org_id}/settings",
            headers=_headers(api_key),
            json=payload,
            timeout=15.0,
        )
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)
    handle_api_response(resp)


def _full_settings_payload(settings: dict, domains: list[str]) -> dict:
    """Re-PUT the full general block (PUT replaces the whole block)."""
    return {
        "kb_retention_days": settings.get("kb_retention_days"),
        "kb_max_context_words": settings.get("kb_max_context_words"),
        "kb_section_page_limit": settings.get("kb_section_page_limit"),
        "auto_join_domains": domains,
    }


@auto_join_app.command("list")
def auto_join_list() -> None:
    """List domains configured for auto-join."""
    api_url, api_key = _load_api_config()
    org_id = _org_id(api_url, api_key)
    domains = _get_settings(api_url, api_key, org_id).get("auto_join_domains") or []
    if not domains:
        console.print("No auto-join domains configured.")
        return
    for domain in domains:
        console.print(domain)


@auto_join_app.command("add")
def auto_join_add(domain: str = typer.Argument(help="Verified domain to auto-join")) -> None:
    """Enable auto-join for a verified email domain (admin only)."""
    api_url, api_key = _load_api_config()
    org_id = _org_id(api_url, api_key)
    settings = _get_settings(api_url, api_key, org_id)
    domains = settings.get("auto_join_domains") or []
    normalized = domain.strip().lower()
    if normalized in domains:
        console.print(f"[yellow]Domain '{normalized}' is already enabled for auto-join.[/yellow]")
        return
    _put_settings(
        api_url, api_key, org_id, _full_settings_payload(settings, domains + [normalized])
    )
    console.print(f"[green]Enabled auto-join for {normalized}.[/green]")


@auto_join_app.command("remove")
def auto_join_remove(domain: str = typer.Argument(help="Domain to remove from auto-join")) -> None:
    """Disable auto-join for a domain (admin only)."""
    api_url, api_key = _load_api_config()
    org_id = _org_id(api_url, api_key)
    settings = _get_settings(api_url, api_key, org_id)
    domains = settings.get("auto_join_domains") or []
    normalized = domain.strip().lower()
    if normalized not in domains:
        console.print(f"[yellow]Domain '{normalized}' is not enabled for auto-join.[/yellow]")
        return
    _put_settings(
        api_url,
        api_key,
        org_id,
        _full_settings_payload(settings, [d for d in domains if d != normalized]),
    )
    console.print(f"[green]Disabled auto-join for {normalized}.[/green]")


org_app.add_typer(auto_join_app, name="auto-join")
