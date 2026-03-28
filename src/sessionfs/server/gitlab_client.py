"""GitLab API client — supports cloud and self-hosted instances."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("sessionfs.gitlab")


class GitLabClient:
    """Client for GitLab API."""

    def __init__(self, base_url: str, access_token: str):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token

    async def post_mr_comment(self, project_id: int, mr_iid: int, body: str) -> dict:
        """Post a comment (note) on a merge request."""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={"PRIVATE-TOKEN": self.access_token},
                json={"body": body},
            )
            resp.raise_for_status()
            return resp.json()

    async def update_mr_comment(self, project_id: int, mr_iid: int, note_id: int, body: str) -> dict:
        """Update an existing MR comment."""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes/{note_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(
                url,
                headers={"PRIVATE-TOKEN": self.access_token},
                json={"body": body},
            )
            resp.raise_for_status()
            return resp.json()

    async def test_connection(self) -> bool:
        """Verify the access token works."""
        url = f"{self.base_url}/api/v4/user"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers={"PRIVATE-TOKEN": self.access_token})
                return resp.status_code == 200
        except Exception:
            return False
