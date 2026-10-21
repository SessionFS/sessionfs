"""`sfs skill` / `sfs plugin` portable package management."""

from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

import typer
from rich.table import Table
from rich.text import Text

from sessionfs.cli.common import confirm_or_exit, console, handle_errors
from sessionfs.skills import SkillError, SkillManager
from sessionfs.skills.models import SUPPORTED_TOOLS, EmitPlan

skill_app = typer.Typer(
    name="skill",
    help="Import, inspect, and install portable agent skills and plugins.",
    no_args_is_help=True,
)
marketplace_app = typer.Typer(
    name="marketplace",
    help="Manage multiple user and project marketplace registries.",
    no_args_is_help=True,
)
skill_app.add_typer(marketplace_app, name="marketplace")

from sessionfs.cli.cmd_skill_publisher import publisher_app  # noqa: E402

skill_app.add_typer(publisher_app, name="publisher")

T = TypeVar("T")


def _call(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except SkillError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _print_json(value: Any) -> None:
    console.print_json(json.dumps(value, sort_keys=True))


def _literal(value: Any, style: str = "") -> Text:
    """Render untrusted metadata (registry descriptions, package names, MCP
    server names inside degradation reasons) LITERALLY: Rich would otherwise
    interpret ``[link=...]`` / ``[bold]`` markup coming from a package author."""
    return Text(str(value if value is not None else ""), style=style)


def _clean(value: Any) -> Any:
    """Server-supplied marketplace text, recursively stripped of terminal
    control, escape, zero-width and bidi characters before printing."""
    from sessionfs.skills.discovery import terminal_safe

    if isinstance(value, str):
        return terminal_safe(value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {terminal_safe(key) if isinstance(key, str) else key: _clean(item) for key, item in value.items()}
    return value


def _print_plans(plans: list[EmitPlan]) -> None:
    table = Table(title="Skill install plan")
    table.add_column("Tool", style="cyan")
    table.add_column("Files", justify="right")
    table.add_column("Omissions")
    table.add_column("Consent")
    for plan in plans:
        omissions = ", ".join(item.component for item in plan.omissions) or "-"
        consent = ", ".join(plan.consent_requirements) or "-"
        table.add_row(plan.tool, str(len(plan.emissions)), _literal(omissions), _literal(consent))
    console.print(table)


_SEVERITY_STYLE = {"critical": "red bold", "high": "red", "medium": "yellow", "low": "dim"}


def _print_disclosure(card: dict[str, Any], *, max_writes: int = 12) -> None:
    """Render the install disclosure card (marketplace v2 §3.3). Every value
    is untrusted package text and is printed literally."""
    console.print("[bold]What this package can do[/bold]")
    capabilities = ", ".join(card.get("capabilities") or []) or "none"
    console.print(_literal(f"  capabilities: {capabilities}"))
    digest = card.get("capability_digest")
    if digest:
        console.print(_literal(f"  capability digest: {digest}", "dim"))
    for tool, item in (card.get("tools") or {}).items():
        console.print(Text.assemble(("  ", ""), _literal(tool, "cyan bold"), (": ", ""), _literal(item.get("summary"))))
        writes = item.get("writes") or []
        for path in writes[:max_writes]:
            console.print(_literal(f"      writes {path}", "dim"))
        more = int(item.get("writes_total") or 0) - min(len(writes), max_writes)
        if more > 0:
            console.print(_literal(f"      ... and {more} more file(s)", "dim"))
        for omitted in item.get("omitted") or []:
            console.print(_literal(f"      not installed: {omitted.get('component')} ({omitted.get('reason')})", "yellow"))
    hooks = [hook for item in (card.get("tools") or {}).values() for hook in item.get("runs", {}).get("hooks", [])]
    if hooks:
        console.print("  [bold]hooks[/bold] (run by the tool without asking):")
        for hook in {(h.get("skill"), h.get("event"), h.get("matcher")) for h in hooks}:
            console.print(_literal(f"    {hook[0]}: {hook[1]} matcher {hook[2]}", "yellow"))
    allowed = [entry for entry in card.get("allowed_tools") or [] if str(entry).startswith("allowed-tools:")]
    if allowed:
        console.print(_literal("  pre-approved tools: " + ", ".join(str(e).split(":", 1)[1] for e in allowed), "yellow"))
    servers = card.get("mcp_servers") or {}
    for server in servers.get("local") or []:
        console.print(_literal(f"  MCP server (local): {server.get('name')} runs {server.get('command')}", "yellow"))
    for server in servers.get("remote") or []:
        console.print(_literal(f"  MCP server (remote): {server.get('name')} at {server.get('origin')}", "yellow"))
    endpoints = card.get("network_endpoints") or []
    if endpoints:
        shown = ", ".join(endpoints[:10]) + (f" (+{len(endpoints) - 10} more)" if len(endpoints) > 10 else "")
        console.print(_literal(f"  network endpoints referenced: {shown}"))
    if card.get("env_names"):
        console.print(_literal("  environment variables: " + ", ".join(card["env_names"])))
    commands = card.get("shell_commands") or []
    if commands:
        console.print(f"  shell commands the instructions tell the agent to run ({len(commands)}):")
        for command in commands[:10]:
            console.print(_literal(f"    {command.get('path')}:{command.get('line')}  {command.get('text')}", "dim"))
    scan = card.get("scan") or {}
    if scan.get("finding_count"):
        style = _SEVERITY_STYLE.get(str(scan.get("max_severity")), "")
        console.print(_literal(
            f"  scanner: {scan.get('finding_count')} finding(s), worst {scan.get('max_severity')} "
            f"(ruleset {scan.get('ruleset')})",
            style,
        ))
    unicode = (card.get("unicode") or {}).get("findings") or []
    if unicode:
        console.print(_literal(f"  INVISIBLE CHARACTERS in {(card.get('unicode') or {}).get('flagged_files')} file(s):", "red bold"))
        for finding in unicode[:10]:
            console.print(_literal(
                f"    {finding.get('path')}:{finding.get('line')}:{finding.get('column')} "
                f"{finding.get('visible') or ''} {finding.get('class')} ({finding.get('severity')})",
                _SEVERITY_STYLE.get(str(finding.get("severity")), ""),
            ))
    consent = [item for item in card.get("consent_required") or [] if item != "instructions"]
    if consent:
        console.print(_literal("  consent required: " + ", ".join(consent), "bold"))
    # Exhaustive: every field of the card (everything its disclosure digest
    # covers), literally and unwrapped, so nothing approved is off-screen.
    from sessionfs.skills.disclosure import card_leaves

    console.print("[bold]Every disclosed field[/bold]")
    for path, value in card_leaves(card):
        console.print(_literal(f"  {path} = {value}"), soft_wrap=True)


def _print_diff(diff: dict[str, Any], *, max_files: int = 30, show_text: bool = True) -> None:
    """Render an update diff (marketplace v2 §3.4)."""
    update_class = str(diff.get("update_class"))
    style = {"widening": "red bold", "code": "yellow bold"}.get(update_class, "green")
    console.print(Text.assemble(("Update class: ", "bold"), _literal(update_class, style)))
    rule = (diff.get("consent") or {}).get("description")
    if rule:
        console.print(_literal(f"  {rule}", "dim"))
    for entry in diff.get("widens") or []:
        console.print(_literal(f"  + {entry.get('kind')}: {entry.get('item')}  (NEW ACCESS)", "red bold"))
    for entry in diff.get("code_changes") or []:
        console.print(_literal(f"  ~ {entry.get('kind')} {entry.get('change')}: {entry.get('path')}", "yellow"))
    for entry in diff.get("narrows") or []:
        console.print(_literal(f"  - {entry.get('kind')}: {entry.get('item')}", "green"))
    files = diff.get("files") or {}
    if files.get("unknown"):
        console.print("  [yellow]file changes unknown (the installed version is not cached)[/yellow]")
    else:
        counts = ", ".join(f"{len(files.get(key) or [])} {key}" for key in ("added", "changed", "removed"))
        console.print(_literal(f"  files: {counts}"))
        listed = 0
        for key, mark in (("added", "+"), ("changed", "~"), ("removed", "-")):
            for path in files.get(key) or []:
                if listed >= max_files:
                    break
                console.print(_literal(f"    {mark} {path}", "dim"))
                listed += 1
    new_unicode = (diff.get("unicode_delta") or {}).get("new_findings") or []
    for finding in new_unicode[:10]:
        console.print(_literal(
            f"  NEW invisible character {finding.get('visible') or ''} at "
            f"{finding.get('path')}:{finding.get('line')}:{finding.get('column')} ({finding.get('class')})",
            "red bold",
        ))
    scan = diff.get("scan_delta") or {}
    if scan.get("old_max_severity") != scan.get("new_max_severity"):
        console.print(_literal(
            f"  scanner worst finding: {scan.get('old_max_severity')} -> {scan.get('new_max_severity')}"
        ))
    texts = (diff.get("text_diffs") or {}).get("entries") or []
    if show_text and texts:
        console.print("[bold]Changes[/bold]")
        for entry in texts:
            if entry.get("binary"):
                hashes = ""
                old_hash = entry.get("old_sha256")
                new_hash = entry.get("new_sha256")
                if old_hash or new_hash:
                    hashes = f" ({old_hash or 'none'} -> {new_hash or 'none'})"
                console.print(_literal(f"  {entry.get('path')}: binary file {entry.get('change')}{hashes}", "dim"))
            elif entry.get("diff"):
                console.print(_literal(entry["diff"]))
        if (diff.get("text_diffs") or {}).get("truncated"):
            console.print("[yellow]  ... diff truncated[/yellow]")


def _validate_targets(tools: list[str]) -> list[str]:
    unknown = sorted(set(tools) - set(SUPPORTED_TOOLS))
    if unknown:
        raise typer.BadParameter(
            f"Unknown tool(s): {', '.join(unknown)}. Supported: {', '.join(SUPPORTED_TOOLS)}"
        )
    return tools


@skill_app.command("inspect")
@handle_errors
def inspect_skill(
    source: str = typer.Argument(
        ...,
        help="Local path, Git URL, marketplace/package, or sessionfs:<namespace>/<name>[@version]",
    ),
    pin: str | None = typer.Option(None, "--pin", help="Git ref or commit to resolve"),
    subdir: str = typer.Option("", "--subdir", help="Package subdirectory in the source"),
    scope: str = typer.Option("project", "--scope", help="project or user identity"),
    tools: list[str] = typer.Option([], "--for", help="Tool(s) to disclose (repeatable; default all)"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Inspect and validate a package without installing it into a tool."""
    selected = _validate_targets(tools)
    manager = SkillManager()
    # The disclosure is computed while the inspected copy still exists (it
    # lives in a temporary store that is removed when inspection returns).
    holder: dict[str, Any] = {}

    def _inspect() -> Any:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(prefix="sfs-skill-inspect-") as tmp:
            package = manager.import_source(
                source, pin=pin, subdir=subdir, scope=scope, _store_root=Path(tmp)
            )
            holder["disclosure"] = manager.disclose(package, targets=selected, scope=scope)
            return package

    package = _call(_inspect)
    card = holder["disclosure"]
    if json_output:
        _print_json({**package.as_dict(include_path=False), "disclosure": card})
        return
    console.print(Text.assemble(_literal(package.name, "bold cyan"), f"  {package.content_hash}"))
    console.print(Text.assemble(f"Package: {package.package_id}  Origin: ", _literal(package.origin_format)))
    console.print(
        f"Skills: {len(package.skills)}  MCP: {'yes' if package.has_mcp else 'no'}  "
        f"Risky: {'yes' if package.risky else 'no'}"
    )
    _print_disclosure(card)


@skill_app.command("add")
@handle_errors
def add_skill(
    source: str = typer.Argument(
        ...,
        help="Local path, Git URL, marketplace/package, or sessionfs:<namespace>/<name>[@version]",
    ),
    tools: list[str] = typer.Option([], "--for", help="Target tool (repeatable; default all)"),
    scope: str = typer.Option("project", "--scope", help="project or user"),
    pin: str | None = typer.Option(None, "--pin", help="Git ref or commit to resolve"),
    subdir: str = typer.Option("", "--subdir", help="Package subdirectory in the source"),
    allow_scripts: bool = typer.Option(
        False, "--allow-scripts", help="Approve skill scripts for this exact package content"
    ),
    enable_mcp: bool = typer.Option(
        False, "--enable-mcp", help="Reserved; local MCP activation is not implemented"
    ),
    allow_executable_extensions: bool = typer.Option(
        False,
        "--allow-executable-extensions",
        help="Approve hooks.json/hooks/ executable extensions shipped inside a skill",
    ),
    allow_hooks: bool = typer.Option(
        False,
        "--allow-hooks",
        help="Approve SKILL.md frontmatter hooks / allowed-tools for this exact content",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the displayed plan"),
    allow_dlp_findings: bool = typer.Option(
        False,
        "--allow-dlp-findings",
        help="Accept the package's redacted DLP findings for this exact content",
    ),
    allow_hidden_unicode: bool = typer.Option(
        False,
        "--allow-hidden-unicode",
        help="Install even though files contain hidden / bidirectional / control characters",
    ),
) -> None:
    """Import and install a package with explicit risky-capability consent."""
    selected = _validate_targets(tools)
    manager = SkillManager()
    package = _call(
        lambda: manager.import_source(
            source, pin=pin, subdir=subdir, scope=scope, allow_dlp_findings=allow_dlp_findings
        )
    )
    if package.dlp_findings:
        console.print(_literal("DLP findings (redacted): " + SkillManager.describe_dlp_findings(package), "yellow bold"))
        if allow_dlp_findings:
            console.print(_literal("  allowed by --allow-dlp-findings (disclosed in state as dlp/allowed-by-user)", "yellow"))
    # `add` is the ONLY caller where an omitted `--for` means "all tools".
    try:
        plans = manager.plan(package, targets=selected, scope=scope, default_all=True)
    except SkillError as exc:
        # A hosted package that cannot be translated counts as a translation
        # failure, for exactly the tools whose own plan fails (best effort).
        # Only a planning error counts: never Ctrl+C or an exit.
        _report_hosted_usage(
            package, "translation_failure",
            _failed_tools(manager, package, selected or list(SUPPORTED_TOOLS), scope),
        )
        raise typer.BadParameter(str(exc)) from exc
    # Disclosure BEFORE any consent decision: what lands where, what can run,
    # where it connects, scanner and hidden-character findings.
    manifest = _call(lambda: manager.capability_manifest(package))
    _print_disclosure(
        manager.disclose(package, targets=[plan.tool for plan in plans], scope=scope, manifest=manifest)
    )
    _call(lambda: manager.enforce_hidden_unicode(manifest, allow_hidden_unicode=allow_hidden_unicode))
    _call(
        lambda: manager._enforce_consent(
            plans,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            package=package,
        )
    )
    _print_plans(plans)
    # The per-capability flags gate the CAPABILITY; `--yes` gates the PLAN.
    # They are NOT interchangeable: a package carrying scripts / hooks /
    # executable extensions / DLP findings needs MORE review than a pure
    # instruction package, never less (design 7.3). Treating the capability
    # flags as plan confirmation inverted that and auto-installed exactly the
    # risky packages under a closed stdin.
    if not confirm_or_exit(
        f"Install {_literal(package.name).plain} at {package.content_hash}?",
        yes=yes,
    ):
        raise typer.Abort()
    from sessionfs.skills import hosted

    # One mutation in the global lock order (scope -> operation -> hosted):
    # an explicit add is the user's choice, so `sfs skill pull` may no longer
    # uninstall it (its records keep protecting it as plain references).
    with manager.explicit_change(scope):
        _call(
            lambda: manager.install_prepared(
                package,
                plans,
                allow_scripts=allow_scripts,
                enable_mcp=enable_mcp,
                allow_executable_extensions=allow_executable_extensions,
                allow_hooks=allow_hooks,
                allow_dlp_findings=allow_dlp_findings,
                consent_context=manager.consent_snapshot(package, manifest),
                # A new install id in the SAME commit: earlier pull records
                # (and pull operations) lose any claim even if the cleanup
                # below never runs.
                rotate_install_id=True,
            )
        )
        hosted.retire_pull_authority(manager, scope, package.package_id)
        # Adding it explicitly is also an explicit opt-in.
        hosted.clear_opt_outs(manager, scope, local_package_id=package.package_id)
    console.print(
        Text.assemble(
            ("Installed ", "green"),
            _literal(package.name, "green"),
            f" ({package.package_id}) at {package.content_hash}",
        )
    )
    _report_hosted_usage(package, "install", [plan.tool for plan in plans])
    deprecation = _hosted_deprecation(package)
    if deprecation:
        console.print(_literal(deprecation, "yellow"))


def _hosted_ids(source: Any) -> tuple[str, str] | None:
    """(hosted package id, version id) of a hosted source, else None."""
    from sessionfs.skills import hosted

    package_id = hosted.hosted_package_id(source)
    if package_id is None:
        return None
    commit = source.get("commit") if isinstance(source, dict) else getattr(source, "commit", None)
    return (package_id, str(commit)) if commit else None


def _report_hosted_usage(package: Any, event: str, tools: list[str], *, source: str = "download") -> None:
    """Best-effort marketplace install count for a hosted package (never
    fails the command; see SFS_SKILL_USAGE_REPORTING)."""
    from sessionfs.skills import hosted

    try:
        ids = _hosted_ids(getattr(package, "source", None))
        if ids is None or not tools:
            return
        client = hosted.client_from_auth()
        if client is not None:
            # Interactive: on a terminal the one-time install-count notice
            # is shown right before the first count.
            client.report_usage(ids[1], event, tools, source=source, interactive=True)
    except Exception:  # noqa: BLE001 - counting must never break an install
        return


def _failed_tools(manager: Any, package: Any, tools: list[str], scope: str) -> list[str]:
    """The tools whose individual plan fails with a SkillError."""
    if _hosted_ids(getattr(package, "source", None)) is None:
        return []
    failed = []
    for tool in tools:
        try:
            manager.plan(package, targets=[tool], scope=scope)
        except SkillError:
            failed.append(tool)
        except Exception:  # noqa: BLE001 - unknown failures are not translation failures
            continue
    return failed


def _report_row_usage(row: dict[str, Any], event: str) -> None:
    """Best-effort count for an installed row (e.g. an uninstall)."""
    from types import SimpleNamespace

    targets = [str(tool) for tool in row.get("targets") or []]
    _report_hosted_usage(SimpleNamespace(source=row.get("source")), event, targets)


def _hosted_deprecation(package: Any) -> str | None:
    """A warning line when the installed hosted package is deprecated."""
    from sessionfs.skills import hosted

    try:
        ids = _hosted_ids(getattr(package, "source", None))
        client = hosted.client_from_auth() if ids else None
        if ids is None or client is None:
            return None
        item = client.get_package(ids[0])
    except Exception:  # noqa: BLE001 - informational only
        return None
    deprecation = item.get("deprecation") if isinstance(item, dict) else None
    if not isinstance(deprecation, dict):
        return None
    line = f"Warning: {package.name} is deprecated by its publisher"
    if deprecation.get("message"):
        line += f": {deprecation['message']}"
    successor = deprecation.get("successor")
    if isinstance(successor, dict) and successor.get("namespace") and successor.get("name"):
        line += f" (successor: sessionfs:{successor['namespace']}/{successor['name']})"
    return _clean(line)


@skill_app.command("list")
@handle_errors
def list_skills(
    scope: str | None = typer.Option(None, "--scope", help="project or user"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """List locally installed packages."""
    manager = SkillManager()
    rows = _call(lambda: manager.list_installed(scope))
    if json_output:
        _print_json(rows)
        return
    if not rows:
        console.print("[dim]No installed skills.[/dim]")
        return
    table = Table(title="Installed skills")
    table.add_column("Name", style="cyan")
    table.add_column("Package")
    table.add_column("Scope")
    table.add_column("Targets")
    table.add_column("Hash")
    for row in rows:
        table.add_row(
            _literal(row.get("name", "")),
            str(row.get("package_id", "")),
            str(row.get("scope", "")),
            ", ".join(str(item) for item in (row.get("targets") or [])),
            str(row.get("content_hash", ""))[:20] + "...",
        )
    console.print(table)
    _print_revocation_warnings(manager, scope)


@skill_app.command("show")
@handle_errors
def show_skill(
    identifier: str = typer.Argument(..., help="Package name or package_id"),
    scope: str | None = typer.Option(None, "--scope", help="project or user"),
) -> None:
    """Show local package state, provenance, plans, and omissions. For a
    hosted package, also its marketplace freshness (stale, unmaintained,
    deprecated, last updated and last verified per tool) when signed in."""
    row = _call(lambda: SkillManager().find_installed(identifier, scope=scope))
    marketplace = _marketplace_freshness(row)
    _print_json({**row, "marketplace": marketplace} if marketplace is not None else row)


def _marketplace_freshness(row: dict[str, Any]) -> dict[str, Any] | None:
    from sessionfs.skills import hosted

    ids = _hosted_ids(row.get("source"))
    if ids is None:
        return None
    client = hosted.client_from_auth()
    if client is None:
        return {"available": False, "reason": "not signed in"}
    try:
        item = client.get_package(ids[0])
    except SkillError as exc:
        return {"available": False, "reason": str(exc)}
    return _clean({
        "available": True,
        "package_id": ids[0],
        "installed_version_id": ids[1],
        "current_version_id": item.get("version_id"),
        "update_available": bool(item.get("version_id")) and item.get("version_id") != ids[1],
        "categories": item.get("categories") or [],
        "freshness": item.get("freshness"),
        "deprecation": item.get("deprecation"),
        "installs": item.get("installs"),
    })


@skill_app.command("plan")
@handle_errors
def plan_skill(
    identifier: str = typer.Argument(..., help="Installed package name or package_id"),
    tools: list[str] = typer.Option(
        [],
        "--for",
        help="Add a target tool (repeatable, ADDITIVE: the installed targets are kept)",
    ),
    drop: list[str] = typer.Option(
        [], "--drop", help="Model removing a target tool (repeatable)"
    ),
    scope: str = typer.Option("project", "--scope", help="project or user"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Compute a deterministic, side-effect-free install plan.

    `plan` is the pre-mutation preview of `compile`, so it resolves `--for` /
    `--drop` through the SAME `compile_targets` resolver: `--for` is ADDITIVE
    over the installed targets and `--drop` is the only way to shrink the set.
    A preview that modelled `--for` as a REPLACEMENT would show one tool while
    the compile it previews wrote three.
    """
    selected = _validate_targets(tools)
    dropped = _validate_targets(drop)
    manager = SkillManager()
    package = _call(lambda: manager.load_installed_package(identifier, scope=scope))
    current = _call(lambda: manager.find_installed(identifier, scope=scope))
    targets = _call(
        lambda: SkillManager.compile_targets(
            [str(item) for item in (current.get("targets") or [])], add=selected, drop=dropped
        )
    )
    plans = _call(lambda: manager.plan(package, targets=targets, scope=scope))
    if json_output:
        _print_json([item.as_dict() for item in plans])
    else:
        _print_plans(plans)


@skill_app.command("compile")
@handle_errors
def compile_skill(
    identifier: str = typer.Argument(..., help="Installed package name or package_id"),
    tools: list[str] = typer.Option(
        [],
        "--for",
        help="Add a target tool (repeatable, ADDITIVE: the installed targets are kept)",
    ),
    drop: list[str] = typer.Option(
        [],
        "--drop",
        help=(
            "Remove a target tool (repeatable); lists the managed files that will be "
            "unlinked and asks for confirmation (--yes in non-interactive mode)"
        ),
    ),
    scope: str = typer.Option("project", "--scope", help="project or user"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan without writing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the removal listed by --drop"),
    allow_scripts: bool = typer.Option(False, "--allow-scripts"),
    enable_mcp: bool = typer.Option(False, "--enable-mcp"),
    allow_executable_extensions: bool = typer.Option(
        False, "--allow-executable-extensions"
    ),
    allow_hooks: bool = typer.Option(False, "--allow-hooks"),
    allow_dlp_findings: bool = typer.Option(
        False, "--allow-dlp-findings", help="Accept the package's redacted DLP findings for this exact content"
    ),
    allow_hidden_unicode: bool = typer.Option(
        False, "--allow-hidden-unicode", help="Add tools even though files contain hidden / control characters"
    ),
) -> None:
    """Re-apply an installed package; `--for` adds tools, `--drop` removes them.

    The installed targets are always kept: `--for codex` on a package installed
    for three tools re-applies all three plus codex. Only `--drop TOOL` shrinks
    the set, after showing the files that will be removed and confirming.
    """
    selected = _validate_targets(tools)
    dropped = _validate_targets(drop)
    manager = SkillManager()
    package = _call(lambda: manager.load_installed_package(identifier, scope=scope))
    current = _call(lambda: manager.find_installed(identifier, scope=scope))
    targets = _call(
        lambda: SkillManager.compile_targets(
            [str(item) for item in (current.get("targets") or [])], add=selected, drop=dropped
        )
    )
    plans = _call(lambda: manager.plan(package, targets=targets, scope=scope))
    if dropped:
        removed = SkillManager.outputs_dropped_by(current, plans)
        console.print(
            Text.assemble(
                ("Dropping ", "yellow"),
                _literal(", ".join(dropped), "yellow"),
                (f" removes {len(removed)} managed file(s):", "yellow"),
            )
        )
        for path in removed:
            console.print(_literal("  " + path))
        if not dry_run and not confirm_or_exit(
            f"Remove these files and drop {', '.join(dropped)}?", yes=yes
        ):
            raise typer.Abort()
    if not dry_run:
        _, plans, _ = _call(
            lambda: manager.compile(
                identifier,
                scope=scope,
                targets=selected,
                drop=dropped,
                allow_scripts=allow_scripts,
                enable_mcp=enable_mcp,
                allow_executable_extensions=allow_executable_extensions,
                allow_hooks=allow_hooks,
                allow_dlp_findings=allow_dlp_findings,
                allow_hidden_unicode=allow_hidden_unicode,
            )
        )
    _print_plans(plans)


@skill_app.command("sync")
@handle_errors
def sync_skills(
    locked: bool = typer.Option(False, "--locked", help="Restore exact locked versions"),
    scope: str = typer.Option("project", "--scope", help="project or user"),
    allow_scripts: bool = typer.Option(False, "--allow-scripts"),
    enable_mcp: bool = typer.Option(False, "--enable-mcp"),
    allow_executable_extensions: bool = typer.Option(
        False, "--allow-executable-extensions"
    ),
    allow_hooks: bool = typer.Option(False, "--allow-hooks"),
    allow_dlp_findings: bool = typer.Option(
        False, "--allow-dlp-findings", help="Accept the package's redacted DLP findings for this exact content"
    ),
    approve_widening: bool = typer.Option(
        False, "--approve-widening", help="Approve a version that asks for MORE access than you consented to"
    ),
    allow_hidden_unicode: bool = typer.Option(
        False, "--allow-hidden-unicode", help="Accept hidden / bidirectional / control characters"
    ),
) -> None:
    """Restore exact desired state from the lockfile.

    Each installed version the lockfile replaces is classified like an
    update (text / code / widening) before anything changes; one refused
    transition refuses the whole sync."""
    if not locked:
        raise typer.BadParameter("Only reproducible `sfs skill sync --locked` is supported")
    rows = _call(
        lambda: SkillManager().sync(
            scope=scope,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            approve_widening=approve_widening,
            allow_hidden_unicode=allow_hidden_unicode,
        )
    )
    removed = [row for row in rows if row.get("action") == "removed"]
    console.print(
        f"[green]Synchronized {len(rows) - len(removed)} locked package(s); "
        f"removed {len(removed)} package(s) absent from the lockfile.[/green]"
    )
    for row in removed:
        for path in row.get("dirty") or []:
            console.print(_literal(f"Preserved modified file: {path}", "yellow"))


@skill_app.command("remove")
@handle_errors
def remove_skill(
    identifier: str = typer.Argument(..., help="Package name or package_id"),
    scope: str = typer.Option("project", "--scope", help="project or user"),
) -> None:
    """Remove unchanged managed outputs; preserve dirty files."""
    from sessionfs.skills import hosted

    manager = SkillManager()
    # One mutation in the global lock order (scope -> operation -> hosted):
    # the user removed it themselves, so pull loses its authority over it and
    # optional assignments of this version are opted out.
    with manager.explicit_change(scope):
        row = _call(lambda: manager.find_installed(identifier, scope=scope))
        # Pending opt-outs are written BEFORE the removal (crash-safe), then
        # confirmed - or rolled back if the removal fails.
        keys = hosted.begin_opt_out(manager, scope, str(row["package_id"]))
        try:
            result = _call(lambda: manager.remove(identifier, scope=scope))
        except BaseException:
            hosted.finish_opt_out(manager, scope, keys, removed=False)
            raise
        hosted.finish_opt_out(manager, scope, keys, removed=True)
    _report_row_usage(row, "uninstall")
    console.print(f"[green]Removed {len(result['removed'])} managed file(s).[/green]")
    for path in result["dirty"]:
        console.print(_literal(f"Preserved modified file: {path}", "yellow"))


def _print_update_preview(prepared: Any) -> None:
    """Show everything a reviewer needs BEFORE new upstream content is applied."""
    package = prepared.package
    current = prepared.current
    old_source = current.get("source") if isinstance(current.get("source"), dict) else {}
    console.print(
        Text.assemble(
            ("Update preview: ", "bold"),
            _literal(package.name, "bold"),
            f" ({current.get('package_id')})",
        )
    )
    console.print(f"  content hash: {current.get('content_hash')} -> [bold]{package.content_hash}[/bold]")
    if str(current.get("package_id")) != package.package_id:
        console.print(f"  package id:   {current.get('package_id')} -> {package.package_id}")
    console.print(
        Text.assemble(
            f"  source:       {package.source.source_type} ",
            _literal(package.source.locator),
            _literal(f" @ {package.source.subdir}" if package.source.subdir else ""),
        )
    )
    console.print(
        f"  commit:       {old_source.get('commit') or 'n/a'} -> {package.source.commit or 'n/a'}"
    )
    if package.content_hash == current.get("content_hash"):
        console.print("  [dim]content is unchanged; targets will be re-applied[/dim]")
    _print_plans(prepared.plans)
    console.print("[bold]Capability diff[/bold]")
    if not prepared.escalations and not prepared.dropped_capabilities:
        console.print(
            "  unchanged: " + (", ".join(prepared.new_capabilities) or "none required")
        )
    for capability in prepared.escalations:
        console.print(f"  [red bold]+ {capability}  (NEW - escalation, review the new content)[/red bold]")
    for capability in prepared.dropped_capabilities:
        console.print(f"  [green]- {capability}  (no longer required)[/green]")
    degradations = [
        (plan.tool, item.component, item.reason, item.transform)
        for plan in prepared.plans
        for item in plan.degradations
    ]
    package_degradations = [
        ("import", component, reason, transform)
        for component, reason, transform in package.degradations
    ]
    if degradations or package_degradations:
        console.print("[bold]Degradations[/bold]")
        for tool, component, reason, transform in [*package_degradations, *degradations]:
            console.print(_literal(f"  {tool}: {component} - {reason} ({transform})", "yellow"))
    if getattr(prepared, "diff", None):
        _print_diff(prepared.diff)


@skill_app.command("update")
@handle_errors
def update_skill(
    identifier: str = typer.Argument(..., help="Package name or package_id"),
    scope: str = typer.Option("project", "--scope", help="project or user"),
    pin: str | None = typer.Option(None, "--pin", help="New Git ref or commit"),
    allow_scripts: bool = typer.Option(False, "--allow-scripts"),
    enable_mcp: bool = typer.Option(False, "--enable-mcp"),
    allow_executable_extensions: bool = typer.Option(
        False, "--allow-executable-extensions"
    ),
    allow_hooks: bool = typer.Option(False, "--allow-hooks"),
    allow_dlp_findings: bool = typer.Option(
        False, "--allow-dlp-findings", help="Accept the package's redacted DLP findings for this exact content"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the displayed update"),
    rebind_origin: bool = typer.Option(
        False,
        "--rebind-origin",
        help="Bind a hosted package installed before its server was recorded to the ACTIVE server",
    ),
    approve_widening: bool = typer.Option(
        False,
        "--approve-widening",
        help="Approve an update that asks for MORE access (never implied by --yes)",
    ),
    allow_hidden_unicode: bool = typer.Option(
        False,
        "--allow-hidden-unicode",
        help="Update even though files contain hidden / bidirectional / control characters",
    ),
) -> None:
    """Fetch a new source version, show what would change, confirm, then apply.

    Re-consent follows the update class (marketplace v2 D2): text-only
    updates need one confirmation and keep the consent you gave; script,
    hook or MCP-config changes need the changed capability's flag again;
    updates that widen access always stop and need --approve-widening (or an
    interactive yes to the widening prompt)."""
    manager = SkillManager()
    # Prepare: fetch + plan only. Nothing is written to adapter roots or state
    # until the reviewer has seen the new hash, commit, plan and capability
    # diff - a compromised upstream must never land (or receive consent for a
    # new hash) without review.
    prepared = _call(
        lambda: manager.prepare_update(
            identifier, scope=scope, pin=pin, rebind_origin=rebind_origin
        )
    )
    _print_update_preview(prepared)
    _call(
        lambda: manager.enforce_hidden_unicode(
            prepared.new_manifest or {}, allow_hidden_unicode=allow_hidden_unicode
        )
    )
    widening_ok = approve_widening
    if prepared.update_class == "widening" and not widening_ok:
        # A widening update ALWAYS stops and asks. --yes and receipts for the
        # installed version never answer this question.
        widening_ok = confirm_or_exit(
            "This update asks for MORE access than you approved (listed above). Approve it?",
            yes=False,
            yes_hint="Pass --approve-widening to approve the new access in non-interactive mode.",
        )
        if not widening_ok:
            raise typer.Abort()
    flags, consent_context = _call(
        lambda: manager.update_consent(
            prepared,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            approve_widening=widening_ok,
        )
    )
    if prepared.package.dlp_findings:
        console.print(_literal("DLP findings (redacted): " + SkillManager.describe_dlp_findings(prepared.package), "yellow bold"))
    # Unlike add, capability flags alone do not confirm an update: they were
    # given before the new content was seen. --yes (or an interactive answer)
    # is the only confirmation; non-interactive without --yes refuses.
    if not confirm_or_exit(
        f"Update {_literal(prepared.package.name).plain} "
        f"{prepared.current.get('content_hash')} -> {prepared.package.content_hash}?",
        yes=yes,
    ):
        raise typer.Abort()
    _call(lambda: manager.apply_update(prepared, consent_context=consent_context, **flags))
    console.print(
        Text.assemble(
            ("Updated ", "green"),
            _literal(prepared.package.name, "green"),
            f" ({prepared.package.package_id}) to {prepared.package.content_hash}",
        )
    )


@skill_app.command("diff")
@handle_errors
def diff_skill(
    identifier: str = typer.Argument(..., help="Installed package name or package_id"),
    to: str | None = typer.Option(
        None, "--to", help="Version to compare with (hosted version label/id or Git ref; default latest)"
    ),
    scope: str = typer.Option("project", "--scope", help="project or user"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
    rebind_origin: bool = typer.Option(False, "--rebind-origin"),
) -> None:
    """Show what an update would change (files, access, scanner findings) without applying it."""
    prepared = _call(
        lambda: SkillManager().prepare_update(identifier, scope=scope, pin=to, rebind_origin=rebind_origin)
    )
    diff = prepared.diff or {}
    if json_output:
        _print_json(
            {
                "package_id": prepared.current.get("package_id"),
                "from_content_hash": prepared.current.get("content_hash"),
                "to_content_hash": prepared.package.content_hash,
                **diff,
            }
        )
        return
    console.print(
        Text.assemble(
            ("Diff: ", "bold"),
            _literal(prepared.package.name, "bold"),
            f"  {prepared.current.get('content_hash')} -> {prepared.package.content_hash}",
        )
    )
    _print_diff(diff)


_BADGE_STYLES = {
    "verified": "green",
    "format-valid": "cyan",
    "regressed": "red",
    "failed": "red",
    "format-invalid": "red",
    "not-applicable": "dim",
    "unverified": "dim",
}


@skill_app.command("works-in")
@handle_errors
def works_in(
    identifier: str = typer.Argument(..., help="Hosted package id (spk_...) or version id (skv_...)"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Show which tools a hosted package is verified to work in.

    "verified" comes only from the works-in harness (the tool's own
    installer accepted it); "format-valid" means the generated catalog entry
    matches the tool's documented format; "expected" is adapter-computed."""
    from sessionfs.skills import hosted

    client = _call(lambda: hosted.require_client(hosted.client_from_auth()))
    version_id = identifier
    if identifier.startswith("spk_"):
        version_id = str(_call(lambda: client.get_package(identifier)).get("version_id", ""))
    body = _call(lambda: client.works_in(version_id))
    if json_output:
        _print_json(body)
        return
    table = Table(title=f"Works in ({version_id})")
    for column in ("Tool", "Badge", "Level", "Tool version", "Checked", "Detail"):
        table.add_column(column)
    for tool, entry in sorted(body.get("tools", {}).items()):
        levels = entry.get("levels") or {}
        best = next((levels[level] | {"level": level} for level in ("L2", "L1", "L0") if level in levels), None)
        badge = str(entry.get("badge", "unverified"))
        table.add_row(
            _literal(tool),
            Text(badge, style=_BADGE_STYLES.get(badge, "")),
            Text(best["level"] if best else "-"),
            _literal(best.get("tool_version", "-") if best else "-"),
            Text(str(best.get("run_at", "-"))[:10] if best else "-"),
            _literal(str(best.get("reason", ""))[:80] if best else ""),
        )
    for tool, entry in sorted(body.get("expected_only", {}).items()):
        omitted = ", ".join(entry.get("omitted_components") or []) or "-"
        table.add_row(_literal(tool), Text("expected", style="dim"), Text("-"), Text("-"), Text("-"),
                      _literal(f"not yet verified; omitted: {omitted}"))
    console.print(table)


@skill_app.command("rollback")
@handle_errors
def rollback_skill(
    identifier: str = typer.Argument(..., help="Package name or package_id"),
    content_hash: str = typer.Option(..., "--to", help="Cached content hash"),
    scope: str = typer.Option("project", "--scope", help="project or user"),
    allow_scripts: bool = typer.Option(False, "--allow-scripts"),
    enable_mcp: bool = typer.Option(False, "--enable-mcp"),
    allow_executable_extensions: bool = typer.Option(
        False, "--allow-executable-extensions"
    ),
    allow_hooks: bool = typer.Option(False, "--allow-hooks"),
    allow_dlp_findings: bool = typer.Option(
        False, "--allow-dlp-findings", help="Accept the package's redacted DLP findings for this exact content"
    ),
    approve_widening: bool = typer.Option(
        False, "--approve-widening", help="Approve a version that asks for MORE access than you consented to"
    ),
    allow_hidden_unicode: bool = typer.Option(
        False, "--allow-hidden-unicode", help="Accept hidden / bidirectional / control characters"
    ),
) -> None:
    """Re-apply a cached immutable package version (classified and consented like an update)."""
    row = _call(
        lambda: SkillManager().rollback(
            identifier,
            content_hash,
            scope=scope,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            approve_widening=approve_widening,
            allow_hidden_unicode=allow_hidden_unicode,
        )
    )
    console.print(
        Text.assemble(("Rolled back ", "green"), _literal(row["name"], "green"), (f" to {row['content_hash']}", "green"))
    )


def _print_unassigned(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        console.print(
            _literal(
                f"{entry['name']} ({entry['scope']}) is no longer assigned - run "
                f"`sfs skill remove {entry['package_id']} --scope {entry['scope']}` to remove it",
                "yellow",
            )
        )


def _print_unassigned_tools(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        tools = [str(tool) for tool in entry.get("tools") or []]
        console.print(
            _literal(
                f"{entry.get('name') or entry.get('package_id')} ({entry.get('scope')}): "
                f"{', '.join(tools)} no longer assigned - still installed; run "
                f"`sfs skill compile {entry.get('package_id')} --scope {entry.get('scope')} "
                + " ".join(f"--drop {tool}" for tool in tools) + "` to remove "
                + ("it" if len(tools) == 1 else "them"),
                "yellow",
            )
        )


@skill_app.command("doctor")
@handle_errors
def doctor_skills(
    scope: str | None = typer.Option(None, "--scope", help="project or user"),
) -> None:
    """Report missing or modified managed outputs and revoked hosted versions."""
    manager = SkillManager()
    drift = _call(lambda: manager.doctor(scope))
    # Yanked versions warn; a BLOCKED version still installed fails doctor
    # (`sfs skill pull` removes it).
    revoked = _print_revocation_warnings(manager, scope)
    from sessionfs.skills import hosted

    # Quarantined blocked files that are active in a tool directory again.
    trash = manager.trash_usage()
    if trash["files"]:
        console.print(
            _literal(
                f"Trash: {trash['files']} removed file(s), {trash['bytes'] / 1024:.1f} KiB in "
                f"{trash['path']} (kept {trash['retention_days']} days)",
                "dim",
            )
        )
    # Managed packages no longer assigned: informational, never removed.
    _print_unassigned(hosted.unassigned_packages(manager, scope))
    leftovers = hosted.quarantine_leftovers(manager, scope)
    for item in leftovers:
        console.print(
            _literal(
                f"BLOCKED file active again: {item['path']} ({item['name']}, quarantined copy "
                f"in {item['quarantine_dir']})",
                "red bold",
            )
        )
    if leftovers or any(item["state"] == "blocked" for item in revoked):
        console.print(
            "[red]Blocked content is still active: `sfs skill pull` removes installed blocked "
            "packages; delete any file reported as active again by hand.[/red]"
        )
        if not drift:
            raise typer.Exit(1)
    if not drift:
        console.print("[green]Skill state is consistent.[/green]")
        return
    for item in drift:
        console.print(
            Text.assemble(
                (f"{item['scope']} drift", "yellow"),
                _literal(f" {item['path']} ({item['actual']} != {item['expected']})"),
            )
        )
    raise typer.Exit(1)


@marketplace_app.command("add")
@handle_errors
def add_marketplace(
    source: str = typer.Argument(..., help="HTTPS/SSH Git marketplace repository"),
    name: str = typer.Option(..., "--name", help="Scoped marketplace alias"),
    scope: str = typer.Option("user", "--scope", help="user or project"),
    pin: str | None = typer.Option(None, "--pin", help="Marketplace Git ref or commit"),
) -> None:
    """Register another immutable external marketplace catalog."""
    registry = _call(lambda: SkillManager().registries.add(name, source, scope=scope, pin=pin))
    console.print(
        f"[green]Added {registry.alias}[/green] ({registry.scope}) at {registry.commit} "
        f"with {len(registry.packages)} package(s), {len(registry.unsupported)} unsupported"
    )
    for item in registry.unsupported:
        console.print(_literal(f"  unsupported {item.get('name')}: {item.get('reason')}", "yellow"))


@marketplace_app.command("list")
@handle_errors
def list_marketplaces(
    scope: str | None = typer.Option(None, "--scope", help="user or project"),
) -> None:
    """List every registered marketplace without hidden precedence."""
    rows = _call(lambda: SkillManager().registries.list(scope))
    if not rows:
        console.print("[dim]No marketplace registries.[/dim]")
        return
    table = Table(title="Marketplace registries")
    table.add_column("Alias", style="cyan")
    table.add_column("Scope")
    table.add_column("Packages", justify="right")
    table.add_column("Unsupported", justify="right")
    table.add_column("Commit")
    table.add_column("Source", overflow="fold")  # never hide a source behind an ellipsis
    for row in rows:
        table.add_row(
            row.alias, row.scope, str(len(row.packages)), str(len(row.unsupported)), row.commit[:12], _literal(row.source)
        )
    console.print(table)


@marketplace_app.command("refresh")
@handle_errors
def refresh_marketplace(
    name: str = typer.Argument(..., help="Marketplace alias"),
    scope: str = typer.Option("user", "--scope", help="user or project"),
) -> None:
    """Refresh discovery metadata without updating installed packages."""
    registry = _call(lambda: SkillManager().registries.refresh(name, scope=scope))
    console.print(
        f"[green]Refreshed {registry.alias}[/green] at {registry.commit}: "
        f"{len(registry.packages)} package(s), {len(registry.unsupported)} unsupported; "
        f"installed packages were not changed"
    )


@marketplace_app.command("remove")
@handle_errors
def remove_marketplace(
    name: str = typer.Argument(..., help="Marketplace alias"),
    scope: str = typer.Option("user", "--scope", help="user or project"),
) -> None:
    """Remove an unreferenced registry."""
    _call(lambda: SkillManager().remove_registry(name, scope=scope))
    console.print(f"[green]Removed marketplace {name} ({scope}).[/green]")


@skill_app.command("search")
@handle_errors
def search_marketplaces(
    query: str = typer.Argument(..., help="Package name or description text"),
    marketplace: str | None = typer.Option(None, "--marketplace", help="Alias filter"),
    tool: str | None = typer.Option(
        None, "--tool", help="Hosted results only: packages compatible with this tool"
    ),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Hosted result limit"),
    local_only: bool = typer.Option(
        False, "--local-only", help="Skip the hosted SessionFS Marketplace"
    ),
    category: str | None = typer.Option(None, "--category", help="Hosted: category slug (see `sfs skill categories`)"),
    publisher: str | None = typer.Option(None, "--publisher", help="Hosted: publisher namespace or id"),
    verified: bool = typer.Option(False, "--verified", help="Hosted: verified publishers only"),
    attested: bool = typer.Option(False, "--attested", help="Hosted: verified publication provenance only"),
    license_filter: str | None = typer.Option(None, "--license", help="Hosted: declared license (e.g. MIT), or 'none'"),
    max_severity: str | None = typer.Option(
        None, "--max-severity", help="Hosted: worst open scan finding allowed (none, low, medium, high, critical)"
    ),
    capability: str | None = typer.Option(
        None, "--capability",
        help="Hosted: instructions_only, scripts, mcp, hooks, executables or allowed_tools",
    ),
    works_in: str | None = typer.Option(None, "--works-in", help="Hosted: verified to work in this tool"),
    updated_within: int | None = typer.Option(
        None, "--updated-within", min=1, max=3650, help="Hosted: published within N days"
    ),
    hide_deprecated: bool = typer.Option(False, "--hide-deprecated", help="Hosted: leave out deprecated packages"),
    sort: str = typer.Option("relevance", "--sort", help="Hosted: relevance or recent"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Search registered marketplaces and the hosted SessionFS Marketplace."""
    from sessionfs.skills import discovery, hosted

    if tool is not None:
        _validate_targets([tool])
    if works_in is not None:
        _validate_targets([works_in])
    if category is not None and category not in discovery.CATEGORY_SLUGS:
        raise typer.BadParameter(f"--category must be one of: {', '.join(sorted(discovery.CATEGORY_SLUGS))}")
    if capability is not None and capability not in discovery.CAPABILITY_FILTERS:
        raise typer.BadParameter(f"--capability must be one of: {', '.join(discovery.CAPABILITY_FILTERS)}")
    if max_severity is not None and max_severity not in discovery.SEVERITY_RANK:
        raise typer.BadParameter(f"--max-severity must be one of: {', '.join(discovery.SEVERITY_RANK)}")
    if sort not in ("relevance", "recent"):
        raise typer.BadParameter("--sort must be relevance or recent")
    filters: dict[str, Any] = {
        "category": category,
        "publisher": publisher,
        "verified": True if verified else None,
        "attested": True if attested else None,
        "license": license_filter,
        "max_severity": max_severity,
        "capability": capability,
        "works_in": works_in,
        "updated_within": updated_within,
        "include_deprecated": False if hide_deprecated else None,
        "sort": sort,
    }
    hosted_details: list[dict[str, Any]] = []
    needle = query.casefold()
    rows: list[tuple[str, str, str, str, str]] = []
    total_packages = 0
    total_unsupported = 0
    for registry in _call(lambda: SkillManager().registries.list()):
        if marketplace and registry.alias != marketplace:
            continue
        total_packages += len(registry.packages)
        total_unsupported += len(registry.unsupported)
        for package in registry.packages:
            name = str(package.get("name", ""))
            description = str(package.get("description", ""))
            if needle in name.casefold() or needle in description.casefold():
                ref = f"{registry.alias}/{name}"
                rows.append((f"local:{registry.scope}", registry.alias, name, description, ref))
    # The hosted catalog is searched alongside, never instead: signed out or
    # offline skips it with a note; --local-only / --marketplace skip it.
    hosted_note: str | None = None
    if not (local_only or marketplace):
        client = hosted.client_from_auth()
        if client is None:
            hosted_note = "Hosted SessionFS Marketplace skipped: not signed in (sfs auth login)."
        else:
            try:
                for item in client.search(query, tool=tool, limit=limit, filters=filters):
                    ref = f"sessionfs:{item.get('namespace')}/{item.get('name')}"
                    if item.get("version"):
                        ref += f"@{item.get('version')}"
                    rows.append(
                        (
                            "hosted",
                            "sessionfs",
                            str(item.get("name", "")),
                            str(item.get("description", "")),
                            ref,
                        )
                    )
                    hosted_details.append(_discovery_summary(item))
            except SkillError as exc:
                hosted_note = f"Hosted SessionFS Marketplace skipped: {exc}"
    if json_output:
        details = iter(hosted_details)
        _print_json(
            {
                "results": [
                    {"source": source, "marketplace": alias, "name": name,
                     "description": description, "install": ref,
                     **({"discovery": next(details)} if source == "hosted" else {})}
                    for source, alias, name, description, ref in rows
                ],
                "notes": [hosted_note] if hosted_note else [],
            }
        )
        return
    console.print(
        f"[dim]{total_packages} packages, {total_unsupported} unsupported "
        "(local registries)[/dim]"
    )
    if hosted_note:
        console.print(_literal(hosted_note, "yellow"))
    if not rows:
        console.print("[dim]No matching marketplace packages.[/dim]")
        return
    table = Table(title="Marketplace search")
    table.add_column("Source")
    table.add_column("Marketplace", style="cyan")
    table.add_column("Package")
    table.add_column("Description")
    # Hosted rows need their `sessionfs:` reference; local-only results keep
    # the classic four columns.
    show_ref = any(row[0] == "hosted" for row in rows)
    if show_ref:
        table.add_column("Install with", overflow="fold")
        table.add_column("Installs (30d)", justify="right")
        table.add_column("Status")
    details = iter(hosted_details)
    for source, alias, name, description, ref in rows:
        cells = [source, _literal(alias), _literal(name), _literal(description)]
        if show_ref:
            cells.append(_literal(ref))
            if source == "hosted":
                detail = next(details)
                cells.append(_literal(detail["installs_30d"]))
                cells.append(_literal(", ".join(detail["status"]) or "-"))
            else:
                cells.extend([Text("-"), Text("-")])
        table.add_row(*cells)
    console.print(table)
    for detail in hosted_details:
        if detail.get("successor"):
            console.print(
                _literal(f"{detail['name']} is deprecated; use {detail['successor']} instead", "yellow")
            )


def _discovery_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Discovery fields of a hosted search result (marketplace v2 §9)."""
    def _obj(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    freshness = _obj(item.get("freshness"))
    total = _obj(_obj(item.get("installs")).get("total"))
    deprecation = _obj(item.get("deprecation"))
    publisher = _obj(item.get("publisher"))
    status = []
    if publisher.get("verified"):
        status.append("verified")
    if deprecation:
        status.append("deprecated")
    if freshness.get("stale"):
        status.append("stale")
    if freshness.get("unmaintained"):
        status.append("unmaintained")
    successor = deprecation.get("successor") if deprecation else None
    return _clean({
        "name": str(item.get("name", "")),
        "categories": list(item.get("categories") or []),
        "installs_30d": str(total.get("display", "-")),
        "status": status,
        "last_updated_at": freshness.get("last_updated_at"),
        "works_in": sorted(
            tool for tool, row in (freshness.get("tools") or {}).items()
            if isinstance(row, dict) and row.get("verify_status") == "passing"
        ),
        "successor": (
            f"sessionfs:{successor.get('namespace')}/{successor.get('name')}"
            if isinstance(successor, dict) else None
        ),
    })


@skill_app.command("categories")
@handle_errors
def list_categories(json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON")) -> None:
    """List the hosted marketplace's categories (for `sfs skill search --category`)."""
    from sessionfs.skills import hosted

    client = _call(lambda: hosted.require_client(hosted.client_from_auth()))
    items = _call(client.categories)
    if json_output:
        _print_json({"items": items})
        return
    table = Table(title="Marketplace categories")
    table.add_column("Slug", style="cyan")
    table.add_column("Name")
    for item in items:
        table.add_row(_literal(item.get("slug")), _literal(item.get("display_name")))
    console.print(table)


@skill_app.command("usage")
@handle_errors
def org_usage(
    org: str = typer.Option(..., "--org", help="Organization id (owners and admins, Team plan)"),
    days: int = typer.Option(30, "--days", min=1, max=90),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """What your organization's members actually use, by package and tool."""
    from sessionfs.cli.cmd_skill_publisher import _request, _segment

    body = _clean(_request("GET", f"/orgs/{_segment(org)}/skills/usage?days={days}"))
    if json_output:
        _print_json(body)
        return
    items = body.get("items") or []
    if not items:
        console.print(f"[dim]No marketplace usage recorded in the last {days} days.[/dim]")
        return
    table = Table(title=f"Skill usage, last {days} days")
    for column in ("Package", "Installs", "Active", "Uninstalls", "Translation failures", "Tools", "Status"):
        table.add_column(column)
    for item in items:
        status = [label for label in ("deprecated", "stale", "unmaintained") if item.get(label)]
        table.add_row(
            _literal(f"{item.get('namespace')}/{item.get('name')}"),
            str(item.get("installs", 0)),
            str(item.get("active", 0)),
            str(item.get("uninstalls", 0)),
            str(item.get("translation_failures", 0)),
            _literal(", ".join(row.get("tool", "") for row in item.get("by_tool") or [])),
            _literal(", ".join(status) or "-"),
        )
    console.print(table)


# --- Hosted publishing (logic in sessionfs.skills.publishing) ----------------


@skill_app.command("validate")
@handle_errors
def validate_package(
    source: str = typer.Argument(..., help="Package directory or .tar.gz archive"),
    visibility: str = typer.Option(
        "personal", "--visibility", help="Publish target to validate for: personal, org, or public"
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Run the hosted marketplace's publish checks locally (exit 1 on errors)."""
    from pathlib import Path

    from sessionfs.skills import publishing

    if visibility not in publishing.VISIBILITIES:
        raise typer.BadParameter(f"--visibility must be one of: {', '.join(publishing.VISIBILITIES)}")
    result = publishing.validate_source(Path(source).expanduser(), visibility=visibility)
    if json_output:
        _print_json(result.to_dict())
    else:
        if result.problems:
            publishing.print_problems(console, result.problems)
        if result.deferred_dlp is not None:
            publishing.print_problems(console, [result.deferred_dlp], label="WARNING")
        if result.package is not None:
            console.print(
                publishing.summary_table(result.package, upload_bytes=len(result.data or b""))
            )
            publishing.print_scan_findings(console, result.package.risk_summary)
        for warning in result.warnings:
            console.print(_literal("Warning: " + warning, "yellow"))
        if result.ok:
            console.print(_literal(f"Valid for {visibility} publishing.", "bold green"))
            console.print("[dim]Checked by the server at publish time:[/dim]")
            for check in publishing.server_only_checks(visibility):
                console.print(_literal("  - " + check, "dim"))
    if not result.ok:
        raise typer.Exit(1)


@skill_app.command("publish")
@handle_errors
def publish_package(
    source: str = typer.Argument(..., help="Package directory or .tar.gz archive"),
    org: str | None = typer.Option(None, "--org", help="Publish into this organization (org_id)"),
    visibility: str | None = typer.Option(
        None,
        "--visibility",
        help="personal (default), org (default with --org), or public (your verified publisher "
        "namespace, once SessionFS enables public publishing for it)",
    ),
    namespace: str | None = typer.Option(None, "--namespace", help="Listing namespace (default: personal / org)"),
    collection: str | None = typer.Option(None, "--collection", help="Listing collection (default: personal / org)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Publish without the confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Print the server response as JSON"),
    trusted_publishing: bool = typer.Option(
        False,
        "--trusted-publishing",
        help="Publish from GitHub Actions with an OIDC token instead of an API key "
        "(needs `permissions: id-token: write` and a trusted publisher for --namespace)",
    ),
    package_name: str | None = typer.Option(
        None, "--package", help="With --trusted-publishing: the package name the trust is scoped to"
    ),
    audience: str | None = typer.Option(
        None,
        "--audience",
        help="With --trusted-publishing: OIDC audience (default: read from the server)",
    ),
) -> None:
    """Validate locally, then upload a package to the hosted marketplace."""
    from pathlib import Path

    from sessionfs.cli.common import err_console
    from sessionfs.profiles import resolve_auth
    from sessionfs.skills import publishing

    if trusted_publishing:
        _publish_trusted(
            source=source,
            visibility=visibility,
            namespace=namespace,
            collection=collection,
            package_name=package_name,
            audience=audience,
            json_output=json_output,
        )
        return
    try:
        target = publishing.resolve_target(
            org_id=org, visibility=visibility, namespace=namespace, collection=collection
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    result = publishing.validate_source(Path(source).expanduser(), visibility=target.visibility)
    if not result.ok or result.package is None or result.data is None:
        publishing.print_problems(err_console, result.problems)
        err_console.print("[red]Not published:[/red] fix the problems above and run again.")
        raise typer.Exit(1)
    auth = resolve_auth()
    if not auth.api_key:
        err_console.print(
            "[red]Not authenticated.[/red] Run [bold]sfs auth login[/bold] first, or set "
            "[bold]SESSIONFS_API_KEY[/bold] for non-interactive use."
        )
        raise typer.Exit(1)
    package = result.package
    console.print(
        publishing.summary_table(
            package, upload_bytes=len(result.data), target=f"{target.describe()} on {auth.api_url}"
        )
    )
    if result.deferred_dlp is not None:
        publishing.print_problems(console, [result.deferred_dlp], label="WARNING")
    for warning in result.warnings:
        console.print(_literal("Warning: " + warning, "yellow"))
    if not confirm_or_exit(
        f"Publish {package.name} {package.version or ''}".rstrip() + "?",
        yes=yes,
    ):
        err_console.print("Cancelled: nothing was published.")
        raise typer.Exit(1)
    import httpx

    try:
        status, body, headers = publishing.upload_package(
            api_url=auth.api_url,
            api_key=auth.api_key,
            target=target,
            filename=f"{package.name}.tar.gz",
            data=result.data,
        )
    except httpx.TimeoutException as exc:
        err_console.print(
            "[red]The upload timed out.[/red] The server may still be processing it; run "
            "[bold]sfs skill publish[/bold] again (an identical version is reused, not duplicated)."
        )
        raise typer.Exit(1) from exc
    except httpx.RequestError as exc:
        err_console.print(f"[red]Can't reach the SessionFS API at {auth.api_url}.[/red] ({type(exc).__name__})")
        raise typer.Exit(1) from exc
    if status not in (200, 201) or not isinstance(body, dict):
        lines = publishing.explain_failure(status, body, headers, target=target)
        err_console.print(_literal(lines[0], "red"))
        for line in lines[1:]:
            err_console.print(_literal(line, "yellow"))
        raise typer.Exit(1)
    if json_output:
        _print_json(body)
        return
    if body.get("content_hash") != package.content_hash:
        console.print(
            _literal(
                f"Warning: the server stored {body.get('content_hash')} but local validation computed "
                f"{package.content_hash}.",
                "yellow",
            )
        )
    console.print(_literal(f"Published {body.get('name')} {body.get('version') or ''}".rstrip(), "bold green"))
    for label, key in (
        ("Listing", "listing_id"),
        ("Package", "package_id"),
        ("Version", "version_id"),
        ("Content hash", "content_hash"),
        ("Visibility", "visibility"),
    ):
        console.print(_literal(f"  {label}: {body.get(key)}"))
    console.print(_literal(f"  URL: {publishing.published_url(auth.api_url, body)}"))


def _publish_trusted(
    *,
    source: str,
    visibility: str | None,
    namespace: str | None,
    collection: str | None,
    package_name: str | None,
    audience: str | None,
    json_output: bool,
) -> None:
    """`sfs skill publish --trusted-publishing`: validate, get a GitHub OIDC
    token, exchange it for a single-use publish token, upload. No API key."""
    from pathlib import Path

    import httpx

    from sessionfs.cli.common import err_console
    from sessionfs.profiles import resolve_auth
    from sessionfs.skills import publishing

    if not namespace:
        raise typer.BadParameter("--trusted-publishing needs --namespace (your publisher namespace).")
    if visibility not in (None, "public"):
        raise typer.BadParameter(
            "With --trusted-publishing, omit --visibility (the publisher's own library) or pass public."
        )
    source_path = Path(source).expanduser()

    def _validated(target: str) -> tuple[Any, bytes]:
        checked = publishing.validate_source(source_path, visibility=target)
        if not checked.ok or checked.package is None or checked.data is None:
            publishing.print_problems(err_console, checked.problems)
            err_console.print("[red]Not published:[/red] fix the problems above and run again.")
            raise typer.Exit(1)
        return checked.package, checked.data

    # Structural checks first (personal is the least strict target), then the
    # real target once the exchange says what this publisher publishes to.
    package, data = _validated("public" if visibility == "public" else "personal")
    api_url = resolve_auth().api_url
    try:
        expected = audience or publishing.trusted_publishing_audience(api_url=api_url)
        oidc = publishing.github_oidc_token(expected)
        exchanged = publishing.exchange_trusted_publishing_token(
            api_url=api_url, oidc_token=oidc, namespace=namespace, package=package_name or package.name
        )
        target = publishing.trusted_validation_visibility(visibility, exchanged)
        if target != ("public" if visibility == "public" else "personal"):
            package, data = _validated(target)
        status, body, _headers = publishing.upload_trusted(
            api_url=api_url,
            publish_token=str(exchanged["token"]),
            filename=f"{package.name}.tar.gz",
            data=data,
            visibility=visibility,
            collection=collection,
        )
    except publishing.TrustedPublishingError as exc:
        err_console.print(_literal(str(exc), "red"))
        raise typer.Exit(1) from exc
    except httpx.RequestError as exc:
        err_console.print(f"[red]Can't reach the SessionFS API at {api_url}.[/red] ({type(exc).__name__})")
        raise typer.Exit(1) from exc
    if status not in (200, 201) or not isinstance(body, dict):
        code, message, _details = publishing._error_parts(body)
        err_console.print(_literal(f"Publish failed ({status}{', ' + code if code else ''}): {message}", "red"))
        raise typer.Exit(1)
    if json_output:
        _print_json(body)
        return
    console.print(_literal(f"Published {body.get('name')} {body.get('version') or ''}".rstrip(), "bold green"))
    raw_signature = body.get("signature")
    signature: dict[str, Any] = raw_signature if isinstance(raw_signature, dict) else {}
    for label, value in (
        ("Namespace", body.get("namespace")),
        ("Version", body.get("version_id")),
        ("Content hash", body.get("content_hash")),
        ("Moderation", body.get("moderation_state")),
        # Publication provenance (which workflow run published it), not build
        # provenance: nothing yet binds the archive digest to that commit.
        ("Published from", f"{signature.get('published_from_repo')}@{signature.get('publishing_run_sha')}"),
        ("Publishing workflow", signature.get("publishing_workflow_ref")),
        ("Publishing run", signature.get("publishing_run_url")),
    ):
        console.print(_literal(f"  {label}: {value}"))


def _print_revocation_warnings(manager: SkillManager, scope: str | None) -> list[dict[str, Any]]:
    from sessionfs.skills import hosted

    warnings = hosted.revocation_warnings(manager, scope)
    unknown = hosted.identity_unknown(manager)
    if unknown:
        # An unregistered credential (e.g. a rotated key, offline): only
        # device-wide blocks are checked until it is registered online.
        console.print(
            _literal(
                f"Identity unknown for {unknown} on this device: only device-wide blocks are "
                "checked - run `sfs skill pull` while online",
                "yellow",
            )
        )
    for item in warnings:
        style = "red bold" if item["state"] == "blocked" else "yellow"
        console.print(
            _literal(
                f"{item['state'].upper()}: {item['name']} ({item['scope']}, "
                f"{item['content_hash'][:20]}...) - {item['reason'] or 'no reason given'}",
                style,
            )
        )
    return warnings


@skill_app.command("pull")
@handle_errors
def pull_skills(
    tools: list[str] = typer.Option(
        [], "--for", help="Target tools when an assignment names none (default all)"
    ),
    device_id: str | None = typer.Option(
        None, "--device-id", help="This device's id for device assignments (or SESSIONFS_DEVICE_ID)"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Install optional items without asking"
    ),
    allow_scripts: bool = typer.Option(False, "--allow-scripts"),
    enable_mcp: bool = typer.Option(False, "--enable-mcp"),
    allow_executable_extensions: bool = typer.Option(False, "--allow-executable-extensions"),
    allow_hooks: bool = typer.Option(False, "--allow-hooks"),
    allow_dlp_findings: bool = typer.Option(False, "--allow-dlp-findings"),
    approve_widening: bool = typer.Option(
        False,
        "--approve-widening",
        help="Apply assigned updates that ask for MORE access (never implied by --yes)",
    ),
    allow_hidden_unicode: bool = typer.Option(
        False,
        "--allow-hidden-unicode",
        help="Install packages that contain hidden / bidirectional / control characters",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Project id for install requests (default: resolved from the git remote)",
    ),
) -> None:
    """Reconcile org skill assignments and approved install requests to this device."""
    from sessionfs.skills import hosted

    selected = _validate_targets(tools)
    manager = SkillManager()
    client = _call(lambda: hosted.require_client(hosted.client_from_auth()))
    resolved_device = _call(lambda: hosted.resolve_device_id(device_id))
    interactive = hosted.is_interactive() and not json_output

    def _offer_install(item: Any) -> bool | str:
        # A version you explicitly removed (opt-out) is only reinstalled when
        # you say yes interactively - never by --yes alone. The answer names
        # how it was accepted (recorded as the acceptance provenance).
        if yes and not item.opted_out:
            return "yes"
        if not interactive:
            return False
        suffix = " (you removed it before)" if item.opted_out else ""
        tools = f" for {', '.join(item.targets)}" if item.targets else ""
        accepted = typer.confirm(
            f"Install optional {item.origin} {item.version_id}{tools} "
            f"({item.content_hash[:20]}...){suffix}?",
            default=False,
        )
        return "prompt" if accepted else False

    report = _call(
        lambda: hosted.pull(
            manager,
            client,
            device_id=resolved_device,
            targets=selected or None,
            flags={
                "allow_scripts": allow_scripts,
                "enable_mcp": enable_mcp,
                "allow_executable_extensions": allow_executable_extensions,
                "allow_hooks": allow_hooks,
                "allow_dlp_findings": allow_dlp_findings,
                "approve_widening": approve_widening,
                "allow_hidden_unicode": allow_hidden_unicode,
            },
            install_optional=_offer_install,
            project_id=project,
            # Notes (e.g. a listing that could not be read) and pending
            # recoveries are shown as they happen, before any prompt.
            notify=(
                None if json_output
                else (lambda note: console.print(_literal(note, "yellow")))
            ),
            # With --json, pending recoveries go to stderr at once (stdout
            # stays the JSON document), so a later failure cannot hide them.
            on_recovery=(
                (lambda message: typer.echo(message, err=True)) if json_output else None
            ),
        )
    )
    try:
        hosted.report_pull_usage(manager, client, report, interactive=True)
    except Exception:  # noqa: BLE001 - counts never affect the pull
        pass
    if json_output:
        _print_json(report.as_dict())
    else:
        for item in report.blocked_removed:
            console.print(
                _literal(
                    f"Removed BLOCKED package {item['name']} ({item['scope']}): "
                    f"{item['reason'] or 'blocked by the marketplace'}"
                    + (f" - {item['error']}" if item.get("error") else ""),
                    "red bold",
                )
            )
            if item.get("quarantine_dir"):
                console.print(
                    _literal(
                        f"  Its files (including {len(item.get('dirty') or [])} you modified) "
                        f"were moved to {item['quarantine_dir']}",
                        "yellow",
                    )
                )
            for path in item.get("failed") or []:
                console.print(_literal(f"  STILL ACTIVE (could not be moved): {path}", "red"))
        if not report.items:
            console.print("[dim]No skill assignments or approved install requests.[/dim]")
        else:
            table = Table(title="Skill pull")
            table.add_column("Origin")
            table.add_column("Mode")
            table.add_column("Scope")
            table.add_column("Version")
            table.add_column("Status")
            table.add_column("Detail", overflow="fold")
            for item in report.items:
                table.add_row(
                    item.origin, item.mode, item.scope, item.version_id,
                    item.status, _literal(item.name + (" " if item.name else "") + item.detail),
                )
            console.print(table)
            for item in report.items:
                if item.status == "held":
                    console.print(
                        _literal(
                            f"held: {item.name or item.version_id} needs approval - {item.detail}",
                            "yellow bold",
                        )
                    )
        _print_unassigned(report.unassigned)
        _print_unassigned_tools(report.unassigned_tools)
        _print_revocation_warnings(manager, None)
        if report.policy_refused:
            console.print(
                _literal(
                    f"{len(report.policy_refused)} item(s) withheld by your organization's skills "
                    "policy (see the notes above)",
                    "yellow",
                )
            )
        for sent in report.policy_reports:
            state = "compliant" if sent.get("compliant") else "not compliant"
            console.print(_literal(f"Skills policy ({sent.get('org_id')}): {state}, reported", "dim"))
    if any(item.status == "failed" for item in report.items):
        raise typer.Exit(1)
