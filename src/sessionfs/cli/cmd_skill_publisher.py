"""`sfs skill publisher` - claim a marketplace namespace, verify it, and
configure GitHub Actions trusted publishing."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from sessionfs.cli.common import console, err_console, format_api_error, handle_errors

publisher_app = typer.Typer(
    name="publisher",
    help="Claim and verify a marketplace publisher namespace; configure trusted publishing.",
    no_args_is_help=True,
)


def _request(method: str, path: str, payload: dict | None = None) -> Any:
    import httpx

    from sessionfs.profiles import resolve_auth

    auth = resolve_auth()
    if not auth.api_key:
        err_console.print("[red]Not authenticated.[/red] Run [bold]sfs auth login[/bold] first.")
        raise typer.Exit(1)
    url = f"{auth.api_url.rstrip('/')}/api/v1{path}"
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(
                method, url, headers={"Authorization": f"Bearer {auth.api_key}"}, json=payload
            )
    except httpx.RequestError as exc:
        err_console.print(f"[red]Can't reach the SessionFS API at {auth.api_url}.[/red] ({type(exc).__name__})")
        raise typer.Exit(1) from exc
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    if response.status_code >= 400:
        err_console.print(Text(format_api_error(body, response.status_code), style="red"))
        raise typer.Exit(1)
    return body


def _segment(value: str) -> str:
    from sessionfs.url_path_safety import path_segment

    try:
        return path_segment(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _show(publisher: dict, json_output: bool) -> None:
    if json_output:
        console.print_json(json.dumps(publisher, sort_keys=True))
        return
    badges = ", ".join(publisher.get("badges") or []) or "not verified"
    console.print(Text(f"{publisher.get('namespace')}  ({publisher.get('id')})", style="bold"))
    console.print(Text(f"  Owner: {publisher.get('owner_type')}   Status: {publisher.get('status')}"))
    console.print(Text(f"  Badge: {badges}"))
    if publisher.get("public_publishing") is not None:
        console.print(Text(f"  Public publishing: {'enabled' if publisher.get('public_publishing') else 'not enabled'}"))
    for row in publisher.get("verifications") or []:
        line = f"  - {row.get('method')} {row.get('subject_display')}: {row.get('status')}"
        if row.get("status") == "pending" and row.get("txt_record"):
            line += f"  (add TXT {row.get('txt_record')} at {row.get('txt_host')})"
        console.print(Text(line))
    for row in publisher.get("trusted_publishers") or []:
        constraint = ", ".join(
            f"{key}={row[key]}" for key in ("environment", "ref_pattern", "package_name") if row.get(key)
        )
        console.print(Text(f"  * trusts {row.get('repository')} {row.get('workflow')} {constraint}".rstrip()))


@publisher_app.command("claim")
@handle_errors
def claim(
    namespace: str = typer.Argument(..., help="3-39 lowercase letters, digits or hyphens"),
    org: str | None = typer.Option(None, "--org", help="Claim for this organization (org_id)"),
    display_name: str | None = typer.Option(None, "--display-name", help="Shown next to the namespace"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Claim a globally unique namespace. Lookalikes of reserved or verified
    names are refused."""
    payload: dict[str, Any] = {"namespace": namespace}
    if org:
        payload["org_id"] = org
    if display_name:
        payload["display_name"] = display_name
    _show(_request("POST", "/publishers", payload), json_output)


@publisher_app.command("list")
@handle_errors
def list_publishers(json_output: bool = typer.Option(False, "--json")) -> None:
    """Publishers you manage (yours and your organizations')."""
    body = _request("GET", "/publishers")
    if json_output:
        console.print_json(json.dumps(body, sort_keys=True))
        return
    table = Table(title="Publishers")
    for column in ("Namespace", "Id", "Owner", "Status", "Badge"):
        table.add_column(column)
    for item in body.get("items", []):
        table.add_row(
            Text(str(item.get("namespace"))),
            Text(str(item.get("id"))),
            Text(str(item.get("owner_type"))),
            Text(str(item.get("status"))),
            Text(", ".join(item.get("badges") or []) or "-"),
        )
    console.print(table)


@publisher_app.command("show")
@handle_errors
def show(publisher_id: str, json_output: bool = typer.Option(False, "--json")) -> None:
    """Show a publisher (owners also see verifications and trusted publishers)."""
    _show(_request("GET", f"/publishers/{_segment(publisher_id)}"), json_output)


@publisher_app.command("verify")
@handle_errors
def verify(
    publisher_id: str,
    dns: str | None = typer.Option(None, "--dns", help="Verify this domain with a DNS TXT record"),
    github_org: int | None = typer.Option(
        None, "--github-org", help="Verify a GitHub organization via your SessionFS GitHub App installation id"
    ),
    github_user: int | None = typer.Option(
        None, "--github-user", help="Verify your GitHub account via your GitHub App installation id"
    ),
) -> None:
    """Start a verification. DNS prints the TXT record to add; then run
    `sfs skill publisher check`."""
    chosen = [value is not None for value in (dns, github_org, github_user)]
    if sum(chosen) != 1:
        raise typer.BadParameter("Pass exactly one of --dns, --github-org, --github-user.")
    if dns:
        payload: dict[str, Any] = {"method": "dns_txt", "domain": dns}
    elif github_org is not None:
        payload = {"method": "github_org", "installation_id": github_org}
    else:
        payload = {"method": "github_user", "installation_id": github_user}
    row = _request("POST", f"/publishers/{_segment(publisher_id)}/verifications", payload)
    console.print(Text(f"Verification {row.get('id')}: {row.get('status')}", style="bold"))
    if row.get("status") == "pending" and row.get("txt_record"):
        console.print(Text(f"Add this TXT record at {row.get('txt_host')}:"))
        console.print(Text(f"  {row.get('txt_record')}", style="cyan"))
        console.print(Text(f"Then run: sfs skill publisher check {publisher_id} {row.get('id')}"))
    elif row.get("badge"):
        console.print(Text(f"Badge: {row.get('badge')}", style="green"))


@publisher_app.command("check")
@handle_errors
def check(publisher_id: str, verification_id: str) -> None:
    """Re-check a verification now (DNS lookup or GitHub installation)."""
    row = _request(
        "POST", f"/publishers/{_segment(publisher_id)}/verifications/{_segment(verification_id)}/check"
    )
    style = "green" if row.get("status") == "verified" else "yellow"
    console.print(Text(f"{row.get('method')} {row.get('subject_display')}: {row.get('status')}", style=style))


@publisher_app.command("trust")
@handle_errors
def trust(
    publisher_id: str,
    repo: str = typer.Option(..., "--repo", help="GitHub repository, owner/repo"),
    workflow: str = typer.Option(..., "--workflow", help="Workflow file, e.g. release.yml"),
    environment: str | None = typer.Option(None, "--environment", help="Require this GitHub environment"),
    ref_pattern: str | None = typer.Option(None, "--ref-pattern", help="Require a matching ref, e.g. refs/tags/v*"),
    package: str | None = typer.Option(None, "--package", help="Limit to one package name"),
    reusable_workflow: str | None = typer.Option(
        None,
        "--reusable-workflow",
        help="Also require this reusable (called) workflow: owner/repo/.github/workflows/publish.yml",
    ),
    allow_workflow_run: bool = typer.Option(
        False, "--allow-workflow-run", help="Also accept runs triggered by workflow_run (off by default)"
    ),
    repository_id: int | None = typer.Option(None, "--repository-id", help="Numeric repository id (skips lookup)"),
    repository_owner_id: int | None = typer.Option(None, "--repository-owner-id", help="Numeric owner id"),
) -> None:
    """Allow a GitHub Actions workflow to publish for this publisher. The
    trust binds to the repository's numeric ids, so a renamed or recreated
    repository does not inherit it."""
    payload: dict[str, Any] = {"repository": repo, "workflow": workflow}
    for key, value in (
        ("environment", environment),
        ("ref_pattern", ref_pattern),
        ("package_name", package),
        ("reusable_workflow", reusable_workflow),
        ("repository_id", repository_id),
        ("repository_owner_id", repository_owner_id),
    ):
        if value is not None:
            payload[key] = value
    if allow_workflow_run:
        payload["allow_workflow_run"] = True
    row = _request("POST", f"/publishers/{_segment(publisher_id)}/trusted-publishers", payload)
    console.print(
        Text(
            f"Trusted {row.get('repository')} (repo #{row.get('repository_id')}) {row.get('workflow')} "
            f"as {row.get('id')}",
            style="green",
        )
    )


@publisher_app.command("untrust")
@handle_errors
def untrust(publisher_id: str, trusted_id: str) -> None:
    """Remove a trusted publisher (tokens already minted under it stop working)."""
    _request("DELETE", f"/publishers/{_segment(publisher_id)}/trusted-publishers/{_segment(trusted_id)}")
    console.print(Text(f"Removed {trusted_id}", style="green"))


def _clean(value: Any) -> Any:
    """Server-supplied text stripped of terminal control, escape,
    zero-width and bidi characters before printing (recursive)."""
    from sessionfs.skills.discovery import terminal_safe

    if isinstance(value, str):
        return terminal_safe(value)
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {terminal_safe(key) if isinstance(key, str) else key: _clean(item) for key, item in value.items()}
    return value


def _count(value: Any) -> str:
    """A k-floored count from the analytics API ({"count", "display"})."""
    if isinstance(value, dict):
        return str(value.get("display", "-"))
    return "-" if value is None else str(value)


def _rate(value: Any) -> str:
    return f"{value * 100:.1f}%" if isinstance(value, (int, float)) else "n/a (<10 installs)"


@publisher_app.command("stats")
@handle_errors
def stats(
    publisher_id: str,
    days: int = typer.Option(30, "--days", help="Window in days: 7, 30 or 90"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Publisher analytics: installs by tool and version, active installs,
    uninstall rate, translation failures, works-in regressions, scan findings
    and open reports. Counts under 10 show as "<10"."""
    if days not in (7, 30, 90):
        raise typer.BadParameter("--days must be 7, 30 or 90")
    body = _clean(_request("GET", f"/publishers/{_segment(publisher_id)}/analytics?days={days}"))
    if json_output:
        console.print_json(json.dumps(body, sort_keys=True))
        return
    totals = body.get("totals") or {}
    console.print(Text(f"{body.get('namespace')}: last {body.get('window_days')} days", style="bold"))
    console.print(Text(
        f"  Installs {_count(totals.get('installs'))}   Active {_count(totals.get('active_installs'))}   "
        f"Uninstalls {_count(totals.get('uninstalls'))} ({_rate(totals.get('uninstall_rate'))})   "
        f"Translation failures {_count(totals.get('translation_failures'))}   "
        f"Service-key installs {_count(totals.get('service_installs'))}"
    ))
    by_tool = body.get("by_tool") or []
    if by_tool:
        table = Table(title="By tool")
        for column in ("Tool", "Installs", "Active", "Uninstalls", "Uninstall rate", "Translation failures"):
            table.add_column(column)
        for row in by_tool:
            table.add_row(
                Text(str(row.get("tool"))), _count(row.get("installs")), _count(row.get("active")),
                _count(row.get("uninstalls")), _rate(row.get("uninstall_rate")),
                _count(row.get("translation_failures")),
            )
        console.print(table)
    packages = body.get("packages") or []
    if packages:
        table = Table(title="Packages")
        for column in ("Package", "Version", "Installs", "Active", "Works-in regressions", "Scan", "Reports", "Status"):
            table.add_column(column)
        for item in packages:
            status = [label for label in ("deprecated", "stale", "unmaintained") if item.get(label)]
            scan = item.get("scan") or {}
            table.add_row(
                Text(f"{item.get('namespace')}/{item.get('name')}"),
                Text(str(item.get("current_version") or "-")),
                _count(item.get("installs")),
                _count(item.get("active_installs")),
                Text(", ".join(item.get("works_in_regressions") or []) or "-"),
                Text(str(scan.get("max_severity") or "none")),
                Text(str(item.get("open_reports", 0))),
                Text(", ".join(status) or "-"),
            )
        console.print(table)


@publisher_app.command("package")
@handle_errors
def package_settings(
    publisher_id: str,
    package_id: str,
    category: list[str] = typer.Option([], "--category", help="Category slug (repeatable, up to 3; replaces the set)"),
    clear_categories: bool = typer.Option(False, "--clear-categories", help="Remove every category"),
    deprecate: bool = typer.Option(False, "--deprecate", help="Mark the package deprecated"),
    undeprecate: bool = typer.Option(False, "--undeprecate", help="Clear the deprecation"),
    message: str | None = typer.Option(None, "--message", help="Deprecation message shown to installers"),
    successor: str | None = typer.Option(None, "--successor", help="Successor package id (same publisher)"),
    readme_path: str | None = typer.Option(None, "--readme-path", help="README path inside the package ('' resets)"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Set a package's categories, deprecation (with a successor from the same
    publisher) and README path."""
    if deprecate and undeprecate:
        raise typer.BadParameter("Pass --deprecate or --undeprecate, not both.")
    if category and clear_categories:
        raise typer.BadParameter("Pass --category or --clear-categories, not both.")
    payload: dict[str, Any] = {}
    if category or clear_categories:
        payload["categories"] = list(category)
    if deprecate:
        payload["deprecated"] = True
    if undeprecate:
        payload["deprecated"] = False
    if message is not None:
        payload["deprecation_message"] = message
    if successor is not None:
        payload["successor_package_id"] = successor or None
    if readme_path is not None:
        payload["readme_path"] = readme_path
    if not payload:
        raise typer.BadParameter("Nothing to change.")
    row = _clean(_request(
        "PATCH", f"/publishers/{_segment(publisher_id)}/packages/{_segment(package_id)}", payload
    ))
    if json_output:
        console.print_json(json.dumps(row, sort_keys=True))
        return
    console.print(Text(f"{row.get('namespace')}/{row.get('name')} ({row.get('package_id')})", style="bold"))
    console.print(Text(f"  Categories: {', '.join(row.get('categories') or []) or '-'}"))
    if row.get("deprecated_at"):
        line = f"  Deprecated since {row.get('deprecated_at')}"
        if row.get("successor_package_id"):
            line += f"; successor {row.get('successor_package_id')}"
        console.print(Text(line, style="yellow"))
    if row.get("readme_path"):
        console.print(Text(f"  README: {row.get('readme_path')}"))
