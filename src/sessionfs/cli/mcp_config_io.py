"""Safe edits of JSON MCP config files (``sfs mcp install`` / ``uninstall``).

Every edit is planned first and applied second:

* **Preflight** — each target is read, parsed and checked (regular file,
  writable, its directory writable; a symlinked config is followed only to a
  regular file owned by the current user, which is then edited in place of
  the link so dotfile setups keep working). If any target fails preflight,
  nothing is written.
* **Write** — each target is then written on its own: it must still be the
  version that was planned from (else it is re-read and re-planned), the
  previous version is kept as ``<file>.sessionfs-backup-<UTC time>`` (the
  three newest are kept), and the new content is written to a temp file in
  the same directory with the original permission bits, fsynced and renamed
  over the file. The config path always holds a complete file and no user
  file is ever deleted. There is no cross-file rollback: each target's
  result is reported, so a partial success is visible.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_ATTEMPTS = 3


class ConfigFileError(Exception):
    """A user-facing reason a config file cannot be changed safely."""


Snapshot = tuple[int, int, int, str]


@dataclass
class ConfigPlan:
    path: Path  # the path SessionFS manages (may be a symlink)
    target: Path  # the regular file that is actually replaced
    display: str
    original: bytes | None
    snapshot: Snapshot | None
    mode: int | None
    new_data: dict[str, Any] | None  # None = no write needed
    action: str  # added | updated | unchanged | removed | absent | missing


def _snapshot(target: Path) -> Snapshot | None:
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return None
    with open(target, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return (st.st_ino, st.st_mtime_ns, st.st_size, digest)


def _nearest_existing_dir(path: Path) -> Path:
    current = path
    while not current.exists():
        if current == current.parent:
            break
        current = current.parent
    return current


def _resolve_target(path: Path) -> Path:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return path
    if stat.S_ISLNK(st.st_mode):
        real = Path(os.path.realpath(path))
        try:
            rst = os.stat(real)
        except FileNotFoundError:
            raise ConfigFileError(f"{path} is a symlink to a missing file — fix it manually.")
        getuid = getattr(os, "getuid", None)
        if not stat.S_ISREG(rst.st_mode) or (getuid is not None and rst.st_uid != getuid()):
            raise ConfigFileError(
                f"{path} is a symlink to something other than a file you own — "
                "SessionFS will not edit it; add the server manually."
            )
        return real
    if not stat.S_ISREG(st.st_mode):
        raise ConfigFileError(f"{path} is not a regular file — fix it manually.")
    return path


def _load(path: Path, allow_empty: bool) -> tuple[Path, bytes | None, Snapshot | None, int | None, dict | None]:
    target = _resolve_target(path)
    parent = target.parent if target.parent.exists() else _nearest_existing_dir(target.parent)
    if not os.access(parent, os.W_OK | os.X_OK):
        raise ConfigFileError(f"Cannot write to {parent} — check its permissions.")
    if not target.exists():
        return target, None, None, None, None
    if not os.access(target, os.R_OK | os.W_OK):
        raise ConfigFileError(f"Cannot read and write {target} — check its permissions.")
    snapshot = _snapshot(target)
    original = target.read_bytes()
    mode = stat.S_IMODE(os.lstat(target).st_mode)
    try:
        text = original.decode("utf-8")
        data = {} if allow_empty and not text.strip() else json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigFileError(f"Cannot parse {path} — fix the file manually before changing it.")
    if not isinstance(data, dict):
        raise ConfigFileError(f"{path} is not a JSON object — fix it manually before changing it.")
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ConfigFileError(f"{path}: mcpServers is not a JSON object — fix it manually.")
    return target, original, snapshot, mode, data


def plan_install(
    path: Path, name: str, entry: dict[str, Any], display: str, *, allow_empty: bool = False,
) -> ConfigPlan:
    target, original, snapshot, mode, data = _load(path, allow_empty)
    data = dict(data or {})
    servers = dict(data.get("mcpServers") or {})
    existing = servers.get(name)
    if existing == entry:
        return ConfigPlan(path, target, display, original, snapshot, mode, None, "unchanged")
    servers[name] = entry
    data["mcpServers"] = servers
    action = "updated" if existing else "added"
    return ConfigPlan(path, target, display, original, snapshot, mode, data, action)


def plan_uninstall(
    path: Path, name: str, display: str, *, allow_empty: bool = False,
) -> ConfigPlan:
    target, original, snapshot, mode, data = _load(path, allow_empty)
    if original is None:
        return ConfigPlan(path, target, display, None, None, None, None, "missing")
    servers = (data or {}).get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return ConfigPlan(path, target, display, original, snapshot, mode, None, "absent")
    new = dict(data or {})
    new_servers = dict(servers)
    del new_servers[name]
    new["mcpServers"] = new_servers
    return ConfigPlan(path, target, display, original, snapshot, mode, new, "removed")


def _default_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


BACKUP_MARK = ".sessionfs-backup-"
KEEP_BACKUPS = 3  # per config file; older SessionFS backups are pruned


@dataclass
class ApplyResult:
    plan: ConfigPlan
    ok: bool
    backup: Path | None = None
    error: str | None = None


def _write_temp(target: Path, payload: bytes, mode: int | None) -> str:
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fchmod(f.fileno(), mode if mode is not None else _default_mode())
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return tmp


def _backup(plan: ConfigPlan, content: bytes) -> Path:
    """Keep ``content`` — the bytes read from the file right before it is
    replaced — next to it under a timestamped name (created exclusively,
    never overwriting anything), and prune older SessionFS backups of the
    same file beyond KEEP_BACKUPS."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = plan.target.parent / f"{plan.target.name}{BACKUP_MARK}{stamp}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(backup), flags, plan.mode if plan.mode is not None else 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    older = sorted(plan.target.parent.glob(f"{plan.target.name}{BACKUP_MARK}*"))
    for stale in older[:-KEEP_BACKUPS]:
        if stale.is_file() and not stale.is_symlink():
            with contextlib.suppress(OSError):
                stale.unlink()
    return backup


def apply_plan(plan: ConfigPlan, replan: Callable[[ConfigPlan], ConfigPlan]) -> ApplyResult:
    """Write one planned config change.

    The file must still be the version that was planned from (else it is
    re-read and re-planned, a few times); the previous version is kept as a
    timestamped backup; the new content is written to a temp file in the
    same directory, fsynced, and renamed over the file — the path always
    holds a complete config. A new file is linked into place, so it never
    replaces one another program created meanwhile. No user file is ever
    deleted.
    """
    current = plan
    for _attempt in range(MAX_ATTEMPTS):
        if current.new_data is None:
            return ApplyResult(current, True)
        if _snapshot(current.target) != current.snapshot:
            current = replan(current)
            continue
        payload = json.dumps(current.new_data, indent=2).encode("utf-8")
        current.target.parent.mkdir(parents=True, exist_ok=True)
        tmp = _write_temp(current.target, payload, current.mode)
        try:
            if current.snapshot is None:
                try:
                    os.link(tmp, current.target)
                except FileExistsError:
                    current = replan(current)
                    continue
                return ApplyResult(current, True)
            # Back up what is actually there now, not what was planned from.
            backup = _backup(current, current.target.read_bytes())
            os.replace(tmp, current.target)
            return ApplyResult(current, True, backup=backup)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
    return ApplyResult(
        current, False,
        error=f"{plan.path} kept changing while SessionFS was updating it; it was left as is.",
    )


def apply_plans(plans: list[ConfigPlan], replan: Callable[[ConfigPlan], ConfigPlan]) -> list[ApplyResult]:
    """Apply each plan independently and report every target's result.

    There is no cross-file rollback: a target that fails is left exactly as
    it was, the others keep their change, and the caller reports both.
    """
    results: list[ApplyResult] = []
    for plan in plans:
        try:
            results.append(apply_plan(plan, replan))
        except (OSError, ConfigFileError) as exc:
            results.append(ApplyResult(plan, False, error=f"{plan.path}: {exc}"))
    return results