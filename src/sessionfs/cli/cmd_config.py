"""Configuration commands: sfs config show|set|default-org."""

from __future__ import annotations

import asyncio
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
from pathlib import Path

import typer

from sessionfs.cli.common import console, err_console, get_store_dir
from sessionfs.daemon.config import ensure_config

config_app = typer.Typer(name="config", help="Manage SessionFS configuration.", no_args_is_help=True)


def _config_path() -> Path:
    return get_store_dir() / "config.toml"


def _toml_value(v: object) -> str:
    """Convert a Python value to TOML string representation."""
    if isinstance(v, bool):
        return "true" if v else "false"
    elif isinstance(v, int):
        return str(v)
    elif isinstance(v, float):
        return str(v)
    elif isinstance(v, str):
        return f'"{v}"'
    else:
        return f'"{v}"'


def _write_toml(path: Path, data: dict) -> None:
    """Simple TOML writer for flat/single-nested config."""
    lines: list[str] = []

    # Write top-level scalars first
    for key, value in data.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")

    if lines:
        lines.append("")

    # Write sections
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"[{key}]")
            for k, v in value.items():
                lines.append(f"{k} = {_toml_value(v)}")
            lines.append("")

    path.write_text("\n".join(lines))


@config_app.command("show")
def config_show() -> None:
    """Show current configuration."""
    path = _config_path()
    ensure_config(path)

    if not path.exists():
        console.print("[dim]No config file found.[/dim]")
        return

    console.print(f"[bold]Config:[/bold] {path}")
    console.print()

    # Print raw TOML — escape Rich markup so [section] headers aren't swallowed
    text = path.read_text()
    from rich.text import Text
    console.print(Text(text))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(help="Config key (dotted path, e.g., 'claude_code.enabled')."),
    value: str = typer.Argument(help="Value to set."),
) -> None:
    """Set a configuration value."""
    path = _config_path()
    ensure_config(path)

    # Read existing config
    if path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
    else:
        data = {}

    # Type coercion
    if value.lower() == "true":
        typed_value: object = True
    elif value.lower() == "false":
        typed_value = False
    else:
        try:
            typed_value = int(value)
        except ValueError:
            try:
                typed_value = float(value)
            except ValueError:
                typed_value = value

    # Set value (supports dotted keys)
    parts = key.split(".")
    target = data
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = typed_value

    _write_toml(path, data)
    console.print(f"[green]Set {key} = {_toml_value(typed_value)}[/green]")


# ─────────────────────────────────────────────────────────────────────────
# v0.10.0 Phase 5 — server-canonical default org preference.
#
# `sfs config default-org <org_id>` sets User.default_org_id on the
# server (validated against OrgMember). The value is consumed by
# `sfs project init` when neither --org nor --personal is passed: the
# CLI reads /api/v1/auth/me and uses default_org_id as the new
# project's scope. Session→project linkage at sync time keys on git
# remote → Project lookup, not on default_org_id (see
# routes/sessions.py:_resolve_project_id_for_session); a v0.10.x
# follow-up may add a default-org fallback for unmatched remotes.
# Storing this locally would risk a stale read after the user is
# removed from the org; the server is the source of truth.
# ─────────────────────────────────────────────────────────────────────────


def _auth_client() -> tuple[str, str]:
    """Resolve api_url + api_key from the daemon sync config."""
    from sessionfs.cli.cmd_cloud import _load_sync_config
    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise typer.Exit(1)
    return cfg["api_url"], cfg["api_key"]


async def _http_json(method: str, path: str, api_url: str, api_key: str, body: dict | None = None) -> dict:
    import httpx
    url = f"{api_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=30) as client:
        if method == "GET":
            resp = await client.get(url, headers=headers)
        elif method == "PUT":
            resp = await client.put(url, headers=headers, json=body)
        else:
            raise ValueError(f"Unsupported method: {method}")
    if resp.status_code >= 400:
        detail = resp.json().get("detail", resp.text) if resp.headers.get(
            "content-type", ""
        ).startswith("application/json") else resp.text
        return {"_status": resp.status_code, "_detail": detail}
    return resp.json()


@config_app.command("default-org")
def config_default_org(
    org_id: str | None = typer.Argument(
        None,
        help="Org id to set as default. Omit and pass --clear to remove.",
    ),
    clear: bool = typer.Option(
        False, "--clear", help="Clear the default org preference."
    ),
) -> None:
    """Show, set, or clear your default org.

    With no arguments: shows the current default_org_id from the server.
    With an org id: sets default_org_id (must be a member of that org).
    With --clear: removes the preference (falls back to personal scope).

    `sfs project init` consults this when neither --org nor --personal
    is passed and uses it as the new project's scope. Session→project
    linkage at sync time keys on workspace git remote → Project lookup
    instead, so untracked workspaces stay unlinked regardless of this
    value (run `sfs project init` first to create the project row).
    """
    if org_id is not None and clear:
        err_console.print(
            "[red]Pass either an org id OR --clear, not both.[/red]"
        )
        raise typer.Exit(2)

    api_url, api_key = _auth_client()

    if org_id is None and not clear:
        # Show mode.
        result = asyncio.run(_http_json("GET", "/api/v1/auth/me", api_url, api_key))
        if "_status" in result:
            err_console.print(
                f"[red]Could not load profile (HTTP {result['_status']}): {result.get('_detail', '')}[/red]"
            )
            raise typer.Exit(1)
        current = result.get("default_org_id")
        if current is None:
            console.print(
                "[dim]No default org set. Sessions will route to personal "
                "scope unless their project is org-scoped.[/dim]"
            )
        else:
            console.print(f"Default org: [bold]{current}[/bold]")
        return

    # Set or clear.
    payload = {"org_id": None if clear else org_id}
    result = asyncio.run(
        _http_json("PUT", "/api/v1/auth/me/default-org", api_url, api_key, body=payload)
    )
    if "_status" in result:
        if result["_status"] == 403:
            err_console.print(
                "[red]You are not a member of that org. Pick one you belong to "
                "or run 'sfs config default-org --clear'.[/red]"
            )
        else:
            err_console.print(
                f"[red]Set failed (HTTP {result['_status']}): {result.get('_detail', '')}[/red]"
            )
        raise typer.Exit(1)

    if clear:
        console.print("[green]Default org cleared.[/green]")
    else:
        console.print(f"[green]Default org set to {org_id}.[/green]")
