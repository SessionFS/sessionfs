"""Shared CLI utilities."""

from __future__ import annotations

import functools
import json
import sqlite3
from pathlib import Path
from typing import Any

from rich.console import Console

from sessionfs.daemon.config import load_config
from sessionfs.store.local import LocalStore

console = Console()
err_console = Console(stderr=True)


def handle_errors(func):
    """Decorator that catches common exceptions and prints friendly messages."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SystemExit:
            raise
        except KeyboardInterrupt:
            err_console.print("\nCancelled.")
            raise SystemExit(130)
        except sqlite3.DatabaseError as exc:
            err_console.print(f"[red]Database error: {exc}[/red]")
            err_console.print(
                "[dim]Hint: try deleting ~/.sessionfs/index.db and running "
                "'sfs daemon rebuild-index'.[/dim]"
            )
            raise SystemExit(1)
        except ConnectionError as exc:
            err_console.print(f"[red]Connection failed: {exc}[/red]")
            err_console.print(
                "[dim]Hint: check your network connection and server URL.[/dim]"
            )
            raise SystemExit(1)
        except PermissionError as exc:
            err_console.print(f"[red]Permission denied: {exc}[/red]")
            err_console.print(
                "[dim]Hint: check file permissions with "
                "'chmod 700 ~/.sessionfs'.[/dim]"
            )
            raise SystemExit(1)
        except FileNotFoundError as exc:
            err_console.print(f"[red]File not found: {exc}[/red]")
            err_console.print(
                "[dim]Hint: run 'sfs init' to set up SessionFS.[/dim]"
            )
            raise SystemExit(1)
        except Exception as exc:
            err_console.print(f"[red]Unexpected error: {exc}[/red]")
            err_console.print(
                "[dim]If this persists, please report at "
                "https://github.com/SessionFS/sessionfs/issues[/dim]"
            )
            raise SystemExit(1)

    return wrapper


def get_store_dir() -> Path:
    """Resolve the store directory from config."""
    config = load_config()
    return config.store_dir


def open_store(initialize: bool = True) -> LocalStore:
    """Open the local store, optionally initializing it."""
    store = LocalStore(get_store_dir())
    if initialize:
        store.initialize()
    return store


def resolve_session_id(store: LocalStore, prefix: str) -> str:
    """Resolve a session ID prefix or alias to a full session ID.

    Checks: exact ID match, alias match (from local manifests), then prefix search.
    Requires at least 3 characters for aliases, 4 for ID prefixes.
    Errors on ambiguity or not found.
    """
    # Exact match first
    session = store.get_session_metadata(prefix)
    if session:
        return prefix

    # Check if input matches an alias in any local session's manifest
    alias_match = _resolve_alias(store, prefix)
    if alias_match:
        return alias_match

    if len(prefix) < 4:
        err_console.print("[red]Session ID prefix must be at least 4 characters.[/red]")
        raise SystemExit(1)

    # Prefix search
    matches = store.find_sessions_by_prefix(prefix)
    if len(matches) == 0:
        err_console.print(f"[red]No session found matching '{prefix}'.[/red]")
        raise SystemExit(1)
    if len(matches) > 1:
        err_console.print(f"[red]Ambiguous prefix '{prefix}' — matches {len(matches)} sessions:[/red]")
        for m in matches[:5]:
            err_console.print(f"  {m['session_id']}")
        if len(matches) > 5:
            err_console.print(f"  ... and {len(matches) - 5} more")
        raise SystemExit(1)

    return matches[0]["session_id"]


def _resolve_alias(store: LocalStore, alias: str) -> str | None:
    """Search local session manifests for a matching alias.

    Returns the session ID if found, None otherwise.
    """
    sessions = store.list_sessions()
    for s in sessions:
        session_dir = store.get_session_dir(s["session_id"])
        if session_dir is None:
            continue
        manifest_path = session_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("alias") == alias:
                return s["session_id"]
        except (json.JSONDecodeError, OSError):
            continue
    return None


def read_sfs_messages(session_dir: Path) -> list[dict[str, Any]]:
    """Read messages from a session's messages.jsonl."""
    messages_path = session_dir / "messages.jsonl"
    if not messages_path.exists():
        return []
    messages = []
    with open(messages_path) as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    return messages


def get_session_dir_or_exit(store: LocalStore, session_id: str) -> Path:
    """Get a session directory or exit with error."""
    session_dir = store.get_session_dir(session_id)
    if session_dir is None:
        err_console.print(f"[red]Session directory not found for '{session_id}'.[/red]")
        raise SystemExit(1)
    return session_dir
