"""Thin SessionFS API client for the resident runner.

Calls run_work_queue_step and complete_work_queue_step, authed with the
org-profile's service key. Follows the same error-envelope pattern as
the CLI _api_request helper but is self-contained (no Typer dependency).

The service key is NEVER logged. The LLM key is NEVER sent here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("sessionfs.resident.client")


@dataclass
class ApiResponse:
    status_code: int
    body: Any  # dict | list | str
    headers: dict


async def _api_request(
    method: str,
    api_url: str,
    api_key: str,
    path: str,
    json_data: dict | None = None,
    timeout: int = 30,
) -> ApiResponse:
    """Make an authenticated request to the SessionFS API.

    Returns ApiResponse with status_code + parsed body.
    On network error, returns status_code=0 with the error in body.
    """
    url = f"{api_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            elif method == "POST":
                resp = await client.post(url, headers=headers, json=json_data)
            else:
                resp = await client.request(
                    method, url, headers=headers, json=json_data
                )
    except httpx.RequestError as exc:
        logger.error("API request failed (%s %s): %s", method, path, exc)
        return ApiResponse(
            status_code=0,
            body={"error": str(exc)},
            headers={},
        )

    if not resp.content:
        body: Any = ""
    elif resp.headers.get("content-type", "").startswith("application/json"):
        try:
            body = resp.json()
        except (ValueError, Exception):
            body = resp.text
    else:
        body = resp.text

    return ApiResponse(
        status_code=resp.status_code,
        body=body,
        headers=dict(resp.headers),
    )


async def run_work_queue_step(
    api_url: str,
    api_key: str,
    project_id: str,
    queue_id: str,
    wake_source: str = "resident",
    wake_ref: str = "",
) -> ApiResponse:
    """Call POST .../work-queues/{queue_id}/step — the heartbeat.

    Returns directives (if any) or idle/stopped status.
    """
    path = f"/api/v1/projects/{project_id}/work-queues/{queue_id}/step"
    body: dict = {"wake_source": wake_source}
    if wake_ref:
        body["wake_ref"] = wake_ref

    resp = await _api_request("POST", api_url, api_key, path, json_data=body)
    if resp.status_code == 429:
        logger.warning("Step rate-limited; backing off.")
    return resp


async def complete_work_queue_step(
    api_url: str,
    api_key: str,
    project_id: str,
    queue_id: str,
    *,
    item_id: str,
    directive_id: str,
    ticket_id: str,
    outcome: str,
    ticket_lease_epoch: int | None = None,
    verdict_content: str | None = None,
    verdict: str | None = None,
    comment_id: str | None = None,
    failed: bool = False,
    summary: str | None = None,
) -> ApiResponse:
    """Call POST .../step/complete — settle a directive.

    CRITICAL: the client NEVER sets author_persona or verdict_trusted.
    The server derives both from the authenticated identity + queue config.
    """
    path = f"/api/v1/projects/{project_id}/work-queues/{queue_id}/step/complete"
    body: dict = {
        "item_id": item_id,
        "directive_id": directive_id,
        "ticket_id": ticket_id,
        "outcome": outcome,
    }
    if ticket_lease_epoch is not None:
        body["ticket_lease_epoch"] = ticket_lease_epoch
    if verdict_content is not None:
        body["verdict_content"] = verdict_content
    if verdict is not None:
        body["verdict"] = verdict
    if comment_id is not None:
        body["comment_id"] = comment_id
    if failed:
        body["failed"] = True
    if summary is not None:
        body["summary"] = summary

    resp = await _api_request("POST", api_url, api_key, path, json_data=body)
    if resp.status_code == 409:
        logger.warning(
            "Settle 409 (stale lease) for directive %s on ticket %s; "
            "directive will re-emit next wake.",
            directive_id,
            ticket_id,
        )
    return resp


async def add_ticket_comment(
    api_url: str,
    api_key: str,
    project_id: str,
    ticket_id: str,
    content: str,
    author_persona: str | None = None,
    lease_epoch: int | None = None,
) -> ApiResponse:
    """Post a generic comment on a ticket via POST .../tickets/{id}/comments.

    Used by the implementer resident to post diff-ref comments (branch/SHA/
    changed-paths — metadata only, NEVER code contents per C7).

    Returns the created comment's id in the response body on success.
    """
    path = f"/api/v1/projects/{project_id}/tickets/{ticket_id}/comments"
    body: dict = {"content": content[:10000]}  # server cap
    if author_persona:
        body["author_persona"] = author_persona
    if lease_epoch is not None:
        body["lease_epoch"] = lease_epoch

    resp = await _api_request("POST", api_url, api_key, path, json_data=body)
    if resp.status_code in (200, 201):
        logger.info(
            "Comment posted on ticket=%s: id=%s",
            ticket_id,
            resp.body.get("id") if isinstance(resp.body, dict) else "?",
        )
    return resp
