"""`sfs eval …` and `sfs artifact …` — record-only evaluations and
model/dataset lineage (GitHub #121/#122, Team+ for writes).

SessionFS never runs a grader, calls a model or fetches an artifact. Your CI
grades and posts results; the server computes the verdict and decides
whether the run is trusted (a registered trusted grader, independent of
whoever produced the artifact).

Evals:
- sfs eval suite list | show <suite> | create <file> | update <suite_id> <file> | disable <suite_id>
- sfs eval case add <suite> <cases.jsonl> | list <suite> | retire <suite> <key>...
- sfs eval run start <suite> (--artifact ID | --commit SHA | --label TEXT) [--output-id]
- sfs eval run submit <run_id> --from results.jsonl [--finish]
- sfs eval run finish <run_id>
- sfs eval runs [--suite S] [--artifact A]
- sfs eval compare <a> <b>        (exits 1 on any regression, for CI)
- sfs eval void <run_id> --reason TEXT

Artifacts:
- sfs artifact register --kind K --name N --version V --uri URI [--digest D] [--tag k=v]...
- sfs artifact list | show <id> | link <parent> <child> --relation R | lineage <id>
- sfs artifact tag <id> k=v... [--remove k] | deprecate <id> | promote <id> --to STAGE
- sfs artifact requirements <kind> <name> [--suite S]... [--require-tag k=v]...

Suite files are YAML (the same strict loader as workflow templates) or JSON
with slug, name, grader, acceptance, case_storage and optional cases. Case
and result files are JSON Lines. Everything is validated locally with the
models the server uses before any request is sent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from sessionfs.cli.cmd_loop import _resolve_project
from sessionfs.cli.cmd_rules import _api_request
from sessionfs.cli.common import console, err_console, format_api_error, handle_errors
from sessionfs.eval_definition import (
    MAX_BATCH,
    EvalDefinitionError,
    case_content_sha256,
    parse_case,
    parse_definition,
    parse_result,
)
from sessionfs.url_path_safety import _path_segment
from sessionfs.workflow_definition import WorkflowDefinitionError

eval_app = typer.Typer(
    name="eval",
    help="Record-only evaluations: suites, cases, graded runs and comparisons (TEAM+ for writes).",
    no_args_is_help=True,
)
suite_app = typer.Typer(help="Evaluation suites (people define the bar).", no_args_is_help=True)
case_app = typer.Typer(help="Cases in a suite (each change is a new suite version).", no_args_is_help=True)
run_app = typer.Typer(help="Graded runs posted from your own CI.", no_args_is_help=True)
eval_app.add_typer(suite_app, name="suite")
eval_app.add_typer(case_app, name="case")
eval_app.add_typer(run_app, name="run")

artifact_app = typer.Typer(
    name="artifact",
    help="Model/dataset lineage: artifact pointers, edges and gated promotion (TEAM+ for writes).",
    no_args_is_help=True,
)

_MAX_FILE_BYTES = 64 * 1024 * 1024


# ── HTTP ──


def _call(method: str, tail: str, *, body: dict | None = None, not_found: str = "Not found.") -> Any:
    api_url, api_key, project_id = _resolve_project()

    async def _go() -> Any:
        status, payload, _ = await _api_request(
            method, f"/api/v1/projects/{project_id}/{tail}", api_url, api_key, json_data=body
        )
        if status == 404:
            err_console.print(f"[red]{not_found}[/red]")
            raise typer.Exit(1)
        if status >= 400:
            err_console.print(f"[red]API error ({status}): {format_api_error(payload, status)}[/red]")
            raise typer.Exit(1)
        return payload

    return asyncio.run(_go())


def _seg(value: str) -> str:
    return _path_segment(value.strip())


def _echo_json(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2))


# ── Files ──


def _read_text(path: str) -> str:
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise EvalDefinitionError(f"could not read {path}: {exc}") from exc
    if len(raw) > _MAX_FILE_BYTES:
        raise EvalDefinitionError(f"{path} is larger than {_MAX_FILE_BYTES} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvalDefinitionError(f"{path} is not UTF-8 text") from exc


def load_suite_file(path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a YAML/JSON suite file -> (definition, cases). Validated locally."""
    from sessionfs.cli.cmd_workflow import _check_json_nesting, _strict_yaml_load

    text = _read_text(path)
    try:
        if Path(path).suffix.lower() == ".json":
            _check_json_nesting(text)
            data = json.loads(text)
        else:
            import yaml

            try:
                data = _strict_yaml_load(text)
            except yaml.YAMLError as exc:
                raise EvalDefinitionError(f"{path} is not valid YAML: {exc}") from exc
    except WorkflowDefinitionError as exc:
        raise EvalDefinitionError(str(exc)) from exc
    except ValueError as exc:
        raise EvalDefinitionError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EvalDefinitionError("a suite file must be a mapping")
    data = dict(data)
    cases = data.pop("cases", []) or []
    if not isinstance(cases, list):
        raise EvalDefinitionError("cases must be a list")
    definition = parse_definition(data).model_dump(mode="json")
    checked = [_check_case(c, definition["case_storage"], i) for i, c in enumerate(cases)]
    return definition, checked


def _check_case(raw: Any, storage: str, index: int) -> dict[str, Any]:
    try:
        spec = parse_case(raw)
    except EvalDefinitionError as exc:
        raise EvalDefinitionError(f"case {index + 1}: {exc}") from exc
    out = spec.model_dump(mode="json", exclude_none=True)
    if storage == "reference":
        # Keep the text local: send only the hash.
        if spec.input is not None or spec.expected is not None:
            out["content_sha256"] = spec.content_sha256 or case_content_sha256(spec.input, spec.expected)
            out.pop("input", None)
            out.pop("expected", None)
        if not out.get("content_sha256"):
            raise EvalDefinitionError(f"case {index + 1}: reference storage needs content_sha256 (or local input/expected to hash)")
    return out


def load_jsonl(path: str, what: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for no, line in enumerate(_read_text(path).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise EvalDefinitionError(f"{path} line {no}: not valid JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise EvalDefinitionError(f"{path} line {no}: each {what} must be a JSON object")
        rows.append(row)
    return rows


def _fail(exc: Exception, what: str) -> None:
    err_console.print(f"[red]Invalid {what}:[/red] {exc}")
    raise typer.Exit(1)


def _batches(items: list[Any]) -> list[list[Any]]:
    return [items[i : i + MAX_BATCH] for i in range(0, len(items), MAX_BATCH)]


# ── Rendering ──


def _trust(run: dict) -> str:
    if run.get("state") != "finished":
        return run.get("state") or ""
    return "trusted" if run.get("trusted") else f"unverified ({run.get('untrusted_reason') or 'unknown'})"


def _print_run(run: dict) -> None:
    summary = run.get("summary") or {}
    console.print(
        f"[bold]{run.get('id')}[/bold] {run.get('state')} "
        f"verdict={run.get('verdict') or '-'} {_trust(run)}"
    )
    if summary:
        console.print(
            f"  {summary.get('passed', 0)}/{summary.get('graded', 0)} passed of "
            f"{summary.get('total_cases', 0)} cases, pass rate {summary.get('pass_rate')}"
        )
        for reason in summary.get("reasons") or []:
            console.print(f"  - {reason}")


# ── Suites ──


@suite_app.command("list")
@handle_errors
def suite_list(as_json: bool = typer.Option(False, "--json")) -> None:
    """Active suites."""
    out = _call("GET", "eval-suites")
    suites = (out or {}).get("suites", [])
    if as_json:
        _echo_json(suites)
        return
    if not suites:
        console.print("[dim]No eval suites.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("Slug", "Name", "Version", "Grader", "Storage", "Cases"):
        table.add_column(col)
    for s in suites:
        grader = s.get("grader") or {}
        table.add_row(
            s.get("slug", ""), s.get("name", ""), str(s.get("version", "")),
            f"{grader.get('kind')}:{grader.get('persona')}", s.get("case_storage", ""),
            str(s.get("live_cases", "")),
        )
    console.print(table)


@suite_app.command("show")
@handle_errors
def suite_show(
    suite: str = typer.Argument(..., help="evs_ id, evl_ lineage id or slug."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    out = _call("GET", f"eval-suites/{_seg(suite)}", not_found=f"Eval suite '{suite}' not found.")
    if as_json:
        _echo_json(out)
        return
    console.print(f"[bold]{out.get('name')}[/bold] {out.get('slug')} v{out.get('version')} ({out.get('id')})")
    console.print(f"  grader: {json.dumps(out.get('grader'))}")
    console.print(f"  acceptance: {json.dumps(out.get('acceptance'))}")
    console.print(f"  storage: {out.get('case_storage')}, live cases: {out.get('live_cases')}")


@suite_app.command("create")
@handle_errors
def suite_create(file: str = typer.Argument(..., help="Suite file (.yaml/.yml or .json).")) -> None:
    """Create a suite (project admins, user keys). Cases in the file go in v1."""
    try:
        definition, cases = load_suite_file(file)
    except EvalDefinitionError as exc:
        _fail(exc, "eval suite")
    first, rest = cases[:MAX_BATCH], cases[MAX_BATCH:]
    out = _call("POST", "eval-suites", body={**definition, "cases": first})
    for batch in _batches(rest):
        out = _call("POST", f"eval-suites/{_seg(out['lineage_id'])}/cases", body={"cases": batch})
    console.print(
        f"[green]✓[/green] suite [bold]{out.get('slug')}[/bold] v{out.get('version')} "
        f"({out.get('live_cases')} cases)"
    )


@suite_app.command("update")
@handle_errors
def suite_update(
    suite_id: str = typer.Argument(..., help="Active version id (evs_...)."),
    file: str = typer.Argument(...),
) -> None:
    """Save a new version of the definition (cases carry over; cases in the file are ignored)."""
    try:
        definition, _cases = load_suite_file(file)
    except EvalDefinitionError as exc:
        _fail(exc, "eval suite")
    out = _call("PUT", f"eval-suites/{_seg(suite_id)}", body=definition, not_found=f"Eval suite '{suite_id}' not found.")
    console.print(f"[green]✓[/green] saved {out.get('slug')} v{out.get('version')}")


@suite_app.command("disable")
@handle_errors
def suite_disable(suite_id: str = typer.Argument(...)) -> None:
    _call("DELETE", f"eval-suites/{_seg(suite_id)}", not_found=f"Eval suite '{suite_id}' not found.")
    console.print(f"[green]✓[/green] {suite_id} disabled")


# ── Cases ──


@case_app.command("add")
@handle_errors
def case_add(
    suite: str = typer.Argument(..., help="Suite slug or id."),
    file: str = typer.Argument(..., help="JSON Lines file, one case per line."),
    reference: bool = typer.Option(False, "--reference", help="The suite stores cases by reference: send hashes only."),
) -> None:
    """Add cases (batches of 500; each batch is a new suite version)."""
    try:
        cases = [
            _check_case(c, "reference" if reference else "inline", i)
            for i, c in enumerate(load_jsonl(file, "case"))
        ]
    except EvalDefinitionError as exc:
        _fail(exc, "eval cases")
    if not cases:
        err_console.print("[red]No cases in the file.[/red]")
        raise typer.Exit(1)
    out: Any = None
    for batch in _batches(cases):
        out = _call("POST", f"eval-suites/{_seg(suite)}/cases", body={"cases": batch}, not_found=f"Eval suite '{suite}' not found.")
        suite = out.get("lineage_id", suite)
    console.print(f"[green]✓[/green] added {len(cases)} cases; now v{out.get('version')} ({out.get('live_cases')} live)")


@case_app.command("list")
@handle_errors
def case_list(suite: str = typer.Argument(...), as_json: bool = typer.Option(False, "--json")) -> None:
    out = _call("GET", f"eval-suites/{_seg(suite)}/cases", not_found=f"Eval suite '{suite}' not found.")
    cases = out.get("cases", [])
    if as_json:
        _echo_json(out)
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("Key", "Name", "Tags", "sha256"):
        table.add_column(col)
    for c in cases:
        table.add_row(c.get("case_key", ""), c.get("name", ""), ", ".join(c.get("tags") or []), (c.get("content_sha256") or "")[:12])
    console.print(table)
    if out.get("next_cursor"):
        console.print("[dim]More cases exist; use --json with the API cursor.[/dim]")


@case_app.command("retire")
@handle_errors
def case_retire(suite: str = typer.Argument(...), keys: list[str] = typer.Argument(...)) -> None:
    out = _call("POST", f"eval-suites/{_seg(suite)}/cases/retire", body={"case_keys": keys}, not_found=f"Eval suite '{suite}' not found.")
    console.print(f"[green]✓[/green] retired {len(keys)}; now v{out.get('version')}")


# ── Runs ──


@run_app.command("start")
@handle_errors
def run_start(
    suite: str = typer.Argument(...),
    artifact: str | None = typer.Option(None, "--artifact", help="Artifact id graded (art_...)."),
    commit: str | None = typer.Option(None, "--commit", help="Full commit sha graded."),
    label: str | None = typer.Option(None, "--label", help="Free-text subject label."),
    ticket: str | None = typer.Option(None, "--ticket"),
    trigger: str = typer.Option("ci", "--trigger", help="manual | ci | workflow | promotion"),
    trigger_ref: str | None = typer.Option(None, "--trigger-ref"),
    cases_out: str | None = typer.Option(None, "--cases-out", help="Write the live cases to this JSONL file."),
    output_id: bool = typer.Option(False, "--output-id", help="Print only the run id."),
) -> None:
    """Start a run; the calling credential becomes the grader."""
    body: dict[str, Any] = {"suite": suite, "trigger_source": trigger}
    for key, val in (("artifact_id", artifact), ("commit_sha", commit), ("subject_label", label), ("ticket_id", ticket), ("trigger_ref", trigger_ref)):
        if val and val.strip():
            body[key] = val.strip()
    if not any(k in body for k in ("artifact_id", "commit_sha", "subject_label")):
        err_console.print("[red]Pass --artifact, --commit or --label.[/red]")
        raise typer.Exit(1)
    out = _call("POST", "eval-runs", body=body, not_found="Suite, artifact or ticket not found.")
    run = out.get("run", {})
    if cases_out:
        Path(cases_out).write_text("".join(json.dumps(c) + "\n" for c in out.get("cases", [])), encoding="utf-8")
    if output_id:
        typer.echo(run.get("id", ""))
        return
    trust = out.get("trust_preview") or {}
    console.print(f"[green]✓[/green] run [bold]{run.get('id')}[/bold] ({len(out.get('cases', []))} cases)")
    if not trust.get("trusted"):
        console.print(f"[yellow]This run will be unverified: {trust.get('reason')}.[/yellow]")


@run_app.command("submit")
@handle_errors
def run_submit(
    run_id: str = typer.Argument(...),
    from_file: str = typer.Option(..., "--from", help="results.jsonl, one result per line."),
    finish: bool = typer.Option(False, "--finish", help="Finish the run after posting."),
) -> None:
    """Post results in batches of 500 (idempotent per case)."""
    try:
        rows = [parse_result(r).model_dump(mode="json", exclude_none=True) for r in load_jsonl(from_file, "result")]
    except EvalDefinitionError as exc:
        _fail(exc, "eval results")
    total = 0
    for batch in _batches(rows):
        out = _call("POST", f"eval-runs/{_seg(run_id)}/results", body={"results": batch}, not_found=f"Eval run '{run_id}' not found.")
        total += int(out.get("recorded", 0))
    console.print(f"[green]✓[/green] recorded {total} results")
    if finish:
        _print_run(_call("POST", f"eval-runs/{_seg(run_id)}/finish", body={}))


@run_app.command("finish")
@handle_errors
def run_finish(run_id: str = typer.Argument(...), as_json: bool = typer.Option(False, "--json")) -> None:
    out = _call("POST", f"eval-runs/{_seg(run_id)}/finish", body={}, not_found=f"Eval run '{run_id}' not found.")
    if as_json:
        _echo_json(out)
        return
    _print_run(out)
    if out.get("verdict") != "passed":
        raise typer.Exit(1)


@eval_app.command("runs")
@handle_errors
def runs_list(
    suite: str | None = typer.Option(None, "--suite"),
    artifact: str | None = typer.Option(None, "--artifact"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in (("suite", suite), ("artifact_id", artifact)) if v})
    out = _call("GET", "eval-runs" + (f"?{query}" if query else ""))
    runs = out.get("runs", [])
    if as_json:
        _echo_json(runs)
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("Run", "Subject", "State", "Verdict", "Trust", "Pass rate"):
        table.add_column(col)
    for r in runs:
        table.add_row(
            r.get("id", ""), r.get("artifact_id") or r.get("commit_sha") or r.get("subject_label") or "",
            r.get("state", ""), r.get("verdict") or "-", _trust(r),
            str((r.get("summary") or {}).get("pass_rate") or "-"),
        )
    console.print(table)


@eval_app.command("compare")
@handle_errors
def compare(
    a: str = typer.Argument(..., help="Reference run."),
    b: str = typer.Argument(..., help="Candidate run."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Compare two runs; exits 1 on any regression (for CI)."""
    from urllib.parse import urlencode

    out = _call("GET", "eval-runs/compare?" + urlencode({"a": a, "b": b}))
    if as_json:
        _echo_json(out)
    else:
        for warning in out.get("warnings") or []:
            console.print(f"[yellow]{warning}[/yellow]")
        console.print(f"regressions: {out.get('regression_count', 0)}  fixes: {out.get('fix_count', 0)}")
        for key in out.get("regressions") or []:
            console.print(f"  [red]-[/red] {key}")
    if out.get("regression_count"):
        raise typer.Exit(1)


@eval_app.command("void")
@handle_errors
def void(run_id: str = typer.Argument(...), reason: str = typer.Option(..., "--reason")) -> None:
    out = _call("POST", f"eval-runs/{_seg(run_id)}/void", body={"reason": reason}, not_found=f"Eval run '{run_id}' not found.")
    console.print(f"[green]✓[/green] {out.get('id')} voided")


# ── Artifacts ──


def _pairs(values: list[str] | None, what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            err_console.print(f"[red]{what} must be key=value: {item!r}[/red]")
            raise typer.Exit(1)
        out[key.strip()] = value
    return out


def _print_artifact(a: dict) -> None:
    console.print(
        f"[bold]{a.get('kind')} {a.get('name')} {a.get('version')}[/bold] {a.get('id')} "
        f"stage={a.get('stage')}{' deprecated' if a.get('deprecated_at') else ''}"
    )
    console.print(f"  {a.get('external_uri')}")
    if a.get("tags"):
        console.print("  tags: " + ", ".join(f"{k}={v}" for k, v in a["tags"].items()))


@artifact_app.command("register")
@handle_errors
def artifact_register(
    kind: str = typer.Option(..., "--kind", help="dataset | model | feature_set | prompt | checkpoint"),
    name: str = typer.Option(..., "--name"),
    version: str = typer.Option(..., "--version"),
    uri: str = typer.Option(..., "--uri", help="s3:// gs:// hf:// https:// mlflow models:/ runs:/ vertex (never fetched)."),
    digest: str | None = typer.Option(None, "--digest"),
    description: str | None = typer.Option(None, "--description"),
    schema_file: str | None = typer.Option(None, "--schema", help="JSON file with a schema summary."),
    tag: list[str] = typer.Option(None, "--tag", help="key=value (repeatable)."),
    produced_by: str | None = typer.Option(None, "--produced-by", help="Agent run id that produced it."),
    output_id: bool = typer.Option(False, "--output-id"),
) -> None:
    from sessionfs.artifact_uri import ArtifactUriError, validate_uri

    try:
        uri = validate_uri(uri)
    except ArtifactUriError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    body: dict[str, Any] = {"kind": kind, "name": name, "version": version, "external_uri": uri, "tags": _pairs(tag, "--tag")}
    for key, val in (("digest", digest), ("description", description), ("produced_by_agent_run_id", produced_by)):
        if val:
            body[key] = val
    if schema_file:
        try:
            body["schema_summary"] = json.loads(_read_text(schema_file))
        except (ValueError, EvalDefinitionError) as exc:
            _fail(exc, "schema file")
    out = _call("POST", "artifacts", body=body)
    if output_id:
        typer.echo(out.get("id", ""))
        return
    console.print("[green]✓[/green] registered")
    _print_artifact(out)


@artifact_app.command("list")
@handle_errors
def artifact_list(
    kind: str | None = typer.Option(None, "--kind"),
    name: str | None = typer.Option(None, "--name"),
    stage: str | None = typer.Option(None, "--stage"),
    tag: str | None = typer.Option(None, "--tag", help="key or key=value"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in (("kind", kind), ("name", name), ("stage", stage), ("tag", tag)) if v})
    out = _call("GET", "artifacts" + (f"?{query}" if query else ""))
    items = out.get("artifacts", [])
    if as_json:
        _echo_json(items)
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("ID", "Kind", "Name", "Version", "Stage", "URI"):
        table.add_column(col)
    for a in items:
        table.add_row(a.get("id", ""), a.get("kind", ""), a.get("name", ""), a.get("version", ""), a.get("stage", ""), a.get("external_uri", ""))
    console.print(table)


@artifact_app.command("show")
@handle_errors
def artifact_show(artifact_id: str = typer.Argument(...), as_json: bool = typer.Option(False, "--json")) -> None:
    out = _call("GET", f"artifacts/{_seg(artifact_id)}", not_found=f"Artifact '{artifact_id}' not found.")
    if as_json:
        _echo_json(out)
        return
    _print_artifact(out)
    console.print(f"  parents: {out.get('parents')}  children: {out.get('children')}")


@artifact_app.command("link")
@handle_errors
def artifact_link(
    parent: str = typer.Argument(...),
    child: str = typer.Argument(...),
    relation: str = typer.Option(..., "--relation", help="derived_from | trained_on | fine_tuned_from | distilled_from | evaluated_against | uses"),
    context_ref: str | None = typer.Option(None, "--context-ref"),
) -> None:
    body: dict[str, Any] = {"parent_id": parent, "child_id": child, "relation": relation}
    if context_ref:
        body["context_ref"] = context_ref
    out = _call("POST", "artifact-edges", body=body, not_found="Artifact not found.")
    console.print(f"[green]✓[/green] {out.get('parent_id')} -{out.get('relation')}-> {out.get('child_id')}")


@artifact_app.command("lineage")
@handle_errors
def artifact_lineage(
    artifact_id: str = typer.Argument(...),
    direction: str = typer.Option("both", "--direction", help="up | down | both"),
    depth: int = typer.Option(25, "--depth", min=1, max=25),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    out = _call("GET", f"artifacts/{_seg(artifact_id)}/lineage?direction={direction}&depth={depth}", not_found=f"Artifact '{artifact_id}' not found.")
    if as_json:
        _echo_json(out)
        return
    names = {n["id"]: f"{n['kind']} {n['name']} {n['version']}" for n in out.get("nodes", [])}
    for e in out.get("edges", []):
        console.print(f"{names.get(e['parent_id'], e['parent_id'])} -{e['relation']}-> {names.get(e['child_id'], e['child_id'])}")
    if out.get("truncated"):
        console.print("[yellow]Truncated (500 nodes or the depth limit).[/yellow]")


@artifact_app.command("tag")
@handle_errors
def artifact_tag(
    artifact_id: str = typer.Argument(...),
    pairs: list[str] = typer.Argument(None, help="key=value"),
    remove: list[str] = typer.Option(None, "--remove"),
) -> None:
    out = _call("PUT", f"artifacts/{_seg(artifact_id)}/tags", body={"set": _pairs(pairs, "tag"), "remove": remove or []}, not_found=f"Artifact '{artifact_id}' not found.")
    _print_artifact(out)


@artifact_app.command("deprecate")
@handle_errors
def artifact_deprecate(artifact_id: str = typer.Argument(...)) -> None:
    _call("POST", f"artifacts/{_seg(artifact_id)}/deprecate", body={}, not_found=f"Artifact '{artifact_id}' not found.")
    console.print(f"[green]✓[/green] {artifact_id} deprecated")


@artifact_app.command("promote")
@handle_errors
def artifact_promote(
    artifact_id: str = typer.Argument(...),
    to: str = typer.Option(..., "--to", help="candidate | staging | production | archived"),
) -> None:
    """Promote a version (people only). Staging and production need a trusted
    pass of every required suite on this exact version."""
    api_url, api_key, project_id = _resolve_project()

    async def _go() -> tuple[int, Any]:
        status, payload, _ = await _api_request(
            "POST", f"/api/v1/projects/{project_id}/artifacts/{_seg(artifact_id)}/promote",
            api_url, api_key, json_data={"to_stage": to},
        )
        return status, payload

    status, payload = asyncio.run(_go())
    if status == 409 and isinstance(payload, dict):
        details = ((payload.get("error") or {}).get("details") or payload.get("detail") or {})
        blockers = details.get("blockers") if isinstance(details, dict) else None
        if blockers:
            err_console.print("[red]Promotion blocked:[/red]")
            for b in blockers:
                err_console.print(f"  - {b.get('message')}")
            raise typer.Exit(1)
    if status == 404:
        err_console.print(f"[red]Artifact '{artifact_id}' not found.[/red]")
        raise typer.Exit(1)
    if status >= 400:
        err_console.print(f"[red]API error ({status}): {format_api_error(payload, status)}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] {artifact_id} -> {payload.get('stage')}")
    for archived in payload.get("archived_ids") or []:
        console.print(f"  archived {archived}")


@artifact_app.command("requirements")
@handle_errors
def artifact_requirements(
    kind: str = typer.Argument(...),
    name: str = typer.Argument(...),
    suite: list[str] = typer.Option(None, "--suite", help="Required suite (slug or id; repeatable)."),
    require_tag: list[str] = typer.Option(None, "--require-tag", help="key=value on trained_on parents."),
    show: bool = typer.Option(False, "--show", help="Only print the current requirements."),
) -> None:
    """Set (or --show) which suites must pass before a version is promoted."""
    path = f"artifact-families/{_seg(kind)}/{_seg(name)}/requirements"
    if show:
        _echo_json(_call("GET", path))
        return
    out = _call("PUT", path, body={"suites": suite or [], "require_tags": _pairs(require_tag, "--require-tag")})
    console.print(f"[green]✓[/green] {kind}/{name}: {len(out.get('suites', []))} required suites")


__all__ = ["artifact_app", "eval_app", "load_jsonl", "load_suite_file"]
