"""Bookmark folder request/response schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CreateFolderRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")


class FolderResponse(BaseModel):
    id: str
    name: str
    color: str | None
    bookmark_count: int
    created_at: datetime


class UpdateFolderRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    color: str | None = None


class CreateBookmarkRequest(BaseModel):
    folder_id: str
    session_id: str


class BookmarkResponse(BaseModel):
    id: str
    folder_id: str
    session_id: str
    created_at: datetime


class FolderListResponse(BaseModel):
    folders: list[FolderResponse]
