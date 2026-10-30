"""v0.10.1 Phase 6 — annotate a captured .sfs session manifest with
the active ticket + persona from ``~/.sessionfs/active_ticket.json``.

Mirrors the shape of ``watchers/provenance.py``. Best-effort: failures
log a warning and leave the manifest untouched.

G1 per-worker resolution: the bundle is read from the session's workspace
first, then the env-selected path, then the home file. A bundle tagged
with a ``git_remote`` only annotates sessions captured in that same
workspace (git remote → project), so a worker in one worktree never tags
another project's sessions.

The manifest gains two top-level fields when a bundle exists:
- ``persona_name`` — the persona the user is working under
- ``ticket_id`` — the ticket the user has started
- ``retrieval_audit_id`` — server-side retrieval audit context id

These match the column names already in the ``sessions`` table
(migration 037), so the cloud sync path will populate the SQL columns
directly without further mapping.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("sfsd.active_ticket")


def _workspace_info(session_dir: Path) -> tuple[str | None, str | None]:
    """Return ``(root_path, git_remote)`` from ``workspace.json``.

    Both are None when the file is missing, malformed, or lacks the field.
    """
    try:
        raw = (session_dir / "workspace.json").read_text(encoding="utf-8")
        value = json.loads(raw)
        if not isinstance(value, dict):
            return None, None
        root = value.get("root_path")
        root = root if isinstance(root, str) and root else None
        git = value.get("git")
        remote = None
        if isinstance(git, dict):
            candidate = git.get("remote_url")
            remote = candidate if isinstance(candidate, str) and candidate else None
        return root, remote
    except (OSError, ValueError):
        return None, None


def annotate_manifest_with_active_ticket(session_dir: Path) -> None:
    """Read the active-ticket bundle and tag the session manifest.

    No-op when the bundle is missing or malformed — the user simply
    isn't working under a ticket. Also no-op when the bundle's
    ``git_remote`` doesn't match the session's workspace remote, so a
    worker's ticket never leaks onto another project's sessions.
    """
    try:
        from sessionfs.active_ticket import read_bundle
        from sessionfs.git_remote import normalize_git_remote

        root, session_remote = _workspace_info(session_dir)
        workspace = Path(root) if root else None
        bundle = read_bundle(workspace=workspace)
        if not isinstance(bundle, dict):
            return
        ticket_id = bundle.get("ticket_id")
        persona_name = bundle.get("persona_name")
        retrieval_audit_id = bundle.get("retrieval_audit_id")
        # v0.10.1 Phase 8 — persona-only bundles (assume_persona) carry
        # ticket_id=None. Annotate when EITHER field is present so ad-hoc
        # agent work still tags captured sessions with the persona.
        if not ticket_id and not persona_name:
            return

        # G1 — only tag when the bundle belongs to this workspace. Legacy
        # bundles (no git_remote) keep the old behaviour. A bundle carrying
        # a git_remote tags only sessions whose workspace remote matches it;
        # a session with no remote can't be proven to belong to the bundle,
        # so it is left untagged.
        bundle_remote = bundle.get("git_remote")
        if isinstance(bundle_remote, str) and bundle_remote:
            if not session_remote or normalize_git_remote(session_remote) != bundle_remote:
                logger.info(
                    "Skipping active-ticket annotation for %s: bundle remote "
                    "%s does not match workspace remote %s",
                    session_dir, bundle_remote, session_remote,
                )
                return

        manifest_path = session_dir / "manifest.json"
        if not manifest_path.exists():
            return
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(manifest, dict):
            return

        if ticket_id:
            manifest["ticket_id"] = ticket_id
        if persona_name:
            manifest["persona_name"] = persona_name
        if isinstance(retrieval_audit_id, str) and retrieval_audit_id.strip():
            manifest["retrieval_audit_id"] = retrieval_audit_id.strip()[:64]

        manifest_path.write_text(json.dumps(manifest, indent=2))
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning(
            "Active-ticket annotation failed for %s: %s", session_dir, exc
        )
