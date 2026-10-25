"""Integration tests for the session changes endpoint."""

from __future__ import annotations

import io
import json
import tarfile
import uuid

import pytest
from httpx import AsyncClient


def _changes_tar() -> bytes:
    """Build a .sfs tar.gz with Edit/Write/apply_patch tool uses + workspace."""
    manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_changes",
        "title": "changes",
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 4, "turn_count": 2},
    }).encode()

    messages = (
        json.dumps({
            "msg_id": "msg_001", "role": "user",
            "content": [{"type": "text", "text": "fix it"}],
        }) + "\n"
        + json.dumps({
            "msg_id": "msg_002", "role": "assistant",
            "content": [{
                "type": "tool_use", "tool_use_id": "tu_1", "name": "Edit",
                "input": {
                    "file_path": "src/app.py",
                    "old_string": "return 1",
                    "new_string": "return 2",
                },
            }],
        }) + "\n"
        + json.dumps({
            "msg_id": "msg_003", "role": "assistant",
            "content": [{
                "type": "tool_use", "tool_use_id": "tu_2", "name": "Write",
                "input": {"file_path": "src/new.py", "content": "def hi():\n    pass"},
            }],
        }) + "\n"
        + json.dumps({
            "msg_id": "msg_004", "role": "assistant",
            "content": [{
                "type": "tool_use", "tool_use_id": "tu_3", "name": "apply_patch",
                "input": {"patch": "--- a/src/patched.py\n+++ b/src/patched.py\n@@ -1 +1 @@\n-old\n+new"},
            }],
        }) + "\n"
    ).encode()

    workspace = json.dumps({
        "root_path": "/home/user/project",
        "git": {"branch": "main", "commit_sha": "abc123"},
        "files": [
            {"path": "src/app.py", "role": "edited"},
            {"path": "gone.py", "role": "deleted"},
        ],
    }).encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (
            ("manifest.json", manifest),
            ("messages.jsonl", messages),
            ("workspace.json", workspace),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_get_changes_returns_derived_files(
    client: AsyncClient, auth_headers: dict
):
    await client.put(
        "/api/v1/sessions/ses_changes1234abc/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(_changes_tar()), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_changes1234abc/changes", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["git_ref"] == "main@abc123"
    assert data["truncated"] is False

    by_path = {f["path"]: f for f in data["files"]}
    assert set(by_path) == {"src/app.py", "src/new.py", "src/patched.py", "gone.py"}

    edit = by_path["src/app.py"]
    assert edit["operation"] == "edited"
    assert "-return 1" in edit["diff"] and "+return 2" in edit["diff"]
    assert edit["added_lines"] == 1 and edit["removed_lines"] == 1
    assert edit["message_indexes"] == [1]

    write = by_path["src/new.py"]
    assert write["operation"] == "created"
    assert write["diff"] == "--- /dev/null\n+++ b/src/new.py\n+def hi():\n+    pass"
    assert write["added_lines"] == 2

    patched = by_path["src/patched.py"]
    assert patched["operation"] == "edited"
    assert patched["path"] == "src/patched.py"

    deleted = by_path["gone.py"]
    assert deleted["operation"] == "deleted"
    assert deleted["diff"] == ""


@pytest.mark.asyncio
async def test_changes_empty_when_no_file_tools(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    await client.put(
        "/api/v1/sessions/ses_changesempty12ab/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    resp = await client.get(
        "/api/v1/sessions/ses_changesempty12ab/changes", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["files"] == []


@pytest.mark.asyncio
async def test_changes_404_not_found(client: AsyncClient, auth_headers: dict):
    resp = await client.get(
        "/api/v1/sessions/ses_nochanges1234ab/changes", headers=auth_headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_changes_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/sessions/ses_noauthchg1234ab/changes")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_changes_etag_304(client: AsyncClient, auth_headers: dict):
    await client.put(
        "/api/v1/sessions/ses_changesetag123ab/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(_changes_tar()), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_changesetag123ab/changes", headers=auth_headers
    )
    assert resp.status_code == 200
    etag = resp.headers["ETag"]
    # Derived ETag is "<session.etag>-chg2", distinct from the raw session ETag.
    assert etag.startswith('"') and etag.endswith('-chg2"')

    # Exact match
    cached = await client.get(
        "/api/v1/sessions/ses_changesetag123ab/changes",
        headers={**auth_headers, "If-None-Match": etag},
    )
    assert cached.status_code == 304
    assert cached.headers["ETag"] == etag  # the 304 must carry the ETag too


@pytest.mark.asyncio
async def test_changes_etag_weak_list_and_star(client: AsyncClient, auth_headers: dict):
    await client.put(
        "/api/v1/sessions/ses_changesetagweak12a/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(_changes_tar()), "application/gzip")},
    )
    resp = await client.get(
        "/api/v1/sessions/ses_changesetagweak12a/changes", headers=auth_headers
    )
    etag = resp.headers["ETag"]
    assert etag.startswith('"')

    # Weak prefix
    weak = await client.get(
        "/api/v1/sessions/ses_changesetagweak12a/changes",
        headers={**auth_headers, "If-None-Match": f"W/{etag}"},
    )
    assert weak.status_code == 304

    # Comma-separated list
    listed = await client.get(
        "/api/v1/sessions/ses_changesetagweak12a/changes",
        headers={**auth_headers, "If-None-Match": f'"other", {etag}'},
    )
    assert listed.status_code == 304

    # Star matches anything
    star = await client.get(
        "/api/v1/sessions/ses_changesetagweak12a/changes",
        headers={**auth_headers, "If-None-Match": "*"},
    )
    assert star.status_code == 304

    # A non-matching ETag still returns the full body.
    miss = await client.get(
        "/api/v1/sessions/ses_changesetagweak12a/changes",
        headers={**auth_headers, "If-None-Match": '"something-else"'},
    )
    assert miss.status_code == 200


@pytest.mark.asyncio
async def test_changes_skips_non_object_messages_lines(client: AsyncClient, auth_headers: dict):
    """A non-object line in messages.jsonl must not 500 the changes endpoint."""
    manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_changesbadline12",
        "title": "badline",
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 1, "turn_count": 1},
    }).encode()
    # A bare string line sandwiched between valid messages.
    messages = (
        json.dumps({
            "msg_id": "m1",
            "role": "assistant",
            "content": [{
                "type": "tool_use", "tool_use_id": "tu_1", "name": "Write",
                "input": {"file_path": "new.py", "content": "print(1)"},
            }],
        }) + "\n"
        + '"this is not an object"\n'
    ).encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (("manifest.json", manifest), ("messages.jsonl", messages)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

    await client.put(
        "/api/v1/sessions/ses_changesbadline12/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(buf.getvalue()), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_changesbadline12/changes", headers=auth_headers
    )
    assert resp.status_code == 200
    assert len(resp.json()["files"]) == 1


def _surrogate_changes_tar() -> bytes:
    """A .sfs tar whose Edit/Write inputs each carry a lone UTF-16 surrogate."""
    manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_surrogate",
        "title": "surrogate",
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 3, "turn_count": 1},
    }).encode()

    # A lone high surrogate (U+D800) in each of the three fields the finding
    # calls out: Edit old_string, Write content, and file_path.
    messages = (
        json.dumps({
            "msg_id": "m1", "role": "assistant",
            "content": [{
                "type": "tool_use", "tool_use_id": "tu_1", "name": "Edit",
                "input": {"file_path": "src/a.py", "old_string": "a\ud800b", "new_string": "c"},
            }],
        }) + "\n"
        + json.dumps({
            "msg_id": "m2", "role": "assistant",
            "content": [{
                "type": "tool_use", "tool_use_id": "tu_2", "name": "Write",
                "input": {"file_path": "src/new.py", "content": "x\ud800y"},
            }],
        }) + "\n"
        + json.dumps({
            "msg_id": "m3", "role": "assistant",
            "content": [{
                "type": "tool_use", "tool_use_id": "tu_3", "name": "Write",
                "input": {"file_path": "src/f\ud800.py", "content": "ok"},
            }],
        }) + "\n"
    ).encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (("manifest.json", manifest), ("messages.jsonl", messages)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_changes_surrogate_inputs_return_200(client: AsyncClient, auth_headers: dict):
    """Lone UTF-16 surrogates in Edit/Write/file_path must not 500 the endpoint."""
    await client.put(
        "/api/v1/sessions/ses_surrogate1234ab/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(_surrogate_changes_tar()), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_surrogate1234ab/changes", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    # Surrogates are replaced with U+FFFD; the body is plain valid UTF-8 JSON.
    body = json.dumps(data, ensure_ascii=False)
    assert "\ud800" not in body
    assert "�" in body


@pytest.mark.asyncio
async def test_changes_redaction_parity(
    client: AsyncClient, auth_headers: dict, db_session, test_user
):
    """A secret in an Edit input is redacted at push time and never surfaces
    in the derived changes output (parity with the messages view)."""
    from sessionfs.server.db.models import OrgMember, Organization

    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps(
            {"dlp": {"enabled": True, "mode": "redact", "categories": ["secrets"]}}
        ),
    )
    db_session.add(org)
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    secret = "supersecretvalue123"
    manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_changesredact12a",
        "title": "redact",
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 1, "turn_count": 1},
    }).encode()
    messages = (
        json.dumps({
            "msg_id": "m1",
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "tool_use_id": "tu_1",
                "name": "Edit",
                "input": {
                    "file_path": "src/config.py",
                    "old_string": "return placeholder",
                    "new_string": f'password = "{secret}"',
                },
            }],
        }) + "\n"
    ).encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (("manifest.json", manifest), ("messages.jsonl", messages)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

    await client.put(
        "/api/v1/sessions/ses_changesredact12a/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(buf.getvalue()), "application/gzip")},
    )

    resp = await client.get(
        "/api/v1/sessions/ses_changesredact12a/changes", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["files"]) == 1
    diff = data["files"][0]["diff"]
    assert secret not in diff
    assert "[REDACTED:generic_password_assignment]" in diff
