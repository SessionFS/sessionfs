"""Health check command: sfs doctor."""

from __future__ import annotations

import os
import sqlite3
import stat

from rich.table import Table

from sessionfs.cli.common import console, err_console, get_store_dir, handle_errors


def _check_mark(ok: bool) -> str:
    return "[green]\u2713[/green]" if ok else "[red]\u2717[/red]"


@handle_errors
def doctor() -> None:
    """Run health checks and auto-repair."""
    store_dir = get_store_dir()

    table = Table(title="SessionFS Health Check", show_lines=False)
    table.add_column("Check", min_width=30)
    table.add_column("Status", min_width=6)
    table.add_column("Detail")

    repaired = False

    # 1. Store directory exists and writable
    store_ok = store_dir.is_dir() and os.access(store_dir, os.W_OK)
    table.add_row(
        "Store directory",
        _check_mark(store_ok),
        str(store_dir) if store_ok else f"{store_dir} (missing or not writable)",
    )

    # 2. Sessions directory exists, count .sfs dirs
    sessions_dir = store_dir / "sessions"
    sessions_ok = sessions_dir.is_dir()
    disk_count = 0
    if sessions_ok:
        disk_count = sum(
            1
            for p in sessions_dir.iterdir()
            if p.is_dir() and p.name.endswith(".sfs")
        )
    table.add_row(
        "Sessions directory",
        _check_mark(sessions_ok),
        f"{disk_count} session(s) on disk" if sessions_ok else "missing",
    )

    # 3. Index opens, count matches disk
    index_path = store_dir / "index.db"
    index_ok = False
    index_count = 0
    index_detail = "missing"
    if index_path.exists():
        try:
            conn = sqlite3.connect(str(index_path))
            conn.execute("PRAGMA integrity_check")
            row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
            index_count = row[0] if row else 0
            conn.close()
            index_ok = True
            match_note = ""
            if index_count != disk_count:
                match_note = f" [yellow](disk has {disk_count})[/yellow]"
            index_detail = f"{index_count} indexed{match_note}"
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
            index_detail = f"corrupted: {exc}"
            # Auto-repair
            err_console.print(
                "[yellow]Index corrupted. Attempting auto-repair...[/yellow]"
            )
            try:
                from sessionfs.cli.common import open_store

                store = open_store(initialize=True)
                store.close()
                repaired = True
                index_ok = True
                index_detail = "repaired (reindexed from disk)"
            except Exception as repair_exc:
                index_detail = f"repair failed: {repair_exc}"
    table.add_row("Session index", _check_mark(index_ok), index_detail)

    # 4. Index freshness (disk vs index count)
    freshness_ok = index_ok and index_count == disk_count
    if index_ok:
        table.add_row(
            "Index freshness",
            _check_mark(freshness_ok),
            "in sync" if freshness_ok else f"stale ({disk_count} on disk, {index_count} indexed)",
        )

    # 5. Daemon running (check PID file)
    pid_path = store_dir / "sfsd.pid"
    daemon_ok = False
    daemon_detail = "not running"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            daemon_ok = True
            daemon_detail = f"running (PID {pid})"
        except (ValueError, ProcessLookupError, PermissionError):
            daemon_detail = "stale PID file (not running)"
    table.add_row("Daemon", _check_mark(daemon_ok), daemon_detail)

    # 6. API reachable (GET /health with timeout)
    api_ok = False
    api_detail = "not configured"
    try:
        from sessionfs.daemon.config import load_config

        config = load_config()
        if config.sync.enabled and config.sync.api_url:
            try:
                import httpx

                resp = httpx.get(
                    f"{config.sync.api_url}/health",
                    timeout=5.0,
                )
                api_ok = resp.status_code == 200
                api_detail = (
                    f"reachable ({config.sync.api_url})"
                    if api_ok
                    else f"HTTP {resp.status_code}"
                )
            except Exception as exc:
                api_detail = f"unreachable: {exc}"
        else:
            api_detail = "sync not enabled"
    except Exception:
        pass
    table.add_row("API server", _check_mark(api_ok), api_detail)

    # 7. Auth valid (GET /api/v1/auth/me)
    auth_ok = False
    auth_detail = "not authenticated"
    try:
        from sessionfs.daemon.config import load_config

        config = load_config()
        if config.sync.api_key:
            try:
                import httpx

                resp = httpx.get(
                    f"{config.sync.api_url}/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {config.sync.api_key}"},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    me = resp.json()
                    auth_ok = True
                    auth_detail = f"{me.get('email', '?')} ({me.get('tier', 'free')})"
                else:
                    auth_detail = f"invalid (HTTP {resp.status_code})"
            except Exception as exc:
                auth_detail = f"check failed: {exc}"
    except Exception:
        pass
    table.add_row("Authentication", _check_mark(auth_ok), auth_detail)

    # 8. Config permissions
    config_path = store_dir / "config.toml"
    config_ok = False
    config_detail = "not found"
    if config_path.exists():
        mode = config_path.stat().st_mode
        world_readable = bool(
            mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH)
        )
        config_ok = not world_readable
        if config_ok:
            config_detail = f"permissions OK ({oct(mode & 0o777)})"
        else:
            config_detail = f"too permissive ({oct(mode & 0o777)}) — should be 0o600"
    table.add_row("Config permissions", _check_mark(config_ok), config_detail)

    # 9. Stale-install detection — pip upgraded but PATH still resolves to
    # an older `sfs`. Real bug: users pip-install to user-site, see the
    # "scripts installed in X which is not on PATH" warning fly past, then
    # their `sfs` keeps running an older binary and commands added in a
    # newer version error out as "No such command". Compare the version
    # the import system sees vs the version the running `sfs` came from.
    install_ok, install_detail = _check_install_consistency()
    table.add_row("Install consistency", _check_mark(install_ok), install_detail)

    console.print()
    console.print(table)
    console.print()

    if repaired:
        console.print("[green]Auto-repair completed successfully.[/green]")


def _vtuple(v: str) -> tuple:
    """Best-effort version tuple — stops at the first non-numeric segment."""
    out: list[int] = []
    for part in v.split("."):
        try:
            out.append(int(part))
        except ValueError:
            return tuple(out)
    return tuple(out)


def _resolve_sfs_binary_python() -> str | None:
    """Return the interpreter path baked into the `sfs` binary's shebang,
    or None if it can't be parsed. The interpreter that ships pip-installed
    console scripts is encoded in the script's first line — that's the
    Python whose `sessionfs` import the running `sfs` actually uses,
    even if it differs from sys.executable in this process.
    """
    import shutil
    from pathlib import Path

    sfs_path = shutil.which("sfs")
    if not sfs_path:
        return None
    try:
        head = Path(sfs_path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if not head:
        return None
    first = head[0].strip()
    if first.startswith("#!"):
        # Standard shebang. May be `#!/path/to/python` or
        # `#!/usr/bin/env python3.X`. Take the first whitespace token
        # after the `#!`. For env-shebangs we get "env" — fall through
        # to the simpler path below.
        body = first[2:].strip()
        first_tok = body.split(None, 1)[0] if body else ""
        if first_tok.endswith("env") and " " in body:
            # `#!/usr/bin/env python3.11` → take what follows.
            return body.split(None, 1)[1].split()[0]
        if first_tok:
            return first_tok
    return None


def _peer_sessionfs_version(python_path: str) -> str | None:
    """Subprocess the given interpreter to ask what sessionfs version
    it sees. Returns None on any failure — drift detection is best
    effort, never fatal."""
    import subprocess

    try:
        result = subprocess.run(
            [python_path, "-c",
             "import sessionfs; print(sessionfs.__version__)"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    return out or None


def _check_install_consistency() -> tuple[bool, str]:
    """Detect the "pip upgraded but PATH points at an older sfs" case.
    Returns (ok, human_detail).

    The Pius bug shape: pip-installed sessionfs 0.9.9.7 into
    `~/Library/Python/3.14/bin` (Python 3.14 user-site). PATH still
    resolves `sfs` to a binary tied to a DIFFERENT Python (e.g. system
    3.12). The user sees errors like "No such command 'pull-handoff'"
    even though pip says the install succeeded.

    Strategy:
      1. Read the running module's version (the one in THIS interpreter).
      2. Read the shebang of the `sfs` binary on PATH to find the
         interpreter it uses.
      3. Subprocess that interpreter to read what sessionfs version
         IT sees. If it differs from this process's runtime version,
         we have cross-Python drift — the real bug.
      4. Also enumerate current-interpreter dist-info as a secondary
         check (the in-process drift case from R3 of v0.9.9.7).
    """
    import shutil
    import sys
    from importlib.metadata import distributions

    try:
        import sessionfs

        runtime_version = getattr(sessionfs, "__version__", "unknown")
    except Exception as exc:  # pragma: no cover — defensive
        return False, f"could not import sessionfs: {exc}"

    sfs_on_path = shutil.which("sfs") or "(not on PATH)"
    detail_parts = [f"running {runtime_version}", f"sfs on PATH: {sfs_on_path}"]

    # --- Primary check: cross-Python drift via shebang ---
    drift_detected = False
    binary_python = _resolve_sfs_binary_python()
    if binary_python and binary_python != sys.executable:
        peer_version = _peer_sessionfs_version(binary_python)
        if peer_version is None:
            detail_parts.append(
                f"[yellow]could not query sessionfs from sfs's Python ({binary_python}); "
                f"may not be installed there[/yellow]"
            )
            drift_detected = True
        elif _vtuple(peer_version) != _vtuple(runtime_version):
            detail_parts.append(
                f"[yellow]drift: this Python sees {runtime_version}, "
                f"but the `sfs` on PATH uses {binary_python} which sees {peer_version}[/yellow]"
            )
            drift_detected = True

    # --- Secondary check: same-interpreter dist-info drift ---
    seen: list[tuple[str, str]] = []
    try:
        for dist in distributions():
            name = (dist.metadata.get("Name") or "").lower()
            if name == "sessionfs":
                seen.append((dist.version, str(getattr(dist, "_path", "?"))))
    except Exception:
        pass
    higher = [(v, p) for v, p in seen if _vtuple(v) > _vtuple(runtime_version)]
    if higher:
        higher.sort(key=lambda x: _vtuple(x[0]), reverse=True)
        ver, path = higher[0]
        detail_parts.append(
            f"[yellow]drift (same interpreter): installed {ver} at {path} "
            f"but importlib sees {runtime_version}[/yellow]"
        )
        drift_detected = True

    if drift_detected:
        detail_parts.append(
            "[dim]Fix: add the newer install's bin dir to PATH, or run "
            "`python -m sessionfs.cli.main` to invoke this interpreter's sfs directly.[/dim]"
        )
        return False, " · ".join(detail_parts)

    return True, " · ".join(detail_parts)
