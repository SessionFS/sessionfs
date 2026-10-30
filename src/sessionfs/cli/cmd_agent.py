"""`sfs agent …` — manage tracked agent execution runs.

v0.10.2 — AgentRun layer for ephemeral agent execution tracking.

Commands:
- sfs agent run <persona>      — create + start a run, print compiled context
- sfs agent complete <run_id>  — record result and exit per fail_on policy
- sfs agent usage <run_id>     — add token/cost usage to a run in flight
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
from urllib.parse import urlencode

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
    persona: str | None = typer.Argument(None, help="Persona to run as"),
    run_id: str | None = typer.Option(
        None, "--run-id",
        help="Resume an existing queued run (created by `sfs agent replay`) "
             "instead of creating a new one.",
    ),
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
    model: str | None = typer.Option(None, "--model", help="Model this run reports it ran"),
    provider: str | None = typer.Option(None, "--provider", help="Provider of the reported model"),
    role: str | None = typer.Option(
        None, "--role",
        help="implementer / ui-implementer / reviewer / design-reviewer / "
             "security-reviewer / release",
    ),
    branch: str | None = typer.Option(None, "--branch", help="Working-tree branch for the run"),
    prompt: str | None = typer.Option(
        None, "--prompt",
        help="Edited prompt for the run (GitHub #95).",
    ),
    context_ref: list[str] = typer.Option(
        None, "--context-ref",
        help="Context reference to attach to the run (repeatable, GitHub #95).",
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
    """Create + start an agent run (or resume one via --run-id).

    `--run-id` starts an already-queued run (e.g. one created by
    `sfs agent replay`) without creating a new row. The persona and
    create-only flags are ignored in that mode — the queued row already
    carries its persona, prompt and context refs.
    """
    if persona and run_id:
        err_console.print("[red]Pass either a persona or --run-id, not both.[/red]")
        raise typer.Exit(2)
    if not persona and not run_id:
        err_console.print(
            "[red]A persona is required (or pass --run-id to resume an "
            "existing run).[/red]"
        )
        raise typer.Exit(2)

    if run_id:
        create_only = [
            name for name, given in (
                ("--ticket", ticket), ("--trigger-ref", trigger_ref),
                ("--ci-provider", ci_provider), ("--ci-run-url", ci_run_url),
                ("--fail-on", fail_on), ("--prompt", prompt),
                ("--context-ref", context_ref),
                ("--tool", tool != "generic"),
                ("--trigger-source", trigger_source != "manual"),
                ("--model", model), ("--provider", provider),
                ("--role", role), ("--branch", branch),
            ) if given
        ]
        if create_only:
            err_console.print(
                f"[red]{', '.join(create_only)} can't be combined with --run-id: "
                "the queued run already carries its settings.[/red]"
            )
            raise typer.Exit(2)
        # Resume the queued row — its fields already live on the server.
        s2, resp2 = _post(f"/api/v1/projects/{{p}}/agent-runs/{run_id}/start", None)
        if s2 >= 400 or not isinstance(resp2, dict):
            err_console.print(f"[red]Start failed ({s2}): {resp2}[/red]")
            raise typer.Exit(1)
        compiled = resp2.get("compiled_context", "")
    else:
        # Create.
        create_path = "/api/v1/projects/{p}/agent-runs"
        body: dict = {
            "persona_name": persona,
            "tool": tool,
            "trigger_source": trigger_source,
        }
        for k, v in (("ticket_id", ticket), ("trigger_ref", trigger_ref),
                     ("ci_provider", ci_provider), ("ci_run_url", ci_run_url),
                     ("fail_on", fail_on), ("model", model),
                     ("provider", provider), ("role", role), ("branch", branch)):
            if v:
                body[k] = v
        if prompt:
            body["prompt"] = prompt
        if context_ref:
            body["context_refs"] = context_ref

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
    input_tokens: int | None = typer.Option(None, "--input-tokens"),
    output_tokens: int | None = typer.Option(None, "--output-tokens"),
    cache_read_tokens: int | None = typer.Option(None, "--cache-read-tokens"),
    cache_write_tokens: int | None = typer.Option(None, "--cache-write-tokens"),
    cost_usd: float | None = typer.Option(None, "--cost-usd"),
    cost_basis: str | None = typer.Option(
        None, "--cost-basis",
        help="reported / estimated / plan / unknown",
    ),
    head_sha: str | None = typer.Option(None, "--head-sha"),
    exit_reason: str | None = typer.Option(
        None, "--exit-reason",
        help="finished / out_of_turns / crashed",
    ),
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
        # The API model accepts `findings: list[dict[str, Any]]`. Plain
        # arrays like `[1]` or `["bad"]` pass `isinstance(..., list)` but
        # the server rejects them with 422, which leaves the run stuck
        # in `running`. Catch the bad shape locally with a clear message.
        bad = next(
            (i for i, f in enumerate(findings) if not isinstance(f, dict)),
            None,
        )
        if bad is not None:
            err_console.print(
                f"[red]Findings file must be a list of objects. Element "
                f"{bad} is {type(findings[bad]).__name__}, not an object.[/red]"
            )
            raise typer.Exit(2)

    body = {
        "status": status,
        "severity": severity,
        "result_summary": summary,
        "findings": findings,
    }
    if session_id:
        body["session_id"] = session_id
    for k, v in (
        ("input_tokens", input_tokens), ("output_tokens", output_tokens),
        ("cache_read_tokens", cache_read_tokens),
        ("cache_write_tokens", cache_write_tokens),
        ("cost_usd", cost_usd), ("cost_basis", cost_basis),
        ("head_sha", head_sha), ("exit_reason", exit_reason),
    ):
        if v is not None:
            body[k] = v

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


@agent_app.command("usage")
@handle_errors
def usage_agent(
    run_id: str = typer.Argument(...),
    input_tokens: int | None = typer.Option(None, "--input-tokens"),
    output_tokens: int | None = typer.Option(None, "--output-tokens"),
    cache_read_tokens: int | None = typer.Option(None, "--cache-read-tokens"),
    cache_write_tokens: int | None = typer.Option(None, "--cache-write-tokens"),
    cost_usd: float | None = typer.Option(None, "--cost-usd"),
) -> None:
    """Add token/cost usage to a queued or running run (additive).

    Usage can be reported incrementally while the run is still in flight;
    once the run completes, its usage is fixed and further reports are
    rejected with 409.
    """
    body: dict = {}
    for k, v in (
        ("input_tokens", input_tokens), ("output_tokens", output_tokens),
        ("cache_read_tokens", cache_read_tokens),
        ("cache_write_tokens", cache_write_tokens),
        ("cost_usd", cost_usd),
    ):
        if v is not None:
            body[k] = v
    if not body:
        err_console.print(
            "[red]Pass at least one usage field "
            "(--input-tokens / --output-tokens / --cache-read-tokens / "
            "--cache-write-tokens / --cost-usd).[/red]"
        )
        raise typer.Exit(2)

    s, resp = _post(f"/api/v1/projects/{{p}}/agent-runs/{run_id}/usage", body)
    if s == 409:
        err_console.print(
            f"[red]Run {run_id} is complete — usage can't be changed.[/red]"
        )
        raise typer.Exit(1)
    if s >= 400 or not isinstance(resp, dict):
        err_console.print(f"[red]Usage report failed ({s}): {resp}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[green]Recorded usage for {run_id}.[/green] — "
        f"tokens={resp.get('input_tokens') or 0}/{resp.get('output_tokens') or 0}"
        + (f", cost={resp.get('cost_usd')}" if resp.get("cost_usd") is not None else "")
    )


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


@agent_app.command("replay")
@handle_errors
def replay_agent(
    run_id: str = typer.Argument(..., help="Run to replay"),
    prompt: str | None = typer.Option(
        None, "--prompt", help="Edited prompt for the replayed run",
    ),
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", help="Read the edited prompt from a file",
    ),
    ticket: str | None = typer.Option(
        None, "--ticket", help="Override the ticket id",
    ),
    persona: str | None = typer.Option(
        None, "--persona", help="Override the persona",
    ),
    context_ref: list[str] = typer.Option(
        None, "--context-ref", help="Context reference to attach (repeatable)",
    ),
    output_id: bool = typer.Option(
        False, "--output-id",
        help="Print ONLY the new run id to stdout (CI-safe).",
    ),
) -> None:
    """Replay a run with an edited prompt/ticket/context (GitHub #95).

    Unspecified fields copy from the parent run. Prints the new run id,
    the equivalent `sfs agent run` command, and a CI hint block.
    """
    if prompt is not None and prompt_file is not None:
        err_console.print("[red]Pass --prompt or --prompt-file, not both.[/red]")
        raise typer.Exit(2)
    if prompt_file is not None:
        try:
            prompt = prompt_file.read_text()
        except OSError as exc:
            err_console.print(f"[red]Could not read prompt file: {exc}[/red]")
            raise typer.Exit(2)

    body: dict = {}
    if prompt is not None:
        body["prompt"] = prompt
    if ticket:
        body["ticket_id"] = ticket
    if persona:
        body["persona_name"] = persona
    if context_ref:
        body["context_refs"] = context_ref

    s, resp = _post(f"/api/v1/projects/{{p}}/agent-runs/{run_id}/replay", body)
    if s in (404, 405):
        # Servers without the replay route answer 405 (another route owns the
        # path) or a bare "Not Found"; a missing run says "Agent run not found".
        message = ""
        if isinstance(resp, dict):
            err = resp.get("error")
            message = str(err.get("message", "")) if isinstance(err, dict) else str(resp.get("detail", ""))
        if s == 405 or message == "Not Found":
            err_console.print(
                "[red]This server doesn't support replay (upgrade the server).[/red]"
            )
        else:
            err_console.print(f"[red]Run {run_id} not found.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(resp, dict):
        err_console.print(f"[red]Replay failed ({s}): {resp}[/red]")
        raise typer.Exit(1)

    new_run = resp["run"]
    if output_id:
        print(new_run["id"])
        return

    console.print(f"[green]Replayed {run_id} as {new_run['id']}.[/green]")
    console.print("[dim]Equivalent command:[/dim]")
    console.print(resp["command"])
    console.print("[dim]CI hint:[/dim]")
    console.print(resp["ci_hint"])


@agent_app.command("compare")
@handle_errors
def compare_agents(
    a: str = typer.Argument(..., help="Baseline run id"),
    b: str = typer.Argument(..., help="Run to compare against the baseline"),
    format_: str = typer.Option("text", "--format", help="text / json"),
) -> None:
    """Diff the findings of two agent runs (GitHub #95)."""
    s, resp = _get(f"/api/v1/projects/{{p}}/agent-runs/{a}/compare/{b}")
    if s == 404:
        err_console.print("[red]One of the runs was not found.[/red]")
        raise typer.Exit(1)
    if s >= 400 or not isinstance(resp, dict):
        err_console.print(f"[red]API error ({s}): {resp}[/red]")
        raise typer.Exit(1)

    if format_ == "json":
        sys.stdout.write(_json.dumps(resp, indent=2, default=str) + "\n")
        return
    _render_compare(resp, console)


def _render_compare(resp: dict, stream) -> None:
    stream.print(
        f"[bold]{resp['a_id']}[/bold] → [bold]{resp['b_id']}[/bold]  "
        f"findings {resp['a_findings_count']} → {resp['b_findings_count']}"
    )
    stream.print(
        f"added={len(resp['added'])}  removed={len(resp['removed'])}  "
        f"unchanged={len(resp['unchanged'])}"
    )
    for label, key, color in (
        ("Added", "added", "green"),
        ("Removed", "removed", "red"),
        ("Unchanged", "unchanged", "dim"),
    ):
        items = resp.get(key, [])
        if not items:
            continue
        stream.print(f"[{color}]{label} ({len(items)}):[/{color}]")
        for f in items:
            title = f.get("title") or f.get("rule") or "(no title)"
            sev = f.get("severity") or "none"
            loc = str(f.get("file") or "")
            if f.get("line") is not None:
                loc += f":{f['line']}"
            stream.print(
                f"  - [{sev}] {title}" + (f"  ({loc})" if loc else "")
            )


@agent_app.command("spend")
@handle_errors
def spend_agent(
    ticket: str | None = typer.Option(None, "--ticket", help="Ticket id to summarize"),
    project: bool = typer.Option(False, "--project", help="Summarize the current project"),
    group_by: str | None = typer.Option(
        None, "--group-by", help="persona / model / ticket / role / day",
    ),
    from_: str | None = typer.Option(
        None, "--from", help="Start of the window (ISO datetime)",
    ),
    to_: str | None = typer.Option(
        None, "--to", help="End of the window (ISO datetime)",
    ),
    format_: str = typer.Option("text", "--format", help="text / json"),
) -> None:
    """Summarize agent-run spend for a ticket or the current project.

    Dollar amounts only appear when the caller has admin standing on the
    project (org owners/admins or the owner of a personal project);
    token totals are always shown.
    """
    if ticket and project:
        err_console.print("[red]Pass --ticket or --project, not both.[/red]")
        raise typer.Exit(2)

    api_url, api_key, project_id = _resolve_project()
    if ticket:
        path = f"/api/v1/projects/{project_id}/tickets/{ticket}/spend"
        params: dict | None = None
    else:
        path = f"/api/v1/projects/{project_id}/spend"
        params = {}
        if group_by:
            params["group_by"] = group_by
        if from_:
            params["from"] = from_
        if to_:
            params["to"] = to_

    s, resp = asyncio.run(
        _api_request("GET", path, api_url, api_key, params=params)
    )[:2]
    if s == 404:
        err_console.print(
            f"[red]{'Ticket' if ticket else 'Project'} not found.[/red]"
        )
        raise typer.Exit(1)
    if s >= 400 or not isinstance(resp, dict):
        err_console.print(f"[red]API error ({s}): {resp}[/red]")
        raise typer.Exit(1)

    if format_ == "json":
        sys.stdout.write(_json.dumps(resp, indent=2, default=str) + "\n")
        return
    _render_spend(resp, console)


def _render_spend(resp: dict, stream) -> None:
    totals = resp.get("totals", {})
    stream.print(
        f"[bold]{resp.get('ticket_id') or resp.get('project_id')}[/bold] — "
        f"{totals.get('runs', 0)} runs, "
        f"{totals.get('input_tokens', 0)} in / "
        f"{totals.get('output_tokens', 0)} out tokens"
        + (f", ${totals['cost_usd']:.2f}" if "cost_usd" in totals else "")
    )

    if "review_rounds" in resp:
        stream.print(f"Review rounds: {resp['review_rounds']}")

    for label, key in (("Per persona", "per_persona"), ("Per model", "per_model")):
        groups = resp.get(key)
        if not groups:
            continue
        table = Table()
        table.add_column(label, style="cyan")
        table.add_column("Runs", justify="right")
        table.add_column("In", justify="right")
        table.add_column("Out", justify="right")
        if any("cost_usd" in g for g in groups):
            table.add_column("Cost", justify="right")
        for g in groups:
            row = [
                str(g.get("key") or "-"),
                str(g.get("runs", 0)),
                str(g.get("input_tokens", 0)),
                str(g.get("output_tokens", 0)),
            ]
            if "cost_usd" in g:
                row.append(f"${g['cost_usd']:.2f}")
            table.add_row(*row)
        stream.print(table)

    groups = resp.get("groups")
    if groups:
        stream.print(f"[bold]By {resp.get('group_by') or 'group'}[/bold]")
        table = Table()
        table.add_column("Key", style="cyan")
        table.add_column("Runs", justify="right")
        table.add_column("In", justify="right")
        table.add_column("Out", justify="right")
        if any("cost_usd" in g for g in groups):
            table.add_column("Cost", justify="right")
        for g in groups:
            row = [
                str(g.get("key") or "-"),
                str(g.get("runs", 0)),
                str(g.get("input_tokens", 0)),
                str(g.get("output_tokens", 0)),
            ]
            if "cost_usd" in g:
                row.append(f"${g['cost_usd']:.2f}")
            table.add_row(*row)
        stream.print(table)


# ── `sfs agent feedback …` — structured feedback/outcome records ──
#
# v0.17.0. A feedback record points at least one target (agent run,
# ticket, or ticket comment) within a single project, classified by
# feedback_type × verdict × optional label. Append-only; `sfs agent
# feedback summary` is pure SQL group-by, no LLM.


feedback_app = typer.Typer(
    name="feedback",
    help="Structured feedback/outcome records on agent work (Team+).",
    no_args_is_help=True,
)

agent_app.add_typer(feedback_app, name="feedback")


def _feedback_params(locals_: dict, keys: tuple[str, ...]) -> str:
    """Build a URL query suffix from non-empty option values."""
    params: list[tuple[str, str]] = []
    for key in keys:
        val = locals_.get(key)
        if isinstance(val, str) and val.strip():
            params.append((key, val.strip()))
    return ("?" + urlencode(params)) if params else ""


@feedback_app.command("add")
@handle_errors
def add_feedback(
    feedback_type: str = typer.Option(
        ..., "--type",
        help="route / summary / actionability / duplicate / resolution",
    ),
    verdict: str = typer.Option(
        ..., "--verdict", help="positive / negative / neutral",
    ),
    label: str | None = typer.Option(None, "--label"),
    final_owner: str | None = typer.Option(None, "--final-owner"),
    notes: str | None = typer.Option(None, "--notes"),
    agent_run_id: str | None = typer.Option(None, "--agent-run"),
    ticket_id: str | None = typer.Option(None, "--ticket"),
    comment_id: str | None = typer.Option(None, "--comment"),
    format_: str = typer.Option("text", "--format", help="text / json"),
) -> None:
    """Record a structured feedback/outcome note on agent work."""
    body: dict = {
        "feedback_type": feedback_type.strip(),
        "verdict": verdict.strip(),
    }
    for k, v in (
        ("label", label), ("final_owner", final_owner), ("notes", notes),
        ("agent_run_id", agent_run_id), ("ticket_id", ticket_id),
        ("comment_id", comment_id),
    ):
        if v and v.strip():
            body[k] = v.strip()

    s, resp = _post("/api/v1/projects/{p}/agent-feedback", body)
    if s >= 400 or not isinstance(resp, dict):
        err_console.print(f"[red]Create failed ({s}): {resp}[/red]")
        raise typer.Exit(1)
    if format_ == "json":
        sys.stdout.write(_json.dumps(resp, indent=2, default=str) + "\n")
        return
    console.print(
        f"[green]Recorded {resp['id']}[/green] — "
        f"{resp['feedback_type']} / {resp['verdict']}"
        + (f" / {resp['label']}" if resp.get("label") else "")
    )


@feedback_app.command("list")
@handle_errors
def list_feedback(
    agent_run_id: str | None = typer.Option(None, "--agent-run"),
    ticket_id: str | None = typer.Option(None, "--ticket"),
    feedback_type: str | None = typer.Option(None, "--type"),
    verdict: str | None = typer.Option(None, "--verdict"),
    label: str | None = typer.Option(None, "--label"),
    persona: str | None = typer.Option(None, "--persona"),
    limit: int = typer.Option(100, "--limit"),
    format_: str = typer.Option("text", "--format", help="text / json"),
) -> None:
    """List feedback records, oldest-first with filters."""
    suffix = _feedback_params(
        locals(),
        ("agent_run_id", "ticket_id", "feedback_type", "verdict",
         "label", "persona"),
    )
    if suffix:
        suffix += f"{'&' if '?' in suffix else '?'}limit={limit}"
    else:
        suffix = f"?limit={limit}"
    s, resp = _get(f"/api/v1/projects/{{p}}/agent-feedback{suffix}")
    if s >= 400 or not isinstance(resp, list):
        err_console.print(f"[red]API error ({s}): {resp}[/red]")
        raise typer.Exit(1)
    if format_ == "json":
        sys.stdout.write(_json.dumps(resp, indent=2, default=str) + "\n")
        return
    if not resp:
        console.print("[dim]No feedback records match.[/dim]")
        return
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Type")
    table.add_column("Verdict")
    table.add_column("Label", style="dim")
    table.add_column("Source")
    table.add_column("Created", style="dim")
    for r in resp:
        table.add_row(
            r["id"],
            r["feedback_type"],
            r["verdict"],
            r.get("label") or "-",
            r.get("source") or "-",
            r.get("created_at", "")[:19],
        )
    console.print(table)


@feedback_app.command("summary")
@handle_errors
def summary_feedback(
    persona: str | None = typer.Option(None, "--persona"),
    since: str | None = typer.Option(None, "--since"),
    format_: str = typer.Option("text", "--format", help="text / json"),
) -> None:
    """Grouped counts by feedback_type × verdict × label (no LLM)."""
    suffix = _feedback_params(locals(), ("persona", "since"))
    s, resp = _get(f"/api/v1/projects/{{p}}/agent-feedback/summary{suffix}")
    if s >= 400 or not isinstance(resp, dict):
        err_console.print(f"[red]API error ({s}): {resp}[/red]")
        raise typer.Exit(1)
    if format_ == "json":
        sys.stdout.write(_json.dumps(resp, indent=2, default=str) + "\n")
        return
    groups = resp.get("groups", [])
    if not groups:
        console.print("[dim]No feedback records yet.[/dim]")
        return
    table = Table()
    table.add_column("Type")
    table.add_column("Verdict")
    table.add_column("Label", style="dim")
    table.add_column("Count", justify="right")
    for g in groups:
        table.add_row(
            g["feedback_type"],
            g["verdict"],
            g.get("label") or "-",
            str(g["count"]),
        )
    table.title = f"Agent feedback — {resp.get('total', 0)} total"
    console.print(table)
