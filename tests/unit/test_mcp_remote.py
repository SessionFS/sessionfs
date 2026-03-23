"""Tests for the remote MCP server cloud client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sessionfs.mcp.cloud_client import CloudAPIClient


@pytest.fixture
def client():
    return CloudAPIClient("https://api.test.dev")


def _mock_response(status: int = 200, data: dict | None = None) -> MagicMock:
    """Create a mock httpx.Response with sync .json() method."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = data or {}
    resp.raise_for_status = MagicMock()
    return resp


class TestCloudAPIClient:
    @pytest.mark.asyncio
    async def test_validate_key_success(self, client):
        with patch("httpx.AsyncClient.get", return_value=_mock_response(200)):
            assert await client.validate_key("sk_sfs_valid") is True

    @pytest.mark.asyncio
    async def test_validate_key_invalid(self, client):
        with patch("httpx.AsyncClient.get", return_value=_mock_response(401)):
            assert await client.validate_key("sk_sfs_bad") is False

    @pytest.mark.asyncio
    async def test_search(self, client):
        data = {"results": [{"session_id": "ses_abc", "title": "Auth debug"}], "total": 1}
        with patch("httpx.AsyncClient.get", return_value=_mock_response(200, data)):
            result = await client.search("sk_sfs_key", "auth error", tool_filter="claude-code")
            assert result["total"] == 1

    @pytest.mark.asyncio
    async def test_get_session(self, client):
        data = {"id": "ses_abc", "title": "Test"}
        with patch("httpx.AsyncClient.get", return_value=_mock_response(200, data)):
            result = await client.get_session("sk_sfs_key", "ses_abc")
            assert result["id"] == "ses_abc"

    @pytest.mark.asyncio
    async def test_get_messages(self, client):
        data = {"messages": [{"role": "user"}], "total": 1}
        with patch("httpx.AsyncClient.get", return_value=_mock_response(200, data)):
            result = await client.get_messages("sk_sfs_key", "ses_abc")
            assert len(result["messages"]) == 1

    @pytest.mark.asyncio
    async def test_list_sessions(self, client):
        data = {"sessions": [], "total": 0}
        with patch("httpx.AsyncClient.get", return_value=_mock_response(200, data)):
            result = await client.list_sessions("sk_sfs_key", page_size=5)
            assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_headers_include_bearer(self, client):
        headers = client._headers("sk_sfs_test123")
        assert headers["Authorization"] == "Bearer sk_sfs_test123"

    @pytest.mark.asyncio
    async def test_base_url_trailing_slash(self):
        c = CloudAPIClient("https://api.test.dev/")
        assert c.base_url == "https://api.test.dev"
