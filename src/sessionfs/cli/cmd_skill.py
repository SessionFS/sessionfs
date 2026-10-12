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
    source: str = typer.Argument(..., help="Local path, Git URL, or marketplace/package"),
    pin: str | None = typer.Option(None, "--pin", help="Git ref or commit to resolve"),
    subdir: str = typer.Option("", "--subdir", help="Package subdirectory in the source"),
    scope: str = typer.Option("project", "--scope", help="project or user identity"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Inspect and validate a package without installing it into a tool."""
    package = _call(
        lambda: SkillManager().inspect_source(
            source,
            pin=pin,
            subdir=subdir,
            scope=scope,
        )
    )
    if json_output:
        _print_json(package.as_dict(include_path=False))
        return
    console.print(Text.assemble(_literal(package.name, "bold cyan"), f"  {package.content_hash}"))
    console.print(Text.assemble(f"Package: {package.package_id}  Origin: ", _literal(package.origin_format)))
    console.print(
        f"Skills: {len(package.skills)}  MCP: {'yes' if package.has_mcp else 'no'}  "
        f"Risky: {'yes' if package.risky else 'no'}"
    )


@skill_app.command("add")
@handle_errors
def add_skill(
    source: str = typer.Argument(..., help="Local path, Git URL, or marketplace/package"),
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
    plans = _call(lambda: manager.plan(package, targets=selected, scope=scope, default_all=True))
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
    _call(
        lambda: manager.install_prepared(
            package,
            plans,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
        )
    )
    console.print(
        Text.assemble(
            ("Installed ", "green"),
            _literal(package.name, "green"),
            f" ({package.package_id}) at {package.content_hash}",
        )
    )


@skill_app.command("list")
@handle_errors
def list_skills(
    scope: str | None = typer.Option(None, "--scope", help="project or user"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """List locally installed packages."""
    rows = _call(lambda: SkillManager().list_installed(scope))
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


@skill_app.command("show")
@handle_errors
def show_skill(
    identifier: str = typer.Argument(..., help="Package name or package_id"),
    scope: str | None = typer.Option(None, "--scope", help="project or user"),
) -> None:
    """Show local package state, provenance, plans, and omissions."""
    row = _call(lambda: SkillManager().find_installed(identifier, scope=scope))
    _print_json(row)


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
) -> None:
    """Restore exact desired state from the lockfile."""
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
    result = _call(lambda: SkillManager().remove(identifier, scope=scope))
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
) -> None:
    """Fetch a new source version, show what would change, confirm, then apply."""
    manager = SkillManager()
    # Prepare: fetch + plan only. Nothing is written to adapter roots or state
    # until the reviewer has seen the new hash, commit, plan and capability
    # diff - a compromised upstream must never land (or receive consent for a
    # new hash) without review.
    prepared = _call(lambda: manager.prepare_update(identifier, scope=scope, pin=pin))
    _call(
        lambda: manager._enforce_consent(
            prepared.plans,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
            package=prepared.package,
        )
    )
    _print_update_preview(prepared)
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
    _call(
        lambda: manager.apply_update(
            prepared,
            allow_scripts=allow_scripts,
            enable_mcp=enable_mcp,
            allow_executable_extensions=allow_executable_extensions,
            allow_hooks=allow_hooks,
            allow_dlp_findings=allow_dlp_findings,
        )
    )
    console.print(
        Text.assemble(
            ("Updated ", "green"),
            _literal(prepared.package.name, "green"),
            f" ({prepared.package.package_id}) to {prepared.package.content_hash}",
        )
    )


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
) -> None:
    """Re-apply a cached immutable package version."""
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
        )
    )
    console.print(
        Text.assemble(("Rolled back ", "green"), _literal(row["name"], "green"), (f" to {row['content_hash']}", "green"))
    )


@skill_app.command("doctor")
@handle_errors
def doctor_skills(
    scope: str | None = typer.Option(None, "--scope", help="project or user"),
) -> None:
    """Report missing or modified managed outputs."""
    drift = _call(lambda: SkillManager().doctor(scope))
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
) -> None:
    """Search all registered external marketplaces."""
    needle = query.casefold()
    rows: list[tuple[str, str, str, str]] = []
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
                rows.append((registry.scope, registry.alias, name, description))
    console.print(f"[dim]{total_packages} packages, {total_unsupported} unsupported[/dim]")
    if not rows:
        console.print("[dim]No matching marketplace packages.[/dim]")
        return
    table = Table(title="Marketplace search")
    table.add_column("Scope")
    table.add_column("Marketplace", style="cyan")
    table.add_column("Package")
    table.add_column("Description")
    for scope, alias, name, description in rows:
        table.add_row(scope, alias, _literal(name), _literal(description))
    console.print(table)
