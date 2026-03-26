"""Storage management commands: sfs storage, sfs storage prune."""

from __future__ import annotations

import typer
from rich.table import Table

from sessionfs.cli.common import console, err_console, open_store
from sessionfs.store.pruner import (
    SessionPruner,
    StorageConfig,
    _human_bytes,
    parse_size,
)

storage_app = typer.Typer(
    name="storage", help="Manage local session storage.", no_args_is_help=False
)


def _load_storage_config() -> StorageConfig:
    """Load storage config from config.toml [storage] section."""
    from sessionfs.daemon.config import load_config

    config = load_config()
    raw = {}
    # Read raw TOML for [storage] section
    config_path = config.store_dir / "config.toml"
    if config_path.exists():
        import sys

        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib
        with open(config_path, "rb") as f:
            raw = tomllib.load(f).get("storage", {})

    sc = StorageConfig()
    if "max_local_storage" in raw:
        sc.max_local_bytes = parse_size(str(raw["max_local_storage"]))
    if "local_retention_days" in raw:
        sc.local_retention_days = int(raw["local_retention_days"])
    if "synced_retention_days" in raw:
        sc.synced_retention_days = int(raw["synced_retention_days"])
    if "preserve_bookmarked" in raw:
        sc.preserve_bookmarked = bool(raw["preserve_bookmarked"])
    if "preserve_aliased" in raw:
        sc.preserve_aliased = bool(raw["preserve_aliased"])
    return sc


def _format_age(days: float) -> str:
    """Format age in days to human string."""
    if days < 1:
        return "< 1 day"
    elif days < 30:
        return f"{int(days)} days"
    elif days < 365:
        return f"{int(days / 30)} months"
    else:
        return f"{days / 365:.1f} years"


@storage_app.callback(invoke_without_command=True)
def storage_status(ctx: typer.Context) -> None:
    """Show current local storage usage."""
    if ctx.invoked_subcommand is not None:
        return

    store = open_store()
    try:
        sc = _load_storage_config()
        pruner = SessionPruner(store.sessions_dir, store.index.conn)
        usage = pruner.calculate_usage()

        pct = (usage.total_bytes / sc.max_local_bytes * 100) if sc.max_local_bytes > 0 else 0
        pct_clamped = min(pct, 100)

        # Color based on usage
        if pct >= 95:
            bar_color = "red"
        elif pct >= 80:
            bar_color = "yellow"
        else:
            bar_color = "green"

        console.print()
        console.print("[bold]Local Storage[/bold]")
        console.print("─" * 40)
        console.print(
            f"Used:     {_human_bytes(usage.total_bytes)} / "
            f"{_human_bytes(sc.max_local_bytes)} ({pct:.0f}%)"
        )
        console.print(
            f"Sessions: {usage.session_count} "
            f"({usage.synced_count} synced, {usage.unsynced_count} local-only)"
        )

        if usage.oldest_session_at:
            from datetime import datetime, timezone

            try:
                oldest = datetime.fromisoformat(
                    usage.oldest_session_at.replace("Z", "+00:00")
                )
                age_days = (
                    datetime.now(timezone.utc) - oldest
                ).total_seconds() / 86400.0
                console.print(f"Oldest:   {_format_age(age_days)} ago")
            except (ValueError, TypeError):
                pass

        console.print(
            f"Policy:   {sc.local_retention_days}-day retention, "
            f"{sc.synced_retention_days}-day synced retention"
        )
        console.print()

        # Progress bar
        filled = int(pct_clamped / 4)
        empty = 25 - filled
        bar = f"[{bar_color}]{'█' * filled}[/{bar_color}][dim]{'░' * empty}[/dim] {pct:.0f}%"
        console.print(bar)
        console.print()

        if pct >= 95:
            console.print(
                "[red bold]Storage nearly full. "
                "Run 'sfs storage prune' to free space.[/red bold]"
            )
        elif pct >= 80:
            console.print(
                "[yellow]Storage above 80%. Consider running 'sfs storage prune'.[/yellow]"
            )
    finally:
        store.close()


@storage_app.command("prune")
def prune_sessions(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be pruned without deleting."
    ),
    force: bool = typer.Option(
        False, "--force", help="Delete unsynced, bookmarked, and aliased sessions too."
    ),
) -> None:
    """Prune old sessions to free disk space."""
    store = open_store()
    try:
        sc = _load_storage_config()
        pruner = SessionPruner(store.sessions_dir, store.index.conn)
        usage = pruner.calculate_usage()
        candidates = pruner.get_prunable_sessions(sc, usage)

        if not candidates:
            console.print("[green]Nothing to prune. Storage is within policy.[/green]")
            return

        if dry_run:
            # Show what would be pruned
            table = Table(title="Would prune")
            table.add_column("Session ID", style="cyan", no_wrap=True)
            table.add_column("Tool", style="dim")
            table.add_column("Size", justify="right")
            table.add_column("Age", justify="right")
            table.add_column("Synced")
            table.add_column("Reason")

            total_size = 0
            prune_count = 0
            skip_unsynced = 0
            skip_bookmarked = 0
            skip_aliased = 0

            for s in candidates:
                if s.is_bookmarked and sc.preserve_bookmarked and not force:
                    skip_bookmarked += 1
                    continue
                if s.is_aliased and sc.preserve_aliased and not force:
                    skip_aliased += 1
                    continue
                if not s.is_synced and not force:
                    skip_unsynced += 1
                    # Still show in table with warning
                    synced_str = "[yellow]local only ⚠[/yellow]"
                    table.add_row(
                        s.session_id[:16],
                        _get_tool(store, s.session_id),
                        s.size_human,
                        _format_age(s.age_days),
                        synced_str,
                        s.reason,
                    )
                    continue

                synced_str = "[green]synced ✓[/green]" if s.is_synced else "[yellow]local only ⚠[/yellow]"
                table.add_row(
                    s.session_id[:16],
                    _get_tool(store, s.session_id),
                    s.size_human,
                    _format_age(s.age_days),
                    synced_str,
                    s.reason,
                )
                total_size += s.size_bytes
                prune_count += 1

            console.print(table)
            console.print()

            if prune_count > 0:
                console.print(
                    f"Would prune {prune_count} sessions ({_human_bytes(total_size)})."
                )
            if skip_unsynced > 0:
                console.print(
                    f"[yellow]⚠ {skip_unsynced} sessions are local-only (not synced to cloud).[/yellow]"
                )
                console.print(
                    "[yellow]  Run 'sfs sync' first to preserve them, or use --force.[/yellow]"
                )
            if skip_bookmarked > 0:
                console.print(
                    f"[dim]Skipped {skip_bookmarked} bookmarked sessions.[/dim]"
                )
            if skip_aliased > 0:
                console.print(
                    f"[dim]Skipped {skip_aliased} aliased sessions.[/dim]"
                )
            return

        # Actually prune
        result = pruner.prune(sc, dry_run=False, force=force)

        if result.pruned_count > 0:
            console.print(
                f"[green]Pruned {result.pruned_count} sessions, "
                f"freed {result.freed_bytes_human}.[/green]"
            )
        else:
            console.print("No sessions pruned.")

        if result.skipped_unsynced > 0:
            console.print(
                f"[yellow]Skipped {result.skipped_unsynced} local-only sessions "
                f"(use --force or sync first).[/yellow]"
            )
        if result.skipped_bookmarked > 0:
            console.print(
                f"[dim]Skipped {result.skipped_bookmarked} bookmarked sessions.[/dim]"
            )
        if result.skipped_aliased > 0:
            console.print(
                f"[dim]Skipped {result.skipped_aliased} aliased sessions.[/dim]"
            )
        for err in result.errors:
            err_console.print(f"[red]Error: {err}[/red]")
    finally:
        store.close()


def _get_tool(store, session_id: str) -> str:
    """Get source tool from index."""
    meta = store.get_session_metadata(session_id)
    return meta.get("source_tool", "?") if meta else "?"
