"""Base protocol for external documentation providers.

Confluence Cloud is the only implementation in the MVP. Rendering in
``promote.py`` and the routes in ``routes/confluence.py`` program against
``DocumentationProvider`` and never import a concrete client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class DocumentationError(Exception):
    """A sanitized, user-safe documentation-provider failure.

    ``message`` must never contain the API token, request headers, or any
    other credential. The Confluence client raises this after scrubbing the
    underlying transport error. ``status_code`` is the HTTP status when
    Confluence answered (None for transport failures); a 4xx is a definitive
    refusal, anything else leaves the outcome unknown.
    """

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DocumentationNotFound(DocumentationError):
    """The provider answered 404: the page (or space) does not exist or is
    no longer visible to the connection's account."""


@dataclass
class DocSpace:
    """A Confluence space the connection can see.

    ``id`` is the numeric space id the v2 API's space-scoped paths take;
    ``homepage_id`` is the space root page (never a valid mapping parent).
    """

    key: str
    name: str
    id: str = ""
    homepage_id: str | None = None


@dataclass
class DocSpacePage:
    """One page of a space search: the spaces plus whether more matched."""

    spaces: list[DocSpace]
    has_more: bool


@dataclass
class DocPage:
    """A lightweight view of a Confluence page (enough to fill the link row).

    ``id`` is the Confluence page id; ``version`` is its current version
    number (used to detect drift before an update); ``title`` is the page
    title. ``space_id``/``parent_id``/``author_id``/``created_at`` let the
    routes check a parent belongs to the mapped space and validate a page
    before adopting it. ``storage`` is only filled when the body was asked for.
    """

    id: str
    version: int
    title: str
    space_id: str | None = None
    parent_id: str | None = None
    author_id: str | None = None
    created_at: str | None = None
    storage: str | None = None


@dataclass
class DocChildPage:
    """A child page under a parent (for the space/page picker)."""

    id: str
    title: str


@runtime_checkable
class DocumentationProvider(Protocol):
    """What page sync and the Confluence routes expect from a client."""

    async def validate_connection(self) -> dict:
        """Probe the credentials. Raise ``DocumentationError`` on failure."""
        ...

    async def search_spaces(self, query: str | None, limit: int) -> DocSpacePage:
        """Up to ``limit`` spaces whose key or name matches ``query``."""
        ...

    async def get_space(self, space_key: str) -> DocSpace:
        """Resolve a space key to its id and homepage id."""
        ...

    async def search_pages(
        self, space_key: str, query: str | None, limit: int
    ) -> list[DocChildPage]:
        """Up to ``limit`` pages in ``space_key`` matching ``query``."""
        ...

    async def get_page(self, page_id: str, *, include_body: bool = False) -> DocPage:
        """Fetch the current page (id, version, title, space, parent, author)."""
        ...

    async def create_page(
        self,
        space_id: str,
        parent_page_id: str,
        title: str,
        storage: str,
    ) -> DocPage:
        ...

    async def add_label(self, page_id: str, label: str) -> None:
        """Stamp the SessionFS adoption label on a page."""
        ...

    async def update_page(
        self,
        page_id: str,
        current_version: int,
        title: str,
        storage: str,
    ) -> DocPage:
        """Replace title/storage; ``current_version`` is the remote version."""
        ...

    async def find_page_by_label(self, space_key: str, label: str) -> str | None:
        """Id of the page in the space carrying ``label``, or None."""
        ...

    async def find_page_by_title(self, space_id: str, title: str) -> str | None:
        """Id of the current page with exactly ``title`` in the space, or None."""
        ...
