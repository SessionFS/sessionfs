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

    ``status_code`` carries the remote HTTP status (or ``None`` for a transport
    error) so a caller can distinguish a missing issue (404) or a permission
    refusal (403) from a generic upstream failure without parsing the message.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
class TrackerProjectPage:
    """One page of a project search: the projects plus whether more matched."""

    projects: list[TrackerProject]
    has_more: bool


@dataclass
class TrackerBoard:
    """One Agile board (used to list its sprints)."""

    id: str
    name: str


@dataclass
class TrackerSprint:
    """One sprint surfaced by ``list_sprints``.

    ``board_name`` is the board this sprint was first seen on; the route dedupes
    across boards by ``id`` and keeps the first board's name.
    """

    id: str
    name: str
    state: str
    board_name: str
    start_date: str | None = None
    end_date: str | None = None


@dataclass
class TrackerIssue:
    """A lightweight view of a remote issue (enough to fill the link row).

    ``project_key`` / ``creator_account_id`` / ``reporter_account_id`` /
    ``created_at`` are only populated by the label search used for adoptions;
    they let the route refuse to adopt an issue that wasn't created by this
    connection in the mapped project after the unknown publish began.

    ``sprint_*`` and ``parent_key`` are only populated when the caller asks for
    them (``get_issue`` with the sprint/epic-link field ids); they feed the
    link's sprint + parent cache and are never written back to the ticket.
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
    sprint_id: str | None = None
    sprint_name: str | None = None
    sprint_state: str | None = None
    parent_key: str | None = None
    # Scheduling fields (GitHub #113). ``due_date`` is "YYYY-MM-DD" or None;
    # ``original_estimate`` is the Jira-duration string ("2d 4h", "30m").
    due_date: str | None = None
    original_estimate: str | None = None


@runtime_checkable
class TrackerProvider(Protocol):
    """What promotion and the Jira routes expect from a tracker client."""

    async def validate_connection(self) -> dict:
        """Probe the credentials. Raise ``TrackerError`` on failure."""
        ...

    async def list_projects(self) -> list[TrackerProject]:
        """All projects the credentials can see, with their issue types."""
        ...

    async def search_projects(
        self, query: str | None, limit: int
    ) -> TrackerProjectPage:
        """Up to ``limit`` projects whose key or name matches ``query``."""
        ...

    async def list_issue_types(self, project_key: str) -> list[TrackerIssueType]:
        """Issue types creatable in ``project_key``."""
        ...

    async def detect_fields(self) -> dict[str, str | None]:
        """Return ``{"sprint_field_id", "epic_link_field_id"}`` for the site.

        Both are custom-field ids discovered from the site's field list; either
        may be None when the field does not exist (epic-link in particular is
        absent on Jira Cloud, which uses ``parent`` instead).
        """
        ...

    async def list_boards(self, project_key: str, *, limit: int = 10) -> list[TrackerBoard]:
        """The first page of Agile boards scoped to ``project_key``."""
        ...

    async def list_sprints(self, board: TrackerBoard) -> list[TrackerSprint]:
        """Active and future sprints on ``board``."""
        ...

    async def move_issue_to_sprint(self, issue_key: str, sprint_id: int) -> None:
        """Move ``issue_key`` into the Agile sprint ``sprint_id``."""
        ...

    async def get_sprint(self, sprint_id: int) -> TrackerSprint:
        """Read one Agile sprint's name/state by id."""
        ...

    async def create_issue(
        self,
        project_key: str,
        issue_type: str,
        summary: str,
        description_adf: dict,
        *,
        labels: list[str] | None = None,
        extra_fields: dict[str, object] | None = None,
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
        *,
        extra_fields: dict[str, object] | None = None,
    ) -> TrackerIssue:
        """Replace summary/description only; never status/assignee/sprint.

        ``extra_fields`` are arbitrary custom-field values merged into the
        update (e.g. the sprint field id or epic-link parent).
        """
        ...

    async def get_issue(
        self,
        issue_key: str,
        *,
        sprint_field_id: str | None = None,
        epic_link_field_id: str | None = None,
    ) -> TrackerIssue:
        """Read one issue, including sprint/parent when the field ids are set."""
        ...
