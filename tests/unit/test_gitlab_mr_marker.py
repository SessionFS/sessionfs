"""GitLab MR note adoption only trusts our own notes (tk_e3eecad8c3874d92).

The hidden marker is predictable, so a note is adopted only when the token's
own user wrote it and it isn't a system note. A participant who pre-posts the
marker can't block our comment or get their note overwritten.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from sessionfs.server import gitlab_client
from sessionfs.server.gitlab_client import GitLabClient

MARKER = "<!-- sessionfs-mr:a/b:1 -->"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.delenv("SFS_GITLAB_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    gitlab_client._TOKEN_USER_IDS.clear()


def _mock(monkeypatch, notes: list[dict], own_id: int = 42) -> list[str]:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/api/v4/user"):
            return httpx.Response(200, json={"id": own_id})
        if request.url.path.endswith("/notes") and request.method == "GET":
            return httpx.Response(200, json=notes)
        if request.method == "POST":
            return httpx.Response(201, json={"id": 999})
        return httpx.Response(200, json={"id": 1})

    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return paths


async def test_a_participants_note_with_the_marker_is_ignored(monkeypatch):
    _mock(monkeypatch, [
        {"id": 5, "body": f"gotcha {MARKER}", "author": {"id": 7}, "system": False},
        {"id": 6, "body": f"{MARKER}", "author": {"id": 42}, "system": True},
    ])
    client = GitLabClient("https://gitlab.example.com", "tok")
    assert await client.find_mr_note_with_marker(1, 1, MARKER) is None


async def test_our_own_note_is_adopted_and_the_user_id_is_cached(monkeypatch):
    paths = _mock(monkeypatch, [
        {"id": 5, "body": f"gotcha {MARKER}", "author": {"id": 7}},
        {"id": 8, "body": f"ours {MARKER}", "author": {"id": 42}, "system": False},
    ])
    client = GitLabClient("https://gitlab.example.com", "tok")
    assert await client.find_mr_note_with_marker(1, 1, MARKER) == 8
    assert await GitLabClient("https://gitlab.example.com", "tok").find_mr_note_with_marker(
        1, 1, MARKER
    ) == 8
    assert sum(p.endswith("/api/v4/user") for p in paths) == 1


async def test_handler_posts_its_own_note_when_only_a_planted_one_exists(
    monkeypatch, tmp_path
):
    """End to end through the client: a planted note is ignored, so the
    caller posts its own note."""
    paths = _mock(monkeypatch, [
        {"id": 5, "body": f"gotcha {MARKER}", "author": {"id": 7}},
    ])
    client = GitLabClient("https://gitlab.example.com", "tok")
    note_id = await client.find_mr_note_with_marker(1, 1, MARKER)
    assert note_id is None
    posted = await client.post_mr_comment(1, 1, f"ours {MARKER}")
    assert posted == {"id": 999}
    assert not any(p.endswith("/notes/5") for p in paths)
