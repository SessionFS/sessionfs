"""Server test fixtures — aiosqlite in-memory + LocalBlobStore."""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Skip all server tests if server dependencies are not installed
pytest.importorskip("fastapi", reason="Server tests require: pip install -e '.[dev]'")
pytest.importorskip("sqlalchemy", reason="Server tests require: pip install -e '.[dev]'")

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, Base, Session, User
from sessionfs.server.storage.local import LocalBlobStore


@pytest.fixture
async def db_engine():
    """Create an in-memory aiosqlite engine with all tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    """Async session per test."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
def blob_store(tmp_path: Path):
    """LocalBlobStore in a temp directory."""
    return LocalBlobStore(tmp_path / "blobs")


@pytest.fixture
async def test_user(db_session: AsyncSession) -> User:
    """Create and return a test user."""
    user = User(
        id=str(uuid.uuid4()),
        email="test@example.com",
        display_name="Test User",
        tier="pro",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def test_api_key(db_session: AsyncSession, test_user: User) -> tuple[str, ApiKey]:
    """Create an API key and return (raw_key, ApiKey)."""
    raw_key = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=test_user.id,
        key_hash=hash_api_key(raw_key),
        name="test-key",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)
    return raw_key, api_key


@pytest.fixture
def auth_headers(test_api_key: tuple[str, ApiKey]) -> dict[str, str]:
    """Authorization headers with the test API key."""
    raw_key = test_api_key[0]
    return {"Authorization": f"Bearer {raw_key}"}


@pytest.fixture
async def client(db_engine, blob_store, test_user, test_api_key):
    """httpx AsyncClient with dependency overrides."""
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.db.engine import get_db

    config = ServerConfig(database_url="sqlite+aiosqlite://")
    app = create_app(config)

    # Override DB dependency
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.state.blob_store = blob_store

    # Set module-level _session_factory so sync_push Phase 3 can create fresh sessions
    import sessionfs.server.db.engine as _engine_mod
    _engine_mod._session_factory = factory

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    # Reset to avoid polluting other tests
    _engine_mod._session_factory = None


@pytest.fixture
def sample_sfs_tar() -> bytes:
    """Create a minimal .sfs tar.gz for testing with full manifest metadata."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = json.dumps({
            "sfs_version": "0.1.0",
            "session_id": "ses_test123",
            "title": "Test session title",
            "tags": ["test", "fixture"],
            "source": {
                "tool": "claude-code",
                "tool_version": "1.0.0",
                "original_session_id": "abc-123-def",
            },
            "model": {
                "provider": "anthropic",
                "model_id": "claude-opus-4-6",
            },
            "stats": {
                "message_count": 5,
                "turn_count": 3,
                "tool_use_count": 2,
                "total_input_tokens": 1500,
                "total_output_tokens": 800,
                "duration_ms": 45000,
            },
        }).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

        msg1 = json.dumps({
            "msg_id": "msg_001", "role": "user",
            "content": [{"type": "text", "text": "hello"}],
            "timestamp": "2026-03-20T14:31:00Z",
        })
        msg2 = json.dumps({
            "msg_id": "msg_002", "role": "assistant",
            "content": [
                {"type": "thinking", "text": "Let me analyze..."},
                {"type": "text", "text": "Hi there!"},
                {"type": "tool_use", "tool_name": "bash", "tool_use_id": "tu_001",
                 "input": {"command": "ls"}},
            ],
            "timestamp": "2026-03-20T14:31:15Z",
            "parent_msg_id": "msg_001",
            "model_used": "claude-opus-4-6",
        })
        msg3 = json.dumps({
            "msg_id": "msg_003", "role": "tool",
            "content": [{"type": "tool_result", "tool_use_id": "tu_001",
                         "content": "file1.py\nfile2.py"}],
            "timestamp": "2026-03-20T14:31:16Z",
            "parent_msg_id": "msg_002",
        })
        messages = (msg1 + "\n" + msg2 + "\n" + msg3 + "\n").encode()
        info = tarfile.TarInfo(name="messages.jsonl")
        info.size = len(messages)
        tar.addfile(info, io.BytesIO(messages))

        workspace = json.dumps({
            "root_path": "/home/user/project",
            "git": {
                "remote_url": "https://github.com/example/repo",
                "branch": "main",
                "commit_sha": "abc123",
            },
            "files": [
                {"path": "src/main.py", "role": "edited"},
            ],
        }).encode()
        info = tarfile.TarInfo(name="workspace.json")
        info.size = len(workspace)
        tar.addfile(info, io.BytesIO(workspace))

        tools = json.dumps({
            "tools": [
                {"name": "bash", "type": "built-in"},
                {"name": "read", "type": "built-in"},
            ],
            "working_directory": "/home/user/project",
            "default_shell": "/bin/bash",
        }).encode()
        info = tarfile.TarInfo(name="tools.json")
        info.size = len(tools)
        tar.addfile(info, io.BytesIO(tools))

    return buf.getvalue()


@pytest.fixture
async def uploaded_session(
    db_session: AsyncSession, test_user: User, blob_store: LocalBlobStore, sample_sfs_tar: bytes
) -> Session:
    """Create a session record with blob already stored."""
    import hashlib

    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    key = f"sessions/{test_user.id}/{session_id}/session.tar.gz"
    await blob_store.put(key, sample_sfs_tar)

    now = datetime.now(timezone.utc)
    session = Session(
        id=session_id,
        user_id=test_user.id,
        title="Test Session",
        tags='["test"]',
        source_tool="claude-code",
        blob_key=key,
        blob_size_bytes=len(sample_sfs_tar),
        etag=hashlib.sha256(sample_sfs_tar).hexdigest(),
        created_at=now,
        updated_at=now,
        uploaded_at=now,
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session
