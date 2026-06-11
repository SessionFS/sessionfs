"""Cloud sync commands: sfs auth, sfs push, sfs pull, sfs sync, sfs handoff."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional

import typer
from rich.table import Table

from sessionfs.cli.common import (
    console,
    err_console,
    get_session_dir_or_exit,
    open_store,
    resolve_session_id,
)

auth_app = typer.Typer(name="auth", help="Authentication for cloud sync.")


# Final-fallback per-file cap. Used only when the env-var override is
# unset AND server discovery via GET /sync/settings fails. See
# `_resolve_max_member_size` for the full precedence chain.
import os as _os
MAX_MEMBER_SIZE = int(
    _os.environ.get("SFS_MAX_SYNC_MEMBER_BYTES_PAID", str(50 * 1024 * 1024))
)

# Per-process cache so each `sfs` invocation hits /sync/settings at most
# once. Reset between tests via `_reset_max_member_size_cache()`.
_max_member_size_cache: int | None = None


def _resolve_max_member_size(
    api_url: str | None,
    api_key: str | None,
    *,
    force_refresh: bool = False,
) -> int:
    """Return the effective per-file byte cap for sync uploads.

    Precedence (per tk_d5945c4bce3245ce, parent Issue tk_714456298d424202):
      1. `SFS_MAX_SYNC_MEMBER_BYTES_PAID` env var — explicit operator override.
         Always wins; lets customers experiment ahead of the server.
      2. Server-supplied `max_member_bytes` from `GET /sync/settings` — the
         tier-aware value the server enforces (added by tk_bb56f6a56da34b05).
      3. Hardcoded 50 MB literal — final fallback. Logged with a warning
         so operators can spot the misconfiguration.

    Cached per CLI invocation; subsequent calls return the cached value
    without re-hitting the server. Older servers that don't return
    `max_member_bytes` fall through to (3) without crashing.
    """
    global _max_member_size_cache
    if _max_member_size_cache is not None and not force_refresh:
        return _max_member_size_cache

    # (1) Explicit env-var override always wins. Re-read each call so the
    # caller can mutate it between invocations in tests; the cache then
    # captures the post-override value for subsequent reads.
    env_override = _os.environ.get("SFS_MAX_SYNC_MEMBER_BYTES_PAID")
    if env_override:
        try:
            _max_member_size_cache = int(env_override)
            return _max_member_size_cache
        except ValueError:
            # Malformed env var — log and fall through to server discovery.
            import logging as _logging
            _logging.getLogger("sessionfs.cli").warning(
                "SFS_MAX_SYNC_MEMBER_BYTES_PAID=%r is not an integer; "
                "ignoring and discovering from server.",
                env_override,
            )

    # (2) Server discovery — short timeout so a slow server doesn't block
    # every `sfs push`. Older servers that pre-date Task 2 won't return
    # the field; we treat that as "no server value" and fall through.
    if api_url and api_key:
        try:
            import httpx as _httpx
            resp = _httpx.get(
                f"{api_url.rstrip('/')}/api/v1/sync/settings",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict):
                    raw = body.get("max_member_bytes")
                    if isinstance(raw, int) and raw > 0:
                        _max_member_size_cache = raw
                        return _max_member_size_cache
        except Exception:
            # Network error / DNS / 5xx / JSON parse error — fall through.
            pass

    # (3) Final fallback.
    import logging as _logging
    _logging.getLogger("sessionfs.cli").warning(
        "Could not discover sync member-size cap from server; "
        "falling back to %d MB literal. Set SFS_MAX_SYNC_MEMBER_BYTES_PAID "
        "to silence this warning, or upgrade the server to v0.10.27+.",
        50,
    )
    _max_member_size_cache = 50 * 1024 * 1024
    return _max_member_size_cache


def _reset_max_member_size_cache() -> None:
    """Test-only: clear the per-process cache between cases."""
    global _max_member_size_cache
    _max_member_size_cache = None


def _find_oversized_member(
    archive_data: bytes,
    max_size: int | None = None,
) -> tuple[str, int] | None:
    """Return (name, size) of the first archive member exceeding `max_size`.

    `max_size` defaults to `MAX_MEMBER_SIZE` when callers haven't resolved
    the tier-aware value yet (e.g. older code paths). Newer call sites
    should resolve via `_resolve_max_member_size()` once at command entry
    and pass the result here.
    """
    import io as _io
    import tarfile as _tarfile

    if max_size is None:
        max_size = MAX_MEMBER_SIZE

    try:
        with _tarfile.open(fileobj=_io.BytesIO(archive_data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.size > max_size:
                    return member.name, member.size
    except _tarfile.TarError:
        # Bad archive — let the server-side validation surface a clean error.
        return None
    return None


def _load_sync_config() -> dict:
    """Load sync config for the active profile.

    tk_457d060822bc48c0 — resolves through the SHARED profile resolver
    so push/pull/sync use the same identity as ticket/persona/agent.
    Precedence: SESSIONFS_API_KEY > SESSIONFS_PROFILE > active_profile
    > default (config.toml). `enabled` is read from config.toml for the
    default profile (back-compat with the daemon's sync.enabled gate);
    named profiles + the env-key path are enabled-by-construction.
    """
    from sessionfs.profiles import DEFAULT_PROFILE, resolve_auth

    auth = resolve_auth()
    enabled = True
    if auth.source == "profile" and auth.profile_name == DEFAULT_PROFILE:
        # Honor the daemon's sync.enabled flag for the default profile so
        # existing "sync disabled until login" behavior is unchanged.
        from sessionfs.daemon.config import load_config

        enabled = load_config().sync.enabled
    return {
        "api_url": auth.api_url,
        "api_key": auth.api_key,
        "enabled": enabled,
    }


def _save_sync_config(api_url: str, api_key: str) -> None:
    """Update the sync section in config.toml."""
    import sys

    if sys.version_info >= (3, 11):
        pass
    else:
        pass

    from sessionfs.daemon.config import DEFAULT_CONFIG_PATH, ensure_config

    ensure_config()
    config_path = DEFAULT_CONFIG_PATH

    if config_path.exists():
        text = config_path.read_text()
    else:
        text = ""

    # Simple TOML update: replace or append [sync] section
    lines = text.split("\n")
    new_lines = []
    in_sync_section = False
    sync_written = False

    for line in lines:
        stripped = line.strip()
        if stripped == "[sync]":
            in_sync_section = True
            sync_written = True
            new_lines.append("[sync]")
            new_lines.append('enabled = true')
            new_lines.append(f'api_url = "{api_url}"')
            new_lines.append(f'api_key = "{api_key}"')
            new_lines.append('push_interval = 30')
            new_lines.append('retry_max = 5')
            continue
        if in_sync_section:
            if stripped.startswith("[") and stripped != "[sync]":
                in_sync_section = False
                new_lines.append(line)
            # Skip old sync lines
            continue
        new_lines.append(line)

    if not sync_written:
        new_lines.append("")
        new_lines.append("[sync]")
        new_lines.append('enabled = true')
        new_lines.append(f'api_url = "{api_url}"')
        new_lines.append(f'api_key = "{api_key}"')
        new_lines.append('push_interval = 30')
        new_lines.append('retry_max = 5')

    # 0o600 enforced at creation — holds a raw key (Shield-SR v0.10.29:
    # write_text + chmod left a umask-default window on first login).
    from sessionfs.profiles import _write_private_file
    _write_private_file(config_path, "\n".join(new_lines))


def _get_sync_client():
    """Create a SyncClient from stored config."""
    from sessionfs.sync.client import SyncClient

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print(
            "[red]Not authenticated. Run 'sfs auth login' first.[/red]"
        )
        raise SystemExit(1)
    return SyncClient(api_url=cfg["api_url"], api_key=cfg["api_key"])


@auth_app.command("login")
def auth_login(
    api_url: str = typer.Option(
        "https://api.sessionfs.dev", "--url", help="Server URL."
    ),
    api_key: Optional[str] = typer.Option(
        None, "--key", help="API key (will prompt if not provided)."
    ),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help=(
            "Save under a named profile for multi-account use "
            "(default: the 'default' profile / config.toml). Switch with "
            "`sfs auth use <name>`."
        ),
    ),
) -> None:
    """Authenticate with a SessionFS server.

    tk_457d060822bc48c0 — pass `--profile <name>` to store the key as a
    named profile (a second account on the same device). Without it, the
    key lands in the `default` profile (config.toml), unchanged behavior.
    """
    from sessionfs.profiles import (
        DEFAULT_PROFILE,
        is_valid_profile_name,
        write_named_profile,
    )

    if profile is not None:
        profile = profile.strip()
        if not is_valid_profile_name(profile):
            err_console.print(
                f"[red]Invalid profile name '{profile}'.[/red] Use lowercase "
                "letters, digits, '-' or '_' (max 64 chars)."
            )
            raise SystemExit(2)
        if profile == DEFAULT_PROFILE:
            profile = None  # 'default' is just config.toml

    if not api_key:
        api_key = typer.prompt("Enter your API key")

    if not api_key:
        err_console.print("[red]API key is required.[/red]")
        raise SystemExit(1)

    # Verify the key works
    from sessionfs.sync.client import SyncClient, SyncAuthError, SyncError

    client = SyncClient(api_url=api_url, api_key=api_key)

    async def _verify():
        try:
            return await client.health_check()
        finally:
            await client.close()

    console.print(f"Verifying key against {api_url}...")
    try:
        result = asyncio.run(_verify())
    except SyncAuthError:
        err_console.print("[red]Authentication failed. Check your API key.[/red]")
        raise SystemExit(1)
    except SyncError as exc:
        err_console.print(f"[red]Could not reach server: {exc}[/red]")
        raise SystemExit(1)
    except Exception as exc:
        err_console.print(f"[red]Connection error: {exc}[/red]")
        raise SystemExit(1)

    # Save: named profile → its own file; default → config.toml (preserves
    # the watcher sections etc. via the surgical [sync]-block updater).
    if profile is not None:
        write_named_profile(profile, api_url, api_key)
        console.print(f"[green]Authenticated with {api_url}[/green] "
                      f"(profile: [bold]{profile}[/bold])")
        console.print(f"Server status: {result.get('status', 'unknown')}")
        console.print(
            f"Switch to it with [bold]sfs auth use {profile}[/bold] "
            f"or [bold]SESSIONFS_PROFILE={profile}[/bold] in a shell."
        )
    else:
        _save_sync_config(api_url, api_key)
        console.print(f"[green]Authenticated with {api_url}[/green]")
        console.print(f"Server status: {result.get('status', 'unknown')}")
        console.print("Cloud sync is now enabled. The daemon will push sessions automatically.")


@auth_app.command("use")
def auth_use(
    name: str = typer.Argument(..., help="Profile name to activate (e.g. 'default')."),
) -> None:
    """Set the active auth profile for all subsequent commands.

    tk_457d060822bc48c0 — persists the choice so every command (push,
    ticket, sync, agent, …) in any new shell uses this identity, until
    overridden by SESSIONFS_PROFILE or SESSIONFS_API_KEY.
    """
    from sessionfs.profiles import (
        DEFAULT_PROFILE,
        is_valid_profile_name,
        list_profiles,
        profile_config_path,
        set_active_profile,
    )

    name = name.strip()
    if not is_valid_profile_name(name):
        err_console.print(f"[red]Invalid profile name '{name}'.[/red]")
        raise SystemExit(2)
    if name != DEFAULT_PROFILE and not profile_config_path(name).exists():
        known = ", ".join(list_profiles()) or "(none)"
        err_console.print(
            f"[red]Profile '{name}' not found.[/red] Known profiles: {known}. "
            f"Create one with [bold]sfs auth login --profile {name}[/bold]."
        )
        raise SystemExit(1)
    set_active_profile(name)
    console.print(f"[green]Active profile is now '{name}'.[/green]")


@auth_app.command("profiles")
def auth_profiles() -> None:
    """List known auth profiles and mark the active one."""
    from sessionfs.profiles import list_profiles, resolve_active_profile_name

    names = list_profiles()
    if not names:
        console.print("[dim]No profiles yet. Run `sfs auth login`.[/dim]")
        return
    active = resolve_active_profile_name()
    for n in names:
        marker = "[green]*[/green]" if n == active else " "
        console.print(f" {marker} {n}")
    import os as _os
    if _os.environ.get("SESSIONFS_API_KEY"):
        console.print(
            "[yellow]Note:[/yellow] SESSIONFS_API_KEY is set — it overrides "
            "the active profile for this shell."
        )
    elif _os.environ.get("SESSIONFS_PROFILE"):
        console.print(
            f"[dim]SESSIONFS_PROFILE={_os.environ['SESSIONFS_PROFILE']} "
            f"is set for this shell.[/dim]"
        )


@auth_app.command("whoami")
def auth_whoami() -> None:
    """Show the active profile + the account it resolves to (one /auth/me call)."""
    from sessionfs.profiles import resolve_auth

    auth = resolve_auth()
    if auth.source == "env_key":
        label = "SESSIONFS_API_KEY (env)"
    else:
        label = f"profile '{auth.profile_name}'"
    if not auth.api_key:
        err_console.print(
            f"[red]Not authenticated[/red] ({label}). Run `sfs auth login`."
        )
        raise SystemExit(1)

    import httpx
    try:
        resp = httpx.get(
            f"{auth.api_url}/api/v1/auth/me",
            headers={"Authorization": f"Bearer {auth.api_key}"},
            timeout=10.0,
        )
    except Exception as exc:
        err_console.print(f"[red]Could not reach server: {exc}[/red]")
        raise SystemExit(1)
    if resp.status_code != 200:
        err_console.print(
            f"[red]Auth check failed (HTTP {resp.status_code}) for {label}.[/red]"
        )
        raise SystemExit(1)
    me = resp.json()
    console.print(f"Identity:  [bold]{me.get('email', '?')}[/bold]")
    console.print(f"Resolved from: {label}")
    console.print(f"Server:    {auth.api_url}")
    console.print(f"Tier:      {me.get('tier', '?')}")


@auth_app.command("signup")
def auth_signup(
    email: str = typer.Option(..., prompt="Email", help="Your email address."),
    api_url: str = typer.Option(
        "https://api.sessionfs.dev", "--url", help="Server URL."
    ),
) -> None:
    """Create a new account and get your first API key."""
    import httpx

    try:
        resp = httpx.post(
            f"{api_url}/api/v1/auth/signup",
            json={"email": email},
            timeout=15.0,
        )
    except httpx.ConnectError:
        err_console.print(f"[red]Could not reach server at {api_url}[/red]")
        raise SystemExit(1)

    if resp.status_code == 409:
        err_console.print("[red]Email already registered. Use 'sfs auth login' instead.[/red]")
        raise SystemExit(1)

    if resp.status_code != 201:
        err_console.print(f"[red]Signup failed: {resp.text}[/red]")
        raise SystemExit(1)

    data = resp.json()
    raw_key = data["raw_key"]

    # Save to config
    _save_sync_config(api_url, raw_key)
    console.print(f"[green]Account created for {email}[/green]")
    console.print(f"  API key: {raw_key}")
    console.print("  [bold]Save this key — it won't be shown again.[/bold]")
    console.print("Cloud sync is now enabled.")


@auth_app.command("status")
def auth_status() -> None:
    """Show current authentication status."""
    cfg = _load_sync_config()
    if not cfg["api_key"]:
        console.print("[dim]Not authenticated. Run 'sfs auth login'.[/dim]")
        return

    console.print("[green]Authenticated[/green]")
    console.print(f"  Server: {cfg['api_url']}")
    console.print(f"  API key: {cfg['api_key'][:12]}...")

    # Fetch user profile from server
    try:
        import httpx

        resp = httpx.get(
            f"{cfg['api_url']}/api/v1/auth/me",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=10,
        )
        if resp.status_code == 200:
            me = resp.json()
            console.print(f"  Email: [cyan]{me.get('email', '?')}[/cyan]")
            console.print(f"  Tier: {me.get('tier', 'free')}")
            verified = me.get("email_verified", False)
            console.print(f"  Verified: {'[green]yes[/green]' if verified else '[yellow]no — check your inbox[/yellow]'}")
        else:
            console.print(f"  [dim]Could not fetch profile (HTTP {resp.status_code})[/dim]")
    except Exception:
        console.print("  [dim]Could not reach server[/dim]")

    console.print(f"  Sync enabled: {cfg['enabled']}")


def push(
    session_id: str = typer.Argument(help="Session ID or prefix."),
    yes: bool = typer.Option(
        False,
        "--yes", "-y",
        help="Skip interactive confirmations (DLP findings, cloud-undelete). Findings are still shown but don't block the push.",
    ),
) -> None:
    """Push a local session to the server."""
    from sessionfs.sync.archive import pack_session
    from sessionfs.sync.client import SyncConflictError, SyncDeletedError, SyncTooLargeError

    store = open_store()
    client = _get_sync_client()

    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        # DLP pre-scan: only when org has DLP enabled
        # Check org policy via API before scanning locally
        dlp_enabled = False
        try:
            import httpx as _httpx
            _sync_cfg = _load_sync_config()
            _dlp_resp = _httpx.get(
                f"{_sync_cfg['api_url']}/api/v1/dlp/policy",
                headers={"Authorization": f"Bearer {_sync_cfg['api_key']}"},
                timeout=5.0,
            )
            if _dlp_resp.status_code == 200:
                _dlp_data = _dlp_resp.json()
                dlp_enabled = _dlp_data.get("enabled", False)
        except Exception:
            pass  # Can't reach API — skip DLP preview

        if dlp_enabled:
            messages_path = session_dir / "messages.jsonl"
            if messages_path.is_file():
                from sessionfs.security.secrets import scan_dlp, DLPFinding

                text = messages_path.read_text(encoding="utf-8", errors="replace")
                findings: list[DLPFinding] = scan_dlp(text, categories=["secrets"])
                if findings:
                    console.print(
                        f"\n[yellow]DLP scan found {len(findings)} potential secret(s):[/yellow]"
                    )
                    by_severity: dict[str, int] = {}
                    for f in findings:
                        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
                    for sev in ("critical", "high", "medium", "low"):
                        count = by_severity.get(sev, 0)
                        if count:
                            console.print(f"  {sev}: {count}")

                    if yes:
                        console.print(
                            "[dim]--yes: proceeding despite DLP findings.[/dim]"
                        )
                    else:
                        from sessionfs.cli.common import confirm_or_exit
                        # In a piped / non-TTY shell, this exits with a
                        # clean message instead of letting click.confirm
                        # hit EOF → Abort → "Unexpected error:".
                        if not confirm_or_exit(
                            "Continue pushing with these findings?",
                            default=False,
                            yes_hint=(
                                "Pass --yes to push despite DLP findings "
                                "in non-interactive mode."
                            ),
                        ):
                            console.print("[dim]Push cancelled.[/dim]")
                            return

        # Check if session is in the local exclusion list (deleted from cloud).
        # Scope the exclusion store to the active profile (tk_457d060822bc48c0
        # R1 MED #3) so two accounts don't share one deleted.json/backoff file.
        from sessionfs.store.deleted import is_excluded, get_entry, remove_exclusion
        from sessionfs.cli.common import get_store_dir
        _del_base = get_store_dir()

        extra_headers: dict[str, str] = {}
        if is_excluded(full_id, base_dir=_del_base):
            entry = get_entry(full_id, base_dir=_del_base)
            scope = entry.get("scope", "?") if entry else "?"
            from sessionfs.cli.common import confirm_or_exit
            if not confirm_or_exit(
                f"This session was deleted from the cloud (scope={scope}). Push anyway?",
                default=False,
                yes=yes,
                yes_hint="Pass --yes to undelete + push in non-interactive mode.",
            ):
                console.print("[dim]Push cancelled.[/dim]")
                return
            extra_headers["X-SessionFS-Undelete"] = "true"

        console.print(f"Packing session {full_id[:12]}...")
        archive_data = pack_session(session_dir)

        # Resolve the server-advertised member-size cap once per command.
        # Cached so the unpack path below sees the same value.
        _sync_cfg = _load_sync_config()
        max_member_size = _resolve_max_member_size(
            _sync_cfg.get("api_url"), _sync_cfg.get("api_key")
        )

        # Local size check: don't waste an upload for a session the server
        # will reject with 413. This typically means the user needs to
        # /clear or /compact in their AI tool.
        oversized = _find_oversized_member(archive_data, max_size=max_member_size)
        if oversized is not None:
            name, size = oversized
            err_console.print(
                f"[red]Session too large to push:[/red] '{name}' is "
                f"{size // (1024 * 1024)}MB (server hard cap "
                f"{max_member_size // (1024 * 1024)}MB per file).\n"
                f"[yellow]Try /compact in your AI tool to start a fresh "
                f"session, then push again. Free tier has a tighter "
                f"{10}MB cap; Pro/Team/Enterprise get the full {max_member_size // (1024 * 1024)}MB.[/yellow]"
            )
            raise SystemExit(1)

        # Read local etag
        manifest = store.get_session_manifest(full_id)
        local_etag = (manifest or {}).get("sync", {}).get("etag")

        async def _push():
            try:
                return await client.push_session(
                    full_id, archive_data, etag=local_etag,
                    extra_headers=extra_headers if extra_headers else None,
                )
            finally:
                await client.close()

        console.print(f"Pushing ({len(archive_data):,} bytes)...")
        result = asyncio.run(_push())

        # On successful push of a previously deleted session, clear exclusion
        if is_excluded(full_id, base_dir=_del_base):
            remove_exclusion(full_id, base_dir=_del_base)

        # Update local manifest with new etag
        _update_manifest_sync(session_dir, result.etag)

        action = "Created" if result.created else "Updated"
        console.print(
            f"[green]{action} remote session {result.session_id}[/green]\n"
            f"  ETag: {result.etag[:16]}...\n"
            f"  Size: {result.blob_size_bytes:,} bytes"
        )

    except SyncDeletedError:
        from sessionfs.sync.deleted_cleanup import cleanup_deleted_session
        cleanup_deleted_session(full_id, session_dir, store, base_dir=_del_base)
        err_console.print(
            f"[yellow]Session {full_id[:12]} has been deleted on the server.[/yellow]\n"
            f"Local copy cleaned up. Restore with: sfs restore {full_id}"
        )
        raise SystemExit(1)
    except SyncTooLargeError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)
    except SyncConflictError as exc:
        err_console.print(
            f"[red]Conflict: remote session was updated (etag={exc.current_etag[:16]}...).[/red]\n"
            f"Pull the latest version first with: sfs pull {session_id}"
        )
        raise SystemExit(1)
    finally:
        store.close()


def pull(
    session_id: str = typer.Argument(help="Session ID."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local without prompting."),
) -> None:
    """Pull a session from the server."""
    from sessionfs.sync.archive import unpack_session

    store = open_store()
    client = _get_sync_client()

    try:
        # Check if session exists locally
        local_dir = store.get_session_dir(session_id)
        local_etag = None

        if local_dir:
            manifest = store.get_session_manifest(session_id)
            local_etag = (manifest or {}).get("sync", {}).get("etag")

        async def _pull():
            try:
                return await client.pull_session(session_id, etag=local_etag)
            finally:
                await client.close()

        console.print(f"Pulling session {session_id[:12]}...")
        result = asyncio.run(_pull())

        if result.not_modified:
            console.print("[dim]Session is up to date (304 Not Modified).[/dim]")
            return

        if result.data is None:
            err_console.print("[red]No data received from server.[/red]")
            raise SystemExit(1)

        # If local exists and differs, prompt
        if local_dir and not force:
            if not typer.confirm("Local session differs from remote. Overwrite local?", default=True):
                console.print("[dim]Pull cancelled.[/dim]")
                return

        # Extract to local store
        target_dir = store.allocate_session_dir(session_id)
        _sync_cfg = _load_sync_config()
        unpack_session(
            result.data,
            target_dir,
            member_limit_bytes=_resolve_max_member_size(
                _sync_cfg.get("api_url"), _sync_cfg.get("api_key")
            ),
        )

        # Update index
        manifest_path = target_dir / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            store.upsert_session_metadata(session_id, manifest, str(target_dir))

        # Store etag
        _update_manifest_sync(target_dir, result.etag)

        # Explicit pull overrides exclusion — remove from deleted.json
        # (active-profile-scoped store, tk_457d060822bc48c0 R1 MED #3).
        from sessionfs.store.deleted import is_excluded, remove_exclusion
        from sessionfs.cli.common import get_store_dir
        _del_base = get_store_dir()
        if is_excluded(session_id, base_dir=_del_base):
            remove_exclusion(session_id, base_dir=_del_base)

        console.print(
            f"[green]Pulled session {session_id}[/green]\n"
            f"  ETag: {result.etag[:16]}...\n"
            f"  Size: {len(result.data):,} bytes"
        )

    finally:
        store.close()


def list_remote(
    page: int = typer.Option(1, help="Page number."),
    page_size: int = typer.Option(20, help="Sessions per page."),
    source_tool: Optional[str] = typer.Option(None, "--tool", help="Filter by tool."),
) -> None:
    """List sessions on the remote server."""
    client = _get_sync_client()
    store = open_store()

    async def _list():
        try:
            return await client.list_remote_sessions(
                page=page, page_size=page_size, source_tool=source_tool
            )
        finally:
            await client.close()

    try:
        result = asyncio.run(_list())

        if not result.sessions:
            console.print("[dim]No remote sessions found.[/dim]")
            return

        # Build set of local session IDs for comparison
        local_sessions = {s["session_id"] for s in store.list_sessions()}

        table = Table(title=f"Remote Sessions ({result.total} total)")
        table.add_column("ID", style="cyan", max_width=16)
        table.add_column("Tool", style="dim")
        table.add_column("Model")
        table.add_column("Msgs", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Local", justify="center")
        table.add_column("Title", max_width=30)

        for s in result.sessions:
            is_local = s.id in local_sessions
            size_kb = s.blob_size_bytes / 1024
            table.add_row(
                s.id[:16],
                s.source_tool,
                s.model_id or "",
                str(s.message_count),
                f"{size_kb:.1f}K",
                "[green]yes[/green]" if is_local else "[dim]no[/dim]",
                (s.title or "")[:30],
            )

        console.print(table)

        if result.has_more:
            console.print(f"[dim]Page {result.page} — more available (--page {result.page + 1})[/dim]")

    finally:
        store.close()


def sync_all() -> None:
    """Bidirectional sync: push local changes, pull remote-only sessions."""
    from sessionfs.sync.archive import pack_session, unpack_session
    from sessionfs.sync.client import SyncConflictError, SyncDeletedError

    # Construct the client FIRST so auth/config failures (no API key,
    # bad URL, etc.) exit before we touch the exclusion list.
    client = _get_sync_client()

    # NOTE: transient exclusions are NOT cleared in bulk here. They're
    # cleared per-session at the moment of retry (in _push_one /
    # _pull_one), so if sync crashes before reaching session X, X's
    # exclusion stays intact and the daemon's backoff guard keeps
    # working on the next cycle. This is the only way to guarantee
    # "exclusion cleared ⇒ retry actually attempted" — bulk-clearing
    # upfront breaks that invariant on any partial-failure path.

    store = open_store()

    # Active-profile-scoped exclusion store, captured once so both
    # _push_one and _pull_one closures use the same dir
    # (tk_457d060822bc48c0 R1 MED #3).
    from sessionfs.cli.common import get_store_dir
    _del_base = get_store_dir()

    # Resolve the tier-aware member-size cap once for this command.
    _sync_cfg = _load_sync_config()
    max_member_size = _resolve_max_member_size(
        _sync_cfg.get("api_url"), _sync_cfg.get("api_key")
    )

    async def _sync():
        pushed = 0
        pulled = 0
        conflicts = 0
        # Limit concurrent uploads to avoid overwhelming the server
        upload_sem = asyncio.Semaphore(5)

        try:
            # Get all remote sessions (paginate through all pages). The
            # first successful page-1 response is our "server is
            # reachable" signal — only after that do we clear
            # transient exclusions. If the network is down or the
            # server is 5xx, list_remote_sessions raises here, the
            # exclusion file stays intact, and the daemon's backoff
            # guard continues to do its job.
            remote_by_id: dict = {}
            page = 1
            while True:
                remote_result = await client.list_remote_sessions(page=page, page_size=100)
                for s in remote_result.sessions:
                    remote_by_id[s.id] = s
                if not remote_result.has_more:
                    break
                page += 1

            # Get local sessions
            local_sessions = store.list_sessions()
            local_by_id = {s["session_id"]: s for s in local_sessions}

            # Push local sessions not on remote or with different etag
            push_results: dict[str, str] = {}  # sid -> "pushed" | "conflict" | "error"

            async def _push_one(sid: str) -> None:
                nonlocal pushed, conflicts

                # Cheap unlocked pre-filter for the common case:
                # already a hard delete? Skip without doing filesystem
                # work. This is a hint, not a guarantee — the
                # authoritative gate is the atomic acquire_for_retry()
                # call immediately before the network request below.
                from sessionfs.store.deleted import (
                    TRANSIENT_REASONS,
                    acquire_for_retry,
                    get_entry,
                )

                hint = get_entry(sid, base_dir=_del_base)
                if (
                    hint is not None
                    and hint.get("reason") not in TRANSIENT_REASONS
                ):
                    return  # Already hard-deleted at snapshot time

                session_dir = store.get_session_dir(sid)
                if not session_dir:
                    return

                manifest = store.get_session_manifest(sid)
                local_etag = (manifest or {}).get("sync", {}).get("etag")

                remote = remote_by_id.get(sid)
                if remote and remote.etag == local_etag:
                    return  # Already in sync — no retry needed

                async with upload_sem:
                    try:
                        archive_data = pack_session(session_dir)
                        # Pre-upload oversize check — mirrors the same
                        # gate as `sfs push` and `sfs handoff` so the
                        # `sfs sync` upload path doesn't waste bandwidth
                        # on a guaranteed-413 archive. Codex R1 MEDIUM
                        # on tk_d5945c4bce3245ce — this site was the
                        # only `push_session()` caller still skipping
                        # the resolver-aware preflight.
                        oversized = _find_oversized_member(
                            archive_data, max_size=max_member_size
                        )
                        if oversized is not None:
                            name, size = oversized
                            push_results[sid] = "error"
                            err_console.print(
                                f"[red]Skipped {sid[:12]}: too large to push:[/red] "
                                f"'{name}' is {size // (1024 * 1024)}MB "
                                f"(server cap {max_member_size // (1024 * 1024)}MB per file). "
                                f"Try /compact in your AI tool first."
                            )
                            return
                        # Authoritative gate: atomically asks the
                        # deleted.json file "may I proceed?". Returns
                        # True if no entry OR a transient entry was
                        # cleared in the same critical section. Returns
                        # False if a hard delete is present (whether it
                        # was there at snapshot time or a concurrent
                        # writer added it between the snapshot and
                        # now). Closes the round-7 race where an
                        # unlocked-None-then-hard-delete sequence
                        # would have pushed the deleted session.
                        if not acquire_for_retry(sid, base_dir=_del_base):
                            return
                        result = await client.push_session(sid, archive_data, etag=local_etag)
                        _update_manifest_sync(session_dir, result.etag)
                        push_results[sid] = "pushed"
                    except SyncConflictError:
                        push_results[sid] = "conflict"
                        err_console.print(
                            f"[yellow]Conflict: {sid[:12]} — pull first[/yellow]"
                        )
                    except SyncDeletedError:
                        from sessionfs.sync.deleted_cleanup import cleanup_deleted_session
                        cleanup_deleted_session(sid, session_dir, store, base_dir=_del_base)
                        push_results[sid] = "skipped"
                    except Exception as exc:
                        push_results[sid] = "error"
                        err_console.print(f"[red]Push failed for {sid[:12]}: {exc}[/red]")

            push_tasks = [_push_one(sid) for sid in local_by_id]
            await asyncio.gather(*push_tasks)

            pushed = sum(1 for v in push_results.values() if v == "pushed")
            conflicts = sum(1 for v in push_results.values() if v == "conflict")
            push_errors = sum(1 for v in push_results.values() if v == "error")
            push_skipped = sum(1 for v in push_results.values() if v == "skipped")

            # Pull remote sessions not present locally (also concurrency-limited)
            pull_sem = asyncio.Semaphore(5)

            async def _pull_one(sid: str) -> bool:
                # Same shape as _push_one: cheap unlocked hint to
                # short-circuit known hard deletes, then a definitive
                # atomic gate inside the semaphore.
                from sessionfs.store.deleted import (
                    TRANSIENT_REASONS,
                    acquire_for_retry,
                    get_entry,
                )

                hint = get_entry(sid, base_dir=_del_base)
                if (
                    hint is not None
                    and hint.get("reason") not in TRANSIENT_REASONS
                ):
                    return False

                async with pull_sem:
                    try:
                        if not acquire_for_retry(sid, base_dir=_del_base):
                            return False
                        result = await client.pull_session(sid)
                        if result.data:
                            target_dir = store.allocate_session_dir(sid)
                            unpack_session(
                                result.data,
                                target_dir,
                                member_limit_bytes=max_member_size,
                            )

                            manifest_path = target_dir / "manifest.json"
                            if manifest_path.exists():
                                manifest = json.loads(manifest_path.read_text())
                                store.upsert_session_metadata(sid, manifest, str(target_dir))

                            _update_manifest_sync(target_dir, result.etag)
                            return True
                    except Exception as exc:
                        err_console.print(f"[red]Pull failed for {sid[:12]}: {exc}[/red]")
                return False

            pull_tasks = [
                _pull_one(sid) for sid in remote_by_id if sid not in local_by_id
            ]
            pull_results = await asyncio.gather(*pull_tasks)
            pulled = sum(1 for r in pull_results if r)
            pull_errors = sum(1 for r in pull_results if not r) - sum(1 for sid in remote_by_id if sid in local_by_id)
            pull_errors = max(0, pull_errors)  # Don't count skipped sessions as errors
            total_errors = push_errors + pull_errors

            return pushed, pulled, conflicts, total_errors, push_skipped
        finally:
            await client.close()

    try:
        console.print("Fetching remote sessions...")
        pushed, pulled, conflicts, errors, skipped = asyncio.run(_sync())
        color = "green" if errors == 0 else "yellow"
        summary = f"Sync complete: {pushed} pushed, {pulled} pulled, {conflicts} conflicts"
        if skipped > 0:
            summary += f", {skipped} skipped (deleted)"
        if errors > 0:
            summary += f", {errors} failed"
        console.print(f"[{color}]{summary}[/{color}]")
    finally:
        store.close()


handoffs_app = typer.Typer(name="handoffs", help="View handoff inbox and sent items.")


_HANDOFF_ID_RE = re.compile(r"^hnd_[a-f0-9]{8,}$")


def handoff(
    session_id: str = typer.Argument(help="Session ID or prefix."),
    # --to is intentionally NOT required at the Typer level. Making it
    # Option(...) (required) means Typer's argument parser rejects
    # `sfs handoff hnd_<id>` BEFORE this function runs — the exact user
    # mistake we want to catch with a friendly redirect. We validate
    # presence ourselves below, after the redirect check has had a
    # chance to fire.
    to: str = typer.Option(None, "--to", help="Recipient email."),
    to_user_id: str = typer.Option(
        None, "--to-user-id", help="Recipient user id (direct account match)."
    ),
    to_team_id: str = typer.Option(
        None, "--to-team-id", help="Recipient team id (Team+ tier)."
    ),
    message: str = typer.Option("", "--message", "-m", help="Message to include."),
    ticket_id: str = typer.Option(
        None, "--ticket", help="Carry an active ticket id through to the recipient."
    ),
    persona_name: str = typer.Option(
        None, "--persona", help="Carry the persona name through to the recipient."
    ),
    expires_hours: int = typer.Option(
        None,
        "--expires-hours",
        help="Expiry in hours (default 168=7d; tier-clamped to 30d / 90d).",
    ),
    attach: list[str] = typer.Option(
        None,
        "--attach",
        help=(
            "Attach a project ref. Repeatable. Format: 'kind:ref_id' "
            "(kind: kb_entry|wiki_page|ticket). Example: --attach kb_entry:42 "
            "--attach wiki_page:auth-flow."
        ),
    ),
) -> None:
    """Hand off a session to another user (push + email notification)."""
    # Common UX confusion: recipients with a handoff ID type
    # `sfs handoff hnd_...` expecting to claim it, hit a wall of
    # `--to` errors, and never find pull-handoff. Detect that exact
    # case and redirect.
    #
    # Gate on `not to`: when --to IS provided the user clearly intends a
    # send, so respect their input even if the positional looks like a
    # handoff ID (a session alias might legitimately be named
    # "hnd_deadbeef" — resolve_session_id below will resolve it). This
    # keeps the redirect tight to the bug pattern Pius hit.
    if not (to or to_user_id or to_team_id) and _HANDOFF_ID_RE.match(session_id):
        err_console.print(
            f"[yellow]'{session_id}' looks like a handoff ID, not a session ID.[/yellow]\n"
            f"\n"
            f"To CLAIM a handoff someone sent you, run:\n"
            f"    [cyan]sfs pull-handoff {session_id}[/cyan]\n"
            f"\n"
            f"[dim]`sfs handoff` is for SENDING — it pushes one of your sessions\n"
            f"and emails a recipient. Different verbs, easy to mix up.[/dim]"
        )
        raise SystemExit(2)

    # Exactly one recipient must be specified. Mirrors the server-side
    # @model_validator(exactly_one_recipient) so we surface a friendly
    # error before any push work happens.
    recipient_count = sum(1 for v in (to, to_user_id, to_team_id) if v)
    if recipient_count == 0:
        raise typer.BadParameter(
            "Specify exactly one recipient via --to, --to-user-id, or --to-team-id.",
            param_hint="'--to'",
        )
    if recipient_count > 1:
        raise typer.BadParameter(
            "Specify only one of --to, --to-user-id, --to-team-id.",
            param_hint="'--to'",
        )

    parsed_attachments: list[dict[str, str]] = []
    if attach:
        for raw in attach:
            if ":" not in raw:
                raise typer.BadParameter(
                    f"--attach {raw!r}: expected 'kind:ref_id' (e.g. 'kb_entry:42')",
                    param_hint="'--attach'",
                )
            kind, ref_id = raw.split(":", 1)
            kind = kind.strip()
            ref_id = ref_id.strip()
            if kind not in {"kb_entry", "wiki_page", "ticket"}:
                raise typer.BadParameter(
                    f"--attach kind {kind!r}: must be kb_entry|wiki_page|ticket",
                    param_hint="'--attach'",
                )
            if not ref_id:
                raise typer.BadParameter(
                    f"--attach {raw!r}: ref_id is empty", param_hint="'--attach'"
                )
            parsed_attachments.append({"kind": kind, "ref_id": ref_id})

    from sessionfs.sync.archive import pack_session
    from sessionfs.sync.client import SyncConflictError, SyncDeletedError, SyncTooLargeError

    store = open_store()
    client = _get_sync_client()

    try:
        full_id = resolve_session_id(store, session_id)
        session_dir = get_session_dir_or_exit(store, full_id)

        # Push to server if not already synced
        manifest = store.get_session_manifest(full_id)
        local_etag = (manifest or {}).get("sync", {}).get("etag")

        console.print(f"Ensuring session {full_id[:12]} is synced...")
        archive_data = pack_session(session_dir)

        # Resolve the tier-aware member-size cap from server settings.
        _sync_cfg = _load_sync_config()
        max_member_size = _resolve_max_member_size(
            _sync_cfg.get("api_url"), _sync_cfg.get("api_key")
        )

        # Pre-upload oversize check — same gate as `sfs push` so we catch
        # the per-file cap locally and give the user an actionable message
        # before wasting bandwidth on a guaranteed-413 upload.
        oversized = _find_oversized_member(archive_data, max_size=max_member_size)
        if oversized is not None:
            name, size = oversized
            err_console.print(
                f"[red]Session too large to hand off:[/red] '{name}' is "
                f"{size // (1024 * 1024)}MB (server hard cap "
                f"{max_member_size // (1024 * 1024)}MB per file).\n"
                f"Try /compact in your AI tool to start a fresh session, "
                f"then re-run sfs handoff."
            )
            raise SystemExit(1)

        async def _push_and_handoff():
            try:
                push_result = await client.push_session(full_id, archive_data, etag=local_etag)
                return push_result
            finally:
                await client.close()

        result = asyncio.run(_push_and_handoff())
        _update_manifest_sync(session_dir, result.etag)

        # Create handoff via API
        import httpx

        cfg = _load_sync_config()
        body: dict[str, Any] = {"session_id": full_id}
        if to:
            body["recipient_email"] = to
        if to_user_id:
            body["recipient_user_id"] = to_user_id
        if to_team_id:
            body["recipient_team_id"] = to_team_id
        if message:
            body["message"] = message
        if ticket_id:
            body["ticket_id"] = ticket_id
        if persona_name:
            body["persona_name"] = persona_name
        if expires_hours is not None:
            body["expires_in_hours"] = expires_hours
        if parsed_attachments:
            body["attachments"] = parsed_attachments

        resp = httpx.post(
            f"{cfg['api_url']}/api/v1/handoffs",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json=body,
            timeout=15.0,
        )

        if resp.status_code == 201:
            data = resp.json()
            handoff_id = data["id"]
            console.print(f"\n[green]Handoff created: {handoff_id}[/green]")
            recipient_display = (
                to
                or (f"user_id={to_user_id}" if to_user_id else None)
                or (f"team_id={to_team_id}" if to_team_id else "")
            )
            console.print(f"  Recipient: {recipient_display}")
            console.print(f"  Expires: {data['expires_at']}")
            if ticket_id or persona_name:
                console.print(
                    "  Provenance: "
                    + ", ".join(
                        x for x in (
                            f"ticket={ticket_id}" if ticket_id else None,
                            f"persona={persona_name}" if persona_name else None,
                        ) if x
                    )
                )
            if parsed_attachments:
                console.print(f"  Attachments: {len(parsed_attachments)}")
            console.print("\nRecipient can pull with:")
            console.print(f"  sfs pull-handoff {handoff_id}")
        else:
            err_console.print(f"[red]Handoff failed: {resp.text}[/red]")
            raise SystemExit(1)

    except SyncDeletedError:
        from sessionfs.sync.deleted_cleanup import cleanup_deleted_session
        from sessionfs.cli.common import get_store_dir as _gsd
        cleanup_deleted_session(full_id, session_dir, store, base_dir=_gsd())
        err_console.print(
            f"[yellow]Session {full_id[:12]} has been deleted on the server.[/yellow]\n"
            f"Local copy cleaned up. Cannot hand off a deleted session.\n"
            f"Restore with: sfs restore {full_id}"
        )
        raise SystemExit(1)
    except SyncTooLargeError as exc:
        # Server-side 413 (a member sneaked past the local check, or the
        # local check was disabled). Surface the server's friendly message.
        err_console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)
    except SyncConflictError:
        err_console.print("[red]Conflict pushing session. Pull latest first.[/red]")
        raise SystemExit(1)
    finally:
        store.close()


def pull_handoff(
    handoff_id: str = typer.Argument(help="Handoff ID (hnd_xxx)."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite local without prompting."),
    tool: str = typer.Option(
        "claude-code", "--in",
        help="Target tool for resume: claude-code, codex, copilot, gemini",
    ),
) -> None:
    """Pull a session from a handoff link."""
    import httpx
    from sessionfs.sync.archive import unpack_session

    store = open_store()
    cfg = _load_sync_config()
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}

    try:
        # Get handoff details
        console.print(f"Fetching handoff {handoff_id}...")
        resp = httpx.get(
            f"{cfg['api_url']}/api/v1/handoffs/{handoff_id}",
            headers=headers,
            timeout=15.0,
        )
        if resp.status_code == 410:
            err_console.print("[red]Handoff has expired.[/red]")
            raise SystemExit(1)
        if resp.status_code != 200:
            err_console.print(f"[red]Failed to get handoff: {resp.text}[/red]")
            raise SystemExit(1)

        handoff_data = resp.json()
        session_id = handoff_data["session_id"]

        console.print(f"  Session: {handoff_data.get('session_title') or session_id}")
        console.print(f"  From: {handoff_data['sender_email']}")
        if handoff_data.get("message"):
            console.print(f"  Message: {handoff_data['message']}")

        # Claim the handoff
        claim_resp = httpx.post(
            f"{cfg['api_url']}/api/v1/handoffs/{handoff_id}/claim",
            headers=headers,
            timeout=15.0,
        )
        if claim_resp.status_code == 409:
            console.print("[dim]Handoff already claimed — continuing with pull.[/dim]")
        elif claim_resp.status_code not in (200, 201):
            err_console.print(f"[yellow]Warning: could not claim handoff: {claim_resp.text}[/yellow]")

        # Use recipient's copied session ID (persisted on handoff record)
        recipient_sid = handoff_data.get("recipient_session_id")
        claim_data: dict | None = None
        if claim_resp.status_code in (200, 201):
            claim_data = claim_resp.json()
            recipient_sid = claim_data.get("recipient_session_id") or recipient_sid
        if recipient_sid:
            session_id = recipient_sid

        # R3 HIGH 2 — persist active_ticket_payload from the claim
        # response so the recipient's next captured session inherits the
        # ticket+persona context the sender attached. This is the core
        # v0.10.9 provenance carry-through promise; without it, all the
        # server-side validation/derivation work is wasted.
        if claim_data is not None:
            payload = claim_data.get("active_ticket_payload")
            if isinstance(payload, dict) and payload.get("project_id"):
                from sessionfs.active_ticket import write_bundle

                ok = write_bundle(
                    ticket_id=payload.get("ticket_id"),
                    persona_name=payload.get("persona_name"),
                    project_id=payload["project_id"],
                    lease_epoch=payload.get("lease_epoch"),
                )
                if ok:
                    parts = []
                    if payload.get("ticket_id"):
                        parts.append(f"ticket={payload['ticket_id']}")
                    if payload.get("persona_name"):
                        parts.append(f"persona={payload['persona_name']}")
                    console.print(
                        f"  Active context written: {', '.join(parts)} "
                        f"in project {payload['project_id'][:12]}"
                    )
                else:
                    err_console.print(
                        "[yellow]Warning: provenance bundle write failed — "
                        "next session will not be tagged with the handed-off "
                        "ticket/persona context.[/yellow]"
                    )

        # Pull the session
        client = _get_sync_client()

        async def _pull():
            try:
                return await client.pull_session(session_id)
            finally:
                await client.close()

        console.print(f"Pulling session {session_id[:12]}...")
        pull_result = asyncio.run(_pull())

        if pull_result.data is None:
            err_console.print("[red]No data received from server.[/red]")
            raise SystemExit(1)

        # Check if session exists locally
        local_dir = store.get_session_dir(session_id)
        if local_dir and not force:
            if not typer.confirm("Local session exists. Overwrite?", default=True):
                console.print("[dim]Pull cancelled.[/dim]")
                return

        # Extract to local store
        target_dir = store.allocate_session_dir(session_id)
        _sync_cfg = _load_sync_config()
        unpack_session(
            pull_result.data,
            target_dir,
            member_limit_bytes=_resolve_max_member_size(
                _sync_cfg.get("api_url"), _sync_cfg.get("api_key")
            ),
        )

        # Update index
        manifest_path = target_dir / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            store.upsert_session_metadata(session_id, manifest, str(target_dir))

        _update_manifest_sync(target_dir, pull_result.etag)

        # Try workspace resolution
        workspace_path = target_dir / "workspace.json"
        if workspace_path.exists():
            workspace = json.loads(workspace_path.read_text())
            git_info = workspace.get("git", {})
            git_remote = git_info.get("remote_url")
            if git_remote:
                from sessionfs.workspace.resolver import WorkspaceResolver
                resolver = WorkspaceResolver()
                resolved = resolver.resolve(git_remote, git_info.get("branch"))
                if resolved.path:
                    console.print(f"  Found local repo: {resolved.path}")
                    # Update workspace root_path to local clone
                    workspace["root_path"] = str(resolved.path)
                    workspace_path.write_text(json.dumps(workspace, indent=2))
                else:
                    console.print(
                        f"[dim]  Could not find local clone of {git_remote}[/dim]"
                    )

        console.print(
            f"\n[green]Pulled handoff session {session_id}[/green]\n"
            f"  Size: {len(pull_result.data):,} bytes"
        )
        console.print("\nResume with:")
        console.print(f"  sfs resume {session_id} --in {tool}")

    finally:
        store.close()


@handoffs_app.command("inbox")
def handoffs_inbox() -> None:
    """List handoffs sent to you."""
    import httpx

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    resp = httpx.get(
        f"{cfg['api_url']}/api/v1/handoffs/inbox/",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=15.0,
    )

    if resp.status_code != 200:
        err_console.print(f"[red]Failed to fetch inbox: {resp.text}[/red]")
        raise SystemExit(1)

    data = resp.json()
    handoffs = data.get("handoffs", [])

    if not handoffs:
        console.print("[dim]No incoming handoffs.[/dim]")
        return

    table = Table(title=f"Inbox ({data['total']} handoffs)")
    table.add_column("ID", style="cyan")
    table.add_column("From")
    table.add_column("Session")
    table.add_column("Tool", style="dim")
    table.add_column("Status")
    table.add_column("Created")

    for h in handoffs:
        status_style = "[green]" if h["status"] == "pending" else "[dim]"
        table.add_row(
            h["id"],
            h["sender_email"],
            (h.get("session_title") or "")[:30],
            h.get("session_tool") or "",
            f"{status_style}{h['status']}[/{status_style.strip('[')}",
            h["created_at"][:10],
        )

    console.print(table)


@handoffs_app.command("sent")
def handoffs_sent() -> None:
    """List handoffs you've sent."""
    import httpx

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    resp = httpx.get(
        f"{cfg['api_url']}/api/v1/handoffs/sent/",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=15.0,
    )

    if resp.status_code != 200:
        err_console.print(f"[red]Failed to fetch sent handoffs: {resp.text}[/red]")
        raise SystemExit(1)

    data = resp.json()
    handoffs = data.get("handoffs", [])

    if not handoffs:
        console.print("[dim]No sent handoffs.[/dim]")
        return

    table = Table(title=f"Sent ({data['total']} handoffs)")
    table.add_column("ID", style="cyan")
    table.add_column("To")
    table.add_column("Session")
    table.add_column("Status")
    table.add_column("Created")

    for h in handoffs:
        status_style = "[yellow]" if h["status"] == "pending" else "[green]"
        table.add_row(
            h["id"],
            h["recipient_email"],
            (h.get("session_title") or "")[:30],
            f"{status_style}{h['status']}[/{status_style.strip('[')}",
            h["created_at"][:10],
        )

    console.print(table)


@handoffs_app.command("get")
def handoffs_get(
    handoff_id: str = typer.Argument(help="Handoff id (hnd_...)."),
) -> None:
    """Show full handoff detail: events, comments, attachments, status."""
    import httpx

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    resp = httpx.get(
        f"{cfg['api_url']}/api/v1/handoffs/{handoff_id}",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        timeout=15.0,
    )
    if resp.status_code == 404:
        err_console.print(f"[red]Handoff {handoff_id} not found or not visible to you.[/red]")
        raise SystemExit(1)
    if resp.status_code != 200:
        err_console.print(f"[red]Failed: {resp.status_code} {resp.text}[/red]")
        raise SystemExit(1)
    data = resp.json()

    console.print(f"\n[bold cyan]{data['id']}[/bold cyan] [dim]({data['handoff_kind']})[/dim]")
    console.print(f"  Status: {data['status']}")
    console.print(f"  From: {data['sender_email']}")
    if data.get("recipient_email"):
        console.print(f"  To: {data['recipient_email']}")
    if data.get("recipient_user_id"):
        console.print(f"  To user: {data['recipient_user_id']}")
    if data.get("recipient_team_id"):
        console.print(f"  To team: {data['recipient_team_id']}")
    console.print(f"  Session: {data.get('session_title') or '(untitled)'} ({data['session_id'][:16]})")
    if data.get("ticket_id"):
        console.print(f"  Ticket: {data['ticket_id']}" + (f" ({data['snapshot_ticket_title']})" if data.get('snapshot_ticket_title') else ""))
    if data.get("persona_name"):
        console.print(f"  Persona: {data['persona_name']}")
    console.print(f"  Created: {data['created_at']}")
    console.print(f"  Expires: {data['expires_at']}")
    if data.get("viewed_at"):
        console.print(f"  Viewed: {data['viewed_at']}")
    if data.get("claimed_at"):
        console.print(f"  Claimed: {data['claimed_at']}")
    if data.get("revoked_at"):
        console.print(f"  Revoked: {data['revoked_at']}" + (f" — {data['revoke_reason']}" if data.get('revoke_reason') else ""))

    atts = data.get("attachments") or []
    if atts:
        console.print(f"\n[bold]Attachments ({len(atts)}):[/bold]")
        for a in atts:
            console.print(f"  - {a['kind']}: {a['ref_id']}")

    comments = data.get("comments") or []
    if comments:
        console.print(f"\n[bold]Comments ({len(comments)}):[/bold]")
        for c in comments:
            console.print(f"  [{c['created_at'][:19]}] {c.get('author_user_id') or 'unknown'}: {c['content']}")

    events = data.get("events") or []
    if events:
        console.print(f"\n[bold]Events ({len(events)}):[/bold]")
        for e in events:
            console.print(f"  [{e['created_at'][:19]}] {e['event_type']}")


@handoffs_app.command("revoke")
def handoffs_revoke(
    handoff_id: str = typer.Argument(help="Handoff id (hnd_...)."),
    reason: str = typer.Option(..., "--reason", "-r", help="Required reason (1-500 chars)."),
) -> None:
    """Sender revokes a pending handoff (recipient is notified)."""
    import httpx

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    resp = httpx.post(
        f"{cfg['api_url']}/api/v1/handoffs/{handoff_id}/revoke",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json={"reason": reason},
        timeout=15.0,
    )
    if resp.status_code == 200:
        console.print(f"[green]Handoff {handoff_id} revoked.[/green]")
    else:
        err_console.print(f"[red]Revoke failed: {resp.status_code} {resp.text}[/red]")
        raise SystemExit(1)


@handoffs_app.command("decline")
def handoffs_decline(
    handoff_id: str = typer.Argument(help="Handoff id (hnd_...)."),
    reason: str = typer.Option(None, "--reason", "-r", help="Optional reason (max 500 chars)."),
) -> None:
    """Recipient declines a pending handoff (sender is notified)."""
    import httpx

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    body: dict[str, Any] = {}
    if reason:
        body["reason"] = reason

    resp = httpx.post(
        f"{cfg['api_url']}/api/v1/handoffs/{handoff_id}/decline",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json=body,
        timeout=15.0,
    )
    if resp.status_code == 200:
        console.print(f"[green]Handoff {handoff_id} declined.[/green]")
    else:
        err_console.print(f"[red]Decline failed: {resp.status_code} {resp.text}[/red]")
        raise SystemExit(1)


@handoffs_app.command("comment")
def handoffs_comment(
    handoff_id: str = typer.Argument(help="Handoff id (hnd_...)."),
    message: str = typer.Option(..., "--message", "-m", help="Comment body (1-10000 chars)."),
) -> None:
    """Post a comment on a handoff thread (other party is notified)."""
    import httpx

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    resp = httpx.post(
        f"{cfg['api_url']}/api/v1/handoffs/{handoff_id}/comments",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json={"content": message},
        timeout=15.0,
    )
    if resp.status_code == 201:
        data = resp.json()
        console.print(f"[green]Comment {data['id']} posted on {handoff_id}.[/green]")
    else:
        err_console.print(f"[red]Comment failed: {resp.status_code} {resp.text}[/red]")
        raise SystemExit(1)


@handoffs_app.command("comments")
def handoffs_comments(
    handoff_id: str = typer.Argument(help="Handoff id (hnd_...)."),
    limit: int = typer.Option(200, "--limit", help="Max comments to fetch (1-200)."),
) -> None:
    """List all comments on a handoff, oldest first."""
    import httpx

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    resp = httpx.get(
        f"{cfg['api_url']}/api/v1/handoffs/{handoff_id}/comments",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        params={"limit": limit},
        timeout=15.0,
    )
    if resp.status_code != 200:
        err_console.print(f"[red]Failed: {resp.status_code} {resp.text}[/red]")
        raise SystemExit(1)
    rows = resp.json() or []
    if not rows:
        console.print("[dim]No comments yet.[/dim]")
        return
    for c in rows:
        console.print(f"[{c['created_at'][:19]}] {c.get('author_user_id') or 'unknown'}: {c['content']}")


@handoffs_app.command("events")
def handoffs_events(
    handoff_id: str = typer.Argument(help="Handoff id (hnd_...)."),
    limit: int = typer.Option(200, "--limit", help="Max events to fetch (1-200)."),
) -> None:
    """Show the handoff audit log (oldest first)."""
    import httpx

    cfg = _load_sync_config()
    if not cfg["api_key"]:
        err_console.print("[red]Not authenticated. Run 'sfs auth login' first.[/red]")
        raise SystemExit(1)

    resp = httpx.get(
        f"{cfg['api_url']}/api/v1/handoffs/{handoff_id}/events",
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        params={"limit": limit},
        timeout=15.0,
    )
    if resp.status_code != 200:
        err_console.print(f"[red]Failed: {resp.status_code} {resp.text}[/red]")
        raise SystemExit(1)
    rows = resp.json() or []
    if not rows:
        console.print("[dim]No events recorded.[/dim]")
        return
    for e in rows:
        line = f"[{e['created_at'][:19]}] {e['event_type']}"
        if e.get("actor_user_id"):
            line += f" by {e['actor_user_id'][:8]}"
        if e.get("payload"):
            line += f"  {e['payload']}"
        console.print(line)


def _update_manifest_sync(session_dir: Path, etag: str) -> None:
    """Update the sync block in a session's manifest.json."""
    from datetime import datetime, timezone

    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text())
    if "sync" not in manifest:
        manifest["sync"] = {}
    manifest["sync"]["etag"] = etag
    manifest["sync"]["last_sync_at"] = datetime.now(timezone.utc).isoformat()
    manifest["sync"]["dirty"] = False
    manifest_path.write_text(json.dumps(manifest, indent=2))
