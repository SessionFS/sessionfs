"""Session CRUD and sync routes."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import secrets
import tarfile
import threading
import uuid
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel as _BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user, require_verified_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Session, ShareLink, User
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context
from sessionfs.server.schemas.sessions import (
    CreateShareLinkRequest,
    MessagesResponse,
    SearchMatch,
    SearchResponse,
    SearchResult,
    SetAliasRequest,
    SessionDetail,
    SessionListResponse,
    SessionMetadataUpdate,
    SessionSummary,
    SessionUploadResponse,
    ShareLinkResponse,
    SyncPushResponse,
    WorkspaceResponse,
    ToolsResponse,
)
from sessionfs.server.storage.base import BlobStore

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

SFS_MAX_SYNC_BYTES_FREE = int(os.environ.get("SFS_MAX_SYNC_BYTES_FREE", str(50 * 1024 * 1024)))
SFS_MAX_SYNC_BYTES_PAID = int(os.environ.get("SFS_MAX_SYNC_BYTES_PAID", str(300 * 1024 * 1024)))

# Per-user concurrency limit for sync_push (max 3 concurrent syncs per user)
_user_sync_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(3)
)

def _sync_limit_for_tier(tier) -> int:
    """Return sync byte limit based on a resolved effective Tier enum value.

    Use `get_effective_tier(user, db)` from tier_gate to resolve the tier first
    so org members inherit their org's tier instead of being limited by their
    personal user.tier field.
    """
    # Import locally to avoid circular imports at module load time
    from sessionfs.server.tiers import Tier

    if tier in (Tier.PRO, Tier.TEAM, Tier.ENTERPRISE):
        return SFS_MAX_SYNC_BYTES_PAID
    return SFS_MAX_SYNC_BYTES_FREE

def _sync_limit_human(limit: int) -> str:
    """Format byte limit for error messages."""
    return f"{limit // (1024 * 1024)}MB"

# --- Validation helpers ---

_SESSION_ID_RE = re.compile(r"^ses_[a-z0-9]{8,40}$")
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


def _validate_session_id(session_id: str) -> str:
    """Validate and return session ID, or raise 400."""
    if not _SESSION_ID_RE.match(session_id):
        logger.warning(
            "Rejected invalid session ID: %r (length=%d)",
            session_id[:50],
            len(session_id),
        )
        raise HTTPException(status_code=400, detail="Invalid session ID format")
    return session_id


def _validate_session_id_or_alias(session_id_or_alias: str) -> str:
    """Validate as either a session ID or alias format, or raise 400."""
    if _SESSION_ID_RE.match(session_id_or_alias):
        return session_id_or_alias
    # Check alias format: 3-100 chars, alphanumeric + hyphens + underscores
    if re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,99}$", session_id_or_alias):
        return session_id_or_alias
    raise HTTPException(status_code=400, detail="Invalid session ID or alias format")


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
                if member.size > 100 * 1024 * 1024:  # 100MB per member
                    raise ValueError(f"Member too large: {member.name} ({member.size // (1024*1024)}MB, limit 100MB)")
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
        alias=s.alias,
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
        parent_session_id=s.parent_session_id,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _session_to_detail(s: Session) -> SessionDetail:
    return SessionDetail(
        id=s.id,
        title=s.title,
        alias=s.alias,
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
        dlp_scan_results=getattr(s, "dlp_scan_results", None),
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
        "parent_session_id": None,
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
        raw_model_id = model.get("model_id")
        if raw_model_id in ("<synthetic>", "synthetic", ""):
            raw_model_id = None
        defaults["model_id"] = raw_model_id
        defaults["message_count"] = msg_count
        defaults["turn_count"] = stats.get("turn_count", 0)
        defaults["tool_use_count"] = stats.get("tool_use_count", 0)
        defaults["total_input_tokens"] = stats.get("total_input_tokens", 0)
        defaults["total_output_tokens"] = stats.get("total_output_tokens", 0)
        defaults["duration_ms"] = stats.get("duration_ms")
        defaults["tags"] = json.dumps(manifest.get("tags", []))
        defaults["parent_session_id"] = manifest.get("parent_session_id") or manifest.get("_resume_parent_id")

        # Rules provenance (migration 028) — embedded by the watchers when
        # they capture a native session. Unknown clients leave this absent,
        # which is fine (defaults to rules_source="none").
        provenance = manifest.get("instruction_provenance") or {}
        if isinstance(provenance, dict):
            defaults["rules_source"] = provenance.get("rules_source") or "none"
            defaults["rules_version"] = provenance.get("rules_version")
            raw_hash = provenance.get("rules_hash") or ""
            # Strip algorithm prefix (e.g. "sha256:") — the column is
            # String(80) but older deployments may still have String(64)
            # from migration 028 before 029 runs. The hex digest alone
            # fits in 64 chars and is sufficient for identity matching.
            if ":" in raw_hash:
                raw_hash = raw_hash.split(":", 1)[1]
            defaults["rules_hash"] = raw_hash or None
            arts = provenance.get("instruction_artifacts") or []
            if isinstance(arts, list):
                defaults["instruction_artifacts"] = json.dumps(arts)
            else:
                defaults["instruction_artifacts"] = "[]"
        else:
            defaults["rules_source"] = "none"
            defaults["rules_version"] = None
            defaults["rules_hash"] = None
            defaults["instruction_artifacts"] = "[]"

    except Exception as exc:
        _logger.warning("Failed to extract manifest metadata: %s", exc)

    # Provide safe defaults for provenance even if manifest was missing.
    defaults.setdefault("rules_source", "none")
    defaults.setdefault("rules_version", None)
    defaults.setdefault("rules_hash", None)
    defaults.setdefault("instruction_artifacts", "[]")

    return defaults


def _extract_workspace_from_archive(data: bytes) -> dict | None:
    """Extract git metadata from workspace.json in a .sfs tar.gz archive.

    Returns a dict with git_remote, git_branch, git_commit or None if missing.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name == "workspace.json" or member.name.endswith("/workspace.json"):
                    f = tar.extractfile(member)
                    if f:
                        ws = json.loads(f.read())
                        git = ws.get("git") or {}
                        return {
                            "git_remote": git.get("remote_url", ""),
                            "git_branch": git.get("branch", ""),
                            "git_commit": git.get("commit_sha", ""),
                        }
    except Exception as exc:
        _logger.warning("Failed to extract workspace metadata: %s", exc)
    return None


_MAX_MESSAGES_TEXT_BYTES = 100 * 1024  # 100KB limit


def _extract_messages_text(data: bytes) -> str:
    """Extract all text content from messages.jsonl in a tar.gz archive.

    Concatenates user/assistant text, tool names, and error output.
    Result is limited to 100KB for storage efficiency.
    """
    parts: list[str] = []
    total_len = 0

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                basename = member.name.rsplit("/", 1)[-1] if "/" in member.name else member.name
                if basename != "messages.jsonl":
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = _extract_msg_text(msg)
                    if text:
                        parts.append(text)
                        total_len += len(text)
                        if total_len >= _MAX_MESSAGES_TEXT_BYTES:
                            break
                break  # Only process first messages.jsonl found
    except Exception as exc:
        _logger.warning("Failed to extract messages text: %s", exc)

    result = "\n".join(parts)
    return result[:_MAX_MESSAGES_TEXT_BYTES]


def _extract_msg_text(msg: dict) -> str:
    """Extract plain text from a single message for indexing."""
    content = msg.get("content", [])
    if isinstance(content, str):
        return content

    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name", block.get("tool_name", ""))
                inp = block.get("input", {})
                if isinstance(inp, dict):
                    cmd = inp.get("command", "")
                    if cmd:
                        parts.append(f"[{name}] {cmd}")
                    else:
                        parts.append(f"[{name}]")
            elif btype == "tool_result":
                result = block.get("content", "")
                if isinstance(result, str):
                    parts.append(result[:500])
    return "\n".join(parts)


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
    user: User = Depends(require_verified_user),
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
    messages_text = _extract_messages_text(data)

    # Extract git metadata for PR matching
    workspace_data = _extract_workspace_from_archive(data)
    git_remote_normalized = ""
    git_branch = ""
    git_commit = ""
    if workspace_data:
        from sessionfs.server.github_app import normalize_git_remote
        git_remote_normalized = normalize_git_remote(workspace_data.get("git_remote", ""))
        git_branch = workspace_data.get("git_branch", "")
        git_commit = workspace_data.get("git_commit", "")

    now = datetime.now(timezone.utc)
    session = Session(
        id=session_id,
        user_id=user.id,
        title=title or meta["title"],
        tags=tags if tags != "[]" else meta["tags"],
        source_tool=source_tool,
        source_tool_version=meta["source_tool_version"],
        original_session_id=meta["original_session_id"],
        parent_session_id=meta.get("parent_session_id"),
        model_provider=meta["model_provider"],
        model_id=meta["model_id"],
        message_count=meta["message_count"],
        turn_count=meta["turn_count"],
        tool_use_count=meta["tool_use_count"],
        total_input_tokens=meta["total_input_tokens"],
        total_output_tokens=meta["total_output_tokens"],
        duration_ms=meta["duration_ms"],
        messages_text=messages_text,
        blob_key=key,
        blob_size_bytes=len(data),
        etag=etag,
        created_at=now,
        updated_at=now,
        uploaded_at=now,
        git_remote_normalized=git_remote_normalized,
        git_branch=git_branch,
        git_commit=git_commit,
        rules_source=meta.get("rules_source", "none"),
        rules_version=meta.get("rules_version"),
        rules_hash=meta.get("rules_hash"),
        instruction_artifacts=meta.get("instruction_artifacts", "[]"),
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

    query = query.order_by(Session.updated_at.desc())
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


@router.get("/search", response_model=SearchResponse)
async def search_sessions(
    q: str = Query(..., min_length=1, max_length=500),
    tool: str | None = Query(None),
    days: int | None = Query(None, ge=1, le=365),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
):
    """Full-text search across sessions (Starter+ tier required)."""
    check_feature(ctx, "cloud_sync")

    offset = (page - 1) * page_size

    # Build the search query — use SQLite LIKE fallback for test environments,
    # PostgreSQL ts_vector for production
    dialect = db.bind.dialect.name if db.bind else "sqlite"

    if dialect == "postgresql":
        # PostgreSQL full-text search with ts_vector
        from sqlalchemy import text as sa_text

        # Build query dynamically to avoid asyncpg ambiguous param types
        tool_clause = "AND source_tool = :tool" if tool else ""
        cutoff_clause = "AND updated_at >= CAST(:cutoff AS timestamptz)" if days else ""

        search_sql = sa_text(f"""
            SELECT id, title, alias, source_tool, model_id, message_count, updated_at,
                   ts_headline('english', messages_text, query,
                               'MaxWords=30, MinWords=15, StartSel=<mark>, StopSel=</mark>') as snippet
            FROM sessions, plainto_tsquery('english', :q) query
            WHERE user_id = :user_id
              AND search_vector @@ query
              AND is_deleted = false
              {tool_clause}
              {cutoff_clause}
            ORDER BY ts_rank(search_vector, query) DESC
            LIMIT :limit OFFSET :offset
        """)
        count_sql = sa_text(f"""
            SELECT count(*)
            FROM sessions, plainto_tsquery('english', :q) query
            WHERE user_id = :user_id
              AND search_vector @@ query
              AND is_deleted = false
              {tool_clause}
              {cutoff_clause}
        """)
    else:
        # SQLite fallback: simple LIKE-based search
        from sqlalchemy import text as sa_text

        tool_clause = "AND source_tool = :tool" if tool else ""
        cutoff_clause = "AND updated_at >= :cutoff" if days else ""

        search_sql = sa_text(f"""
            SELECT id, title, alias, source_tool, model_id, message_count, updated_at,
                   substr(messages_text, max(1, instr(lower(messages_text), lower(:q)) - 40), 100) as snippet
            FROM sessions
            WHERE user_id = :user_id
              AND (lower(title) LIKE '%' || lower(:q) || '%'
                   OR lower(messages_text) LIKE '%' || lower(:q) || '%')
              AND is_deleted = 0
              {tool_clause}
              {cutoff_clause}
            ORDER BY updated_at DESC
            LIMIT :limit OFFSET :offset
        """)
        count_sql = sa_text(f"""
            SELECT count(*)
            FROM sessions
            WHERE user_id = :user_id
              AND (lower(title) LIKE '%' || lower(:q) || '%'
                   OR lower(messages_text) LIKE '%' || lower(:q) || '%')
              AND is_deleted = 0
              {tool_clause}
              {cutoff_clause}
        """)

    cutoff = None
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    params: dict = {
        "q": q,
        "user_id": user.id,
        "limit": page_size,
        "offset": offset,
    }
    count_params: dict = {"q": q, "user_id": user.id}
    if tool:
        params["tool"] = tool
        count_params["tool"] = tool
    if cutoff:
        params["cutoff"] = cutoff
        count_params["cutoff"] = cutoff

    result = await db.execute(search_sql, params)
    rows = result.fetchall()

    count_result = await db.execute(count_sql, count_params)
    total = count_result.scalar() or 0

    results = []
    for row in rows:
        snippet = row.snippet or ""
        results.append(SearchResult(
            session_id=row.id,
            title=row.title,
            alias=row.alias,
            source_tool=row.source_tool,
            model_id=row.model_id,
            message_count=row.message_count,
            updated_at=row.updated_at,
            matches=[SearchMatch(snippet=snippet)] if snippet else [],
        ))

    return SearchResponse(
        results=results,
        total=total,
        page=page,
        page_size=page_size,
        query=q,
    )


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get session metadata."""
    _validate_session_id_or_alias(session_id)
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
    _validate_session_id_or_alias(session_id)
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
    """Update session title, alias, and/or tags."""
    _validate_session_id_or_alias(session_id)
    session = await _get_user_session(db, user.id, session_id)

    if body.title is not None:
        session.title = _sanitize_string(body.title)
    if body.alias is not None:
        if not _ALIAS_RE.match(body.alias):
            raise HTTPException(status_code=400, detail="Alias must be 3-100 chars, alphanumeric/hyphens/underscores, starting with alphanumeric")
        # Check uniqueness
        existing = await db.execute(
            select(Session).where(
                Session.alias == body.alias,
                Session.user_id == user.id,
                Session.id != session.id,
                Session.is_deleted == False,  # noqa: E712
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"Alias '{body.alias}' is already in use")
        session.alias = body.alias
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


@router.put("/{session_id}/alias", response_model=SessionDetail)
async def set_alias(
    session_id: str,
    body: SetAliasRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set or update a session alias."""
    _validate_session_id_or_alias(session_id)
    session = await _get_user_session(db, user.id, session_id)

    # Check uniqueness per user
    existing = await db.execute(
        select(Session).where(
            Session.alias == body.alias,
            Session.user_id == user.id,
            Session.id != session.id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Alias '{body.alias}' is already in use")

    session.alias = body.alias
    session.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    return _session_to_detail(session)


@router.delete("/{session_id}/alias", status_code=200, response_model=SessionDetail)
async def clear_alias(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clear a session alias."""
    _validate_session_id_or_alias(session_id)
    session = await _get_user_session(db, user.id, session_id)
    session.alias = None
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
    _validate_session_id_or_alias(session_id)
    session = await _get_user_session(db, user.id, session_id)
    session.is_deleted = True
    session.deleted_at = datetime.now(timezone.utc)
    await db.commit()


@router.put("/{session_id}/sync", status_code=200)
async def sync_push(
    session_id: str,
    file: UploadFile,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_verified_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Push session data with ETag-based conflict detection."""
    # Per-user concurrency limit
    semaphore = _user_sync_semaphores[user.id]
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(
            429,
            detail={"error": "sync_rate_limit", "message": "Too many concurrent syncs. Please wait."},
            headers={"Retry-After": "5"},
        )

    try:
        check_feature(ctx, "cloud_sync")
        _validate_session_id(session_id)
        blob_store = _get_blob_store(request)

        # Track client version and device info from headers
        _client_version = request.headers.get("X-Client-Version", "")
        _client_platform = request.headers.get("X-Client-Platform", "")
        _client_device = request.headers.get("X-Client-Device", "")
        if not _client_version:
            ua = request.headers.get("User-Agent", "")
            if ua.startswith("sessionfs-cli/"):
                _client_version = ua.split("/", 1)[1].split(" ", 1)[0]

        # ── Phase 1: DB reads (user, session, features, storage, DLP policy) ──
        user_id = user.id
        if _client_version or _client_platform:
            user.last_client_version = _client_version[:20] if _client_version else None
            user.last_client_platform = _client_platform[:50] if _client_platform else None
            user.last_client_device = _client_device[:100] if _client_device else None
            user.last_sync_at = datetime.now(timezone.utc)

        # Tier-based sync limit — resolve effective tier so org members
        # inherit their org's tier instead of being limited by user.tier.
        from sessionfs.server.tier_gate import get_effective_tier
        effective_tier = await get_effective_tier(user, db)
        sync_limit = _sync_limit_for_tier(effective_tier)
        limit_str = _sync_limit_human(sync_limit)

        # Check content-length against sync limit
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > sync_limit:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "PAYLOAD_TOO_LARGE",
                    "message": (
                        f"Session exceeds {limit_str} cloud limit for your tier. "
                        "Run sfs compact to reduce size, or upgrade your plan."
                    ),
                },
            )

        # M3: Upload size limit
        data = await _read_upload(file, max_bytes=sync_limit)

        # Enforce sync byte limit on actual data
        if len(data) > sync_limit:
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "PAYLOAD_TOO_LARGE",
                    "message": (
                        f"Session exceeds {limit_str} cloud limit for your tier. "
                        "Run sfs compact to reduce size, or upgrade your plan."
                    ),
                },
            )

        # Check if session exists
        result = await db.execute(
            select(Session).where(
                Session.id == session_id,
                Session.user_id == user_id,
                Session.is_deleted == False,  # noqa: E712
            )
        )
        existing = result.scalar_one_or_none()

        # Check for soft-deleted or other-user conflict
        is_undelete = False
        if existing is None:
            any_result = await db.execute(
                select(Session).where(Session.id == session_id)
            )
            any_existing = any_result.scalar_one_or_none()
            if any_existing is not None:
                if any_existing.user_id != user_id:
                    raise HTTPException(status_code=409, detail="Session ID already claimed by another user")
                # Same user's soft-deleted session — mark for un-delete
                is_undelete = True

        # Capture DLP policy info while we still have DB context
        is_org_user = ctx.is_org_user
        org = ctx.org

        # Flush client version tracking updates before releasing connection
        await db.commit()

        # ── Release DB connection back to pool before CPU/IO-heavy work ──
        await db.close()

        # ── Phase 2: CPU work + blob upload (no DB connection held) ──

        # M7: Tar archive validation
        try:
            _validate_tar_gz(data)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        new_etag = hashlib.sha256(data).hexdigest()
        key = _blob_key(user_id, session_id)
        now = datetime.now(timezone.utc)

        # Extract metadata from the archive's manifest.json
        meta = _extract_manifest_metadata(data)
        messages_text = _extract_messages_text(data)

        # DLP scan (after extraction, before blob storage)
        dlp_scan_results = None
        if is_org_user and org:
            from sessionfs.server.dlp import get_org_dlp_policy, redact_and_repack

            dlp_policy = get_org_dlp_policy(org)
            if dlp_policy and dlp_policy.get("enabled"):
                from sessionfs.security.secrets import scan_dlp

                import io as _io
                import tarfile as _tarfile
                scan_text_parts: list[str] = []
                try:
                    with _tarfile.open(fileobj=_io.BytesIO(data), mode="r:gz") as _tar:
                        for _member in _tar.getmembers():
                            if _member.name.endswith((".json", ".jsonl")):
                                _f = _tar.extractfile(_member)
                                if _f:
                                    scan_text_parts.append(_f.read().decode("utf-8", errors="replace"))
                except Exception:
                    if not scan_text_parts:
                        scan_text_parts.append(messages_text)
                full_scan_text = "\n".join(scan_text_parts)

                findings = scan_dlp(
                    full_scan_text,
                    categories=dlp_policy.get("categories", ["secrets"]),
                    custom_patterns=dlp_policy.get("custom_patterns"),
                    allowlist=dlp_policy.get("allowlist"),
                )
                mode = dlp_policy.get("mode", "warn")
                dlp_scan_results = {
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                    "findings_count": len(findings),
                    "mode": mode,
                    "finding_types": list({f.pattern_name for f in findings}),
                    "action_taken": mode if findings else "clean",
                    "categories_scanned": dlp_policy.get("categories", ["secrets"]),
                }
                if findings:
                    if mode == "block":
                        raise HTTPException(
                            status_code=403,
                            detail={
                                "error": "dlp_blocked",
                                "message": f"DLP policy blocked sync: {len(findings)} finding(s)",
                                "findings": [
                                    {
                                        "pattern": f.pattern_name,
                                        "category": f.category,
                                        "severity": f.severity,
                                        "line": f.line_number,
                                    }
                                    for f in findings
                                ],
                            },
                        )
                    elif mode == "redact":
                        data = redact_and_repack(data, findings)
                        messages_text = _extract_messages_text(data)

        # Extract git metadata for PR matching
        workspace_data = _extract_workspace_from_archive(data)
        git_remote_normalized = ""
        git_branch = ""
        git_commit = ""
        if workspace_data:
            from sessionfs.server.github_app import normalize_git_remote
            git_remote_normalized = normalize_git_remote(workspace_data.get("git_remote", ""))
            git_branch = workspace_data.get("git_branch", "")
            git_commit = workspace_data.get("git_commit", "")

        # Upload blob to a temporary key first (prevents race where two
        # overlapping pushes corrupt the same blob key). The final key is
        # committed atomically with the DB write in Phase 3.
        import uuid as _uuid
        temp_blob_key = f"sessions/_tmp/{_uuid.uuid4().hex}/{session_id}.tar.gz"
        await blob_store.put(temp_blob_key, data)

        # ── Phase 3: Final DB writes using a fresh session ──
        from sessionfs.server.db.engine import _session_factory as _sf

        async def _phase3_writes() -> Response | SyncPushResponse:
            """Execute final DB writes in a fresh session context."""
            if _sf is not None:
                async with _sf() as db2:
                    return await _do_phase3_writes(db2)
            else:
                from sessionfs.server.db.engine import get_db as _get_db_gen
                async for db2 in _get_db_gen():
                    return await _do_phase3_writes(db2)
            raise RuntimeError("No database session available for Phase 3")

        async def _cleanup_temp_blob():
            """Best-effort cleanup of temp blob on any exit path."""
            try:
                await blob_store.delete(temp_blob_key)
            except Exception:
                pass

        async def _promote_blob():
            """Copy temp blob to final key. Does NOT delete temp — caller
            must call _cleanup_temp_blob() after second commit succeeds."""
            temp_data = await blob_store.get(temp_blob_key)
            if not temp_data:
                raise HTTPException(500, "Blob upload lost during sync — please retry")
            await blob_store.put(key, temp_data)

        async def _do_phase3_writes(db2: AsyncSession) -> Response | SyncPushResponse:
          _row_committed = False
          try:
            if existing is None and not is_undelete:
                # Create: insert with temp blob key first, use PK constraint
                # as the concurrency guard. If another request already created
                # the session, the INSERT fails and we clean up.
                session = Session(
                    id=session_id,
                    user_id=user_id,
                    title=meta["title"],
                    tags=meta["tags"],
                    source_tool=meta["source_tool"],
                    source_tool_version=meta["source_tool_version"],
                    original_session_id=meta["original_session_id"],
                    parent_session_id=meta.get("parent_session_id"),
                    model_provider=meta["model_provider"],
                    model_id=meta["model_id"],
                    message_count=meta["message_count"],
                    turn_count=meta["turn_count"],
                    tool_use_count=meta["tool_use_count"],
                    total_input_tokens=meta["total_input_tokens"],
                    total_output_tokens=meta["total_output_tokens"],
                    duration_ms=meta["duration_ms"],
                    messages_text=messages_text,
                    blob_key=temp_blob_key,  # temp key until commit succeeds
                    blob_size_bytes=len(data),
                    etag=new_etag,
                    created_at=now,
                    updated_at=now,
                    uploaded_at=now,
                    git_remote_normalized=git_remote_normalized,
                    git_branch=git_branch,
                    git_commit=git_commit,
                    rules_source=meta.get("rules_source", "none"),
                    rules_version=meta.get("rules_version"),
                    rules_hash=meta.get("rules_hash"),
                    instruction_artifacts=meta.get("instruction_artifacts", "[]"),
                )
                if dlp_scan_results and hasattr(session, "dlp_scan_results"):
                    session.dlp_scan_results = json.dumps(dlp_scan_results)
                db2.add(session)
                try:
                    await db2.commit()
                except IntegrityError:
                    # PK violation = another request created the session first
                    await _cleanup_temp_blob()
                    raise HTTPException(409, "Session created by another request during upload")
                _row_committed = True
                await db2.refresh(session)

                # Commit succeeded — we own the row. Now promote blob.
                await _promote_blob()
                session.blob_key = key
                await db2.commit()
                # Both commits succeeded — safe to clean up temp blob
                await _cleanup_temp_blob()

                if meta["message_count"] >= 5 and git_remote_normalized:
                    background_tasks.add_task(
                        _auto_extract_knowledge, session.id, data, git_remote_normalized, user_id
                    )

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

            # Un-delete or update existing session (row locked via FOR UPDATE)
            if is_undelete:
                from sessionfs.server.db.models import Handoff, ShareLink
                result2 = await db2.execute(
                    select(Session).where(Session.id == session_id, Session.user_id == user_id).with_for_update()
                )
                sess = result2.scalar_one_or_none()
                if sess is None:
                    raise HTTPException(status_code=404, detail="Session not found")
                # Expire old handoffs and revoke share links
                await db2.execute(
                    update(Handoff)
                    .where(Handoff.session_id == session_id, Handoff.status == "pending")
                    .values(status="expired")
                )
                await db2.execute(
                    update(ShareLink)
                    .where(ShareLink.session_id == session_id, ShareLink.is_revoked == False)  # noqa: E712
                    .values(is_revoked=True)
                )
                sess.is_deleted = False
                sess.deleted_at = None
            else:
                # Re-fetch with FOR UPDATE lock to prevent concurrent updates
                result2 = await db2.execute(
                    select(Session).where(
                        Session.id == session_id,
                        Session.user_id == user_id,
                        Session.is_deleted == False,  # noqa: E712
                    ).with_for_update()
                )
                sess = result2.scalar_one_or_none()
                if sess is None:
                    raise HTTPException(status_code=404, detail="Session not found")

                # Re-check ETag against fresh data
                if_match = request.headers.get("If-Match", "").strip('"')
                if if_match and if_match != sess.etag:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "etag_mismatch",
                            "message": "ETag mismatch — session has been updated",
                            "current_etag": sess.etag,
                        },
                    )

            # Update metadata with temp blob key first, commit under lock,
            # then promote blob after commit succeeds.
            sess.title = meta["title"]
            sess.tags = meta["tags"]
            sess.source_tool = meta["source_tool"]
            sess.source_tool_version = meta["source_tool_version"]
            sess.original_session_id = meta["original_session_id"]
            sess.parent_session_id = meta.get("parent_session_id")
            sess.model_provider = meta["model_provider"]
            sess.model_id = meta["model_id"]
            sess.message_count = meta["message_count"]
            sess.turn_count = meta["turn_count"]
            sess.tool_use_count = meta["tool_use_count"]
            sess.total_input_tokens = meta["total_input_tokens"]
            sess.total_output_tokens = meta["total_output_tokens"]
            sess.duration_ms = meta["duration_ms"]
            sess.messages_text = messages_text
            sess.blob_key = temp_blob_key  # temp key until blob promoted
            sess.blob_size_bytes = len(data)
            sess.etag = new_etag
            sess.updated_at = now
            sess.git_remote_normalized = git_remote_normalized
            sess.git_branch = git_branch
            sess.git_commit = git_commit
            sess.rules_source = meta.get("rules_source", "none")
            sess.rules_version = meta.get("rules_version")
            sess.rules_hash = meta.get("rules_hash")
            sess.instruction_artifacts = meta.get("instruction_artifacts", "[]")
            if dlp_scan_results and hasattr(sess, "dlp_scan_results"):
                sess.dlp_scan_results = json.dumps(dlp_scan_results)
            await db2.commit()
            _row_committed = True

            # Commit succeeded (FOR UPDATE lock released). Now promote blob.
            await _promote_blob()
            sess.blob_key = key
            await db2.commit()
            # Both commits succeeded — safe to clean up temp blob
            await _cleanup_temp_blob()
            await db2.refresh(sess)

            if meta["message_count"] >= 5 and git_remote_normalized:
                background_tasks.add_task(
                    _auto_extract_knowledge, sess.id, data, git_remote_normalized, user_id
                )

            return SyncPushResponse(
                session_id=sess.id,
                etag=sess.etag,
                blob_size_bytes=sess.blob_size_bytes,
                synced_at=now,
            )
          except HTTPException:
            # Only clean up temp blob if the row was NOT yet committed
            # (i.e., validation failed before first commit). After first
            # commit, the temp blob IS the live data — do not delete it.
            if not _row_committed:
                await _cleanup_temp_blob()
            raise
          except Exception:
            if not _row_committed:
                await _cleanup_temp_blob()
            raise

        return await _phase3_writes()
    finally:
        semaphore.release()


async def _auto_extract_knowledge(
    session_id: str, archive_data: bytes, git_remote: str, user_id: str
) -> None:
    """Background task: summarize session and extract knowledge entries."""
    from sessionfs.server.db.engine import get_db as _get_db_gen
    from sessionfs.server.services.summarizer import summarize_session

    try:
        # Extract messages and manifest from archive for summarization
        messages = _extract_messages_list(archive_data)
        manifest = _extract_manifest_dict(archive_data)
        workspace = _extract_workspace_from_archive(archive_data)

        summary = summarize_session(messages, manifest, workspace)

        # Find project by git remote
        async for db in _get_db_gen():
            from sessionfs.server.db.models import Project
            result = await db.execute(
                select(Project).where(Project.git_remote_normalized == git_remote)
            )
            project = result.scalar_one_or_none()
            if not project:
                return

            from sessionfs.server.services.knowledge import extract_knowledge_entries
            await extract_knowledge_entries(session_id, summary, project.id, user_id, db)

            # Auto-narrative: if project has auto_narrative enabled and user has
            # judge settings, run LLM extraction for high-quality knowledge entries.
            if getattr(project, "auto_narrative", False):
                try:
                    from sessionfs.server.db.models import UserJudgeSettings
                    from sessionfs.security.encryption import decrypt_api_key

                    judge_result = await db.execute(
                        select(UserJudgeSettings).where(UserJudgeSettings.user_id == user_id)
                    )
                    judge_settings = judge_result.scalar_one_or_none()
                    if judge_settings:
                        api_key = decrypt_api_key(judge_settings.encrypted_api_key)
                        from sessionfs.server.services.knowledge import extract_knowledge_with_llm
                        await extract_knowledge_with_llm(
                            session_id=session_id,
                            messages=messages,
                            project_id=project.id,
                            user_id=user_id,
                            api_key=api_key,
                            model=judge_settings.model,
                            provider=judge_settings.provider,
                            base_url=judge_settings.base_url,
                            db=db,
                        )
                        logger.info(
                            "LLM knowledge extraction completed for session %s",
                            session_id,
                        )
                except Exception:
                    logger.warning(
                        "LLM knowledge extraction failed for session %s", session_id, exc_info=True
                    )
    except Exception:
        logger.warning("Background knowledge extraction failed for %s", session_id, exc_info=True)


def _extract_messages_list(data: bytes) -> list[dict]:
    """Extract messages from archive as a list of dicts."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("messages.jsonl"):
                    f = tar.extractfile(member)
                    if f:
                        text = f.read().decode("utf-8", errors="replace")
                        return [json.loads(line) for line in text.strip().split("\n") if line.strip()]
    except Exception:
        pass
    return []


def _extract_manifest_dict(data: bytes) -> dict:
    """Extract manifest.json from archive as a dict."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("manifest.json"):
                    f = tar.extractfile(member)
                    if f:
                        return json.loads(f.read())
    except Exception:
        pass
    return {}


@router.get("/{session_id}/sync")
async def sync_pull(
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Pull session data with ETag-based caching."""
    _validate_session_id_or_alias(session_id)
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
    _validate_session_id_or_alias(session_id)
    session = await _get_user_session(db, user.id, session_id)
    blob_store = _get_blob_store(request)
    data = await blob_store.get(session.blob_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Session blob not found")

    content = _get_cached_archive_content(session_id, session.etag, data)
    all_messages = content["messages"]

    # Filter out sidechain and empty messages — they render as blank pages
    messages = [
        m for m in all_messages
        if not m.get("is_sidechain")
        and (m.get("content") or m.get("role") == "tool")
    ]

    # Support order parameter: "newest" reverses so most recent is page 1
    order = request.query_params.get("order", "oldest") if request else "oldest"
    if order == "newest":
        messages = list(reversed(messages))

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
    _validate_session_id_or_alias(session_id)
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
    _validate_session_id_or_alias(session_id)
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
            messages_text = _extract_messages_text(data)

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
            session.messages_text = messages_text
            # Migration 028: backfill rules provenance if present in manifest.
            session.rules_source = meta.get("rules_source", "none")
            session.rules_version = meta.get("rules_version")
            session.rules_hash = meta.get("rules_hash")
            session.instruction_artifacts = meta.get("instruction_artifacts", "[]")

            # Update git metadata for PR matching
            ws = _extract_workspace_from_archive(data)
            if ws:
                from sessionfs.server.github_app import normalize_git_remote
                session.git_remote_normalized = normalize_git_remote(ws.get("git_remote", ""))
                session.git_branch = ws.get("git_branch", "")
                session.git_commit = ws.get("git_commit", "")

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


@router.post("/admin/cleanup")
async def cleanup_expired_sessions(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
):
    """Soft-delete stale free-tier sessions older than 14 days.

    Intended for admin or cron job use.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    blob_store = _get_blob_store(request)

    # Find free-tier users
    free_users_q = select(User.id).where(User.tier == "free")
    free_user_ids = (await db.execute(free_users_q)).scalars().all()

    if not free_user_ids:
        return {"deleted": 0}

    result = await db.execute(
        select(Session).where(
            Session.user_id.in_(free_user_ids),
            Session.updated_at < cutoff,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    sessions = result.scalars().all()

    now = datetime.now(timezone.utc)
    deleted = 0
    for session in sessions:
        # Delete blob from storage
        try:
            await blob_store.delete(session.blob_key)
        except Exception as exc:
            _logger.warning("Failed to delete blob for session %s: %s", session.id, exc)

        session.is_deleted = True
        session.deleted_at = now
        deleted += 1

    await db.commit()
    return {"deleted": deleted}


@router.post("/{session_id}/share", response_model=ShareLinkResponse, status_code=201)
async def create_share_link(
    session_id: str,
    body: CreateShareLinkRequest,
    user: User = Depends(require_verified_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a share link for a session."""
    _validate_session_id_or_alias(session_id)
    session = await _get_user_session(db, user.id, session_id)
    # Use the real session ID for the share link FK
    session_id = session.id

    link_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=body.expires_in_hours)

    password_hash = None
    if body.password:
        salt = secrets.token_hex(16)
        dk = hashlib.pbkdf2_hmac("sha256", body.password.encode(), salt.encode(), 100_000)
        password_hash = f"{salt}${dk.hex()}"

    share_link = ShareLink(
        id=link_id,
        session_id=session_id,
        user_id=user.id,
        token=token,
        expires_at=expires_at,
        password_hash=password_hash,
    )
    db.add(share_link)
    await db.commit()

    return ShareLinkResponse(
        link_id=link_id,
        url=f"{os.environ.get('SFS_API_URL', 'https://api.sessionfs.dev')}/api/v1/sessions/share/{token}",
        expires_at=expires_at,
        has_password=password_hash is not None,
    )


@router.delete("/{session_id}/share/{link_id}", status_code=204)
async def revoke_share_link(
    session_id: str,
    link_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a share link."""
    _validate_session_id_or_alias(session_id)
    session = await _get_user_session(db, user.id, session_id)
    result = await db.execute(
        select(ShareLink).where(
            ShareLink.id == link_id,
            ShareLink.session_id == session.id,
            ShareLink.user_id == user.id,
        )
    )
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="Share link not found")

    link.is_revoked = True
    await db.commit()


async def _access_share_link_impl(
    token: str, password: str | None, db: AsyncSession, request: Request,
) -> Response:
    """Shared implementation for share link access (GET and POST)."""
    result = await db.execute(
        select(ShareLink).where(ShareLink.token == token)
    )
    link = result.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="Share link not found")

    if link.is_revoked:
        raise HTTPException(status_code=410, detail="Share link has been revoked")

    if link.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Share link has expired")

    if link.password_hash is not None:
        if not password:
            raise HTTPException(status_code=401, detail="Password required")
        import hmac
        stored = link.password_hash
        if "$" in stored:
            # PBKDF2 format: salt$hash
            salt, expected_hex = stored.split("$", 1)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
            if not hmac.compare_digest(dk.hex(), expected_hex):
                raise HTTPException(status_code=401, detail="Invalid password")
        else:
            # Legacy SHA-256 format (pre-migration)
            provided = hashlib.sha256(password.encode()).hexdigest()
            if not hmac.compare_digest(stored, provided):
                raise HTTPException(status_code=401, detail="Invalid password")

    # Fetch session and blob
    result = await db.execute(
        select(Session).where(
            Session.id == link.session_id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    blob_store = _get_blob_store(request)
    data = await blob_store.get(session.blob_key)
    if data is None:
        raise HTTPException(status_code=404, detail="Session blob not found")

    return Response(
        content=data,
        media_type="application/gzip",
        headers={"ETag": f'"{session.etag}"'},
    )


class ShareAccessRequest(_BaseModel):
    password: str | None = None


@router.get("/share/{token}")
async def access_share_link_get(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Access a non-password-protected share link (public, no auth)."""
    return await _access_share_link_impl(token, None, db, request)


@router.post("/share/{token}")
async def access_share_link_post(
    token: str,
    body: ShareAccessRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Access a password-protected share link via POST body (public, no auth)."""
    return await _access_share_link_impl(token, body.password, db, request)


_ALIAS_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,99}$")


async def _get_user_session(db: AsyncSession, user_id: str, session_id_or_alias: str) -> Session:
    """Get a session owned by the user by ID or alias, or raise 404."""
    # Try by ID first
    result = await db.execute(
        select(Session).where(
            Session.id == session_id_or_alias,
            Session.user_id == user_id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    session = result.scalar_one_or_none()
    if session:
        return session

    # Try by alias
    result = await db.execute(
        select(Session).where(
            Session.alias == session_id_or_alias,
            Session.user_id == user_id,
            Session.is_deleted == False,  # noqa: E712
        )
    )
    session = result.scalar_one_or_none()
    if session:
        return session

    raise HTTPException(status_code=404, detail="Session not found")
