"""Bookmark folder routes: CRUD for folders and bookmarks."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Bookmark, BookmarkFolder, Session, User
from sessionfs.server.schemas.bookmarks import (
    BookmarkResponse,
    CreateBookmarkRequest,
    CreateFolderRequest,
    FolderListResponse,
    FolderResponse,
    UpdateFolderRequest,
)

router = APIRouter(prefix="/api/v1/bookmarks", tags=["bookmarks"])


# ---------------------------------------------------------------------------
# Folder endpoints
# ---------------------------------------------------------------------------


@router.post("/folders", status_code=201, response_model=FolderResponse)
async def create_folder(
    body: CreateFolderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new bookmark folder."""
    folder = BookmarkFolder(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name=body.name,
        color=body.color,
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)

    return FolderResponse(
        id=folder.id,
        name=folder.name,
        color=folder.color,
        bookmark_count=0,
        created_at=folder.created_at,
    )


@router.get("/folders", response_model=FolderListResponse)
async def list_folders(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all bookmark folders for the current user with bookmark counts."""
    stmt = (
        select(
            BookmarkFolder,
            func.count(Bookmark.id).label("bookmark_count"),
        )
        .outerjoin(Bookmark, Bookmark.folder_id == BookmarkFolder.id)
        .where(BookmarkFolder.user_id == user.id)
        .group_by(BookmarkFolder.id)
        .order_by(BookmarkFolder.created_at)
    )
    result = await db.execute(stmt)
    rows = result.all()

    folders = [
        FolderResponse(
            id=folder.id,
            name=folder.name,
            color=folder.color,
            bookmark_count=count,
            created_at=folder.created_at,
        )
        for folder, count in rows
    ]
    return FolderListResponse(folders=folders)


@router.put("/folders/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: str,
    body: UpdateFolderRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rename or recolor a bookmark folder."""
    result = await db.execute(
        select(BookmarkFolder).where(
            BookmarkFolder.id == folder_id,
            BookmarkFolder.user_id == user.id,
        )
    )
    folder = result.scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    if body.name is not None:
        folder.name = body.name
    if body.color is not None:
        folder.color = body.color

    await db.commit()
    await db.refresh(folder)

    # Get bookmark count
    count_result = await db.execute(
        select(func.count(Bookmark.id)).where(Bookmark.folder_id == folder.id)
    )
    count = count_result.scalar() or 0

    return FolderResponse(
        id=folder.id,
        name=folder.name,
        color=folder.color,
        bookmark_count=count,
        created_at=folder.created_at,
    )


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a bookmark folder (cascades bookmarks)."""
    result = await db.execute(
        select(BookmarkFolder).where(
            BookmarkFolder.id == folder_id,
            BookmarkFolder.user_id == user.id,
        )
    )
    folder = result.scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    # Delete bookmarks first (SQLite doesn't always enforce CASCADE)
    await db.execute(delete(Bookmark).where(Bookmark.folder_id == folder_id))
    await db.delete(folder)
    await db.commit()


# ---------------------------------------------------------------------------
# Bookmark endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=201, response_model=BookmarkResponse)
async def add_bookmark(
    body: CreateBookmarkRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bookmark a session into a folder. Prevents duplicates."""
    # Verify folder belongs to user
    folder_result = await db.execute(
        select(BookmarkFolder).where(
            BookmarkFolder.id == body.folder_id,
            BookmarkFolder.user_id == user.id,
        )
    )
    if folder_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    # Verify session exists and belongs to user
    session_result = await db.execute(
        select(Session).where(
            Session.id == body.session_id,
            Session.user_id == user.id,
        )
    )
    if session_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Check for duplicate
    dup_result = await db.execute(
        select(Bookmark).where(
            Bookmark.folder_id == body.folder_id,
            Bookmark.session_id == body.session_id,
            Bookmark.user_id == user.id,
        )
    )
    if dup_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Session already bookmarked in this folder")

    bookmark = Bookmark(
        id=str(uuid.uuid4()),
        folder_id=body.folder_id,
        session_id=body.session_id,
        user_id=user.id,
    )
    db.add(bookmark)
    await db.commit()
    await db.refresh(bookmark)

    return BookmarkResponse(
        id=bookmark.id,
        folder_id=bookmark.folder_id,
        session_id=bookmark.session_id,
        created_at=bookmark.created_at,
    )


@router.delete("/{bookmark_id}", status_code=204)
async def remove_bookmark(
    bookmark_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove a bookmark."""
    result = await db.execute(
        select(Bookmark).where(
            Bookmark.id == bookmark_id,
            Bookmark.user_id == user.id,
        )
    )
    bookmark = result.scalar_one_or_none()
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")

    await db.delete(bookmark)
    await db.commit()


@router.get("/folders/{folder_id}/sessions")
async def list_folder_sessions(
    folder_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List sessions in a bookmark folder with bookmark info."""
    # Verify folder belongs to user
    folder_result = await db.execute(
        select(BookmarkFolder).where(
            BookmarkFolder.id == folder_id,
            BookmarkFolder.user_id == user.id,
        )
    )
    if folder_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    stmt = (
        select(Session, Bookmark.id.label("bookmark_id"), Bookmark.created_at.label("bookmarked_at"))
        .join(Bookmark, Bookmark.session_id == Session.id)
        .where(
            Bookmark.folder_id == folder_id,
            Bookmark.user_id == user.id,
        )
        .order_by(Bookmark.created_at.desc())
    )
    result = await db.execute(stmt)
    rows = result.all()

    sessions = []
    for session, bookmark_id, bookmarked_at in rows:
        import json as _json

        tags = _json.loads(session.tags) if session.tags else []
        sessions.append(
            {
                "id": session.id,
                "title": session.title,
                "alias": session.alias,
                "tags": tags,
                "source_tool": session.source_tool,
                "model_id": session.model_id,
                "message_count": session.message_count,
                "turn_count": session.turn_count,
                "tool_use_count": session.tool_use_count,
                "total_input_tokens": session.total_input_tokens,
                "total_output_tokens": session.total_output_tokens,
                "blob_size_bytes": session.blob_size_bytes,
                "etag": session.etag,
                "created_at": session.created_at.isoformat() if session.created_at else None,
                "updated_at": session.updated_at.isoformat() if session.updated_at else None,
                "bookmark_id": bookmark_id,
                "bookmarked_at": bookmarked_at.isoformat() if bookmarked_at else None,
            }
        )

    return {"sessions": sessions, "total": len(sessions)}
