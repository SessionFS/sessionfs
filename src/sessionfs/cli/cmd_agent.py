"""`sfs agent …` — manage tracked agent execution runs.

v0.10.2 — AgentRun layer for ephemeral agent execution tracking.

Commands:
- sfs agent run <persona>      — create + start a run, print compiled context
- sfs agent complete <run_id>  — record result and exit per fail_on policy
- sfs agent status <run_id>    — print run detail (text / json / markdown)
- sfs agent list               — list recent runs with filters

CI-safe output modes:
- `sfs agent run --output-id` prints exactly the run id (no decorations).
- `sfs agent run --context-file PATH` writes compiled context to a file
  so stdout can carry the run id only.
- `sfs agent status --format markdown` emits GitHub/GitLab step-summary-
  compatible markdown for `>> $GITHUB_STEP_SUMMARY` and equivalents.

This tracks an execution — it does NOT spawn a model runtime. The
caller is responsible for executing the actual agent work and feeding
the compiled persona+ticket context into its own runtime.
"""

from __future__ import annotations

import asyncio
import json as _json
import sys
import time
from pathlib import Path
from typing import Any

import typer
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from sessionfs.cli.cmd_rules import (
    _api_request,
    _get_api_config,
    _get_git_remote,
    _normalize_remote,
    _resolve_project_id,
)
from sessionfs.cli.common import console, err_console, handle_errors

agent_app = typer.Typer(
    name="agent",
    help="Track ephemeral agent execution runs (Team+).",
    no_args_is_help=True,
)


_TERMINAL_STATUSES = {"passed", "failed", "errored", "cancelled"}


def _resolve_project() -> tuple[str, str, str]:
    remote = _get_git_remote()
    if not remote:
        err_console.print(
            "[red]No git remote found.[/red] "
            "Run inside a git repo with an `origin` remote."
        )
        raise typer.Exit(1)
    normalized = _normalize_remote(remote)
    if not normalized:
        err_console.print(f"[red]Could not normalize remote: {remote}[/red]")
        raise typer.Exit(1)
    api_url, api_key = _get_api_config()
    project_id = _resolve_project_id(api_url, api_key, normalized)
    return api_url, api_key, project_id


def _post(path: str, json_data: dict | None = None) -> tuple[int, dict | list | str]:
    api_url, api_key, project_id = _resolve_project()
    return asyncio.run(
        _api_request("POST", path.replace("{p}", project_id), api_url, api_key, json_data=json_data)
    )[:2]


def _get(path: str) -> tuple[int, dict | list | str]:
    api_url, api_key, project_id = _resolve_project()
    return asyncio.run(
        _api_request("GET", path.replace("{p}", project_id), api_url, api_key)
    )[:2]


@agent_app.command("run")
@handle_errors
def run_agent(
    persona: str = typer.Argument(..., help="Persona to run as"),
    ticket: str | None = typer.Option(None, "--ticket", help="Ticket id to scope the run"),
    tool: str = typer.Option("generic", "--tool", help="Token budget hint"),
    trigger_source: str = typer.Option(
        "manual", "--trigger-source",
        help="manual / ci / webhook / scheduled / mcp / api",
    ),
    trigger_ref: str | None = typer.Option(None, "--trigger-ref"),
    ci_provider: str | None = typer.Option(None, "--ci-provider"),
    ci_run_url: str | None = typer.Option(None, "--ci-run-url"),
    fail_on: str | None = typer.Option(
        None, "--fail-on",
        help="Severity threshold (low/medium/high/critical) — exit non-zero "
             "at `complete` time when met.",
    ),
    timeout: int = typer.Option(
        0, "--timeout",
        help="Seconds to poll for completion. 0 = return after start.",
    ),
    output_id: bool = typer.Option(
        False, "--output-id",
        help="Print ONLY the run id to stdout (suppresses compiled context). "
             "CI-safe — use `$(sfs agent run ... --output-id)` to capture.",
    ),
    context_file: Path | None = typer.Option(
        None, "--context-file",
        help="Write compiled context to this file instead of printing it.",
    ),
    format_: str = typer.Option(
        "text", "--format",
        help="Status output format when polling: text / json / markdown.",
    ),
) -> None:
    """Create + start an agent run, return run id + compiled context."""
    # Create.
    create_path = "/api/v1/projects/{p}/agent-runs"
    body: dict = {
        "persona_name": persona,
        "tool": tool,
        "trigger_source": trigger_source,
    }
    for k, v in (("ticket_id", ticket), ("trigger_ref", trigger_ref),
                 ("ci_provider", ci_provider), ("ci_run_url", ci_run_url),
                 ("fail_on", fail_on)):
        if v:
            body[k] = v

    s, resp = _post(create_path, body)
    if s >= 400 or not isinstance(resp, dict):
        err_console.print(f"[red]Create failed ({s}): {resp}[/red]")
        raise typer.Exit(1)
    run_id = resp["id"]

    # Start.
    s2, resp2 = _post(f"/api/v1/projects/{{p}}/agent-runs/{run_id}/start", None)
    if s2 >= 400 or not isinstance(resp2, dict):
        err_console.print(f"[red]Start failed ({s2}): {resp2}[/red]")
        raise typer.Exit(1)
    compiled = resp2.get("compiled_context", "")

    # Output modes.
    if output_id:
        # Machine-safe: stdout = run_id, nothing else. Context goes to
        # --context-file (if set) or stderr (so callers can capture
        # both without mixing).
        print(run_id)
        if context_file:
            context_file.write_text(compiled)
        elif compiled:
            err_console.print(compiled)
    else:
        console.print(f"[green]Started {run_id}.[/green]")
        if context_file:
            context_file.write_text(compiled)
            console.print(f"[dim]Context written to {context_file}[/dim]")
        elif compiled:
            console.print(Markdown(compiled))

    # Optional polling.
    if timeout > 0:
        deadline = time.time() + timeout
        r3: dict[str, Any] | list[Any] | str = {}
        while time.time() < deadline:
            time.sleep(2)
            s3, r3 = _get(f"/api/v1/projects/{{p}}/agent-runs/{run_id}")
            if s3 == 200 and isinstance(r3, dict) and r3.get("status") in _TERMINAL_STATUSES:
                # KB review post-Round 1 MEDIUM: in --output-id mode,
                # ALL polling output must go to stderr — including json
                # and markdown, which `_render_status` writes to stdout.
                # Override the format to 'text' (stderr-safe) when
                # output_id is set so command substitution captures only
                # the run id we printed earlier.
                effective_format = "text" if output_id else format_
                stream = err_console if output_id else console
                _render_status(r3, effective_format, stream)
                # Match the post-Round 1 enforcement contract: failed
                # and errored statuses exit non-zero even when
                # exit_code is 0 (pre-fix rows or upstream tooling bugs).
                exit_code = r3.get("exit_code") or 0
                if exit_code:
                    raise typer.Exit(exit_code)
                if r3.get("status") in {"failed", "errored"}:
                    raise typer.Exit(1)
                return
        # Timed out — DO NOT mark the run failed; just exit 0 (run still
        # running externally). Caller can poll status later.
        last_status = (
            str(r3.get("status", "?")) if isinstance(r3, dict) else "?"
        )
        msg = f"[yellow]Run {run_id} still in {last_status} after {timeout}s — exiting without marking failed.[/yellow]"
        err_console.print(msg)


@agent_app.command("complete")
@handle_errors
def complete_agent(
    run_id: str = typer.Argument(...),
    summary: str = typer.Option(..., "--summary", "-s"),
    severity: str = typer.Option(
        "none", "--severity",
        help="none / low / medium / high / critical",
    ),
    findings_file: Path | None = typer.Option(
        None, "--findings-file",
        help="Path to a JSON file with a list of findings objects.",
    ),
    status: str = typer.Option(
        "passed", "--status",
        help="passed / failed / errored. Policy may flip passed→failed.",
    ),
    session_id: str | None = typer.Option(None, "--session-id"),
    enforce: bool = typer.Option(
        False, "--enforce",
        help="Exit with the stored exit_code (0 pass, 1 fail) for CI gating.",
    ),
) -> None:
    """Record run result. With --enforce, CLI exits per stored exit_code."""
    findings: list = []
    if findings_file:
        try:
            findings = _json.loads(findings_file.read_text())
        except (OSError, _json.JSONDecodeError) as exc:
            err_console.print(f"[red]Could not read findings file: {exc}[/red]")
            raise typer.Exit(2)
        if not isinstance(findings, list):
            err_console.print("[red]Findings file must contain a JSON list.[/red]")
            raise typer.Exit(2)

    body = {
        "status": status,
        "severity": severity,
        "result_summary": summary,
        "findings": findings,
    }
    if session_id:
        body["session_id"] = session_id

    s, resp = _post(f"/api/v1/projects/{{p}}/agent-runs/{run_id}/complete", body)
    if s == 409:
        err_console.print(f"[red]Run {run_id} is already in a terminal state.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(resp, dict):
        err_console.print(f"[red]Complete failed ({s}): {resp}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[green]Completed {run_id}[/green] — status={resp['status']}, "
        f"policy={resp['policy_result']}, exit_code={resp['exit_code']}"
    )
    # Defense-in-depth: --enforce exits non-zero for ANY non-passed
    # terminal status, even if the stored exit_code is 0 (which would
    # only happen on a pre-fix row). The server now forces exit_code=1
    # for failed/errored, but the CLI gate stays robust regardless.
    if enforce:
        if resp.get("exit_code"):
            raise typer.Exit(resp["exit_code"])
        if resp.get("status") in {"failed", "errored"}:
            raise typer.Exit(1)


@agent_app.command("status")
@handle_errors
def status_agent(
    run_id: str = typer.Argument(...),
    format_: str = typer.Option("text", "--format", help="text / json / markdown"),
) -> None:
    """Show run detail (CI-safe markdown for step summaries)."""
    s, resp = _get(f"/api/v1/projects/{{p}}/agent-runs/{run_id}")
    if s == 404:
        err_console.print(f"[red]Run {run_id} not found.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(resp, dict):
        err_console.print(f"[red]API error ({s}): {resp}[/red]")
        raise typer.Exit(1)
    _render_status(resp, format_, console)


def _render_status(row: dict, fmt: str, stream) -> None:
    fmt = (fmt or "text").lower()
    if fmt == "json":
        # Stream JSON to raw stdout (skip Rich) so machines can parse it.
        sys.stdout.write(_json.dumps(row, indent=2, default=str))
        sys.stdout.write("\n")
        return
    if fmt == "markdown":
        # GitHub/GitLab step-summary-compatible markdown.
        out = []
        out.append(f"### AgentRun `{row['id']}`")
        out.append("")
        out.append(f"- **Persona:** {row['persona_name']}")
        out.append(f"- **Status:** `{row['status']}`")
        if row.get("severity"):
            out.append(f"- **Severity:** {row['severity']}")
        out.append(f"- **Findings:** {row['findings_count']}")
        if row.get("policy_result"):
            out.append(f"- **Policy:** {row['policy_result']} (exit {row['exit_code']})")
        if row.get("result_summary"):
            out.append("")
            out.append("**Summary:**")
            out.append("")
            out.append(row["result_summary"])
        # Plain print so the markdown is literal (not Rich-rendered).
        print("\n".join(out))
        return
    # Text (Rich panel).
    stream.print(Panel(
        f"[bold]{row['id']}[/bold]  "
        f"persona={row['persona_name']}  "
        f"status={row['status']}\n"
        f"severity={row.get('severity') or '-'}  "
        f"findings={row['findings_count']}  "
        f"policy={row.get('policy_result') or '-'}  "
        f"exit={row.get('exit_code') if row.get('exit_code') is not None else '-'}\n\n"
        f"{row.get('result_summary') or '[dim]no summary[/dim]'}",
        title="AgentRun",
        expand=False,
    ))


@agent_app.command("list")
@handle_errors
def list_agents(
    persona: str | None = typer.Option(None, "--persona"),
    status: str | None = typer.Option(None, "--status"),
    trigger_source: str | None = typer.Option(None, "--trigger-source"),
    ticket: str | None = typer.Option(None, "--ticket"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List recent runs with filters."""
    params = []
    if persona:
        params.append(f"persona_name={persona}")
    if status:
        params.append(f"status={status}")
    if trigger_source:
        params.append(f"trigger_source={trigger_source}")
    if ticket:
        params.append(f"ticket_id={ticket}")
    params.append(f"limit={limit}")
    suffix = "?" + "&".join(params) if params else ""
    s, resp = _get(f"/api/v1/projects/{{p}}/agent-runs{suffix}")
    if s >= 400 or not isinstance(resp, list):
        err_console.print(f"[red]API error ({s}): {resp}[/red]")
        raise typer.Exit(1)
    if not resp:
        console.print("[dim]No runs match.[/dim]")
        return
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Persona")
    table.add_column("Status")
    table.add_column("Severity", style="dim")
    table.add_column("Findings")
    table.add_column("Policy")
    table.add_column("Created", style="dim")
    for r in resp:
        table.add_row(
            r["id"],
            r["persona_name"],
            r["status"],
            r.get("severity") or "-",
            str(r["findings_count"]),
            r.get("policy_result") or "-",
            r.get("created_at", "")[:19],
        )
    console.print(table)
