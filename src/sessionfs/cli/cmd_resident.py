"""`sfs resident …` — manage resident reviewer/implementer runners.

R1: reviewer-runner skeleton — a long-running client process that drives
a review_until_clean work queue by calling the operator's OWN LLM and
posting verdicts via the settle-path.

Commands:
- sfs resident run --queue <id> --org-profile <name>  — start the loop
"""

from __future__ import annotations

import asyncio
import logging
import sys

import typer
from rich.panel import Panel

from sessionfs.cli.common import console, err_console, handle_errors
from sessionfs.resident.config import ResidentConfig
from sessionfs.resident.runner import ResidentRunner

resident_app = typer.Typer(
    name="resident",
    help="Manage SessionFS resident runners (long-running review/implement loops).",
    no_args_is_help=True,
)


def _setup_logging(debug: bool = False) -> None:
    """Configure structured logging for the resident runner.

    Never logs the LLM key or service key. The runner's own sanitizer
    also redacts sensitive fields.
    """
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s [%(name)s] %(levelname)s %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        stream=sys.stderr,
    )
    # Keep httpx noise down unless debugging.
    if not debug:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def _resolve_config(
    config_name: str | None,
    queue_id: str | None,
    org_profile: str | None,
    project: str | None,
    poll_interval: int | None,
    resident_id: str | None = None,
    org_id: str | None = None,
    mode: str | None = None,
    worktree_path: str | None = None,
    daily_token_budget: int | None = None,
    per_wake_token_budget: int | None = None,
) -> ResidentConfig:
    """Load config from file + apply CLI overrides.

    If --config is given, load that named TOML. Otherwise build a minimal
    config from the required CLI flags (for ad-hoc / test use).
    """
    if config_name:
        cfg = ResidentConfig.from_toml(config_name)
    else:
        # Build a minimal config from CLI flags.
        cfg = ResidentConfig(name="cli")

    # CLI overrides.
    if queue_id:
        cfg.queue_id = queue_id
    if org_profile:
        cfg.org_profile = org_profile
    if project:
        cfg.project = project
    if resident_id:
        cfg.resident_id = resident_id
    if org_id:
        cfg.org_id = org_id
    if poll_interval is not None:
        cfg.poll_interval_seconds = max(10, min(300, poll_interval))
    if mode:
        cfg.mode = mode
        # R3 identity separation: when switching to implement mode, default
        # the persona to 'atlas' if it was the reviewer default.
        if mode == "implement" and cfg.persona == "codex-reviewer":
            cfg.persona = "atlas"
    if worktree_path:
        cfg.worktree_path = worktree_path
    # Pass budgets through UNCLAMPED so a NEGATIVE value is rejected by
    # validate() (not silently turned into 0 = unlimited, the opposite intent).
    if daily_token_budget is not None:
        cfg.daily_token_budget = daily_token_budget
    if per_wake_token_budget is not None:
        cfg.per_wake_token_budget = per_wake_token_budget

    # Apply the RESIDENT_LLM_API_KEY / RESIDENT_LLM_API_KEY_ENV env resolution
    # for the ad-hoc (no --config) path — from_toml already did it for the
    # config-file path. Without this, the env var would be ignored and
    # validate() would fail the missing-key check even when it is set.
    if not config_name:
        cfg.resolve_llm_key()

    errors = cfg.validate()
    if errors:
        for e in errors:
            err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)

    return cfg


@resident_app.command("run")
@handle_errors
def run_resident(
    queue: str | None = typer.Option(
        None, "--queue", "-q", help="Work queue ID to drive (or set queue_id in --config)."
    ),
    org_profile: str | None = typer.Option(
        None, "--org-profile", "-p",
        help="Named org profile for the SessionFS service key (or set org_profile in --config).",
    ),
    project: str = typer.Option(
        "", "--project", "-P",
        help="Project ID (proj_...). Service-key residents cannot use git remotes.",
    ),
    resident_id: str | None = typer.Option(
        None, "--resident-id", "-r",
        help="Registered resident id (res_...) for the memory endpoints (or set in --config).",
    ),
    org_id: str | None = typer.Option(
        None, "--org-id",
        help="Org id (org_...) the resident belongs to (or set in --config).",
    ),
    config: str | None = typer.Option(
        None, "--config", "-c", help="Named resident config TOML (~/.sessionfs/residents/<name>.toml)."
    ),
    poll_interval: int | None = typer.Option(
        None, "--poll-interval", help="Seconds between heartbeats (10-300, default 30)."
    ),
    mode: str | None = typer.Option(
        None, "--mode", "-m",
        help="Resident mode: 'review' (review_until_clean) or 'implement' "
             "(implement_until_done). Default 'review'.",
    ),
    worktree: str | None = typer.Option(
        None, "--worktree", "-w",
        help="Path to the git worktree for implement mode "
             "(required when --mode=implement).",
    ),
    daily_token_budget: int | None = typer.Option(
        None, "--daily-token-budget",
        help="Daily LLM-token ceiling (0 = unlimited). Fail-closed: the resident "
             "PARKS (no LLM work) when exhausted, resuming at UTC midnight. "
             "REQUIRES --per-wake-token-budget (the bound that keeps one call "
             "from overshooting the daily ceiling).",
    ),
    per_wake_token_budget: int | None = typer.Option(
        None, "--per-wake-token-budget",
        help="Per-wake token reserve — the resident won't start a wake it can't "
             "afford within the daily budget (0 = unlimited).",
    ),
    cold: bool = typer.Option(
        False, "--cold",
        help="Cold start: rebuild the mind from durable sources, ignoring the "
             "existing memory digest on the first wake (recovers from a poisoned "
             "or stale warm digest).",
    ),
    once: bool = typer.Option(
        False, "--once", help="Run a single heartbeat then exit (for testing)."
    ),
    debug: bool = typer.Option(
        False, "--debug", help="Enable debug logging."
    ),
) -> None:
    """Start the resident loop.

    Drives a work queue: wakes on cadence, calls the operator's OWN LLM,
    and posts results via the server's settle-path.

    In review mode: reviews tickets and posts trusted verdicts.
    In implement mode: writes code in a worktree and posts diff-refs.

    The SessionFS service key comes from the named org profile (--org-profile).
    The LLM key comes from RESIDENT_LLM_API_KEY env var or resident config TOML.
    These two credentials are single-purpose and NEVER cross.
    """
    _setup_logging(debug=debug)

    cfg = _resolve_config(
        config_name=config,
        queue_id=queue,
        org_profile=org_profile,
        project=project,
        poll_interval=poll_interval,
        resident_id=resident_id,
        org_id=org_id,
        mode=mode,
        worktree_path=worktree,
        daily_token_budget=daily_token_budget,
        per_wake_token_budget=per_wake_token_budget,
    )

    runner = ResidentRunner(cfg, cold_start=cold)

    if once:
        results = asyncio.run(runner.run_once())
        _print_results(results)
    else:
        console.print(
            Panel(
                f"[bold]Resident: {cfg.name or 'cli'}[/bold]\n"
                f"Mode:    {cfg.mode}\n"
                f"Queue:   {cfg.queue_id}\n"
                f"Project: {cfg.project}\n"
                f"Persona: {cfg.persona}\n"
                f"Profile: {cfg.org_profile}\n"
                f"Worktree:{cfg.worktree_path or ' (n/a)'}\n"
                f"Poll:    {cfg.poll_interval_seconds}s\n"
                f"LLM:     {cfg.llm.model} @ {cfg.llm.base_url}\n"
                "\n[dim]Press Ctrl+C to stop.[/dim]",
                title="Resident Runner",
                expand=False,
            )
        )
        try:
            asyncio.run(runner.run())
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped.[/dim]")


@resident_app.command("health")
@handle_errors
def resident_health(
    config: str = typer.Option(
        ..., "--config", "-c",
        help="Named resident config TOML (~/.sessionfs/residents/<name>.toml).",
    ),
) -> None:
    """Show a resident's local health: config summary + today's LLM budget spend.

    Read-only — reads the resident config and its local budget state file. It
    does not contact the server or start the loop.
    """
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    from rich.table import Table

    cfg = ResidentConfig.from_toml(config)
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Locate the budget state file the runner writes (same key + sanitization).
    budget_key = cfg.resident_id or f"{cfg.name}-{cfg.queue_id}"
    safe = "".join(c for c in budget_key if c.isalnum() or c in ("-", "_")) or "resident"
    state_path = Path.home() / ".sessionfs" / "residents" / f"{safe}-budget.json"

    spent_today: int | None = None
    budget_date: str | None = None
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("budget state is not an object")
            # REQUIRE both keys (like the runner's _load) — a state file missing
            # them is unreadable, not silently 0.
            budget_date = str(data["date"])
            spent_today = int(data["spent"])
            # A state file from a PREVIOUS UTC day is stale — the runner resets
            # spend at the date rollover, so report 0 for today.
            if budget_date != today_utc:
                spent_today = 0
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            spent_today = None  # unreadable / malformed → the runner fails closed
            budget_date = None

    table = Table(title=f"Resident health: {cfg.name}", show_header=False, expand=False)
    table.add_row("Mode", cfg.mode)
    table.add_row("Persona", cfg.persona)
    table.add_row("Queue", cfg.queue_id or "(unset)")
    table.add_row("Project", cfg.project or "(unset)")
    table.add_row("Resident id", cfg.resident_id or "(unregistered)")
    table.add_row("Poll interval", f"{cfg.poll_interval_seconds}s")
    if cfg.mode == "implement":
        table.add_row("Worktree", cfg.worktree_path or "(unset)")
        table.add_row("Base branch", cfg.base_branch)

    daily = cfg.daily_token_budget
    per_wake = cfg.per_wake_token_budget
    table.add_row("Daily token budget", str(daily) if daily else "unlimited")
    table.add_row("Per-wake token budget", str(per_wake) if per_wake else "unlimited")
    if daily:
        if spent_today is None and state_path.exists():
            spend_str = "[red]state unreadable (runner fails closed)[/red]"
        elif spent_today is None:
            spend_str = "0 (no state yet)"
        else:
            remaining = max(0, daily - spent_today)
            spend_str = f"{spent_today} / {daily}  ({remaining} remaining today)"
        table.add_row("Spent today (UTC)", spend_str)
        # Only show the state date when it's for today; a previous-day file is
        # stale (spend already reported as 0 above).
        if budget_date == today_utc:
            table.add_row("Budget date", budget_date)
    table.add_row("LLM", f"{cfg.llm.model} @ {cfg.llm.base_url}")

    console.print(table)
    console.print(
        "[dim]auto_close_review_kind on an item marks whether a HUMAN or only a "
        "resident reviewed it — never merge a 'resident_trusted' high-risk item "
        "without the merge checklist.[/dim]"
    )


def _print_results(results: list[dict]) -> None:
    """Print a summary of --once results."""
    if not results:
        console.print("[dim]No directives processed (queue idle/stopped).[/dim]")
        return

    for r in results:
        ticket = r.get("ticket_id", "?")
        verdict = r.get("verdict", "?")
        settled = "✓" if r.get("settled") else "✗"
        error = r.get("error", "")
        style = "green" if r.get("settled") else "red"
        console.print(
            f"[{style}]{settled}[/{style}] ticket={ticket} "
            f"verdict={verdict}"
        )
        if error:
            console.print(f"  [red]error: {error}[/red]")
