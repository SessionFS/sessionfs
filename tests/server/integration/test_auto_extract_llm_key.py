"""_auto_extract_knowledge's LLM key path (auto-narrative extraction on sync).

Regression: the background task imported the removed module
``sessionfs.security.encryption``, so every sync with auto_narrative on logged
"LLM knowledge extraction failed" and never ran the extraction.
"""

from __future__ import annotations

import io
import json
import tarfile
from types import SimpleNamespace

import pytest

from sessionfs.server.db.models import UserJudgeSettings
from sessionfs.server.llm_keys import encrypt_llm_key
from sessionfs.server.routes import sessions as sessions_mod


def _archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in {
            "messages.jsonl": json.dumps({"role": "user", "content": "hi"}) + "\n",
            "manifest.json": json.dumps({"session_id": "ses_x"}),
        }.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def wired(monkeypatch, db_session):
    calls: list[dict] = []

    async def fake_get_db():
        yield db_session

    async def fake_resolve(db, remote):
        return SimpleNamespace(id="proj_x", auto_narrative=True)

    async def no_restrict(db, project):
        return False

    async def noop_extract(*a, **k):
        return None

    async def capture(**kwargs):
        calls.append(kwargs)

    import sessionfs.server.auth.project_access as pa
    import sessionfs.server.db.engine as engine
    import sessionfs.server.services.knowledge as kn
    import sessionfs.server.services.project_resolver as pr

    monkeypatch.setattr(engine, "get_db", fake_get_db)
    monkeypatch.setattr(pr, "resolve_project_by_remote", fake_resolve)
    monkeypatch.setattr(pa, "project_restricts_visibility", no_restrict)
    monkeypatch.setattr(kn, "extract_knowledge_entries", noop_extract)
    monkeypatch.setattr(kn, "extract_knowledge_with_llm", capture)
    return calls


async def _judge(db, user_id: str, base_url: str | None) -> None:
    db.add(
        UserJudgeSettings(
            user_id=user_id,
            provider="openai",
            model="gpt-x",
            encrypted_api_key=encrypt_llm_key("sk-saved"),
            base_url=base_url,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_saved_key_decrypts_and_reaches_extraction(wired, db_session, test_user):
    await _judge(db_session, test_user.id, None)
    await sessions_mod._auto_extract_knowledge(
        "ses_x", _archive(), "github.com/a/b", test_user.id
    )
    assert len(wired) == 1
    call = wired[0]
    assert call["api_key"] == "sk-saved"
    assert call["provider"] == "openai"
    assert call["model"] == "gpt-x"
    assert call["base_url"] is None


@pytest.mark.asyncio
async def test_saved_endpoint_refused_by_ssrf_guard_skips_extraction(
    wired, db_session, test_user, monkeypatch
):
    from sessionfs.server import llm_url_guard

    def refuse(url, **_):
        raise llm_url_guard.SsrfError("private")

    monkeypatch.setattr(llm_url_guard, "validate_llm_base_url", refuse)
    await _judge(db_session, test_user.id, "http://10.0.0.5/v1")
    await sessions_mod._auto_extract_knowledge(
        "ses_x", _archive(), "github.com/a/b", test_user.id
    )
    assert wired == []


@pytest.mark.asyncio
async def test_saved_endpoint_passes_guard_and_is_used(
    wired, db_session, test_user, monkeypatch
):
    from sessionfs.server import llm_url_guard

    monkeypatch.setattr(
        llm_url_guard, "validate_llm_base_url", lambda url, **_: url.rstrip("/")
    )
    await _judge(db_session, test_user.id, "https://llm.example.com/v1/")
    await sessions_mod._auto_extract_knowledge(
        "ses_x", _archive(), "github.com/a/b", test_user.id
    )
    assert len(wired) == 1
    assert wired[0]["base_url"] == "https://llm.example.com/v1"
    assert wired[0]["api_key"] == "sk-saved"
