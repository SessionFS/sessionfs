"""CLI commands for shared project context."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile

import typer

from sessionfs.cli.common import console, err_console

project_app = typer.Typer(name="project", help="Manage shared project context.", no_args_is_help=True)


def _get_git_remote() -> str | None:
    """Detect the git remote URL from the current directory."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _normalize_remote(url: str) -> str:
    """Normalize git remote URL to owner/repo format."""
    from sessionfs.server.github_app import normalize_git_remote
    return normalize_git_remote(url)


def _get_project_client():
    """Create an authenticated HTTP client for project API."""
    from sessionfs.cli.cmd_cloud import _load_sync_config

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise typer.Exit(1)
    return cfg["api_url"], cfg["api_key"]


async def _api_request(method: str, path: str, api_url: str, api_key: str, json_data: dict | None = None) -> dict:
    """Make an authenticated API request."""
    import httpx

    url = f"{api_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=30) as client:
        if method == "GET":
            resp = await client.get(url, headers=headers)
        elif method == "POST":
            resp = await client.post(url, headers=headers, json=json_data)
        elif method == "PUT":
            resp = await client.put(url, headers=headers, json=json_data)
        elif method == "DELETE":
            resp = await client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported method: {method}")

    if resp.status_code == 404:
        return {"_status": 404}
    if resp.status_code == 409:
        return {"_status": 409}
    if resp.status_code >= 400:
        detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        err_console.print(f"[red]API error ({resp.status_code}): {detail}[/red]")
        raise typer.Exit(1)
    return resp.json()


@project_app.command("init")
def project_init() -> None:
    """Initialize a project context for the current repo."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository. Run from inside a git repo.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    if not normalized:
        err_console.print("[red]Could not parse git remote URL.[/red]")
        raise typer.Exit(1)

    api_url, api_key = _get_project_client()

    # Check if project already exists
    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") != 404:
        console.print(f"Project already exists for [bold]{normalized}[/bold]")
        console.print("Run 'sfs project edit' to update context.")
        return

    # Create project
    name = normalized.split("/")[-1]
    result = asyncio.run(_api_request(
        "POST", "/api/v1/projects/",
        api_url, api_key,
        json_data={"name": name, "git_remote_normalized": normalized},
    ))

    console.print(f"Project created for [bold]{normalized}[/bold]")
    console.print("Run 'sfs project edit' to add context.")


@project_app.command("show")
def project_show() -> None:
    """Show the current project context."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    console.print(f"[bold]Project:[/bold] {result['name']}")
    console.print(f"[bold]Remote:[/bold]  {result['git_remote_normalized']}")
    console.print(f"[bold]Updated:[/bold] {result['updated_at'][:10]}")
    console.print()
    console.print(result.get("context_document", ""))


@project_app.command("edit")
def project_edit() -> None:
    """Edit the project context document in $EDITOR."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    current_doc = result.get("context_document", "")

    with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
        f.write(current_doc)
        temp_path = f.name

    editor = os.environ.get("EDITOR", "nano")
    try:
        subprocess.run([editor, temp_path])
    except FileNotFoundError:
        err_console.print(f"[red]Editor not found: {editor}. Set $EDITOR.[/red]")
        os.unlink(temp_path)
        raise typer.Exit(1)

    with open(temp_path) as f:
        new_content = f.read()

    os.unlink(temp_path)

    if new_content == current_doc:
        console.print("No changes.")
        return

    asyncio.run(_api_request(
        "PUT", f"/api/v1/projects/{normalized}/context",
        api_url, api_key,
        json_data={"context_document": new_content},
    ))
    console.print(f"Project context updated ({len(new_content)} bytes).")


@project_app.command("set-context")
def project_set_context(
    file_path: str = typer.Argument(..., help="Path to markdown file"),
) -> None:
    """Set project context from a file."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    # Verify project exists
    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    try:
        with open(file_path) as f:
            content = f.read()
    except FileNotFoundError:
        err_console.print(f"[red]File not found: {file_path}[/red]")
        raise typer.Exit(1)

    asyncio.run(_api_request(
        "PUT", f"/api/v1/projects/{normalized}/context",
        api_url, api_key,
        json_data={"context_document": content},
    ))
    size_kb = len(content) / 1024
    console.print(f"Project context updated ({size_kb:.1f} KB).")


@project_app.command("get-context")
def project_get_context() -> None:
    """Output raw project context markdown to stdout."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        raise typer.Exit(1)

    # Raw output to stdout (no Rich formatting)
    import sys
    sys.stdout.write(result.get("context_document", ""))


@project_app.command("compile")
def project_compile() -> None:
    """Compile pending knowledge entries into project context."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    # Verify project exists
    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    project_id = result["id"]
    console.print("[dim]Compiling pending entries...[/dim]")

    compile_result = asyncio.run(_api_request(
        "POST", f"/api/v1/projects/{project_id}/compile",
        api_url, api_key,
    ))

    entries_compiled = compile_result.get("entries_compiled", 0)
    if entries_compiled == 0:
        console.print("No pending entries to compile.")
    else:
        console.print(f"Project context updated. [bold]{entries_compiled}[/bold] entries compiled.")


@project_app.command("entries")
def project_entries(
    pending: bool = typer.Option(False, help="Show only pending entries"),
    entry_type: str = typer.Option(None, "--type", help="Filter by type (decision, pattern, discovery, convention, bug, dependency)"),
    limit: int = typer.Option(20, help="Max entries to show"),
) -> None:
    """List knowledge entries for this project."""
    from rich.table import Table

    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    # Verify project exists
    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    project_id = result["id"]

    # Build query params
    params = f"?limit={limit}"
    if pending:
        params += "&pending=true"
    if entry_type:
        params += f"&type={entry_type}"

    entries_result = asyncio.run(_api_request(
        "GET", f"/api/v1/projects/{project_id}/entries{params}",
        api_url, api_key,
    ))

    entries = entries_result if isinstance(entries_result, list) else entries_result.get("entries", [])
    if not entries:
        console.print("[dim]No knowledge entries found.[/dim]")
        return

    type_colors = {
        "decision": "green",
        "pattern": "blue",
        "discovery": "magenta",
        "convention": "cyan",
        "bug": "red",
        "dependency": "yellow",
    }

    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("ID", style="dim", width=6)
    table.add_column("Type", width=12)
    table.add_column("Content", ratio=3)
    table.add_column("Age", width=10)
    table.add_column("Session", style="dim", width=16)
    table.add_column("Status", width=10)

    for entry in entries:
        etype = entry.get("entry_type", "unknown")
        color = type_colors.get(etype, "white")
        type_badge = f"[{color}]{etype}[/{color}]"

        content = entry.get("content", "")
        if len(content) > 80:
            content = content[:77] + "..."

        # Calculate age from created_at
        created = entry.get("created_at", "")
        age = _format_age(created) if created else "-"

        session_id = entry.get("session_id", "-")
        if session_id and len(session_id) > 14:
            session_id = session_id[:14] + ".."

        if entry.get("dismissed"):
            status = "[dim strikethrough]dismissed[/dim strikethrough]"
        elif entry.get("compiled_at"):
            status = "[green]compiled[/green]"
        else:
            status = "[yellow]pending[/yellow]"

        table.add_row(str(entry.get("id", "")), type_badge, content, age, session_id, status)

    console.print(table)
    total = len(entries) if isinstance(entries_result, list) else entries_result.get("total", len(entries))
    console.print(f"[dim]Showing {len(entries)} of {total} entries[/dim]")


def _format_age(iso_str: str) -> str:
    """Format an ISO timestamp as a human-readable age."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        days = seconds // 86400
        if days < 7:
            return f"{days}d ago"
        return f"{days // 7}w ago"
    except (ValueError, TypeError):
        return "-"


@project_app.command("health")
def project_health() -> None:
    """Run health checks on project context."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    # Verify project exists
    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    project_id = result["id"]

    health_result = asyncio.run(_api_request(
        "GET", f"/api/v1/projects/{project_id}/health",
        api_url, api_key,
    ))

    console.print(f"[bold]Project Health: {result['name']}[/bold]")
    console.print()

    total = health_result.get("total_entries", 0)
    pending = health_result.get("pending_entries", 0)
    compiled = health_result.get("compiled_entries", 0)
    word_count = health_result.get("word_count", 0)
    section_count = health_result.get("section_count", 0)
    stale = health_result.get("potentially_stale", False)
    last_compiled = health_result.get("last_compiled")

    # Build checks from the API data
    ok = "[green]\u2713[/green]"
    warn = "[yellow]\u26a0[/yellow]"

    console.print(f"  {ok}  Context document exists ({word_count} words, {section_count} sections)")
    console.print(f"  {ok}  {total} knowledge entries ({compiled} compiled, {health_result.get('dismissed_entries', 0)} dismissed)")

    if pending > 0:
        console.print(f"  {warn}  {pending} entries pending compilation")
    else:
        console.print(f"  {ok}  All entries compiled")

    if last_compiled:
        console.print(f"  {ok}  Last compiled: {str(last_compiled)[:10]}")
    elif total > 0:
        console.print(f"  {warn}  Never compiled — run 'sfs project compile'")

    if stale:
        console.print(f"  {warn}  Context may be stale — pending entries contain new information")
    else:
        console.print(f"  {ok}  Context appears up to date")

    # Suggestions
    suggestions = []
    if pending > 5:
        suggestions.append(f"Run 'sfs project compile' to merge {pending} pending entries")
    if word_count == 0:
        suggestions.append("Add project context: sfs project edit")
    if stale:
        suggestions.append("Review pending entries for new information")

    if suggestions:
        console.print()
        console.print("[bold]Suggestions:[/bold]")
        for s in suggestions:
            console.print(f"  [yellow]\u2022[/yellow] {s}")

    # Score
    score = 100
    if pending > 10:
        score -= 20
    elif pending > 0:
        score -= 10
    if stale:
        score -= 15
    if word_count == 0:
        score -= 30

    console.print()
    color = "green" if score >= 80 else "yellow" if score >= 50 else "red"
    console.print(f"[bold]Health Score:[/bold] [{color}]{score}%[/{color}]")


@project_app.command("ask")
def ask_project(
    question: str = typer.Argument(help="Question about the project"),
) -> None:
    """Ask a question about the project using the knowledge base."""
    from rich.markdown import Markdown
    from rich.panel import Panel

    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    # 1. Get project context
    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    project_id = result["id"]
    context_doc = result.get("context_document", "")

    # 2. Search knowledge entries for the question
    search_params = f"?search={question}&limit=10"
    entries_result = asyncio.run(_api_request(
        "GET", f"/api/v1/projects/{project_id}/entries{search_params}",
        api_url, api_key,
    ))

    entries = entries_result if isinstance(entries_result, list) else entries_result.get("entries", [])

    # 3. Format context
    console.print(Panel(f"[bold]Question:[/bold] {question}", border_style="blue"))
    console.print()

    if context_doc.strip():
        console.print("[bold]Project Context (excerpt):[/bold]")
        # Show first 500 chars of context
        excerpt = context_doc[:500]
        if len(context_doc) > 500:
            excerpt += "..."
        console.print(Markdown(excerpt))
        console.print()

    # 4. Show matching entries
    if entries:
        type_colors = {
            "decision": "green", "pattern": "blue", "discovery": "magenta",
            "convention": "cyan", "bug": "red", "dependency": "yellow",
        }
        console.print(f"[bold]Matching Knowledge Entries ({len(entries)}):[/bold]")
        for entry in entries:
            etype = entry.get("entry_type", "unknown")
            color = type_colors.get(etype, "white")
            confidence = entry.get("confidence", 0)
            session_id = entry.get("session_id", "")
            created = entry.get("created_at", "")[:10]
            unverified = " (unverified)" if confidence < 0.5 else ""
            console.print(
                f"  [{color}][{etype}][/{color}]{unverified} {entry.get('content', '')}"
            )
            console.print(f"    [dim]Session: {session_id} | {created} | confidence: {confidence:.0%}[/dim]")
        console.print()

        # 5. Show relevant session links
        session_ids = list({e.get("session_id", "") for e in entries if e.get("session_id")})
        if session_ids:
            console.print("[bold]Related Sessions:[/bold]")
            for sid in session_ids[:5]:
                console.print(f"  sfs show {sid}")
            console.print()
    else:
        console.print("[dim]No matching knowledge entries found.[/dim]")
        console.print()

    # 6. Optionally save the Q&A as a discovery entry
    # Build answer from matching entries for filing back
    answer_parts = []
    if entries:
        for e in entries[:5]:
            answer_parts.append(f"[{e.get('entry_type', '?')}] {e.get('content', '')}")

    if typer.confirm("Save this Q&A to the knowledge base?", default=False):
        qa_content = f"Q: {question}"
        if answer_parts:
            qa_content += f"\nA: {'; '.join(answer_parts)}"
        save_result = asyncio.run(_api_request(
            "POST", f"/api/v1/projects/{project_id}/entries/add",
            api_url, api_key,
            json_data={
                "entry_type": "discovery",
                "content": qa_content[:1000],
                "session_id": "cli-ask",
                "confidence": 0.8,
                "source_context": f"Answered from {len(answer_parts)} knowledge entries via sfs project ask",
            },
        ))
        if save_result.get("id"):
            console.print("[green]Q&A saved to knowledge base.[/green]")
        else:
            err_console.print("[red]Failed to save Q&A entry.[/red]")


@project_app.command("dismiss")
def project_dismiss(
    entry_id: int = typer.Argument(help="Entry ID to dismiss"),
) -> None:
    """Dismiss an irrelevant knowledge entry."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    # Verify project exists
    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    project_id = result["id"]

    asyncio.run(_api_request(
        "PUT", f"/api/v1/projects/{project_id}/entries/{entry_id}",
        api_url, api_key,
        json_data={"dismissed": True},
    ))

    console.print(f"Entry {entry_id} dismissed.")


@project_app.command("pages")
def project_pages() -> None:
    """List wiki pages for this project."""
    from rich.table import Table

    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    project_id = result["id"]

    pages_result = asyncio.run(_api_request(
        "GET", f"/api/v1/projects/{project_id}/pages",
        api_url, api_key,
    ))

    pages = pages_result if isinstance(pages_result, list) else pages_result.get("pages", [])
    if not pages:
        console.print("[dim]No wiki pages found.[/dim]")
        return

    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("Slug", style="bold", ratio=1)
    table.add_column("Title", ratio=2)
    table.add_column("Type", width=10)
    table.add_column("Words", width=8, justify="right")
    table.add_column("Entries", width=8, justify="right")
    table.add_column("Auto", width=6, justify="center")

    for page in pages:
        auto = "[yellow]yes[/yellow]" if page.get("auto_generated") else "[dim]no[/dim]"
        table.add_row(
            page.get("slug", ""),
            page.get("title", ""),
            page.get("page_type", ""),
            str(page.get("word_count", 0)),
            str(page.get("entry_count", 0)),
            auto,
        )

    console.print(table)
    console.print(f"[dim]{len(pages)} pages[/dim]")


@project_app.command("page")
def project_page(
    slug: str = typer.Argument(help="Page slug"),
) -> None:
    """View a wiki page."""
    from rich.markdown import Markdown
    from rich.panel import Panel

    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    project_id = result["id"]

    from urllib.parse import quote
    page_result = asyncio.run(_api_request(
        "GET", f"/api/v1/projects/{project_id}/pages/{quote(slug, safe='')}",
        api_url, api_key,
    ))

    if page_result.get("_status") == 404:
        err_console.print(f"[red]Page not found: {slug}[/red]")
        raise typer.Exit(1)

    title = page_result.get("title") or page_result.get("slug", slug)
    content = page_result.get("content", "")
    auto = page_result.get("auto_generated", False)

    subtitle = f"[dim]{page_result.get('slug', '')}[/dim]"
    if auto:
        subtitle += "  [yellow](auto-generated)[/yellow]"

    console.print(Panel(Markdown(content), title=title, subtitle=subtitle, border_style="blue"))

    backlinks = page_result.get("backlinks", [])
    if backlinks:
        console.print()
        console.print("[bold]Backlinks:[/bold]")
        for bl in backlinks:
            label = f"{bl.get('source_type', 'unknown')}:{bl.get('source_id', '?')}"
            link_type = bl.get("link_type", "")
            if link_type:
                label += f" [dim]({link_type})[/dim]"
            console.print(f"  {label}")


@project_app.command("regenerate")
def project_regenerate(
    slug: str = typer.Argument(help="Page slug to regenerate"),
) -> None:
    """Regenerate an auto-generated concept article from latest entries."""
    from urllib.parse import quote

    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    project_id = result["id"]

    console.print(f"[dim]Regenerating page '{slug}'...[/dim]")

    regen_result = asyncio.run(_api_request(
        "POST", f"/api/v1/projects/{project_id}/pages/{quote(slug, safe='')}/regenerate",
        api_url, api_key,
    ))

    if regen_result.get("_status") == 404:
        err_console.print(f"[red]Page not found: {slug}[/red]")
        raise typer.Exit(1)

    word_count = regen_result.get("word_count", 0)
    entries_used = regen_result.get("entries_used", 0)
    console.print(
        f"Article regenerated ({word_count} words from {entries_used} entries)."
    )


@project_app.command("set")
def project_set(
    auto_narrative: bool | None = typer.Option(
        None, "--auto-narrative/--no-auto-narrative",
        help="Enable or disable auto-narrative on sync",
    ),
) -> None:
    """Update project settings."""
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[yellow]No project context found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    project_id = result["id"]

    settings: dict = {}
    if auto_narrative is not None:
        settings["auto_narrative"] = auto_narrative

    if not settings:
        err_console.print("[yellow]No settings specified. Use --auto-narrative or --no-auto-narrative.[/yellow]")
        raise typer.Exit(1)

    asyncio.run(_api_request(
        "PUT", f"/api/v1/projects/{project_id}/settings",
        api_url, api_key,
        json_data=settings,
    ))

    for key, value in settings.items():
        console.print(f"[bold]{key}[/bold] = {value}")
