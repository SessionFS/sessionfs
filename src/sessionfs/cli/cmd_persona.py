"""`sfs persona …` — manage agent personas for the current project.

Commands:
- sfs persona list                 — list active personas in this project
- sfs persona show <name>          — print persona content + metadata
- sfs persona create <name>        — create from --content or --file or $EDITOR
- sfs persona edit <name>          — open content in $EDITOR and update
- sfs persona delete <name>        — soft-delete (sets is_active=false)

Personas are portable AI roles (atlas/prism/scribe/...). Tier-gated to
Pro+ (`agent_personas` feature flag).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

import typer
from rich.markdown import Markdown
from rich.table import Table

from sessionfs.cli.common import console, err_console, handle_errors
from sessionfs.cli.cmd_rules import (
    _api_request,
    _get_api_config,
    _get_git_remote,
    _normalize_remote,
    _resolve_project_id,
)

persona_app = typer.Typer(
    name="persona",
    help="Manage agent personas for this project (Pro+).",
    no_args_is_help=True,
)


def _resolve_project() -> tuple[str, str, str]:
    """Return (api_url, api_key, project_id) for the current repo."""
    remote = _get_git_remote()
    if not remote:
        err_console.print(
            "[red]No git remote found.[/red] "
            "Run inside a git repo with an `origin` remote."
        )
        raise typer.Exit(1)
    normalized = _normalize_remote(remote)
    if not normalized:
        err_console.print(f"[red]Could not normalize remote: {remote}[/red]")
        raise typer.Exit(1)
    api_url, api_key = _get_api_config()
    project_id = _resolve_project_id(api_url, api_key, normalized)
    return api_url, api_key, project_id


def _edit_in_editor(initial: str = "") -> str:
    """Open $EDITOR (or $VISUAL or vi) on a tmp file seeded with `initial`."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(initial)
        tmp_path = Path(tf.name)
    try:
        subprocess.run([editor, str(tmp_path)], check=True)
        return tmp_path.read_text(encoding="utf-8")
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


@persona_app.command("list")
@handle_errors
def list_personas() -> None:
    """List active personas in the current project."""
    api_url, api_key, project_id = _resolve_project()
    status, body, _ = asyncio.run(
        _api_request("GET", f"/api/v1/projects/{project_id}/personas", api_url, api_key)
    )
    if status >= 400:
        err_console.print(f"[red]API error ({status}): {body}[/red]")
        raise typer.Exit(1)
    if not isinstance(body, list) or not body:
        console.print("[dim]No personas in this project. Create one with[/dim] "
                      "[bold]sfs persona create <name>[/bold].")
        return
    table = Table(title="Personas")
    table.add_column("Name", style="cyan")
    table.add_column("Role")
    table.add_column("Specializations", style="dim")
    for p in body:
        specs = ", ".join(p.get("specializations") or [])
        table.add_row(p.get("name", ""), p.get("role", ""), specs)
    console.print(table)


@persona_app.command("show")
@handle_errors
def show_persona(
    name: str = typer.Argument(..., help="Persona name (e.g. 'atlas')"),
    raw: bool = typer.Option(False, "--raw", help="Print raw markdown without rendering"),
) -> None:
    """Show a persona's role, specializations, and full content."""
    api_url, api_key, project_id = _resolve_project()
    status, body, _ = asyncio.run(
        _api_request(
            "GET", f"/api/v1/projects/{project_id}/personas/{name}", api_url, api_key
        )
    )
    if status == 404:
        err_console.print(f"[red]Persona '{name}' not found.[/red]")
        raise typer.Exit(1)
    if status >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({status}): {body}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold cyan]{body['name']}[/bold cyan] — {body['role']}")
    specs = body.get("specializations") or []
    if specs:
        console.print(f"[dim]Specializations:[/dim] {', '.join(specs)}")
    console.print()
    content = body.get("content") or ""
    if raw:
        console.print(content)
    else:
        console.print(Markdown(content))


def _kebab_role_fragment(role: str) -> str:
    """Pick the short suffix used in `.agents/<name>-<fragment>.md` filenames.

    Heuristic: take the first meaningful word in the role. The existing
    `.agents/` files use short fragments (backend, devops, revenue,
    frontend, docs, security, compliance, licensing), not the literal
    role kebab-cased. Map them by the leading word so atlas's "Backend
    Architect" → atlas-backend.md, forge's "DevOps and GCP Platform
    Engineer" → forge-devops.md, scribe's "Documentation and Positioning
    Lead" → scribe-documentation.md. Callers can override with --path.
    """
    head = (role or "").strip().split()
    if not head:
        return ""
    return head[0].lower().rstrip(",")


def _resolve_pull_target(out_dir: Path, name: str, role: str) -> Path:
    """Pick the target filename for `sfs persona pull`.

    Prefers an existing `.agents/<name>-*.md` so the established naming
    convention is preserved on re-pull. Falls back to
    `<name>-<role-fragment>.md`, then `<name>.md`.
    """
    existing = sorted(out_dir.glob(f"{name}-*.md"))
    if existing:
        return existing[0]
    fragment = _kebab_role_fragment(role)
    if fragment:
        return out_dir / f"{name}-{fragment}.md"
    return out_dir / f"{name}.md"


def _format_persona_markdown(persona: dict) -> str:
    """Render a persona record as the markdown body to write to disk.

    The persona's own `content` is expected to start with an H1 header,
    so we don't add another. A short HTML-comment preamble records the
    server version + specializations + pull timestamp so a human
    browsing `.agents/` can tell the file is auto-synced.
    """
    specs = persona.get("specializations") or []
    body = (persona.get("content") or "").rstrip() + "\n"
    preamble = [
        f"<!-- Pulled from SessionFS persona store. "
        f"Server version: {persona.get('version', '?')}. "
        f"Run `sfs persona pull --all --force` to refresh. -->",
    ]
    if specs:
        preamble.append(f"<!-- Specializations: {', '.join(specs)} -->")
    preamble.append("")
    return "\n".join(preamble) + body


@persona_app.command("pull")
@handle_errors
def pull_personas(
    name: str | None = typer.Argument(
        None,
        help="Persona name to pull (e.g. 'atlas'). Omit with --all to pull every persona.",
    ),
    all_: bool = typer.Option(
        False, "--all", help="Pull every active persona in the project.",
    ),
    out_dir: Path = typer.Option(
        Path(".agents"),
        "--dir",
        help="Output directory. Defaults to .agents/ (matches CLAUDE.md / release-skill convention).",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing files.",
    ),
    path: Path | None = typer.Option(
        None, "--path", help="Explicit output path (single-persona only).",
    ),
) -> None:
    """Pull server-side personas to local `.agents/*.md` files.

    Use this after personas have been revised on the server (via
    `sfs persona edit`, dashboard, or MCP) so local Agent-tool spawns
    and the /release skill's sub-agents see the same content.

    Default target: `.agents/<name>-<role-fragment>.md`. Preserves
    existing filenames on re-pull so the established convention is
    stable.
    """
    if not name and not all_:
        err_console.print(
            "[red]Pass a persona name or --all.[/red] See `sfs persona pull --help`."
        )
        raise typer.Exit(2)
    if name and all_:
        err_console.print("[red]Pass either a name or --all, not both.[/red]")
        raise typer.Exit(2)
    if path and (all_ or not name):
        err_console.print("[red]--path is only valid when pulling a single persona by name.[/red]")
        raise typer.Exit(2)

    api_url, api_key, project_id = _resolve_project()
    out_dir.mkdir(parents=True, exist_ok=True)

    if all_:
        status, body, _ = asyncio.run(
            _api_request(
                "GET", f"/api/v1/projects/{project_id}/personas", api_url, api_key,
            )
        )
        if status >= 400 or not isinstance(body, list):
            err_console.print(f"[red]API error ({status}): {body}[/red]")
            raise typer.Exit(1)
        personas = body
    else:
        status, body, _ = asyncio.run(
            _api_request(
                "GET",
                f"/api/v1/projects/{project_id}/personas/{name}",
                api_url, api_key,
            )
        )
        if status == 404:
            err_console.print(f"[red]Persona '{name}' not found.[/red]")
            raise typer.Exit(1)
        if status >= 400 or not isinstance(body, dict):
            err_console.print(f"[red]API error ({status}): {body}[/red]")
            raise typer.Exit(1)
        personas = [body]

    written: list[Path] = []
    skipped: list[Path] = []
    for p in personas:
        if path is not None:
            target = path
        else:
            target = _resolve_pull_target(out_dir, p["name"], p.get("role", ""))
        if target.exists() and not force:
            skipped.append(target)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_format_persona_markdown(p), encoding="utf-8")
        written.append(target)

    for t in written:
        console.print(f"[green]wrote[/green] {t}")
    for t in skipped:
        err_console.print(
            f"[yellow]skipped[/yellow] {t} [dim](exists; use --force to overwrite)[/dim]"
        )
    if not written:
        if skipped:
            raise typer.Exit(1)
        err_console.print("[yellow]No personas pulled.[/yellow]")
        raise typer.Exit(1)


@persona_app.command("create")
@handle_errors
def create_persona(
    name: str = typer.Argument(..., help="Persona name (ASCII, 1-50 chars)"),
    role: str = typer.Option(..., "--role", "-r", help="Short role description"),
    content_file: Path | None = typer.Option(
        None, "--file", "-f", help="Path to markdown file with persona content"
    ),
    content: str | None = typer.Option(
        None, "--content", help="Inline persona content (markdown)"
    ),
    specialization: list[str] = typer.Option(
        [], "--spec", help="Specialization tag (repeatable)"
    ),
) -> None:
    """Create a new persona. Opens $EDITOR if --content/--file are absent."""
    if content_file and content:
        err_console.print("[red]Pass --file OR --content, not both.[/red]")
        raise typer.Exit(2)

    if content_file:
        body_text = content_file.read_text(encoding="utf-8")
    elif content is not None:
        body_text = content
    else:
        body_text = _edit_in_editor(f"# {name}\n\n{role}\n\n")
        if not body_text.strip():
            err_console.print("[yellow]Empty content — aborting.[/yellow]")
            raise typer.Exit(1)

    api_url, api_key, project_id = _resolve_project()
    payload = {
        "name": name,
        "role": role,
        "content": body_text,
        "specializations": list(specialization),
    }
    status, body, _ = asyncio.run(
        _api_request(
            "POST", f"/api/v1/projects/{project_id}/personas", api_url, api_key,
            json_data=payload,
        )
    )
    if status == 409:
        err_console.print(f"[red]Persona '{name}' already exists.[/red]")
        raise typer.Exit(1)
    if status >= 400:
        err_console.print(f"[red]API error ({status}): {body}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Created persona '{name}'.[/green]")


@persona_app.command("edit")
@handle_errors
def edit_persona(
    name: str = typer.Argument(..., help="Persona name"),
) -> None:
    """Open the persona's content in $EDITOR and update on save."""
    api_url, api_key, project_id = _resolve_project()
    status, body, _ = asyncio.run(
        _api_request(
            "GET", f"/api/v1/projects/{project_id}/personas/{name}", api_url, api_key
        )
    )
    if status == 404:
        err_console.print(f"[red]Persona '{name}' not found.[/red]")
        raise typer.Exit(1)
    if status >= 400 or not isinstance(body, dict):
        err_console.print(f"[red]API error ({status}): {body}[/red]")
        raise typer.Exit(1)

    original = body.get("content") or ""
    updated = _edit_in_editor(original)
    if updated == original:
        console.print("[dim]No changes — leaving persona untouched.[/dim]")
        return

    status, body, _ = asyncio.run(
        _api_request(
            "PUT", f"/api/v1/projects/{project_id}/personas/{name}", api_url, api_key,
            json_data={"content": updated},
        )
    )
    if status >= 400:
        err_console.print(f"[red]API error ({status}): {body}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Updated persona '{name}'.[/green]")


@persona_app.command("delete")
@handle_errors
def delete_persona(
    name: str = typer.Argument(..., help="Persona name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    force: bool = typer.Option(
        False, "--force",
        help="Delete even if non-terminal tickets reference this persona "
             "(they will be left stranded until reassigned).",
    ),
) -> None:
    """Soft-delete a persona (sets is_active=false; name stays reserved)."""
    if not yes:
        typer.confirm(
            f"Soft-delete persona '{name}'? Past tickets keep their reference.",
            abort=True,
        )
    api_url, api_key, project_id = _resolve_project()
    suffix = "?force=true" if force else ""
    status, body, _ = asyncio.run(
        _api_request(
            "DELETE",
            f"/api/v1/projects/{project_id}/personas/{name}{suffix}",
            api_url, api_key,
        )
    )
    if status == 404:
        err_console.print(f"[red]Persona '{name}' not found.[/red]")
        raise typer.Exit(1)
    if status == 409:
        # Server-side stranded-ticket guard (KB 339 MEDIUM).
        # The global error handler reshapes HTTPException(detail=...) into
        # `body["error"]["message"]`, so read both shapes (KB 341 LOW).
        if isinstance(body, dict):
            detail = (
                body.get("detail")
                or (body.get("error") or {}).get("message")
                or str(body)
            )
        else:
            detail = str(body)
        err_console.print(f"[red]{detail}[/red]")
        err_console.print(
            "[dim]Reassign affected tickets with[/dim] "
            "[bold]sfs ticket list --assigned-to " + name + "[/bold]"
            "[dim], or re-run with --force.[/dim]"
        )
        raise typer.Exit(1)
    if status >= 400:
        err_console.print(f"[red]API error ({status}): {body}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Deleted persona '{name}'.[/green]")


@persona_app.command("assume")
@handle_errors
def assume_persona(
    name: str = typer.Argument(..., help="Persona to assume"),
) -> None:
    """Declare you are working as a persona (no ticket required).

    Writes ~/.sessionfs/active_ticket.json so the daemon tags every
    captured session with this persona name. Pair with `sfs persona
    forget` to clear when done.
    """
    api_url, api_key, project_id = _resolve_project()
    # Verify the persona exists in this project.
    status, body, _ = asyncio.run(
        _api_request(
            "GET", f"/api/v1/projects/{project_id}/personas/{name}",
            api_url, api_key,
        )
    )
    if status == 404:
        err_console.print(f"[red]Persona '{name}' not found.[/red]")
        raise typer.Exit(1)
    if status >= 400:
        err_console.print(f"[red]API error ({status}): {body}[/red]")
        raise typer.Exit(1)

    from sessionfs.active_ticket import bundle_path, write_bundle
    ok = write_bundle(
        ticket_id=None, persona_name=name, project_id=project_id,
    )
    if ok:
        console.print(
            f"[green]Now acting as '{name}'.[/green] "
            f"Subsequent sessions tagged via {bundle_path()}."
        )
    else:
        err_console.print(
            f"[yellow]Could not write {bundle_path()} — sessions will NOT "
            f"be tagged with persona '{name}'.[/yellow]"
        )
        raise typer.Exit(1)


@persona_app.command("forget")
@handle_errors
def forget_persona() -> None:
    """Clear the local persona bundle written by `sfs persona assume`.

    Refuses when the bundle is ticket-tagged — use `sfs ticket complete`
    or the MCP `complete_ticket` tool to retire a ticket attribution
    (KB 352 MEDIUM).
    """
    from sessionfs.active_ticket import bundle_path, clear_bundle, read_bundle
    bundle = read_bundle()
    if isinstance(bundle, dict) and bundle.get("ticket_id"):
        err_console.print(
            f"[red]Bundle is tagged to ticket {bundle.get('ticket_id')!r}.[/red] "
            "Use [bold]sfs ticket complete[/bold] to retire a ticket "
            "attribution; [bold]sfs persona forget[/bold] only clears "
            "persona-only bundles written by `sfs persona assume`."
        )
        raise typer.Exit(1)
    cleared = clear_bundle()
    if cleared:
        console.print(f"[green]Cleared {bundle_path()}.[/green]")
    else:
        console.print("[dim]No active persona to clear.[/dim]")
