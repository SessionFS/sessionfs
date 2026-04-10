"""End-to-end integration test: capture → push → pull → resume.

This test validates the entire SessionFS value proposition in a single flow.
It uses an in-memory test server and temporary local stores to simulate
the full pipeline across two machines.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="Server tests require: pip install -e '.[dev]'")
pytest.importorskip("sqlalchemy", reason="Server tests require: pip install -e '.[dev]'")

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sessionfs.server.app import create_app
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.config import ServerConfig
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import ApiKey, Base, User
from sessionfs.server.storage.local import LocalBlobStore
from sessionfs.store.local import LocalStore
from sessionfs.sync.archive import pack_session, unpack_session
from sessionfs import __version__
from sessionfs.sync.client import SyncClient


@pytest.fixture
async def pipeline_env(tmp_path: Path):
    """Set up the full pipeline environment: test server + two local stores."""
    # --- Server setup ---
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    blob_store = LocalBlobStore(tmp_path / "server_blobs")

    # Create test user + API key
    raw_key = generate_api_key()
    async with factory() as session:
        user = User(
            id=str(uuid.uuid4()),
            email="pipeline@test.com",
            display_name="Pipeline Tester",
            tier="pro",
            created_at=datetime.now(timezone.utc),
            email_verified=True,
        )
        session.add(user)
        await session.commit()

        api_key = ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw_key),
            name="pipeline-key",
            created_at=datetime.now(timezone.utc),
        )
        session.add(api_key)
        await session.commit()

    # Create FastAPI app with overrides
    config = ServerConfig(database_url="sqlite+aiosqlite://")
    app = create_app(config)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.state.blob_store = blob_store

    # Set module-level _session_factory so sync_push Phase 3 can create fresh sessions
    import sessionfs.server.db.engine as _engine_mod
    _engine_mod._session_factory = factory

    transport = ASGITransport(app=app)
    http_client = AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {raw_key}",
            "User-Agent": f"sessionfs-cli/{__version__}",
        },
    )

    # --- Local stores (simulating two machines) ---
    store_a_dir = tmp_path / "machine_a" / ".sessionfs"
    store_b_dir = tmp_path / "machine_b" / ".sessionfs"

    store_a = LocalStore(store_a_dir)
    store_a.initialize()

    store_b = LocalStore(store_b_dir)
    store_b.initialize()

    yield {
        "http_client": http_client,
        "raw_key": raw_key,
        "store_a": store_a,
        "store_b": store_b,
        "tmp_path": tmp_path,
    }

    store_a.close()
    store_b.close()
    await http_client.aclose()
    await engine.dispose()
    # Reset module-level _session_factory to avoid polluting other tests
    _engine_mod._session_factory = None


def _create_sample_sfs_session(store: LocalStore, session_id: str) -> Path:
    """Create a sample .sfs session in the store."""
    session_dir = store.allocate_session_dir(session_id)
    manifest = {
        "sfs_version": "0.1.0",
        "session_id": session_id,
        "title": "Pipeline Test Session",
        "source": {
            "tool": "claude-code",
            "tool_version": "1.0.0",
            "original_session_id": session_id,
        },
        "model": {
            "provider": "anthropic",
            "model_id": "claude-opus-4-6",
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "message_count": 3,
            "turn_count": 2,
            "tool_use_count": 1,
            "total_input_tokens": 500,
            "total_output_tokens": 200,
        },
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "Explain the auth middleware"}],
            "timestamp": "2024-01-15T10:00:00Z",
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "The auth middleware validates JWT tokens..."}],
            "timestamp": "2024-01-15T10:00:05Z",
            "model": "claude-opus-4-6",
        },
        {
            "role": "user",
            "content": [{"type": "text", "text": "Can you refactor it to use API keys?"}],
            "timestamp": "2024-01-15T10:01:00Z",
        },
    ]
    with open(session_dir / "messages.jsonl", "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")

    store.upsert_session_metadata(session_id, manifest, str(session_dir))
    return session_dir


def _make_test_client(env: dict) -> SyncClient:
    """Create a SyncClient wired to the test server."""
    client = SyncClient("http://localhost:8000", env["raw_key"])
    client.api_url = "http://test"
    client._client = env["http_client"]
    return client


@pytest.mark.asyncio
async def test_full_pipeline(pipeline_env):
    """Test the complete pipeline: capture → push → pull → verify.

    Simulates:
    1. Machine A captures a CC session into .sfs
    2. Machine A pushes to server
    3. Machine B pulls from server
    4. Verify the session content matches exactly
    """
    env = pipeline_env
    store_a: LocalStore = env["store_a"]
    store_b: LocalStore = env["store_b"]
    raw_key: str = env["raw_key"]

    session_id = f"ses_{uuid.uuid4().hex[:16]}"

    # Step 1: Create session on Machine A (simulates daemon capture)
    session_dir_a = _create_sample_sfs_session(store_a, session_id)
    assert (session_dir_a / "manifest.json").exists()
    assert (session_dir_a / "messages.jsonl").exists()

    # Step 2: Pack and push to server
    archive_data = pack_session(session_dir_a)
    assert len(archive_data) > 0

    client = _make_test_client(env)

    push_result = await client.push_session(session_id, archive_data)
    assert push_result.session_id == session_id
    assert push_result.etag
    assert push_result.blob_size_bytes == len(archive_data)
    assert push_result.created is True

    # Step 3: Pull from server (simulates Machine B)
    pull_result = await client.pull_session(session_id)
    assert pull_result.data is not None
    assert pull_result.etag == push_result.etag
    assert not pull_result.not_modified

    # Step 4: Unpack on Machine B
    session_dir_b = store_b.allocate_session_dir(session_id)
    unpack_session(pull_result.data, session_dir_b)

    # Step 5: Verify content matches
    manifest_a = json.loads((session_dir_a / "manifest.json").read_text())
    manifest_b = json.loads((session_dir_b / "manifest.json").read_text())
    assert manifest_a == manifest_b

    messages_a = (session_dir_a / "messages.jsonl").read_text()
    messages_b = (session_dir_b / "messages.jsonl").read_text()
    assert messages_a == messages_b

    # Step 6: Verify pull with etag returns 304
    cached_result = await client.pull_session(session_id, etag=push_result.etag)
    assert cached_result.not_modified is True


@pytest.mark.asyncio
async def test_push_update_with_etag(pipeline_env):
    """Test updating an existing session with ETag conflict detection."""
    env = pipeline_env
    store_a: LocalStore = env["store_a"]
    raw_key: str = env["raw_key"]

    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    session_dir = _create_sample_sfs_session(store_a, session_id)

    client = _make_test_client(env)

    # First push
    archive_v1 = pack_session(session_dir)
    result_v1 = await client.push_session(session_id, archive_v1)
    assert result_v1.created is True

    # Add a message (simulate session continuing)
    with open(session_dir / "messages.jsonl", "a") as f:
        f.write(json.dumps({
            "role": "assistant",
            "content": [{"type": "text", "text": "Here's the refactored version..."}],
            "timestamp": "2024-01-15T10:02:00Z",
        }) + "\n")

    # Second push with correct etag
    archive_v2 = pack_session(session_dir)
    result_v2 = await client.push_session(session_id, archive_v2, etag=result_v1.etag)
    assert result_v2.created is False
    assert result_v2.etag != result_v1.etag

    # Pull and verify it's the updated version
    pull_result = await client.pull_session(session_id)
    assert pull_result.data is not None

    # Unpack and check message count
    unpack_dir = env["tmp_path"] / "verify_update"
    unpack_session(pull_result.data, unpack_dir)
    messages = (unpack_dir / "messages.jsonl").read_text().strip().split("\n")
    assert len(messages) == 4  # Original 3 + 1 appended


@pytest.mark.asyncio
async def test_conflict_detection(pipeline_env):
    """Test that concurrent pushes from two machines trigger conflict."""
    env = pipeline_env
    store_a: LocalStore = env["store_a"]
    store_b: LocalStore = env["store_b"]
    raw_key: str = env["raw_key"]

    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    session_dir_a = _create_sample_sfs_session(store_a, session_id)

    client = _make_test_client(env)

    # Machine A pushes v1
    archive_v1 = pack_session(session_dir_a)
    result_v1 = await client.push_session(session_id, archive_v1)

    # Machine B also has the session (simulated)
    session_dir_b = _create_sample_sfs_session(store_b, session_id)

    # Machine A pushes v2 (with correct etag)
    with open(session_dir_a / "messages.jsonl", "a") as f:
        f.write('{"role": "user", "content": [{"type": "text", "text": "from machine A"}]}\n')
    archive_v2a = pack_session(session_dir_a)
    result_v2 = await client.push_session(session_id, archive_v2a, etag=result_v1.etag)

    # Machine B tries to push with v1's etag → conflict
    with open(session_dir_b / "messages.jsonl", "a") as f:
        f.write('{"role": "user", "content": [{"type": "text", "text": "from machine B"}]}\n')
    archive_v2b = pack_session(session_dir_b)

    from sessionfs.sync.client import SyncConflictError

    with pytest.raises(SyncConflictError) as exc_info:
        await client.push_session(session_id, archive_v2b, etag=result_v1.etag)

    assert exc_info.value.current_etag == result_v2.etag


@pytest.mark.asyncio
async def test_list_remote_sessions(pipeline_env):
    """Test listing remote sessions and checking local presence."""
    env = pipeline_env
    store_a: LocalStore = env["store_a"]
    raw_key: str = env["raw_key"]

    client = _make_test_client(env)

    # Push two sessions
    for i in range(2):
        sid = f"ses_{uuid.uuid4().hex[:16]}"
        session_dir = _create_sample_sfs_session(store_a, sid)
        archive = pack_session(session_dir)
        await client.push_session(sid, archive)

    # List remote
    result = await client.list_remote_sessions()
    assert result.total == 2
    assert len(result.sessions) == 2
