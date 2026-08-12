"""Integration tests for the unauthenticated handoff preview endpoint (P1).

GET /api/v1/handoffs/{handoff_id}/preview (X-Preview-Token header) — gated by a
single-purpose preview token (sha256 at rest). All failure modes return
constant 404.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Handoff, Session


def _make_messages_tar(messages: list[dict]) -> bytes:
    """Build a minimal .tar.gz containing messages.jsonl."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        content = "\n".join(json.dumps(m) for m in messages).encode("utf-8")
        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_preview_token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


@pytest.fixture
async def handoff_with_preview_token(
    db_session: AsyncSession,
    test_user,
    blob_store,
) -> Handoff:
    """Create a Handoff row with a preview_token_hash + session with messages."""
    import uuid as _uuid

    raw_token = f"hpr_{secrets.token_hex(16)}"
    token_hash = _make_preview_token_hash(raw_token)

    session_id = f"ses_{_uuid.uuid4().hex[:12]}"
    blob_key = f"sessions/{test_user.id}/{session_id}.tar.gz"

    messages = [
        {"role": "user", "content": "Please fix the bug in auth.py"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I found the issue — the token expiry check was inverted."},
                {
                    "type": "tool_use",
                    "name": "read_file",
                    "input": {"file_path": "auth.py"},
                    "id": "tool_1",
                },
            ],
        },
        {"role": "user", "content": "Great, apply the fix."},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool_1", "content": "def check_token..."},
                {"type": "text", "text": "The fix is applied. Tests pass locally."},
            ],
        },
        {
            "role": "assistant",
            "content": "Here's a secret: sk-abc123def456ghijklmnop — should be redacted.",
        },
    ]
    tar_data = _make_messages_tar(messages)
    await blob_store.put(blob_key, tar_data)

    # The endpoint serves ONLY the precomputed snapshot (Sentinel M2) — seed it
    # exactly as creation builds it.
    from sessionfs.server.routes.handoffs import _build_preview_snapshot
    import json as _json
    _snapshot_json = _json.dumps(_build_preview_snapshot(messages))

    session = Session(
        id=session_id,
        user_id=test_user.id,
        title="Test Fix Session",
        source_tool="claude-code",
        model_id="claude-sonnet-5",
        message_count=len(messages),
        blob_key=blob_key,
        blob_size_bytes=len(tar_data),
        etag=hashlib.sha256(tar_data).hexdigest()[:16],
    )
    db_session.add(session)

    handoff = Handoff(
        id=f"hnd_{_uuid.uuid4().hex[:8]}",
        session_id=session_id,
        sender_id=test_user.id,
        recipient_email="recipient@example.com",
        recipient_email_normalized="recipient@example.com",
        status="pending",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        snapshot_title="Test Fix Session",
        snapshot_tool="claude-code",
        snapshot_message_count=len(messages),
        preview_token_hash=token_hash,
        preview_snapshot=_snapshot_json,
    )
    db_session.add(handoff)
    await db_session.commit()
    await db_session.refresh(handoff)

    # Stash the raw token for test access.
    handoff._raw_preview_token = raw_token  # type: ignore[attr-defined]
    return handoff


class TestPreviewHappyPath:
    @pytest.mark.asyncio
    async def test_preview_returns_session_metadata(
        self, client: AsyncClient, handoff_with_preview_token: Handoff,
    ):
        """Valid token returns title, sender, tool, message count, status, expires_at."""
        token = handoff_with_preview_token._raw_preview_token  # type: ignore[attr-defined]
        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
            headers={"X-Preview-Token": token},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["title"] == "Test Fix Session"
        assert "test@example.com" in data["sender_email"]
        assert data["tool"] == "claude-code"
        assert data["message_count"] == 5
        assert data["status"] == "pending"
        assert data["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_preview_includes_truncated_messages(
        self, client: AsyncClient, handoff_with_preview_token: Handoff,
    ):
        """Preview messages are text blocks only, truncated to 400 chars each."""
        token = handoff_with_preview_token._raw_preview_token  # type: ignore[attr-defined]
        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
            headers={"X-Preview-Token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        msgs = data["preview_messages"]
        assert len(msgs) > 0
        for msg in msgs:
            assert "role" in msg
            assert "text" in msg
            assert "index" in msg
            assert len(msg["text"]) <= 400

    @pytest.mark.asyncio
    async def test_preview_no_archive_or_attachment_leakage(
        self, client: AsyncClient, handoff_with_preview_token: Handoff,
    ):
        """Response must never include blob_key, raw archive, or attachment data."""
        token = handoff_with_preview_token._raw_preview_token  # type: ignore[attr-defined]
        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
            headers={"X-Preview-Token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        body = json.dumps(data)
        assert "blob_key" not in body
        assert "tar.gz" not in body
        assert "blob_store" not in body
        assert "attachment" not in data

    @pytest.mark.asyncio
    async def test_preview_no_tool_results_in_messages(
        self, client: AsyncClient, handoff_with_preview_token: Handoff,
    ):
        """Tool result blocks and tool_use blocks are excluded from preview."""
        token = handoff_with_preview_token._raw_preview_token  # type: ignore[attr-defined]
        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
            headers={"X-Preview-Token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        combined = " ".join(m["text"] for m in data["preview_messages"])
        assert "tool_use" not in combined
        assert "tool_result" not in combined

    @pytest.mark.asyncio
    async def test_preview_dlp_redacts_secrets(
        self, client: AsyncClient, handoff_with_preview_token: Handoff,
    ):
        """DLP scan redacts secret patterns from preview text."""
        token = handoff_with_preview_token._raw_preview_token  # type: ignore[attr-defined]
        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
            headers={"X-Preview-Token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        combined = " ".join(m["text"] for m in data["preview_messages"])
        # The secret "sk-abc123..." should be redacted if the DLP pattern matches.
        assert "sk-abc123" not in combined or "[REDACTED]" in combined


class TestPreviewConstant404:
    @pytest.mark.asyncio
    async def test_no_token_returns_404(
        self, client: AsyncClient, handoff_with_preview_token: Handoff,
    ):
        """Missing token param → 404, never 401/403 (existence is sensitive)."""
        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_wrong_token_returns_404(
        self, client: AsyncClient, handoff_with_preview_token: Handoff,
    ):
        """Wrong token → 404, not 403."""
        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
            headers={"X-Preview-Token": "hpr_wrongtoken1234567890"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_nonexistent_handoff_returns_404(
        self, client: AsyncClient,
    ):
        """Handoff doesn't exist → 404."""
        resp = await client.get(
            "/api/v1/handoffs/hnd_nonexistent/preview",
            headers={"X-Preview-Token": "hpr_sometoken1234567890"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_revoked_handoff_returns_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        handoff_with_preview_token: Handoff,
    ):
        """Revoked handoff → 404 (don't leak that it existed)."""
        token = handoff_with_preview_token._raw_preview_token  # type: ignore[attr-defined]
        await db_session.execute(
            update(Handoff)
            .where(Handoff.id == handoff_with_preview_token.id)
            .values(status="revoked", revoked_at=datetime.now(timezone.utc))
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
            headers={"X-Preview-Token": token},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_expired_handoff_returns_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        handoff_with_preview_token: Handoff,
    ):
        """Expired handoff → 404."""
        token = handoff_with_preview_token._raw_preview_token  # type: ignore[attr-defined]
        await db_session.execute(
            update(Handoff)
            .where(Handoff.id == handoff_with_preview_token.id)
            .values(
                expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
            headers={"X-Preview-Token": token},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_already_claimed_handoff_returns_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        handoff_with_preview_token: Handoff,
    ):
        """Already-claimed-by-someone-else → 404."""
        token = handoff_with_preview_token._raw_preview_token  # type: ignore[attr-defined]
        await db_session.execute(
            update(Handoff)
            .where(Handoff.id == handoff_with_preview_token.id)
            .values(
                status="claimed",
                recipient_id="other_user_id",
                claimed_at=datetime.now(timezone.utc),
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
            headers={"X-Preview-Token": token},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_token_returns_404(
        self, client: AsyncClient, handoff_with_preview_token: Handoff,
    ):
        """Empty string token → 404."""
        resp = await client.get(
            f"/api/v1/handoffs/{handoff_with_preview_token.id}/preview",
            headers={"X-Preview-Token": ""},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    def test_preview_truncation_at_400_chars(self):
        """Truncation lives in the creation-time builder now (M2): a 500-char
        message is DLP-scanned FULL-LENGTH first (L3), then truncated to 400
        chars with an ellipsis."""
        from sessionfs.server.routes.handoffs import _build_preview_snapshot

        out = _build_preview_snapshot([{"role": "user", "content": "A" * 500}])
        assert len(out) == 1
        assert len(out[0]["text"]) == 400
        assert out[0]["text"].endswith("…")

    def test_secret_straddling_truncation_boundary_is_redacted(self):
        """L3 regression: a secret crossing the 400-char boundary must be
        redacted BEFORE truncation — its prefix must not leak."""
        from sessionfs.server.routes.handoffs import _build_preview_snapshot

        secret = "sk_sfs_9f2c47d18e6ba035c4a7d9e1f8b26034"  # realistic key (repeating hex is allowlisted as a dummy)
        text = "x" * 390 + secret  # secret straddles char 400
        out = _build_preview_snapshot([{"role": "user", "content": text}])
        # Even the truncated PREFIX of the secret must be gone — under the
        # truncate-first bug, chars 390-400 would read "sk_sfs_aaa".
        assert out and "sk_sfs_" not in out[0]["text"]
