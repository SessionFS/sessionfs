"""`sfs workflow …` — record-only multi-agent workflow templates (GitHub #120).

SessionFS never starts an agent. A template is a small DAG of steps; starting
one creates a review loop with one child ticket per step slot. Your own
orchestrator reads the plan, launches agents for the ready steps and reports
through `sfs loop run start|finish --step` and `sfs loop review --step`.

Commands:
- sfs workflow list [--org ORG]                   — active templates
- sfs workflow show <template_id> [--org ORG]     — one version with its steps
- sfs workflow create <file> [--org ORG]          — new template from YAML/JSON
- sfs workflow update <template_id> <file>        — new version of a template
- sfs workflow disable <template_id> [--org ORG]  — deactivate a version
- sfs workflow start <template_id> --ticket T | --title T [--output-id]
- sfs workflow plan <loop_id>                     — ready steps, agents, briefs

Template files are YAML (a strict ``yaml.SafeLoader``) or JSON with ``name``
and either ``steps`` at the top level or a ``definition`` mapping. They are
validated locally with the same model the server uses before any request is
sent. YAML that another reader (the dashboard editor) could read differently
is refused: anchors, aliases, tags, block indentation indicators,
more-indented folded lines, and plain scalars YAML 1.1 resolves to anything
but a string, a plain integer, true/false or null (yes/no/on/off/y/n,
True/FALSE, Null, 1.5, 0x1f, 012, dates...). Quote such values.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import typer
from rich.panel import Panel
from rich.table import Table

from sessionfs.cli.cmd_loop import _resolve_project
from sessionfs.cli.cmd_rules import _api_request, _get_api_config
from sessionfs.cli.common import console, err_console, format_api_error, handle_errors
from sessionfs.url_path_safety import _path_segment
from sessionfs.workflow_definition import WorkflowDefinitionError, parse_definition

workflow_app = typer.Typer(
    name="workflow",
    help="Record-only workflow templates: plans and briefs for your own orchestrator (TEAM+).",
    no_args_is_help=True,
)

# Generous: a definition is capped at 32 KiB, but a YAML file may carry
# comments and indentation.
_MAX_FILE_BYTES = 256 * 1024
_MAX_NAME = 100
# A valid template nests about six levels (file -> definition -> steps ->
# step -> depends_on -> id). Anything far deeper is refused with a clear
# error before the YAML composer or the JSON decoder recurses that far.
_MAX_NESTING = 32


# ── Template files ──

_BOOLISH = frozenset({"yes", "no", "on", "off", "y", "n", "true", "false", "null"})
_NUMBERISH_RE = re.compile(
    r"^[-+]?(\.\d|\d)[\d_.:eE+-]*$|^[-+]?\.(inf|nan)$|^[-+]?0[xob][0-9a-f_]+$",
    re.IGNORECASE,
)
_DATEISH_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}")
_BLOCK_HEADER_RE = re.compile(r"[|>][+-]?(?=\s|$)")


def ambiguous_plain(value: str) -> str | None:
    """Why a plain YAML scalar could be read differently by two readers, or
    None. Mirrors ``ambiguousPlain`` in the dashboard template editor."""
    if value.lower() in _BOOLISH and value not in ("true", "false", "null"):
        return f"{value!r} reads differently in YAML readers; quote it, or write true, false or null"
    if re.fullmatch(r"-?\d+", value):
        if re.match(r"-?0\d", value):
            return f"{value!r} has a leading zero (an octal number in YAML); quote it or drop the zero"
        return None
    if _NUMBERISH_RE.match(value) or _DATEISH_RE.match(value) or value == "=":
        return f"{value!r} is not a plain integer, so YAML readers may not read it as text; quote it"
    return None


def _strict_yaml_load(text: str) -> Any:
    """``yaml.safe_load`` that refuses what the dashboard's reader refuses."""
    import yaml
    from yaml.events import AliasEvent, ScalarEvent

    lines = text.split("\n")

    def refuse(mark: Any, message: str) -> WorkflowDefinitionError:
        return WorkflowDefinitionError(f"line {mark.line + 1}: {message}")

    def check_block(event: Any) -> None:
        start = event.start_mark
        if not _BLOCK_HEADER_RE.match(text, start.index):
            raise refuse(start, "block scalars with indentation indicators (like |2) are not supported; use | or >")
        base = -1
        for no in range(start.line + 1, min(event.end_mark.line + 1, len(lines))):
            line = lines[no]
            if line.strip() == "":
                if base >= 0 and len(line) > base:
                    raise WorkflowDefinitionError(
                        f"line {no + 1}: a blank line inside a block has trailing spaces; remove them"
                    )
                continue
            indent = len(line) - len(line.lstrip(" "))
            if base < 0:
                base = indent
            elif event.style == ">" and indent > base:
                raise WorkflowDefinitionError(
                    f"line {no + 1}: more-indented lines inside a folded (>) block are not supported; use | instead"
                )

    class _StrictLoader(yaml.SafeLoader):
        _depth = 0

        def compose_node(self, parent: Any, index: Any) -> Any:
            event = self.peek_event()
            if self._depth >= _MAX_NESTING:
                raise refuse(
                    event.start_mark, f"nested more than {_MAX_NESTING} levels deep"
                )
            if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
                raise refuse(event.start_mark, "anchors (&) and aliases (*) are not supported")
            if getattr(event, "tag", None) is not None:
                raise refuse(event.start_mark, "tags (!) are not supported")
            if isinstance(event, ScalarEvent):
                if event.style in ("|", ">"):
                    check_block(event)
                elif event.style is None and event.value:
                    why = ambiguous_plain(event.value)
                    if why:
                        raise refuse(event.start_mark, why)
            self._depth += 1
            try:
                return super().compose_node(parent, index)
            finally:
                self._depth -= 1

    # _StrictLoader subclasses yaml.SafeLoader (no python/* tags, no object
    # construction) and refuses even more; bandit can't see through the
    # subclass, so B506 is a false positive here.
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506  # nosec B506


def _check_json_nesting(text: str) -> None:
    """Refuse JSON nested deeper than ``_MAX_NESTING`` before decoding it
    (``json.loads`` recurses once per level). Brackets inside strings are
    skipped."""
    depth = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > _MAX_NESTING:
                raise WorkflowDefinitionError(
                    f"nested more than {_MAX_NESTING} levels deep"
                )
        elif ch in "]}":
            depth -= 1


def load_template_file(path: str) -> tuple[str | None, dict[str, Any]]:
    """Read a YAML or JSON template file -> (name or None, definition dict).

    Raises ``WorkflowDefinitionError`` for anything that is not a valid v1
    definition, so callers can refuse before sending a request.
    """
    p = Path(path)
    try:
        raw_bytes = p.read_bytes()
    except OSError as exc:
        raise WorkflowDefinitionError(f"could not read {path}: {exc}") from exc
    if len(raw_bytes) > _MAX_FILE_BYTES:
        raise WorkflowDefinitionError(f"{path} is larger than {_MAX_FILE_BYTES} bytes")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowDefinitionError(f"{path} is not UTF-8 text") from exc

    data: Any
    if p.suffix.lower() == ".json":
        _check_json_nesting(text)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise WorkflowDefinitionError(f"{path} is not valid JSON: {exc}") from exc
    else:
        import yaml

        try:
            data = _strict_yaml_load(text)
        except yaml.YAMLError as exc:
            raise WorkflowDefinitionError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowDefinitionError("a template file must be a mapping with 'steps'")

    data = dict(data)
    name = data.pop("name", None)
    if name is not None and not isinstance(name, str):
        raise WorkflowDefinitionError("name must be a string")
    if "definition" in data:
        definition = data.pop("definition")
        if data:
            raise WorkflowDefinitionError(
                f"unexpected keys next to 'definition': {sorted(data)}"
            )
        if not isinstance(definition, dict):
            raise WorkflowDefinitionError("definition must be a mapping")
    else:
        definition = data
    parsed = parse_definition(definition)
    return (name.strip() or None) if isinstance(name, str) else None, parsed.model_dump(
        mode="json"
    )


def _template_body(file: str, name: str | None) -> tuple[str | None, dict[str, Any]]:
    try:
        file_name, definition = load_template_file(file)
    except WorkflowDefinitionError as exc:
        err_console.print(f"[red]Invalid workflow template:[/red] {exc}")
        raise typer.Exit(1)
    chosen = (name or "").strip() or file_name
    if chosen is not None and len(chosen) > _MAX_NAME:
        err_console.print(f"[red]Template name must be at most {_MAX_NAME} characters.[/red]")
        raise typer.Exit(1)
    return chosen, definition


# ── HTTP ──


def _base(org: str | None) -> tuple[str, str, str]:
    """(api_url, api_key, path prefix) for the project or an org."""
    if org:
        api_url, api_key = _get_api_config()
        return api_url, api_key, f"/api/v1/orgs/{_path_segment(org)}"
    api_url, api_key, project_id = _resolve_project()
    return api_url, api_key, f"/api/v1/projects/{project_id}"


def _call(
    method: str,
    api_url: str,
    api_key: str,
    path: str,
    *,
    body: dict | None = None,
    not_found: str,
) -> Any:
    async def _go() -> Any:
        status, payload, _ = await _api_request(
            method, path, api_url, api_key, json_data=body
        )
        if status == 404:
            err_console.print(f"[red]{not_found}[/red]")
            raise typer.Exit(1)
        if status >= 400:
            err_console.print(f"[red]API error ({status}): {format_api_error(payload, status)}[/red]")
            raise typer.Exit(1)
        return payload

    return asyncio.run(_go())


# ── Rendering ──


def _steps_table(steps: list[dict]) -> Table:
    table = Table(show_header=True, header_style="bold")
    for col in ("Step", "Role", "Agent", "Depends on", "Fan-out", "Gate"):
        table.add_column(col)
    for s in steps:
        table.add_row(
            str(s.get("id") or s.get("step_id") or ""),
            str(s.get("role", "")),
            str(s.get("agent", "")),
            ", ".join(s.get("depends_on") or []) or "-",
            str(s.get("fan_out", 1)),
            str(s.get("gate", "none")),
        )
    return table


# ── Commands ──


@workflow_app.command("list")
@handle_errors
def workflow_list(
    org: str | None = typer.Option(None, "--org", help="List this org's templates instead."),
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """List active templates (the project's own plus its org's)."""
    api_url, api_key, prefix = _base(org)
    rows = _call("GET", api_url, api_key, f"{prefix}/workflow-templates", not_found="Not found.")
    rows = rows if isinstance(rows, list) else []
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        console.print("[dim]No workflow templates.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("ID", "Name", "Version", "Scope", "Steps"):
        table.add_column(col)
    for r in rows:
        steps = ((r.get("definition") or {}).get("steps")) or []
        table.add_row(
            str(r.get("id", "")),
            str(r.get("name", "")),
            str(r.get("version", "")),
            str(r.get("scope", "")),
            str(len(steps)),
        )
    console.print(table)


@workflow_app.command("show")
@handle_errors
def workflow_show(
    template_id: str = typer.Argument(..., help="Template id (wft_...)."),
    org: str | None = typer.Option(None, "--org", help="Read through this org."),
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """Show one template version with its steps and briefs."""
    api_url, api_key, prefix = _base(org)
    row = _call(
        "GET",
        api_url,
        api_key,
        f"{prefix}/workflow-templates/{_path_segment(template_id)}",
        not_found=f"Workflow template '{template_id}' not found.",
    )
    if as_json or not isinstance(row, dict):
        typer.echo(json.dumps(row, indent=2))
        return
    active = "active" if row.get("active") else "inactive"
    console.print(
        f"[bold]{row.get('name')}[/bold] {row.get('id')} v{row.get('version')} "
        f"({row.get('scope')}, {active})"
    )
    steps = ((row.get("definition") or {}).get("steps")) or []
    console.print(_steps_table(steps))
    for s in steps:
        if s.get("brief_template"):
            console.print(Panel(s["brief_template"], title=f"{s.get('id')} brief", expand=False))


def _print_template_result(row: Any, verb: str) -> None:
    if isinstance(row, dict):
        console.print(
            f"[green]✓[/green] {verb} [bold]{row.get('id')}[/bold] "
            f"{row.get('name')} v{row.get('version')}"
        )


@workflow_app.command("create")
@handle_errors
def workflow_create(
    file: str = typer.Argument(..., help="Template file (.yaml/.yml or .json)."),
    name: str | None = typer.Option(None, "--name", help="Template name (overrides the file's)."),
    org: str | None = typer.Option(None, "--org", help="Create an org template (org admins)."),
) -> None:
    """Create a template; validated locally before anything is sent."""
    chosen, definition = _template_body(file, name)
    if not chosen:
        err_console.print("[red]A template name is required (set 'name' in the file or --name).[/red]")
        raise typer.Exit(1)
    api_url, api_key, prefix = _base(org)
    row = _call(
        "POST",
        api_url,
        api_key,
        f"{prefix}/workflow-templates",
        body={"name": chosen, "definition": definition},
        not_found="Project or org not found.",
    )
    _print_template_result(row, "created")


@workflow_app.command("update")
@handle_errors
def workflow_update(
    template_id: str = typer.Argument(..., help="Active template version id (wft_...)."),
    file: str = typer.Argument(..., help="Template file (.yaml/.yml or .json)."),
    name: str | None = typer.Option(None, "--name", help="New name (default: keep the current one)."),
    org: str | None = typer.Option(None, "--org", help="Update an org template (org admins)."),
) -> None:
    """Save a new version of a template (the old version is deactivated)."""
    chosen, definition = _template_body(file, name)
    api_url, api_key, prefix = _base(org)
    path = f"{prefix}/workflow-templates/{_path_segment(template_id)}"
    not_found = f"Workflow template '{template_id}' not found."
    if not chosen:
        current = _call("GET", api_url, api_key, path, not_found=not_found)
        chosen = current.get("name") if isinstance(current, dict) else None
    row = _call(
        "PUT",
        api_url,
        api_key,
        path,
        body={"name": chosen, "definition": definition},
        not_found=not_found,
    )
    _print_template_result(row, "saved")


@workflow_app.command("disable")
@handle_errors
def workflow_disable(
    template_id: str = typer.Argument(..., help="Template version id (wft_...)."),
    org: str | None = typer.Option(None, "--org", help="An org template (org admins)."),
) -> None:
    """Deactivate a template version (running workflows keep their pin)."""
    api_url, api_key, prefix = _base(org)
    _call(
        "DELETE",
        api_url,
        api_key,
        f"{prefix}/workflow-templates/{_path_segment(template_id)}",
        not_found=f"Workflow template '{template_id}' not found.",
    )
    console.print(f"[green]✓[/green] {template_id} disabled")


@workflow_app.command("start")
@handle_errors
def workflow_start(
    template_id: str = typer.Argument(..., help="Template id (wft_...)."),
    ticket: str | None = typer.Option(None, "--ticket", help="Adopt this issue (tk_...) as the loop parent."),
    title: str | None = typer.Option(None, "--title", help="Title for a new loop parent issue."),
    description: str | None = typer.Option(None, "--description"),
    reviewer_persona: str | None = typer.Option(None, "--reviewer-persona"),
    token_budget: int | None = typer.Option(None, "--token-budget", min=1),
    cost_budget_usd: float | None = typer.Option(None, "--cost-budget-usd", help="Org owners/admins only."),
    max_review_rounds: int | None = typer.Option(None, "--max-review-rounds", min=1),
    output_id: bool = typer.Option(False, "--output-id", help="Print only the loop id (for scripts)."),
) -> None:
    """Start a workflow loop. Nothing runs: your orchestrator runs the steps."""
    if not (ticket and ticket.strip()) and not (title and title.strip()):
        err_console.print("[red]Pass --ticket to adopt an issue or --title for a new one.[/red]")
        raise typer.Exit(1)
    if cost_budget_usd is not None and not cost_budget_usd > 0:
        err_console.print("[red]--cost-budget-usd must be greater than 0.[/red]")
        raise typer.Exit(1)
    body: dict[str, Any] = {"template_id": template_id.strip()}
    for key, val in (
        ("ticket_id", ticket),
        ("title", title),
        ("description", description),
        ("reviewer_persona", reviewer_persona),
    ):
        if val and val.strip():
            body[key] = val.strip()
    for key, num in (
        ("token_budget", token_budget),
        ("cost_budget_usd", cost_budget_usd),
        ("max_review_rounds", max_review_rounds),
    ):
        if num is not None:
            body[key] = num
    api_url, api_key, prefix = _base(None)
    result = _call(
        "POST",
        api_url,
        api_key,
        f"{prefix}/workflows",
        body=body,
        not_found=f"Workflow template '{template_id}' not found.",
    )
    result = result if isinstance(result, dict) else {}
    if output_id:
        typer.echo(result.get("loop_id", ""))
        return
    wf = result.get("workflow") or {}
    console.print(
        f"[green]✓[/green] workflow loop [bold]{result.get('loop_id')}[/bold] "
        f"from {wf.get('name')} v{wf.get('version')}"
    )
    for step in result.get("steps") or []:
        tickets = ", ".join(t.get("id", "") for t in step.get("tickets") or []) or "-"
        console.print(f"  {step.get('step_id')}: {tickets}")
    reviewer = result.get("reviewer") or {}
    if reviewer.get("persona") and not reviewer.get("trusted"):
        console.print(
            f"[yellow]Reviewer {reviewer['persona']} is not a trusted reviewer yet; "
            "trusted_clean gates will not pass until one is registered.[/yellow]"
        )
    console.print(f"[dim]Next: sfs workflow plan {result.get('loop_id')}[/dim]")


def render_plan(plan: dict) -> None:
    tmpl = plan.get("template") or {}
    console.print(
        f"[bold]{plan.get('loop_id')}[/bold] {tmpl.get('name') or 'workflow'} v{tmpl.get('version')}"
        + (" [green]done[/green]" if plan.get("done") else "")
    )
    table = Table(show_header=True, header_style="bold")
    for col in ("Step", "Role", "Agent", "State", "Gate", "Why"):
        table.add_column(col)
    for s in plan.get("steps") or []:
        # Fields from an org template the caller cannot see come back null.
        gate = str(s.get("gate") or "-")
        gs = s.get("gate_status") or {}
        if gate not in ("none", "-"):
            gate += " ✓" if gs.get("passed") else ""
        table.add_row(
            str(s.get("step_id", "")),
            str(s.get("role", "")),
            str(s.get("agent") or "-"),
            str(s.get("state", "")),
            gate,
            str(s.get("reason") or gs.get("reason") or ""),
        )
    console.print(table)
    ready = [s for s in plan.get("steps") or [] if s.get("ready_children")]
    if not ready:
        console.print("[dim]No step is ready right now.[/dim]")
        return
    for s in ready:
        for child in s["ready_children"]:
            console.print(
                Panel(
                    child.get("brief_markdown") or "(no brief)",
                    title=(
                        f"ready: {s.get('step_id')} #{child.get('index')} "
                        f"{child.get('ticket_id')} agent={child.get('agent') or s.get('agent')}"
                    ),
                    expand=False,
                )
            )


@workflow_app.command("plan")
@handle_errors
def workflow_plan(
    loop_id: str = typer.Argument(..., help="Workflow loop (parent issue) id (tk_...)."),
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """Show each step's state and the ready steps with their agents and briefs."""
    api_url, api_key, prefix = _base(None)
    plan = _call(
        "GET",
        api_url,
        api_key,
        f"{prefix}/workflows/{_path_segment(loop_id)}/plan",
        not_found=f"Workflow loop '{loop_id}' not found.",
    )
    if as_json or not isinstance(plan, dict):
        typer.echo(json.dumps(plan, indent=2))
        return
    render_plan(plan)


__all__ = ["load_template_file", "render_plan", "workflow_app"]
