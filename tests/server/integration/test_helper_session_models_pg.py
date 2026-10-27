"""PG-only: the helper's model/provider/date filters on the real dialect.

Runs when ``SFS_TEST_PG_URL`` points at a scratch PostgreSQL database.  The
model filters compile to ``ILIKE … ESCAPE '\\'`` and ``lower(...) IN``, the
date filters to timestamptz comparisons, ``list_session_models`` to
min/max aggregates, and a text query to the ``search_vector`` full-text
path — all PostgreSQL-specific SQL that the SQLite suite cannot prove.  The
schema is built by the real migration chain (``search_vector`` is a
migration-only generated column).
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.judge.agent_stream import ToolCall
from sessionfs.server.db.models import Project, Session, User
from sessionfs.server.helper.context import HelperContext, HelperScope, RetrievalBudget
from sessionfs.server.helper.tools import MAX_ACTION_TARGETS, execute

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema(_pg_schema_isolation):
    from tests.pg_helpers import upgrade_pg_to_head

    upgrade_pg_to_head()
    yield


@pytest.fixture
async def pg_db():
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            yield db
    finally:
        await engine.dispose()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _user(db) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        display_name="PG",
        tier="enterprise",
        email_verified=True,
        created_at=_now(),
    )
    db.add(user)
    await db.commit()
    return user


async def _project(db, owner: User) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="P",
        git_remote_normalized=f"github.com/pg/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    return project


def _session(
    user: User,
    model_id: str | None,
    *,
    provider: str | None = None,
    age_minutes: int = 0,
    project_id: str | None = None,
    text: str = "",
) -> Session:
    when = _now() - timedelta(minutes=age_minutes)
    return Session(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=user.id,
        title=f"work on {model_id}",
        source_tool="claude-code",
        model_id=model_id,
        model_provider=provider,
        blob_key=f"blobs/{uuid.uuid4().hex}",
        blob_size_bytes=0,
        etag="x",
        project_id=project_id,
        messages_text=text,
        created_at=when,
        updated_at=when,
        uploaded_at=when,
    )


def _ctx(db, user: User, scope: HelperScope | None = None) -> HelperContext:
    return HelperContext(
        user=user,
        db=db,
        org_id=None,
        scope=scope or HelperScope(),
        budget=RetrievalBudget(),
        audit=None,
    )


async def _run(ctx: HelperContext, name: str, args: dict):
    return await execute(ctx, ToolCall(id=f"tc_{uuid.uuid4().hex[:8]}", name=name, arguments=args))


def _ids(text: str) -> list[str]:
    return re.findall(r"^\[session:([A-Za-z0-9_-]+)\]", text, flags=re.M)


async def test_model_filters_on_postgres(pg_db):
    user = await _user(pg_db)
    other = await _user(pg_db)
    project = await _project(pg_db, user)
    rows = {
        "pro": _session(user, "deepseek-v4-pro", provider="DeepSeek", age_minutes=1, project_id=project.id),
        "flash": _session(user, "DeepSeek-Flash", provider="openrouter", age_minutes=2),
        "claude": _session(user, "claude-opus-4-5", provider="anthropic", age_minutes=3, text="mentions deepseek-v4-pro and the login bug"),
        "none": _session(user, None, age_minutes=4),
        "synthetic": _session(user, "<synthetic>", provider="Unknown", age_minutes=5),
        "underscore": _session(user, "local_model-7b", age_minutes=6),
        "lookalike": _session(user, "localXmodel-7b", age_minutes=7),
        "old": _session(user, "deepseek-v4-pro", age_minutes=60 * 24 * 40, text="login bug"),
        "theirs": _session(other, "deepseek-v4-pro"),
    }
    pg_db.add_all(rows.values())
    await pg_db.commit()
    ctx = _ctx(pg_db, user)

    fam = await _run(ctx, "list_recent_sessions", {"model": "DEEPSEEK"})
    assert set(_ids(fam.content)) == {rows["pro"].id, rows["flash"].id, rows["old"].id}

    unknown = await _run(ctx, "list_recent_sessions", {"model": "unknown"})
    assert set(_ids(unknown.content)) == {rows["none"].id, rows["synthetic"].id}

    prov = await _run(ctx, "list_recent_sessions", {"provider": ["deepseek", "OPENROUTER"]})
    assert set(_ids(prov.content)) == {rows["pro"].id, rows["flash"].id}
    # lower(model_provider) folds a stored "Unknown" into "no provider".
    no_prov = await _run(ctx, "list_recent_sessions", {"provider": "unknown"})
    assert rows["synthetic"].id in _ids(no_prov.content)
    assert rows["pro"].id not in no_prov.content

    for value in ("%", "\\", "%deepseek%"):
        res = await _run(ctx, "list_recent_sessions", {"model": value})
        assert _ids(res.content) == [], value
    lit = await _run(ctx, "list_recent_sessions", {"model": "local_model"})
    assert _ids(lit.content) == [rows["underscore"].id]

    recent = await _run(ctx, "list_recent_sessions", {"model": "deepseek", "days": 7})
    assert set(_ids(recent.content)) == {rows["pro"].id, rows["flash"].id}
    cutoff = (_now() - timedelta(days=20)).strftime("%Y-%m-%d")
    until = await _run(ctx, "list_recent_sessions", {"model": "deepseek", "until": cutoff})
    assert _ids(until.content) == [rows["old"].id]

    # Full-text (search_vector) combined with the model + date filters.
    text_hit = await _run(ctx, "search_sessions", {"query": "login", "model": "deepseek"})
    assert _ids(text_hit.content) == [rows["old"].id]
    text_recent = await _run(ctx, "search_sessions", {"query": "login", "days": 7})
    assert _ids(text_recent.content) == [rows["claude"].id]
    assert "model: claude-opus-4-5" in text_recent.content

    scoped = _ctx(pg_db, user, HelperScope(project_id=project.id))
    in_scope = await _run(scoped, "list_recent_sessions", {"model": "deepseek"})
    assert _ids(in_scope.content) == [rows["pro"].id]
    assert rows["theirs"].id not in fam.content

    models = await _run(ctx, "list_session_models", {})
    assert not models.is_error, models.content
    lines = [ln for ln in models.content.splitlines() if ln.startswith("- ")]
    assert lines[-1].startswith("- unknown (no model recorded) — 2 session(s)")
    pro_line = next(ln for ln in lines if ln.startswith("- deepseek-v4-pro"))
    # Two rows with different providers ("DeepSeek" vs none) stay separate
    # buckets; the dated aggregates come back as real timestamps.
    assert re.search(r"first \d{4}-\d{2}-\d{2}, last \d{4}-\d{2}-\d{2}", pro_line)


async def test_trash_filter_on_postgres(pg_db):
    user = await _user(pg_db)
    made = [_session(user, "deepseek-v4-pro", age_minutes=i) for i in range(MAX_ACTION_TARGETS + 3)]
    pg_db.add_all(made)
    pg_db.add(_session(user, "claude-opus-4-5"))
    await pg_db.commit()

    ctx = _ctx(pg_db, user)
    result = await _run(ctx, "trash_sessions", {"model": "deepseek"})
    assert not result.is_error, result.content
    assert f"{MAX_ACTION_TARGETS + 3} sessions match" in result.content
    [proposal] = ctx.pending_actions
    assert proposal["params"]["session_ids"] == [s.id for s in made[:MAX_ACTION_TARGETS]]
    flags = (
        await pg_db.execute(select(Session.is_deleted).where(Session.user_id == user.id))
    ).scalars().all()
    assert len(flags) == MAX_ACTION_TARGETS + 4 and not any(flags)


async def test_decision_audit_for_missing_project_on_postgres(pg_db):
    """``retrieval_audit_*.project_id`` is a real FK here: an action whose
    stored project no longer exists audits project-less instead of failing."""
    import json

    import sessionfs.server.routes.helper as helper_mod
    from sessionfs.server.db.models import HelperAction, HelperConversation, RetrievalAuditEvent

    user = await _user(pg_db)
    project = await _project(pg_db, user)
    conv = HelperConversation(id=f"hc_{uuid.uuid4().hex[:24]}", user_id=user.id, title="t")
    pg_db.add(conv)
    await pg_db.commit()
    actions = []
    for kind, params in (
        ("create_ticket", {"project_id": "prj_deleted"}),
        ("set_auto_narrative", {"project_ids": ["prj_deleted", project.id], "enabled": True}),
    ):
        action = HelperAction(
            id=f"ha_{uuid.uuid4().hex[:24]}",
            user_id=user.id,
            conversation_id=conv.id,
            kind=kind,
            params=json.dumps(params),
            preview=json.dumps({"summary": "s", "targets": []}),
            status="expired",
            expires_at=_now(),
            created_at=_now(),
        )
        pg_db.add(action)
        actions.append(action)
    await pg_db.commit()

    for action in actions:
        await helper_mod._record_action_audit(pg_db, user.id, action)
    await pg_db.commit()

    events = (
        await pg_db.execute(
            select(RetrievalAuditEvent.arguments, RetrievalAuditEvent.project_id).where(
                RetrievalAuditEvent.caller_user_id == user.id
            )
        )
    ).all()
    by_action: dict[str, list] = {}
    for arguments, project_id in events:
        by_action.setdefault(json.loads(arguments)["action_id"], []).append(project_id)
    assert by_action[actions[0].id] == [None]
    assert by_action[actions[1].id] == [project.id]


async def test_search_paging_breaks_ties_on_postgres(pg_db):
    from sessionfs.server.services.session_search import search_sessions

    user = await _user(pg_db)
    when = _now()
    made = [_session(user, "m", text="tie breaker text") for _ in range(5)]
    for s in made:
        s.updated_at = when
    pg_db.add_all(made)
    await pg_db.commit()
    seen: list[str] = []
    for offset in (0, 2, 4):
        hits, total = await search_sessions(
            pg_db, user_id=user.id, query="breaker", limit=2, offset=offset
        )
        assert total == 5
        seen.extend(h.session_id for h in hits)
    # Equal rank and recency: the id decides, so pages never repeat or skip.
    assert seen == sorted(s.id for s in made)


async def test_provider_unknown_trim_on_postgres(pg_db):
    user = await _user(pg_db)
    padded = _session(user, "m-1", provider=" Unknown ")
    real = _session(user, "m-2", provider=" DeepSeek ")
    pg_db.add_all([padded, real])
    await pg_db.commit()
    ctx = _ctx(pg_db, user)
    unknown = await _run(ctx, "list_recent_sessions", {"provider": "unknown"})
    assert _ids(unknown.content) == [padded.id]
