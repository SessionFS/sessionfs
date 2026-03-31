"""Security scanning and compliance commands: sfs security scan, sfs security fix."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer

from sessionfs.cli.common import console, err_console

security_app = typer.Typer(
    name="security", help="Security scanning and compliance.", no_args_is_help=True
)

# Pattern for SessionFS API keys
_API_KEY_PATTERN = re.compile(r"sk_sfs_[A-Za-z0-9_-]{10,}")

# Files to check for leaked API keys
_SHELL_CONFIG_FILES = [
    ".env",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".zprofile",
    ".profile",
]


def _check_config_permissions() -> tuple[bool, str]:
    """Check that config.toml has restrictive permissions (600)."""
    config_path = Path.home() / ".sessionfs" / "config.toml"
    if not config_path.exists():
        return True, "config.toml not found (not yet initialized)"

    mode = config_path.stat().st_mode
    file_perms = stat.S_IMODE(mode)
    expected = stat.S_IRUSR | stat.S_IWUSR  # 0o600

    if file_perms == expected:
        return True, f"OK ({oct(file_perms)})"
    else:
        return False, f"Insecure ({oct(file_perms)}, expected 0o600)"


def _check_api_key_exposure() -> tuple[bool, str, list[str]]:
    """Scan common shell config files for leaked API keys."""
    home = Path.home()
    found_in: list[str] = []

    for filename in _SHELL_CONFIG_FILES:
        filepath = home / filename
        if not filepath.exists():
            continue
        try:
            content = filepath.read_text(errors="ignore")
            if _API_KEY_PATTERN.search(content):
                found_in.append(str(filepath))
        except OSError:
            continue

    # Also check .env in current directory
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        try:
            content = cwd_env.read_text(errors="ignore")
            if _API_KEY_PATTERN.search(content):
                found_in.append(str(cwd_env))
        except OSError:
            pass

    if found_in:
        details = ", ".join(found_in)
        return False, f"API keys found in: {details}", found_in
    return True, "No keys found in shell configs", []


def _check_dependencies() -> tuple[bool, str, list[dict]]:
    """Run pip-audit if available and report vulnerabilities."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip_audit", "--format=json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        return True, "pip-audit not installed (run: pip install pip-audit)", []
    except subprocess.TimeoutExpired:
        return True, "pip-audit timed out", []

    import json

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        if result.returncode != 0:
            return False, "pip-audit failed to run", []
        return True, "No vulnerabilities found", []

    vulns = []
    deps = data.get("dependencies", [])
    for dep in deps:
        for v in dep.get("vulns", []):
            vulns.append(
                {
                    "name": dep["name"],
                    "version": dep["version"],
                    "id": v.get("id", "unknown"),
                    "fix_versions": v.get("fix_versions", []),
                    "description": v.get("description", ""),
                }
            )

    if vulns:
        return False, f"{len(vulns)} vulnerabilities found", vulns
    return True, "No vulnerabilities found", []


def _check_stale_sessions() -> tuple[bool, str, int]:
    """Check for sessions older than 90 days."""
    try:
        from sessionfs.cli.common import open_store

        store = open_store(initialize=False)
    except Exception:
        return True, "Could not open store", 0

    try:
        sessions = store.list_sessions()
        stale_count = 0
        now = datetime.now(timezone.utc)

        for s in sessions:
            created = s.get("created_at") or s.get("started_at")
            if not created:
                continue
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_days = (now - ts).total_seconds() / 86400.0
                if age_days > 90:
                    stale_count += 1
            except (ValueError, TypeError):
                continue

        if stale_count > 0:
            return (
                True,
                f"{stale_count} sessions older than 90 days (run: sfs storage prune)",
                stale_count,
            )
        return True, "No stale sessions", 0
    finally:
        store.close()


@security_app.command("scan")
def scan() -> None:
    """Run local security checks."""
    console.print()
    console.print("[bold]Security Scan Results[/bold]")
    console.print("\u2500" * 40)

    issues = 0

    # 1. Config permissions
    ok, msg = _check_config_permissions()
    if ok:
        console.print(f"[green]\u2713[/green] Config permissions: {msg}")
    else:
        console.print(f"[red]\u2717[/red] Config permissions: {msg}")
        issues += 1

    # 2. API key exposure
    ok, msg, _found = _check_api_key_exposure()
    if ok:
        console.print(f"[green]\u2713[/green] API key exposure: {msg}")
    else:
        console.print(f"[red]\u2717[/red] API key exposure: {msg}")
        issues += 1

    # 3. Dependency audit
    ok, msg, vulns = _check_dependencies()
    if ok:
        console.print(f"[green]\u2713[/green] Dependencies: {msg}")
    else:
        console.print(f"[red]\u2717[/red] Dependencies: {msg}")
        for v in vulns:
            fix = v["fix_versions"][0] if v["fix_versions"] else "no fix available"
            console.print(
                f"  - {v['name']} {v['version']} -> {v['id']} -- upgrade to {fix}"
            )
        issues += 1

    # 4. Stale sessions
    ok, msg, _count = _check_stale_sessions()
    if ok:
        console.print(f"[green]\u2713[/green] Stale sessions: {msg}")
    else:
        console.print(f"[red]\u2717[/red] Stale sessions: {msg}")
        issues += 1

    console.print()
    if issues > 0:
        console.print(
            f"[yellow]{issues} issue(s) found. "
            f"Run 'sfs security fix' to auto-fix.[/yellow]"
        )
    else:
        console.print("[green]All checks passed.[/green]")


@security_app.command("fix")
def fix(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompts."),
) -> None:
    """Auto-fix security issues where safe to do so."""
    console.print()
    console.print("[bold]Security Fix[/bold]")
    console.print("\u2500" * 40)

    fixed = 0

    # 1. Fix config permissions
    config_path = Path.home() / ".sessionfs" / "config.toml"
    if config_path.exists():
        mode = stat.S_IMODE(config_path.stat().st_mode)
        expected = stat.S_IRUSR | stat.S_IWUSR
        if mode != expected:
            os.chmod(config_path, expected)
            console.print(
                f"[green]\u2713[/green] Fixed config permissions: "
                f"{oct(mode)} -> {oct(expected)}"
            )
            fixed += 1
        else:
            console.print("[green]\u2713[/green] Config permissions already correct")
    else:
        console.print("[dim]- Config file not found (skipped)[/dim]")

    # 2. Upgrade vulnerable packages
    _ok, _msg, vulns = _check_dependencies()
    if vulns:
        packages_to_upgrade = []
        for v in vulns:
            if v["fix_versions"]:
                packages_to_upgrade.append(
                    f"{v['name']}>={v['fix_versions'][0]}"
                )

        if packages_to_upgrade:
            console.print()
            console.print("[bold]Vulnerable packages with available fixes:[/bold]")
            for pkg in packages_to_upgrade:
                console.print(f"  - {pkg}")
            console.print()

            if yes or typer.confirm("Upgrade these packages?"):
                try:
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install", "--upgrade"]
                        + packages_to_upgrade,
                        check=True,
                    )
                    console.print(
                        f"[green]\u2713[/green] Upgraded {len(packages_to_upgrade)} packages"
                    )
                    fixed += len(packages_to_upgrade)
                except subprocess.CalledProcessError:
                    err_console.print("[red]Failed to upgrade some packages.[/red]")
            else:
                console.print("[dim]Skipped package upgrades.[/dim]")
        else:
            console.print(
                "[yellow]- Vulnerabilities found but no fix versions available[/yellow]"
            )
    else:
        console.print("[green]\u2713[/green] No vulnerable packages to fix")

    # 3. API key exposure — warn only
    _ok, _msg, found = _check_api_key_exposure()
    if found:
        console.print()
        console.print(
            "[yellow]API keys found in config files (manual removal required):[/yellow]"
        )
        for f in found:
            console.print(f"  - {f}")
        console.print(
            "[dim]Remove keys and use 'sfs config set api_key <key>' instead.[/dim]"
        )

    # 4. Stale sessions — warn only
    _ok, msg, count = _check_stale_sessions()
    if count > 0:
        console.print()
        console.print(
            f"[yellow]{count} stale sessions found. "
            f"Run 'sfs storage prune' to clean up.[/yellow]"
        )

    console.print()
    if fixed > 0:
        console.print(f"[green]Fixed {fixed} issue(s).[/green]")
    else:
        console.print("[dim]Nothing to auto-fix.[/dim]")
