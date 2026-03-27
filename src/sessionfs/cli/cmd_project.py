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
