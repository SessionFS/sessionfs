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
