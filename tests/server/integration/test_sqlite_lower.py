"""SQLite `lower()` override must be NULL-safe (GH #70).

The app installs a Unicode-aware `lower()` on every SQLite connection so the
org-persona (org_id, lower(name)) UNIQUE index folds non-ASCII case. That
override replaces the built-in app-wide, so it must keep the built-in's NULL
semantics: `lower(NULL)` returns NULL instead of raising.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Session


@pytest.mark.asyncio
async def test_sqlite_lower_is_null_safe(db_session: AsyncSession, test_user) -> None:
    # `str.lower(None)` raises TypeError, which sqlite3 turns into
    # "OperationalError: user-defined function raised exception" — a 500 on
    # any query folding a nullable column once a scanned row holds NULL.
    null_sid = f"ses_{uuid.uuid4().hex[:12]}"
    db_session.add(Session(
        id=null_sid,
        user_id=test_user.id,
        source_tool="claude-code",
        blob_key=f"b/{null_sid}",
        etag="e",
        title=None,
    ))
    named_sid = f"ses_{uuid.uuid4().hex[:12]}"
    db_session.add(Session(
        id=named_sid,
        user_id=test_user.id,
        source_tool="claude-code",
        blob_key=f"b/{named_sid}",
        etag="e",
        title="Álex",
    ))
    await db_session.commit()

    folded = (
        await db_session.execute(
            select(func.lower(Session.title)).where(Session.id == null_sid)
        )
    ).scalar_one()
    assert folded is None

    # Non-NULL input still folds (Unicode included).
    folded = (
        await db_session.execute(
            select(func.lower(Session.title)).where(Session.id == named_sid)
        )
    ).scalar_one()
    assert folded == "álex"
