"""Integration tests for session CRUD operations."""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from sessionfs.server.db.models import Session, User


def _mk_session(
    user_id: str,
    session_id: str,
    *,
    model_id: str | None,
    source_tool: str = "claude-code",
    updated_at: datetime | None = None,
    is_deleted: bool = False,
) -> Session:
    now = updated_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Session(
        id=session_id,
        user_id=user_id,
        title=f"Session {session_id}",
        tags="[]",
        source_tool=source_tool,
        model_id=model_id,
        message_count=1,
        blob_key=f"blobs/{user_id}/{session_id}",
        blob_size_bytes=10,
        etag=f"etag-{session_id}",
        created_at=now,
        updated_at=now,
        uploaded_at=now,
        is_deleted=is_deleted,
    )


async def _add_sessions(db_session, *sessions: Session) -> None:
    db_session.add_all(sessions)
    await db_session.commit()


@pytest.mark.asyncio
async def test_upload_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code", "title": "My Session"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["session_id"].startswith("ses_")
    assert data["etag"]
    assert data["blob_size_bytes"] > 0


@pytest.mark.asyncio
async def test_list_sessions_empty(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/v1/sessions", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessions"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_sessions_with_data(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    # Upload a session first
    await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get("/api/v1/sessions", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["sessions"]) == 1


@pytest.mark.asyncio
async def test_list_sessions_filter_source_tool(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"source_tool": "codex"}
    )
    assert resp.json()["total"] == 0

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"source_tool": "claude-code"}
    )
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_list_sessions_filter_source_tool_alias_family(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "gemini-cli"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"source_tool": "gemini"}
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_get_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code", "title": "Test"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.get(f"/api/v1/sessions/{session_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == session_id
    assert data["title"] == "Test"


@pytest.mark.asyncio
async def test_get_session_404(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/v1/sessions/ses_nonexistent12ab", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.get(f"/api/v1/sessions/{session_id}/download", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert resp.content == sample_sfs_tar


@pytest.mark.asyncio
async def test_patch_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code", "title": "Old Title"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.patch(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        json={"title": "New Title", "tags": ["updated"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "New Title"
    assert data["tags"] == ["updated"]


@pytest.mark.asyncio
async def test_delete_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    session_id = upload.json()["session_id"]

    resp = await client.delete(
        f"/api/v1/sessions/{session_id}",
        headers=auth_headers,
        params={"scope": "cloud"},
    )
    assert resp.status_code == 200

    # Should be 404 after soft delete
    resp = await client.get(f"/api/v1/sessions/{session_id}", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_404(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/v1/sessions/ses_nope12345678ab/download", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_session_provenance(
    client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes
):
    """GET /api/v1/sessions/{id}/provenance returns the 4 provenance
    fields. Sessions captured before migration 028 (or with provenance
    capture disabled) return all-null fields rather than 404 — only
    nonexistent sessions 404."""
    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert upload.status_code == 201
    session_id = upload.json()["session_id"]

    # Existing session, no provenance recorded (uploaded fresh) — should
    # return 200 with all-null fields and an empty artifacts list.
    resp = await client.get(
        f"/api/v1/sessions/{session_id}/provenance",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["session_id"] == session_id
    assert data["rules_version"] is None
    assert data["rules_hash"] is None
    # rules_source defaults to DB literal "none" — surface as None
    assert data["rules_source"] is None
    assert data["instruction_artifacts"] == []

    # Nonexistent session → 404
    nf = await client.get(
        "/api/v1/sessions/ses_nonexistent12ab/provenance",
        headers=auth_headers,
    )
    assert nf.status_code == 404


@pytest.mark.asyncio
async def test_get_session_provenance_cross_user_404(
    client: AsyncClient,
    auth_headers: dict,
    sample_sfs_tar: bytes,
    db_session,
):
    """A user must not be able to read another user's session provenance.
    _get_user_session() filters by ownership and returns 404 (not 403) so
    the route does not leak the existence of sessions across user
    boundaries. Tier A added this endpoint, so we lock the behaviour in."""
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz

    from sessionfs.server.auth.keys import (
        generate_api_key as _gen_key,
        hash_api_key as _hash_key,
    )
    from sessionfs.server.db.models import ApiKey as _ApiKey, User as _User

    upload = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert upload.status_code == 201
    owned_session_id = upload.json()["session_id"]

    outsider = _User(
        id=str(_uuid.uuid4()),
        email=f"outsider_{_uuid.uuid4().hex[:8]}@example.com",
        tier="pro",
        email_verified=True,
        created_at=_dt.now(_tz.utc),
    )
    db_session.add(outsider)
    await db_session.commit()
    raw = _gen_key()
    db_session.add(_ApiKey(
        id=str(_uuid.uuid4()),
        user_id=outsider.id,
        key_hash=_hash_key(raw),
        name="outsider-key",
        created_at=_dt.now(_tz.utc),
    ))
    await db_session.commit()
    outsider_headers = {"Authorization": f"Bearer {raw}"}

    resp = await client.get(
        f"/api/v1/sessions/{owned_session_id}/provenance",
        headers=outsider_headers,
    )
    assert resp.status_code == 404, (
        f"Cross-user provenance must 404 (not leak existence), got "
        f"{resp.status_code}: {resp.text[:200]}"
    )


# --- Model filter / sort / distinct-models endpoint (feat/model-filter) ---


@pytest.mark.asyncio
async def test_list_sessions_filter_model_single(
    client: AsyncClient, auth_headers: dict, test_user: User, db_session
):
    await _add_sessions(
        db_session,
        _mk_session(test_user.id, "ses_a1", model_id="claude-sonnet-4-6"),
        _mk_session(test_user.id, "ses_b1", model_id="gpt-5-20250127"),
    )

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"model": "claude-sonnet-4-6"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert [s["id"] for s in data["sessions"]] == ["ses_a1"]
    assert data["sessions"][0]["model_id"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_list_sessions_filter_model_multi(
    client: AsyncClient, auth_headers: dict, test_user: User, db_session
):
    await _add_sessions(
        db_session,
        _mk_session(test_user.id, "ses_a1", model_id="claude-sonnet-4-6"),
        _mk_session(test_user.id, "ses_b1", model_id="gpt-5-20250127"),
        _mk_session(test_user.id, "ses_c1", model_id="deepseek-v4-pro"),
    )

    resp = await client.get(
        "/api/v1/sessions",
        headers=auth_headers,
        params=[("model", "claude-sonnet-4-6"), ("model", "deepseek-v4-pro")],
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert {s["id"] for s in data["sessions"]} == {"ses_a1", "ses_c1"}


@pytest.mark.asyncio
async def test_list_sessions_filter_model_unknown(
    client: AsyncClient, auth_headers: dict, test_user: User, db_session
):
    await _add_sessions(
        db_session,
        _mk_session(test_user.id, "ses_null", model_id=None),
        _mk_session(test_user.id, "ses_empty", model_id=""),
        _mk_session(test_user.id, "ses_literal", model_id="unknown"),
        _mk_session(test_user.id, "ses_synth", model_id="<synthetic>"),
        _mk_session(test_user.id, "ses_known", model_id="claude-sonnet-4-6"),
    )

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"model": "unknown"}
    )
    assert resp.status_code == 200
    data = resp.json()
    # NULL, "", a literal "unknown" id and <synthetic> are all the unknown bucket
    assert data["total"] == 4
    assert {s["id"] for s in data["sessions"]} == {
        "ses_null", "ses_empty", "ses_literal", "ses_synth",
    }

    # A known id plus the sentinel: one IN clause OR the unknown bucket
    resp = await client.get(
        "/api/v1/sessions",
        headers=auth_headers,
        params=[("model", "claude-sonnet-4-6"), ("model", "unknown")],
    )
    assert resp.json()["total"] == 5

    # <synthetic> asked for by name is still the unknown bucket, not an exact id
    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"model": "<synthetic>"}
    )
    assert resp.json()["total"] == 4


@pytest.mark.asyncio
async def test_list_sessions_sort_model(
    client: AsyncClient, auth_headers: dict, test_user: User, db_session
):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    await _add_sessions(
        db_session,
        _mk_session(test_user.id, "ses_gb", model_id="gpt-5-20250127", updated_at=t2),
        _mk_session(test_user.id, "ses_ga", model_id="gpt-5-20250127", updated_at=t2),
        _mk_session(test_user.id, "ses_gold", model_id="gpt-5-20250127", updated_at=t1),
        _mk_session(test_user.id, "ses_claude", model_id="claude-sonnet-4-6", updated_at=t2),
        # Unknown bucket: NULL, "", a literal "unknown" and <synthetic>. They
        # sort LAST in both directions and tie-break by updated desc, then id.
        _mk_session(test_user.id, "ses_u_null_new", model_id=None, updated_at=t2),
        _mk_session(test_user.id, "ses_u_null_old", model_id=None, updated_at=t1),
        _mk_session(test_user.id, "ses_u_empty", model_id="", updated_at=t1),
        _mk_session(test_user.id, "ses_u_literal", model_id="unknown", updated_at=t1),
        _mk_session(test_user.id, "ses_u_synth", model_id="<synthetic>", updated_at=t1),
    )
    unknown_tail = [
        "ses_u_null_new", "ses_u_empty", "ses_u_literal", "ses_u_null_old", "ses_u_synth",
    ]

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"sort": "model", "order": "asc"}
    )
    assert [s["id"] for s in resp.json()["sessions"]] == [
        "ses_claude", "ses_ga", "ses_gb", "ses_gold", *unknown_tail,
    ]

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"sort": "model", "order": "desc"}
    )
    assert [s["id"] for s in resp.json()["sessions"]] == [
        "ses_ga", "ses_gb", "ses_gold", "ses_claude", *unknown_tail,
    ]

    # Sorting spans pages: page 2 of size 4 starts where page 1 stopped.
    resp = await client.get(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"sort": "model", "order": "asc", "page": 2, "page_size": 4},
    )
    assert [s["id"] for s in resp.json()["sessions"]] == unknown_tail[:4]


@pytest.mark.asyncio
async def test_list_sessions_total_respects_model_filter(
    client: AsyncClient, auth_headers: dict, test_user: User, db_session
):
    await _add_sessions(
        db_session,
        _mk_session(test_user.id, "ses_a1", model_id="claude-sonnet-4-6"),
        _mk_session(test_user.id, "ses_a2", model_id="claude-sonnet-4-6"),
        _mk_session(test_user.id, "ses_b1", model_id="gpt-5-20250127"),
    )

    resp = await client.get(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"model": "claude-sonnet-4-6", "page": 1, "page_size": 1},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["sessions"]) == 1
    assert data["has_more"] is True


@pytest.mark.asyncio
async def test_list_session_models_respects_visibility_trash_source_tool(
    client: AsyncClient, auth_headers: dict, test_user: User, db_session
):
    outsider = User(
        id=str(uuid.uuid4()),
        email=f"outsider_{uuid.uuid4().hex[:8]}@example.com",
        tier="pro",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(outsider)
    await db_session.commit()

    await _add_sessions(
        db_session,
        _mk_session(test_user.id, "ses_c1", model_id="claude-sonnet-4-6"),
        _mk_session(test_user.id, "ses_c2", model_id="claude-sonnet-4-6"),
        _mk_session(test_user.id, "ses_d1", model_id="gpt-5-20250127"),
        _mk_session(test_user.id, "ses_null", model_id=None),
        _mk_session(test_user.id, "ses_empty", model_id=""),
        _mk_session(test_user.id, "ses_literal", model_id="unknown"),
        _mk_session(test_user.id, "ses_synth", model_id="<synthetic>"),
        _mk_session(test_user.id, "ses_trash", model_id="deepseek-v4-pro", is_deleted=True),
        _mk_session(test_user.id, "ses_codex", model_id="codex-4-1", source_tool="codex"),
        _mk_session(outsider.id, "ses_out", model_id="claude-sonnet-4-6"),
    )

    resp = await client.get("/api/v1/sessions/models", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    # Count desc, then model id asc; the unknown bucket sorts as "".
    assert [m["model_id"] for m in data["models"]] == [
        None, "claude-sonnet-4-6", "codex-4-1", "gpt-5-20250127",
    ]
    # One unknown bucket: no "", "unknown" or "<synthetic>" entries of their own
    ids = [m["model_id"] for m in data["models"]]
    assert ids.count(None) == 1
    assert "" not in ids and "unknown" not in ids and "<synthetic>" not in ids
    by_model = {m["model_id"]: m["count"] for m in data["models"]}
    assert by_model["claude-sonnet-4-6"] == 2  # outsider's identical model not counted
    assert by_model["gpt-5-20250127"] == 1
    assert by_model["codex-4-1"] == 1
    # NULL + "" + literal "unknown" + <synthetic> collapse into one bucket
    assert by_model[None] == 4
    assert "deepseek-v4-pro" not in by_model  # soft-deleted excluded by default

    # source_tool narrows the same way the list route does
    resp = await client.get(
        "/api/v1/sessions/models", headers=auth_headers, params={"source_tool": "codex"}
    )
    assert [m["model_id"] for m in resp.json()["models"]] == ["codex-4-1"]

    # trash view counts only soft-deleted sessions
    resp = await client.get(
        "/api/v1/sessions/models", headers=auth_headers, params={"deleted": True}
    )
    assert [m["model_id"] for m in resp.json()["models"]] == ["deepseek-v4-pro"]


@pytest.mark.asyncio
async def test_list_sessions_model_param_dedupes(
    client: AsyncClient, auth_headers: dict, test_user: User, db_session
):
    await _add_sessions(
        db_session,
        _mk_session(test_user.id, "ses_a1", model_id="claude-sonnet-4-6"),
        _mk_session(test_user.id, "ses_b1", model_id="gpt-5-20250127"),
    )
    resp = await client.get(
        "/api/v1/sessions",
        headers=auth_headers,
        params=[("model", "claude-sonnet-4-6")] * 80,
    )
    # 80 copies of one value dedupe to a single value: well under the limit
    assert resp.status_code == 200
    assert [s["id"] for s in resp.json()["sessions"]] == ["ses_a1"]


@pytest.mark.asyncio
async def test_list_sessions_model_param_limits(
    client: AsyncClient, auth_headers: dict, test_user: User
):
    fifty = [("model", f"model-{i}") for i in range(50)]
    resp = await client.get("/api/v1/sessions", headers=auth_headers, params=fifty)
    assert resp.status_code == 200

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params=[*fifty, ("model", "model-50")]
    )
    assert resp.status_code == 422

    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"model": "m" * 200}
    )
    assert resp.status_code == 200
    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"model": "m" * 201}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_sessions_sort_and_order_validated(
    client: AsyncClient, auth_headers: dict, test_user: User
):
    for params in (
        {},
        {"sort": "updated_at"},
        {"sort": "model", "order": "asc"},
        {"order": "asc"},
    ):
        resp = await client.get("/api/v1/sessions", headers=auth_headers, params=params)
        assert resp.status_code == 200, params
    for params in ({"sort": "title"}, {"order": "sideways"}, {"sort": "model; drop"}):
        resp = await client.get("/api/v1/sessions", headers=auth_headers, params=params)
        assert resp.status_code == 422, params


@pytest.mark.asyncio
async def test_list_sessions_order_asc_by_updated(
    client: AsyncClient, auth_headers: dict, test_user: User, db_session
):
    await _add_sessions(
        db_session,
        _mk_session(test_user.id, "ses_new", model_id=None,
                    updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
        _mk_session(test_user.id, "ses_old", model_id=None,
                    updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )
    resp = await client.get("/api/v1/sessions", headers=auth_headers)
    assert [s["id"] for s in resp.json()["sessions"]] == ["ses_new", "ses_old"]
    resp = await client.get(
        "/api/v1/sessions", headers=auth_headers, params={"order": "asc"}
    )
    assert [s["id"] for s in resp.json()["sessions"]] == ["ses_old", "ses_new"]
