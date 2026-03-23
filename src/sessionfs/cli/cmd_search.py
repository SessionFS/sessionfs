"""CLI search command: sfs search <query>."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from sessionfs.cli.common import console, err_console, get_store_dir


def search(
    query: str = typer.Argument(..., help="Search query"),
    tool: Optional[str] = typer.Option(None, "--tool", help="Filter by tool"),
    days: Optional[int] = typer.Option(None, "--days", help="Limit to last N days"),
    limit: int = typer.Option(10, "--limit", help="Max results"),
    cloud: bool = typer.Option(False, "--cloud", help="Search cloud (Pro+)"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Search sessions by keyword across all indexed content."""
    if cloud:
        _search_cloud(query, tool=tool, days=days, limit=limit, json_output=json_output)
    else:
        _search_local(query, tool=tool, days=days, limit=limit, json_output=json_output)


def _search_local(
    query: str,
    *,
    tool: str | None,
    days: int | None,
    limit: int,
    json_output: bool,
) -> None:
    """Search the local FTS5 index (free, works offline)."""
    from sessionfs.mcp.search import SessionSearchIndex

    store_dir = get_store_dir()
    db_path = store_dir / "search.db"

    if not db_path.exists():
        # Try to build the index first
        index = SessionSearchIndex(db_path)
        index.initialize()
        count = index.reindex_all(store_dir)
        if count == 0:
            err_console.print("[yellow]No sessions indexed yet. Run 'sfs daemon start' to capture sessions.[/yellow]")
            raise typer.Exit(1)
    else:
        index = SessionSearchIndex(db_path)
        index.initialize()

    try:
        results = index.search(query, tool_filter=tool, limit=limit)
    finally:
        index.close()

    if not results:
        if json_output:
            console.print("[]")
        else:
            err_console.print(f"[dim]No results for '{query}'[/dim]")
        return

    if json_output:
        console.print(json.dumps(results, indent=2, default=str))
        return

    table = Table(title=f"Search: {query}", show_lines=True)
    table.add_column("Session", style="cyan", no_wrap=True)
    table.add_column("Title", max_width=40)
    table.add_column("Tool", style="green")
    table.add_column("Date", style="dim")
    table.add_column("Excerpt", max_width=60)

    for r in results:
        table.add_row(
            r["session_id"][:20],
            r.get("title") or "[dim]untitled[/dim]",
            r.get("source_tool", ""),
            r.get("created_at", "")[:10] if r.get("created_at") else "",
            r.get("excerpt", ""),
        )

    console.print(table)


def _search_cloud(
    query: str,
    *,
    tool: str | None,
    days: int | None,
    limit: int,
    json_output: bool,
) -> None:
    """Search the cloud API (requires auth, Pro+ tier)."""
    import asyncio

    try:
        asyncio.run(_search_cloud_async(
            query, tool=tool, days=days, limit=limit, json_output=json_output,
        ))
    except KeyboardInterrupt:
        raise typer.Exit(1)


async def _search_cloud_async(
    query: str,
    *,
    tool: str | None,
    days: int | None,
    limit: int,
    json_output: bool,
) -> None:
    """Async implementation for cloud search."""
    try:
        import httpx
    except ImportError:
        err_console.print("[red]Cloud search requires httpx. Install with: pip install httpx[/red]")
        raise typer.Exit(1)

    from sessionfs.daemon.config import load_config

    config = load_config()
    if not config.sync.api_key:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise typer.Exit(1)

    params: dict = {"q": query, "page_size": min(limit, 50)}
    if tool:
        params["tool"] = tool
    if days:
        params["days"] = days

    api_url = config.sync.api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {config.sync.api_key}"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{api_url}/api/v1/sessions/search",
            params=params,
            headers=headers,
        )

    if resp.status_code == 403:
        detail = resp.json().get("detail", {})
        error = detail.get("error", detail) if isinstance(detail, dict) else detail
        if isinstance(error, dict) and error.get("code") == "TIER_LIMIT":
            err_console.print(f"[yellow]{error.get('message', 'Upgrade required')}[/yellow]")
        else:
            err_console.print(f"[red]Access denied: {detail}[/red]")
        raise typer.Exit(1)

    if resp.status_code != 200:
        err_console.print(f"[red]Search failed ({resp.status_code}): {resp.text}[/red]")
        raise typer.Exit(1)

    data = resp.json()
    results = data.get("results", [])

    if not results:
        if json_output:
            console.print("[]")
        else:
            err_console.print(f"[dim]No cloud results for '{query}'[/dim]")
        return

    if json_output:
        console.print(json.dumps(results, indent=2, default=str))
        return

    table = Table(title=f"Cloud Search: {query} ({data.get('total', 0)} total)", show_lines=True)
    table.add_column("Session", style="cyan", no_wrap=True)
    table.add_column("Title", max_width=40)
    table.add_column("Tool", style="green")
    table.add_column("Date", style="dim")
    table.add_column("Snippet", max_width=60)

    for r in results:
        snippet = ""
        matches = r.get("matches", [])
        if matches:
            snippet = matches[0].get("snippet", "")
        table.add_row(
            r["session_id"][:20],
            r.get("title") or "[dim]untitled[/dim]",
            r.get("source_tool", ""),
            r.get("updated_at", "")[:10] if r.get("updated_at") else "",
            snippet,
        )

    console.print(table)
