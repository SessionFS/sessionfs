"""tk_7d33fecd44cf43d8 item 3 — lone surrogates in pushed metadata.

A lone UTF-16 surrogate in ``workspace.json`` (e.g. ``git.branch``) or
``manifest.json`` string fields cannot be UTF-8 encoded, so binding it to a
PostgreSQL text column raises ``UnicodeEncodeError`` and 500s the sync push.
The server must replace lone surrogates with U+FFFD before binding them to
Session columns, exactly like ``services/session_changes._sanitize``.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, Session, User


async def _make_user(db: AsyncSession) -> tuple[User, dict[str, str]]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"alice-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Alice",
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="alice-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


def _archive_with_surrogates() -> bytes:
    """A .sfs archive with a lone surrogate in git.branch, manifest title and
    message text."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = json.dumps({
            "sfs_version": "0.1.0",
            "session_id": "ses_x",
            "title": "Surrogate \ud800title",
            "tags": [],
            "source": {"tool": "claude-code"},
            "model": {"provider": "anthropic", "model_id": "claude-opus-4-6"},
            "stats": {"message_count": 1},
        }).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

        # A lone surrogate in message text: json.dumps escapes it as \ud800,
        # and json.loads on the extraction path turns it back into a lone
        # surrogate code point that cannot be UTF-8 encoded.
        message_line = json.dumps({"content": "hi \ud800 there"}).encode() + b"\n"
        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(message_line)
        tar.addfile(info, io.BytesIO(message_line))

        workspace = json.dumps({
            "root_path": "/home/user/project",
            "git": {
                "remote_url": "https://github.com/example/repo",
                "branch": "a\ud800b",
                "commit_sha": "abc123",
            },
        }).encode()
        info = tarfile.TarInfo(name="workspace.json")
        info.size = len(workspace)
        tar.addfile(info, io.BytesIO(workspace))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_sync_push_sanitizes_lone_surrogates(
    client: AsyncClient, db_session: AsyncSession
):
    user, headers = await _make_user(db_session)
    sid = f"ses_{uuid.uuid4().hex[:16]}"
    archive = _archive_with_surrogates()

    resp = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=headers,
        files={"file": ("s.tar.gz", io.BytesIO(archive), "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text

    row = (
        await db_session.execute(select(Session).where(Session.id == sid))
    ).scalar_one()
    assert row.git_branch == "a�b"
    assert row.title == "Surrogate �title"
    assert row.messages_text == "hi � there"
    # The stored values must round-trip cleanly to UTF-8 (no surrogate left).
    row.git_branch.encode("utf-8")
    row.title.encode("utf-8")
    row.messages_text.encode("utf-8")
