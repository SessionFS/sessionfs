"""`sfs hooks …` — install/remove SessionStart hooks for native AI tools.

Today only Claude Code exposes a native ``SessionStart`` hook. This module
wires the command ``sfs rules emit --tool claude-code --format hook`` into
``settings.json`` so compiled project rules are injected at every Claude
Code session start without requiring a CLAUDE.md file in the repo.

Other tools surface as ``N/A`` in ``status`` until their hook systems
exist or stabilise. The CLI keeps a single ``--for <tool>`` flag so the
shape of the command doesn't shift when (e.g.) Codex adds equivalent
support.

Subcommands
-----------

- ``sfs hooks install --for claude-code [--user|--project] [--force]``
- ``sfs hooks uninstall --for claude-code [--user|--project] [--force]``
- ``sfs hooks status``
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from sessionfs.cli.common import console, err_console
from sessionfs.cli.cmd_rules import TOOL_FILES, _is_managed
from sessionfs.sync.hooks_installer import (
    MalformedSettingsError,
    install_session_start_hook,
    is_hook_installed,
    uninstall_session_start_hook,
)

hooks_app = typer.Typer(
    name="hooks",
    help="Install and manage SessionFS hooks for native AI tools.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The exact command Claude Code runs at SessionStart. We point at the
# canonical CLI binary name `sfs`. If the user has a non-standard binary
# location, the system PATH at hook-execution time will resolve it just
# like any other shell command would. This matches what every other
# `command:` hook in Claude Code does — we don't try to be cleverer.
CLAUDE_CODE_HOOK_COMMAND = "sfs rules emit --tool claude-code --format hook"

# Tools we currently support installing hooks FOR. Distinct from
# `cmd_rules.SUPPORTED_TOOLS` because most tools don't have a native hook
# system today.
HOOK_CAPABLE_TOOLS: tuple[str, ...] = ("claude-code",)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _user_settings_path(tool: str) -> Path:
    """Return the user-scope settings.json path for ``tool``.

    We hard-code Claude Code's path because it's the only hook target
    today. When other tools land we'll branch here.
    """
    if tool == "claude-code":
        return Path.home() / ".claude" / "settings.json"
    raise typer.BadParameter(f"Unknown hook-capable tool: {tool}")


def _git_root() -> Path | None:
    """Return the current repo's toplevel, or None if not in a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _project_settings_path(tool: str) -> Path | None:
    """Return ``<repo>/.claude/settings.json`` for ``--project`` installs.

    Returns None if cwd isn't inside a git repo — project scope only makes
    sense when there's a repo to commit the file to.
    """
    if tool != "claude-code":
        raise typer.BadParameter(f"Unknown hook-capable tool: {tool}")
    root = _git_root()
    if root is None:
        return None
    return root / ".claude" / "settings.json"


def _resolve_target(tool: str, *, user: bool, project: bool) -> Path:
    """Pick the settings.json path based on the scope flags."""
    if user and project:
        err_console.print("[red]Cannot combine --user and --project.[/red]")
        raise typer.Exit(1)
    if project:
        path = _project_settings_path(tool)
        if path is None:
            err_console.print(
                "[red]--project requires running inside a git repository.[/red]"
            )
            raise typer.Exit(1)
        return path
    # Default to user scope. This matches Claude Code's most common
    # configuration shape (machine-wide preferences).
    return _user_settings_path(tool)


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


def _warn_on_managed_claude_md(scope_label: str) -> None:
    """If a SessionFS-managed CLAUDE.md exists in the current repo, print a
    soft advisory after install. This isn't fatal — many teams keep the
    file as a shared baseline and use the hook for personal freshness.
    """
    root = _git_root()
    if root is None:
        return
    path = root / TOOL_FILES["claude-code"]
    if not path.is_file():
        return
    if not _is_managed(path):
        return
    console.print(
        f"\n[yellow]Note:[/yellow] managed [bold]CLAUDE.md[/bold] is also "
        f"present in this repo. Both will inject the same compiled rules at "
        f"session start ({scope_label} hook + file)."
    )
    console.print(
        "[dim]  • Delete CLAUDE.md to dedupe, OR[/dim]"
    )
    console.print(
        "[dim]  • Keep both for team-baseline + personal-fresh hybrid.[/dim]"
    )


# ---------------------------------------------------------------------------
# install / uninstall / status
# ---------------------------------------------------------------------------


@hooks_app.command("install")
def hooks_install(
    for_tool: str = typer.Option(
        ...,
        "--for",
        help=f"Tool to install hook for. One of: {', '.join(HOOK_CAPABLE_TOOLS)}.",
    ),
    user: bool = typer.Option(
        False,
        "--user",
        help="Install at user scope (~/.claude/settings.json). Default.",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help="Install at project scope (.claude/settings.json in repo).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing managed entry whose command differs.",
    ),
) -> None:
    """Install a SessionStart hook so SessionFS injects rules at session start."""
    if for_tool not in HOOK_CAPABLE_TOOLS:
        err_console.print(
            f"[red]No native hook support for {for_tool!r}. "
            f"Hook-capable tools: {', '.join(HOOK_CAPABLE_TOOLS)}.[/red]"
        )
        raise typer.Exit(1)

    target_path = _resolve_target(for_tool, user=user, project=project)
    scope_label = "project" if project else "user"

    try:
        changed = install_session_start_hook(target_path, CLAUDE_CODE_HOOK_COMMAND)
    except MalformedSettingsError as exc:
        err_console.print("[red]Refusing to modify malformed settings file:[/red]")
        err_console.print(f"  {exc}")
        err_console.print(
            "[dim]Hint: open the file manually, fix the JSON, then re-run.[/dim]"
        )
        raise typer.Exit(1) from exc
    except OSError as exc:
        err_console.print(f"[red]Could not write {target_path}: {exc}[/red]")
        raise typer.Exit(1) from exc

    pretty = _shorten_home(target_path)
    if changed:
        console.print(
            f"[green]Hook installed[/green] at [bold]{pretty}[/bold] "
            f"({scope_label} scope, SessionStart)."
        )
        console.print(
            "[dim]SessionFS will inject project rules at every Claude Code startup.[/dim]"
        )
    else:
        console.print(
            f"[dim]Hook already installed at {pretty} — no changes.[/dim]"
        )

    # Soft advisory if a managed CLAUDE.md is also present in this repo —
    # both will inject the same content. Not an error; user decides.
    _warn_on_managed_claude_md(scope_label)
    # `force` is reserved for future divergence between desired/installed
    # commands. Today the merge logic already updates a stale command in
    # place, so the flag is currently a no-op kept for forward-compat.
    _ = force


@hooks_app.command("uninstall")
def hooks_uninstall(
    for_tool: str = typer.Option(
        ...,
        "--for",
        help=f"Tool to uninstall hook for. One of: {', '.join(HOOK_CAPABLE_TOOLS)}.",
    ),
    user: bool = typer.Option(
        False,
        "--user",
        help="Uninstall at user scope (~/.claude/settings.json). Default.",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help="Uninstall at project scope (.claude/settings.json in repo).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Remove SessionFS-managed SessionStart entries (preserves user hooks)."""
    if for_tool not in HOOK_CAPABLE_TOOLS:
        err_console.print(
            f"[red]No native hook support for {for_tool!r}. "
            f"Hook-capable tools: {', '.join(HOOK_CAPABLE_TOOLS)}.[/red]"
        )
        raise typer.Exit(1)

    target_path = _resolve_target(for_tool, user=user, project=project)
    pretty = _shorten_home(target_path)

    if not target_path.exists():
        console.print(f"[dim]No settings file at {pretty} — nothing to uninstall.[/dim]")
        return

    if not is_hook_installed(target_path):
        console.print(
            f"[dim]No SessionFS hook installed at {pretty} — nothing to uninstall.[/dim]"
        )
        return

    if not force:
        confirmed = typer.confirm(
            f"Remove SessionFS SessionStart hook from {pretty}?",
            default=True,
        )
        if not confirmed:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        changed = uninstall_session_start_hook(target_path)
    except MalformedSettingsError as exc:
        err_console.print("[red]Refusing to modify malformed settings file:[/red]")
        err_console.print(f"  {exc}")
        raise typer.Exit(1) from exc
    except OSError as exc:
        err_console.print(f"[red]Could not write {target_path}: {exc}[/red]")
        raise typer.Exit(1) from exc

    if changed:
        console.print(
            f"[green]Hook removed[/green] from [bold]{pretty}[/bold]. "
            f"User-defined hooks preserved."
        )
    else:
        # is_hook_installed said yes but uninstall did nothing — race or
        # marker mismatch. Surface honestly.
        console.print(f"[dim]No SessionFS hook to remove at {pretty}.[/dim]")


@hooks_app.command("status")
def hooks_status() -> None:
    """Show installed SessionFS hooks across tools.

    Reports both ``--user`` and ``--project`` scopes for Claude Code, and
    a single ``N/A`` line per tool that lacks a native hook system.
    """
    rows: list[tuple[str, str]] = []

    # Claude Code: user + project scopes.
    user_path = _user_settings_path("claude-code")
    if is_hook_installed(user_path):
        rows.append(
            ("claude-code (user)",
             f"INSTALLED at {_shorten_home(user_path)} (SessionStart)")
        )
    else:
        rows.append(("claude-code (user)", "not installed"))

    project_path = _project_settings_path("claude-code")
    if project_path is None:
        rows.append(("claude-code (project)", "not in a git repo"))
    elif is_hook_installed(project_path):
        rows.append(
            ("claude-code (project)",
             f"INSTALLED at {_shorten_home(project_path)} (SessionStart)")
        )
    else:
        rows.append(("claude-code (project)", "not installed"))

    # Tools with no native hook system today. We list them so users know we
    # haven't simply forgotten — they get the file-based path via
    # `sfs rules compile`.
    rows.extend([
        ("codex", "N/A (no native hook system)"),
        ("gemini", "N/A"),
        ("cursor", "N/A"),
        ("copilot", "N/A"),
    ])

    console.print("[bold]SessionFS Hooks[/bold]")
    console.print("[dim]" + "─" * 15 + "[/dim]")
    label_width = max(len(label) for label, _ in rows) + 2
    for label, status in rows:
        padded = label + ":"
        console.print(f"{padded:<{label_width + 1}} {status}")


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _shorten_home(path: Path) -> str:
    """Render absolute paths as ``~/...`` when they live under HOME."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
