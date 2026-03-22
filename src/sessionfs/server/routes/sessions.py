"""Session CRUD and sync routes."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Session, User
from sessionfs.server.schemas.sessions import (
    MessagesResponse,
    SessionDetail,
    SessionListResponse,
    SessionMetadataUpdate,
    SessionSummary,
    SessionUploadResponse,
    SyncPushResponse,
    WorkspaceResponse,
    ToolsResponse,
)
from sessionfs.server.storage.base import BlobStore

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

# --- Validation helpers ---

_SESSION_ID_RE = re.compile(r"^ses_[a-zA-Z0-9]{12,20}$")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


def _validate_session_id(session_id: str) -> str:
    """Validate and return session ID, or raise 400."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format")
    return session_id


async def _read_upload(file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read an upload with a size limit."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds maximum size of {max_bytes} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_tar_gz(data: bytes) -> None:
    """Validate that data is a legitimate .sfs tar.gz archive."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if ".." in member.name:
                    raise ValueError(f"Path traversal in tar member: {member.name}")
                if member.name.startswith("/"):
                    raise ValueError(f"Absolute path in tar member: {member.name}")
                if member.issym() or member.islnk():
                    raise ValueError(f"Symlink in tar archive: {member.name}")
                if member.size > 50 * 1024 * 1024:
                    raise ValueError(f"Member too large: {member.name} ({member.size} bytes)")
    except tarfile.TarError as e:
        raise ValueError(f"Invalid tar.gz archive: {e}") from e


def _sanitize_string(value: str) -> str:
    """Strip HTML tags and null bytes from a string."""
    if "\x00" in value:
        raise HTTPException(status_code=400, detail="Null bytes not allowed in input")
    # Strip HTML/script tags
    value = re.sub(r"<[^>]*>", "", value)
    return value


def _validate_tags(tags_json: str) -> str:
    """Validate tags JSON: array of strings, max 20 tags, max 50 chars each."""
    try:
        parsed = json.loads(tags_json)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="tags must be a valid JSON array of strings")
    if not isinstance(parsed, list) or not all(isinstance(t, str) for t in parsed):
        raise HTTPException(status_code=400, detail="tags must be a JSON array of strings")
    if len(parsed) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 tags allowed")
    for tag in parsed:
        if len(tag) > 50:
            raise HTTPException(status_code=400, detail="Each tag must be 50 characters or fewer")
        if "\x00" in tag:
            raise HTTPException(status_code=400, detail="Null bytes not allowed in tags")
    return tags_json


# --- Helpers ---


def _get_blob_store(request: Request) -> BlobStore:
    return request.app.state.blob_store


def _session_to_summary(s: Session) -> SessionSummary:
    return SessionSummary(
        id=s.id,
        title=s.title,
        tags=json.loads(s.tags) if s.tags else [],
        source_tool=s.source_tool,
        model_id=s.model_id,
        message_count=s.message_count,
        turn_count=s.turn_count,
        tool_use_count=s.tool_use_count,
        total_input_tokens=s.total_input_tokens,
        total_output_tokens=s.total_output_tokens,
        blob_size_bytes=s.blob_size_bytes,
        etag=s.etag,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _session_to_detail(s: Session) -> SessionDetail:
    return SessionDetail(
        id=s.id,
        title=s.title,
        tags=json.loads(s.tags) if s.tags else [],
        source_tool=s.source_tool,
        source_tool_version=s.source_tool_version,
        model_id=s.model_id,
        model_provider=s.model_provider,
        original_session_id=s.original_session_id,
        message_count=s.message_count,
        turn_count=s.turn_count,
        tool_use_count=s.tool_use_count,
        total_input_tokens=s.total_input_tokens,
        total_output_tokens=s.total_output_tokens,
        blob_size_bytes=s.blob_size_bytes,
        etag=s.etag,
        duration_ms=s.duration_ms,
        parent_session_id=s.parent_session_id,
        created_at=s.created_at,
        updated_at=s.updated_at,
        uploaded_at=s.uploaded_at,
    )


def _blob_key(user_id: str, session_id: str) -> str:
    return f"sessions/{user_id}/{session_id}/session.tar.gz"


import logging

_logger = logging.getLogger("sessionfs.server.routes.sessions")


def _extract_manifest_metadata(data: bytes) -> dict:
    """Extract metadata from a .sfs tar.gz archive's manifest.json.

    Returns a dict with fields suitable for populating a Session row.
    Applies smart title extraction (skips agent personas, system messages,
    redacts secrets) using the shared title_utils module.
    Returns empty defaults if manifest is missing or unparseable.
    """
    from sessionfs.utils.title_utils import extract_smart_title

    defaults = {
        "title": None,
        "source_tool": "unknown",
        "source_tool_version": None,
        "original_session_id": None,
        "model_provider": None,
        "model_id": None,
        "message_count": 0,
        "turn_count": 0,
        "tool_use_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "duration_ms": None,
        "tags": "[]",
    }
    try:
        manifest = None
        messages: list[dict] = []

        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name == "manifest.json" or member.name.endswith("/manifest.json"):
                    f = tar.extractfile(member)
                    if f:
                        manifest = json.loads(f.read())
                elif member.name == "messages.jsonl" or member.name.endswith("/messages.jsonl"):
                    f = tar.extractfile(member)
                    if f:
                        for line in f:
                            line = line.strip()
                            if line:
                                messages.append(json.loads(line))

        if manifest is None:
            return defaults

        source = manifest.get("source", {})
        model = manifest.get("model") or {}
        stats = manifest.get("stats") or {}

        raw_title = manifest.get("title")
        msg_count = stats.get("message_count", 0)

        defaults["title"] = extract_smart_title(
            messages=messages or None,
            raw_title=raw_title,
            message_count=msg_count,
        )
        defaults["source_tool"] = source.get("tool", "unknown")
        defaults["source_tool_version"] = source.get("tool_version")
        defaults["original_session_id"] = source.get("original_session_id")
        defaults["model_provider"] = model.get("provider")
        defaults["model_id"] = model.get("model_id")
        defaults["message_count"] = msg_count
        defaults["turn_count"] = stats.get("turn_count", 0)
        defaults["tool_use_count"] = stats.get("tool_use_count", 0)
        defaults["total_input_tokens"] = stats.get("total_input_tokens", 0)
        defaults["total_output_tokens"] = stats.get("total_output_tokens", 0)
        defaults["duration_ms"] = stats.get("duration_ms")
        defaults["tags"] = json.dumps(manifest.get("tags", []))

    except Exception as exc:
        _logger.warning("Failed to extract manifest metadata: %s", exc)

    return defaults


# --- Archive content cache ---

_CACHE_MAX_SIZE = 50
_archive_cache: OrderedDict[tuple[str, str], dict[str, object]] = OrderedDict()
_cache_lock = threading.Lock()


def _get_cached_archive_content(session_id: str, etag: str, data: bytes) -> dict[str, object]:
    """Get parsed archive contents, using an LRU cache keyed by (session_id, etag).

    Returns a dict with keys: "messages" (list[dict]), "workspace" (dict|None),
    "tools" (dict|None).
    """
    cache_key = (session_id, etag)

    with _cache_lock:
        if cache_key in _archive_cache:
            _archive_cache.move_to_end(cache_key)
            return _archive_cache[cache_key]

    # Parse outside the lock
    content = _extract_archive_content(data)

    with _cache_lock:
        _archive_cache[cache_key] = content
        _archive_cache.move_to_end(cache_key)
        while len(_archive_cache) > _CACHE_MAX_SIZE:
            _archive_cache.popitem(last=False)

    return content


def _extract_archive_content(data: bytes) -> dict[str, object]:
    """Extract messages, workspace, and tools from a .sfs tar.gz archive."""
    messages: list[dict] = []
    workspace: dict | None = None
    tools: dict | None = None

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                name = member.name
                # Strip any leading directory component
                basename = name.rsplit("/", 1)[-1] if "/" in name else name

                if basename == "messages.jsonl":
                    f = tar.extractfile(member)
                    if f is not None:
                        for line in f:
                            line = line.strip()
                            if line:
                                messages.append(json.loads(line))
                elif basename == "workspace.json":
                    f = tar.extractfile(member)
                    if f is not None:
                        workspace = json.loads(f.read())
                elif basename == "tools.json":
                    f = tar.extractfile(member)
                    if f is not None:
                        tools = json.loads(f.read())
    except Exception as exc:
        _logger.warning("Failed to extract archive content: %s", exc)

    return {"messages": messages, "workspace": workspace, "tools": tools}


def clear_archive_cache() -> None:
    """Clear the archive content cache. Exposed for testing."""
    with _cache_lock:
        _archive_cache.clear()


# --- Routes ---


@router.post("", response_model=SessionUploadResponse, status_code=201)
async def upload_session(
    file: UploadFile,
    source_tool: str = Query(..., min_length=1, max_length=50, pattern=r"^[a-z0-9_-]+$"),
    title: str | None = Query(None, max_length=500),
    tags: str = Query("[]", max_length=5000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Upload a new .sfs tar.gz session."""
    # M11: Input validation
    if title is not None:
        title = _sanitize_string(title)
    _validate_tags(tags)

    blob_store = _get_blob_store(request)

    # M3: Upload size limit
    data = await _read_upload(file)

    # M7: Tar archive validation
    try:
        _validate_tar_gz(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    etag = hashlib.sha256(data).hexdigest()
    key = _blob_key(user.id, session_id)

    await blob_store.put(key, data)

    # Extract metadata from manifest to fill in any gaps
    meta = _extract_manifest_metadata(data)

    now = datetime.now(timezone.utc)
    session = Session(
        id=session_id,
        user_id=user.id,
        title=title or meta["title"],
        tags=tags if tags != "[]" else meta["tags"],
        source_tool=source_tool,
        source_tool_version=meta["source_tool_version"],
        original_session_id=meta["original_session_id"],
        model_provider=meta["model_provider"],
        model_id=meta["model_id"],
        message_count=meta["message_count"],
        turn_count=meta["turn_count"],
        tool_use_count=meta["tool_use_count"],
        total_input_tokens=meta["total_input_tokens"],
        total_output_tokens=meta["total_output_tokens"],
        duration_ms=meta["duration_ms"],
        blob_key=key,
        blob_size_bytes=len(data),
        etag=etag,
        created_at=now,
        updated_at=now,
        uploaded_at=now,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    return SessionUploadResponse(
        session_id=session.id,
        etag=session.etag,
        blob_size_bytes=session.blob_size_bytes,
        uploaded_at=session.uploaded_at,
    )


@router.get("", response_model=SessionListResponse)
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    source_tool: str | None = Query(None),
    tag: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List sessions with pagination and optional filters."""
    query = select(Session).where(
        Session.user_id == user.id,
        Session.is_deleted == False,  # noqa: E712
    )
    count_query = select(func.count()).select_from(Session).where(
        Session.user_id == user.id,
        Session.is_deleted == False,  # noqa: E712
    )

    if source_tool:
        query = query.where(Session.source_tool == source_tool)
        count_query = count_query.where(Session.source_tool == source_tool)
    if tag:
        # JSON text search — works for SQLite and PostgreSQL
        query = query.where(Session.tags.contains(f'"{tag}"'))
        count_query = count_query.where(Session.tags.contains(f'"{tag}"'))

    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(Session.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    sessions = result.scalars().all()

    return SessionListResponse(
        sessions=[_session_to_summary(s) for s in sessions],
        total=total,
        page=page,
        page_size=page_size,
        has_more=(page * page_size) < total,
    )


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get session metadata."""
    _validate_session_id(session_id)
    session = await _get_user_session(db, user.id, session_id)
    return _session_to_detail(session)


@router.get("/{session_id}/download")
async def download_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Download the session tar.gz blob."""
    _validate_session_id(session_id)
    session = await _get_user_session(db, user.id, session_id)
    blob_store = _get_blob_store(request)
    data = await blob_store.get(session.blob_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Session blob not found")

    return Response(
        content=data,
        media_type="application/gzip",
        headers={"ETag": f'"{session.etag}"'},
    )


@router.patch("/{session_id}", response_model=SessionDetail)
async def update_session(
    session_id: str,
    body: SessionMetadataUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update session title and/or tags."""
    _validate_session_id(session_id)
    session = await _get_user_session(db, user.id, session_id)

    if body.title is not None:
        session.title = _sanitize_string(body.title)
    if body.tags is not None:
        # M11: Validate tags
        for tag in body.tags:
            if len(tag) > 50:
                raise HTTPException(status_code=400, detail="Each tag must be 50 characters or fewer")
            if "\x00" in tag:
                raise HTTPException(status_code=400, detail="Null bytes not allowed in tags")
        if len(body.tags) > 20:
            raise HTTPException(status_code=400, detail="Maximum 20 tags allowed")
        session.tags = json.dumps(body.tags)
    session.updated_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(session)
    return _session_to_detail(session)


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft delete a session."""
    _validate_session_id(session_id)
    session = await _get_user_session(db, user.id, session_id)
    session.is_deleted = True
    session.deleted_at = datetime.now(timezone.utc)
    await db.commit()


@router.put("/{session_id}/sync", status_code=200)
async def sync_push(
    session_id: str,
    file: UploadFile,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Push session data with ETag-based conflict detection."""
    _validate_session_id(session_id)
    blob_store = _get_blob_store(request)

    # M3: Upload size limit
    data = await _read_upload(file)

    # M7: Tar archive validation
    try:
        _validate_tar_gz(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    new_etag = hashlib.sha256(data).hexdigest()
    key = _blob_key(user.id, session_id)
    now = datetime.now(timezone.utc)

    # Check if session exists
    result = await db.execute(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == user.id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    existing = result.scalar_one_or_none()

    # Extract metadata from the archive's manifest.json
    meta = _extract_manifest_metadata(data)

    if existing is None:
        # New session -> create with full metadata
        await blob_store.put(key, data)
        session = Session(
            id=session_id,
            user_id=user.id,
            title=meta["title"],
            tags=meta["tags"],
            source_tool=meta["source_tool"],
            source_tool_version=meta["source_tool_version"],
            original_session_id=meta["original_session_id"],
            model_provider=meta["model_provider"],
            model_id=meta["model_id"],
            message_count=meta["message_count"],
            turn_count=meta["turn_count"],
            tool_use_count=meta["tool_use_count"],
            total_input_tokens=meta["total_input_tokens"],
            total_output_tokens=meta["total_output_tokens"],
            duration_ms=meta["duration_ms"],
            blob_key=key,
            blob_size_bytes=len(data),
            etag=new_etag,
            created_at=now,
            updated_at=now,
            uploaded_at=now,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        return Response(
            status_code=201,
            content=SyncPushResponse(
                session_id=session.id,
                etag=session.etag,
                blob_size_bytes=session.blob_size_bytes,
                synced_at=now,
            ).model_dump_json(),
            media_type="application/json",
        )

    # Existing -> check If-Match
    if_match = request.headers.get("If-Match", "").strip('"')
    if not if_match or if_match != existing.etag:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "etag_mismatch",
                "message": "ETag mismatch — session has been updated",
                "current_etag": existing.etag,
            },
        )

    # ETag matches -> replace blob and update metadata
    await blob_store.put(key, data)
    existing.title = meta["title"]
    existing.tags = meta["tags"]
    existing.source_tool = meta["source_tool"]
    existing.source_tool_version = meta["source_tool_version"]
    existing.original_session_id = meta["original_session_id"]
    existing.model_provider = meta["model_provider"]
    existing.model_id = meta["model_id"]
    existing.message_count = meta["message_count"]
    existing.turn_count = meta["turn_count"]
    existing.tool_use_count = meta["tool_use_count"]
    existing.total_input_tokens = meta["total_input_tokens"]
    existing.total_output_tokens = meta["total_output_tokens"]
    existing.duration_ms = meta["duration_ms"]
    existing.blob_size_bytes = len(data)
    existing.etag = new_etag
    existing.updated_at = now
    await db.commit()
    await db.refresh(existing)

    return SyncPushResponse(
        session_id=existing.id,
        etag=existing.etag,
        blob_size_bytes=existing.blob_size_bytes,
        synced_at=now,
    )


@router.get("/{session_id}/sync")
async def sync_pull(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Pull session data with ETag-based caching."""
    _validate_session_id(session_id)
    session = await _get_user_session(db, user.id, session_id)

    # Check If-None-Match
    if_none_match = request.headers.get("If-None-Match", "").strip('"')
    if if_none_match and if_none_match == session.etag:
        return Response(status_code=304)

    blob_store = _get_blob_store(request)
    data = await blob_store.get(session.blob_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Session blob not found")

    return Response(
        content=data,
        media_type="application/gzip",
        headers={"ETag": f'"{session.etag}"'},
    )


@router.get("/{session_id}/messages", response_model=MessagesResponse)
async def get_session_messages(
    session_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Get paginated messages from a session archive."""
    _validate_session_id(session_id)
    session = await _get_user_session(db, user.id, session_id)
    blob_store = _get_blob_store(request)
    data = await blob_store.get(session.blob_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Session blob not found")

    content = _get_cached_archive_content(session_id, session.etag, data)
    messages = content["messages"]

    total = len(messages)
    start = (page - 1) * page_size
    end = start + page_size
    page_messages = messages[start:end]

    return {
        "messages": page_messages,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
    }


@router.get("/{session_id}/workspace", response_model=WorkspaceResponse)
async def get_session_workspace(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Get workspace metadata from a session archive."""
    _validate_session_id(session_id)
    session = await _get_user_session(db, user.id, session_id)
    blob_store = _get_blob_store(request)
    data = await blob_store.get(session.blob_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Session blob not found")

    content = _get_cached_archive_content(session_id, session.etag, data)
    workspace = content["workspace"]

    if workspace is None:
        raise HTTPException(status_code=404, detail="No workspace.json in session archive")

    return {"workspace": workspace}


@router.get("/{session_id}/tools", response_model=ToolsResponse)
async def get_session_tools(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Get tools metadata from a session archive."""
    _validate_session_id(session_id)
    session = await _get_user_session(db, user.id, session_id)
    blob_store = _get_blob_store(request)
    data = await blob_store.get(session.blob_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Session blob not found")

    content = _get_cached_archive_content(session_id, session.etag, data)
    tools = content["tools"]

    if tools is None:
        raise HTTPException(status_code=404, detail="No tools.json in session archive")

    return {"tools": tools}


@router.post("/admin/reindex")
async def reindex_sessions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Re-extract metadata from stored archives for all user sessions.

    Useful after deploying metadata extraction improvements to backfill
    sessions that were pushed before the extraction code existed.
    """
    blob_store = _get_blob_store(request)
    result_query = await db.execute(
        select(Session).where(
            Session.user_id == user.id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    sessions = result_query.scalars().all()

    reindexed = 0
    updated = 0
    errors = 0

    for session in sessions:
        reindexed += 1
        try:
            data = await blob_store.get(session.blob_key)
            if data is None:
                errors += 1
                continue

            meta = _extract_manifest_metadata(data)

            session.title = meta["title"]
            session.tags = meta["tags"]
            session.source_tool = meta["source_tool"]
            session.source_tool_version = meta["source_tool_version"]
            session.original_session_id = meta["original_session_id"]
            session.model_provider = meta["model_provider"]
            session.model_id = meta["model_id"]
            session.message_count = meta["message_count"]
            session.turn_count = meta["turn_count"]
            session.tool_use_count = meta["tool_use_count"]
            session.total_input_tokens = meta["total_input_tokens"]
            session.total_output_tokens = meta["total_output_tokens"]
            session.duration_ms = meta["duration_ms"]
            updated += 1

        except Exception as exc:
            _logger.warning("Failed to reindex session %s: %s", session.id, exc)
            errors += 1

    await db.commit()

    return {
        "reindexed": reindexed,
        "updated": updated,
        "errors": errors,
    }


async def _get_user_session(db: AsyncSession, user_id: str, session_id: str) -> Session:
    """Get a session owned by the user, or raise 404."""
    result = await db.execute(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == user_id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session
