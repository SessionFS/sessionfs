"""PostgreSQL: the group promote / extraction reauthorization takes real row
locks (GitHub #116). Runs only with SFS_TEST_PG_URL.

A grant revocation, group deletion or removal of the target from the group does
not take the scope advisory lock, so the reauthorization has to hold FOR UPDATE
locks on those rows: a concurrent writer must block until the reauthorizing
transaction commits.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.server.auth.dependencies import AuthContext
from sessionfs.server.db.models import (
    MeetingActionItem,
    MeetingTranscript,
    OrgMember,
    Organization,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
    ProjectGroupMember,
    User,
)
from sessionfs.server.routes.meeting_actions import _lock_and_recheck_group_promote
from sessionfs.server.services.meeting_extraction import _lock_and_reauthorize
from tests.pg_helpers import reset_pg_schema, upgrade_pg_to_head

pytestmark = pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set"
)


async def _seed(factory):
    now = datetime.now(timezone.utc)
    async with factory() as db:
        owner = User(id=str(uuid.uuid4()), email="o@pg.test", display_name="O",
                     tier="enterprise", email_verified=True, created_at=now)
        editor = User(id=str(uuid.uuid4()), email="e@pg.test", display_name="E",
                      tier="enterprise", email_verified=True, created_at=now)
        db.add_all([owner, editor])
        await db.flush()
        org = Organization(id="org_pg", name="PG", slug="pg-org", tier="enterprise",
                           seats_limit=10, settings="{}")
        db.add(org)
        await db.flush()
        db.add_all([
            OrgMember(org_id=org.id, user_id=owner.id, role="owner"),
            OrgMember(org_id=org.id, user_id=editor.id, role="member"),
        ])
        project = Project(id="proj_pg", name="P", git_remote_normalized="github.com/a/pg",
                          context_document="", owner_id=owner.id, org_id=org.id)
        group = ProjectGroup(id="pg_pg", org_id=org.id, slug="g", name="G",
                             created_by_user_id=owner.id, created_at=now, updated_at=now)
        db.add_all([project, group])
        await db.flush()
        db.add_all([
            ProjectGroupAccess(group_id=group.id, project_id=project.id),
            ProjectGroupMember(group_id=group.id, user_id=editor.id, role="editor"),
        ])
        content = "Alice: plan"
        meeting = MeetingTranscript(
            id="meet_pg", org_id=org.id, group_id=group.id, title="t",
            source="manual_paste", content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            line_count=1, created_by_user_id=owner.id,
        )
        db.add(meeting)
        await db.flush()
        db.add(MeetingActionItem(
            id="mai_pg", transcript_id=meeting.id, kind="action", description="x",
            line_start=1, line_end=1, status="pending", extracted_at=now,
        ))
        await db.commit()
        return editor.id, project.id


async def _blocked_until_commit(factory, holder, writer_sql: str) -> None:
    """``writer_sql`` must wait for ``holder``'s locks, then finish once it
    commits."""
    async def _write() -> None:
        async with factory() as w:
            await w.execute(text(writer_sql))
            await w.commit()

    task = asyncio.create_task(_write())
    await asyncio.sleep(1.0)
    assert not task.done(), "writer was not blocked by the reauthorization's row locks"
    await holder.commit()
    await asyncio.wait_for(task, 10)


@pytest.mark.parametrize(
    "writer_sql",
    [
        "DELETE FROM project_group_members WHERE group_id = 'pg_pg'",
        "UPDATE project_groups SET deleted_at = now() WHERE id = 'pg_pg'",
    ],
)
def test_extraction_group_reauthorization_blocks_revocation_and_deletion(writer_sql) -> None:
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    upgrade_pg_to_head(url)

    async def _go() -> None:
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            editor_id, _ = await _seed(factory)
            async with factory() as db:
                user = await db.get(User, editor_id)
                auth = AuthContext(user=user, api_key_id="", key_kind="user")
                await _lock_and_reauthorize(db, "meet_pg", auth)
                await _blocked_until_commit(factory, db, writer_sql)
            async with factory() as db:
                user = await db.get(User, editor_id)
                auth = AuthContext(user=user, api_key_id="", key_kind="user")
                with pytest.raises(HTTPException) as exc:
                    await _lock_and_reauthorize(db, "meet_pg", auth)
                assert exc.value.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(_go())


@pytest.mark.parametrize(
    "writer_sql",
    [
        "DELETE FROM project_group_members WHERE group_id = 'pg_pg'",
        "UPDATE project_groups SET deleted_at = now() WHERE id = 'pg_pg'",
        "DELETE FROM project_group_access WHERE group_id = 'pg_pg'",
        "UPDATE meeting_transcripts SET deleted_at = now() WHERE id = 'meet_pg'",
    ],
)
def test_group_promote_recheck_blocks_revocation_deletion_and_target_removal(writer_sql) -> None:
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    upgrade_pg_to_head(url)

    async def _go() -> None:
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            editor_id, project_id = await _seed(factory)
            async with factory() as db:
                user = await db.get(User, editor_id)
                target = await db.get(Project, project_id)
                auth = AuthContext(user=user, api_key_id="", key_kind="user")
                await _lock_and_recheck_group_promote(db, "meet_pg", "mai_pg", target, auth)
                await _blocked_until_commit(factory, db, writer_sql)
            async with factory() as db:
                user = await db.get(User, editor_id)
                target = await db.get(Project, project_id)
                auth = AuthContext(user=user, api_key_id="", key_kind="user")
                with pytest.raises(HTTPException):
                    await _lock_and_recheck_group_promote(
                        db, "meet_pg", "mai_pg", target, auth
                    )
        finally:
            await engine.dispose()

    asyncio.run(_go())
