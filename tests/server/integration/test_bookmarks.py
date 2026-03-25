"""Integration tests for bookmark folders and bookmarks."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient


async def _upload_session(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes) -> str:
    """Upload a session and return its ID."""
    resp = await client.post(
        "/api/v1/sessions",
        headers=auth_headers,
        params={"source_tool": "claude-code"},
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201
    return resp.json()["session_id"]


@pytest.mark.asyncio
async def test_create_folder(client: AsyncClient, auth_headers: dict):
    """Create a bookmark folder."""
    resp = await client.post(
        "/api/v1/bookmarks/folders",
        headers=auth_headers,
        json={"name": "Important", "color": "#4f9cf7"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Important"
    assert data["color"] == "#4f9cf7"
    assert data["bookmark_count"] == 0
    assert "id" in data
    assert "created_at" in data


@pytest.mark.asyncio
async def test_list_folders(client: AsyncClient, auth_headers: dict):
    """List bookmark folders with counts."""
    # Create two folders
    await client.post(
        "/api/v1/bookmarks/folders",
        headers=auth_headers,
        json={"name": "Folder A"},
    )
    await client.post(
        "/api/v1/bookmarks/folders",
        headers=auth_headers,
        json={"name": "Folder B", "color": "#3ddc84"},
    )

    resp = await client.get("/api/v1/bookmarks/folders", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["folders"]) == 2
    names = [f["name"] for f in data["folders"]]
    assert "Folder A" in names
    assert "Folder B" in names


@pytest.mark.asyncio
async def test_update_folder(client: AsyncClient, auth_headers: dict):
    """Rename and recolor a folder."""
    create_resp = await client.post(
        "/api/v1/bookmarks/folders",
        headers=auth_headers,
        json={"name": "Old Name", "color": "#4f9cf7"},
    )
    folder_id = create_resp.json()["id"]

    resp = await client.put(
        f"/api/v1/bookmarks/folders/{folder_id}",
        headers=auth_headers,
        json={"name": "New Name", "color": "#f04060"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "New Name"
    assert data["color"] == "#f04060"


@pytest.mark.asyncio
async def test_add_bookmark(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Bookmark a session into a folder."""
    session_id = await _upload_session(client, auth_headers, sample_sfs_tar)

    folder_resp = await client.post(
        "/api/v1/bookmarks/folders",
        headers=auth_headers,
        json={"name": "My Folder"},
    )
    folder_id = folder_resp.json()["id"]

    resp = await client.post(
        "/api/v1/bookmarks",
        headers=auth_headers,
        json={"folder_id": folder_id, "session_id": session_id},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["folder_id"] == folder_id
    assert data["session_id"] == session_id
    assert "id" in data


@pytest.mark.asyncio
async def test_list_folder_sessions(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """List sessions in a bookmark folder."""
    session_id = await _upload_session(client, auth_headers, sample_sfs_tar)

    folder_resp = await client.post(
        "/api/v1/bookmarks/folders",
        headers=auth_headers,
        json={"name": "Reading List"},
    )
    folder_id = folder_resp.json()["id"]

    await client.post(
        "/api/v1/bookmarks",
        headers=auth_headers,
        json={"folder_id": folder_id, "session_id": session_id},
    )

    resp = await client.get(
        f"/api/v1/bookmarks/folders/{folder_id}/sessions",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["sessions"][0]["id"] == session_id
    assert "bookmark_id" in data["sessions"][0]


@pytest.mark.asyncio
async def test_remove_bookmark(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Remove a bookmark."""
    session_id = await _upload_session(client, auth_headers, sample_sfs_tar)

    folder_resp = await client.post(
        "/api/v1/bookmarks/folders",
        headers=auth_headers,
        json={"name": "Temp"},
    )
    folder_id = folder_resp.json()["id"]

    bookmark_resp = await client.post(
        "/api/v1/bookmarks",
        headers=auth_headers,
        json={"folder_id": folder_id, "session_id": session_id},
    )
    bookmark_id = bookmark_resp.json()["id"]

    # Remove bookmark
    resp = await client.delete(
        f"/api/v1/bookmarks/{bookmark_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 204

    # Folder sessions should be empty
    resp = await client.get(
        f"/api/v1/bookmarks/folders/{folder_id}/sessions",
        headers=auth_headers,
    )
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_delete_folder_cascades(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Deleting a folder also removes its bookmarks."""
    session_id = await _upload_session(client, auth_headers, sample_sfs_tar)

    folder_resp = await client.post(
        "/api/v1/bookmarks/folders",
        headers=auth_headers,
        json={"name": "Will Delete"},
    )
    folder_id = folder_resp.json()["id"]

    await client.post(
        "/api/v1/bookmarks",
        headers=auth_headers,
        json={"folder_id": folder_id, "session_id": session_id},
    )

    # Delete folder
    resp = await client.delete(
        f"/api/v1/bookmarks/folders/{folder_id}",
        headers=auth_headers,
    )
    assert resp.status_code == 204

    # Folder should be gone
    resp = await client.get("/api/v1/bookmarks/folders", headers=auth_headers)
    folder_ids = [f["id"] for f in resp.json()["folders"]]
    assert folder_id not in folder_ids


@pytest.mark.asyncio
async def test_duplicate_bookmark_prevention(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Cannot bookmark the same session into the same folder twice."""
    session_id = await _upload_session(client, auth_headers, sample_sfs_tar)

    folder_resp = await client.post(
        "/api/v1/bookmarks/folders",
        headers=auth_headers,
        json={"name": "Dupes"},
    )
    folder_id = folder_resp.json()["id"]

    # First bookmark succeeds
    resp = await client.post(
        "/api/v1/bookmarks",
        headers=auth_headers,
        json={"folder_id": folder_id, "session_id": session_id},
    )
    assert resp.status_code == 201

    # Second bookmark is duplicate
    resp = await client.post(
        "/api/v1/bookmarks",
        headers=auth_headers,
        json={"folder_id": folder_id, "session_id": session_id},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_folder_list_includes_bookmark_count(client: AsyncClient, auth_headers: dict, sample_sfs_tar: bytes):
    """Folder list shows correct bookmark counts."""
    session_id = await _upload_session(client, auth_headers, sample_sfs_tar)

    folder_resp = await client.post(
        "/api/v1/bookmarks/folders",
        headers=auth_headers,
        json={"name": "Counted"},
    )
    folder_id = folder_resp.json()["id"]

    await client.post(
        "/api/v1/bookmarks",
        headers=auth_headers,
        json={"folder_id": folder_id, "session_id": session_id},
    )

    resp = await client.get("/api/v1/bookmarks/folders", headers=auth_headers)
    folders = resp.json()["folders"]
    counted = [f for f in folders if f["id"] == folder_id][0]
    assert counted["bookmark_count"] == 1
