"""Active-ticket provenance bundle — shared by MCP + CLI.

The bundle records which ticket the user is currently working on.
``start_ticket`` (MCP or CLI) writes it; ``complete_ticket`` removes it,
but only when both ``ticket_id`` and ``project_id`` match (KB 332 LOW fix
— never delete another tool's bundle).

Per-worker resolution (G1): the bundle is normally one per-user file at
``~/.sessionfs/active_ticket.json``, but that lets parallel workers in
different worktrees clobber each other and mis-tag sessions. So the read
path prefers a per-workspace bundle (``workspace_bundle_path``, kept under
``~/.sessionfs/workspaces/`` rather than inside the checkout) and then an
env-selected path, and the write path
targets the env-selected path whenever ``SFS_ACTIVE_TICKET_PATH`` or
``SFS_AGENT_RUN_ID`` is set.

Phase 6 (daemon) reads this bundle when a session is captured and tags
the manifest with ``persona_name`` + ``ticket_id``, but only when the
bundle's ``git_remote`` matches the session's workspace remote.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.active_ticket")

_HOME_BUNDLE = Path.home() / ".sessionfs" / "active_ticket.json"

# Bundle hardening (G1) — a run id is joined into the per-worker bundle path,
# so it must be a safe path segment (no traversal, no ".", no "..").
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _valid_run_id(run_id: str) -> bool:
    if run_id in {".", ".."}:
        return False
    return bool(_RUN_ID_RE.fullmatch(run_id))


def _env_bundle_path() -> Path | None:
    """Return the env-selected bundle path, or None when no env is set.

    ``SFS_ACTIVE_TICKET_PATH`` wins outright (an explicit path). Otherwise
    ``SFS_AGENT_RUN_ID`` scopes the bundle to this worker under
    ``~/.sessionfs/agents/<run_id>/active_ticket.json`` so parallel workers
    never clobber each other's ticket.

    An invalid ``SFS_AGENT_RUN_ID`` is ignored (None) rather than ever being
    joined into a path — a malicious or accidental value must not escape the
    agents directory.
    """
    explicit = os.environ.get("SFS_ACTIVE_TICKET_PATH")
    if explicit:
        return Path(explicit).expanduser()
    run_id = os.environ.get("SFS_AGENT_RUN_ID")
    if run_id:
        if not _valid_run_id(run_id):
            logger.warning(
                "Ignoring invalid SFS_AGENT_RUN_ID %r: must match "
                "^[A-Za-z0-9._-]{1,128}$ and not be '.' or '..'",
                run_id,
            )
            return None
        return Path.home() / ".sessionfs" / "agents" / run_id / "active_ticket.json"
    return None


def workspace_bundle_path(workspace: Path) -> Path:
    """Per-workspace mirror of a worker's bundle, kept OUT of the repo.

    ``~/.sessionfs/workspaces/<sha256 of the resolved workspace path>/
    active_ticket.json``. A file inside the checkout could be shipped by a
    cloned repository (tagging sessions with a ticket the user never chose)
    or committed by mistake; a path under the user's home can be neither.
    """
    try:
        resolved = str(workspace.resolve())
    except OSError:
        resolved = str(workspace.absolute())
    digest = hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()
    return Path.home() / ".sessionfs" / "workspaces" / digest / "active_ticket.json"


def bundle_path() -> Path:
    """Return the on-disk location of the active-ticket bundle.

    Env-selected when ``SFS_ACTIVE_TICKET_PATH`` or ``SFS_AGENT_RUN_ID`` is
    set, otherwise the per-user home file.
    """
    return _env_bundle_path() or _HOME_BUNDLE


def _read_path(path: Path) -> dict[str, Any] | None:
    """Return the parsed dict at ``path`` or None when missing/unreadable."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_bundle(workspace: Path | None = None) -> dict[str, Any] | None:
    """Return the parsed bundle or None if missing/unreadable.

    Resolution order: a workspace-local bundle (``workspace_bundle_path``) when ``workspace`` is given and that file exists,
    then the env-selected path, then the per-user home file. Each candidate
    is tried in turn so a worker-scoped bundle never leaks into a different
    workspace.
    """
    candidates: list[Path] = []
    if workspace is not None:
        candidates.append(workspace_bundle_path(workspace))
    candidates.append(bundle_path())
    for path in candidates:
        if not path.exists():
            continue
        data = _read_path(path)
        if data is not None:
            return data
    return None


def write_bundle(
    *,
    ticket_id: str | None,
    persona_name: str | None,
    project_id: str,
    lease_epoch: int | None = None,
    retrieval_audit_id: str | None = None,
    git_remote: str | None = None,
    workspace: Path | None = None,
) -> bool:
    """Write the active-ticket bundle. Returns True on success, False on
    OSError so callers can surface a "provenance NOT recorded" warning
    (KB 339 LOW). Failures are still logged.

    Either `ticket_id` or `persona_name` must be set (the bundle is
    meaningless if both are None). Persona-only entries (ticket_id=None)
    are written by `assume_persona` for ad-hoc agent work that isn't
    tied to a specific ticket — the daemon tags captured sessions with
    just the persona in that case.

    `git_remote` is the *normalized* remote (``normalize_git_remote``) of
    the workspace that started the ticket. The daemon compares it against
    a captured session's workspace remote so a worker's bundle never tags
    a session from a different project. Legacy bundles written before this
    field existed simply carry no remote and keep the old behaviour.

    `workspace` is the repo root (git toplevel) of the caller. When an
    env-selected path is in use (``SFS_ACTIVE_TICKET_PATH`` or
    ``SFS_AGENT_RUN_ID``) the bundle is ALSO mirrored to
    ``workspace_bundle_path(workspace)`` — the daemon has no
    worker env, so it reads that workspace-local file to tag captured
    sessions. Without env selection the single home file is sufficient and
    no workspace file is written.
    """
    if not ticket_id and not persona_name:
        raise ValueError("write_bundle requires ticket_id or persona_name")
    payload: dict[str, Any] = {
        "ticket_id": ticket_id,
        "persona_name": persona_name,
        "project_id": project_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    if lease_epoch is not None:
        payload["lease_epoch"] = lease_epoch
    if retrieval_audit_id:
        payload["retrieval_audit_id"] = retrieval_audit_id
    if git_remote:
        payload["git_remote"] = git_remote

    env_path = _env_bundle_path()
    paths = [bundle_path()]
    if env_path is not None and workspace is not None:
        paths.append(workspace_bundle_path(workspace))

    ok = True
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload))
        except OSError as exc:
            logger.warning("Failed to write active_ticket.json: %s", exc)
            ok = False
    return ok


def clear_bundle(workspace: Path | None = None) -> bool:
    """Unconditionally remove the bundle. Returns True if removed,
    False if it was already absent or removal failed.

    Used by `forget_persona` / `sfs persona forget` to retire an
    `assume_persona` bundle. Ticket bundles should use
    `clear_bundle_if_owned()` instead so concurrent ticket work isn't
    disturbed (KB 332 LOW).

    ``workspace`` mirrors ``write_bundle``: when an env-selected path is in
    use the workspace-local mirror is removed too.
    """
    env_path = _env_bundle_path()
    paths = [bundle_path()]
    if env_path is not None and workspace is not None:
        paths.append(workspace_bundle_path(workspace))

    removed = False
    for path in paths:
        if not path.exists():
            continue
        try:
            path.unlink()
            removed = True
        except OSError as exc:
            logger.warning("Failed to remove active_ticket.json: %s", exc)
    return removed


def clear_bundle_if_owned(
    *, ticket_id: str, project_id: str, workspace: Path | None = None
) -> bool:
    """Remove the bundle only when it points at this exact ticket.

    Returns True if the bundle was removed, False if it was preserved
    (either missing, unreadable, or owned by a different ticket). When an
    env-selected path is in use the workspace-local mirror is checked and
    cleared too.
    """
    env_path = _env_bundle_path()
    paths = [bundle_path()]
    if env_path is not None and workspace is not None:
        paths.append(workspace_bundle_path(workspace))

    removed = False
    for path in paths:
        if not path.exists():
            continue
        bundle = _read_path(path)
        if (
            isinstance(bundle, dict)
            and bundle.get("ticket_id") == ticket_id
            and bundle.get("project_id") == project_id
        ):
            try:
                path.unlink()
                removed = True
            except OSError as exc:
                logger.warning("Failed to remove active_ticket.json: %s", exc)
    return removed
