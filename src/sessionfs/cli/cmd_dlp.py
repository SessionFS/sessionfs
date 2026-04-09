"""DLP scanning and policy management commands: sfs dlp scan, sfs dlp policy."""

from __future__ import annotations

from typing import Optional

import typer
from rich.table import Table

from sessionfs.cli.common import (
    console,
    err_console,
    open_store,
    resolve_session_id,
    get_session_dir_or_exit,
)

dlp_app = typer.Typer(name="dlp", help="DLP scanning and policy management.", no_args_is_help=True)


@dlp_app.command("scan")
def dlp_scan(
    session_id: str = typer.Argument(help="Session ID or prefix to scan."),
    categories: Optional[str] = typer.Option(
        None,
        "--categories",
        "-c",
        help="Comma-separated categories: secrets,phi. Default: secrets.",
    ),
) -> None:
    """Scan a local session for secrets and PHI."""
    from sessionfs.security.secrets import scan_dlp, DLPFinding

    store = open_store()
    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        # Read messages.jsonl
        messages_path = session_dir / "messages.jsonl"
        if not messages_path.is_file():
            err_console.print("[red]No messages.jsonl found in session.[/red]")
            raise SystemExit(1)

        text = messages_path.read_text(encoding="utf-8", errors="replace")

        # Parse categories
        cats = ["secrets"]
        if categories:
            cats = [c.strip() for c in categories.split(",")]

        findings: list[DLPFinding] = scan_dlp(text, categories=cats)

        if not findings:
            console.print(f"[green]No DLP findings in session {full_id[:12]}.[/green]")
            return

        # Display findings
        table = Table(title=f"DLP Findings ({len(findings)})")
        table.add_column("Line", justify="right", style="dim")
        table.add_column("Pattern", style="cyan")
        table.add_column("Category")
        table.add_column("Severity")

        severity_styles = {
            "critical": "[bold red]",
            "high": "[red]",
            "medium": "[yellow]",
            "low": "[dim]",
        }

        for f in findings:
            style = severity_styles.get(f.severity, "")
            end_style = style.replace("[", "[/") if style else ""
            table.add_row(
                str(f.line_number),
                f.pattern_name,
                f.category,
                f"{style}{f.severity}{end_style}",
            )

        console.print(table)

        # Summary
        by_severity: dict[str, int] = {}
        for f in findings:
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        parts = [f"{count} {sev}" for sev, count in sorted(by_severity.items())]
        console.print(f"\nTotal: {len(findings)} finding(s) ({', '.join(parts)})")

    finally:
        store.close()


@dlp_app.command("policy")
def dlp_policy() -> None:
    """View the organization's DLP policy."""
    import httpx

    from sessionfs.cli.cmd_cloud import _load_sync_config

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    try:
        resp = httpx.get(
            f"{cfg['api_url']}/api/v1/dlp/policy",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=15.0,
        )
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {cfg['api_url']}[/red]")
        raise SystemExit(1)

    if resp.status_code == 403:
        err_console.print(
            "[yellow]DLP policy requires a Pro tier or higher, "
            "and organization membership.[/yellow]"
        )
        raise SystemExit(1)

    if resp.status_code != 200:
        err_console.print(f"[red]Failed to fetch org settings: {resp.text}[/red]")
        raise SystemExit(1)

    dlp = resp.json()

    if not dlp or not dlp.get("enabled"):
        console.print("[dim]DLP is not enabled for your organization.[/dim]")
        console.print(
            "\nTo enable, ask your org admin to configure DLP in the dashboard "
            "or via the API."
        )
        return

    console.print("[bold]DLP Policy[/bold]")
    console.print("  Enabled:    [green]yes[/green]")
    console.print(f"  Mode:       {dlp.get('mode', 'warn')}")
    console.print(f"  Categories: {', '.join(dlp.get('categories', ['secrets']))}")

    custom = dlp.get("custom_patterns", [])
    if custom:
        console.print(f"  Custom patterns: {len(custom)}")
        for cp in custom:
            console.print(f"    - {cp.get('name', '?')} ({cp.get('severity', '?')})")

    allowlist = dlp.get("allowlist", [])
    if allowlist:
        console.print(f"  Allowlist entries: {len(allowlist)}")
