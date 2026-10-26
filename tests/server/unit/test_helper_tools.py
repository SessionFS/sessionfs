"""Session helper tools (piece 3) unit tests.

Covers the 9 tools in ``docs/design/session-helper.md`` against the real
SQLite schema and a local blob store.  Each test drives ``execute`` with a
``HelperContext`` the way piece 4 will, and pins the security invariants:
ownership / project-access gates, scope as a hard filter, result caps, budget
truncation, DLP redaction, retrieval audit, and malformed-argument handling.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.judge.agent_stream import ToolCall
from sessionfs.retrieval_audit import sanitize_arguments
from sessionfs.server.db.models import (
    KnowledgeEntry,
    OrgMember,
    Organization,
    Project,
    RetrievalAuditContext,
    RetrievalAuditEvent,
    Session,
    User,
)
from sessionfs.server.helper.context import (
    HelperAuditSink,
    HelperContext,
    HelperScope,
    RetrievalBudget,
)
from sessionfs.server.helper.excerpts import relativize_path, touches_path
from sessionfs.server.helper.redaction import redact, union_dlp_policies
from sessionfs.server.helper.tools import execute, tool_specs
from sessionfs.server.services.session_archive import extract_archive

SECRET = "AKIA1234567890ABCDEF"


# ── fixtures / helpers ──


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_user(
    db: AsyncSession, *, email: str, display_name: str = "Member", tier: str = "pro"
) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name=display_name,
        tier=tier,
        email_verified=True,
        created_at=_now(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_org(db: AsyncSession, *, tier: str = "team") -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Org",
        slug=f"slug-{uuid.uuid4().hex[:8]}",
        tier=tier,
    )
    db.add(org)
    await db.commit()
    return org


async def _add_member(
    db: AsyncSession, *, org: Organization, user: User, role: str = "member"
) -> None:
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role=role,
            invited_by=user.id,
            invited_at=_now(),
        )
    )
    await db.commit()


async def _make_project(
    db: AsyncSession,
    *,
    owner: User,
    org: Organization | None = None,
    context_document: str = "# ctx",
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Proj",
        git_remote_normalized=f"github.com/t/{uuid.uuid4().hex[:8]}",
        context_document=context_document,
        owner_id=owner.id,
        org_id=org.id if org is not None else None,
    )
    db.add(project)
    await db.commit()
    return project


def _msg(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _error_message(secret: str = SECRET) -> dict:
    """A tool_result whose ``Error: …`` line is 192 chars before the secret.

    The summarizer's ``_extract_errors`` cuts error lines to 200 chars, so a
    secret past character 192 would leak its first 8 chars pre-fix.  Helper
    redaction must run on the full message list first, so the cut lands on a
    ``[REDACTED:…]`` marker instead.
    """
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "x"}},
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "Error: " + "x" * 185 + secret}]},
        ],
    }


def _archive(
    messages: list[dict],
    *,
    workspace: dict | None = None,
    manifest: dict | None = None,
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = "\n".join(json.dumps(m) for m in messages).encode()
        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        for name, obj in (("workspace.json", workspace), ("manifest.json", manifest)):
            if obj is None:
                continue
            data = json.dumps(obj).encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _raw_archive(lines: list[str], *, workspace: dict | None = None) -> bytes:
    """Build an archive whose ``messages.jsonl`` holds exactly ``lines``.

    Lines are written verbatim (no JSON re-encoding), so tests can inject
    blank, malformed, and non-dict lines to pin the line-index convention.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = "\n".join(lines).encode()
        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        if workspace is not None:
            data = json.dumps(workspace).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _make_session(
    db: AsyncSession,
    blob_store,
    user: User,
    *,
    title: str = "Session",
    source_tool: str = "claude-code",
    project_id: str | None = None,
    messages: list[dict] | None = None,
    workspace: dict | None = None,
    manifest: dict | None = None,
    messages_text: str = "",
) -> Session:
    messages = messages or []
    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    key = f"sessions/{user.id}/{session_id}/s.tar.gz"
    await blob_store.put(key, _archive(messages, workspace=workspace, manifest=manifest))
    session = Session(
        id=session_id,
        user_id=user.id,
        title=title,
        tags="[]",
        source_tool=source_tool,
        blob_key=key,
        blob_size_bytes=0,
        etag="x",
        project_id=project_id,
        message_count=len(messages),
        messages_text=messages_text,
        created_at=_now(),
        updated_at=_now(),
        uploaded_at=_now(),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


def _ctx(
    db: AsyncSession,
    user: User,
    *,
    scope: HelperScope | None = None,
    budget: RetrievalBudget | None = None,
    audit: "RecordingSink | None" = None,
    blob_store=None,
    dlp_policy: dict | None = None,
    org_id: str | None = None,
) -> HelperContext:
    return HelperContext(
        user=user,
        db=db,
        org_id=org_id,
        scope=scope or HelperScope(),
        budget=budget or RetrievalBudget(),
        audit=audit,
        blob_store=blob_store,
        dlp_policy=dlp_policy,
    )


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict, dict]] = []

    async def record(self, tool_name: str, sanitized_args: dict, returned_refs: dict) -> None:
        self.records.append((tool_name, sanitized_args, returned_refs))


class FailingSink:
    """Audit sink that raises, to exercise the fail-closed audit path."""

    async def record(self, tool_name: str, sanitized_args: dict, returned_refs: dict) -> None:
        raise RuntimeError("audit backend down")


class CountingBlobStore:
    """Wraps a blob store and counts ``get`` calls (blob reads)."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.gets = 0

    async def put(self, key: str, data: bytes) -> None:
        await self.inner.put(key, data)

    async def get(self, key: str) -> bytes | None:
        self.gets += 1
        return await self.inner.get(key)

    async def delete(self, key: str) -> None:
        await self.inner.delete(key)

    async def exists(self, key: str) -> bool:
        return await self.inner.exists(key)


async def _run(ctx: HelperContext, name: str, args: dict, *, invalid: bool = False):
    call = ToolCall(id="tc_1", name=name, arguments=args, invalid_arguments=invalid)
    return await execute(ctx, call)


# ── registry shape ──


def test_tool_specs_exposes_read_and_proposal_tools():
    specs = tool_specs()
    assert {s.name for s in specs} == {
        "search_sessions",
        "list_recent_sessions",
        "find_related_sessions",
        "get_session_summary",
        "get_session_excerpts",
        "search_project_knowledge",
        "get_project_context",
        "summarize_sessions",
        "sessions_touching_path",
        "list_projects",
        "get_project_settings",
        "set_project_auto_narrative",
        "search_sessionfs_docs",
        "get_sessionfs_doc",
        "trash_sessions",
    }
    for s in specs:
        assert isinstance(s.input_schema, dict)
        assert s.input_schema.get("type") == "object"


# ── search_sessions ──


@pytest.mark.asyncio
async def test_search_sessions_happy_path(db_session, test_user, blob_store):
    await _make_session(
        db_session, blob_store, test_user, title="fix the SSO redirect", messages_text="we fixed the SSO redirect today"
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "search_sessions", {"query": "SSO"})
    assert not result.is_error
    assert "[session:" in result.content
    assert "SSO" in result.content


@pytest.mark.asyncio
async def test_search_sessions_caps_results(db_session, test_user, blob_store):
    for i in range(25):
        await _make_session(
            db_session, blob_store, test_user,
            title=f"SSO work {i}", messages_text="sso",
        )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "search_sessions", {"query": "sso"})
    lines = [ln for ln in result.content.splitlines() if ln.startswith("[session:")]
    assert len(lines) <= 20


@pytest.mark.asyncio
async def test_search_sessions_scope_project_not_widened(db_session, test_user, blob_store):
    pa = await _make_project(db_session, owner=test_user)
    pb = await _make_project(db_session, owner=test_user)
    await _make_session(db_session, blob_store, test_user, title="alpha SSO", project_id=pa.id, messages_text="sso")
    await _make_session(db_session, blob_store, test_user, title="beta SSO", project_id=pb.id, messages_text="sso")

    ctx = _ctx(
        db_session, test_user, blob_store=blob_store,
        scope=HelperScope(project_id=pa.id), audit=RecordingSink(),
    )
    # Model asks for project B; scope (project A) must win.
    result = await _run(ctx, "search_sessions", {"query": "sso", "project_id": pb.id})
    assert "alpha SSO" in result.content
    assert "beta SSO" not in result.content


@pytest.mark.asyncio
async def test_search_sessions_scope_session_ids_hard_filter(db_session, test_user, blob_store):
    s1 = await _make_session(db_session, blob_store, test_user, title="one SSO", messages_text="sso")
    await _make_session(db_session, blob_store, test_user, title="two SSO", messages_text="sso")

    ctx = _ctx(
        db_session, test_user, blob_store=blob_store,
        scope=HelperScope(session_ids=(s1.id,)), audit=RecordingSink(),
    )
    result = await _run(ctx, "search_sessions", {"query": "sso"})
    assert "one SSO" in result.content
    assert "two SSO" not in result.content


# ── list_recent_sessions ──


@pytest.mark.asyncio
async def test_list_recent_sessions_happy_path(db_session, test_user, blob_store):
    await _make_session(db_session, blob_store, test_user, title="newest")
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "list_recent_sessions", {})
    assert "newest" in result.content
    assert result.content.startswith("[session:")


@pytest.mark.asyncio
async def test_list_recent_sessions_respects_project_filter(db_session, test_user, blob_store):
    pa = await _make_project(db_session, owner=test_user)
    pb = await _make_project(db_session, owner=test_user)
    await _make_session(db_session, blob_store, test_user, title="in A", project_id=pa.id)
    await _make_session(db_session, blob_store, test_user, title="in B", project_id=pb.id)

    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "list_recent_sessions", {"project_id": pa.id})
    assert "in A" in result.content
    assert "in B" not in result.content


# ── find_related_sessions ──


@pytest.mark.asyncio
async def test_find_related_sessions_uses_file_path(db_session, test_user, blob_store):
    await _make_session(db_session, blob_store, test_user, title="auth fix", messages_text="src/auth/login.ts fixed")
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "find_related_sessions", {"file_path": "src/auth/login.ts"})
    assert not result.is_error
    assert "auth fix" in result.content


@pytest.mark.asyncio
async def test_find_related_sessions_requires_input(db_session, test_user, blob_store):
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "find_related_sessions", {})
    assert result.is_error


# ── get_session_summary ──


@pytest.mark.asyncio
async def test_get_session_summary_happy_path(db_session, test_user, blob_store):
    session = await _make_session(
        db_session, blob_store, test_user, title="Build auth",
        messages=[_msg("user", "implement login"), _msg("assistant", "done")],
        manifest={"session_id": "x", "title": "Build auth", "source_tool": "claude-code", "model": {"model_id": "claude-opus"}},
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_session_summary", {"session_id": session.id})
    assert not result.is_error
    assert f"[session:{session.id}]" in result.content
    assert "messages: 2" in result.content


@pytest.mark.asyncio
async def test_get_session_summary_cross_user_denied(db_session, test_user, blob_store):
    other = await _make_user(db_session, email="other@example.com")
    session = await _make_session(db_session, blob_store, other, title="other session")
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_session_summary", {"session_id": session.id})
    assert not result.is_error  # reported as not-found, not a crash
    assert result.content == "Session not found."


@pytest.mark.asyncio
async def test_get_session_summary_redacts_before_error_truncation(db_session, test_user, blob_store):
    session = await _make_session(db_session, blob_store, test_user, messages=[_error_message()])
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_session_summary", {"session_id": session.id})
    assert not result.is_error
    assert "errors:" in result.content  # the error line really was extracted
    assert SECRET not in result.content
    assert "AKIA" not in result.content  # no partial-secret leak from the [:200] cut


@pytest.mark.asyncio
async def test_get_session_summary_offloads_redaction_and_summarize(
    db_session, test_user, blob_store, monkeypatch
):
    session = await _make_session(db_session, blob_store, test_user, messages=[_msg("user", "hello")])
    offloaded: list[str] = []
    real_to_thread = asyncio.to_thread

    async def fake_to_thread(fn, *args, **kwargs):
        offloaded.append(getattr(fn, "__name__", repr(fn)))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr("sessionfs.server.helper.tools.asyncio.to_thread", fake_to_thread)
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    await _run(ctx, "get_session_summary", {"session_id": session.id})
    # _redact_messages + summarize_session run together off the event loop.
    assert "_summarize_redacted" in offloaded


@pytest.mark.asyncio
async def test_get_session_summary_caps_to_5000_messages(db_session, test_user, blob_store):
    messages = [_msg("user", f"m{i}") for i in range(6000)]
    session = await _make_session(db_session, blob_store, test_user, messages=messages)
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_session_summary", {"session_id": session.id})
    assert not result.is_error
    assert "messages: 5000" in result.content


@pytest.mark.asyncio
async def test_summarize_sessions_redacts_before_error_truncation(db_session, test_user, blob_store):
    session = await _make_session(db_session, blob_store, test_user, messages=[_error_message()])
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "summarize_sessions", {"session_ids": [session.id]})
    assert not result.is_error
    assert SECRET not in result.content
    assert "AKIA" not in result.content  # no partial-secret leak from the [:200] cut


# ── get_session_excerpts ──


@pytest.mark.asyncio
async def test_get_session_excerpts_ranks_and_indexes(db_session, test_user, blob_store):
    session = await _make_session(
        db_session, blob_store, test_user,
        messages=[
            _msg("user", "what about the redis cache?"),
            _msg("assistant", "the redis cache uses ttl 60"),
            _msg("user", "unrelated note"),
        ],
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_session_excerpts", {"session_id": session.id, "query": "redis"})
    assert not result.is_error
    assert "redis" in result.content
    assert f"[session:{session.id} msg:" in result.content


@pytest.mark.asyncio
async def test_get_session_excerpts_redacts_secrets(db_session, test_user, blob_store):
    session = await _make_session(
        db_session, blob_store, test_user,
        messages=[_msg("assistant", f"the key is {SECRET}")],
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_session_excerpts", {"session_id": session.id})
    assert SECRET not in result.content
    assert "[REDACTED:" in result.content


# ── search_project_knowledge ──


@pytest.mark.asyncio
async def test_search_project_knowledge_happy_path(db_session, test_user, blob_store):
    project = await _make_project(db_session, owner=test_user)
    db_session.add(
        KnowledgeEntry(
            project_id=project.id,
            session_id=f"ses_{uuid.uuid4().hex[:12]}",
            user_id=test_user.id,
            entry_type="decision",
            content="the SSO redirect fix",
            confidence=0.9,
            claim_class="claim",
            created_at=_now(),
        )
    )
    await db_session.commit()

    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "search_project_knowledge", {"project_id": project.id, "query": "SSO"})
    assert not result.is_error
    assert "SSO" in result.content
    assert "[kb:" in result.content


@pytest.mark.asyncio
async def test_search_project_knowledge_escapes_like_wildcards(
    db_session, test_user, blob_store
):
    """A literal ``%`` query must match only entries containing a ``%``, not
    every entry (the pre-fix ``ilike(f"%{query}%")`` turned it into a
    match-everything wildcard)."""
    project = await _make_project(db_session, owner=test_user)
    for content in ("has a % sign", "no percent here"):
        db_session.add(
            KnowledgeEntry(
                project_id=project.id,
                session_id=f"ses_{uuid.uuid4().hex[:12]}",
                user_id=test_user.id,
                entry_type="decision",
                content=content,
                confidence=0.9,
                claim_class="claim",
                created_at=_now(),
            )
        )
    await db_session.commit()

    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(
        ctx, "search_project_knowledge", {"project_id": project.id, "query": "%"}
    )
    assert "has a % sign" in result.content
    assert "no percent here" not in result.content


@pytest.mark.asyncio
async def test_search_project_knowledge_cross_org_denied(db_session, test_user, blob_store):
    owner = await _make_user(db_session, email="owner@example.com")
    org_b = await _make_org(db_session)
    project_b = await _make_project(db_session, owner=owner, org=org_b)
    # test_user is not a member of org_b.
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "search_project_knowledge", {"project_id": project_b.id, "query": "anything"})
    assert not result.is_error
    assert result.content == "Project not found."


@pytest.mark.asyncio
async def test_org_admin_sees_exactly_project_access_allows(db_session, test_user, blob_store):
    # test_user is an org admin: KB read is allowed (project_access), but
    # session ownership still gates session tools (no admin super-view).
    owner = await _make_user(db_session, email="owner@example.com")
    org = await _make_org(db_session)
    await _add_member(db_session, org=org, user=test_user, role="admin")
    project = await _make_project(db_session, owner=owner, org=org)

    other = await _make_user(db_session, email="other@example.com")
    other_session = await _make_session(db_session, blob_store, other, project_id=project.id, title="other's")

    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())

    kb = await _run(ctx, "search_project_knowledge", {"project_id": project.id, "query": "x"})
    assert kb.content == "No knowledge entries matched."  # allowed to read, sees the (empty) KB

    summary = await _run(ctx, "get_session_summary", {"session_id": other_session.id})
    assert summary.content == "Session not found."  # not the admin's session


# ── get_project_context ──


@pytest.mark.asyncio
async def test_get_project_context_returns_document(db_session, test_user, blob_store):
    project = await _make_project(db_session, owner=test_user, context_document="# Architecture\ntext")
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_project_context", {"project_id": project.id})
    assert not result.is_error
    assert "Architecture" in result.content


@pytest.mark.asyncio
async def test_get_project_context_denied_for_stranger(db_session, test_user, blob_store):
    owner = await _make_user(db_session, email="owner@example.com")
    project = await _make_project(db_session, owner=owner)
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_project_context", {"project_id": project.id})
    assert result.content == "Project not found."


# ── summarize_sessions ──


@pytest.mark.asyncio
async def test_summarize_sessions_packs_multiple(db_session, test_user, blob_store):
    s1 = await _make_session(db_session, blob_store, test_user, title="one", messages=[_msg("user", "hello one")])
    s2 = await _make_session(db_session, blob_store, test_user, title="two", messages=[_msg("user", "hello two")])
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "summarize_sessions", {"session_ids": [s1.id, s2.id]})
    assert not result.is_error
    assert f"[session:{s1.id}]" in result.content
    assert f"[session:{s2.id}]" in result.content


@pytest.mark.asyncio
async def test_summarize_sessions_caps_to_five(db_session, test_user, blob_store):
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "summarize_sessions", {"session_ids": ["a", "b", "c", "d", "e", "f"]})
    assert result.is_error
    assert "at most 5" in result.content


# ── sessions_touching_path ──


@pytest.mark.asyncio
async def test_sessions_touching_path(db_session, test_user, blob_store):
    await _make_session(
        db_session, blob_store, test_user, title="auth work",
        messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "edit", "input": {"file_path": "src/auth/login.ts"}},
            ]},
        ],
    )
    await _make_session(
        db_session, blob_store, test_user, title="billing work",
        messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "edit", "input": {"file_path": "src/billing/invoice.ts"}},
            ]},
        ],
    )

    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "sessions_touching_path", {"path": "src/auth"})
    assert "auth work" in result.content
    assert "billing work" not in result.content


@pytest.mark.asyncio
async def test_sessions_touching_path_workspace_files(db_session, test_user, blob_store):
    await _make_session(
        db_session, blob_store, test_user, title="repo work",
        messages=[],
        workspace={"files": [{"path": "src/auth/login.ts"}]},
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "sessions_touching_path", {"path": "src/auth"})
    assert "repo work" in result.content


# ── docs search ──


@pytest.mark.asyncio
async def test_search_sessionfs_docs_returns_markers_and_records_refs(db_session, test_user):
    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, audit=sink)
    result = await _run(ctx, "search_sessionfs_docs", {"query": "jira", "limit": 2})
    assert not result.is_error
    assert "[doc:integrations/jira" in result.content
    assert len(sink.records) == 1
    _, args, refs = sink.records[0]
    assert args["query"] == "jira"
    assert "integrations/jira" in refs["doc_slugs"]


@pytest.mark.asyncio
async def test_search_sessionfs_docs_caps_a_huge_query(db_session, test_user, monkeypatch):
    """The tool passes at most 500 characters / 32 distinct words to search."""
    from sessionfs.server.helper import docs as docs_mod
    from sessionfs.server.helper import tools as tools_mod

    seen: list[str] = []
    real = docs_mod.get_docs_index()

    class _Spy:
        def search(self, query, limit):
            seen.append(query)
            return real.search(query, limit)

    monkeypatch.setattr(tools_mod, "get_docs_index", lambda: _Spy())
    ctx = _ctx(db_session, test_user)
    query = "jira " + " ".join(f"word{i}" for i in range(20_000))
    result = await _run(ctx, "search_sessionfs_docs", {"query": query})
    assert not result.is_error
    assert "[doc:integrations/jira" in result.content
    assert len(seen[0]) <= 500
    assert len(set(seen[0].split())) <= 32


@pytest.mark.asyncio
async def test_get_sessionfs_doc_returns_page_and_suggests_unknown(db_session, test_user):
    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, audit=sink)
    result = await _run(ctx, "get_sessionfs_doc", {"slug": "quickstart"})
    assert not result.is_error
    assert "[doc:quickstart]" in result.content
    assert "https://sessionfs.dev/quickstart/" in result.content
    assert sink.records[-1][2]["doc_slugs"] == ["quickstart"]

    not_found = await _run(ctx, "get_sessionfs_doc", {"slug": "jirra"})
    assert not not_found.is_error
    assert "not found" in not_found.content.lower()
    assert "integrations/jira" in not_found.content


# ── budget / audit / malformed args ──


@pytest.mark.asyncio
async def test_budget_truncation_note(db_session, test_user, blob_store):
    session = await _make_session(
        db_session, blob_store, test_user, messages=[_msg("assistant", "y" * 4000)]
    )
    ctx = _ctx(
        db_session, test_user, blob_store=blob_store,
        budget=RetrievalBudget(token_limit=20), audit=RecordingSink(),
    )
    result = await _run(ctx, "get_session_excerpts", {"session_id": session.id})
    assert "per-turn retrieval budget" in result.content


@pytest.mark.asyncio
async def test_audit_event_sanitizes_arguments(db_session, test_user, blob_store):
    await _make_session(db_session, blob_store, test_user, title="sso", messages_text="sso")
    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=sink)
    await _run(ctx, "search_sessions", {"query": "sso", "api_key": "super-secret", "git_remote": "x"})
    assert len(sink.records) == 1
    tool_name, args, refs = sink.records[0]
    assert tool_name == "search_sessions"
    assert "api_key" not in args
    assert "git_remote" not in args
    assert args["query"] == "sso"
    assert "session_ids" in refs or refs.get("session_ids") == []


@pytest.mark.asyncio
async def test_malformed_args_is_error(db_session, test_user, blob_store):
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "search_sessions", {})
    assert result.is_error
    assert "query" in result.content


@pytest.mark.asyncio
async def test_invalid_arguments_flag_is_error(db_session, test_user, blob_store):
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "search_sessions", {}, invalid=True)
    assert result.is_error


@pytest.mark.asyncio
async def test_unknown_tool_is_error(db_session, test_user, blob_store):
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "not_a_tool", {})
    assert result.is_error


@pytest.mark.asyncio
async def test_audit_records_one_event_per_call(db_session, test_user, blob_store):
    await _make_session(db_session, blob_store, test_user, title="sso", messages_text="sso")
    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=sink)
    await _run(ctx, "search_sessions", {"query": "sso"})
    await _run(ctx, "search_sessions", {})  # error call still records
    assert len(sink.records) == 2
    # The error call recorded an event with empty refs.
    assert sink.records[1][0] == "search_sessions"


# ── finding 1: cost bound for sessions_touching_path ──


@pytest.mark.asyncio
async def test_budget_exhausted_does_no_work(db_session, test_user, blob_store):
    store = CountingBlobStore(blob_store)
    await _make_session(db_session, store, test_user, title="path work", messages=[_msg("assistant", "x")])
    ctx = _ctx(
        db_session, test_user, blob_store=store,
        budget=RetrievalBudget(token_limit=0), audit=RecordingSink(),
    )
    result = await _run(ctx, "sessions_touching_path", {"path": "src"})
    assert result.is_error
    assert "Retrieval budget exhausted" in result.content
    assert store.gets == 0


@pytest.mark.asyncio
async def test_sessions_touching_path_finds_tool_use_path_with_unrelated_text(db_session, test_user, blob_store):
    # The indexed text mentions nothing about the path (it lives only in a
    # tool_use file_path input, which messages_text never indexes), so a
    # prefilter that excluded on text would miss this session entirely.
    await _make_session(
        db_session, blob_store, test_user, title="auth work",
        messages_text="unrelated chat about the weather",
        messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "edit", "input": {"file_path": "src/auth/login.ts"}},
            ]},
        ],
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "sessions_touching_path", {"path": "src/auth"})
    assert "auth work" in result.content


@pytest.mark.asyncio
async def test_sessions_touching_path_caps_candidates(db_session, test_user, blob_store):
    store = CountingBlobStore(blob_store)
    # Empty messages_text can't be ruled out, so each candidate is fetched —
    # but the query is capped at the 200 most recent sessions.
    for i in range(300):
        await _make_session(db_session, store, test_user, title=f"cap {i}", messages_text="")
    ctx = _ctx(db_session, test_user, blob_store=store, audit=RecordingSink())
    await _run(ctx, "sessions_touching_path", {"path": "src/auth"})
    assert store.gets <= 200


@pytest.mark.asyncio
async def test_sessions_touching_path_capped_scan_notes_incomplete(db_session, test_user, blob_store):
    store = CountingBlobStore(blob_store)
    # One more session than the 200-candidate cap: the scan stops early and
    # must say so rather than silently presenting a partial result.
    for i in range(201):
        await _make_session(db_session, store, test_user, title=f"cap {i}", messages_text="")
    ctx = _ctx(db_session, test_user, blob_store=store, audit=RecordingSink())
    result = await _run(ctx, "sessions_touching_path", {"path": "src/auth"})
    assert "results may be incomplete" in result.content


@pytest.mark.asyncio
async def test_sessions_touching_path_checked_count_excludes_unexamined(
    db_session, test_user, blob_store, monkeypatch
):
    # With the byte cap lowered to 1, the first session is examined and the
    # second trips the cap *before* being examined — "checked" must not count
    # that never-examined session.
    from sessionfs.server.helper import tools as tools_mod

    monkeypatch.setattr(tools_mod, "MAX_PATH_BLOB_BYTES", 1)
    await _make_session(db_session, blob_store, test_user, title="s1", messages=[_msg("assistant", "x" * 200)])
    await _make_session(db_session, blob_store, test_user, title="s2", messages=[_msg("assistant", "y" * 200)])

    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "sessions_touching_path", {"path": "src/auth"})
    assert "[checked 1 of 2 sessions" in result.content


# ── finding 2: redact full text before truncation / snippet windows ──


@pytest.mark.asyncio
async def test_get_session_excerpts_redacts_before_truncation(db_session, test_user, blob_store):
    # The secret straddles the truncation boundary: redacting the full text
    # first means truncation can never cut a secret in half.
    session = await _make_session(
        db_session, blob_store, test_user,
        messages=[_msg("assistant", "a" * 108 + SECRET + "y" * 50)],
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_session_excerpts", {"session_id": session.id, "max_tokens": 30})
    assert not result.is_error
    assert SECRET not in result.content
    assert "AKIA" not in result.content  # no partial-secret leak
    assert "[REDACTED:" in result.content


@pytest.mark.asyncio
async def test_search_sessions_snippet_redacts_secret(db_session, test_user, blob_store):
    await _make_session(db_session, blob_store, test_user, title="deploy", messages_text=f"we set the key {SECRET} on deploy")
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "search_sessions", {"query": "deploy"})
    assert not result.is_error
    assert SECRET not in result.content
    assert "[REDACTED:" in result.content


# ── finding 3: secrets are always scanned ──


def test_redact_always_applies_secrets_regardless_of_org_categories():
    out = redact(f"token {SECRET} for the PHI record", {"mode": "redact", "categories": ["phi"]})
    assert SECRET not in out
    assert "[REDACTED:" in out


def test_union_dlp_policies_relabels_foreign_custom_pattern_names():
    """A scope org's own custom pattern keeps its name; every other org's
    pattern is relabelled ``custom`` so a private name never leaks through a
    redaction marker (N2)."""
    scope_pattern = {"name": "scope_code", "regex": r"SCOPE-\d+", "category": "secret", "severity": "high"}
    foreign_pattern = {"name": "other_code", "regex": r"OTHER-\d+", "category": "secret", "severity": "high"}

    union = union_dlp_policies(
        [
            {"mode": "redact", "custom_patterns": [scope_pattern]},
            {"mode": "redact", "custom_patterns": [foreign_pattern]},
        ],
        scope_pattern_names={"scope_code"},
    )
    assert [p["name"] for p in union["custom_patterns"]] == ["scope_code", "custom"]

    # No scope org (personal scope): every custom pattern is relabelled.
    union_no_scope = union_dlp_policies(
        [{"mode": "redact", "custom_patterns": [foreign_pattern]}],
        scope_pattern_names=set(),
    )
    assert [p["name"] for p in union_no_scope["custom_patterns"]] == ["custom"]


def test_union_dlp_policies_relabels_markers_end_to_end():
    """A foreign pattern still redacts, but its marker reads ``custom`` — the
    org-private name never appears in the redacted text (N2)."""
    foreign = {"name": "org_private_name", "regex": r"PROJ-\d+", "category": "secret", "severity": "high"}
    union = union_dlp_policies(
        [{"mode": "redact", "custom_patterns": [foreign]}],
        scope_pattern_names=set(),
    )
    out = redact("see PROJ-123 for details", union)
    assert "PROJ-123" not in out
    assert "[REDACTED:custom]" in out
    assert "org_private_name" not in out


# ── finding 4: fail-closed retrieval audit ──


@pytest.mark.asyncio
async def test_audit_write_failure_fails_closed(db_session, test_user, blob_store):
    await _make_session(db_session, blob_store, test_user, title="sso", messages_text="sso")
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=FailingSink())
    result = await _run(ctx, "search_sessions", {"query": "sso"})
    assert result.is_error
    assert "could not record retrieval audit" in result.content
    assert "[session:" not in result.content


@pytest.mark.asyncio
async def test_audit_sink_failure_does_not_poison_session(db_session, test_user):
    # A genuine flush failure inside the sink's savepoint must not leave the
    # shared session in a "pending rollback" state for the next call.
    sink = HelperAuditSink(db=db_session, context_id=None, user_id=test_user.id)
    with pytest.raises(IntegrityError):
        await sink.record("search_sessions", {"query": "sso"}, {})
    db_session.add(RetrievalAuditContext(id="ra_after", project_id=None))
    await db_session.commit()
    assert (await db_session.get(RetrievalAuditContext, "ra_after")) is not None


@pytest.mark.asyncio
async def test_audit_sink_writes_event_with_nullable_project_id(db_session, test_user):
    db_session.add(RetrievalAuditContext(id="ra_ctx", project_id=None))
    await db_session.commit()
    sink = HelperAuditSink(db=db_session, context_id="ra_ctx", user_id=test_user.id)
    await sink.record("search_sessions", {"query": "sso"}, {"session_ids": ["s1"]})
    await db_session.commit()
    event = (await db_session.execute(select(RetrievalAuditEvent))).scalar_one()
    assert event.project_id is None
    assert event.source == "helper"


# ── finding 5: query-term cost cap ──


def test_query_terms_caps_count_and_drops_short():
    from sessionfs.server.helper.excerpts import _query_terms

    terms = _query_terms(" ".join(f"term{i}" for i in range(100)))
    assert len(terms) == 32
    assert "a" not in _query_terms("a bb ccc")


# ── finding 6: redaction errors fail closed ──


@pytest.mark.asyncio
async def test_non_string_custom_regex_is_skipped_not_fatal(db_session, test_user, blob_store):
    # A custom pattern with a non-string regex (regex=None) is skipped at scan
    # time (finding 3), and builtin secret redaction still runs — redaction
    # must not fail closed on a malformed stored pattern.
    session = await _make_session(
        db_session, blob_store, test_user, messages=[_msg("assistant", f"token {SECRET} in text")]
    )
    policy = {
        "mode": "redact",
        "categories": ["secrets"],
        "custom_patterns": [{"name": "bad", "regex": None, "category": "secret", "severity": "high"}],
    }
    ctx = _ctx(db_session, test_user, blob_store=blob_store, dlp_policy=policy, audit=RecordingSink())
    result = await _run(ctx, "get_session_excerpts", {"session_id": session.id})
    assert not result.is_error
    assert SECRET not in result.content
    assert "[REDACTED:" in result.content


@pytest.mark.asyncio
async def test_invalid_stored_custom_regex_is_skipped_not_fatal(db_session, test_user, blob_store):
    # A stored policy with an uncompilable regex string must not fail the
    # whole redaction: the bad pattern is skipped and builtin secret
    # redaction still runs.
    session = await _make_session(
        db_session, blob_store, test_user, messages=[_msg("assistant", f"token {SECRET} in text")]
    )
    policy = {
        "mode": "redact",
        "categories": ["secrets"],
        "custom_patterns": [{"name": "bad", "regex": "(", "category": "secret", "severity": "high"}],
    }
    ctx = _ctx(db_session, test_user, blob_store=blob_store, dlp_policy=policy, audit=RecordingSink())
    result = await _run(ctx, "get_session_excerpts", {"session_id": session.id})
    assert not result.is_error
    assert SECRET not in result.content
    assert "[REDACTED:" in result.content


# ── findings 7 & 8: archive line handling + index convention ──


def test_extract_archive_keeps_only_dict_lines_and_counts_indexes():
    archive = _raw_archive([
        '{"role": "user", "content": "first"}',
        "",
        '"just a string"',
        '{"role": "assistant", "content": "second"}',
        "[1, 2, 3]",
        '{"broken": ',
    ])
    data = extract_archive(archive)
    assert [m.get("content") for m in data["messages"]] == ["first", "second"]
    # 0-based line numbers counting every line, blank and malformed included.
    assert data["message_indexes"] == [0, 3]


@pytest.mark.asyncio
async def test_get_session_excerpts_uses_raw_line_index(db_session, test_user, blob_store):
    archive = _raw_archive([
        '{"role": "user", "content": "first"}',
        "",
        '"not a dict"',
        '{"role": "assistant", "content": "redis cache uses ttl"}',
    ])
    session = await _make_session(db_session, blob_store, test_user, messages=[], messages_text="")
    await blob_store.put(session.blob_key, archive)
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_session_excerpts", {"session_id": session.id, "query": "redis"})
    assert not result.is_error
    assert f"[session:{session.id} msg:3]" in result.content


# ── finding 9: segment-based path prefix matching ──


def test_touches_path_is_segment_prefix_match():
    assert touches_path(["src/auth/login.ts"], "src/auth") is True
    assert touches_path(["./src/auth/login.ts"], "src/auth") is True
    assert touches_path(["lib/src/authz/index.ts"], "src/auth") is False
    assert touches_path(["src/authz.ts"], "src/auth") is False


def test_relativize_path_makes_absolute_paths_relative():
    assert relativize_path("/repo/src/auth/login.ts", "/repo") == "src/auth/login.ts"
    assert relativize_path("src/auth/login.ts", "/repo") == "src/auth/login.ts"
    assert relativize_path("/other/src/auth", "/repo") == "/other/src/auth"


@pytest.mark.asyncio
async def test_sessions_touching_path_relativizes_workspace_root(db_session, test_user, blob_store):
    await _make_session(
        db_session, blob_store, test_user, title="abs work",
        messages=[], workspace={"root_path": "/repo", "files": [{"path": "/repo/src/auth/login.ts"}]},
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "sessions_touching_path", {"path": "src/auth"})
    assert "abs work" in result.content


@pytest.mark.asyncio
async def test_sessions_touching_path_relativizes_tool_call_paths(db_session, test_user, blob_store):
    # Real captured shape: tool-call file_path is absolute under the workspace
    # root, which the index never records — so this must match via the
    # relativized form.
    await _make_session(
        db_session, blob_store, test_user, title="abs tool path",
        workspace={"root_path": "/Users/x/repo"},
        messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "edit", "input": {"file_path": "/Users/x/repo/src/auth/login.py"}},
            ]},
        ],
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "sessions_touching_path", {"path": "src/auth"})
    assert "abs tool path" in result.content


@pytest.mark.asyncio
async def test_sessions_touching_path_absolute_query_matches_raw_path(db_session, test_user, blob_store):
    # An absolute query must still match the raw absolute file_path.
    await _make_session(
        db_session, blob_store, test_user, title="abs query",
        workspace={"root_path": "/Users/x/repo"},
        messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "read", "input": {"file_path": "/Users/x/repo/src/auth/login.py"}},
            ]},
        ],
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "sessions_touching_path", {"path": "/Users/x/repo/src/auth"})
    assert "abs query" in result.content


@pytest.mark.asyncio
async def test_sessions_touching_path_path_outside_root(db_session, test_user, blob_store):
    # A tool-call path outside the workspace root is not relativized: a
    # workspace-relative query must not match it, but an absolute one must.
    await _make_session(
        db_session, blob_store, test_user, title="outside root",
        workspace={"root_path": "/Users/x/repo"},
        messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "write", "input": {"file_path": "/tmp/scratch/notes.md"}},
            ]},
        ],
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    assert "outside root" not in (await _run(ctx, "sessions_touching_path", {"path": "src/auth"})).content
    assert "outside root" in (await _run(ctx, "sessions_touching_path", {"path": "/tmp/scratch"})).content


@pytest.mark.asyncio
async def test_get_session_summary_relativizes_tool_call_paths(db_session, test_user, blob_store):
    session = await _make_session(
        db_session, blob_store, test_user,
        workspace={"root_path": "/Users/x/repo"},
        messages=[
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "/Users/x/repo/src/auth/login.py"}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "wrote"},
            ]},
        ],
    )
    ctx = _ctx(db_session, test_user, blob_store=blob_store, audit=RecordingSink())
    result = await _run(ctx, "get_session_summary", {"session_id": session.id})
    assert not result.is_error
    assert "files modified: src/auth/login.py" in result.content
    assert "/Users/x/repo" not in result.content


# ── finding 10: audit arg allowlist ──


def test_sanitize_arguments_keeps_known_nonsecret_args():
    args = sanitize_arguments({"max_tokens": 100, "query": "q", "api_key": "x", "token": "y", "git_remote": "z"})
    assert args["max_tokens"] == 100
    assert args["query"] == "q"
    assert "api_key" not in args
    assert "token" not in args
    assert "git_remote" not in args


# ── finding 11: KB tools scoped to scoped-session projects ──


@pytest.mark.asyncio
async def test_kb_tools_limited_to_scoped_session_projects(db_session, test_user, blob_store):
    pa = await _make_project(db_session, owner=test_user, context_document="# A doc")
    pb = await _make_project(db_session, owner=test_user, context_document="# B doc")
    session = await _make_session(db_session, blob_store, test_user, project_id=pa.id, title="in A")

    ctx = _ctx(
        db_session, test_user, blob_store=blob_store,
        scope=HelperScope(session_ids=(session.id,)), audit=RecordingSink(),
    )
    ok = await _run(ctx, "get_project_context", {"project_id": pa.id})
    assert ok.content == "# A doc"
    denied = await _run(ctx, "get_project_context", {"project_id": pb.id})
    assert denied.content == "Project not found."


@pytest.mark.asyncio
async def test_kb_tools_exclude_deleted_scoped_sessions(db_session, test_user, blob_store):
    # A deleted session in scope must not grant KB access to its project:
    # _scoped_session_project_ids filters is_deleted, so the project is out of
    # scope and reported not-found.
    project = await _make_project(db_session, owner=test_user, context_document="# A doc")
    session = await _make_session(db_session, blob_store, test_user, project_id=project.id, title="deleted")
    session.is_deleted = True
    await db_session.commit()

    ctx = _ctx(
        db_session, test_user, blob_store=blob_store,
        scope=HelperScope(session_ids=(session.id,)), audit=RecordingSink(),
    )
    denied = await _run(ctx, "get_project_context", {"project_id": project.id})
    assert denied.content == "Project not found."
