"""PG-only: the shared session search runs on the real dialect.

``services.session_search.search_sessions`` builds its query with SQLAlchemy
Core (``plainto_tsquery`` / ``ts_headline`` / ``ts_rank`` on PostgreSQL) and
accepts a ``session_clause`` predicate, which the session helper uses to pass
``project_session_clause`` instead of a list of ids.  Runs when
``SFS_TEST_PG_URL`` points at a scratch PostgreSQL database.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.auth.project_access import project_session_clause
from sessionfs.server.db.models import Base, Project, Session, User
from sessionfs.server.services.session_search import search_sessions

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]


async def test_search_sessions_on_postgres_with_session_clause():
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # The generated tsvector column comes from migration 003.
            await conn.execute(
                text(
                    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS search_vector tsvector "
                    "GENERATED ALWAYS AS (to_tsvector('english', coalesce(title, '') "
                    "|| ' ' || coalesce(messages_text, ''))) STORED"
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with factory() as db:
            user = User(
                id=str(uuid.uuid4()),
                email=f"u-{uuid.uuid4().hex[:6]}@example.com",
                tier="enterprise",
                email_verified=True,
                created_at=now,
            )
            db.add(user)
            await db.flush()
            remote = f"github.com/acme/{uuid.uuid4().hex[:6]}"
            pa = Project(id=f"proj_{uuid.uuid4().hex[:16]}", name="A", owner_id=user.id,
                         git_remote_normalized=remote)
            pb = Project(id=f"proj_{uuid.uuid4().hex[:16]}", name="B", owner_id=user.id)
            db.add_all([pa, pb])
            await db.flush()

            def _session(title: str, project_id: str | None, tool: str = "claude-code",
                         updated: datetime = now) -> Session:
                return Session(
                    id=f"ses_{uuid.uuid4().hex[:16]}",
                    user_id=user.id,
                    title=title,
                    source_tool=tool,
                    blob_key=f"b/{uuid.uuid4().hex}",
                    etag="x",
                    project_id=project_id,
                    git_remote_normalized=remote,
                    messages_text="we fixed the login redirect loop",
                    created_at=updated,
                    updated_at=updated,
                    uploaded_at=updated,
                )

            attributed = _session("attributed", pa.id)
            legacy = _session("legacy", None, tool="gemini-cli")
            elsewhere = _session("elsewhere", pb.id, updated=now - timedelta(days=30))
            db.add_all([attributed, legacy, elsewhere])
            await db.commit()

            hits, total = await search_sessions(
                db, user_id=user.id, query="login redirect", include_text=True
            )
            assert total == 3
            assert {h.session_id for h in hits} == {attributed.id, legacy.id, elsewhere.id}
            assert all("<mark>" in h.snippet for h in hits)
            assert hits[0].messages_text

            hits, total = await search_sessions(
                db,
                user_id=user.id,
                query="login",
                session_clause=project_session_clause(pa, legacy_only=True),
            )
            assert {h.session_id for h in hits} == {attributed.id, legacy.id}
            assert total == 2
            assert all(h.messages_text is None for h in hits)

            hits, _ = await search_sessions(db, user_id=user.id, query="login", tool="gemini")
            assert [h.session_id for h in hits] == [legacy.id]

            hits, _ = await search_sessions(db, user_id=user.id, query="login", days=7)
            assert elsewhere.id not in {h.session_id for h in hits}

            hits, _ = await search_sessions(
                db, user_id=user.id, query="login", session_ids=(elsewhere.id,)
            )
            assert [h.session_id for h in hits] == [elsewhere.id]
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()
