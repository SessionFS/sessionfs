"""`sfs contact …` — an org's external contacts (GitHub #125).

Commands:
- sfs contact list [--org ORG] [--q TEXT] [--status active|archived|all] [--tag T]
- sfs contact show CONTACT_ID [--refs]
- sfs contact create NAME [--email E] [--role R] [--tag T ...] [--alias A ...]
- sfs contact edit CONTACT_ID [--name N] [--email E | --clear-email] [--role R | --clear-role] [--tag T ...]
- sfs contact alias add CONTACT_ID ALIAS / sfs contact alias rm CONTACT_ID ALIAS
- sfs contact archive CONTACT_ID [--undo]
- sfs contact merge TARGET_ID SOURCE_ID [--yes]       (owners/admins; previews without --yes)
- sfs contact delete CONTACT_ID [--yes]               (owners/admins; erases the contact)
- sfs contact export CONTACT_ID [-o FILE]             (owners/admins; a data-request export)
- sfs contact export-all [-o FILE]                    (owners/admins; CSV of the directory)
- sfs contact suggestions [--apply NAME --contact ID] (names in meetings/chats nobody confirmed)

A thin client of ``/api/v1/orgs/{org}/contacts``. Visibility, email masking,
DLP and who may merge or delete are enforced server-side. Contacts are only
ever created by a person; extraction never creates one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import typer
from rich.markup import escape
from rich.table import Table

from sessionfs.profiles import _write_private_file  # exports hold personal data: 0600

from sessionfs.cli.cmd_approval import _call, _org_id, _seg
from sessionfs.cli.common import console, err_console, handle_errors

contact_app = typer.Typer(
    name="contact",
    help="External contacts: people your org works with who are not SessionFS users.",
    no_args_is_help=True,
)
alias_app = typer.Typer(name="alias", help="Add or remove a contact's other names.", no_args_is_help=True)
contact_app.add_typer(alias_app, name="alias")

_ORG_HELP = "Org id (defaults to your default org)."
_NOT_FOUND = "Contact not found."


def _base(org_id: str, contact_id: str | None = None) -> str:
    base = f"/api/v1/orgs/{_seg(org_id)}/contacts"
    return f"{base}/{_seg(contact_id)}" if contact_id else base


def _print_contact(c: dict) -> None:
    console.print(f"[bold]{escape(c.get('display_name') or '')}[/bold]  [dim]{c.get('id')}[/dim]")
    if c.get("redirected_from"):
        console.print(f"[dim]({c['redirected_from']} was merged into this contact)[/dim]")
    rows = [
        ("Status", c.get("status")),
        ("Role", c.get("role")),
        ("Email", c.get("email") if c.get("email") else ("(hidden)" if c.get("has_email") else None)),
        ("Aliases", ", ".join(c.get("aliases") or []) or None),
        ("Tags", ", ".join(c.get("tags") or []) or None),
        ("Linked member", c.get("linked_user_id")),
    ]
    for label, value in rows:
        if value:
            console.print(f"  {label}: {escape(str(value))}")
    for dup in c.get("possible_duplicates") or []:
        console.print(
            f"[yellow]  Possible duplicate: {escape(dup.get('display_name') or '')} ({dup.get('id')})[/yellow]"
        )


@contact_app.command("list")
@handle_errors
def contact_list(
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
    q: str = typer.Option(None, "--q", help="Search names and aliases."),
    status: str = typer.Option("active", "--status", help="active, archived or all."),
    tag: str = typer.Option(None, "--tag"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """List the org's contacts, newest first."""
    org_id = _org_id(org)
    params: dict[str, Any] = {"status": status, "limit": limit}
    if q:
        params["q"] = q
    if tag:
        params["tag"] = tag
    body = _call("GET", _base(org_id), params=params, not_found="Organization not found.")
    if as_json:
        console.print_json(json.dumps(body))
        return
    items = body.get("items") or []
    if not items:
        console.print("[dim]No contacts.[/dim]")
        return
    table = Table("Id", "Name", "Role", "Status", "Aliases")
    for c in items:
        table.add_row(
            c["id"], escape(c["display_name"]), escape(c.get("role") or ""), c["status"],
            escape(", ".join(c.get("aliases") or [])),
        )
    console.print(table)
    if body.get("next_before"):
        console.print("[dim]More contacts exist; narrow with --q or raise --limit.[/dim]")


@contact_app.command("show")
@handle_errors
def contact_show(
    contact_id: str = typer.Argument(...),
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
    refs: bool = typer.Option(False, "--refs", help="Also list where the contact appears."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show one contact."""
    org_id = _org_id(org)
    body = _call("GET", _base(org_id, contact_id), not_found=_NOT_FOUND)
    references = None
    if refs:
        references = _call("GET", _base(org_id, body["id"]) + "/references", not_found=_NOT_FOUND)
    if as_json:
        console.print_json(json.dumps({"contact": body, "references": references}))
        return
    _print_contact(body)
    if references:
        for c in references.get("conversations") or []:
            console.print(f"  {c['kind']}: {escape(c.get('title') or '')} [dim]{c['meeting_id']}[/dim]")
        for t in references.get("tickets") or []:
            console.print(f"  ticket: {escape(t.get('title') or '')} [dim]{t['id']} ({t['status']})[/dim]")


@contact_app.command("create")
@handle_errors
def contact_create(
    name: str = typer.Argument(..., help="Display name."),
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
    email: str = typer.Option(None, "--email"),
    role: str = typer.Option(None, "--role", help='For example "clinical lead".'),
    tag: list[str] = typer.Option(None, "--tag", help="Repeatable."),
    alias: list[str] = typer.Option(None, "--alias", help="Another name (repeatable)."),
) -> None:
    """Create a contact (any org member)."""
    org_id = _org_id(org)
    payload: dict[str, Any] = {"display_name": name}
    if email:
        payload["email"] = email
    if role:
        payload["role"] = role
    if tag:
        payload["tags"] = list(tag)
    if alias:
        payload["aliases"] = list(alias)
    body = _call("POST", _base(org_id), json_data=payload, not_found="Organization not found.")
    console.print("[green]Contact created.[/green]")
    _print_contact(body)


@contact_app.command("edit")
@handle_errors
def contact_edit(
    contact_id: str = typer.Argument(...),
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
    name: str = typer.Option(None, "--name"),
    email: str = typer.Option(None, "--email"),
    clear_email: bool = typer.Option(False, "--clear-email"),
    role: str = typer.Option(None, "--role"),
    clear_role: bool = typer.Option(False, "--clear-role"),
    tag: list[str] = typer.Option(None, "--tag", help="Replaces the tags (repeatable)."),
) -> None:
    """Edit a contact's name, email, role or tags."""
    payload: dict[str, Any] = {}
    if name:
        payload["display_name"] = name
    if email and clear_email:
        raise typer.BadParameter("use either --email or --clear-email")
    if role and clear_role:
        raise typer.BadParameter("use either --role or --clear-role")
    if email:
        payload["email"] = email
    if clear_email:
        payload["email"] = None
    if role:
        payload["role"] = role
    if clear_role:
        payload["role"] = None
    if tag:
        payload["tags"] = list(tag)
    if not payload:
        err_console.print("[red]Nothing to change.[/red]")
        raise typer.Exit(1)
    org_id = _org_id(org)
    body = _call("PATCH", _base(org_id, contact_id), json_data=payload, not_found=_NOT_FOUND)
    _print_contact(body)


@contact_app.command("archive")
@handle_errors
def contact_archive(
    contact_id: str = typer.Argument(...),
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
    undo: bool = typer.Option(False, "--undo", help="Make the contact active again."),
) -> None:
    """Archive a contact: it stays on its references but drops out of matching."""
    org_id = _org_id(org)
    body = _call(
        "PATCH", _base(org_id, contact_id),
        json_data={"status": "active" if undo else "archived"}, not_found=_NOT_FOUND,
    )
    console.print(f"[green]{escape(body['display_name'])} is now {body['status']}.[/green]")


@alias_app.command("add")
@handle_errors
def alias_add(
    contact_id: str = typer.Argument(...),
    alias: str = typer.Argument(...),
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
) -> None:
    """Add another name (for example a first name) the contact goes by."""
    org_id = _org_id(org)
    body = _call("POST", _base(org_id, contact_id) + "/aliases",
                 json_data={"alias": alias}, not_found=_NOT_FOUND)
    _print_contact(body)


@alias_app.command("rm")
@handle_errors
def alias_rm(
    contact_id: str = typer.Argument(...),
    alias: str = typer.Argument(...),
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
) -> None:
    """Remove one of the contact's other names."""
    org_id = _org_id(org)
    body = _call("DELETE", _base(org_id, contact_id) + f"/aliases?alias={quote(alias, safe='')}",
                 not_found=_NOT_FOUND)
    _print_contact(body)


@contact_app.command("merge")
@handle_errors
def contact_merge(
    target_id: str = typer.Argument(..., help="The contact to keep."),
    source_id: str = typer.Argument(..., help="The duplicate to fold into it."),
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
    yes: bool = typer.Option(False, "--yes", help="Merge now (otherwise a preview)."),
) -> None:
    """Merge a duplicate into a contact (owners and admins)."""
    org_id = _org_id(org)
    path = _base(org_id, target_id) + f"/merge-from/{_seg(source_id)}"
    if yes:
        path += "?confirm=true"
    body = _call("POST", path, not_found=_NOT_FOUND)
    verb = "Merged" if body.get("confirmed") else "Would merge"
    console.print(
        f"{verb}: {body['participants_moved']} participant(s) and "
        f"{body['tickets_moved']} ticket(s) move; new aliases: "
        f"{escape(', '.join(body.get('aliases_added') or []) or 'none')}."
    )
    if not body.get("confirmed"):
        console.print("[dim]Run again with --yes to merge.[/dim]")


@contact_app.command("delete")
@handle_errors
def contact_delete(
    contact_id: str = typer.Argument(...),
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Erase a contact (owners and admins). Participants go back to
    unresolved and tickets become unassigned."""
    if not yes and not typer.confirm(f"Erase contact {contact_id}? This can't be undone."):
        raise typer.Exit(1)
    org_id = _org_id(org)
    body = _call("DELETE", _base(org_id, contact_id), not_found=_NOT_FOUND)
    console.print(
        f"[green]Contact erased.[/green] {body['participants_reset']} participant(s) reset, "
        f"{body['tickets_unassigned']} ticket(s) unassigned."
    )


@contact_app.command("export")
@handle_errors
def contact_export(
    contact_id: str = typer.Argument(...),
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
    output: Path = typer.Option(None, "-o", "--output", help="Write to this file."),
) -> None:
    """Everything stored about one contact, for a data request (owners and admins)."""
    org_id = _org_id(org)
    body = _call("GET", _base(org_id, contact_id) + "/export", not_found=_NOT_FOUND)
    text = json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body)
    if output:
        _write_private_file(output, text + "\n")
        console.print(f"[green]Wrote {output}.[/green]")
    else:
        console.print_json(text)


@contact_app.command("export-all")
@handle_errors
def contact_export_all(
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
    output: Path = typer.Option(None, "-o", "--output", help="Write the CSV here."),
) -> None:
    """The whole directory as CSV, at most 10,000 rows (owners and admins)."""
    org_id = _org_id(org)
    body = _call("GET", _base(org_id), params={"format": "csv", "status": "all"},
                 not_found="Organization not found.")
    text = body if isinstance(body, str) else json.dumps(body)
    if output:
        _write_private_file(output, text)
        console.print(f"[green]Wrote {output}.[/green]")
    else:
        console.print(text, markup=False, highlight=False)


@contact_app.command("suggestions")
@handle_errors
def contact_suggestions(
    org: str = typer.Option(None, "--org", help=_ORG_HELP),
    apply: str = typer.Option(None, "--apply", help="A listed name to resolve."),
    contact: str = typer.Option(None, "--contact", help="The contact --apply resolves it to."),
    dismiss: str = typer.Option(None, "--dismiss", help="Hide a listed name from your queue."),
) -> None:
    """Names in your meetings and chats nobody has confirmed yet (last 90 days)."""
    org_id = _org_id(org)
    if apply:
        if not contact:
            raise typer.BadParameter("--apply needs --contact")
        body = _call("POST", _base(org_id) + "/suggestions/apply",
                     json_data={"name": apply, "contact_id": contact}, not_found=_NOT_FOUND)
        console.print(f"[green]Resolved {body['applied']} mention(s).[/green]"
                      + (f" {body['skipped']} in sources you can't edit stay unresolved." if body["skipped"] else ""))
        return
    if dismiss:
        _call("POST", _base(org_id) + "/suggestions/dismiss", json_data={"name": dismiss},
              not_found="Organization not found.")
        console.print("[green]Hidden from your queue.[/green]")
        return
    body = _call("GET", _base(org_id) + "/suggestions", not_found="Organization not found.")
    groups = body.get("groups") or []
    if not groups:
        console.print("[dim]Nothing to confirm.[/dim]")
        return
    table = Table("Name", "Mentions", "Candidates")
    for g in groups:
        cands = ", ".join(f"{c['display_name']} ({c['id']}, {c['hint']})" for c in g.get("candidates") or [])
        table.add_row(escape(g["name"]), str(g["mention_count"]), escape(cands))
    console.print(table)
