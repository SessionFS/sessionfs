"""Base protocol for external ticket trackers.

Jira Cloud is the only implementation in the MVP; Linear / GitHub Projects /
Azure DevOps slot in behind the same interface. Promotion logic in
``promote.py`` and the routes in ``routes/jira.py`` program against
``TrackerProvider`` and never import a concrete client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class TrackerError(Exception):
    """A sanitized, user-safe tracker failure.

    ``message`` must never contain the API token, request headers, or any
    other credential. The Jira client raises this after scrubbing the
    underlying transport error.
    """


@dataclass
class TrackerIssueType:
    name: str
    id: str | None = None


@dataclass
class TrackerProject:
    key: str
    name: str
    issue_types: list[TrackerIssueType]


@dataclass
class TrackerIssue:
    """A lightweight view of a remote issue (enough to fill the link row).

    ``project_key`` / ``creator_account_id`` / ``reporter_account_id`` /
    ``created_at`` are only populated by the label search used for adoptions;
    they let the route refuse to adopt an issue that wasn't created by this
    connection in the mapped project after the unknown publish began.
    """

    key: str
    url: str
    status: str | None = None
    updated_at: str | None = None
    # The remote issue's numeric ``id`` — distinct from ``key`` and stored on
    # the link so a stale link can be identified without trusting the key.
    id: str | None = None
    project_key: str | None = None
    creator_account_id: str | None = None
    reporter_account_id: str | None = None
    created_at: str | None = None


@runtime_checkable
class TrackerProvider(Protocol):
    """What promotion and the Jira routes expect from a tracker client."""

    async def validate_connection(self) -> dict:
        """Probe the credentials. Raise ``TrackerError`` on failure."""
        ...

    async def list_projects(self) -> list[TrackerProject]:
        """All projects the credentials can see, with their issue types."""
        ...

    async def list_issue_types(self, project_key: str) -> list[TrackerIssueType]:
        """Issue types creatable in ``project_key``."""
        ...

    async def create_issue(
        self,
        project_key: str,
        issue_type: str,
        summary: str,
        description_adf: dict,
        *,
        labels: list[str] | None = None,
    ) -> TrackerIssue:
        ...

    async def search_issue_by_label(
        self, project_key: str, label: str
    ) -> TrackerIssue | None:
        """Find an issue by its ``sessionfs-<ticket_id>`` label, or None."""
        ...

    async def update_issue(
        self,
        issue_key: str,
        summary: str,
        description_adf: dict,
    ) -> TrackerIssue:
        """Replace summary/description only; never status/assignee/sprint."""
        ...

    async def get_issue(self, issue_key: str) -> TrackerIssue:
        ...
