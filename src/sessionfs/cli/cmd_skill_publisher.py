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


def _lookup_gitlab_namespace_id(host: str | None, project_id: int) -> int:
    """The project's current namespace id from the GitLab REST API.

    Public projects need no token; GITLAB_TOKEN or CI_JOB_TOKEN from the
    environment is used when set (never printed). Raises ValueError with a
    short reason when the lookup cannot produce an id.
    """
    import os

    import httpx

    origin = (host or "https://gitlab.com").rstrip("/")
    if not origin.startswith("https://"):
        raise ValueError("the GitLab host is not an https origin")
    headers: dict[str, str] = {}
    if os.environ.get("GITLAB_TOKEN"):
        headers["PRIVATE-TOKEN"] = os.environ["GITLAB_TOKEN"]
    elif os.environ.get("CI_JOB_TOKEN"):
        headers["JOB-TOKEN"] = os.environ["CI_JOB_TOKEN"]
    try:
        response = httpx.get(f"{origin}/api/v4/projects/{project_id}", headers=headers, timeout=15.0)
    except httpx.RequestError as exc:
        raise ValueError(f"could not reach {origin} ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise ValueError(f"GitLab answered HTTP {response.status_code} for project #{project_id}")
    try:
        namespace_id = response.json()["namespace"]["id"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("GitLab's answer had no namespace id") from exc
    if isinstance(namespace_id, bool) or not isinstance(namespace_id, int) or namespace_id < 1:
        raise ValueError("GitLab's answer had no valid namespace id")
    return namespace_id


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
        if row.get("provider") == "gitlab":
            source = f"GitLab {row.get('gitlab_host')} project #{row.get('gitlab_project_id')} ({row.get('repository')})"
        else:
            source = f"{row.get('repository')} {row.get('workflow')}"
        console.print(Text(f"  * trusts {source} {constraint}".rstrip()))


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
    repo: str | None = typer.Option(None, "--repo", help="GitHub repository, owner/repo"),
    workflow: str | None = typer.Option(None, "--workflow", help="GitHub workflow file, e.g. release.yml"),
    gitlab_project: int | None = typer.Option(
        None,
        "--gitlab-project",
        min=1,
        help="GitLab CI instead of GitHub: the numeric project id (Settings > General)",
    ),
    gitlab_host: str | None = typer.Option(
        None,
        "--gitlab-host",
        help="GitLab instance origin (default https://gitlab.com; must be enabled on the server)",
    ),
    gitlab_project_path: str | None = typer.Option(
        None, "--gitlab-project-path", help="group/project, shown next to the trust (matching uses the id)"
    ),
    gitlab_namespace_id: int | None = typer.Option(
        None,
        "--gitlab-namespace-id",
        "--gitlab-namespace",
        min=1,
        help="Pin this numeric group/namespace id. Default: look up the project's current namespace id "
        "from the GitLab API (GITLAB_TOKEN or CI_JOB_TOKEN is used when set)",
    ),
    no_namespace_pin: bool = typer.Option(
        False,
        "--no-namespace-pin",
        help="GitLab only: do not pin the namespace. A project transferred to another namespace keeps "
        "matching this trust, so this is discouraged",
    ),
    environment: str | None = typer.Option(
        None,
        "--environment",
        help="Require this deployment environment. On GitLab the job names its own environment, so "
        "this only adds security when the environment is protected in GitLab",
    ),
    require_protected_environment: bool = typer.Option(
        False,
        "--require-protected-environment",
        help="GitLab only, with --environment: also require GitLab to report the environment as "
        "protected (protected environments are a GitLab Premium feature)",
    ),
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
    """Allow a GitHub Actions workflow (--repo/--workflow) or a GitLab CI
    project (--gitlab-project) to publish for this publisher. The trust binds
    to numeric ids, so a renamed or recreated repository or project does not
    inherit it. GitLab jobs must run on a protected ref and never from a
    merge request pipeline."""
    github_flags = [
        flag
        for flag, value in (
            ("--repo", repo),
            ("--workflow", workflow),
            ("--reusable-workflow", reusable_workflow),
            ("--repository-id", repository_id),
            ("--repository-owner-id", repository_owner_id),
        )
        if value is not None
    ] + (["--allow-workflow-run"] if allow_workflow_run else [])
    gitlab_flags = [
        flag
        for flag, value in (
            ("--gitlab-project", gitlab_project),
            ("--gitlab-host", gitlab_host),
            ("--gitlab-project-path", gitlab_project_path),
            ("--gitlab-namespace-id", gitlab_namespace_id),
        )
        if value is not None
    ] + (["--require-protected-environment"] if require_protected_environment else []) + (
        ["--no-namespace-pin"] if no_namespace_pin else []
    )
    if require_protected_environment and environment is None:
        raise typer.BadParameter("--require-protected-environment needs --environment.")
    if gitlab_flags:
        if github_flags:
            raise typer.BadParameter(
                f"GitHub and GitLab options are mutually exclusive ({', '.join(github_flags + gitlab_flags)})."
            )
        if gitlab_project is None:
            raise typer.BadParameter("--gitlab-project (the numeric project id) is required for GitLab.")
        if no_namespace_pin and gitlab_namespace_id is not None:
            raise typer.BadParameter("--no-namespace-pin and --gitlab-namespace cannot be combined.")
        if no_namespace_pin:
            err_console.print(
                "[yellow]Warning:[/yellow] the namespace is not pinned. If this GitLab project is transferred "
                "to another namespace, pipelines there on protected refs can still publish under this trust."
            )
        elif gitlab_namespace_id is None:
            try:
                gitlab_namespace_id = _lookup_gitlab_namespace_id(gitlab_host, gitlab_project)
            except ValueError as exc:
                raise typer.BadParameter(
                    f"Could not look up the namespace of project #{gitlab_project}: {exc}. "
                    "Pass --gitlab-namespace ID to pin it explicitly, or --no-namespace-pin to skip pinning."
                ) from exc
            console.print(f"Pinning GitLab namespace id {gitlab_namespace_id}.")
        gitlab_payload: dict[str, Any] = {"provider": "gitlab", "gitlab_project_id": gitlab_project}
        if require_protected_environment:
            gitlab_payload["require_protected_environment"] = True
        for key, value in (
            ("gitlab_host", gitlab_host),
            ("repository", gitlab_project_path),
            ("gitlab_namespace_id", gitlab_namespace_id),
            ("environment", environment),
            ("ref_pattern", ref_pattern),
            ("package_name", package),
        ):
            if value is not None:
                gitlab_payload[key] = value
        row = _request("POST", f"/publishers/{_segment(publisher_id)}/trusted-publishers", gitlab_payload)
        console.print(
            Text(
                f"Trusted GitLab project #{row.get('gitlab_project_id')} ({row.get('repository')}) "
                f"on {row.get('gitlab_host')} as {row.get('id')}",
                style="green",
            )
        )
        for warning in row.get("warnings") or []:
            err_console.print(Text(f"Warning: {warning}", style="yellow"))
        return
    if repo is None or workflow is None:
        raise typer.BadParameter("Pass --repo and --workflow (GitHub) or --gitlab-project (GitLab).")
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
