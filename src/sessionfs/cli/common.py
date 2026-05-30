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


def format_api_error(body: Any, status: int) -> str:
    """Extract a human-readable error message from a server response body.

    v0.10.24 tk_e7da4c4508d94bac — handles the v0.10.x error envelope
    shape `{"error": {"code", "message", "details"}}` cleanly and falls
    back to the raw body for older shapes. Use instead of formatting the
    raw dict in user-facing error prints — agents and humans both prefer
    "duplicate_resource: A resource with that value already exists." over
    "{'error': {'code': 'duplicate_resource', 'message': '...', 'details': {...}}}".

    Returns a single-line string; callers wrap it in their own
    "API error (status): ..." prefix if they want.
    """
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code") or ""
            message = error.get("message") or ""
            if message and code:
                return f"{code}: {message}"
            if message:
                return message
            if code:
                return code
            return str(status)
        # Legacy `detail` shape (some older routes / FastAPI defaults)
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if isinstance(detail, dict):
            msg = detail.get("message") or detail.get("error") or ""
            if msg:
                return str(msg)
    if isinstance(body, str) and body:
        return body
    return str(body)


def handle_errors(func):
    """Decorator that catches common exceptions and prints friendly messages."""
    # click.exceptions.ClickException is the base for typer.BadParameter
    # and Typer/Click usage errors. They render themselves cleanly with
    # the standard "Usage: ... \n Error: ..." formatting; if we let
    # them fall through to the generic Exception catch they print as
    # "Unexpected error: ..." which looks like a crash. Let Click/Typer
    # handle their own validation errors and we only intercept the
    # truly-unexpected stuff.
    import click.exceptions as _click_exc

    # v0.10.26 hotfix: typer 0.26 vendors its own click module —
    # `typer.BadParameter` is `typer._click.exceptions.BadParameter`,
    # NOT `click.exceptions.ClickException`. The two class hierarchies
    # are disjoint as of typer 0.26, so `except click.exceptions
    # .ClickException` no longer catches typer-raised parameter errors.
    # Result: typer's BadParameter falls through to the generic
    # `except Exception` and renders as "Unexpected error: ..."
    # — exactly the regression the Codex round-2 review on
    # tk_e025375272b84a95 / v0.10.11 originally fixed.
    # Collect both ClickException base classes so the except clauses
    # below catch either family regardless of typer version.
    _click_exception_bases: tuple = (_click_exc.ClickException,)
    _click_exit_bases: tuple = (_click_exc.Exit,)
    _click_abort_bases: tuple = (_click_exc.Abort,)
    try:
        from typer._click import exceptions as _typer_click_exc
        if _typer_click_exc.ClickException not in _click_exception_bases:
            _click_exception_bases = _click_exception_bases + (
                _typer_click_exc.ClickException,
            )
        if hasattr(_typer_click_exc, "Exit"):
            _click_exit_bases = _click_exit_bases + (_typer_click_exc.Exit,)
        if hasattr(_typer_click_exc, "Abort"):
            _click_abort_bases = _click_abort_bases + (_typer_click_exc.Abort,)
    except (ImportError, AttributeError):
        # typer < 0.26 (or future renames) — fall back to standard click.
        pass

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SystemExit:
            raise
        except _click_exit_bases as exc:
            # `typer.Exit(code)` raises a `click.exceptions.Exit`, which
            # is a `RuntimeError`, not a `SystemExit`. Without this
            # branch the generic `except Exception` below would swallow
            # it and emit "Unexpected error: <code>" with `SystemExit(1)`
            # — silently downgrading every CLI's `typer.Exit(2)` to a
            # generic 1. Re-raise as `SystemExit` so callers see the
            # intended code.
            raise SystemExit(exc.exit_code)
        except _click_exception_bases:
            # Re-raise so Typer's outer handler can render the standard
            # parser-error format ("Usage: ... \n Try '... --help'." +
            # "Error: <message>"). Wrapping this in our friendly
            # decorator would mask the actual UX.
            raise
        except KeyboardInterrupt:
            err_console.print("\nCancelled.")
            raise SystemExit(130)
        except _click_abort_bases:
            # click.exceptions.Abort inherits from RuntimeError, not
            # ClickException, so the pass-through above misses it. It
            # fires when an interactive prompt (typer.confirm) hits
            # EOF — typically because stdin is piped or the user hit
            # Ctrl-D. Without this branch, callers see "Unexpected
            # error:" with an empty body which looks like a crash.
            err_console.print(
                "[dim]Cancelled (no interactive input). "
                "Pass --yes to skip confirmation prompts in scripts.[/dim]"
            )
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


def confirm_or_exit(
    message: str,
    *,
    default: bool = False,
    yes: bool = False,
    yes_hint: str = "Pass --yes to confirm in non-interactive mode.",
) -> bool:
    """typer.confirm wrapper that handles non-interactive stdin gracefully.

    - If `yes` is True, return True without prompting.
    - If stdin is not a TTY (piped, redirected, no terminal), print a
      clear error mentioning the `--yes` flag and raise SystemExit(2).
      Without this guard, click.confirm hits EOF immediately and
      raises click.exceptions.Abort which falls through handle_errors
      as a generic "Unexpected error:".
    - Otherwise delegate to typer.confirm.
    """
    import sys
    import typer

    if yes:
        return True
    if not sys.stdin.isatty():
        err_console.print(f"[red]{message}[/red] (no interactive input)")
        err_console.print(f"[dim]{yes_hint}[/dim]")
        raise SystemExit(2)
    return typer.confirm(message, default=default)


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
