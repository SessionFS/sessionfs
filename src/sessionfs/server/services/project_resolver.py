"""Project resolver helpers for multi-repo projects (§3.3).

After v0.11, a project can own N repos via the project_repos join table.
This module provides the dual-read resolution layer: project_repos first,
then legacy projects.git_remote_normalized fallback. Resolvers are
tombstone-aware (follow merged_into_project_id chains) and distinguish
the repo_reclaimed orphaned state (resolves to itself, not redirected).

IMPORTANT (Sentinel F5): These functions RESOLVE; they NEVER authorize.
Every caller MUST run its own access check against the RETURNED project
(which may be a redirect target), not the input remote. Resolution is
not authorization — a redirect through a tombstone must never grant
access the caller would not have had on the target directly.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Project, ProjectRepo


# Sentinel F5 defense-in-depth: bound unbounded tombstone chains. Data
# corruption or a bug could introduce a cycle; the hop cap prevents a
# resolver from looping forever or silently returning a corrupt-chain
# project. Preconditions (merge rejects already-merged projects) prevent
# A→B→A, but this belt-and-suspenders guard catches anything they miss.
_HOP_CAP = 8


class ProjectResolutionLoopError(Exception):
    """Raised when a tombstone chain exceeds the hop cap.

    Indicates possible data corruption (e.g. a cycle in
    merged_into_project_id references). Neither resolver path ever
    returns a normal Project on this condition — callers cannot
    unknowingly operate on a corrupt-chain project.

    Routes should map this to 409 Conflict with a resolution_loop
    error code, falling back to 500 if uncaught.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def resolve_project_by_remote(
    db: AsyncSession,
    git_remote_normalized: str,
    *,
    for_update: bool = False,
    follow_tombstone: bool = True,
) -> Project | None:
    """Resolve a project from a git remote via the project_repos join table.

    Dual-read: project_repos first (source of truth), then fallback to
    legacy projects.git_remote_normalized for backward compatibility.
    Tombstone-aware: if the resolved project has merged_into_project_id
    and follow_tombstone is True, follows the chain transparently.

    NOTE: This function RESOLVES; it NEVER authorizes. Every caller
    MUST run its own access check against the RETURNED project (which
    may be a redirect target), not the input remote.

    Hop-cap: the tombstone chain is bounded at 8 hops (defense-in-depth;
    preconditions prevent cycles, but data corruption could introduce
    one). Exceeding the cap raises ProjectResolutionLoopError — never
    a silent normal Project return.

    If follow_tombstone is False (used by the merge endpoint itself),
    returns the tombstone project directly without following the chain.

    Returns None when no project owns this remote.
    """
    if not git_remote_normalized:
        return None

    # Primary path: project_repos join table (source of truth)
    stmt = (
        select(Project)
        .join(ProjectRepo, ProjectRepo.project_id == Project.id)
        .where(ProjectRepo.git_remote_normalized == git_remote_normalized)
    )
    if for_update:
        stmt = stmt.with_for_update(of=Project)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()

    # Fallback: legacy projects.git_remote_normalized column
    if project is None:
        stmt = select(Project).where(
            Project.git_remote_normalized == git_remote_normalized
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()

    # Tombstone redirect: follow merged_into_project_id chain.
    # repo_reclaimed projects (repo_reclaimed_at IS NOT NULL,
    # merged_into_project_id IS NULL) resolve to themselves — they
    # are NOT redirected (§3.4).
    if project is not None and follow_tombstone and project.merged_into_project_id:
        hops = 0
        while project is not None and project.merged_into_project_id:
            hops += 1
            if hops > _HOP_CAP:
                raise ProjectResolutionLoopError(
                    f"resolve_project_by_remote: tombstone hop cap "
                    f"({_HOP_CAP}) exceeded for remote "
                    f"{git_remote_normalized} — possible data corruption"
                )
            project = await db.get(Project, project.merged_into_project_id)

    return project


async def resolve_project_by_id(
    db: AsyncSession,
    project_id: str,
    *,
    follow_tombstone: bool = True,
) -> Project | None:
    """Get project by ID, optionally following tombstone chain.

    Same hop-cap as resolve_project_by_remote (≤8). Raises
    ProjectResolutionLoopError on exceedance — never a silent return.

    NOTE: Resolves only; never authorizes. Callers must run their own
    access check against the returned project.
    """
    project = await db.get(Project, project_id)
    if project is not None and follow_tombstone and project.merged_into_project_id:
        hops = 0
        while project is not None and project.merged_into_project_id:
            hops += 1
            if hops > _HOP_CAP:
                raise ProjectResolutionLoopError(
                    f"resolve_project_by_id: tombstone hop cap "
                    f"({_HOP_CAP}) exceeded for project {project_id} — "
                    f"possible data corruption"
                )
            project = await db.get(Project, project.merged_into_project_id)
    return project


async def get_primary_remote(
    db: AsyncSession,
    project_id: str,
) -> str | None:
    """Return the primary git_remote_normalized for a project.

    The primary remote is the project_repos row with is_primary=true.
    This is the display remote — what shows up in project lists,
    transfer snapshots, and org member views (Group E sites, §3.3).
    """
    result = await db.execute(
        select(ProjectRepo.git_remote_normalized)
        .where(
            ProjectRepo.project_id == project_id,
            ProjectRepo.is_primary == True,  # noqa: E712
        )
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row if row else None
