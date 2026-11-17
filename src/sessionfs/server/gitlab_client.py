"""GitLab API client — supports cloud and self-hosted instances."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
from urllib.parse import urlparse

import httpx

from sessionfs.server.config import ServerConfig
from sessionfs.server.services.oidc_fetch import (
    _METADATA_IPS,
    SsrfError,
    _validate_url,
)

logger = logging.getLogger("sessionfs.gitlab")


def gitlab_allow_private_hosts() -> bool:
    return ServerConfig().gitlab_allow_private_hosts


def validate_gitlab_instance_url(url: str, *, allow_private: bool | None = None) -> str:
    """Validate a GitLab instance URL against the SSRF policy (blocking DNS).

    Hosted mode (default): https only, every resolved address public.
    Self-hosted mode (SFS_GITLAB_ALLOW_PRIVATE_HOSTS): http or https and
    private/loopback hosts allowed; link-local and cloud-metadata addresses
    stay blocked. Credentials in the URL are always refused.

    Returns the URL without a trailing slash. Raises SsrfError.
    """
    if allow_private is None:
        allow_private = gitlab_allow_private_hosts()
    url = url.strip()
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise SsrfError("Credentials in the GitLab URL are not allowed")
    if parsed.query or parsed.fragment:
        raise SsrfError("The GitLab URL must not contain a query or fragment")
    if not allow_private:
        _validate_url(url)  # https-only + public addresses
        return url.rstrip("/")
    if parsed.scheme not in ("http", "https"):
        raise SsrfError(f"Unsupported scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise SsrfError("Could not parse hostname from the GitLab URL")
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = [ipaddress.ip_address(i) for i in _resolve_any(host)]
        except OSError as exc:
            raise SsrfError(f"DNS resolution failed for {host}: {exc}") from exc
    for ip in ips:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip in _METADATA_IPS or ip.is_link_local or ip.is_unspecified:
            raise SsrfError(f"Host {host} resolves to disallowed address {ip}")
    return url.rstrip("/")


def _resolve_any(host: str) -> list[str]:
    import socket

    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list({str(i[4][0]) for i in infos})


# (instance URL, sha256 of the token) -> the token's GitLab user id.
_TOKEN_USER_IDS: dict[tuple[str, str], int] = {}


class GitLabClient:
    """Client for GitLab API."""

    def __init__(self, base_url: str, access_token: str):
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token

    async def _guard(self) -> None:
        """Re-validate the instance host right before a request goes out, so a
        hostname that started resolving to an internal address after it was
        saved (DNS rebinding) is refused. The token never leaves on failure."""
        await asyncio.to_thread(validate_gitlab_instance_url, self.base_url)

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def post_mr_comment(self, project_id: int, mr_iid: int, body: str) -> dict:
        """Post a comment (note) on a merge request."""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes"
        await self._guard()
        async with self._client(30) as client:
            resp = await client.post(
                url,
                headers={"PRIVATE-TOKEN": self.access_token},
                json={"body": body},
            )
            resp.raise_for_status()
            return resp.json()

    async def current_user_id(self) -> int | None:
        """Id of the user this token belongs to (GET /user), cached per
        instance + token for the life of the process; None if unknown."""
        key = (self.base_url, hashlib.sha256(self.access_token.encode()).hexdigest())
        if key in _TOKEN_USER_IDS:
            return _TOKEN_USER_IDS[key]
        await self._guard()
        async with self._client(30) as client:
            resp = await client.get(
                f"{self.base_url}/api/v4/user",
                headers={"PRIVATE-TOKEN": self.access_token},
            )
            resp.raise_for_status()
            data = resp.json()
        user_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            return None
        if len(_TOKEN_USER_IDS) >= 1024:
            _TOKEN_USER_IDS.clear()
        _TOKEN_USER_IDS[key] = user_id
        return user_id

    async def find_mr_note_with_marker(
        self, project_id: int, mr_iid: int, marker: str, *, max_pages: int = 3
    ) -> int | None:
        """Id of the newest MR note that THIS token's user wrote and whose
        body contains ``marker``, looking at up to ``max_pages`` pages of 100
        notes (newest first); None if absent.

        The marker is predictable, so a note is only adopted when its author
        is the token's own user and it isn't a system note: a participant who
        pre-posts the marker can neither block our comment nor get their note
        overwritten.
        """
        own_id = await self.current_user_id()
        if own_id is None:
            return None
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes"
        await self._guard()
        async with self._client(30) as client:
            for page in range(1, max_pages + 1):
                resp = await client.get(
                    url,
                    headers={"PRIVATE-TOKEN": self.access_token},
                    params={
                        "per_page": 100,
                        "page": page,
                        "sort": "desc",
                        "order_by": "created_at",
                    },
                )
                resp.raise_for_status()
                notes = resp.json()
                if not isinstance(notes, list) or not notes:
                    return None
                for note in notes:
                    if not isinstance(note, dict) or note.get("system") is True:
                        continue
                    author = note.get("author")
                    if not isinstance(author, dict) or author.get("id") != own_id:
                        continue
                    if (
                        isinstance(note.get("body"), str)
                        and marker in note["body"]
                        and isinstance(note.get("id"), int)
                    ):
                        return note["id"]
                if len(notes) < 100:
                    return None
        return None

    async def update_mr_comment(self, project_id: int, mr_iid: int, note_id: int, body: str) -> dict:
        """Update an existing MR comment."""
        url = f"{self.base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes/{note_id}"
        await self._guard()
        async with self._client(30) as client:
            resp = await client.put(
                url,
                headers={"PRIVATE-TOKEN": self.access_token},
                json={"body": body},
            )
            resp.raise_for_status()
            return resp.json()

    async def post_commit_status(
        self,
        project_id: int,
        sha: str,
        state: str,
        *,
        name: str,
        description: str,
        target_url: str | None = None,
    ) -> dict:
        """Set a commit status (the Review Loop merge-readiness badge).

        The SSRF guard re-validates the instance host right before the request
        (DNS rebinding protection) and redirects are never followed, matching
        every other GitLab call.
        """
        url = f"{self.base_url}/api/v4/projects/{project_id}/statuses/{sha}"
        await self._guard()
        payload: dict[str, str] = {
            "state": state,
            "name": name,
            "description": description,
        }
        if target_url:
            payload["target_url"] = target_url
        async with self._client(15) as client:
            resp = await client.post(
                url,
                headers={"PRIVATE-TOKEN": self.access_token},
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def test_connection(self) -> bool:
        """Verify the access token works."""
        url = f"{self.base_url}/api/v4/user"
        try:
            await self._guard()
            async with self._client(10) as client:
                resp = await client.get(url, headers={"PRIVATE-TOKEN": self.access_token})
                return resp.status_code == 200
        except Exception:
            return False
