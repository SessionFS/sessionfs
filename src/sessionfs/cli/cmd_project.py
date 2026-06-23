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


_ASK_STOP_WORDS = frozenset({
    "what", "whats", "is", "the", "a", "an", "how", "does", "do",
    "explain", "tell", "me", "about", "can", "you", "are", "was",
    "were", "this", "that", "it", "of", "in", "to", "for", "with",
    "on", "at", "by",
})
# Mirrors the 3-char floor enforced server-side in routes/knowledge.py:297
# (pg_trgm GIN index falls back to seq scan for 1-2 char patterns).
_ASK_MIN_KEYWORD_LEN = 3
_ASK_MAX_KEYWORDS = 5


def _extract_search_keywords(question: str) -> list[str]:
    """Tokenize an ask-question into search-eligible keywords.

    Strips trailing punctuation, lowercases, drops stop words and any
    token shorter than the server-side search floor. Returns at most
    `_ASK_MAX_KEYWORDS` to bound the per-question API fan-out.
    """
    out: list[str] = []
    seen: set[str] = set()
    for token in question.lower().split():
        stripped = token.strip("?.,!")
        if not stripped or stripped in _ASK_STOP_WORDS:
            continue
        if len(stripped) < _ASK_MIN_KEYWORD_LEN:
            continue
        if stripped in seen:
            continue
        seen.add(stripped)
        out.append(stripped)
        if len(out) >= _ASK_MAX_KEYWORDS:
            break
    return out


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
        elif method == "PATCH":
            resp = await client.patch(url, headers=headers, json=json_data)
        elif method == "DELETE":
            resp = await client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported method: {method}")

    if resp.status_code == 404:
        return {"_status": 404}
    if resp.status_code == 409:
        return {"_status": 409}
    if resp.status_code >= 400:
        from sessionfs.cli.common import format_api_error

        if resp.headers.get("content-type", "").startswith("application/json"):
            body = resp.json()
        else:
            body = resp.text
        err_console.print(
            f"[red]API error ({resp.status_code}): "
            f"{format_api_error(body, resp.status_code)}[/red]"
        )
        raise typer.Exit(1)
    return resp.json()


@project_app.command("init")
def project_init(
    org: str | None = typer.Option(
        None,
        "--org",
        help=(
            "Org id to scope this project to. If omitted, the server uses "
            "your default_org_id; if you have none, the project is personal."
        ),
    ),
    personal: bool = typer.Option(
        False,
        "--personal",
        help=(
            "Force personal scope, overriding any default_org_id. Mutually "
            "exclusive with --org."
        ),
    ),
) -> None:
    """Initialize a project context for the current repo.

    v0.10.0 Phase 5: scope resolution at init time —
    1. `--org <org_id>` wins if provided (caller must be a member).
    2. `--personal` forces NULL org_id even if default_org_id is set.
    3. Otherwise the server's default_org_id is used (read from /me).
    4. Falls back to NULL (personal) if neither default nor flag is set.

    The chosen scope is recorded server-side on the Project row and
    propagates to every future session captured against this repo via
    the daemon's git-remote → project lookup (KB entry 230 #2).
    """
    if org is not None and personal:
        err_console.print(
            "[red]--org and --personal are mutually exclusive.[/red]"
        )
        raise typer.Exit(2)

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

    # Resolve org scope.
    chosen_org_id: str | None
    if personal:
        chosen_org_id = None
    elif org is not None:
        chosen_org_id = org
    else:
        # Server-side default: read /me.default_org_id.
        me = asyncio.run(_api_request("GET", "/api/v1/auth/me", api_url, api_key))
        chosen_org_id = me.get("default_org_id")

    # Create project
    name = normalized.split("/")[-1]
    body: dict = {"name": name, "git_remote_normalized": normalized}
    if chosen_org_id is not None:
        body["org_id"] = chosen_org_id

    result = asyncio.run(_api_request(
        "POST", "/api/v1/projects/",
        api_url, api_key,
        json_data=body,
    ))
    if result.get("_status") == 403:
        err_console.print(
            "[red]You are not a member of that org. Use --personal to scope this "
            "project to yourself, or pick an org you belong to.[/red]"
        )
        raise typer.Exit(1)

    scope_label = "personal" if chosen_org_id is None else f"org {chosen_org_id}"
    console.print(f"Project created for [bold]{normalized}[/bold] ({scope_label})")
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


@project_app.command("promote-eligible")
def project_promote_eligible(
    min_length: int = typer.Option(
        50, "--min-length",
        help="Skip entries shorter than this. Matches the single-entry gate by default.",
    ),
    min_confidence: float = typer.Option(
        0.8, "--min-confidence",
        help=(
            "Only honored when --confidence is omitted. Skip entries "
            "below this confidence. Default 0.8 (parity with the "
            "single-entry promote gate)."
        ),
    ),
    set_confidence: float | None = typer.Option(
        None, "--confidence",
        help=(
            "Override each candidate's confidence to this value before "
            "the near-duplicate check. Use when bulk-asserting that "
            "stuck notes should clear the 0.8 promotion gate."
        ),
    ),
    entry_type: str | None = typer.Option(
        None, "--entry-type",
        help="Optional filter — only promote this entry_type (decision, pattern, etc.).",
    ),
    confirm: bool = typer.Option(
        False, "--confirm",
        help=(
            "Actually mutate. The default is dry-run — without this flag "
            "the command shows what WOULD be promoted but writes nothing."
        ),
    ),
) -> None:
    """Bulk-promote eligible note entries to claim class.

    Default is dry-run. Use --confirm to actually mutate the KB.

    The v0.10.10 confidence-clamp bug left many production KBs with
    hundreds of stuck note entries. This command is the practical
    repair path — per-entry confidence + promote round trips don't
    scale past ~5 entries.
    """
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    api_url, api_key = _get_project_client()

    project = asyncio.run(
        _api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key)
    )
    if project.get("_status") == 404:
        err_console.print("[yellow]No project found. Run 'sfs project init' first.[/yellow]")
        raise typer.Exit(1)

    project_id = project["id"]
    body: dict = {
        "min_length": min_length,
        "min_confidence": min_confidence,
        "dry_run": not confirm,
    }
    if set_confidence is not None:
        body["set_confidence"] = set_confidence
    if entry_type is not None:
        body["entry_type"] = entry_type

    result = asyncio.run(_api_request(
        "POST", f"/api/v1/projects/{project_id}/entries/bulk-promote",
        api_url, api_key, json_data=body,
    ))
    if result.get("_status") in (404, 409) or "_status" in result:
        raise typer.Exit(1)

    promoted = int(result.get("promoted", 0))
    skipped = int(result.get("skipped", 0))
    reasons = result.get("reasons") or {}
    dry = bool(result.get("dry_run", True))

    header = "[bold yellow]DRY RUN[/bold yellow]" if dry else "[bold green]COMMITTED[/bold green]"
    verb = "would promote" if dry else "promoted"
    console.print(
        f"{header}  {verb} [bold cyan]{promoted}[/bold cyan] entries  "
        f"(skipped {skipped})"
    )

    if reasons:
        from rich.table import Table
        ordered = ("too_short", "low_confidence", "duplicate", "dismissed", "superseded", "wrong_type", "already_claim")
        table = Table(title="Skipped — reason breakdown")
        table.add_column("Reason")
        table.add_column("Count", justify="right")
        any_row = False
        for r in ordered:
            n = int(reasons.get(r, 0))
            if n:
                table.add_row(r, str(n))
                any_row = True
        if any_row:
            console.print(table)

    if dry and promoted > 0:
        console.print(
            "\n[dim]Re-run with [bold]--confirm[/bold] to actually promote, "
            "then [bold]sfs project compile[/bold] to fold the new claims "
            "into the project context.[/dim]"
        )


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
    from urllib.parse import quote
    keywords = _extract_search_keywords(question)

    entries: list[dict] = []
    # Search with each keyword to get broader matches
    seen_ids: set[int] = set()
    for kw in keywords:
        search_params = f"?search={quote(kw)}&limit=10"
        kw_result = asyncio.run(_api_request(
            "GET", f"/api/v1/projects/{project_id}/entries{search_params}",
            api_url, api_key,
        ))
        kw_entries = kw_result if isinstance(kw_result, list) else kw_result.get("entries", [])
        for e in kw_entries:
            eid = e.get("id")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                entries.append(e)

    # If keyword search found nothing, try the full question as a phrase
    if not entries and question.strip():
        search_params = f"?search={quote(question)}&limit=10"
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
    name: str | None = typer.Option(
        None, "--name",
        help="Rename the project (display name). 1-255 chars.",
    ),
) -> None:
    """Update project settings.

    --name renames the project (PATCH /projects/{id}); --auto-narrative
    toggles auto-narrative. Either or both may be supplied.
    """
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

    if name is None and auto_narrative is None:
        err_console.print(
            "[yellow]No settings specified. Use --name and/or "
            "--auto-narrative/--no-auto-narrative.[/yellow]"
        )
        raise typer.Exit(1)

    # Rename via PATCH /projects/{id} (distinct from /settings).
    if name is not None:
        updated = asyncio.run(_api_request(
            "PATCH", f"/api/v1/projects/{project_id}",
            api_url, api_key,
            json_data={"name": name},
        ))
        console.print(f"[bold]name[/bold] = {updated.get('name', name)}")

    # Settings (auto_narrative) via PUT /projects/{id}/settings.
    settings: dict = {}
    if auto_narrative is not None:
        settings["auto_narrative"] = auto_narrative
    if settings:
        asyncio.run(_api_request(
            "PUT", f"/api/v1/projects/{project_id}/settings",
            api_url, api_key,
            json_data=settings,
        ))
        for key, value in settings.items():
            console.print(f"[bold]{key}[/bold] = {value}")


@project_app.command("rebuild")
def project_rebuild() -> None:
    """Rebuild project context from scratch using only active claims.

    Refreshes freshness classes, recompiles the context document,
    and regenerates section and concept pages.
    """
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

    console.print("[dim]Rebuilding project context...[/dim]")

    rebuild_result = asyncio.run(_api_request(
        "POST", f"/api/v1/projects/{project_id}/rebuild",
        api_url, api_key,
    ))

    freshness = rebuild_result.get("freshness_updated", 0)
    compiled = rebuild_result.get("entries_compiled", 0)
    words = rebuild_result.get("context_words", 0)
    sections = rebuild_result.get("section_pages_updated", 0)
    concepts = rebuild_result.get("concept_pages_updated", 0)

    console.print("Rebuild complete:")
    console.print(f"  Freshness updated: {freshness} entries")
    console.print(f"  Entries compiled: {compiled}")
    console.print(f"  Context document: {words} words")
    console.print(f"  Section pages: {sections}")
    console.print(f"  Concept pages: {concepts}")


# ─────────────────────────────────────────────────────────────────────────
# v0.10.0 Phase 4 — Project transfer commands.
#
# These hit the Phase 2 transfer routes (KB entry 246):
#   POST /api/v1/projects/{project_id}/transfer
#   POST /api/v1/transfers/{xfer_id}/{accept,reject,cancel}
#   GET  /api/v1/transfers?direction=&state=
#
# Identity for the source project is the git remote of the cwd (same
# pattern as init/show/edit). Destination is either "personal" or an
# org_id provided via `--to`. The destination identifier is the org_id;
# the brief mentions org slugs, but the server addresses orgs by id and
# the CLI defers any slug↔id mapping to a future config-store work
# item (KB 230 deferred per CEO directive).
# ─────────────────────────────────────────────────────────────────────────


def _resolve_project_id() -> tuple[str, str, str]:
    """Resolve the cwd project to (project_id, api_url, api_key).

    Exits with a friendly error if the cwd is not a git repo or no
    project context exists. Mirrors the init/show/edit pattern.
    """
    git_remote = _get_git_remote()
    if not git_remote:
        err_console.print("[red]Not a git repository. Run from inside a git repo.[/red]")
        raise typer.Exit(1)

    normalized = _normalize_remote(git_remote)
    if not normalized:
        err_console.print("[red]Could not parse git remote URL.[/red]")
        raise typer.Exit(1)

    api_url, api_key = _get_project_client()
    result = asyncio.run(_api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key))
    if result.get("_status") == 404:
        err_console.print(
            "[yellow]No project context found. "
            "Run 'sfs project init' first, or if this repo is part of "
            "a multi-repo project, link it with 'sfs project link-repo'.[/yellow]"
        )
        raise typer.Exit(1)
    return result["id"], api_url, api_key


def _print_transfer(t: dict) -> None:
    console.print(
        f"  [bold]{t['id']}[/bold]  "
        f"{t.get('project_name_snapshot') or '(project)'}  "
        f"{t['from_scope']} → {t['to_scope']}  "
        f"[dim]{t['state']}[/dim]"
    )


@project_app.command("transfer")
def project_transfer(
    to: str | None = typer.Option(
        None,
        "--to",
        help="Destination: 'personal' or an org_id. Required for initiate.",
    ),
    accept: str | None = typer.Option(
        None, "--accept", help="Transfer id to accept."
    ),
    reject: str | None = typer.Option(
        None, "--reject", help="Transfer id to reject."
    ),
    cancel: str | None = typer.Option(
        None, "--cancel", help="Transfer id to cancel (initiator only)."
    ),
) -> None:
    """Initiate or act on a project transfer.

    Examples:
        sfs project transfer --to org_acme
        sfs project transfer --to personal
        sfs project transfer --accept xfer_abc
        sfs project transfer --reject xfer_abc
        sfs project transfer --cancel xfer_abc
    """
    # Exactly one of --to / --accept / --reject / --cancel must be set.
    actions = [a for a in (to, accept, reject, cancel) if a is not None]
    if len(actions) != 1:
        err_console.print(
            "[red]Pass exactly one of --to / --accept / --reject / --cancel.[/red]"
        )
        raise typer.Exit(2)

    if to is not None:
        # Initiate from the cwd project.
        project_id, api_url, api_key = _resolve_project_id()
        result = asyncio.run(_api_request(
            "POST",
            f"/api/v1/projects/{project_id}/transfer",
            api_url,
            api_key,
            json_data={"to": to},
        ))
        if result.get("_status") == 404:
            err_console.print("[red]Project not found on the server.[/red]")
            raise typer.Exit(1)
        if result.get("_status") == 409:
            err_console.print(
                "[red]A pending transfer already exists for this project. "
                "Cancel it first.[/red]"
            )
            raise typer.Exit(1)
        if result.get("state") == "accepted":
            console.print(f"Transfer auto-accepted: {result['id']} → {result['to_scope']}")
        else:
            console.print(
                f"Transfer initiated: {result['id']} → {result['to_scope']} "
                f"(waiting on {result.get('target_user_id', '?')})"
            )
        return

    # State-change actions don't need cwd resolution — they reference
    # the transfer by id.
    api_url, api_key = _get_project_client()
    if accept is not None:
        path = f"/api/v1/transfers/{accept}/accept"
        verb = "accepted"
    elif reject is not None:
        path = f"/api/v1/transfers/{reject}/reject"
        verb = "rejected"
    else:
        assert cancel is not None
        path = f"/api/v1/transfers/{cancel}/cancel"
        verb = "cancelled"

    result = asyncio.run(_api_request("POST", path, api_url, api_key))
    if result.get("_status") == 404:
        err_console.print("[red]Transfer not found.[/red]")
        raise typer.Exit(1)
    if result.get("_status") == 409:
        err_console.print(
            "[red]Transfer is no longer pending (already accepted, rejected, or "
            "cancelled).[/red]"
        )
        raise typer.Exit(1)
    console.print(f"Transfer {verb}: {result['id']}")


@project_app.command("transfers")
def project_transfers(
    direction: str = typer.Option(
        "incoming",
        "--direction",
        "-d",
        help="incoming (waiting on you) or outgoing (you initiated).",
    ),
    state: str | None = typer.Option(
        None,
        "--state",
        help="Filter: pending / accepted / rejected / cancelled.",
    ),
) -> None:
    """List project transfers for the current user."""
    if direction not in ("incoming", "outgoing"):
        err_console.print("[red]--direction must be 'incoming' or 'outgoing'.[/red]")
        raise typer.Exit(2)

    api_url, api_key = _get_project_client()
    path = f"/api/v1/transfers?direction={direction}"
    if state:
        path += f"&state={state}"
    result = asyncio.run(_api_request("GET", path, api_url, api_key))
    transfers = result.get("transfers", [])
    if not transfers:
        console.print(f"[dim]No {direction} transfers.[/dim]")
        return
    console.print(f"[bold]{direction.title()} transfers ({len(transfers)}):[/bold]")
    for t in transfers:
        _print_transfer(t)


# ── P3: Multi-Repo Link / Unlink / List ──────────────────────────────────


def _repos_api_request(
    method: str, path: str, api_url: str, api_key: str,
    json_data: dict | None = None,
) -> dict | list:
    """Shared API helper for repo endpoints — handles 409/422 envelopes.

    GET /api/v1/projects/{id}/repos returns a bare JSON ARRAY
    (response_model=list[ProjectRepoResponse]). _api_request returns that
    list verbatim on a 200, so callers must tolerate a list result here —
    a bare list has no `.get()`, which previously crashed `sfs project
    repos` (tk_e1bd970236bc42fa). Error envelopes (404/409/422) always
    come back as dicts, so they are handled below; a list result means
    success and is returned unchanged.
    """
    result = asyncio.run(_api_request(method, path, api_url, api_key, json_data))
    # 200 success bodies may be a list (repo listing) — return as-is.
    # Only dict results carry the `_status` error envelope.
    if not isinstance(result, dict):
        return result
    if result.get("_status") == 404:
        err_console.print("[red]Project not found.[/red]")
        raise typer.Exit(1)
    if result.get("_status") == 409:
        detail = result.get("detail", {})
        if isinstance(detail, dict):
            msg = detail.get("message", str(detail))
            existing = detail.get("existing_project_id")
            if existing:
                msg += f"\nExisting project: {existing}"
            err_console.print(f"[red]{msg}[/red]")
        else:
            err_console.print(f"[red]{detail}[/red]")
        raise typer.Exit(1)
    if result.get("_status") == 422:
        detail = result.get("detail", {})
        if isinstance(detail, dict):
            err_console.print(
                f"[red]{detail.get('message', str(detail))}[/red]"
            )
        else:
            err_console.print(f"[red]{detail}[/red]")
        raise typer.Exit(1)
    return result


@project_app.command("link-repo")
def project_link_repo(
    remote: str = typer.Argument(..., help="Git remote URL to link (e.g. github.com/acme/backend.git)"),
    primary: bool = typer.Option(
        False, "--primary", help="Make this the project's primary (display) remote."
    ),
    project_id: str | None = typer.Option(
        None, "--project-id",
        help="Target project id. If omitted, resolves from the current repo.",
    ),
) -> None:
    """Link a git repo to a project.

    The repo becomes part of the project's multi-repo set.  The server
    verifies repo ownership via the GitHub App when installed; otherwise
    the link is recorded as owner-attested (unverified).

    Examples:
        sfs project link-repo github.com/acme/backend.git
        sfs project link-repo github.com/acme/backend.git --primary
        sfs project link-repo github.com/acme/api.git --project-id proj_abc
    """
    api_url, api_key = _get_project_client()
    if project_id is None:
        project_id, api_url, api_key = _resolve_project_id()

    result = _repos_api_request(
        "POST",
        f"/api/v1/projects/{project_id}/repos",
        api_url,
        api_key,
        json_data={"git_remote": remote, "is_primary": primary},
    )
    assert isinstance(result, dict)  # POST returns a single repo object
    console.print(
        f"[green]Repo linked:[/green] {result['git_remote_normalized']} "
        f"({result['id']})"
    )
    if result.get("is_primary"):
        console.print("[bold]  (primary)[/bold]")
    console.print(
        f"  verified={result['verified']}, "
        f"method={result.get('verification_method', 'none')}"
    )


@project_app.command("unlink-repo")
def project_unlink_repo(
    remote: str = typer.Argument(..., help="Git remote URL or repo id to unlink."),
    project_id: str | None = typer.Option(
        None, "--project-id",
        help="Target project id. If omitted, resolves from the current repo.",
    ),
) -> None:
    """Unlink a repo from a project.

    Refuses if this would leave an active project with zero repos.
    If unlinking the primary, the oldest remaining repo is promoted.

    Examples:
        sfs project unlink-repo github.com/acme/backend.git
        sfs project unlink-repo repo_a1b2c3d4 --project-id proj_abc
    """
    api_url, api_key = _get_project_client()
    if project_id is None:
        project_id, api_url, api_key = _resolve_project_id()

    # If given a remote URL, look up the repo id first.
    repo_id: str
    from sessionfs.server.github_app import normalize_git_remote
    normalized = normalize_git_remote(remote)
    if "/" in normalized:
        # Looks like a remote — find its repo id.
        repos = _repos_api_request(
            "GET",
            f"/api/v1/projects/{project_id}/repos",
            api_url,
            api_key,
        )
        assert isinstance(repos, list)  # GET /repos returns an array
        match = None
        for r in repos:
            if r["git_remote_normalized"] == normalized:
                match = r
                break
        if match is None:
            err_console.print(
                f"[red]Repo '{normalized}' not found on this project. "
                f"Use 'sfs project repos' to list linked repos.[/red]"
            )
            raise typer.Exit(1)
        repo_id = match["id"]
    else:
        repo_id = remote

    result = _repos_api_request(
        "DELETE",
        f"/api/v1/projects/{project_id}/repos/{repo_id}",
        api_url,
        api_key,
    )
    assert isinstance(result, dict)  # DELETE returns {"status", "repo_id"}
    console.print(f"[green]Repo unlinked:[/green] {result['repo_id']}")


@project_app.command("repos")
def project_repos(
    project_id: str | None = typer.Option(
        None, "--project-id",
        help="Target project id. If omitted, resolves from the current repo.",
    ),
) -> None:
    """List repos linked to a project.

    Examples:
        sfs project repos
        sfs project repos --project-id proj_abc
    """
    api_url, api_key = _get_project_client()
    if project_id is None:
        project_id, api_url, api_key = _resolve_project_id()

    repos = _repos_api_request(
        "GET",
        f"/api/v1/projects/{project_id}/repos",
        api_url,
        api_key,
    )
    assert isinstance(repos, list)  # GET /repos returns an array
    if not repos:
        console.print("[dim]No repos linked.[/dim]")
        return
    console.print(f"[bold]Repos for {project_id}:[/bold]")
    for r in repos:
        primary = " [bold](primary)[/bold]" if r.get("is_primary") else ""
        verified = (
            f"verified={r['verified']}, "
            f"method={r.get('verification_method', 'none')}"
        )
        console.print(
            f"  {r['id']}  {r['git_remote_normalized']}{primary}"
        )
        console.print(f"    {verified}")


def _merge_api_request(
    method: str, path: str, api_url: str, api_key: str,
    json_data: dict | None = None,
) -> dict:
    """API helper for merge endpoint — handles all merge-specific errors."""
    result = asyncio.run(_api_request(method, path, api_url, api_key, json_data))
    status = result.get("_status", 0)
    detail = result.get("detail", {})

    if status == 404:
        msg = detail if isinstance(detail, str) else detail.get("message", "Not found")
        err_console.print(f"[red]{msg}[/red]")
        raise typer.Exit(1)
    if status == 403:
        msg = detail if isinstance(detail, str) else detail.get("message", "Forbidden")
        err_console.print(f"[red]{msg}[/red]")
        raise typer.Exit(1)
    if status == 400:
        msg = detail if isinstance(detail, str) else detail.get("message", str(detail))
        err_console.print(f"[red]{msg}[/red]")
        raise typer.Exit(1)
    if status == 409:
        if isinstance(detail, dict):
            err_console.print(
                f"[red]{detail.get('message', str(detail))}[/red]"
            )
        else:
            err_console.print(f"[red]{detail}[/red]")
        raise typer.Exit(1)
    if status == 500:
        err_console.print("[red]Internal server error — merge may have failed.[/red]")
        raise typer.Exit(1)
    return result


@project_app.command("merge")
def project_merge(
    source: str = typer.Argument(
        None,
        help="Source project id to merge INTO the target. "
             "If omitted, resolves from the current repo.",
    ),
    into: str | None = typer.Option(
        None, "--into",
        help="Target project id (merge source INTO this). "
             "If omitted, the first positional argument is the source "
             "and --into is required.",
    ),
    dry_run: bool = typer.Option(
        True, "--dry-run/--confirm",
        help="Dry-run (default): validate + show plan. --confirm to execute.",
    ),
    interactive: bool = typer.Option(
        False, "--interactive",
        help="Prompt for each persona collision before renaming.",
    ),
    persona_policy: str = typer.Option(
        "rename", "--persona-policy",
        help="Collision policy: rename (default), skip, or merge_content.",
    ),
) -> None:
    """Merge one project into another.

    Merges the source project INTO the target project, reassigning all
    repos, personas, knowledge, tickets, sessions, and other data.
    The source project becomes a read-only tombstone that redirects
    to the target.

    Dry-run is the DEFAULT — validates and shows what WOULD happen
    without writing anything.  Use --confirm to execute.

    Examples:
        sfs project merge proj_src123 --into proj_tgt456
        sfs project merge proj_src123 --into proj_tgt456 --dry-run
        sfs project merge proj_src123 --into proj_tgt456 --confirm
        sfs project merge proj_src123 --into proj_tgt456 --confirm --persona-policy skip
    """
    api_url, api_key = _get_project_client()

    if source is None:
        source, api_url, api_key = _resolve_project_id()

    if into is None:
        err_console.print(
            "[red]--into <target_project_id> is required when source "
            "is resolved from the current repo.[/red]"
        )
        raise typer.Exit(1)

    # Interactive mode: pre-fetch dry-run and prompt for each collision.
    if interactive and not dry_run:
        err_console.print(
            "[yellow]Note: --interactive overrides --confirm — "
            "running dry-run first to preview collisions.[/yellow]"
        )

    if interactive:
        # Fetch dry-run first.
        plan = _merge_api_request(
            "POST",
            f"/api/v1/projects/{into}/merge",
            api_url,
            api_key,
            json_data={
                "source_project_id": source,
                "dry_run": True,
                "persona_policy": persona_policy,
            },
        )
        collisions = plan.get("persona_collisions", [])
        if collisions:
            console.print(
                f"[bold yellow]{len(collisions)} persona name "
                f"collision(s) detected:[/bold yellow]"
            )
            for c in collisions:
                console.print(f"  - {c['source_name']}")
            console.print()
            console.print(
                "Policy: [bold]{persona_policy}[/bold] — "
                "source personas will be renamed to "
                "f'{{name}}-{{src8}}'."
            )
            from rich.prompt import Confirm
            if not Confirm.ask("Proceed with merge?", default=False):
                console.print("[dim]Merge cancelled.[/dim]")
                raise typer.Exit(0)
        else:
            console.print("[green]No persona collisions detected.[/green]")

    result = _merge_api_request(
        "POST",
        f"/api/v1/projects/{into}/merge",
        api_url,
        api_key,
        json_data={
            "source_project_id": source,
            "dry_run": dry_run,
            "persona_policy": persona_policy,
        },
    )

    if dry_run:
        _print_merge_plan(result)
    else:
        console.print(
            f"[green]Merge complete![/green] "
            f"Source {source} → Target {into}"
        )
        console.print(f"  Audit ID: {result.get('audit_id')}")
        renames = result.get("persona_renames", [])
        if renames:
            console.print(f"  Persona renames: {len(renames)}")
            for rn in renames:
                console.print(
                    f"    [dim]{rn['old_name']} → "
                    f"{rn['new_name']}[/dim]"
                )
        skipped_ke = result.get("skipped_ke_ids", [])
        if skipped_ke:
            console.print(
                f"  [dim]Skipped {len(skipped_ke)} duplicate "
                f"knowledge entries.[/dim]"
            )
        rules = result.get("rules_action", "none")
        if rules != "none":
            console.print(f"  Rules: {rules}")


def _print_merge_plan(plan: dict) -> None:
    """Pretty-print a dry-run merge plan."""
    stats = plan.get("stats", {})
    console.print("[bold]Merge Plan (dry-run — no changes made)[/bold]")
    console.print()
    console.print("[bold]Rows to reassign:[/bold]")
    for label in (
        "repos", "personas", "tickets", "agent_runs", "sessions",
        "handoff_attachments", "project_transfers",
        "context_compilations", "knowledge_entries",
        "knowledge_links", "knowledge_pages", "wiki_page_revisions",
        "retrieval_audit_contexts", "retrieval_audit_events",
    ):
        count = stats.get(label, 0)
        if count:
            console.print(f"  {label}: {count}")

    collisions = stats.get("persona_collisions", 0)
    if collisions:
        console.print(
            f"\n[yellow]Persona name collisions: {collisions}[/yellow]"
        )
        for c in plan.get("persona_collisions", []):
            console.print(f"  - {c['source_name']}")

    slug_c = stats.get("slug_collisions", 0)
    if slug_c:
        console.print(
            f"\n[yellow]Wiki page slug collisions: {slug_c}[/yellow]"
        )
        for s in plan.get("slug_collisions", []):
            console.print(f"  - {s}")

    ke_d = stats.get("ke_duplicates", 0)
    if ke_d:
        console.print(
            f"\n[dim]Knowledge entry duplicates (will be skipped): "
            f"{ke_d}[/dim]"
        )

    console.print()
    repo_count = stats.get("repos", 0)
    if repo_count:
        console.print(
            f"[green]{repo_count} repo(s) will be linked to the target "
            f"(the source's primary becomes a non-primary linked repo)."
            f"[/green]"
        )

    if stats.get("source_has_rules") and stats.get("target_has_rules"):
        console.print(
            "[yellow]Both projects have rules — source rules "
            "will be archived as a wiki page.[/yellow]"
        )
    elif stats.get("source_has_rules") and not stats.get("target_has_rules"):
        console.print(
            "[green]Source rules will be promoted to target "
            "(target has none).[/green]"
        )

    console.print()
    console.print(
        "[dim]Run with --confirm to execute this merge.[/dim]"
    )
