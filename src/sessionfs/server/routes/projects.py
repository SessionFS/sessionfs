"""Project context CRUD routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Project, Session, User
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

DEFAULT_TEMPLATE = """\
# Project Context

## Overview
<!-- What is this project? One paragraph. -->

## Architecture
<!-- Tech stack, infrastructure, key services. -->

## Conventions
<!-- Coding standards, branch strategy, PR process. -->

## API Contracts
<!-- Key endpoints, request/response formats. -->

## Key Decisions
<!-- Important decisions that are locked and shouldn't be revisited. -->

## Team
<!-- Who works on what. -->
"""


class CreateProjectRequest(BaseModel):
    name: str
    git_remote_normalized: str


class UpdateContextRequest(BaseModel):
    context_document: str


class ProjectResponse(BaseModel):
    id: str
    name: str
    git_remote_normalized: str
    context_document: str
    owner_id: str
    created_at: datetime
    updated_at: datetime


async def _check_repo_access(db: AsyncSession, user_id: str, git_remote: str) -> bool:
    """Check if user has sessions in this repo (grants read/write access)."""
    stmt = (
        select(Session.id)
        .where(Session.user_id == user_id, Session.git_remote_normalized == git_remote)
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


@router.get("/", response_model=list[ProjectResponse])
async def list_projects(
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectResponse]:
    """List all projects the user has access to (owner or has sessions in repo)."""
    from sqlalchemy import distinct, or_

    # Get git remotes from user's sessions
    session_remotes_stmt = select(distinct(Session.git_remote_normalized)).where(
        Session.user_id == user.id,
        Session.git_remote_normalized.isnot(None),
        Session.git_remote_normalized != "",
    )
    result = await db.execute(session_remotes_stmt)
    user_remotes = {r[0] for r in result.all()}

    # Get projects: owned by user OR matching user's session remotes
    stmt = select(Project).where(
        or_(
            Project.owner_id == user.id,
            Project.git_remote_normalized.in_(user_remotes) if user_remotes else False,
        )
    ).order_by(Project.updated_at.desc())
    result = await db.execute(stmt)
    projects = result.scalars().all()

    return [
        ProjectResponse(
            id=p.id,
            name=p.name,
            git_remote_normalized=p.git_remote_normalized,
            context_document=p.context_document,
            owner_id=p.owner_id,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in projects
    ]


@router.post("/", response_model=ProjectResponse, status_code=201)
async def create_project(
    body: CreateProjectRequest,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Create a project context for a repository."""
    check_feature(ctx, "project_context")
    # Check for existing project
    stmt = select(Project).where(Project.git_remote_normalized == body.git_remote_normalized)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Project already exists for this repository")

    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=body.name,
        git_remote_normalized=body.git_remote_normalized,
        context_document=DEFAULT_TEMPLATE,
        owner_id=user.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    return ProjectResponse(
        id=project.id,
        name=project.name,
        git_remote_normalized=project.git_remote_normalized,
        context_document=project.context_document,
        owner_id=project.owner_id,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.get("/{git_remote_normalized:path}", response_model=ProjectResponse)
async def get_project(
    git_remote_normalized: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Get a project context by git remote.

    User must have at least one session with this git remote
    or be the project owner.
    """
    stmt = select(Project).where(Project.git_remote_normalized == git_remote_normalized)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "No project context found")

    # Access check: owner or has sessions in this repo
    if project.owner_id != user.id:
        has_access = await _check_repo_access(db, user.id, git_remote_normalized)
        if not has_access:
            raise HTTPException(403, "No sessions found for this repository")

    return ProjectResponse(
        id=project.id,
        name=project.name,
        git_remote_normalized=project.git_remote_normalized,
        context_document=project.context_document,
        owner_id=project.owner_id,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.put("/{git_remote_normalized:path}/context")
async def update_project_context(
    git_remote_normalized: str,
    body: UpdateContextRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update the project context document."""
    stmt = select(Project).where(Project.git_remote_normalized == git_remote_normalized)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "No project context found")

    # Access check: owner or has sessions in this repo
    if project.owner_id != user.id:
        has_access = await _check_repo_access(db, user.id, git_remote_normalized)
        if not has_access:
            raise HTTPException(403, "No sessions found for this repository")

    project.context_document = body.context_document
    project.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {"status": "updated", "size": len(body.context_document)}


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a project context. Only the owner or an admin can delete."""
    stmt = select(Project).where(Project.id == project_id)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found")

    if project.owner_id != user.id and user.tier != "admin":
        raise HTTPException(403, "Only the project owner or an admin can delete this project")

    await db.delete(project)
    await db.commit()

    return {"status": "deleted", "id": project_id}
