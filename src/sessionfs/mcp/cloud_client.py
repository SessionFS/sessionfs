"""Cloud API client for the remote MCP server.

Thin HTTP wrapper that delegates to the SessionFS Cloud API on behalf
of an authenticated user. Used by remote_server.py to serve MCP tools
without direct database access.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("sessionfs.mcp.cloud_client")

DEFAULT_API_URL = "https://api.sessionfs.dev"
_TIMEOUT = 30.0


class CloudAPIClient:
    """HTTP client for the SessionFS Cloud API."""

    def __init__(self, base_url: str = DEFAULT_API_URL) -> None:
        self.base_url = base_url.rstrip("/")

    def _headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"}

    async def validate_key(self, api_key: str) -> bool:
        """Check if an API key is valid by calling /api/v1/sessions."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{self.base_url}/api/v1/sessions",
                    params={"page_size": 1},
                    headers=self._headers(api_key),
                )
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def search(
        self,
        api_key: str,
        query: str,
        tool_filter: str | None = None,
        days: int | None = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        """Full-text search across user's cloud sessions."""
        params: dict[str, Any] = {"q": query, "page_size": max_results}
        if tool_filter:
            params["tool"] = tool_filter
        if days:
            params["days"] = days
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self.base_url}/api/v1/sessions/search",
                params=params,
                headers=self._headers(api_key),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_session(self, api_key: str, session_id: str) -> dict[str, Any]:
        """Get session metadata."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self.base_url}/api/v1/sessions/{session_id}",
                headers=self._headers(api_key),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_messages(
        self,
        api_key: str,
        session_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Get paginated messages from a session."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self.base_url}/api/v1/sessions/{session_id}/messages",
                params={"page": page, "page_size": page_size},
                headers=self._headers(api_key),
            )
            resp.raise_for_status()
            return resp.json()

    async def get_project_context(self, api_key: str, git_remote_normalized: str) -> dict[str, Any] | None:
        """Get project context by normalized git remote."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(
                    f"{self.base_url}/api/v1/projects/{git_remote_normalized}",
                    headers=self._headers(api_key),
                )
                if resp.status_code == 404:
                    return None
                if resp.status_code == 403:
                    return None
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError:
            return None

    async def list_sessions(
        self,
        api_key: str,
        page: int = 1,
        page_size: int = 10,
        source_tool: str | None = None,
    ) -> dict[str, Any]:
        """List user's sessions."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if source_tool:
            params["source_tool"] = source_tool
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self.base_url}/api/v1/sessions",
                params=params,
                headers=self._headers(api_key),
            )
            resp.raise_for_status()
            return resp.json()
