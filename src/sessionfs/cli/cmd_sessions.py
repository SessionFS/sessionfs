"""Session browsing commands: sfs list, sfs show."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import typer
from rich.panel import Panel
from rich.table import Table

from sessionfs.cli.common import (
    console,
    get_session_dir_or_exit,
    open_store,
    read_sfs_messages,
    resolve_session_id,
)
from sessionfs.cli.cost import estimate_cost
from sessionfs.cli.titles import (
    abbreviate_model,
    abbreviate_tool,
    extract_title,
    format_relative_time,
    format_token_count,
    sanitize_title_secrets,
)

# Default minimum messages to show (filters empty/aborted sessions)
_DEFAULT_MIN_MESSAGES = 2


def _parse_since(since_str: str) -> datetime | None:
    """Parse a 'since' string like '7d', '24h', or ISO date."""
    since_str = since_str.strip()
    now = datetime.now(timezone.utc)

    if since_str.endswith("d"):
        try:
            days = int(since_str[:-1])
            return now - timedelta(days=days)
        except ValueError:
            return None
    elif since_str.endswith("h"):
        try:
            hours = int(since_str[:-1])
            return now - timedelta(hours=hours)
        except ValueError:
            return None
    else:
        try:
            return datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        except ValueError:
            return None


def _get_display_title(session: dict[str, Any], store=None) -> str:
    """Get a clean display title for a session.

    Tries the indexed title first. If it looks like junk, re-extracts from
    messages on disk (fallback for sessions indexed before smart extraction).
    """
    raw_title = session.get("title")

    # Quick check: if title looks usable, sanitize and return
    if raw_title:
        from sessionfs.cli.titles import _is_usable_title
        if _is_usable_title(raw_title):
            return sanitize_title_secrets(raw_title)

    # Title is junk or missing — try re-extracting from messages
    if store:
        session_dir = store.get_session_dir(session["session_id"])
        if session_dir:
            messages = read_sfs_messages(session_dir)
            manifest = None
            manifest_path = session_dir / "manifest.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            return extract_title(manifest=manifest, messages=messages)

    # Last resort
    msg_count = session.get("message_count", 0)
    if msg_count > 0:
        return f"Untitled session ({msg_count} messages)"
    return "Untitled session"


_TREE_MAX_DEPTH = 5
_TREE_MAX_CHILDREN = 10


def _read_parent_id(store, session_id: str) -> str | None:
    """Return parent_session_id from a session's manifest, or None."""
    session_dir = store.get_session_dir(session_id)
    if not session_dir:
        return None
    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        data = json.loads(manifest_path.read_text())
        return data.get("parent_session_id") or data.get("_resume_parent_id")
    except (json.JSONDecodeError, OSError):
        return None


def _render_tree(sessions: list[dict[str, Any]], store) -> None:
    """Render sessions grouped by parent/child lineage."""
    # Index sessions by full id prefix lookup
    by_id: dict[str, dict[str, Any]] = {s["session_id"]: s for s in sessions}

    # Build parent -> children map using manifests
    parent_of: dict[str, str | None] = {}
    for s in sessions:
        sid = s["session_id"]
        parent_of[sid] = _read_parent_id(store, sid)

    # Determine which parents actually exist in the current filtered list
    children_of: dict[str, list[str]] = {s["session_id"]: [] for s in sessions}
    orphans: list[str] = []  # roots or sessions whose parent is not in the list

    for s in sessions:
        sid = s["session_id"]
        pid = parent_of.get(sid)
        if pid and pid in by_id:
            children_of[pid].append(sid)
        else:
            orphans.append(sid)

    # Sort roots by created_at descending
    orphans.sort(key=lambda sid: by_id[sid].get("created_at", ""), reverse=True)
    # Sort children by created_at ascending (chronological forks)
    for pid in children_of:
        children_of[pid].sort(key=lambda sid: by_id[sid].get("created_at", ""))

    def _fmt_session(s: dict[str, Any], indent_prefix: str, connector: str) -> None:
        sid = s["session_id"]
        tool_abbr = abbreviate_tool(s.get("source_tool"))
        model_abbr = abbreviate_model(s.get("model_id"))
        msgs = s.get("message_count", 0)
        title = _get_display_title(s, store)
        if len(title) > 55:
            from sessionfs.cli.titles import _truncate_at_word
            title = _truncate_at_word(title, 55)
        if indent_prefix:
            line = (
                f"[dim]{indent_prefix}{connector}[/dim] "
                f"[cyan]{sid[:8]}[/cyan]  "
                f"[dim]{tool_abbr}[/dim]  "
                f"[dim]{model_abbr}[/dim]  "
                f"{msgs}  {title}"
            )
        else:
            line = (
                f"[cyan]{sid[:8]}[/cyan]  "
                f"[dim]{tool_abbr}[/dim]  "
                f"[dim]{model_abbr}[/dim]  "
                f"{msgs}  {title}"
            )
        console.print(line)

    def _walk(sid: str, depth: int, indent: str) -> None:
        if depth > _TREE_MAX_DEPTH:
            return
        s = by_id[sid]
        kids = children_of.get(sid, [])
        # Render this node (root nodes have no connector prefix)
        if depth == 0:
            _fmt_session(s, "", "")
        # Render children
        for i, kid in enumerate(kids[:_TREE_MAX_CHILDREN]):
            is_last = (i == len(kids[:_TREE_MAX_CHILDREN]) - 1) and len(kids) <= _TREE_MAX_CHILDREN
            connector = "└─" if is_last else "├─"
            child_indent = indent + ("   " if is_last else "│  ")
            kid_s = by_id[kid]
            _fmt_session(kid_s, indent + " ", connector)
            _walk(kid, depth + 1, child_indent)
        if len(kids) > _TREE_MAX_CHILDREN:
            console.print(
                f"[dim]{indent} └─ + {len(kids) - _TREE_MAX_CHILDREN} more children[/dim]"
            )

    console.print(f"[bold]Sessions ({len(sessions)})[/bold]")
    for root_id in orphans:
        _walk(root_id, 0, "")


def list_sessions(
    tool: str | None = typer.Option(None, help="Filter by source tool."),
    since: str | None = typer.Option(None, help="Show sessions since (7d, 24h, ISO date)."),
    tag: str | None = typer.Option(None, help="Filter by tag."),
    sort: str = typer.Option("recent", help="Sort order: recent, oldest, messages, tokens."),
    output_json: bool = typer.Option(False, "--json", help="Output as JSON."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print session IDs."),
    show_all: bool = typer.Option(False, "--all", "-a", help="Show all sessions including empty."),
    min_messages: int = typer.Option(
        _DEFAULT_MIN_MESSAGES, "--min-messages",
        help="Minimum message count to display.",
    ),
    tree: bool = typer.Option(False, "--tree", help="Group sessions by lineage (parent/child)."),
) -> None:
    """List captured sessions."""
    store = open_store()
    try:
        sessions = store.list_sessions()
        total_before_filter = len(sessions)

        # Filter empty sessions (Issue 4)
        if not show_all:
            sessions = [s for s in sessions if s.get("message_count", 0) >= min_messages]

        # Filter by tool
        if tool:
            sessions = [s for s in sessions if s.get("source_tool") == tool]

        # Filter by since
        if since:
            cutoff = _parse_since(since)
            if cutoff:
                sessions = [
                    s for s in sessions
                    if s.get("created_at", "") >= cutoff.isoformat()
                ]

        # Filter by tag
        if tag:
            filtered = []
            for s in sessions:
                tags = json.loads(s.get("tags", "[]"))
                if tag in tags:
                    filtered.append(s)
            sessions = filtered

        # Sort
        if sort == "oldest":
            sessions.sort(key=lambda s: s.get("created_at", ""))
        elif sort == "messages":
            sessions.sort(key=lambda s: s.get("message_count", 0), reverse=True)
        elif sort == "tokens":
            sessions.sort(
                key=lambda s: s.get("total_input_tokens", 0) + s.get("total_output_tokens", 0),
                reverse=True,
            )
        # 'recent' is default from list_sessions (already DESC)

        if not sessions:
            console.print("[dim]No sessions found.[/dim]")
            return

        if quiet:
            for s in sessions:
                console.print(s["session_id"])
            return

        if output_json:
            console.print(json.dumps(sessions, indent=2))
            return

        if tree:
            _render_tree(sessions, store)
            hidden = total_before_filter - len(sessions)
            if hidden > 0 and not show_all:
                console.print(
                    f"[dim]Showing {len(sessions)} of {total_before_filter} sessions "
                    f"(use --all to show all)[/dim]"
                )
            return

        # Table output (Issue 3: clean formatting)
        table = Table(title=f"Sessions ({len(sessions)})", show_lines=False, expand=True)
        table.add_column("ID", style="cyan", no_wrap=True, min_width=8, max_width=8)
        table.add_column("Alias", style="magenta", no_wrap=True, max_width=16)
        table.add_column("Src", style="dim", no_wrap=True, max_width=3)
        table.add_column("Model", no_wrap=True, max_width=10)
        table.add_column("Msgs", justify="right", no_wrap=True, max_width=5)
        table.add_column("Tokens", justify="right", no_wrap=True, max_width=6)
        table.add_column("Age", no_wrap=True, max_width=8)
        table.add_column("Audit", no_wrap=True, max_width=5)
        table.add_column("Title", ratio=1)

        for s in sessions:
            total_tokens = s.get("total_input_tokens", 0) + s.get("total_output_tokens", 0)
            title = _get_display_title(s, store)
            # Truncate title at word boundary for table
            if len(title) > 60:
                from sessionfs.cli.titles import _truncate_at_word
                title = _truncate_at_word(title, 60)

            # Load audit report if it exists
            audit_display = ""
            session_dir = store.get_session_dir(s["session_id"])
            if session_dir:
                audit_path = session_dir / "audit_report.json"
                if audit_path.exists():
                    try:
                        audit_data = json.loads(audit_path.read_text())
                        trust = audit_data.get("summary", {}).get("trust_score", 0)
                        if trust >= 0.8:
                            audit_display = f"[green]{trust:.0%}[/green]"
                        elif trust >= 0.5:
                            audit_display = f"[yellow]{trust:.0%}[/yellow]"
                        else:
                            audit_display = f"[red]{trust:.0%}[/red]"
                    except (json.JSONDecodeError, OSError):
                        pass

            # Load alias from manifest if available
            alias_display = ""
            if session_dir:
                manifest_path = session_dir / "manifest.json"
                if manifest_path.exists():
                    try:
                        manifest_data = json.loads(manifest_path.read_text())
                        alias_display = manifest_data.get("alias", "") or ""
                    except (json.JSONDecodeError, OSError):
                        pass

            table.add_row(
                s["session_id"][:8],
                alias_display,
                abbreviate_tool(s.get("source_tool")),
                abbreviate_model(s.get("model_id")),
                str(s.get("message_count", 0)),
                format_token_count(total_tokens),
                format_relative_time(s.get("updated_at") or s.get("created_at")),
                audit_display,
                title,
            )

        console.print(table)

        # Footer: filter status (Issue 4)
        hidden = total_before_filter - len(sessions)
        if hidden > 0 and not show_all:
            console.print(
                f"[dim]Showing {len(sessions)} of {total_before_filter} sessions "
                f"(use --all to show all)[/dim]"
            )
    finally:
        store.close()


def show_session(
    session_id: str = typer.Argument(help="Session ID or prefix (min 4 chars)."),
    messages: bool = typer.Option(False, "--messages", "-m", help="Show messages."),
    cost: bool = typer.Option(False, "--cost", "-c", help="Show cost estimate."),
    page_size: int = typer.Option(20, "--page-size", help="Messages per page."),
) -> None:
    """Show session details."""
    store = open_store()
    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        manifest = json.loads((session_dir / "manifest.json").read_text())
        source = manifest.get("source", {})
        model = manifest.get("model", {})
        stats = manifest.get("stats", {})

        # Detail panel
        alias_val = manifest.get("alias")
        lines = [
            f"[bold]Session ID:[/bold] {manifest.get('session_id', '')}",
        ]
        if alias_val:
            lines.append(f"[bold]Alias:[/bold] {alias_val}")
        lines += [
            f"[bold]Title:[/bold] {manifest.get('title', 'Untitled')}",
            f"[bold]Tool:[/bold] {source.get('tool', '')} {source.get('tool_version', '')}",
            f"[bold]Model:[/bold] {model.get('model_id', '')} ({model.get('provider', '')})",
            f"[bold]Created:[/bold] {manifest.get('created_at', '')}",
            f"[bold]Updated:[/bold] {manifest.get('updated_at', '')}",
            "",
            f"[bold]Messages:[/bold] {stats.get('message_count', 0)}",
            f"[bold]Turns:[/bold] {stats.get('turn_count', 0)}",
            f"[bold]Tool uses:[/bold] {stats.get('tool_use_count', 0)}",
            f"[bold]Input tokens:[/bold] {stats.get('total_input_tokens', 0):,}",
            f"[bold]Output tokens:[/bold] {stats.get('total_output_tokens', 0):,}",
        ]

        if stats.get("duration_ms"):
            duration_s = stats["duration_ms"] / 1000
            if duration_s >= 3600:
                lines.append(f"[bold]Duration:[/bold] {duration_s / 3600:.1f}h")
            elif duration_s >= 60:
                lines.append(f"[bold]Duration:[/bold] {duration_s / 60:.1f}m")
            else:
                lines.append(f"[bold]Duration:[/bold] {duration_s:.0f}s")

        console.print(Panel("\n".join(lines), title="Session Details"))

        # Lineage
        parent_id = manifest.get("parent_session_id") or manifest.get("_resume_parent_id")
        if parent_id:
            parent_manifest = store.get_session_manifest(parent_id)
            if parent_manifest:
                ptool = parent_manifest.get("source", {}).get("tool", "?")
                pmsgs = parent_manifest.get("stats", {}).get("message_count", 0)
                console.print(f"[dim]Forked from:[/dim] {parent_id} ({ptool}, {pmsgs} msgs)")
            else:
                console.print(f"[dim]Forked from:[/dim] {parent_id} [dim](not found locally)[/dim]")

        # Check for children
        children = [
            s for s in store.list_sessions()
            if store.get_session_manifest(s.get("session_id", "")) and
            (store.get_session_manifest(s.get("session_id", "")) or {}).get("parent_session_id") == full_id
        ]
        if children:
            console.print(f"\n[dim]Forks ({len(children)}):[/dim]")
            for child in children[:10]:
                cid = child.get("session_id", "")
                ctool = child.get("source_tool", "?")
                cmsgs = child.get("message_count", 0)
                console.print(f"  {cid[:16]}  {ctool}  {cmsgs} msgs")
            if len(children) > 10:
                console.print(f"  [dim]+ {len(children) - 10} more[/dim]")

        # Cost estimate
        if cost and model.get("model_id"):
            cost_info = estimate_cost(
                model["model_id"],
                stats.get("total_input_tokens", 0),
                stats.get("total_output_tokens", 0),
                stats.get("cache_read_tokens", 0),
            )
            cost_lines = [
                f"[bold]Model:[/bold] {cost_info['model_id']}",
                f"[bold]Input cost:[/bold] ${cost_info['input_cost_usd']:.4f}",
                f"[bold]Output cost:[/bold] ${cost_info['output_cost_usd']:.4f}",
                f"[bold]Cache cost:[/bold] ${cost_info['cache_cost_usd']:.4f}",
                f"[bold]Total:[/bold] ${cost_info['total_cost_usd']:.4f}",
            ]
            if cost_info["cache_savings_usd"] > 0:
                cost_lines.append(
                    f"[bold]Cache savings:[/bold] ${cost_info['cache_savings_usd']:.4f}"
                )
            if not cost_info["model_matched"]:
                cost_lines.append("[dim]Model not in pricing table — costs shown as $0[/dim]")

            console.print(Panel("\n".join(cost_lines), title="Cost Estimate"))

        # Messages
        if messages:
            msg_list = read_sfs_messages(session_dir)
            main_msgs = [m for m in msg_list if not m.get("is_sidechain")]

            for i, msg in enumerate(main_msgs):
                role = msg.get("role", "unknown")
                ts = msg.get("timestamp", "")[:19]

                role_color = {
                    "user": "green",
                    "assistant": "blue",
                    "system": "yellow",
                    "tool": "dim",
                    "developer": "magenta",
                }.get(role, "white")

                content_blocks = msg.get("content", [])
                text_parts = []
                for block in content_blocks:
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "thinking":
                        text_parts.append("[thinking...]")
                    elif btype == "tool_use":
                        text_parts.append(f"[tool: {block.get('name', '')}]")
                    elif btype == "tool_result":
                        content = block.get("content", "")
                        if len(content) > 200:
                            content = content[:200] + "..."
                        text_parts.append(f"[result: {content}]")
                    elif btype == "summary":
                        text_parts.append(f"[summary: {block.get('text', '')[:100]}]")

                text = "\n".join(text_parts)
                console.print(f"[{role_color}]{role}[/{role_color}] [{ts}]")
                console.print(text)
                console.print()

                # Pagination
                if (i + 1) % page_size == 0 and i + 1 < len(main_msgs):
                    console.print(f"[dim]-- {i + 1}/{len(main_msgs)} messages shown --[/dim]")
                    try:
                        input("Press Enter for more, Ctrl+C to stop...")
                    except (KeyboardInterrupt, EOFError):
                        break
    finally:
        store.close()
