"""The Google model id is one percent-encoded URL path segment.

The model id comes from user settings and is put into the googleapis URL
path. Unencoded, ``../`` or ``?``/``#``/``:`` could move the request to a
different path, method or query on the Google host.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from sessionfs.judge.agent_stream import stream_chat, user_message
from sessionfs.judge.providers import _call_google, google_model_segment

HOSTILE = "../../v1/files?key=x#frag:generateContent"


def test_google_model_segment_encodes_everything_reserved() -> None:
    assert google_model_segment("gemini-2.5-pro") == "gemini-2.5-pro"
    encoded = google_model_segment(HOSTILE)
    for ch in "/?#:":
        assert ch not in encoded, encoded
    assert encoded.startswith("..%2F..%2Fv1%2Ffiles%3Fkey%3Dx%23frag%3A")


@pytest.mark.asyncio
async def test_call_google_keeps_model_in_one_path_segment() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode()
        seen["query"] = request.url.query
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        )

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler), timeout=5)

    with patch("sessionfs.judge.providers.httpx.AsyncClient", side_effect=factory):
        assert await _call_google(HOSTILE, "sys", "hi", "k") == "ok"
    assert seen["path"].startswith("/v1beta/models/..%2F..%2Fv1%2Ffiles%3F")
    assert seen["path"].endswith(":generateContent")
    assert seen["query"] == b""


@pytest.mark.asyncio
async def test_google_stream_keeps_model_in_one_path_segment() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode()
        return httpx.Response(200, content=b"")

    def factory(timeout):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))

    with patch("sessionfs.judge.agent_stream._make_client", side_effect=factory):
        async for _ in stream_chat(
            model=HOSTILE,
            system="sys",
            messages=[user_message("hi")],
            tools=[],
            api_key="k",
            provider="google",
        ):
            pass
    assert seen["path"].startswith("/v1beta/models/..%2F..%2Fv1%2Ffiles%3F")
    assert ":streamGenerateContent" in seen["path"]
