"""Client for the hosted SessionFS Marketplace.

Everything that talks to `/api/v1/marketplace/*`, org skill assignments and
approved install requests lives here so the CLI commands stay thin:

* catalog search (`sfs skill search` hosted section),
* `sessionfs:<namespace>/<name>[@version]` resolution + hash-verified download
  into the immutable content store (`sfs skill add`, `sync --locked`,
  `update`),
* the local revocation feed (blocked / yanked / restored versions) and the
  install guard that refuses revoked versions,
* `sfs skill pull` / daemon reconciliation of org assignments and the
  caller's approved install requests.

Hosted packages are recorded with a `hosted` source identity:
``source_type="hosted"``, ``locator="sessionfs:<server package id>"`` (the
stable identity), ``commit=<server version id>`` (provenance, like a Git
commit) and ``listing_name="<namespace>/<name>"`` (display only).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import tempfile
import threading
import uuid
import time
import zlib
import contextlib
import functools
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Iterator

from sessionfs.skills.models import (
    SUPPORTED_TOOLS,
    PackageRecord,
    SkillError,
    SourceIdentity,
    clean_untrusted_text,
    validate_device_id,
)
from sessionfs.skills.package import fits_ustar, import_package
from sessionfs.skills.path_safety import CasefoldCollisionTracker
from sessionfs.skills.state import atomic_json, interprocess_lock, ordered_lock, read_json

if TYPE_CHECKING:  # pragma: no cover
    import httpx

    from sessionfs.skills.manager import SkillManager

HOSTED_PREFIX = "sessionfs:"
HOSTED_SOURCE_TYPE = "hosted"

_PACKAGE_ID_RE = re.compile(r"^spk_[0-9a-f]{24}$")
_VERSION_ID_RE = re.compile(r"^skv_[0-9a-f]{24}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,98}[a-z0-9])?$")
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")
_VERSION_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

# Download / extraction bounds (mirror the server's hosted-package limits,
# with headroom for the gzip + tar envelope).
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_MEMBERS = 10_000
MAX_PATH_DEPTH = 16
SEARCH_PAGE_LIMIT = 100
MAX_SEARCH_PAGES = 10
# Safety budget for one feed sync (500 events per page = 1,000,000 events).
# Hitting it leaves the stored cursor and flags untouched (reported as
# incomplete) instead of committing a truncated replay.
REVOCATION_PAGE_SIZE = 500
MAX_REVOCATION_PAGES = 2_000

# Local consent requirement -> server capability an install request must
# have been approved for. Fail closed: a requirement that is not in this map
# (e.g. "dlp-findings", which the server has no capability for) can never be
# authorized by an install request; only an org assignment (which carries no
# capability list) plus the local flag can authorize it.
_REQUIREMENT_TO_SERVER_CAPABILITY = {
    "skill-scripts": "scripts",
    "skill-hooks": "hooks",
    # hooks.json / hooks/ inside a skill directory: hook extensions.
    "executable-extensions": "hooks",
    "mcp": "mcp",
}


class HostedUnavailable(SkillError):
    """The hosted marketplace cannot be reached or the caller is signed out."""


class ServiceKeyNotAllowed(HostedUnavailable):
    """The route refused a service key (user-only endpoint)."""


@dataclass(frozen=True)
class ApiError:
    status: int
    code: str
    message: str
    details: dict[str, Any]


_NUMERIC_CODE_RE = re.compile(r"^\d{3}$")


def _is_route_missing(response: Any) -> bool:
    """A 404 that means "this endpoint does not exist" (older server) rather
    than the resolve route's constant "Project not found" answer."""
    error = parse_api_error(response)
    return error.message.strip().casefold() != "project not found"


def parse_api_error(response: Any) -> ApiError:
    """The ONE parser for SessionFS API error bodies.

    Understands every shape the server has produced:

    * current envelope ``{"error": {"code", "message", "details"}}`` with a
      semantic code (``"service_key_not_allowed"``);
    * the same envelope with a numeric code (``"403"``) and the semantic
      code in ``details.error`` / ``details.code`` (older servers);
    * the legacy FastAPI ``{"detail": {...}}`` / ``{"detail": "text"}``.

    The returned `code` is the most specific semantic code available.
    """
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        body = response.json()
    except (ValueError, AttributeError):
        body = None
    code = ""
    message = ""
    details: dict[str, Any] = {}
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        envelope = body["error"]
        code = str(envelope.get("code") or "")
        message = str(envelope.get("message") or "")
        raw_details = envelope.get("details")
        details = raw_details if isinstance(raw_details, dict) else {}
    elif isinstance(body, dict) and "detail" in body:
        legacy = body["detail"]
        if isinstance(legacy, dict):
            details = legacy
            code = str(legacy.get("code") or legacy.get("error") or "")
            message = str(legacy.get("message") or "")
        elif isinstance(legacy, str):
            message = legacy
    if not code or _NUMERIC_CODE_RE.fullmatch(code):
        semantic = details.get("error") or details.get("code")
        if isinstance(semantic, str) and semantic and not _NUMERIC_CODE_RE.fullmatch(semantic):
            code = semantic
    if not code:
        code = str(status) if status else ""
    return ApiError(
        status=status,
        code=clean_untrusted_text(code, limit=100, keep_newlines=False),
        message=clean_untrusted_text(message, limit=200, keep_newlines=False),
        details=details,
    )


# ---------------------------------------------------------------------------
# Locator parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostedRef:
    namespace: str | None
    name: str | None
    package_id: str | None
    version: str | None

    @property
    def display(self) -> str:
        base = self.package_id or f"{self.namespace}/{self.name}"
        return f"sessionfs:{base}" + (f"@{self.version}" if self.version else "")


def is_hosted_ref(source: str) -> bool:
    return source.startswith(HOSTED_PREFIX)


def parse_hosted_ref(source: str) -> HostedRef:
    """`sessionfs:<namespace>/<name>[@version]` or `sessionfs:spk_<id>[@version]`."""
    if not is_hosted_ref(source):
        raise SkillError(f"Not a hosted marketplace reference: {source}")
    body = source[len(HOSTED_PREFIX):]
    version: str | None = None
    if "@" in body:
        body, version = body.rsplit("@", 1)
        if not _VERSION_LABEL_RE.fullmatch(version) and not _VERSION_ID_RE.fullmatch(version):
            raise SkillError(f"Invalid hosted package version: {version!r}")
    if _PACKAGE_ID_RE.fullmatch(body):
        return HostedRef(namespace=None, name=None, package_id=body, version=version)
    namespace, sep, name = body.partition("/")
    if not sep or not _NAMESPACE_RE.fullmatch(namespace) or not _NAME_RE.fullmatch(name):
        raise SkillError(
            "Hosted packages are referenced as sessionfs:<namespace>/<name>[@version] "
            "(lowercase identifiers) or sessionfs:spk_<id>[@version]"
        )
    return HostedRef(namespace=namespace, name=name, package_id=None, version=version)


def canonical_origin(api_url: str) -> str:
    """Canonical API origin a hosted package is bound to: scheme + lowercase
    host + non-default port + path prefix, no trailing slash/query/fragment."""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(str(api_url).strip())
        port = parts.port
    except ValueError as exc:
        raise SkillError(f"Invalid SessionFS API URL: {api_url!r}") from exc
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise SkillError(f"Invalid SessionFS API URL: {api_url!r}")
    if ":" in host:
        host = f"[{host}]"
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path.rstrip("/"), "", ""))


def hosted_identity(item: dict[str, Any], origin: str) -> SourceIdentity:
    """Source identity recorded for a hosted package version (state + lockfile).

    The locator binds the package to the server it came from
    (``sessionfs:<package id>#<canonical origin>``): update, locked sync and
    re-downloads only ever talk to that server, and the same package id on
    another server is a different package.
    """
    package_id = str(item.get("package_id") or "")
    version_id = str(item.get("version_id") or "")
    if not _PACKAGE_ID_RE.fullmatch(package_id) or not _VERSION_ID_RE.fullmatch(version_id):
        raise SkillError("Hosted marketplace returned an invalid package or version id")
    namespace = clean_untrusted_text(item.get("namespace"), limit=100, keep_newlines=False)
    name = clean_untrusted_text(item.get("name"), limit=64, keep_newlines=False)
    return SourceIdentity(
        source_type=HOSTED_SOURCE_TYPE,
        locator=f"{HOSTED_PREFIX}{package_id}#{canonical_origin(origin)}",
        commit=version_id,
        listing_name=f"{namespace}/{name}" if namespace and name else None,
    )


def _split_locator(source: dict[str, Any] | SourceIdentity | None) -> tuple[str | None, str | None]:
    if isinstance(source, SourceIdentity):
        source = source.as_dict()
    if not isinstance(source, dict) or source.get("source_type") != HOSTED_SOURCE_TYPE:
        return None, None
    locator = str(source.get("locator") or "")
    if not locator.startswith(HOSTED_PREFIX):
        return None, None
    body = locator[len(HOSTED_PREFIX):]
    package_id, _, origin = body.partition("#")
    if not _PACKAGE_ID_RE.fullmatch(package_id):
        return None, None
    return package_id, (origin or None)


def hosted_package_id(source: dict[str, Any] | SourceIdentity | None) -> str | None:
    """Server package id of a hosted source row, else None."""
    return _split_locator(source)[0]


def hosted_origin(source: dict[str, Any] | SourceIdentity | None) -> str | None:
    """API origin a hosted source is bound to; None for a legacy unbound row."""
    return _split_locator(source)[1]


SEARCH_FILTERS = frozenset({
    "category", "publisher", "verified", "attested", "license", "max_severity",
    "capability", "works_in", "updated_within", "include_deprecated", "sort",
})
USAGE_REPORTING_ENV = "SFS_SKILL_USAGE_REPORTING"


_USAGE_OFF = ("0", "false", "off", "no")
_USAGE_ON = ("1", "true", "on", "yes")


def _usage_opted_out() -> bool:
    raw = os.environ.get(USAGE_REPORTING_ENV, "").strip().casefold()
    if raw in _USAGE_OFF:
        return True
    try:
        from sessionfs.telemetry import telemetry_enabled

        return not telemetry_enabled()
    except Exception:  # noqa: BLE001 - an unreadable config opts out
        return True


def usage_reporting_enabled(*, interactive: bool = False) -> bool:
    """Whether install counts may be sent now. Opt-outs always win:
    SFS_SKILL_USAGE_REPORTING=0/false/off/no, SFS_NO_TELEMETRY, or
    `telemetry.enabled = false`. Otherwise counts are sent only once the
    install-count notice has been SHOWN (its own marker, separate from the
    telemetry notice) or SFS_SKILL_USAGE_REPORTING=1 is set explicitly. An
    ``interactive`` CLI command on a terminal shows the notice (once) and
    then may send; the daemon and non-terminal runs never show it and send
    nothing until then (fail closed)."""
    if _usage_opted_out():
        return False
    if os.environ.get(USAGE_REPORTING_ENV, "").strip().casefold() in _USAGE_ON:
        return True
    try:
        from sessionfs.telemetry import (
            SKILL_USAGE_NOTICE,
            mark_skill_usage_disclosure_shown,
            skill_usage_disclosure_shown,
        )

        if skill_usage_disclosure_shown():
            return True
        if interactive and sys.stderr.isatty():
            print(SKILL_USAGE_NOTICE, file=sys.stderr)
            mark_skill_usage_disclosure_shown()
            return True
    except Exception:  # noqa: BLE001 - unsure means no counts
        return False
    return False


def usage_reporting_awaiting_notice() -> bool:
    """Not opted out, but counts are held until the notice is shown."""
    return not _usage_opted_out() and not usage_reporting_enabled()


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _assert_safe_api_path(path: str) -> None:
    """Last line of defense before any request carrying the user's token:
    the path must be an `/api/v1/` path whose segments cannot redirect it
    (no `.`/`..` segment - also percent-decoded -, no encoded separator, no
    backslash, `?`, `#` or control character). Every caller builds paths
    only from validated ids or `sessionfs.url_path_safety` keys; this
    refuses anything else without making a request."""
    from urllib.parse import unquote

    bad = (
        not path.startswith("/api/v1/")
        or any(char in path for char in "?#\\")
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    )
    if not bad:
        for segment in path.split("/")[1:]:
            decoded = unquote(segment)
            if (
                not segment
                or segment in {".", ".."}
                or decoded in {".", ".."}
                or "/" in decoded
                or "\\" in decoded
            ):
                bad = True
                break
    if bad:
        raise SkillError("Refusing to send a request to an unsafe API path")


class HostedListing(list):
    """Raw rows of an assignment / install-request listing (the strict row
    validator decides on each), plus whether the SERVER omitted records it
    could not read (`incomplete`; such a source never drives cleanup).

    A listing without the `incomplete` field comes from an older server and
    is judged by its rows alone. A present but non-boolean value is treated
    as incomplete."""

    server_incomplete: bool = False
    omitted_count: int = 0
    # Paging could not be completed (budget, a bad cursor, or an older
    # server's response that may have been cut at its fixed cap).
    truncated: bool = False
    # Stopped by the deadline or a cancel before the last page.
    interrupted: bool = False
    # Grants the org skills policy refuses (marketplace v2 §8): withheld by
    # the server, listed so the device can say why.
    policy_refused: list[dict[str, Any]]

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.policy_refused = []

    @classmethod
    def from_body(cls, body: dict[str, Any], items: list[Any]) -> "HostedListing":
        listing = cls(items)
        listing.absorb(body)
        return listing

    def absorb(self, body: dict[str, Any]) -> None:
        """Fold one page's `incomplete` / `omitted_count` into the listing."""
        if "incomplete" in body and body.get("incomplete") is not False:
            self.server_incomplete = True
        count = body.get("omitted_count")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            self.omitted_count += count
        refused = body.get("policy_refused")
        if isinstance(refused, list):
            self.policy_refused.extend(item for item in refused if isinstance(item, dict))


def _server_omitted(listing: Any) -> tuple[bool, int]:
    if isinstance(listing, HostedListing):
        return listing.server_incomplete, listing.omitted_count
    return False, 0


# Grant listings are paged (`cursor` / `has_more` / `next_cursor`). An older
# server returns one unpaged response capped at this many rows: exactly the
# cap may mean rows were cut off.
LEGACY_GRANT_LIST_CAP = 100
MAX_GRANT_PAGES = 200
# A listing that changes while it is being paged is enumerated again from
# its first page at most this many times, then reported incomplete.
MAX_LISTING_RESTARTS = 2
_NO_GENERATION = object()
GRANT_PAGE_LIMIT = 500  # the server's maximum page size (default 100)


class HostedClient:
    """Small synchronous client; `transport` is injectable for tests."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        transport: Any = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self._transport = transport
        self._timeout = timeout
        self._identity: dict[str, Any] | None = None

    @contextmanager
    def _client(self, timeout: float | None = None) -> Iterator["httpx.Client"]:
        import httpx

        kwargs: dict[str, Any] = {
            "timeout": self._timeout if timeout is None else min(self._timeout, timeout),
            "headers": {"Authorization": f"Bearer {self.api_key}"},
            "follow_redirects": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        with httpx.Client(**kwargs) as client:
            yield client

    def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        method: str = "GET",
        json_body: Any = None,
    ) -> "httpx.Response":
        import httpx

        _assert_safe_api_path(path)
        try:
            with self._client(timeout) as client:
                if method == "POST":
                    return client.post(f"{self.api_url}{path}", params=params, json=json_body)
                return client.get(f"{self.api_url}{path}", params=params)
        except httpx.RequestError as exc:
            raise HostedUnavailable(
                f"Can't reach the SessionFS API at {self.api_url} ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _error(response: "httpx.Response", what: str) -> SkillError:
        error = parse_api_error(response)
        if error.code == "service_key_not_allowed":
            return ServiceKeyNotAllowed(
                f"{what}: this server does not accept service keys for this request"
            )
        if response.status_code in (401, 403):
            return HostedUnavailable(
                f"{what}: not authorized (HTTP {response.status_code}); run `sfs auth login`"
            )
        detail = f": {error.message}" if error.message else ""
        return SkillError(f"{what} failed (HTTP {response.status_code}){detail}")

    def _json(self, path: str, params: dict[str, Any] | None = None, *, what: str) -> Any:
        response = self._request(path, params)
        if response.status_code >= 400:
            raise self._error(response, what)
        try:
            return response.json()
        except ValueError as exc:
            raise SkillError(f"{what}: the server returned invalid JSON") from exc

    # -- catalog -------------------------------------------------------------

    def search_page(
        self,
        query: str | None,
        *,
        tool: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One catalog page. ``filters`` are the discovery filters (marketplace
        v2 §9): category, publisher, verified, attested, license,
        max_severity, capability, works_in, updated_within,
        include_deprecated, sort. None values are omitted."""
        params: dict[str, Any] = {"limit": max(1, min(limit, SEARCH_PAGE_LIMIT))}
        if query:
            params["q"] = query[:200]
        if tool:
            params["tool"] = tool
        if cursor:
            params["cursor"] = cursor
        for key, value in (filters or {}).items():
            if key not in SEARCH_FILTERS or value is None:
                continue
            params[key] = ("true" if value else "false") if isinstance(value, bool) else value
        body = self._json("/api/v1/marketplace/packages", params, what="Marketplace search")
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise SkillError("Marketplace search returned an unexpected response")
        return body

    def search(
        self,
        query: str | None,
        *,
        tool: str | None = None,
        limit: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[dict]:
        return [
            item for item in self.search_page(query, tool=tool, limit=limit, filters=filters)["items"]
            if isinstance(item, dict)
        ]

    def categories(self) -> list[dict]:
        body = self._json("/api/v1/marketplace/categories", None, what="Marketplace categories")
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise SkillError("Marketplace categories returned an unexpected response")
        return [item for item in body["items"] if isinstance(item, dict)]

    def report_usage(
        self,
        version_id: str,
        event: str,
        tools: list[str],
        *,
        source: str = "download",
        project_id: str | None = None,
        interactive: bool = False,
    ) -> bool:
        """Best-effort install-lifecycle count (marketplace v2 §9): never
        raises and never blocks the caller for long. The server keeps daily
        aggregates only, one count per account per version, tool and day.
        ``interactive`` lets a terminal CLI command show the one-time notice
        first (see usage_reporting_enabled)."""
        import httpx

        if not tools or not _VERSION_ID_RE.fullmatch(version_id):
            return False
        if not usage_reporting_enabled(interactive=interactive):
            return False
        path = f"/api/v1/marketplace/versions/{version_id}/events"
        payload: dict[str, Any] = {"event": event, "tools": sorted(set(tools))[:10], "source": source}
        if project_id:
            payload["project_id"] = project_id
        try:
            _assert_safe_api_path(path)
            with self._client(timeout=3.0) as client:
                response = client.post(f"{self.api_url}{path}", json=payload)
            return response.status_code == 202
        except (httpx.HTTPError, SkillError, OSError):
            return False
        except Exception:  # noqa: BLE001 - counts are optional, never fatal
            return False

    def get_package(self, package_id: str) -> dict[str, Any]:
        if not _PACKAGE_ID_RE.fullmatch(package_id):
            raise SkillError(f"Invalid hosted package id: {package_id}")
        response = self._request(f"/api/v1/marketplace/packages/{package_id}")
        if response.status_code == 404:
            raise SkillError(
                f"Hosted package {package_id} is not available (unpublished, yanked, "
                "blocked, or not visible to this account)"
            )
        if response.status_code >= 400:
            raise self._error(response, "Marketplace package lookup")
        body = response.json()
        if not isinstance(body, dict):
            raise SkillError("Marketplace package lookup returned an unexpected response")
        return body

    def works_in(self, version_id: str) -> dict[str, Any]:
        """Per-tool works-in evidence for one version (marketplace v2 §7.6)."""
        if not _VERSION_ID_RE.fullmatch(version_id):
            raise SkillError(f"Invalid hosted version id: {version_id}")
        response = self._request(f"/api/v1/marketplace/versions/{version_id}/works-in")
        if response.status_code == 404:
            raise SkillError(f"Hosted version {version_id} is not available to this account")
        if response.status_code >= 400:
            raise self._error(response, "Works-in lookup")
        body = response.json()
        if not isinstance(body, dict):
            raise SkillError("Works-in lookup returned an unexpected response")
        return body

    def version_metadata(self, version_id: str) -> dict[str, Any]:
        """Package metadata for one version (the preview carries package fields)."""
        if not _VERSION_ID_RE.fullmatch(version_id):
            raise SkillError(f"Invalid hosted version id: {version_id}")
        response = self._request(f"/api/v1/marketplace/versions/{version_id}/preview")
        if response.status_code == 404:
            raise SkillError(
                f"Hosted version {version_id} is not available (unpublished, yanked, "
                "blocked, or not visible to this account)"
            )
        if response.status_code >= 400:
            raise self._error(response, "Marketplace version lookup")
        body = response.json()
        if not isinstance(body, dict):
            raise SkillError("Marketplace version lookup returned an unexpected response")
        return body

    def resolve(self, ref: HostedRef) -> dict[str, Any]:
        """Resolve a reference to ONE deliverable version's metadata."""
        if ref.package_id:
            if ref.version is None:
                return self.get_package(ref.package_id)
            if _VERSION_ID_RE.fullmatch(ref.version):
                item = self.version_metadata(ref.version)
                if item.get("package_id") != ref.package_id:
                    raise SkillError(f"{ref.version} is not a version of {ref.package_id}")
                return item
        matches: list[dict[str, Any]] = []
        cursor: str | None = None
        query = ref.name or None
        for _ in range(MAX_SEARCH_PAGES):
            page = self.search_page(query, limit=SEARCH_PAGE_LIMIT, cursor=cursor)
            for item in page["items"]:
                if not isinstance(item, dict):
                    continue
                if ref.package_id:
                    if item.get("package_id") != ref.package_id:
                        continue
                elif item.get("namespace") != ref.namespace or item.get("name") != ref.name:
                    continue
                matches.append(item)
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                break
        package_ids = sorted({str(item.get("package_id")) for item in matches})
        if not matches:
            raise SkillError(
                f"Hosted package {ref.display} was not found (or is not visible to this account)"
            )
        if len(package_ids) > 1:
            raise SkillError(
                f"{ref.display} matches {len(package_ids)} hosted packages "
                f"({', '.join(package_ids)}); use sessionfs:<package id> instead"
            )
        if ref.version is None:
            # The package's CURRENT version (the server decides which one that is).
            return self.get_package(package_ids[0])
        selected = [
            item for item in matches
            if item.get("version") == ref.version or item.get("version_id") == ref.version
        ]
        if not selected:
            raise SkillError(f"Hosted package {ref.display}: version not found")
        version_ids = {str(item.get("version_id")) for item in selected}
        if len(version_ids) > 1:
            raise SkillError(
                f"Version label {ref.version} is ambiguous for {ref.display}; "
                f"use one of: {', '.join(sorted(version_ids))}"
            )
        return selected[0]

    def download(self, version_id: str) -> tuple[bytes, str]:
        """Bounded download; returns (payload, X-SessionFS-Content-Hash)."""
        import httpx

        if not _VERSION_ID_RE.fullmatch(version_id):
            raise SkillError(f"Invalid hosted version id: {version_id}")
        path = f"/api/v1/marketplace/versions/{version_id}/download"
        _assert_safe_api_path(path)
        url = f"{self.api_url}{path}"
        try:
            with self._client() as client, client.stream("GET", url) as response:
                if response.status_code == 404:
                    raise SkillError(
                        f"Hosted version {version_id} is not available for download"
                    )
                if response.status_code >= 400:
                    response.read()
                    raise self._error(response, "Marketplace download")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                    raise SkillError("Hosted package download exceeds the size limit")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise SkillError("Hosted package download exceeds the size limit")
                    chunks.append(chunk)
                header_hash = response.headers.get("x-sessionfs-content-hash", "")
        except httpx.RequestError as exc:
            raise HostedUnavailable(
                f"Can't reach the SessionFS API at {self.api_url} ({type(exc).__name__})"
            ) from exc
        if not _CONTENT_HASH_RE.fullmatch(header_hash):
            raise SkillError(
                "Hosted download is missing a valid X-SessionFS-Content-Hash header; refusing it"
            )
        return b"".join(chunks), header_hash

    # -- identity / org state -----------------------------------------------

    def me(self) -> dict[str, Any]:
        body = self._json("/api/v1/auth/me", what="Account lookup")
        if not isinstance(body, dict):
            raise SkillError("Account lookup returned an unexpected response")
        return body

    def identity(self) -> dict[str, Any]:
        """`{"kind": "user", "user_id": ...}` or `{"kind": "service", ...}`.

        `/auth/me` rejects service keys with `service_key_not_allowed` (in any
        envelope shape `parse_api_error` understands); a service key is still a
        valid marketplace identity, namespaced by its credential fingerprint.
        """
        if self._identity is not None:
            return self._identity
        response = self._request("/api/v1/auth/me")
        if response.status_code >= 400:
            if parse_api_error(response).code == "service_key_not_allowed":
                self._identity = {"kind": "service", "user_id": None}
                return self._identity
            raise self._error(response, "Account lookup")
        body = response.json()
        user_id = body.get("user_id") if isinstance(body, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise SkillError("Account lookup returned no user id")
        self._identity = {"kind": "user", "user_id": user_id}
        return self._identity

    def device_policy(self) -> dict[str, Any] | None:
        """Skills policies that apply to this account (marketplace v2 §8.6),
        or None when the server does not offer them. Other failures RAISE."""
        response = self._request("/api/v1/devices/me/policy")
        if response.status_code == 404 and _is_route_missing(response):
            return None
        if response.status_code >= 400:
            raise self._error(response, "Skills policy lookup")
        try:
            body = response.json()
        except ValueError as exc:
            raise SkillError("Skills policy lookup: the server returned invalid JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise SkillError("Skills policy lookup returned an unexpected response")
        return body

    def post_policy_report(self, report: dict[str, Any]) -> dict[str, Any]:
        response = self._request("/api/v1/devices/me/policy-report", method="POST", json_body=report)
        if response.status_code >= 400:
            raise self._error(response, "Skills policy report")
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def assignments(
        self,
        device_id: str | None = None,
        *,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> HostedListing:
        """Effective org assignments. Any failure RAISES - an unreadable
        listing is never reported as "no assignments"."""
        params = {"device_id": device_id} if device_id else {}
        return self._grant_listing(
            "/api/v1/users/me/skills/assignments", params, "Skill assignment lookup",
            deadline=deadline, cancel=cancel,
        )

    def _grant_listing(
        self,
        path: str,
        params: dict[str, Any],
        what: str,
        *,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> HostedListing:
        """Every page of a grant listing (raw rows). Paging until `has_more`
        is false; a listing that could not be exhausted (page budget, a
        missing or repeated cursor, the deadline / a cancel before a page,
        the listing changing more than MAX_LISTING_RESTARTS times) is marked
        `truncated`, so its source is never treated as complete. A response
        without `has_more` is an older server's single page: exactly
        LEGACY_GRANT_LIST_CAP rows may have been cut off and is marked
        truncated too. A short page never ends paging (only `has_more:
        false` does); `incomplete: true` on ANY page marks the whole set
        incomplete.

        A change of the listing while paging (a different `generation` on a
        later page, or the server's 409 `listing_changed`) restarts the
        enumeration from the first page. Each request's timeout is bounded
        by the remaining deadline. Any other HTTP or shape failure RAISES -
        including the server's 422 for an invalid cursor, so paging is never
        silently restarted."""
        for _restart in range(MAX_LISTING_RESTARTS + 1):
            listing, changed = self._grant_pass(path, params, what, deadline, cancel)
            if not changed:
                return listing
        listing = HostedListing()
        listing.truncated = True  # kept changing: never treated as complete
        return listing

    def _grant_pass(
        self,
        path: str,
        params: dict[str, Any],
        what: str,
        deadline: float | None,
        cancel: Callable[[], bool] | None,
    ) -> tuple[HostedListing, bool]:
        """One enumeration; (listing, changed-while-paging)."""
        listing = HostedListing()
        cursor: str | None = None
        seen: set[str] = set()
        generation: Any = _NO_GENERATION
        for _page in range(MAX_GRANT_PAGES):
            if _halted(deadline, cancel):
                listing.truncated = listing.interrupted = True
                return listing, False
            page_params = {**params, "limit": GRANT_PAGE_LIMIT}
            if cursor is not None:
                page_params["cursor"] = cursor
            timeout = None
            if deadline is not None:
                timeout = max(0.1, deadline - time.monotonic())
            response = self._request(path, page_params, timeout=timeout)
            if response.status_code == 409 and parse_api_error(response).code == "listing_changed":
                return listing, True
            if response.status_code >= 400:
                raise self._error(response, what)
            try:
                body = response.json()
            except ValueError as exc:
                raise SkillError(f"{what}: the server returned invalid JSON") from exc
            items = body.get("items") if isinstance(body, dict) else None
            if not isinstance(items, list):
                raise SkillError(f"{what} returned an unexpected response")
            page_generation = body.get("generation", _NO_GENERATION)
            if generation is _NO_GENERATION:
                generation = page_generation
            elif page_generation != generation:
                return listing, True  # the listing changed under the cursor
            listing.extend(items)
            listing.absorb(body)
            if "has_more" not in body:
                # Older server: one unpaged response.
                if cursor is None and len(items) >= LEGACY_GRANT_LIST_CAP:
                    listing.truncated = True
                return listing, False
            if body.get("has_more") is False:
                return listing, False
            next_cursor = body.get("next_cursor")
            if body.get("has_more") is not True or not isinstance(next_cursor, str) or (
                not next_cursor or next_cursor in seen
            ):
                listing.truncated = True  # cannot page on safely
                return listing, False
            seen.add(next_cursor)
            cursor = next_cursor
        listing.truncated = True  # page budget exhausted
        return listing, False

    def project_id_for_remote(self, normalized_remote: str) -> str:
        """Project id for a git remote. Raises when it cannot be resolved
        (unknown project, no access, a server that refuses service keys
        here) - never a silent "no project".

        Uses `GET /api/v1/marketplace/projects/resolve` (user AND service
        keys). Its "no such project / no access" answer is a constant 404
        whose message is "Project not found"; any OTHER 404 means the route
        does not exist (older server), and only then is the older user-only
        `GET /api/v1/projects/{remote}` lookup tried.
        """
        response = self._request(
            "/api/v1/marketplace/projects/resolve", {"git_remote": normalized_remote}
        )
        if response.status_code == 404 and not _is_route_missing(response):
            raise SkillError(f"no SessionFS project is linked to {normalized_remote} (or no access)")
        if response.status_code != 404:
            if response.status_code >= 400:
                raise self._error(response, "Project lookup")
            body = response.json()
            project_id = body.get("project_id") if isinstance(body, dict) else None
            if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
                raise SkillError("Project lookup returned an unexpected response")
            return project_id
        return self._legacy_project_id_for_remote(normalized_remote)

    def _legacy_project_id_for_remote(self, normalized_remote: str) -> str:
        """Older servers: the user-only lookup by remote.

        The remote becomes URL PATH segments, so it goes through the shared
        strict validator: a `.`/`..` segment (raw or percent-decoded), a
        backslash, a control character, `?` or `#` is refused BEFORE any
        request is made (it could otherwise send the user's token to another
        endpoint, e.g. `../admin/users`); each segment is percent-encoded."""
        from sessionfs.url_path_safety import repository_path_key

        try:
            key = repository_path_key(normalized_remote)
        except ValueError:
            raise SkillError(
                "This git remote cannot be used to look up a SessionFS project"
            ) from None
        response = self._request(f"/api/v1/projects/{key}")
        if response.status_code == 404:
            raise SkillError(f"no SessionFS project is linked to {normalized_remote}")
        if response.status_code >= 400:
            raise self._error(response, "Project lookup")
        body = response.json()
        project_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
            raise SkillError("Project lookup returned an unexpected response")
        return project_id

    def approved_install_requests(
        self,
        project_id: str,
        *,
        deadline: float | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> HostedListing:
        """Approved install requests of a project. Any failure RAISES."""
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise SkillError("Invalid project id")
        return self._grant_listing(
            f"/api/v1/projects/{project_id}/skill-install-requests",
            {"status": "approved"},
            "Install request lookup",
            deadline=deadline, cancel=cancel,
        )

    def revocations(self, since: str | None) -> dict[str, Any] | None:
        """One revocation-feed page; None when the server predates the feed."""
        params: dict[str, Any] = {"limit": REVOCATION_PAGE_SIZE}
        if since:
            params["since"] = since
        response = self._request("/api/v1/marketplace/revocations", params)
        if response.status_code in (404, 405):
            return None
        if response.status_code >= 400:
            raise self._error(response, "Revocation feed")
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("revocations"), list):
            raise SkillError("Revocation feed returned an unexpected response")
        return body


def client_from_auth(*, transport: Any = None) -> HostedClient | None:
    """Client for the active CLI identity, or None when signed out."""
    from sessionfs.profiles import resolve_auth

    auth = resolve_auth()
    if not auth.api_key:
        return None
    return HostedClient(auth.api_url, auth.api_key, transport=transport)


def clients_from_profiles(*, transport: Any = None) -> list[HostedClient]:
    """One client per configured signed-in profile (any server)."""
    from sessionfs import profiles

    clients: list[HostedClient] = []
    try:
        names = profiles.list_profiles()
    except Exception:  # pragma: no cover - unreadable profile directory
        return clients
    for name in names:
        try:
            api_url, api_key = profiles._read_profile_sync(name)
        except Exception:  # pragma: no cover - unreadable profile
            continue
        if api_key:
            clients.append(HostedClient(api_url, api_key, transport=transport))
    return clients


def require_client(client: HostedClient | None) -> HostedClient:
    if client is None:
        raise HostedUnavailable(
            "The hosted SessionFS Marketplace requires a signed-in account: run "
            "`sfs auth login` (or set SESSIONFS_API_KEY)"
        )
    return client


# ---------------------------------------------------------------------------
# Download -> verified import
# ---------------------------------------------------------------------------


def _safe_member_path(raw: str) -> PurePosixPath:
    if not raw or "\\" in raw or "\x00" in raw:
        raise SkillError("Hosted package archive contains an unsafe path")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(path.parts) > MAX_PATH_DEPTH
        or any(part in {"", "."} for part in path.parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in raw)
    ):
        raise SkillError("Hosted package archive contains an unsafe path")
    return path


def extract_archive(payload: bytes, destination: Path) -> None:
    """Extract a canonical hosted archive (gzip'd ustar, regular files only)."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as compressed:
            expanded = compressed.read(MAX_EXPANDED_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise SkillError("Hosted package is not a valid tar.gz archive") from exc
    if len(expanded) > MAX_EXPANDED_BYTES:
        raise SkillError("Hosted package expands beyond the size limit")
    collisions = CasefoldCollisionTracker()
    members = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            for member in archive:
                members += 1
                if members > MAX_MEMBERS:
                    raise SkillError("Hosted package archive has too many members")
                path = _safe_member_path(member.name.rstrip("/") if member.isdir() else member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise SkillError("Hosted package archive may contain only regular files")
                if member.pax_headers:
                    raise SkillError("Hosted package archive contains extended headers")
                if member.size > MAX_MEMBER_BYTES:
                    raise SkillError("Hosted package member exceeds the per-file limit")
                if not fits_ustar(path.as_posix()):
                    raise SkillError("Hosted package archive contains an over-long path")
                try:
                    collisions.add(path.as_posix())
                except ValueError as exc:
                    raise SkillError("Hosted package archive contains colliding paths") from exc
                source = archive.extractfile(member)
                if source is None:
                    raise SkillError("Hosted package member could not be read")
                data = source.read(MAX_MEMBER_BYTES + 1)
                if len(data) != member.size:
                    raise SkillError("Hosted package member size is invalid")
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "xb") as handle:
                    handle.write(data)
    except tarfile.TarError as exc:
        raise SkillError("Hosted package is not a valid tar.gz archive") from exc


def import_hosted_version(
    client: HostedClient,
    item: dict[str, Any],
    store_root: Path,
    *,
    expected_hash: str | None = None,
    installed_hash: str | None = None,
    fingerprint: str | None = None,
) -> PackageRecord:
    """Download one hosted version, verify its hash three ways, import it.

    The catalog's `content_hash`, the download's `X-SessionFS-Content-Hash`
    header and the hash recomputed locally from the extracted bytes (the same
    aggregate integrity hash the server computed at publish) must all agree,
    and match `expected_hash` (lockfile / assignment) when one is given.
    """
    origin = canonical_origin(client.api_url)
    identity = hosted_identity(item, origin)
    version_id = str(identity.commit)
    catalog_hash = str(item.get("content_hash") or "")
    if not _CONTENT_HASH_RE.fullmatch(catalog_hash):
        raise SkillError("Hosted marketplace returned an invalid content hash")
    if expected_hash is not None and catalog_hash != expected_hash:
        raise SkillError(
            f"Hosted version {version_id} hash {catalog_hash} does not match the expected "
            f"{expected_hash}"
        )
    owner = _item_owner(item)
    # This path is online: map the credential (user OR service key) to its
    # own namespace first (if that fails, it stays unknown: device-wide
    # flags only - never another identity's).
    with contextlib.suppress(Exception):
        register_identity(client, store_root)
    assert_install_allowed(
        store_root,
        content_hash=catalog_hash,
        version_id=version_id,
        package_id=hosted_package_id(identity),
        installed_hash=installed_hash,
        fingerprint=fingerprint if fingerprint is not None else current_fingerprint(client=client),
        owner_org_id=owner,
        origin=origin,
        visibility=_item_visibility(item),
    )
    payload, header_hash = client.download(version_id)
    if header_hash != catalog_hash:
        raise SkillError(
            f"Hosted download hash header {header_hash} does not match the catalog hash "
            f"{catalog_hash}; refusing it"
        )
    with tempfile.TemporaryDirectory(prefix="sfs-skill-hosted-") as tmp:
        root = Path(tmp) / "package"
        root.mkdir()
        extract_archive(payload, root)
        package = import_package(root, store_root, identity)
    if package.content_hash != header_hash:
        raise SkillError(
            f"Hosted package content hash mismatch: the server declared {header_hash} but the "
            f"downloaded bytes hash to {package.content_hash}; refusing it"
        )
    # Safety provenance travels WITH this install (written onto its state row
    # atomically by the install) - never into shared state during an import,
    # so a later (or failed) import of the same version from another listing
    # cannot change how an existing copy is judged.
    provenance: dict[str, Any] = {}
    if isinstance(item.get("listing_id"), str):
        provenance["listing_id"] = item["listing_id"]
    visibility = _item_visibility(item)
    if visibility is not UNKNOWN_OWNER:
        provenance["visibility"] = visibility
    if owner is not UNKNOWN_OWNER:
        provenance["owner_org_id"] = owner
    return replace(package, provenance=provenance)


# ---------------------------------------------------------------------------
# Local hosted state: revocation flags, feed cursor, pull-managed installs
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _halted(deadline: float | None, cancel: Callable[[], bool] | None = None) -> bool:
    """Deadline passed, or the caller (e.g. a stopping daemon) cancelled."""
    return _expired(deadline) or (cancel is not None and bool(cancel()))


# The revocation feed is VISIBILITY-SCOPED (public + the caller's orgs + the
# caller's own listings), so cursor and flags are kept per identity
# (api_url + user id, or the credential fingerprint for a service key). A
# periodic full replay picks up older revocations that became visible through
# a membership change.
FULL_REPLAY_INTERVAL = timedelta(hours=24)
HIDDEN_REASON = "blocked by the marketplace (details are visible to another signed-in account)"
KEPT_YANK_REASON = "yanked by its publisher (kept until the publisher restores it)"
KEPT_SCOPED_REASON = (
    "blocked by its organization (kept until the organization restores it, "
    "even if this account can no longer see the organization's listings)"
)
# "quarantined" (server migration 069): an automated instruction-scan rescan
# stopped NEW installs of a version. Like a yank it only warns about existing
# installs and refuses new ones; it never removes anything (only a human
# moderator's block does). A later "restored" event clears it.
_FEED_STATES = {"yanked", "blocked", "restored", "quarantined"}
# Precedence when several flags cover one install.
_STATE_RANK = {"blocked": 3, "quarantined": 2, "yanked": 1}
QUARANTINE_NOTE = (
    "quarantined by an automated marketplace scan; new installs are refused, "
    "existing installs are kept until a moderator decides"
)
_BLOCK_SCOPES = {"global", "org", "owner"}


def identity_fingerprint(api_url: str, api_key: str) -> str:
    """Opaque local handle for a credential (never the key itself), bound to
    the CANONICAL server origin, so equivalent URLs (case, default port,
    trailing slash) yield the same identity."""
    try:
        origin = canonical_origin(api_url)
    except SkillError:
        origin = api_url.rstrip("/")
    digest = hashlib.sha256(f"{origin}\0{api_key}".encode("utf-8"))
    return digest.hexdigest()[:32]


def _legacy_fingerprint(api_url: str, api_key: str) -> str:
    """The fingerprint older builds derived from the RAW API URL."""
    digest = hashlib.sha256(f"{api_url.rstrip('/')}\0{api_key}".encode("utf-8"))
    return digest.hexdigest()[:32]


def adopt_legacy_identity(store_root: Path, client: HostedClient) -> None:
    """Move state an older build keyed by the raw-URL fingerprint (service-key
    namespace, pull records, identity map) onto the canonical fingerprint."""
    old = _legacy_fingerprint(client.api_url, client.api_key)
    new = identity_fingerprint(client.api_url, client.api_key)
    if old == new:
        return
    state_store = HostedState(store_root)
    state = state_store.read()
    origin = canonical_origin(client.api_url)
    raw_prefixes = {client.api_url.rstrip("/"), origin}
    old_namespaces = {f"{prefix}|key:{old}" for prefix in raw_prefixes}
    new_namespace = f"{origin}|key:{new}"
    needs = old in state["identities"] or any(
        name in state["namespaces"] for name in old_namespaces
    ) or any(
        isinstance(record, dict) and record.get("namespace") in old_namespaces
        for record in state["pulled"].values()
    )
    if not needs:
        return
    with state_store.edit() as state:
        if old in state["identities"]:
            target = state["identities"].pop(old)
            state["identities"].setdefault(
                new, new_namespace if target in old_namespaces else target
            )
        for name in old_namespaces:
            namespace = state["namespaces"].pop(name, None)
            if isinstance(namespace, dict):
                state["namespaces"].setdefault(new_namespace, namespace)
        for key, record in list(state["pulled"].items()):
            if isinstance(record, dict) and record.get("namespace") in old_namespaces:
                record["namespace"] = new_namespace
                old_prefix = next(
                    (name for name in old_namespaces if key.startswith(name + "|")), None
                )
                if old_prefix is not None:
                    state["pulled"].pop(key)
                    state["pulled"][new_namespace + key[len(old_prefix):]] = record


def current_fingerprint(
    manager: "SkillManager | None" = None, client: Any = None
) -> str | None:
    """Fingerprint of the identity in use, resolved WITHOUT network access."""
    source = client if client is not None else getattr(manager, "_hosted_client", None)
    if source is None:
        try:
            source = client_from_auth()
        except Exception:  # pragma: no cover - unreadable profile
            return None
    if source is not None and getattr(source, "api_key", None):
        return identity_fingerprint(source.api_url, source.api_key)
    return None


def namespace_key(client: HostedClient) -> tuple[str, dict[str, Any]]:
    identity = client.identity()
    origin = canonical_origin(client.api_url)
    if identity["kind"] == "service":
        fingerprint = identity_fingerprint(client.api_url, client.api_key)
        return f"{origin}|key:{fingerprint}", identity
    return f"{origin}|{identity['user_id']}", identity


def _resolve_namespace(
    state: dict[str, Any], fingerprint: str | None, origin: str | None = None
) -> str | None:
    """Namespace of the identity in use: the one its credential is mapped to
    (by a revocation sync or a registration - user or service key), even if
    nothing was synced into it yet. An unknown, unregistered credential
    resolves to NO namespace - it cannot be classified offline (user and
    service keys share one format), so only device-wide hash-wide flags
    apply to it (see `identity_unknown`); online, `register_identity` maps
    it before any check."""
    current = (state.get("identities") or {}).get(fingerprint) if fingerprint else None
    return current if isinstance(current, str) else None


def _origin_namespaces(state: dict[str, Any], origin: str | None) -> list[str]:
    """Every namespace (user and service key) of `origin` on this device."""
    return sorted(
        key for key, namespace in (state.get("namespaces") or {}).items()
        if isinstance(namespace, dict) and namespace.get("api_url") == origin
    )


def identity_unknown(manager: "SkillManager") -> str | None:
    """The server origin whose namespace the identity in use cannot be
    resolved to on this device although namespaces of that server exist
    (an unsynced credential - user or service key), or None. Only
    device-wide flags apply then."""
    fingerprint, origin = current_identity(manager)
    if fingerprint is None or origin is None:
        return None
    state = HostedState(manager.store_root).read()
    if not _origin_namespaces(state, origin):
        return None  # nothing synced for this server yet: nothing to miss
    resolved = _resolve_namespace(state, fingerprint, origin)
    if resolved is not None and resolved in (state.get("namespaces") or {}):
        return None
    return origin


def register_identity(client: HostedClient, store_root: Path) -> None:
    """Map this credential to its namespace (one cheap identity lookup) when
    it is not known yet - e.g. after an API-key rotation - so its own target
    blocks and yanks apply before the next revocation sync. Raises on
    network errors; callers treat that as offline (the credential then stays
    unknown: device-wide flags only, and doctor says so)."""
    fingerprint = identity_fingerprint(client.api_url, client.api_key)
    hosted_state = HostedState(store_root)
    if fingerprint in hosted_state.read()["identities"]:
        return
    key, _identity = namespace_key(client)
    with hosted_state.edit() as state:
        state["identities"].setdefault(fingerprint, key)


def ensure_identity_registered(manager: "SkillManager") -> None:
    """Best-effort `register_identity` for the manager's identity before an
    install / rollback / locked sync enforces revocations (no-op when the
    credential is known, signed out, or offline)."""
    client = getattr(manager, "_hosted_client", None)
    if client is None:
        try:
            client = client_from_auth()
        except Exception:  # pragma: no cover - unreadable profile
            return
    if client is None or not getattr(client, "api_key", None):
        return
    try:
        register_identity(client, manager.store_root)
    except Exception:  # offline / server error: it stays unknown (device-wide flags only)
        return


def current_identity(
    manager: "SkillManager | None" = None, client: Any = None
) -> tuple[str | None, str | None]:
    """(credential fingerprint, canonical API origin) of the identity in use,
    resolved WITHOUT network access."""
    source = client if client is not None else getattr(manager, "_hosted_client", None)
    if source is None:
        try:
            source = client_from_auth()
        except Exception:  # pragma: no cover - unreadable profile
            return None, None
    if source is None or not getattr(source, "api_key", None):
        return None, None
    try:
        origin = canonical_origin(source.api_url)
    except SkillError:
        origin = None
    return identity_fingerprint(source.api_url, source.api_key), origin


class _UnknownOwner:
    """Owner of a hosted package not reported by the server (older server or
    an install made before owners were tracked)."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNKNOWN_OWNER"


UNKNOWN_OWNER: Any = _UnknownOwner()


def _item_visibility(item: dict[str, Any]) -> Any:
    """Listing visibility from package metadata ("public" / "org" / "user"),
    or UNKNOWN_OWNER when the server does not send it."""
    value = item.get("visibility")
    return value if value in {"public", "org", "user"} else UNKNOWN_OWNER


def _item_owner(item: dict[str, Any]) -> Any:
    """`owner_org_id` from package metadata: an org id, None (personal or
    public), or UNKNOWN_OWNER when the server does not send the field."""
    if "owner_org_id" not in item:
        return UNKNOWN_OWNER
    value = item.get("owner_org_id")
    return value if isinstance(value, str) and value else None


class HostedState:
    """`<store_root>/hosted.json`, guarded by an interprocess lock.

    Schema (version 2)::

        {"version": 2,
         "namespaces": {"<api_url>|<user_id or key:fingerprint>": {
             "api_url", "user_id", "revocations_since", "last_full_replay",
             "revoked": {<version_id | package:<id>>: flag},
             "content_blocks": {"<hash>|<block_scope>|<scope_org_id>": flag}}},
         "global_content": {"<api_url>": {"<hash>": {"state", "changed_at"}}},
         "package_owners": {"<server package id>": "<org id>" | null},
         "identities": {"<fingerprint>": "<namespace>"},
         "pulled": {...}, "quarantined": [...]}

    Version/package flags and content-hash blocks are kept apart: a content
    `restored` event clears only the hash-wide block of the same scope. GLOBAL
    content events are also folded into `global_content` (latest event per
    hash per server, restores kept as tombstones), which is what other
    identities on the device enforce - so a newer global restore seen by any
    identity lifts it everywhere. A version-1 file (one global cursor) is
    upgraded by discarding its revocation data, forcing a full replay.
    """

    def __init__(self, store_root: Path) -> None:
        self.root = Path(store_root)
        self.path = self.root / "hosted.json"
        self.lock_path = self.root / "hosted.lock"

    def read(self) -> dict[str, Any]:
        value = read_json(self.path, {}) if self.path.exists() else {}
        if not isinstance(value, dict):
            value = {}
        if value.get("version") != 2:
            value = {"pulled": value.get("pulled") if isinstance(value.get("pulled"), dict) else {}}
            value["version"] = 2
        value.pop("last_user_id", None)  # an earlier build's last-user fallback
        for key in (
            "namespaces", "identities", "pulled", "global_content", "package_owners",
            "package_visibility", "scoped_blocks", "opt_outs", "pull_ops",
            "install_id_migrations",
        ):
            if not isinstance(value.get(key), dict):
                value[key] = {}
        if not isinstance(value.get("quarantined"), list):
            value["quarantined"] = []
        # Kept scoped blocks: content blocks only; version/package (target)
        # entries written by earlier builds are dropped on load.
        for origin, table in list(value["scoped_blocks"].items()):
            value["scoped_blocks"][origin] = (
                {
                    key: entry for key, entry in table.items()
                    if str(key).startswith("content:") and isinstance(entry, dict)
                }
                if isinstance(table, dict)
                else {}
            )
        # Only hash-wide events are shared device-wide; version/package
        # (target) events written by earlier builds are dropped on load.
        for origin, table in list(value["global_content"].items()):
            if not isinstance(table, dict):
                value["global_content"][origin] = {}
                continue
            value["global_content"][origin] = {
                key: entry for key, entry in table.items()
                if isinstance(entry, dict) and _shared_device_wide(str(key), entry)
            }
        return value

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the hosted-state lock without writing - serializes a final
        revocation check plus install commit with revocation-state updates
        (which all go through `edit`)."""
        self.root.mkdir(parents=True, exist_ok=True)
        with ordered_lock("hosted"), interprocess_lock(self.lock_path):
            yield

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True)
        with ordered_lock("hosted"), interprocess_lock(self.lock_path):
            value = self.read()
            yield value
            atomic_json(self.path, value)


def _meta_key(package_id: str, version_id: str | None) -> str | None:
    """Owner/visibility are tracked per EXACT version: one package can have
    a private v1 and a public v2."""
    if not _PACKAGE_ID_RE.fullmatch(package_id or ""):
        return None
    if not isinstance(version_id, str) or not _VERSION_ID_RE.fullmatch(version_id):
        return None
    return f"{package_id}@{version_id}"


def record_package_owner(
    store_root: Path,
    origin: str,
    package_id: str,
    owner_org_id: Any,
    visibility: Any = UNKNOWN_OWNER,
    *,
    version_id: str | None,
) -> None:
    """Owner org and listing visibility of ONE hosted version
    (origin, package id, version id). Unknown values are not written."""
    key = _meta_key(package_id, version_id)
    if key is None:
        return
    state_store = HostedState(store_root)
    current = state_store.read()
    owners = current["package_owners"].get(origin) or {}
    visibilities = current["package_visibility"].get(origin) or {}
    owner_ok = owner_org_id is UNKNOWN_OWNER or owners.get(key, UNKNOWN_OWNER) == owner_org_id
    vis_ok = visibility is UNKNOWN_OWNER or visibilities.get(key) == visibility
    if owner_ok and vis_ok:
        return
    with state_store.edit() as state:
        if owner_org_id is not UNKNOWN_OWNER:
            server_owners = state["package_owners"].get(origin)
            if not isinstance(server_owners, dict):
                server_owners = state["package_owners"][origin] = {}
            server_owners[key] = owner_org_id
        if visibility is not UNKNOWN_OWNER:
            server_vis = state["package_visibility"].get(origin)
            if not isinstance(server_vis, dict):
                server_vis = state["package_visibility"][origin] = {}
            server_vis[key] = visibility


def _source_meta_key(
    source: dict[str, Any] | SourceIdentity | None,
) -> tuple[str | None, str | None, bool]:
    """(origin, meta key, is_hosted) for an installed row's source: resolved
    through `source.commit`, the exact hosted version installed."""
    package_id, origin = _split_locator(source)
    if package_id is None:
        return None, None, False
    raw = source.as_dict() if isinstance(source, SourceIdentity) else (source or {})
    return origin, _meta_key(package_id, raw.get("commit")), True


def _local_provenance(local: dict[str, Any] | None) -> dict[str, Any]:
    return local if isinstance(local, dict) else {}


def _shared_meta(
    state: dict[str, Any], field: str, source: dict[str, Any] | SourceIdentity | None
) -> Any:
    """The shared per-version map value (legacy fallback, filled by refresh)."""
    origin, key, hosted = _source_meta_key(source)
    if not hosted or origin is None or key is None:
        return UNKNOWN_OWNER
    values = (state.get(field) or {}).get(origin)
    if not isinstance(values, dict) or key not in values:
        return UNKNOWN_OWNER
    return values[key]


def package_visibility(
    state: dict[str, Any],
    source: dict[str, Any] | SourceIdentity | None,
    local: dict[str, Any] | None = None,
) -> Any:
    """Visibility of the listing THIS installed copy came from: its row-local
    provenance first (`local`, written atomically with the install); the
    shared per-version map is only a fallback for rows whose provenance does
    not carry it (legacy installs, older servers). UNKNOWN_OWNER when
    neither knows."""
    provenance = _local_provenance(local)
    if "visibility" in provenance:
        value = provenance.get("visibility")
        return value if value in {"public", "org", "user"} else UNKNOWN_OWNER
    return _shared_meta(state, "package_visibility", source)


def package_owner(
    state: dict[str, Any],
    source: dict[str, Any] | SourceIdentity | None,
    local: dict[str, Any] | None = None,
) -> Any:
    """Owner org of THIS installed copy: row-local provenance first, then the
    shared per-version fallback (legacy rows). None for anything that is not
    a hosted package, UNKNOWN_OWNER if never recorded."""
    _origin, _key, hosted = _source_meta_key(source)
    if not hosted:
        return None
    provenance = _local_provenance(local)
    if "owner_org_id" in provenance:
        value = provenance.get("owner_org_id")
        return value if isinstance(value, str) and value else None
    return _shared_meta(state, "package_owners", source)


def provenance_complete(local: Any) -> bool:
    """True when an installed row's own provenance carries both safety fields
    (no shared fallback is consulted or needed for it)."""
    return isinstance(local, dict) and "owner_org_id" in local and "visibility" in local


def _namespace_flags(namespace: dict[str, Any]) -> list[dict[str, Any]]:
    flags = [
        flag for flag in (namespace.get("revoked") or {}).values() if isinstance(flag, dict)
    ]
    flags.extend(
        flag for flag in (namespace.get("content_blocks") or {}).values() if isinstance(flag, dict)
    )
    return flags


def _global_key(flag: dict[str, Any]) -> str | None:
    """Key of a GLOBAL flag in the per-server latest-event table. A kindless
    (older server) flag stored without a scope by earlier builds counts as
    global too; modern explicit version/package flags need the literal
    "global"."""
    scope = flag.get("block_scope")
    legacy_global = flag.get("kind_inferred") is True and scope in (None, "global")
    if scope != "global" and not legacy_global:
        return None
    if flag.get("kind") == "content":
        return f"content:{flag.get('content_hash')}"
    if flag.get("version_id"):
        return str(flag["version_id"])
    if flag.get("package_id"):
        return f"package:{flag['package_id']}"
    return None


def _global_state(state: dict[str, Any], api_url: str | None, key: str) -> dict | None:
    table = (state.get("global_content") or {}).get(api_url or "") or {}
    entry = table.get(key)
    return entry if isinstance(entry, dict) else None


def effective_flags(
    state: dict[str, Any],
    fingerprint: str | None,
    origin: str | None = None,
) -> list[dict[str, Any]]:
    """Flags that apply on ONE server (`origin`) for the identity in use.

    The current identity's flags apply in full (yanked + blocked, with their
    reasons) when it belongs to that server, except a GLOBAL block that a
    newer global restore - seen by any identity on the device for that same
    server - has lifted. From other identities only that server's GLOBAL
    blocks are enforced, from its latest-event table, matched by content
    hash alone, with the reason hidden. Tables of other servers never mix in.
    `origin` defaults to the current identity's server.
    """
    namespaces = state.get("namespaces") or {}
    current = _resolve_namespace(state, fingerprint, origin)
    flags: list[dict[str, Any]] = []
    own_global_keys: set[str] = set()
    current_namespace = namespaces.get(current) if current else None
    if origin is None and isinstance(current_namespace, dict):
        origin = current_namespace.get("api_url")
    if isinstance(current_namespace, dict) and current_namespace.get("api_url") == origin:
        api_url = origin
        for flag in _namespace_flags(current_namespace):
            global_key = _global_key(flag)
            if global_key is not None:
                latest = _global_state(state, api_url, global_key)
                if latest is not None and latest.get("state") == "restored":
                    continue  # lifted by a newer global restore seen by any identity
                if flag.get("state") == "blocked":
                    own_global_keys.add(global_key)
            flags.append(flag)
    table = (state.get("global_content") or {}).get(origin or "")
    if isinstance(table, dict):
        for key, entry in table.items():
            if not isinstance(entry, dict) or key in own_global_keys:
                continue
            foreign = _foreign_global_flag(str(key), entry)
            if foreign is not None:
                flags.append(foreign)
    # Kept SCOPED (org/owner) blocks of this server: they stay until an
    # explicit restore, even when no identity can see their event any more
    # (left the org) - `_scope_applies` still limits them to that org's own
    # non-public copies.
    own_keys: set[str] = set()
    if isinstance(current_namespace, dict) and current_namespace.get("api_url") == origin:
        own_keys = set((current_namespace.get("revoked") or {})) | {
            f"content:{key}" for key in (current_namespace.get("content_blocks") or {})
        }
    scoped = (state.get("scoped_blocks") or {}).get(origin or "")
    if isinstance(scoped, dict):
        for key, entry in scoped.items():
            if (
                not isinstance(entry, dict)
                or entry.get("state") not in {"blocked", "yanked"}
                or key in own_keys
            ):
                continue
            reason = KEPT_SCOPED_REASON if entry.get("state") == "blocked" else KEPT_YANK_REASON
            flags.append({**entry, "reason": reason, "foreign": True, "kept": True})
    return flags


def _scope_applies(flag: dict[str, Any], owner: Any, visibility: Any = UNKNOWN_OWNER) -> bool:
    """An ORG-scoped entry (version, package or content) applies only to an
    installed copy that the org owns AND that came from a non-public listing:
    an org block never reaches a public copy. Unknown owner or visibility
    (older install / server) or no scope_org_id: applies, cautiously."""
    block_scope = flag.get("block_scope")
    scope_org_id = flag.get("scope_org_id")
    has_org = isinstance(scope_org_id, str) and bool(scope_org_id)
    if block_scope == "owner":
        # Owner-layer action (a publisher's own yank/restore): only copies
        # installed from a NON-PUBLIC listing; with scope_org_id also only
        # copies that org owns. Unknown owner/visibility: applies, cautiously.
        if has_org and owner is not UNKNOWN_OWNER and owner != scope_org_id:
            return False
        return visibility is UNKNOWN_OWNER or visibility != "public"
    if block_scope != "org" or not has_org:
        return True
    if owner is UNKNOWN_OWNER:
        return True
    if owner != scope_org_id:
        return False
    if visibility is UNKNOWN_OWNER:
        return True
    return visibility != "public"


_content_applies = _scope_applies


def revocation_for(
    flags: list[dict[str, Any]],
    *,
    content_hash: str | None,
    version_id: str | None,
    package_id: str | None = None,
    owner: Any = UNKNOWN_OWNER,
    visibility: Any = UNKNOWN_OWNER,
) -> dict[str, Any] | None:
    """The revocation flag covering this content, if any (blocked wins).
    Org-scoped entries of every kind only cover the org's own non-public
    copy (see `_scope_applies`).

    Version and package events match ONLY by their ids: a yank or block of
    version A never reaches version B (or a local/Git package) that happens
    to have identical bytes. Hash-wide matching is for `kind == "content"`
    events; for an older server that sends no `kind`, a BLOCKED version
    event still matches by its content hash (never a yank)."""
    hits: list[dict[str, Any]] = []
    for flag in flags:
        if not _scope_applies(flag, owner, visibility):
            continue
        kind = flag.get("kind")
        if kind == "content":
            if content_hash and flag.get("content_hash") == content_hash:
                hits.append(flag)
            continue
        flag_version = flag.get("version_id")
        if version_id and flag_version == version_id:
            hits.append(flag)
        elif package_id and not flag_version and flag.get("package_id") == package_id:
            hits.append(flag)
        elif (
            # Older server: the event's kind is unknown.
            (kind is None or flag.get("kind_inferred") is True)
            and flag.get("state") == "blocked"
            and content_hash
            and flag_version
            and flag.get("content_hash") == content_hash
        ):
            hits.append(flag)
    if not hits:
        return None
    # Blocked wins, then quarantined, then yanked (first seen on a tie).
    return max(hits, key=lambda flag: _STATE_RANK.get(str(flag.get("state")), 0))


def _flag_label(flag: dict[str, Any], content_hash: str | None) -> str:
    # Another identity's block is labelled by content (no ids from its feed).
    if flag.get("foreign") or flag.get("kind") == "content":
        return f"Content {flag.get('content_hash') or content_hash}"
    if flag.get("version_id"):
        return f"Hosted version {flag['version_id']}"
    if flag.get("package_id"):
        return f"Hosted package {flag['package_id']}"
    return f"Content {content_hash}"


def assert_install_allowed(
    store_root: Path,
    *,
    content_hash: str | None,
    version_id: str | None,
    package_id: str | None = None,
    installed_hash: str | None = None,
    fingerprint: str | None = None,
    owner_org_id: Any = UNKNOWN_OWNER,
    origin: str | None = None,
    visibility: Any = UNKNOWN_OWNER,
    expands: bool = False,
) -> None:
    """Refuse new installs of blocked, quarantined or yanked hosted versions.

    A yanked or quarantined version that is ALREADY installed (re-applied by
    compile, kept by sync) is allowed; a blocked one never is. While a
    version is QUARANTINED only an exact re-application is allowed:
    ``expands`` (new target tools or capabilities beyond the installed row's
    recorded ones, e.g. ``compile --for claude-code``, or a changed emission
    plan for a kept tool) is refused.
    """
    flags = effective_flags(HostedState(store_root).read(), fingerprint, origin)
    flag = revocation_for(
        flags,
        content_hash=content_hash,
        version_id=version_id,
        package_id=package_id,
        owner=owner_org_id,
        visibility=visibility,
    )
    if flag is None:
        return
    reason = clean_untrusted_text(flag.get("reason") or "no reason given", limit=300,
                                  keep_newlines=False)
    label = _flag_label(flag, content_hash)
    if flag.get("state") == "blocked":
        raise SkillError(
            f"{label} is BLOCKED by the marketplace ({reason}); it cannot be installed"
        )
    quarantined = flag.get("state") == "quarantined"
    if installed_hash is not None and installed_hash == content_hash:
        if quarantined and expands:
            raise SkillError(
                f"{label} is QUARANTINED by an automated marketplace scan ({reason}); the "
                "installed copy is kept as it is, but it cannot be applied to more tools, "
                "given more capabilities or re-emitted with changed output until a moderator "
                "restores it"
            )
        return  # yanked / quarantined content already installed is kept
    if quarantined:
        raise SkillError(
            f"{label} is QUARANTINED by an automated marketplace scan ({reason}); "
            "new installs of it are refused until a moderator restores it"
        )
    raise SkillError(
        f"{label} was yanked by its publisher ({reason}); new installs of it are refused"
    )


MAX_SYNC_ATTEMPTS = 3


def sync_revocations(
    client: HostedClient,
    store_root: Path,
    *,
    force_full: bool = False,
    now: datetime | None = None,
    deadline: float | None = None,
    max_pages: int | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Pull the revocation feed into the CALLER'S local flags.

    Incremental from the identity's cursor; a FULL replay (no `since`, flags
    rebuilt from scratch) on the identity's first sync, every
    FULL_REPLAY_INTERVAL, or when forced, so revocations that became visible
    through a membership change are picked up and GLOBAL ones that are no
    longer visible are dropped. SCOPED (org/owner) blocks are different: they
    are only ever cleared by an explicit `restored` event - leaving an org
    (so its events disappear from a replay) never lifts its block. They are
    kept device-wide in `scoped_blocks` across replays and identity changes.

    The feed is fetched WITHOUT holding the hosted-state lock; the result is
    then merged under the lock with a compare-and-swap on the identity's
    cursor/replay marker (a concurrent sync that moved them makes this one
    retry). Pages until `has_more: false` (older servers: an empty page). If
    the page budget or `deadline` runs out first, NOTHING is committed and
    the summary says `complete: False`. `{"supported": False}` when the
    server predates the feed (404).
    """
    summary: dict[str, Any] = {
        "supported": True, "complete": True, "changed": [], "full_replay": False,
    }
    key, identity = namespace_key(client)
    adopt_legacy_identity(store_root, client)
    moment = now or datetime.now(timezone.utc)
    budget = max_pages if max_pages is not None else MAX_REVOCATION_PAGES
    origin = canonical_origin(client.api_url)
    hosted_state = HostedState(store_root)
    for _attempt in range(MAX_SYNC_ATTEMPTS):
        snapshot = hosted_state.read()["namespaces"].get(key)
        namespace: dict[str, Any] = dict(snapshot) if isinstance(snapshot, dict) else {
            "api_url": origin,
            "user_id": identity.get("user_id"),
            "kind": identity["kind"],
        }
        fence = (namespace.get("revocations_since"), namespace.get("last_full_replay"))
        last_full = _parse_iso(namespace.get("last_full_replay"))
        full = force_full or last_full is None or moment - last_full >= FULL_REPLAY_INTERVAL
        revoked: dict[str, Any] = {} if full else dict(namespace.get("revoked") or {})
        content: dict[str, Any] = {} if full else dict(namespace.get("content_blocks") or {})
        global_events: dict[str, dict[str, Any]] = {}
        scoped_events: dict[str, dict[str, Any]] = {}
        since = None if full else namespace.get("revocations_since")
        cursor = since if isinstance(since, str) else None
        changed: list[dict[str, Any]] = []
        complete = False
        # ---- network, no lock held ----
        for _ in range(budget):
            if _halted(deadline, cancel):
                break
            page = client.revocations(cursor)
            if page is None:
                summary["supported"] = False
                return summary
            entries = [item for item in page["revocations"] if isinstance(item, dict)]
            for entry in entries:
                _apply_revocation(revoked, content, entry, changed, global_events, scoped_events)
            next_since = page.get("next_since")
            advanced = isinstance(next_since, str) and bool(next_since) and next_since != cursor
            if isinstance(next_since, str) and next_since:
                cursor = next_since
            has_more = page.get("has_more")
            if isinstance(has_more, bool):
                if not has_more:
                    complete = True
                    break
                if not advanced:
                    break  # a server that claims more but does not move: incomplete
            elif not entries or not advanced:
                complete = True  # older server: an empty / non-advancing page ends it
                break
        if not complete:
            summary["complete"] = False
            return summary
        # ---- merge: brief lock, compare-and-swap on the fence ----
        with hosted_state.edit() as state:
            current = state["namespaces"].get(key)
            current_fence = (
                (current.get("revocations_since"), current.get("last_full_replay"))
                if isinstance(current, dict)
                else (None, None)
            )
            if current_fence != fence:
                continue  # another sync moved this identity on: redo from its state
            if full:
                old = {**(namespace.get("revoked") or {}), **{
                    f"content:{h}": flag
                    for h, flag in (namespace.get("content_blocks") or {}).items()
                }}
                new = {**revoked, **{f"content:{h}": flag for h, flag in content.items()}}
                changed = [
                    {"key": name, "state": flag.get("state")}
                    for name, flag in new.items()
                    if old.get(name) != flag
                ] + [{"key": name, "state": "restored"} for name in old if name not in new]
                namespace["last_full_replay"] = moment.isoformat()
                summary["full_replay"] = True
            summary["changed"] = changed
            namespace["revoked"] = revoked
            namespace["content_blocks"] = content
            if cursor is not None:
                namespace["revocations_since"] = cursor
            state["namespaces"][key] = namespace
            state["identities"][identity_fingerprint(client.api_url, client.api_key)] = key
            _merge_global_events(state, origin, global_events)
            _merge_scoped_events(state, origin, scoped_events)
        return summary
    summary["complete"] = False
    summary["concurrent"] = True
    return summary


def _merge_scoped_events(
    state: dict[str, Any], api_url: str, events: dict[str, dict[str, Any]]
) -> None:
    """Fold SCOPED (org/owner) block/restore events into the device-wide
    `scoped_blocks` table: latest event per key wins, restores stay as
    tombstones, and an entry is only ever lifted by an explicit restore -
    never by its absence from a later replay."""
    table = state["scoped_blocks"].setdefault(api_url, {})
    for key, event in events.items():
        existing = table.get(key)
        if isinstance(existing, dict):
            old_at = _parse_iso(existing.get("changed_at"))
            new_at = _parse_iso(event.get("changed_at"))
            if old_at is not None and (new_at is None or new_at < old_at):
                continue
        table[key] = dict(event)


def _merge_global_events(
    state: dict[str, Any], api_url: str, events: dict[str, dict[str, Any]]
) -> None:
    """Fold GLOBAL hash-wide events (content events, and kindless older
    server events) into the per-server latest-event table shared by every
    identity on the device. Restores are kept as tombstones so an older
    block replayed by another identity can never resurrect a lifted global
    block."""
    table = state["global_content"].setdefault(api_url, {})
    for key, event in events.items():
        existing = table.get(key)
        known_hash = event.get("content_hash")
        if isinstance(existing, dict):
            old_at = _parse_iso(existing.get("changed_at"))
            new_at = _parse_iso(event.get("changed_at"))
            if old_at is not None and (new_at is None or new_at < old_at):
                continue
            known_hash = known_hash or existing.get("content_hash")
        kind = event.get("kind") or _legacy_global_kind(key)
        if kind == "content" and not known_hash:
            continue  # nothing to match other identities' installs against
        entry: dict[str, Any] = {
            "state": event["state"],
            "changed_at": event.get("changed_at"),
            "content_hash": known_hash,
            "kind": kind,
        }
        if kind != "content":
            entry["kind_inferred"] = event.get("kind_inferred") is True
            entry["version_id"] = event.get("version_id")
            entry["package_id"] = event.get("package_id")
        if _shared_device_wide(key, entry):
            table[key] = entry


def _shared_device_wide(key: str, entry: dict[str, Any]) -> bool:
    """Whether a latest-event table entry may be shared device-wide: content
    events, and kindless (older server) blocks, which are hash-wide. Target
    (version/package) events - including those an earlier build wrote
    without a kind - are not."""
    kind = entry.get("kind")
    if kind is None:
        return key.startswith("content:")
    return kind == "content" or entry.get("kind_inferred") is True


def _legacy_global_kind(key: str) -> str:
    """Kind of a latest-event table entry written without one (older
    builds), from its key: `content:<hash>`, `package:<id>` or a version id."""
    if key.startswith("content:"):
        return "content"
    if key.startswith("package:"):
        return "package"
    return "version"


def _foreign_global_flag(key: str, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Another identity's GLOBAL block from the latest-event table, rebuilt
    with its kind and target id: a version/package block stays scoped to
    that id (only an older server's kindless block also matches by hash,
    see `revocation_for`); a content block matches by hash. Reason hidden."""
    if entry.get("state") != "blocked":
        return None
    kind = entry.get("kind") or _legacy_global_kind(key)
    content_hash = entry.get("content_hash")
    flag: dict[str, Any] = {
        "state": "blocked",
        "kind": kind,
        "content_hash": content_hash if isinstance(content_hash, str) else None,
        "block_scope": "global",
        "reason": HIDDEN_REASON,
        "foreign": True,
    }
    if kind == "content":
        return flag if flag["content_hash"] else None
    if not _shared_device_wide(key, entry):
        return None  # target events are never enforced across identities
    version_id, package_id = entry.get("version_id"), entry.get("package_id")
    flag["kind_inferred"] = True
    flag["version_id"] = version_id if kind == "version" and isinstance(version_id, str) else None
    flag["package_id"] = package_id if isinstance(package_id, str) else None
    if not flag["version_id"] and not flag["package_id"]:
        return None
    return flag


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _apply_revocation(
    revoked: dict[str, Any],
    content: dict[str, Any],
    entry: dict[str, Any],
    changed: list,
    global_events: dict[str, dict[str, Any]] | None = None,
    scoped_events: dict[str, dict[str, Any]] | None = None,
) -> None:
    kind_state = entry.get("state")
    if kind_state not in _FEED_STATES:
        return  # unknown future state: ignore safely
    kind = entry.get("kind")
    block_scope = entry.get("block_scope") if entry.get("block_scope") in _BLOCK_SCOPES else None
    if kind is None and block_scope is None and kind_state in {"blocked", "restored"}:
        # An older server's kindless block/restore carries no scope: it is a
        # platform (global, hash-wide) event - stored as such everywhere, so
        # a restore seen by any identity lifts it for every identity.
        block_scope = "global"
    scope_org_id = entry.get("scope_org_id")
    if not isinstance(scope_org_id, str) or not scope_org_id or block_scope not in {"org", "owner"}:
        scope_org_id = None
    version_id = entry.get("version_id")
    package_id = entry.get("package_id")
    content_hash = entry.get("content_hash")
    if content_hash is not None and not (
        isinstance(content_hash, str) and _CONTENT_HASH_RE.fullmatch(content_hash)
    ):
        return
    reason = clean_untrusted_text(entry.get("reason") or "", limit=500, keep_newlines=False)
    changed_at = str(entry.get("changed_at") or _now())[:64]
    if kind == "content":
        if not content_hash or kind_state not in {"blocked", "restored"}:
            return
        # Keyed by (hash, scope, org): an org's restore never lifts another
        # org's (or the global) block of the same bytes.
        key = f"{content_hash}|{block_scope or ''}|{scope_org_id or ''}"
        if block_scope == "global" and global_events is not None:
            global_events[f"content:{content_hash}"] = {
                "state": kind_state, "changed_at": changed_at, "content_hash": content_hash,
                "kind": "content",
            }
        if kind_state == "restored":
            if scoped_events is not None and block_scope in {"org", "owner"}:
                scoped_events[f"content:{key}"] = {"state": "restored", "changed_at": changed_at}
            if content.pop(key, None) is not None:
                changed.append({"key": f"content:{key}", "state": "restored"})
            return
        flag = {
            "state": "blocked", "kind": "content", "content_hash": content_hash,
            "reason": reason, "changed_at": changed_at, "block_scope": block_scope,
            "scope_org_id": scope_org_id,
        }
        if scoped_events is not None and block_scope in {"org", "owner"}:
            scoped_events[f"content:{key}"] = dict(flag)
        if content.get(key) != flag:
            content[key] = flag
            changed.append({"key": f"content:{key}", "state": "blocked"})
        return
    if kind not in (None, "version", "package"):
        return  # unknown kind from a newer server
    if version_id is not None and not (
        isinstance(version_id, str) and _VERSION_ID_RE.fullmatch(version_id)
    ):
        return
    if package_id is not None and not (
        isinstance(package_id, str) and _PACKAGE_ID_RE.fullmatch(package_id)
    ):
        return
    if kind == "package":
        version_id = None
    target = version_id or (f"package:{package_id}" if package_id else None)
    if target is None:
        return
    # Revocation state is kept PER LAYER: platform transitions (block_scope
    # global / null) and owner-layer transitions (org / owner, per org) are
    # independent - a restore clears only its own layer, and a target is
    # revoked while ANY layer is active.
    owner_layer = block_scope in {"org", "owner"}
    key = f"{target}#owner:{scope_org_id or ''}" if owner_layer else f"{target}#platform"
    # A quarantine is its own platform-layer flag, so a yank or block of the
    # same target never overwrites it (and vice versa). The server re-emits
    # whatever platform state remains after a platform restore.
    quarantine_key = f"{target}#quarantine"
    if kind_state == "quarantined":
        if owner_layer:
            return  # quarantine is platform-wide only; anything else is malformed
        key = quarantine_key
    if (
        kind is None
        and block_scope in (None, "global")  # older servers send no scope
        and global_events is not None
        and kind_state != "yanked"
    ):
        # Only a KINDLESS (older server) global event is hash-wide and is
        # shared device-wide. Explicit version/package events stay in the
        # caller's visibility-scoped namespace: shared device-wide, a block
        # whose scoped restore the user can no longer see (left the org)
        # would never be lifted. Restores keep a tombstone for that key.
        previous = global_events.get(target)
        global_events[target] = {
            "state": kind_state,
            "changed_at": changed_at,
            "content_hash": content_hash or (previous or {}).get("content_hash"),
            "kind": kind or ("version" if version_id else "package"),
            "kind_inferred": kind is None,
            "version_id": version_id,
            "package_id": package_id,
        }
    # Version/package (target) events - org-scoped and owner-layer ones too -
    # live ONLY in this identity's visibility-scoped namespace, which a full
    # replay rebuilds; they are never kept device-wide (an event the user can
    # no longer see, e.g. after leaving the org, must not stay in force with
    # its restore invisible). Only content blocks are kept device-wide.
    if kind_state == "restored":
        if revoked.pop(key, None) is not None:
            changed.append({"key": key, "state": "restored"})
        if not owner_layer and revoked.pop(quarantine_key, None) is not None:
            changed.append({"key": quarantine_key, "state": "restored"})
        return
    flag = {
        "state": kind_state,
        "kind": kind or ("version" if version_id else "package"),
        # The server sent no kind (older server): see `revocation_for`.
        "kind_inferred": kind is None,
        "version_id": version_id,
        "package_id": package_id,
        "content_hash": content_hash,
        "reason": reason,
        "changed_at": changed_at,
        "block_scope": block_scope,
        "scope_org_id": scope_org_id,
    }
    if revoked.get(key) != flag:
        revoked[key] = flag
        changed.append({"key": key, "state": kind_state})


def _row_flag(
    flags: list[dict[str, Any]], row: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any] | None:
    raw = row.get("source")
    source: dict[str, Any] = raw if isinstance(raw, dict) else {}
    package_id = hosted_package_id(source)
    version_id = source.get("commit") if package_id else None
    return revocation_for(
        flags,
        content_hash=str(row.get("content_hash") or "") or None,
        version_id=version_id if isinstance(version_id, str) else None,
        package_id=package_id,
        owner=package_owner(state, source, row.get("provenance")),
        visibility=package_visibility(state, source, row.get("provenance")),
    )


def refresh_package_owners(
    manager: "SkillManager",
    client: HostedClient,
    *,
    deadline: float | None = None,
    cancel: Callable[[], bool] | None = None,
) -> bool:
    """Learn `owner_org_id` for installed hosted packages of THIS server whose
    rows carry no (complete) row-local provenance - installs from before it
    was tracked - into the shared per-version FALLBACK map (so org-scoped
    blocks hit only that org). Owners are recorded per server. One request per package,
    each only while the deadline/cancel allow; returns False if it stopped
    early (the rest is picked up next time)."""
    origin = canonical_origin(client.api_url)
    current = HostedState(manager.store_root).read()
    owners = current["package_owners"].get(origin)
    visibilities = current["package_visibility"].get(origin)
    # Known only when BOTH the owner and the listing visibility are recorded.
    known = (set(owners) if isinstance(owners, dict) else set()) & (
        set(visibilities) if isinstance(visibilities, dict) else set()
    )
    seen: set[str] = set()
    for scope in ("project", "user"):
        for row in _installed_rows(manager, scope):
            source = row.get("source")
            row_origin, key, _hosted = _source_meta_key(source)
            if key is None or row_origin != origin:
                continue
            # A row carrying its own provenance never consults the shared
            # map; only legacy rows (or rows from older servers) need it.
            if provenance_complete(row.get("provenance")):
                continue
            if key in seen or key in known:
                continue
            seen.add(key)
            if _halted(deadline, cancel):
                return False
            package_id, version_id = key.split("@", 1)
            try:
                # The metadata of the EXACT installed version, not whatever
                # version of the package is current.
                meta = client.version_metadata(version_id)
            except SkillError:
                continue
            if meta.get("package_id") != package_id or meta.get("version_id") != version_id:
                continue
            record_package_owner(
                manager.store_root,
                origin,
                package_id,
                _item_owner(meta),
                _item_visibility(meta),
                version_id=version_id,
            )
    return True


def row_revocation(
    state: dict[str, Any],
    row: dict[str, Any],
    *,
    fingerprint: str | None,
    default_origin: str | None,
    cache: dict[str | None, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """The flag covering one installed row, evaluated against the flags of
    the SERVER the row is bound to (legacy rows: the active server)."""
    origin = hosted_origin(row.get("source")) or default_origin
    if cache is not None and origin in cache:
        flags = cache[origin]
    else:
        flags = effective_flags(state, fingerprint, origin)
        if cache is not None:
            cache[origin] = flags
    if not flags:
        return None
    return _row_flag(flags, row, state)


def revocation_warnings(
    manager: "SkillManager",
    scope: str | None = None,
    *,
    fingerprint: str | None = None,
    origin: str | None = None,
) -> list[dict[str, Any]]:
    """Installed rows covered by a yanked / quarantined / blocked flag (for
    list / doctor / pull). Only "blocked" is ever acted on (removed)."""
    state = HostedState(manager.store_root).read()
    if fingerprint is None and origin is None:
        fingerprint, origin = current_identity(manager)
    cache: dict[str | None, list[dict[str, Any]]] = {}
    warnings: list[dict[str, Any]] = []
    for row_scope in ((scope,) if scope else ("project", "user")):
        for row in _installed_rows(manager, row_scope):
            flag = row_revocation(
                state, row, fingerprint=fingerprint, default_origin=origin, cache=cache
            )
            if flag is not None:
                revision = row.get("revision", 0)
                warnings.append(
                    {
                        "scope": row_scope,
                        "package_id": str(row.get("package_id")),
                        "name": clean_untrusted_text(row.get("name"), limit=100,
                                                     keep_newlines=False),
                        "content_hash": str(row.get("content_hash")),
                        "revision": revision if isinstance(revision, int) else 0,
                        "state": flag.get("state"),
                        "reason": flag.get("reason") or "",
                    }
                )
    return warnings


def quarantine_root(store_root: Path) -> Path:
    return Path(store_root) / "quarantine"


MAX_ENFORCE_PASSES = 3


def enforce_blocked(
    manager: "SkillManager",
    *,
    scopes: tuple[str, ...] = ("project", "user"),
    fingerprint: str | None = None,
    origin: str | None = None,
    deadline: float | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Take every installed BLOCKED package out of the tool directories.

    Stops between packages when `deadline` passes or `cancel()` is true; a
    package already being moved is always finished (the move is one
    journaled transaction), never left half-quarantined.

    Every output - unchanged or modified - is moved into
    `~/.sessionfs/skills/quarantine/<package>/<timestamp>/` (recoverable)
    instead of being deleted, so a locally edited copy of blocked content can
    never stay active. The state row is kept until no output remains in
    place, so a partial removal is reported again and retried.

    The removal is bound to the row that was judged blocked: under the scope's
    operation lock the installed row must still have the same hash and
    revision AND still be blocked, otherwise nothing is touched and the pass
    restarts (a concurrently updated safe version is never quarantined).
    """
    from sessionfs.skills.manager import PackageChangedConcurrently

    if fingerprint is None and origin is None:
        fingerprint, origin = current_identity(manager)
    removed: list[dict[str, Any]] = []
    handled: set[tuple[str, str, str]] = set()
    for _ in range(MAX_ENFORCE_PASSES):
        restart = False
        for warning in revocation_warnings(
            manager, fingerprint=fingerprint, origin=origin
        ):
            if warning["state"] != "blocked" or warning["scope"] not in scopes:
                continue
            key = (warning["scope"], warning["package_id"], warning["content_hash"])
            if key in handled:
                continue
            if _halted(deadline, cancel):
                return removed  # before starting another package mutation

            def _still_blocked(row: dict[str, Any]) -> bool:
                flag = row_revocation(
                    HostedState(manager.store_root).read(),
                    row,
                    fingerprint=fingerprint,
                    default_origin=origin,
                )
                return flag is not None and flag.get("state") == "blocked"

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            target = quarantine_root(manager.store_root) / warning["package_id"] / stamp
            try:
                # scope -> operation -> hosted-state lock across the final
                # check and the quarantine: a restore merged concurrently
                # lands either before the check (left alone) or after the
                # quarantine - never in between.
                with (
                    _scope_lock(manager, warning["scope"]),
                    manager._operation_lock(warning["scope"]),
                    HostedState(manager.store_root).locked(),
                ):
                    result = manager.remove(
                        warning["package_id"],
                        scope=warning["scope"],
                        quarantine_dir=target,
                        expected_hash=warning["content_hash"],
                        expected_revision=warning["revision"],
                        recheck=_still_blocked,
                    )
            except PackageChangedConcurrently:
                restart = True  # re-judge the CURRENT row on the next pass
                break
            except SkillError as exc:
                handled.add(key)
                warning["error"] = str(exc)
                removed.append(warning)
                continue
            handled.add(key)
            if result.get("skipped"):
                continue  # no longer blocked once locked: leave it alone
            warning["removed_files"] = result.get("removed", [])
            warning["dirty"] = result.get("dirty", [])
            warning["failed"] = result.get("failed", [])
            warning["quarantine_dir"] = str(target) if result.get("quarantined") else None
            if result.get("failed"):
                warning["error"] = (
                    f"{len(result['failed'])} file(s) could not be moved out of the tool "
                    "directories and are still active"
                )
            if result.get("quarantined"):
                with HostedState(manager.store_root).edit() as state:
                    state["quarantined"].append(
                        {
                            "package_id": warning["package_id"],
                            "name": warning["name"],
                            "scope": warning["scope"],
                            "project_root": (
                                str(manager.project_root)
                                if warning["scope"] == "project"
                                else None
                            ),
                            "content_hash": warning["content_hash"],
                            "dir": str(target),
                            "outputs": result["quarantined"],
                            "at": _now(),
                        }
                    )
            removed.append(warning)
        if not restart:
            break
    return removed


def quarantine_leftovers(manager: "SkillManager", scope: str | None = None) -> list[dict[str, Any]]:
    """Quarantined blocked outputs that are ACTIVE again in a tool directory
    (restored by hand or re-created): doctor reports them."""
    leftovers: list[dict[str, Any]] = []
    for record in HostedState(manager.store_root).read()["quarantined"]:
        if not isinstance(record, dict):
            continue
        record_scope = record.get("scope")
        if scope and record_scope != scope:
            continue
        if record_scope == "project" and record.get("project_root") != str(manager.project_root):
            continue
        for output in record.get("outputs") or []:
            path = Path(str(output.get("from") or ""))
            if output.get("from") and (path.exists() or path.is_symlink()):
                leftovers.append(
                    {
                        "scope": str(record_scope),
                        "name": clean_untrusted_text(record.get("name"), limit=100,
                                                     keep_newlines=False),
                        "path": str(path),
                        "quarantine_dir": str(record.get("dir")),
                    }
                )
    return leftovers


# ---------------------------------------------------------------------------
# Pull: org assignments + approved install requests -> this device
# ---------------------------------------------------------------------------


@dataclass
class PullItem:
    mode: str  # "required" | "optional"
    version_id: str
    content_hash: str
    scope: str
    # Union of every origin's tools; [] = some origin is unrestricted.
    targets: list[str]
    # Every live (origin, origin_id) asking for this version in this scope.
    origins: list[tuple[str, str]] = field(default_factory=list)
    # Desired tools per origin ("<origin>|<id>" -> tools; [] = unrestricted).
    origin_targets: dict[str, list[str]] = field(default_factory=dict)
    # Server-approved capabilities PER ORIGIN ("<origin>|<id>" -> caps); None
    # for an assignment (it authorizes its own tools; local flags still apply).
    # Never unioned across origins: an approval covers only its own tools.
    origin_caps: dict[str, set[str] | None] = field(default_factory=dict)
    # Mode PER ORIGIN ("<origin>|<id>" -> "required" | "optional"): only
    # required origins' tools are reconciled automatically; an optional
    # origin's tools only once THAT origin was accepted (see `pull`).
    origin_modes: dict[str, str] = field(default_factory=dict)
    # installed | updated | up-to-date | skipped | offered | opted-out |
    # failed | deferred | held (an update that widens access, or changes
    # executable content, waiting for explicit approval; the installed version
    # stays)
    status: str = "pending"
    # D2 update class when this item replaces an installed version.
    update_class: str | None = None
    # Capability digest each origin's approver confirmed for THIS version
    # ("<origin>|<id>" -> digest; an org assignment's / approved request's
    # bound ``capability_digest``). An update that widens access applies
    # without --approve-widening only for the tools of origins that hold the
    # version's locally computed digest themselves (never another origin's).
    origin_digests: dict[str, str] = field(default_factory=dict)
    # Two or more DIFFERENT versions of this package are granted for the same
    # scope: never installed / replaced by this pull (no flip-flop, never a
    # downgrade); reported instead.
    conflict: bool = False
    # The user explicitly removed this optional version before (tombstone).
    opted_out: bool = False
    detail: str = ""
    name: str = ""
    package_id: str = ""
    # install_id of the installation THIS pull operation created (a fresh
    # install, not an update of an existing row); the only installation it
    # may record itself as installer of without earlier authority.
    created_install_id: str | None = None
    # Installed tools of this package that no live origin asks for any more
    # (only reported - pull never removes a tool) and its local package id.
    unassigned_tools: list[str] = field(default_factory=list)
    local_package_id: str = ""

    @property
    def origin(self) -> str:
        kinds = {kind for kind, _ in self.origins}
        return next(iter(kinds)) if len(kinds) == 1 else "+".join(sorted(kinds))

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "origins": [
                {
                    "origin": kind,
                    "id": origin_id,
                    "targets": list(self.origin_targets.get(f"{kind}|{origin_id}", [])),
                    "approved_capabilities": (
                        sorted(caps)
                        if (caps := self.origin_caps.get(f"{kind}|{origin_id}")) is not None
                        else None
                    ),
                }
                for kind, origin_id in self.origins
            ],
            "mode": self.mode,
            "version_id": self.version_id,
            "content_hash": self.content_hash,
            "scope": self.scope,
            "targets": list(self.targets),
            "status": self.status,
            "opted_out": self.opted_out,
            "detail": self.detail,
            "name": self.name,
            "package_id": self.package_id,
            "update_class": self.update_class,
        }


@dataclass
class PullReport:
    items: list[PullItem] = field(default_factory=list)
    # Packages pull manages that are no longer assigned or approved: only
    # reported (`sfs skill remove` removes one); pull never uninstalls them.
    unassigned: list[dict[str, Any]] = field(default_factory=list)
    # Interrupted pull operations that could not be settled yet (kept and
    # retried), with what would settle them.
    recovery: list[dict[str, Any]] = field(default_factory=list)
    # Tools of installed packages that are no longer assigned: only reported
    # (`sfs skill compile <id> --drop <tool>` removes one); never removed.
    unassigned_tools: list[dict[str, Any]] = field(default_factory=list)
    blocked_removed: list[dict[str, Any]] = field(default_factory=list)
    # Grants the org skills policy refuses (never installed by this pull),
    # and the compliance report sent for this device (marketplace v2 §8.4).
    policy_refused: list[dict[str, Any]] = field(default_factory=list)
    policy_reports: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    revocations_supported: bool = True
    revocations_complete: bool = True
    # Whether each source ("assignment", "request") was listed completely.
    # Stale origins are only reconciled for a source that completed: an
    # unreadable listing is never treated as "nothing is assigned".
    complete_sources: dict[str, bool] = field(default_factory=dict)
    # Called with each note as soon as it is recorded (before any prompt).
    notify: Callable[[str], None] | None = field(default=None, repr=False, compare=False)
    # Called with each pending-recovery message as soon as it is found
    # (falls back to `notify`).
    on_recovery: Callable[[str], None] | None = field(default=None, repr=False, compare=False)

    def note(self, message: str) -> None:
        self.notes.append(message)
        if self.notify is not None:
            self.notify(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [item.as_dict() for item in self.items],
            "unassigned": self.unassigned,
            "recovery": self.recovery,
            "unassigned_tools": self.unassigned_tools,
            "blocked_removed": self.blocked_removed,
            "policy_refused": self.policy_refused,
            "policy_reports": self.policy_reports,
            "notes": self.notes,
            "revocations_supported": self.revocations_supported,
            "revocations_complete": self.revocations_complete,
            "complete_sources": dict(self.complete_sources),
        }


def resolve_device_id(value: str | None = None) -> str | None:
    raw = value if value is not None else os.environ.get("SESSIONFS_DEVICE_ID")
    if raw is None or not str(raw).strip():
        return None
    try:
        return validate_device_id(raw)
    except ValueError as exc:
        raise SkillError(str(exc)) from exc


def _scope_root(manager: "SkillManager", scope: str) -> str | None:
    return str(manager.project_root) if scope == "project" else None


def _project_id(manager: "SkillManager", client: HostedClient) -> str:
    """Project of the current repository. Raises (never "no project") when
    it cannot be resolved."""
    import subprocess  # nosec B404 - fixed argv, no shell

    from sessionfs.project_root import find_git_toplevel

    if find_git_toplevel(manager.project_root) is None:
        raise SkillError("this directory is not inside a git repository")
    try:
        result = subprocess.run(  # nosec B603 B607
            ["git", "remote", "get-url", "origin"],
            cwd=manager.project_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SkillError("could not read the repository's origin remote") from exc
    remote = result.stdout.strip() if result.returncode == 0 else ""
    if not remote:
        raise SkillError("the repository has no origin remote")
    from sessionfs.git_remote import normalize_git_remote

    return client.project_id_for_remote(normalize_git_remote(remote))


def _merge_item(
    items: dict[tuple[str, str], PullItem],
    *,
    origin: str,
    origin_id: str,
    mode: str,
    version_id: str,
    content_hash: str,
    scope: str,
    tools: list[str],
    approved: set[str] | None = None,
    approved_digest: Any = None,
    package_id: Any = None,
) -> None:
    """Aggregate every origin asking for the same version in the same scope
    (one download). Every origin keeps its own tools and MODE; the item's
    `targets` / `mode` are only a summary - `pull` installs the tools of the
    required origins plus those of each optional origin that was accepted."""
    key = (scope, version_id)
    origin_key = f"{origin}|{origin_id}"
    item = items.get(key)
    if item is None:
        items[key] = PullItem(
            mode=mode,
            version_id=version_id,
            content_hash=content_hash,
            scope=scope,
            targets=list(tools),
            origins=[(origin, origin_id)],
            origin_targets={origin_key: list(tools)},
            origin_caps={origin_key: set(approved) if approved is not None else None},
            origin_modes={origin_key: mode},
            origin_digests=(
                {origin_key: approved_digest}
                if isinstance(approved_digest, str) and approved_digest
                else {}
            ),
            package_id=str(package_id) if isinstance(package_id, str) and package_id else "",
        )
        return
    if isinstance(package_id, str) and package_id and not item.package_id:
        item.package_id = package_id
    if isinstance(approved_digest, str) and approved_digest:
        item.origin_digests[origin_key] = approved_digest
    else:
        item.origin_digests.pop(origin_key, None)
    if (origin, origin_id) not in item.origins:
        item.origins.append((origin, origin_id))
    item.origin_targets[origin_key] = list(tools)
    item.origin_caps[origin_key] = set(approved) if approved is not None else None
    item.origin_modes[origin_key] = mode
    if mode == "required":
        item.mode = "required"
    if not item.targets or not tools:
        item.targets = []  # unrestricted wins
    else:
        item.targets = list(dict.fromkeys([*item.targets, *tools]))


def _origin_view(item: PullItem, active: list[tuple[str, str]]) -> PullItem:
    """`item` limited to the origins in `active`: their tools (union), their
    approvals and modes only - what is installed, authorized and recorded."""
    keys = {f"{kind}|{origin_id}" for kind, origin_id in active}
    targets = {
        key: tools for key, tools in item.origin_targets.items() if key in keys
    }
    modes = {key: mode for key, mode in item.origin_modes.items() if key in keys}
    return replace(
        item,
        origins=list(active),
        origin_targets=targets,
        origin_caps={key: caps for key, caps in item.origin_caps.items() if key in keys},
        origin_modes=modes,
        # A digest binds only its own origin's approval.
        origin_digests={key: value for key, value in item.origin_digests.items() if key in keys},
        targets=list(dict.fromkeys(tool for tools in targets.values() for tool in tools)),
        mode="required" if "required" in modes.values() else "optional",
    )


def _covered(active: PullItem, optional: PullItem) -> bool:
    """An optional origin adds NOTHING beyond the active origins: every one
    of its tools is targeted by an active origin that authorizes at least
    what it authorizes (an assignment - no capability list - authorizes its
    own tools fully; an install request only its approved capabilities).
    Otherwise it is offered."""
    optional_caps = next(iter(optional.origin_caps.values()), None) if optional.origin_caps else None
    for tool in optional.targets:
        if not any(
            tool in tools
            and (
                (caps := active.origin_caps.get(key)) is None
                or (optional_caps is not None and optional_caps <= caps)
            )
            for key, tools in active.origin_targets.items()
        ):
            return False
    return True


def _note_offered(item: PullItem, offered: list[PullItem]) -> None:
    if not offered:
        return
    pending = ", ".join(
        f"{'+'.join(view.targets) or 'all tools'} "
        f"({'opted out' if view.opted_out else 'optional, not accepted'})"
        for view in offered
    )
    item.detail = "; ".join(filter(None, [item.detail, f"not installed: {pending}"]))


def _required_origins(item: PullItem) -> list[tuple[str, str]]:
    return [
        (kind, origin_id) for kind, origin_id in item.origins
        if item.origin_modes.get(f"{kind}|{origin_id}", item.mode) == "required"
    ]


def collect_pull_items(
    manager: "SkillManager",
    client: HostedClient,
    *,
    device_id: str | None,
    include_requests: bool = True,
    project_id: str | None = None,
    report: PullReport | None = None,
    deadline: float | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[PullItem]:
    """Assignments + approved install requests for this device.

    Each source's completeness is recorded on `report.complete_sources`: any
    failure (403, 5xx, network, an unresolvable project, a server too old for
    the caller) marks that source INCOMPLETE with a note - it is never
    returned as an empty listing.
    """
    report = report if report is not None else PullReport()
    items: dict[tuple[str, str], PullItem] = {}
    identity = client.identity()
    try:
        assignments = client.assignments(device_id, deadline=deadline, cancel=cancel)
        report.complete_sources["assignment"] = True
    except SkillError as exc:
        assignments = []
        report.complete_sources["assignment"] = False
        report.note(f"Org assignments could not be read; nothing was changed for them: {exc}")
    _note_server_omitted(assignments, "assignment", "org assignment(s)", report)
    for refused in getattr(assignments, "policy_refused", []):
        _record_policy_refusal(report, "assignment", refused, refused.get("policy"))
    malformed = 0
    for row in assignments:
        if not _valid_row(row, mode_field=True):
            malformed += 1
            continue
        tools = _usable_tools(row, "assignment", report)
        if tools is None:
            continue
        _merge_item(
            items,
            origin="assignment",
            origin_id=str(row["id"]),
            mode=str(row.get("mode") or "optional"),
            version_id=str(row["version_id"]),
            content_hash=str(row["content_hash"]),
            # Org assignments are device-wide: they land in user scope.
            scope="user",
            tools=tools,
            approved_digest=row.get("capability_digest"),
            package_id=row.get("package_id"),
        )
    if malformed:
        report.complete_sources["assignment"] = False
        report.note(
            f"{malformed} org assignment(s) could not be interpreted and were ignored; "
            "nothing pulled from org assignments is removed this time"
        )
    if not include_requests:
        return list(items.values())
    service = identity["kind"] == "service"
    try:
        resolved_project = project_id or _project_id(manager, client)
        requests = client.approved_install_requests(
            resolved_project, deadline=deadline, cancel=cancel
        )
        report.complete_sources["request"] = True
    except ServiceKeyNotAllowed:
        requests = []
        report.complete_sources["request"] = False
        report.note(
            "Install requests skipped: this server cannot resolve the project for a service "
            "key from the git remote; pass --project <project id>"
        )
    except SkillError as exc:
        requests = []
        report.complete_sources["request"] = False
        report.note(f"Install requests could not be read; nothing was changed for them: {exc}")
    _note_server_omitted(requests, "request", "install request(s)", report)
    user_id = str(identity.get("user_id") or "")
    malformed = 0
    for row in requests:
        if not _valid_row(row, request=True):
            malformed += 1
            continue
        target_scope = row.get("target_scope")
        if target_scope == "project":
            pass  # project grants apply to whoever installs in this project
        elif service:
            continue  # user/device grants belong to a person, never a service key
        elif row.get("target_user_id") != user_id:
            continue
        elif target_scope == "device" and (
            not device_id or row.get("target_device_id") != device_id
        ):
            continue
        verdict = row.get("policy")
        if isinstance(verdict, dict) and verdict.get("allowed") is False:
            # The org's CURRENT skills policy refuses this approved request.
            _record_policy_refusal(report, "request", row, verdict)
            continue
        tools = _usable_tools(row, "request", report)
        if tools is None:
            continue
        _merge_item(
            items,
            origin="request",
            origin_id=str(row["id"]),
            mode="required",
            version_id=str(row["version_id"]),
            content_hash=str(row["content_hash"]),
            scope="project" if target_scope == "project" else "user",
            tools=tools,
            approved={str(cap) for cap in row.get("requested_capabilities") or []}
            | {"instructions"},
            approved_digest=row.get("capability_digest"),
            package_id=row.get("package_id"),
        )
    if malformed:
        report.complete_sources["request"] = False
        report.note(
            f"{malformed} install request(s) could not be interpreted and were ignored; "
            "nothing pulled from install requests is removed this time"
        )
    return list(items.values())


def _record_policy_refusal(
    report: PullReport, origin: str, row: dict[str, Any], verdict: Any
) -> None:
    verdict = verdict if isinstance(verdict, dict) else {}
    violations = [
        str(item.get("code"))
        for item in verdict.get("violations") or []
        if isinstance(item, dict) and item.get("code")
    ][:20]
    message = str(verdict.get("message") or "Blocked by your organization's skills policy.")[:1000]
    entry = {
        "origin": origin,
        "id": str(row.get("assignment_id") or row.get("id") or "")[:100],
        "org_id": str(verdict.get("org_id") or row.get("org_id") or "")[:64],
        "version_id": str(row.get("version_id") or "")[:100],
        "package_id": str(row.get("package_id") or "")[:100],
        "policy_revision": verdict.get("policy_revision"),
        "violations": violations,
        "message": message,
    }
    report.policy_refused.append(entry)
    report.note(f"Not installed ({origin} {entry['version_id']}): {message}")


def _note_server_omitted(listing: Any, source: str, label: str, report: PullReport) -> None:
    """The server left out records it could not read: that source is
    incomplete (no stale-origin cleanup); the rows it did return still apply."""
    if isinstance(listing, HostedListing) and listing.truncated:
        report.complete_sources[source] = False
        why = (
            "stopped at the time limit"
            if listing.interrupted
            else "the listing could not be paged to its end"
        )
        report.note(
            f"Not every {label} could be read ({why}); "
            f"nothing pulled from {source}s is removed this time"
        )
    incomplete, omitted = _server_omitted(listing)
    if not incomplete:
        return
    report.complete_sources[source] = False
    count = f"{omitted} " if omitted else "some "
    report.note(
        f"The server omitted {count}{label} it could not read; nothing pulled from "
        f"{source}s is removed this time"
    )


def _valid_row(row: Any, *, mode_field: bool = False, request: bool = False) -> bool:
    """A listing row that can be interpreted SAFELY. Anything else marks its
    source incomplete (so stale-origin cleanup is suppressed)."""
    if not isinstance(row, dict):
        return False
    if not isinstance(row.get("id"), str) or not row["id"]:
        return False
    if not isinstance(row.get("version_id"), str) or not _VERSION_ID_RE.fullmatch(row["version_id"]):
        return False
    if not isinstance(row.get("content_hash"), str) or not _CONTENT_HASH_RE.fullmatch(
        row["content_hash"]
    ):
        return False
    # Targeting must be an explicit list: null, missing or anything else is
    # invalid (never coerced into "unrestricted").
    tools = row.get("target_tools")
    if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
        return False
    if mode_field and row.get("mode") not in {"required", "optional"}:
        return False
    if request:
        if row.get("status") != "approved":
            return False
        if row.get("target_scope") not in {"project", "user", "device"}:
            return False
        caps = row.get("requested_capabilities")
        if not isinstance(caps, list) or not all(isinstance(cap, str) for cap in caps):
            return False
    return True


def _usable_tools(row: dict[str, Any], source: str, report: PullReport) -> list[str] | None:
    """Target tools this client can act on. An EMPTY server list means
    unrestricted ([]). A non-empty list naming only tools this sfs does not
    support is skipped (None) with a note and marks the source incomplete -
    it is never widened to "all tools". Partially unsupported lists keep the
    supported tools."""
    raw = list(dict.fromkeys(row.get("target_tools") or []))
    supported = [tool for tool in raw if tool in SUPPORTED_TOOLS]
    unsupported = [tool for tool in raw if tool not in SUPPORTED_TOOLS]
    names = ", ".join(clean_untrusted_text(tool, limit=40, keep_newlines=False) for tool in unsupported)
    if raw and not supported:
        report.complete_sources[source] = False
        report.note(
            f"Skipped {source} {clean_untrusted_text(row.get('id'), limit=40, keep_newlines=False)}: "
            f"it targets only tools this version of sfs doesn't support: {names}"
        )
        return None
    if unsupported:
        report.note(
            f"{source} {clean_untrusted_text(row.get('id'), limit=40, keep_newlines=False)}: "
            f"ignoring tools this version of sfs doesn't support: {names}"
        )
    return supported


def _installed_rows(
    manager: "SkillManager", scope: str, *, strict: bool = False
) -> list[dict[str, Any]]:
    """Installed rows of a scope. Lenient by default (an unreadable state
    reads as empty); `strict=True` raises instead, for decisions that must
    not mistake a read error for "nothing is installed"."""
    try:
        packages = manager.state_store(scope).state()["packages"]
    except SkillError:
        if strict:
            raise
        return []
    if strict:
        # Nothing malformed is ever silently skipped: a decision made on a
        # partial view could miss the very row it is about.
        for key, row in packages.items():
            if not isinstance(row, dict):
                raise SkillError(f"Installed state row {key!r} is not an object")
            if row.get("package_id") != key:
                raise SkillError(f"Installed state row {key!r} has a missing or different package id")
            install_id = row.get("install_id")
            if install_id is not None and not (isinstance(install_id, str) and install_id):
                raise SkillError(f"Installed state row {key!r} has an invalid install id")
            if not isinstance(row.get("source"), dict) or not isinstance(row.get("content_hash"), str):
                raise SkillError(f"Installed state row {key!r} has no valid source identity")
    return [row for row in packages.values() if isinstance(row, dict)]


_FLAG_FOR_REQUIREMENT = {
    "skill-scripts": ("allow_scripts", "--allow-scripts"),
    "skill-hooks": ("allow_hooks", "--allow-hooks"),
    "mcp": ("enable_mcp", "--enable-mcp"),
    "executable-extensions": ("allow_executable_extensions", "--allow-executable-extensions"),
    "dlp-findings": ("allow_dlp_findings", "--allow-dlp-findings"),
}


def _origin_authorizes(item: PullItem, tool: str, requirements: list[str]) -> bool:
    """True when SOME origin that targets `tool` authorizes every elevated
    requirement of that tool's plan. An assignment authorizes its own tools
    (local consent flags still apply); an install request only the
    capabilities approved on it, and only for its own tools."""
    needed = {
        _REQUIREMENT_TO_SERVER_CAPABILITY[req]
        for req in requirements
        if req in _REQUIREMENT_TO_SERVER_CAPABILITY
    }
    # A requirement with no server capability can never be approved on a
    # request (fail closed).
    unmapped = any(req not in _REQUIREMENT_TO_SERVER_CAPABILITY for req in requirements)
    for key, tools in item.origin_targets.items():
        if tool not in tools:
            continue
        caps = item.origin_caps.get(key)
        if caps is None or (not unmapped and needed <= caps):
            return True
    return False


def _authorize_targets(
    manager: "SkillManager",
    item: PullItem,
    package: PackageRecord,
    candidates: list[str],
    flags: dict[str, bool],
    tool_flags: dict[str, dict[str, bool]] | None = None,
) -> tuple[list[str], list[str]]:
    """Per tool: keep a candidate only if every elevated capability its plan
    needs has the local flag AND is authorized by an origin covering that
    tool. Returns (authorized tools, notes for the excluded ones)."""
    authorized: list[str] = []
    notes: list[str] = []
    for plan in manager.plan(package, targets=candidates, scope=item.scope):
        requirements = sorted(plan.consent_requirements)
        unknown = [req for req in requirements if req not in _FLAG_FOR_REQUIREMENT]
        if unknown:
            # A consent requirement this pull cannot grant: refuse the tool.
            notes.append(f"{plan.tool} needs consent pull cannot give: {', '.join(unknown)}")
            continue
        plan_flags = (tool_flags or {}).get(plan.tool, flags)
        missing = [
            _FLAG_FOR_REQUIREMENT[req][1]
            for req in requirements
            if not plan_flags.get(_FLAG_FOR_REQUIREMENT[req][0])
        ]
        if missing:
            notes.append(f"{plan.tool} needs {', '.join(missing)}")
        elif not _origin_authorizes(item, plan.tool, requirements):
            notes.append(f"{plan.tool}: not approved on an install request for this tool")
        else:
            authorized.append(plan.tool)
    return authorized, notes


_REQUIREMENT_FLAG = {req: flag for req, (flag, _option) in _FLAG_FOR_REQUIREMENT.items()}


def _update_flags(
    manager: "SkillManager",
    item: PullItem,
    current: dict[str, Any],
    package: PackageRecord,
    flags: dict[str, bool],
    new_manifest: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, bool]] | None, dict[str, Any], set[str] | None]:
    """D2 re-consent for an assigned / approved version that REPLACES an
    installed one (marketplace v2 §3.4).

    The third value restricts authorization to these origins (a widening
    approved through bound digests: only the origins that hold the digest
    may authorize tools); None means every origin. The first value maps each
    tool to its effective consent flags: explicit flags plus only the consent
    carried by receipts bound to that tool and scope.

    Returns (effective flags, consent context), or (None, context) when the
    update must be HELD: it widens access without an explicit
    ``approve_widening`` (the daemon never has one), or it changes executable
    content whose capability was not consented again. A held update leaves
    the installed version in place.
    """
    from sessionfs.skills import disclosure

    _old, new_manifest, diff = manager.classify_update(
        current, package, scope=item.scope, include_text=False, new_manifest=new_manifest
    )
    update_class = str(diff.get("update_class"))
    item.update_class = update_class
    context: dict[str, Any] = {
        **manager.consent_snapshot(package, new_manifest),
        "update_class": update_class,
    }
    digest = context.get("capability_digest")
    restrict: set[str] | None = None
    if update_class == "widening" and not flags.get("approve_widening"):
        restrict = {
            key for key, approved in item.origin_digests.items()
            if isinstance(digest, str) and approved == digest and key in item.origin_targets
        }
        if not restrict:
            widened = ", ".join(f"{entry['kind']}: {entry['item']}" for entry in diff.get("widens") or [])
            item.detail = f"update widens access ({widened}); approve with `sfs skill pull --approve-widening`"
            return None, context, None
    receipts = current.get("consent_receipts") or []
    consented = {
        str(receipt.get("capability"))
        for receipt in receipts
        if isinstance(receipt, dict) and receipt.get("capability")
    }
    carried = disclosure.carried_requirements(diff, consented)
    carry_diff = diff
    if update_class == "code":
        org_id = (package.provenance or {}).get("owner_org_id")
        if disclosure.trusted_code_update_allowed(
            org_id=org_id if isinstance(org_id, str) else None,
            old_provenance=current.get("provenance") if isinstance(current.get("provenance"), dict) else None,
            new_provenance=package.provenance,
        ):
            carried = disclosure.carried_requirements({"update_class": "text"}, consented)
            carry_diff = {"update_class": "text"}
        else:
            needed = set(disclosure.consent_requirements(new_manifest))
            missing = [
                _FLAG_FOR_REQUIREMENT[req][1]
                for req in diff.get("changed_requirements") or []
                if req in needed
                and req in _FLAG_FOR_REQUIREMENT
                and not flags.get(_FLAG_FOR_REQUIREMENT[req][0])
            ]
            if missing:
                item.detail = (
                    "update changes executable content; approve with `sfs skill pull "
                    + " ".join(missing) + "`"
                )
                return None, context, None
    tool_flags = {
        tool: {
            **flags,
            **disclosure.flags_for_tool(
                flags,
                disclosure.carried_for_tool(
                    {**carry_diff, "baseline_digest": diff.get("baseline_digest")},
                    receipts,
                    tool=tool,
                    scope=item.scope,
                    content_hash=str(current.get("content_hash") or ""),
                ),
            ),
        }
        for tool in SUPPORTED_TOOLS
    }
    context["carried_requirements"] = sorted(carried)
    context["baseline_content_hash"] = current.get("content_hash")
    context["baseline_digest"] = diff.get("baseline_digest")
    if restrict is not None:
        context["digest_authorized_origins"] = sorted(restrict)
    else:
        context["digest_authorized_origins"] = None
    return tool_flags, context, restrict


def _authorized_by_origin(
    manager: "SkillManager",
    item: PullItem,
    package: PackageRecord,
    authorized: list[str],
    flags: dict[str, bool],
    tool_flags: dict[str, dict[str, bool]] | None = None,
) -> dict[str, list[str]]:
    """For each origin, the tools of `authorized` that origin authorizes ON
    ITS OWN (its tools, its approvals, the local flags). A recovery that
    keeps only some origins restores exactly these - never a tool another
    origin's approval let in."""
    result: dict[str, list[str]] = {}
    for kind, origin_id in item.origins:
        key = f"{kind}|{origin_id}"
        wanted = [tool for tool in authorized if tool in item.origin_targets.get(key, [])]
        if not wanted:
            result[key] = []
            continue
        tools, _notes = _authorize_targets(
            manager, _origin_view(item, [(kind, origin_id)]), package, wanted, flags,
            tool_flags=tool_flags,
        )
        result[key] = tools
    return result


def _consent_kwargs(flags: dict[str, bool]) -> dict[str, bool]:
    return {
        "allow_scripts": flags.get("allow_scripts", False),
        "enable_mcp": flags.get("enable_mcp", False),
        "allow_executable_extensions": flags.get("allow_executable_extensions", False),
        "allow_hooks": flags.get("allow_hooks", False),
        "allow_dlp_findings": flags.get("allow_dlp_findings", False),
    }


def _expand_targets(item: PullItem, default_targets: list[str]) -> None:
    """Replace "unrestricted" (an empty tool list) by the effective default
    tool set, per origin and for the item, so a change between a restricted
    and an unrestricted assignment adds/drops the right tools and the
    persisted managed set is explicit."""
    for key, tools in list(item.origin_targets.items()):
        if not tools:
            item.origin_targets[key] = list(default_targets)
    if not item.origin_targets:
        item.targets = list(item.targets or default_targets)
        return
    item.targets = list(
        dict.fromkeys(tool for tools in item.origin_targets.values() for tool in tools)
    )


def _matches_item(row: dict[str, Any], item: PullItem, origin: str) -> bool:
    """The installed row IS this hosted version from this server (same origin,
    package and version) - not merely identical bytes from somewhere else."""
    raw = row.get("source")
    source: dict[str, Any] = raw if isinstance(raw, dict) else {}
    package_id, row_origin = _split_locator(source)
    return (
        package_id is not None
        and row_origin == origin
        and source.get("commit") == item.version_id
        and (not item.package_id or package_id == item.package_id)
    )


def _same_bytes_elsewhere(
    rows: list[dict[str, Any]], item: PullItem, origin: str
) -> dict[str, Any] | None:
    return next(
        (
            row for row in rows
            if row.get("content_hash") == item.content_hash and not _matches_item(row, item, origin)
        ),
        None,
    )


def _describe_source(row: dict[str, Any]) -> str:
    raw = row.get("source")
    source: dict[str, Any] = raw if isinstance(raw, dict) else {}
    package_id, row_origin = _split_locator(source)
    if package_id is not None:
        return f"hosted package {package_id} from {row_origin or 'an unrecorded server'}"
    return clean_untrusted_text(
        f"{source.get('source_type') or 'unknown'} source {source.get('locator') or ''}".strip(),
        limit=200,
        keep_newlines=False,
    )


def _pending_recovery_for(
    hosted_state: HostedState, pending: set[str], row: dict[str, Any]
) -> str | None:
    """Why a pull must not change this row right now (None: it may). The
    row's installation is still awaiting settlement from an earlier
    interrupted pull, or it is still stamped by another live intent that
    appeared after this run's recovery pass. A pull mutation re-stamps the
    row in the same write, which would destroy the interrupted operation's
    recovery evidence (an unrecorded change it made could never be undone)."""
    install_id = row.get("install_id")
    if isinstance(install_id, str) and install_id and install_id in pending:
        return "an interrupted install of this package is still being settled"
    stamp = row.get("pull_op")
    if isinstance(stamp, str) and stamp and stamp in hosted_state.read()["pull_ops"]:
        return "another interrupted pull operation still owns this installation"
    return None


def _reconcile_targets(
    manager: "SkillManager",
    item: PullItem,
    row: dict[str, Any],
    *,
    previous_requested: set[str],
    flags: dict[str, bool],
    op_token: str | None = None,
    on_authorized: Callable[[dict[str, list[str]]], None] | None = None,
) -> None:
    """Content already installed: ADD the tools the live origins now want
    (and authorize). Pull never removes a tool: an installed tool that an
    earlier origin of this namespace asked for and no live origin asks for
    any more is only reported as no longer assigned. Goes through the normal
    compile, so consent is re-checked."""
    installed = [str(tool) for tool in row.get("targets") or []]
    item.local_package_id = str(row.get("package_id"))
    item.unassigned_tools = sorted(
        tool for tool in installed if tool in previous_requested and tool not in item.targets
    )
    if not item.targets:
        return
    from sessionfs.skills import disclosure

    package_id = str(row.get("package_id"))
    package = manager.load_installed_package(package_id, scope=item.scope)
    auth_item = item
    authorized_origins = row.get("digest_authorized_origins")
    if isinstance(authorized_origins, list) and flags.get("approve_widening"):
        # Explicit approval lifts the restriction durably, even when no tool
        # changes on this pull.
        manager.state_store(item.scope).clear_digest_restriction(package_id)
        authorized_origins = None
    if isinstance(authorized_origins, list):
        # This content was a widening applied through bound digests: only
        # the origins that approved it (or that now bind its digest) may
        # authorize tools, until the user approves the widening explicitly.
        digest = row.get("capability_digest")
        allowed = set(authorized_origins) | {
            key for key, approved in item.origin_digests.items()
            if isinstance(digest, str) and approved == digest
        }
        origin_targets = {k: v for k, v in item.origin_targets.items() if k in allowed}
        auth_item = replace(
            item,
            origin_targets=origin_targets,
            origin_caps={k: v for k, v in item.origin_caps.items() if k in allowed},
            targets=list(dict.fromkeys(t for tools in origin_targets.values() for t in tools)),
        )
    if auth_item.targets:
        desired, notes = _authorize_targets(manager, auth_item, package, list(auth_item.targets), flags)
    else:
        desired, notes = [], ["no origin approved this version's access for another tool"]
    add = [tool for tool in desired if tool not in installed]
    if notes:
        item.detail = "; ".join(notes)
    if add and not flags.get("allow_hidden_unicode") and disclosure.hidden_unicode_blocks(
        manager.capability_manifest(package)
    ):
        # Same gate as a fresh install: hold the additions, keep every
        # existing output.
        item.status = "held"
        item.detail = (
            f"adding {', '.join(add)} needs approval: the package contains hidden or control "
            "characters (`sfs skill pull --allow-hidden-unicode`)"
        )
        return
    if not add:
        return
    if on_authorized is not None:
        # Durable BEFORE the row changes (its recovery undoes only this op).
        # Per origin as `auth_item` authorizes them: an origin a digest
        # restriction excludes authorizes nothing.
        on_authorized(_authorized_by_origin(manager, auth_item, package, desired, flags))
    manager.compile(
        package_id, scope=item.scope, targets=add, pull_op=op_token,
        allow_hidden_unicode=bool(flags.get("allow_hidden_unicode")), **_consent_kwargs(flags),
    )
    item.status = "updated"
    change = " ".join(f"+{tool}" for tool in add)
    item.detail = change + (f" ({'; '.join(notes)})" if notes else "")


def _install_item(
    manager: "SkillManager",
    client: HostedClient,
    item: PullItem,
    *,
    origin: str,
    default_targets: list[str],
    flags: dict[str, bool],
    managed_ids: set[str],
    previous_requested_for: Callable[[str | None], set[str]],
    fingerprint: str | None,
    hosted_state: HostedState,
    namespace: str,
    pending_recovery: set[str],
    op_token: str | None = None,
    on_authorized: Callable[[dict[str, list[str]]], None] | None = None,
) -> None:
    rows = _installed_rows(manager, item.scope)
    existing = next((row for row in rows if _matches_item(row, item, origin)), None)
    if existing is not None:
        item.status = "up-to-date"
        item.name = clean_untrusted_text(existing.get("name"), limit=100, keep_newlines=False)
        if str(existing.get("package_id")) in managed_ids:
            reason = _pending_recovery_for(hosted_state, pending_recovery, existing)
            if reason is not None:
                # Never re-stamp a row whose recovery is still pending.
                _finish_pull_op(hosted_state, op_token)  # this op changed nothing
                item.status = "deferred"
                item.local_package_id = str(existing.get("package_id"))
                item.detail = f"{reason}; retried by the next pull"
                return
            _reconcile_targets(
                manager, item, existing,
                previous_requested=previous_requested_for(str(existing.get("package_id"))),
                flags=flags, op_token=op_token, on_authorized=on_authorized,
            )
        return
    elsewhere = _same_bytes_elsewhere(rows, item, origin)
    if elsewhere is not None:
        # Identical bytes, but a different package, server or source: never
        # claim it as this version, never attach this origin to that row.
        item.status = "skipped"
        item.name = clean_untrusted_text(elsewhere.get("name"), limit=100, keep_newlines=False)
        item.detail = (
            f"conflict: identical content is already installed as {item.name} "
            f"({_describe_source(elsewhere)}); not installing a second copy"
        )
        return
    metadata = client.version_metadata(item.version_id)
    if metadata.get("version_id") != item.version_id:
        raise SkillError("Hosted marketplace returned metadata for a different version")
    item.name = clean_untrusted_text(metadata.get("name"), limit=100, keep_newlines=False)
    item.package_id = str(metadata.get("package_id") or "")
    package = import_hosted_version(
        client,
        metadata,
        manager.store_root,
        expected_hash=item.content_hash,
        fingerprint=fingerprint,
    )
    if op_token is not None:
        # Stamped onto the installed row IN THE SAME WRITE as the install;
        # any later change that is not this operation's clears it.
        package = replace(package, pull_op=op_token)
    # The installed row of this hosted package (any version), resolved by its
    # local id or by its hosted package id on this server.
    current = next(
        (
            row for row in rows
            if row.get("package_id") == package.package_id
            or (
                item.package_id
                and _split_locator(
                    row.get("source") if isinstance(row.get("source"), dict) else {}
                ) == (item.package_id, origin)
            )
        ),
        None,
    )
    if current is not None and str(current.get("package_id")) not in managed_ids:
        # A different version the user installed themselves: never replace it.
        item.status = "skipped"
        item.detail = (
            f"{package.name} is installed at {current.get('content_hash')} by the user; "
            "not replacing it with the assigned version"
        )
        return
    if current is not None:
        reason = _pending_recovery_for(hosted_state, pending_recovery, current)
        if reason is not None:
            # Never re-stamp a row whose recovery is still pending.
            _finish_pull_op(hosted_state, op_token)  # this op changed nothing
            item.status = "deferred"
            item.local_package_id = str(current.get("package_id"))
            item.detail = f"{reason}; retried by the next pull"
            return
    from sessionfs.skills import disclosure

    new_manifest = manager.capability_manifest(package)
    if disclosure.hidden_unicode_blocks(new_manifest) and not flags.get("allow_hidden_unicode"):
        # Same rule as `sfs skill add`: never installed unattended.
        item.status = "held"
        item.detail = (
            "package contains hidden or control characters; review it and run "
            "`sfs skill pull --allow-hidden-unicode`"
        )
        return
    consent_context: dict[str, Any] | None = {
        **manager.consent_snapshot(package, new_manifest),
        "digest_authorized_origins": None,
    }
    auth_item = item
    tool_flags: dict[str, dict[str, bool]] | None = None
    restrict: set[str] | None = None
    if current is not None and current.get("content_hash") != package.content_hash:
        # D2 re-consent BEFORE anything changes: a widening without explicit
        # approval (or a bound digest) and an executable change without its
        # flag are HELD - the installed version stays on every tool.
        tool_flags, consent_context, restrict = _update_flags(
            manager, item, current, package, flags, new_manifest=new_manifest
        )
        if tool_flags is None:
            item.status = "held"
            item.local_package_id = str(current.get("package_id"))
            return
        if restrict is not None:
            # Only the origins that approved THIS digest may authorize tools.
            origin_targets = {k: v for k, v in item.origin_targets.items() if k in restrict}
            auth_item = replace(
                item,
                origin_targets=origin_targets,
                origin_caps={k: v for k, v in item.origin_caps.items() if k in restrict},
                origin_digests={k: v for k, v in item.origin_digests.items() if k in restrict},
                targets=list(dict.fromkeys(t for tools in origin_targets.values() for t in tools)),
            )
    current_tools = [str(tool) for tool in current.get("targets") or []] if current else []
    requested = auth_item.targets or (default_targets if auth_item is item else [])
    candidates = list(dict.fromkeys([*requested, *current_tools]))
    targets, notes = _authorize_targets(
        manager, auth_item, package, candidates, flags, tool_flags=tool_flags
    )
    if tool_flags is not None:
        # Every authorized plan was checked with ITS tool's flags; the union
        # over those tools only is what the install re-checks.
        union = dict(flags)
        for tool in targets:
            for name, value in tool_flags.get(tool, {}).items():
                union[name] = bool(union.get(name)) or bool(value)
        flags = union
    if current is not None:
        # A version update moves EVERY installed tool to the new version: it
        # happens only if the new version is authorized for each of them
        # (server per-tool approval + local consent, and - for a widening
        # applied through bound digests - an origin holding that digest).
        # Otherwise nothing is installed and the reason is reported - a tool
        # never gets a version it was not approved for, and pull never
        # removes one.
        unapproved = [tool for tool in current_tools if tool not in targets]
        if unapproved:
            if restrict is not None:
                notes.append(
                    "this version asks for more access; only origins that approved its "
                    "access digest may authorize tools (or pass --approve-widening)"
                )
            old = str(current.get("version") or current.get("content_hash") or "the installed version")
            tools = ", ".join(unapproved)
            item.status = "skipped"
            item.local_package_id = str(current.get("package_id"))
            previous = previous_requested_for(str(current.get("package_id")))
            # A tool some origin still assigns (only the access-digest
            # restriction or a missing consent holds it back) is NOT
            # unassigned: never steer the user to drop it.
            still_assigned = set(item.targets)
            item.unassigned_tools = sorted(
                tool for tool in unapproved if tool in previous and tool not in still_assigned
            )
            if item.unassigned_tools:
                # Remember them even though the update did not happen: the
                # records' own targets may be rewritten before it can.
                _note_unassigned_targets(
                    manager, hosted_state, item.scope, str(current.get("package_id")),
                    namespace, item.unassigned_tools, set(current_tools),
                )
            droppable = [tool for tool in unapproved if tool not in still_assigned]
            remedy = f"request approval for {tools}"
            if droppable:
                remedy += (
                    f" or remove {', '.join(droppable)} with "
                    f"`sfs skill compile {current.get('package_id')} --scope {item.scope} "
                    + " ".join(f"--drop {tool}" for tool in droppable) + "`"
                )
            item.detail = (
                f"{metadata.get('version') or item.version_id} is approved only for "
                f"{', '.join(targets) or 'no tool'}; {tools} still "
                f"{'has' if len(unapproved) == 1 else 'have'} {old} - {remedy}"
                + (f" ({'; '.join(notes)})" if notes else "")
            )
            return
    if not targets:
        item.status = "skipped"
        item.detail = "; ".join(notes) or "no authorized tool"
        return
    if on_authorized is not None:
        # Durable BEFORE the row changes (its recovery restores only these).
        on_authorized(
            _authorized_by_origin(manager, auth_item, package, targets, flags, tool_flags=tool_flags)
        )
    plans = manager.plan(package, targets=targets, scope=item.scope)
    existing_install_ids = {
        value for value in _install_ids(manager, item.scope).values() if value
    }
    installed_row = manager.install_prepared(
        package,
        plans,
        **_consent_kwargs(flags),
        expected_current_hash=str(current.get("content_hash")) if current else None,
        replace_package_id=str(current["package_id"]) if current else None,
        consent_context=consent_context,
    )
    if current is None and isinstance(installed_row, dict):
        install_id = installed_row.get("install_id")
        # Created by THIS install only if no installation already had it.
        if isinstance(install_id, str) and install_id not in existing_install_ids:
            item.created_install_id = install_id
    item.status = "installed"
    if notes:
        item.detail = "; ".join(notes)


def pull(
    manager: "SkillManager",
    client: HostedClient,
    *,
    notify: Callable[[str], None] | None = None,
    on_recovery: Callable[[str], None] | None = None,
    **kwargs: Any,
) -> PullReport:
    """See `_pull`. If the run fails part-way, the exception carries the
    partial report as `pull_report`, so what local recovery found (pending
    interrupted installs) is still shown or logged by the caller."""
    report = PullReport(notify=notify, on_recovery=on_recovery)
    try:
        _pull(manager, client, report=report, **kwargs)
        if not _halted(kwargs.get("deadline"), kwargs.get("cancel")):
            _send_policy_report(manager, client, report, kwargs.get("device_id"))
        return report
    except Exception as exc:
        with contextlib.suppress(Exception):
            if getattr(exc, "pull_report", None) is None:
                exc.pull_report = report  # type: ignore[attr-defined]
        raise


def _send_policy_report(
    manager: "SkillManager", client: HostedClient, report: PullReport, device_id: str | None
) -> None:
    """Advisory compliance report (marketplace v2 §8.4). Best effort: a
    failure is a note, never a failed pull. Names, versions and policy
    codes only - never file contents."""
    from sessionfs.skills import policy_reconcile

    try:
        sent = policy_reconcile.report_after_pull(manager, client, report, device_id)
    except Exception as exc:  # noqa: BLE001 - telemetry never fails a pull
        report.note(f"Skills policy compliance report not sent: {exc}")
        return
    report.policy_reports = sent


def report_pull_usage(
    manager: "SkillManager", client: HostedClient, report: "PullReport", *, interactive: bool = False
) -> int:
    """Best-effort install counts for a reconcile (marketplace v2 §9):
    installed / updated items count as installs, up-to-date ones as an
    active-install ping, both with source "reconcile". Never raises. The
    daemon passes interactive=False: it sends nothing until the notice has
    been shown by an interactive command (or counts are enabled explicitly)."""
    sent = 0
    if not callable(getattr(client, "report_usage", None)):
        return 0
    events = [
        (item, {"installed": "install", "updated": "install", "up-to-date": "active"}.get(item.status))
        for item in getattr(report, "items", [])
    ]
    events = [(item, event) for item, event in events if event is not None]
    # The notice (if due) is shown right before the first count, never when
    # there is nothing to count.
    if not events or not usage_reporting_enabled(interactive=interactive):
        return 0
    for item, event in events:
        tools = list(item.targets)
        if not tools:
            try:
                row = manager.find_installed(item.local_package_id or item.package_id, scope=item.scope)
                tools = [str(tool) for tool in row.get("targets") or []]
            except Exception:  # noqa: BLE001 - counts are optional
                tools = []
        if client.report_usage(item.version_id, event, tools, source="reconcile"):
            sent += 1
    return sent


def partial_pull_report(exc: BaseException | None) -> "PullReport | None":
    """The partial report a failed `pull` attached to `exc` (or a cause)."""
    seen = 0
    while exc is not None and seen < 10:
        report = getattr(exc, "pull_report", None)
        if isinstance(report, PullReport):
            return report
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


def _pull(
    manager: "SkillManager",
    client: HostedClient,
    *,
    report: PullReport,
    device_id: str | None = None,
    targets: list[str] | None = None,
    flags: dict[str, bool] | None = None,
    install_optional: Callable[[PullItem], bool | str] | None = None,
    include_requests: bool = True,
    scopes: tuple[str, ...] = ("project", "user"),
    deadline: float | None = None,
    project_id: str | None = None,
    cancel: Callable[[], bool] | None = None,
) -> PullReport:
    """Reconcile org assignments + approved install requests to this device.

    * revocations are synced first; blocked content is quarantined (with
      the previously known flags when the feed sync is incomplete);
    * required items install for the tools whose elevated capabilities are
      consented to via `flags` and authorized by an origin covering that
      tool; for content already installed missing tools are ADDED - pull
      never removes a tool (one no longer assigned is only reported in
      `report.unassigned_tools`); a version update happens only when the
      new version is authorized for every installed tool;
    * optional items install only when `install_optional(item)` says so;
    * EVERY live origin whose version is installed is recorded:
      - as an INSTALLER record when this namespace installed / manages the
        package (it may update it and reconcile its tools), else
      - as a plain REFERENCE (e.g. a user's own install, or a package
        another account manages);
    * a managed package whose origins are gone from a COMPLETE listing is
      only REPORTED as no longer assigned (`report.unassigned`, also listed
      by doctor) - pull never uninstalls a package because its assignment or
      approval disappeared.

    Idempotent. With a `deadline` (monotonic seconds) or `cancel()` work
    stops cleanly between steps; unfinished items are reported as `deferred`.
    An item whose installation is still awaiting settlement from an earlier
    interrupted pull is likewise deferred - never re-stamped - until its
    recovery completes.
    """
    flags = dict(flags or {})

    def halted() -> bool:
        return _halted(deadline, cancel)

    # Before ANY network work or early return: finish what an interrupted
    # pull left behind, for the identity this device already knows (an
    # offline or timed-out run must never leave an unrecorded install).
    # The consent given to THIS run may settle one that needed it. An
    # installation whose recovery is still pending is never changed by this
    # run (its stamp is the pending operation's recovery evidence).
    pending_recovery = _recover_locally(
        manager, client, scopes, consent=_consent_kwargs(flags), report=report
    )

    fingerprint, origin = current_identity(client=client)
    origin = origin or canonical_origin(client.api_url)
    summary = sync_revocations(client, manager.store_root, deadline=deadline, cancel=cancel)
    report.revocations_supported = bool(summary.get("supported"))
    report.revocations_complete = bool(summary.get("complete", True))
    if not report.revocations_supported:
        report.note("Revocation feed not available on this server (skipped)")
    elif not report.revocations_complete:
        report.note(
            "Revocation feed sync did not finish; previously known blocks and yanks are "
            "still enforced"
        )
    if not refresh_package_owners(manager, client, deadline=deadline, cancel=cancel):
        report.note("Stopped before learning every package owner; continuing next time")
    if halted():
        report.note("Stopped at the time limit before enforcing revocations")
        return report
    report.blocked_removed = enforce_blocked(
        manager, scopes=scopes, fingerprint=fingerprint, origin=origin,
        deadline=deadline, cancel=cancel,
    )
    if halted():
        report.note("Stopped at the time limit before applying assignments")
        return report
    namespace, _identity = namespace_key(client)
    # Finish (or roll back) installs an interrupted pull left unrecorded.
    pending_recovery |= _recover_pull_ops(
        manager, HostedState(manager.store_root), namespace, scopes,
        report=report, consent=_consent_kwargs(flags),
    )
    # Notes from listing (incomplete sources) are emitted here, BEFORE any
    # install/removal prompt below.
    items = [
        item for item in collect_pull_items(
            manager,
            client,
            device_id=device_id,
            include_requests=include_requests and "project" in scopes,
            project_id=project_id,
            report=report,
            deadline=deadline,
            cancel=cancel,
        )
        if item.scope in scopes
    ]
    default_targets = list(targets or SUPPORTED_TOOLS)
    for item in items:
        _expand_targets(item, default_targets)
    _resolve_package_ids(client, items)
    _mark_version_conflicts(manager, items, origin, report)
    report.items = items
    live_keys = {
        _pull_key(manager, namespace, item.scope, kind, origin_id)
        for item in items
        for kind, origin_id in item.origins
    }
    hosted_state = HostedState(manager.store_root)
    timed_out = False
    for item in items:
        if item.conflict:
            continue  # reported by _mark_version_conflicts; nothing changes
        if halted():
            item.status = "deferred"
            timed_out = True
            continue
        for attempt in range(MAX_RECORD_ATTEMPTS):
            item.status = "pending"
            item.created_install_id = None
            try:
                # Optional origins are decided one by one, WITHOUT any lock
                # held: a required origin never pulls an optional origin's
                # tools in with it, and being installed is never consent.
                snapshot = hosted_state.read()
                active = _required_origins(item)
                # Optional origins accepted NOW: origin key -> (via, was it
                # opted out when the user said yes, the generation of that
                # opt-out tombstone then - None when there was none).
                fresh: dict[str, tuple[str, bool, Any]] = {}
                offered: list[PullItem] = []
                for kind, origin_id in item.origins:
                    if (kind, origin_id) in active:
                        continue
                    view = _origin_view(item, [(kind, origin_id)])
                    if _optional_accepted(
                        manager, snapshot["pulled"],
                        [_pull_key(manager, namespace, item.scope, kind, origin_id)], view,
                        namespace=namespace, opt_outs=snapshot["opt_outs"],
                    ):
                        active.append((kind, origin_id))  # accepted earlier
                        continue
                    if active and _covered(_origin_view(item, active), view):
                        continue  # nothing beyond what the active origins give
                    decision = install_optional(view) if install_optional is not None else False
                    if decision:
                        # An explicit removal opted out of this optional
                        # version: not offered by --yes, never reinstalled by
                        # the daemon, until the user opts in interactively.
                        active.append((kind, origin_id))
                        via = decision if decision in {"prompt", "yes"} else "prompt"
                        tombstone = snapshot["opt_outs"].get(
                            _opt_out_key(manager, namespace, item.scope, kind, item.version_id)
                        )
                        fresh[f"{kind}|{origin_id}"] = (
                            str(via),
                            view.opted_out,
                            tombstone.get("generation") if isinstance(tombstone, dict) else None,
                        )
                    else:
                        offered.append(view)
                item.opted_out = any(view.opted_out for view in offered)
                if not active:
                    rows = _installed_rows(manager, item.scope)
                    installed = any(_matches_item(row, item, origin) for row in rows)
                    item.status = (
                        "up-to-date" if installed  # installed some other way: not consent
                        else "opted-out" if item.opted_out else "offered"
                    )
            except SkillError as exc:
                item.status = "failed"
                item.detail = str(exc)
                break
            if not active:
                _note_offered(item, offered)
                break
            # Install AND record origins in ONE scope -> operation lock
            # critical section: no explicit add/remove can land between them
            # (it would keep or replace the install id pull then records).
            recorded = False
            work = _origin_view(item, active)
            with _scope_lock(manager, item.scope), manager._operation_lock(item.scope):
                # Re-validate every optional origin UNDER the locks: an
                # acceptance that no longer holds, or an opt-out written
                # since the user was asked, drops that origin's tools.
                locked = hosted_state.read()
                dropped: list[str] = []
                for kind, origin_id in list(active):
                    key = f"{kind}|{origin_id}"
                    if item.origin_modes.get(key, item.mode) == "required":
                        continue
                    if key in fresh:
                        _via, was_opted_out, seen = fresh[key]
                        current = locked["opt_outs"].get(
                            _opt_out_key(manager, namespace, item.scope, kind, item.version_id)
                        )
                        # No removal intent since the prompt: none at all, or
                        # (re-acceptance) exactly the tombstone the user saw.
                        still_ok = current is None or (
                            was_opted_out
                            and isinstance(current, dict)
                            and current.get("generation") == seen
                        )
                    else:
                        still_ok = _optional_accepted(
                            manager, locked["pulled"],
                            [_pull_key(manager, namespace, item.scope, kind, origin_id)],
                            _origin_view(item, [(kind, origin_id)]),
                            namespace=namespace, opt_outs=locked["opt_outs"],
                        )
                    if not still_ok:
                        active.remove((kind, origin_id))
                        fresh.pop(key, None)
                        dropped.append("+".join(item.origin_targets.get(key, [])) or "all tools")
                work = _origin_view(item, active)
                work_keys = [
                    _pull_key(manager, namespace, item.scope, kind, origin_id)
                    for kind, origin_id in active
                ]
                op_token: str | None = None
                try:
                    if active and work.status == "pending":
                        pulled = locked["pulled"]
                        # Legacy rows get their install id BEFORE the operation
                        # starts (never during its recovery).
                        # (Strict: a malformed row or an INVALID id is an
                        # error, never "legacy, give it a new id".)
                        for legacy in _installed_rows(manager, item.scope, strict=True):
                            if legacy.get("install_id") is None and not _migrate_install_id(
                                manager, hosted_state, item.scope, str(legacy.get("package_id"))
                            ):
                                raise RecordingDeferred(
                                    f"could not give {legacy.get('package_id')} an install id; "
                                    "retried next pull"
                                )
                        # The migration loop stamped the legacy installer
                        # records in hosted.json, but `locked`/`pulled` is the
                        # pre-migration snapshot: re-read the migrated state so
                        # a just-migrated package counts as managed on THIS
                        # pull (its tool-add or version update is not delayed a
                        # cycle).
                        installer_ids = _managed_ids(
                            manager, hosted_state.read()["pulled"], item.scope, namespace
                        )
                        op_token = _begin_pull_op(
                            manager,
                            hosted_state, namespace=namespace, server=origin, work=work,
                            fresh_keys=list(fresh),
                            # One authoritative read under the operation
                            # lock: an unreadable or malformed state aborts
                            # BEFORE an intent is written.
                            rows=_before_image_rows(manager, item.scope),
                            consent=_consent_kwargs(flags),
                        )
                        _install_item(
                            manager,
                            client,
                            work,
                            origin=origin,
                            default_targets=default_targets,
                            flags=flags,
                            managed_ids=installer_ids,
                            previous_requested_for=lambda local_id: _previous_requested(
                                manager, pulled, item.scope, local_id, namespace,
                                report.complete_sources,
                            ),
                            fingerprint=fingerprint,
                            hosted_state=hosted_state,
                            namespace=namespace,
                            pending_recovery=pending_recovery,
                            op_token=op_token,
                            on_authorized=functools.partial(
                                _set_op_authorized, hosted_state, op_token
                            ),
                        )
                    elif not active:
                        work.status = "offered"
                except RecordingDeferred as exc:
                    work.status = "deferred"
                    work.detail = str(exc)
                except SkillError as exc:
                    work.status = "failed"
                    work.detail = str(exc)
                if active and work.status not in {"failed", "deferred"} and not (
                    work.status not in {"installed", "updated", "up-to-date"}
                    and _local_id_for(manager, work, origin) is None
                ):
                    # Record the ACTIVE origins whose version is installed
                    # (an optional origin that was not accepted is never
                    # recorded); a freshly accepted optional origin carries
                    # its acceptance. Installer authority comes only from an
                    # installation THIS operation created (its exact install
                    # id) or from authority this namespace already held.
                    try:
                        recorded = _record_origins(
                            manager, hosted_state, work, work_keys, live_keys, namespace,
                            report.complete_sources, role="managed", origin=origin,
                            created_install_id=work.created_install_id,
                            accepted={key: via for key, (via, _old, _gen) in fresh.items()},
                            finish_op=op_token,
                            # Re-accepted origins: their tombstone is lifted in
                            # the same locked write - only the generation the
                            # user saw.
                            clear_opt_outs={
                                _opt_out_key(
                                    manager, namespace, item.scope, key.split("|", 1)[0],
                                    item.version_id,
                                ): generation
                                for key, (_via, was_opted_out, generation) in fresh.items()
                                if was_opted_out
                            },
                        )
                    except RecordingDeferred as exc:
                        work.status = "deferred"
                        work.detail = str(exc)
                if work.status != "deferred" and not recorded:
                    # Nothing was installed or recorded: the intent is done
                    # (a recording deleted it in the same write).
                    _finish_pull_op(hosted_state, op_token)
            for name in (
                "status", "detail", "name", "package_id", "created_install_id",
                "unassigned_tools", "local_package_id", "update_class",
            ):
                setattr(item, name, getattr(work, name))
            _note_offered(item, offered)
            if dropped:
                item.detail = "; ".join(filter(None, [
                    item.detail,
                    f"not installed: {', '.join(dropped)} (removed or opted out meanwhile)",
                ]))
            if not active or item.status == "failed" or not recorded and item.status not in {
                "installed", "updated", "up-to-date"
            }:
                break
            if recorded:
                break
            if attempt + 1 < MAX_RECORD_ATTEMPTS and not halted():
                continue  # removed concurrently: install it again
            item.status = "deferred"
            item.detail = "removed concurrently while it was being recorded; retried next pull"
            report.note(f"{item.name or item.version_id} was removed concurrently; deferred")
            break
    report.unassigned_tools = [
        {
            "scope": item.scope,
            "package_id": item.local_package_id,
            "name": item.name,
            "tools": list(item.unassigned_tools),
        }
        for item in items
        if item.unassigned_tools and item.local_package_id
    ]
    if timed_out:
        report.note("Stopped at the time limit; remaining items were deferred")
        return report
    _report_unassigned(manager, report, scopes, live_keys, namespace)
    return report


def _resolve_package_ids(client: HostedClient, items: list[PullItem]) -> None:
    """Fill a missing package id (an older server omits it from grants) from
    the version's metadata, BEFORE conflict grouping. Left empty when the
    lookup fails; _mark_version_conflicts fails closed on those."""
    for item in items:
        if item.package_id:
            continue
        try:
            metadata = client.version_metadata(item.version_id)
        except SkillError:
            continue
        package_id = metadata.get("package_id") if isinstance(metadata, dict) else None
        if (
            isinstance(metadata, dict)
            and metadata.get("version_id") == item.version_id
            and isinstance(package_id, str)
            and package_id
        ):
            item.package_id = package_id


def _mark_version_conflicts(
    manager: "SkillManager", items: list[PullItem], origin: str, report: PullReport
) -> None:
    """Several granted versions of ONE package in one scope (an assignment
    and a request disagree, or a server that predates the one-active-approval
    invariant): never install or replace any of them, so pull can never
    downgrade or flip-flop between versions. The installed version stays."""
    groups: dict[tuple[str, str], list[PullItem]] = {}
    unresolved: dict[str, list[PullItem]] = {}
    for item in items:
        if item.package_id:
            groups.setdefault((item.scope, item.package_id), []).append(item)
        else:
            unresolved.setdefault(item.scope, []).append(item)
    for scope, members in unresolved.items():
        scoped = [item for item in items if item.scope == scope]
        if len(scoped) < 2:
            continue  # a single grant in this scope cannot conflict
        # Some grant's package is unknown while other grants exist in this
        # scope: ANY of them might be the same package, so quarantine every
        # grant in the scope (installed versions stay) until all resolve.
        installed_commits = {
            str((row.get("source") or {}).get("commit")) for row in _installed_rows(manager, scope)
        }
        for member in scoped:
            member.conflict = True
            if member.version_id in installed_commits:
                member.status = "up-to-date"
                member.detail = "kept: other grants in this scope could not be identified"
            else:
                member.status = "skipped"
                member.detail = (
                    "not installed: a granted version's package could not be determined; "
                    "retry when the server is reachable"
                )
        report.note(
            f"Could not resolve the package of {len(members)} granted version(s) ({scope} scope); "
            "nothing in that scope was installed or replaced"
        )
    for (scope, package_id), members in groups.items():
        if len({member.version_id for member in members}) < 2 or any(member.conflict for member in members):
            continue
        installed = next(
            (
                str((row.get("source") or {}).get("commit"))
                for row in _installed_rows(manager, scope)
                if hosted_package_id(row.get("source")) == package_id
            ),
            None,
        )
        versions = ", ".join(sorted(member.version_id for member in members))
        for member in members:
            member.conflict = True
            if member.version_id == installed:
                member.status = "up-to-date"
                member.detail = f"other versions are also granted ({versions}); keeping this one"
            else:
                member.status = "skipped"
                member.detail = (
                    f"several versions of this package are granted ({versions}); not changing "
                    "the installed version - ask an admin to keep one"
                )
        report.note(
            f"Conflicting grants for package {package_id} ({scope} scope): {versions}; nothing changed"
        )


def _pull_key(
    manager: "SkillManager", namespace: str, scope: str, origin: str, origin_id: str
) -> str:
    root = str(manager.project_root) if scope == "project" else "~"
    return f"{namespace}|{scope}|{root}|{origin}|{origin_id}"


_SCOPE_LOCKS = threading.local()


@contextmanager
def _scope_lock(manager: "SkillManager", scope: str) -> Iterator[None]:
    """Interprocess lock serializing origin bookkeeping and the
    re-check-then-remove step of one install scope (project root / user).
    Always taken OUTSIDE the skill operation lock (lock order: scope lock ->
    operation lock / hosted state lock)."""
    root = str(manager.project_root) if scope == "project" else "~"
    digest = hashlib.sha256(f"{scope}\0{root}".encode("utf-8")).hexdigest()[:16]
    lock_dir = Path(manager.store_root)
    path = str(lock_dir / f"pull-{digest}.lock")
    held: dict[str, int] = getattr(_SCOPE_LOCKS, "held", None) or {}
    _SCOPE_LOCKS.held = held
    if held.get(path):
        held[path] += 1  # re-entrant within this thread: nothing is acquired
        try:
            yield
        finally:
            held[path] -= 1
        return
    with ordered_lock("scope"):
        lock_dir.mkdir(parents=True, exist_ok=True)
        with interprocess_lock(Path(path)):
            held[path] = 1
            try:
                yield
            finally:
                held[path] = 0


def _record_in_scope(
    manager: "SkillManager", record: Any, scope: str, local_id: str | None = None
) -> bool:
    return (
        isinstance(record, dict)
        and record.get("scope") == scope
        and record.get("project_root") == _scope_root(manager, scope)
        and (local_id is None or record.get("local_package_id") == local_id)
    )


def _is_installer(record: dict[str, Any]) -> bool:
    # Legacy records (older builds) were written only for packages pull installed.
    return record.get("role", "installer") == "installer"


def _previous_requested(
    manager: "SkillManager",
    pulled: dict[str, Any],
    scope: str,
    local_id: str | None,
    namespace: str,
    complete_sources: dict[str, bool] | None = None,
) -> set[str]:
    """Tools earlier origins of THIS namespace asked for this package (and
    tools already reported as no longer assigned), from sources whose
    listing completed - only used to REPORT tools that are no longer
    assigned; pull never removes a tool."""
    if local_id is None:
        return set()
    tools: set[str] = set()
    for record in pulled.values():
        if (
            _record_in_scope(manager, record, scope, local_id)
            and record.get("namespace") == namespace
            and (complete_sources or {}).get(str(record.get("origin"))) is True
        ):
            for name in ("targets", "unassigned_targets"):
                tools |= {str(tool) for tool in record.get(name) or []}
    return tools


class RecoveryPending(SkillError):
    """Settling an interrupted pull needs consent nobody durably gave."""

    def __init__(self, requirements: list[str]) -> None:
        self.requirements = list(requirements)
        self.flags = [
            _FLAG_FOR_REQUIREMENT[requirement][1]
            for requirement in requirements if requirement in _FLAG_FOR_REQUIREMENT
        ]
        super().__init__(f"consent for {', '.join(requirements)} is missing")


def _report_pending_recovery(
    report: "PullReport", token: str, op: dict[str, Any], exc: SkillError
) -> None:
    """One entry per pending operation (a later pass replaces it)."""
    what = str(op.get("package_id") or op.get("version_id") or "a package")
    flags = list(getattr(exc, "flags", []) or [])
    action = (
        f"run `sfs skill pull {' '.join(flags)}` to settle it" if flags
        else "it is retried by the next pull"
    )
    entry = {
        "token": token,
        "scope": op.get("scope"),
        "version_id": op.get("version_id"),
        "package_id": op.get("package_id"),
        "reason": str(exc),
        "flags": flags,
        "message": (
            f"An interrupted install of {what} ({op.get('version_id')}) could not be "
            f"settled because {exc}; {action}"
        ),
    }
    previous = next((e for e in report.recovery if e.get("token") == token), None)
    report.recovery[:] = [e for e in report.recovery if e.get("token") != token] + [entry]
    announce = report.on_recovery or report.notify
    if announce is not None and (
        previous is None or previous.get("message") != entry["message"]
    ):
        # Shown at once: a later failure (network, server) must not hide it.
        announce(str(entry["message"]))


class RecordingDeferred(SkillError):
    """Origins could not be recorded safely now (nothing was written); the
    item is retried by the next pull."""


def _recover_locally(
    manager: "SkillManager",
    client: HostedClient,
    scopes: tuple[str, ...],
    *,
    consent: dict[str, bool] | None = None,
    report: "PullReport | None" = None,
) -> set[str]:
    """The install ids of installations whose recovery is still PENDING (see
    `_recover_pull_ops`); empty when the identity is not known here yet."""
    hosted_state = HostedState(manager.store_root)
    _complete_install_id_migrations(manager, hosted_state, scopes)
    namespace = hosted_state.read()["identities"].get(
        identity_fingerprint(client.api_url, client.api_key)
    )
    if isinstance(namespace, str):
        return _recover_pull_ops(
            manager, hosted_state, namespace, scopes, report=report, consent=consent
        )
    return set()


def _migrate_install_id(
    manager: "SkillManager", hosted_state: HostedState, scope: str, local_id: str
) -> bool:
    """Give an older row an install id, crash-safe across both files: the
    CANDIDATE id is first written to hosted.json (a migration entry plus
    this package's legacy installer records bound to it), THEN to the skill
    state; the entry is removed last. `_complete_install_id_migrations`
    finishes an interrupted migration, so a live origin never stays
    downgraded. Caller holds the scope and operation locks."""
    key = f"{scope}|{_scope_root(manager, scope)}|{local_id}"
    existing = hosted_state.read()["install_id_migrations"].get(key)
    if isinstance(existing, dict) and str(existing.get("candidate") or ""):
        # An interrupted migration already owns this row, and its candidate
        # is already stamped on this package's legacy installer records.
        # Finish THAT entry rather than replacing its candidate: a new one
        # would strand the records on the old id and downgrade them to
        # references once the row gets the new id. While the existing entry
        # stays unresolved it is left as-is and this returns False.
        return _finish_install_id_migration(manager, hosted_state, key)
    candidate = "sfi_" + uuid.uuid4().hex
    with hosted_state.edit() as state:
        state["install_id_migrations"][key] = {
            "scope": scope, "local_package_id": local_id, "candidate": candidate,
            "project_root": _scope_root(manager, scope),
        }
        for record in state["pulled"].values():
            if (
                _record_in_scope(manager, record, scope, local_id)
                and _is_installer(record)
                and not record.get("install_id")
            ):
                record["install_id"] = candidate
    return _finish_install_id_migration(manager, hosted_state, key)


def _finish_install_id_migration(
    manager: "SkillManager", hosted_state: HostedState, key: str
) -> bool:
    entry = hosted_state.read()["install_id_migrations"].get(key)
    if not isinstance(entry, dict):
        return False
    scope, local_id = str(entry.get("scope")), str(entry.get("local_package_id"))
    candidate = str(entry.get("candidate") or "")
    actual: str | None = None
    if scope in ("project", "user") and candidate:
        if _migration_root(key, entry) != str(_scope_root(manager, scope)):
            return False  # another project's: never settled from here
        try:
            # None ONLY when an authoritative read under the state lock
            # found no such row; a read/write error raises (kept).
            actual = manager.state_store(scope).ensure_install_id(local_id, candidate)
        except SkillError:
            return False  # kept: retried by the next pull
    # Compare-and-set outcome: the candidate is installed (done), the row is
    # provably gone, or a DIFFERENT installation is there now (e.g. an
    # explicit add rotated it) - the candidate's records then simply have no
    # authority; it is never transferred to another id. (An unusable entry
    # is simply dropped.)
    with hosted_state.edit() as state:
        state["install_id_migrations"].pop(key, None)
    return actual == candidate


def _migration_root(key: str, entry: dict[str, Any]) -> str:
    """The project root a migration entry belongs to (as `str(root)`, "None"
    for user scope): stored on newer entries, else taken from the key
    `<scope>|<root>|<local id>`."""
    if "project_root" in entry:
        return str(entry.get("project_root"))
    prefix = f"{entry.get('scope')}|"
    suffix = f"|{entry.get('local_package_id')}"
    if key.startswith(prefix) and key.endswith(suffix) and len(key) > len(prefix) + len(suffix):
        return key[len(prefix):-len(suffix)]
    return ""


def _complete_install_id_migrations(
    manager: "SkillManager", hosted_state: HostedState, scopes: tuple[str, ...]
) -> None:
    for key, entry in list(hosted_state.read()["install_id_migrations"].items()):
        scope = entry.get("scope") if isinstance(entry, dict) else None
        if scope not in scopes:
            continue
        if _migration_root(key, entry) != str(_scope_root(manager, str(scope))):
            continue  # another project's migration: left for a pull there
        with _scope_lock(manager, scope), manager._operation_lock(scope):
            with contextlib.suppress(SkillError):
                _finish_install_id_migration(manager, hosted_state, key)


def _consented_requirements(row: dict[str, Any]) -> list[str]:
    """Capabilities durably consented to for an installed row (its consent
    receipts) - the authority a recovery restore may use."""
    return sorted({
        str(receipt.get("capability"))
        for receipt in row.get("consent_receipts") or []
        if isinstance(receipt, dict) and receipt.get("capability") in _FLAG_FOR_REQUIREMENT
    })


_CONSENT_SNAPSHOT_KEYS = (
    "capability_manifest",
    "capability_digest",
    "consent_rule_version",
    "last_update_class",
    "digest_authorized_origins",
)


def _consent_snapshot_of(row: dict[str, Any]) -> dict[str, Any]:
    """The disclosure baseline an installed row was consented under."""
    return {key: row[key] for key in _CONSENT_SNAPSHOT_KEYS if key in row}


def _restore_consent_context(identity: dict[str, Any]) -> dict[str, Any]:
    """Consent context that restores a before-image's baseline exactly: its
    recorded capability manifest / digest (when it had one) and its digest
    restriction (lifted only when it had none)."""
    snapshot = identity.get("consent_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    context: dict[str, Any] = {}
    if isinstance(snapshot.get("capability_manifest"), dict):
        context["capability_manifest"] = snapshot["capability_manifest"]
        context["capability_digest"] = snapshot.get("capability_digest")
        context["consent_rule_version"] = snapshot.get("consent_rule_version")
        if snapshot.get("last_update_class"):
            context["update_class"] = snapshot["last_update_class"]
    restriction = snapshot.get("digest_authorized_origins")
    context["digest_authorized_origins"] = (
        [str(key) for key in restriction] if isinstance(restriction, list) else None
    )
    return context


def _before_image_rows(manager: "SkillManager", scope: str) -> list[dict[str, Any]]:
    """Strictly read rows for an operation's before-image; every row must
    carry its install id (legacy rows were migrated just before)."""
    rows = _installed_rows(manager, scope, strict=True)
    for row in rows:
        if not (isinstance(row.get("install_id"), str) and row["install_id"]):
            raise SkillError(f"Installed row {row.get('package_id')} has no install id yet")
    return rows


def _op_targets_row(row: dict[str, Any], work: PullItem, server: str) -> bool:
    """True when `work`'s pull operation could change `row`: the row IS this
    version from this server, or (once metadata named the package) another
    version of the same hosted package from this server (a version update is
    all-or-nothing across the row)."""
    if _matches_item(row, work, server):
        return True
    if not work.package_id:
        return False
    package_id, origin = _split_locator(
        row.get("source") if isinstance(row.get("source"), dict) else {}
    )
    return package_id == work.package_id and origin == server


def _begin_pull_op(
    manager: "SkillManager",
    hosted_state: HostedState,
    *,
    namespace: str,
    server: str,
    work: PullItem,
    fresh_keys: list[str],
    rows: list[dict[str, Any]],
    consent: dict[str, bool],
) -> str:
    """Durable intent written BEFORE a pull installs. A NEW installation
    gets the intent's token as its install id; every row's install id,
    version, tools and consented capabilities from before are stored too.
    That is the only evidence `_recover_pull_ops` acts on.

    Refused (RecordingDeferred, nothing written) when a row this operation
    could change is still stamped by another LIVE intent: changing it would
    re-stamp the row and destroy that operation's recovery evidence. The
    next pull's recovery settles the older intent first."""
    token = "sfi_pop" + uuid.uuid4().hex
    with hosted_state.edit() as state:
        live = set(state["pull_ops"])
        blocked = sorted(
            {
                str(row.get("package_id"))
                for row in rows
                if isinstance(row.get("pull_op"), str)
                and row["pull_op"] in live
                and _op_targets_row(row, work, server)
            }
        )
        if blocked:
            raise RecordingDeferred(
                f"an interrupted pull operation still owns {', '.join(blocked)}; "
                "its recovery settles it first (retried next pull)"
            )
        state["pull_ops"][token] = {
            "namespace": namespace,
            "server": server,
            "scope": work.scope,
            # Only ever settled by a pull in this very project.
            "project_root": _scope_root(manager, work.scope),
            "mode": work.mode,
            "version_id": work.version_id,
            "content_hash": work.content_hash,
            "package_id": work.package_id,
            "origins": [list(origin) for origin in work.origins],
            "origin_targets": dict(work.origin_targets),
            "origin_modes": dict(work.origin_modes),
            "origin_caps": {
                key: sorted(caps) if caps is not None else None
                for key, caps in work.origin_caps.items()
            },
            "fresh_keys": list(fresh_keys),
            # The consent this pull was given (its flags).
            "consent": {name: bool(value) for name, value in consent.items()},
            "before": {
                str(row.get("package_id")): {
                    "install_id": row.get("install_id"),
                    "content_hash": row.get("content_hash"),
                    # Version identity and row provenance: identical bytes
                    # of another version are NOT the same installation.
                    "source": dict(row["source"]) if isinstance(row.get("source"), dict) else None,
                    "provenance": (
                        dict(row["provenance"]) if isinstance(row.get("provenance"), dict)
                        else None
                    ),
                    "targets": list(row.get("targets") or []),
                    "consented": _consented_requirements(row),
                    # The consented capability baseline (and any digest
                    # restriction): a restore to this state puts it back
                    # exactly, never a broader one.
                    "consent_snapshot": _consent_snapshot_of(row),
                }
                for row in rows
            },
            "created_at": _now(),
        }
    return token


def _set_op_authorized(
    hosted_state: HostedState, token: str | None, per_origin: dict[str, list[str]]
) -> None:
    """Persist, per origin, the tools the operation actually authorized
    (before it changes any row)."""
    if not token:
        return
    with hosted_state.edit() as state:
        op = state["pull_ops"].get(token)
        if isinstance(op, dict):
            op["authorized"] = {key: list(tools) for key, tools in per_origin.items()}


def _finish_pull_op(hosted_state: HostedState, token: str | None) -> None:
    if not token:
        return
    with hosted_state.edit() as state:
        state["pull_ops"].pop(token, None)


def _op_item(op: dict[str, Any], scope: str, origins: list[tuple[str, str]]) -> PullItem:
    """The operation's item limited to `origins`, with each origin's tools
    as the operation AUTHORIZED them (never its raw requested tools)."""
    keys = {f"{kind}|{origin_id}" for kind, origin_id in origins}
    raw = op.get("authorized")
    authorized: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return PullItem(
        mode=str(op.get("mode") or "required"),
        version_id=str(op.get("version_id") or ""),
        content_hash=str(op.get("content_hash") or ""),
        scope=scope,
        targets=[],
        origins=origins,
        origin_targets={
            key: [str(tool) for tool in authorized.get(key) or []] for key in sorted(keys)
        },
        origin_caps={
            key: set(value) if value is not None else None
            for key, value in (op.get("origin_caps") or {}).items() if key in keys
        },
        origin_modes={
            key: str(value) for key, value in (op.get("origin_modes") or {}).items()
            if key in keys
        },
        status="up-to-date",
        package_id=str(op.get("package_id") or ""),
    )


def _op_row(
    manager: "SkillManager", token: str, scope: str
) -> tuple[dict[str, Any] | None, bool]:
    """The installed row this operation changed, and whether it created it.
    Only a row still STAMPED with the operation's token is evidence (every
    pull mutation writes the stamp in the same state commit; any later
    compile, update, rollback or reinstall clears it). An install id alone
    identifies an installation, not which operation last changed it.
    Raises when the state cannot be read (the intent is kept)."""
    for row in _installed_rows(manager, scope, strict=True):
        if row.get("pull_op") == token:
            return row, row.get("install_id") == token
    return None, False


def _recover_pull_ops(
    manager: "SkillManager",
    hosted_state: HostedState,
    namespace: str,
    scopes: tuple[str, ...],
    *,
    report: "PullReport | None" = None,
    consent: dict[str, bool] | None = None,
) -> set[str]:
    """Settle what an interrupted pull left behind, acting ONLY on evidence
    it can prove (see `_op_row`); otherwise the stale intent is discarded
    without touching anything. Under the scope and operation locks, after
    the skill state's own journal recovery:

    * origins whose acceptance was already recorded and bound to the
      installation: nothing to do;
    * the other non-fresh origins are recorded against that exact
      installation, and the intent is deleted in the same write;
    * what existed only for an unrecorded FRESH optional acceptance is
      undone - a new installation of this op is removed through the trash;
      a row changed in place is restored to its pre-op version and tools
      (plus the recorded origins' tools) in ONE install, authorized only by
      its durable pre-op consent and this op's consent. Without that
      consent the intent stays pending and is reported.

    Recovery never removes or downgrades an installation the op did not
    provably make or change.

    Returns the install ids of the installations whose recovery is still
    PENDING: the row was provably the operation's, but settling it failed.
    A pull in this run must never change one of them - its own stamp would
    replace the pending operation's, destroying its recovery evidence."""
    pending: set[str] = set()
    for token, op in list(hosted_state.read()["pull_ops"].items()):
        if not isinstance(op, dict) or op.get("namespace") != namespace:
            continue
        scope = op.get("scope")
        if scope not in scopes:
            continue
        if "project_root" in op:
            if op.get("project_root") != _scope_root(manager, str(scope)):
                continue  # another project's operation: settled only there
        elif scope == "project":
            continue  # no root recorded: never settled from a guess
        server = str(op.get("server") or "")
        row: dict[str, Any] | None = None
        try:
            with _scope_lock(manager, scope), manager._operation_lock(scope):
                # The snapshot above was read before the locks: another
                # recovery pass may have settled this intent or saved its
                # restore decision meanwhile. Only the live copy is acted on.
                live = hosted_state.read()["pull_ops"].get(token)
                if not isinstance(live, dict):
                    if report is not None:
                        report.recovery[:] = [
                            e for e in report.recovery if e.get("token") != token
                        ]
                    continue  # settled elsewhere
                op = live
                manager.state_store(scope).recover()
                row, created = _op_row(manager, token, scope)
                if row is not None and _settle_pull_op(
                    manager, hosted_state, op, token, scope, server, row, created, namespace,
                    consent=consent or {},
                ):
                    continue  # the intent was deleted with the recording
        except SkillError as exc:
            if row is not None:
                install_id = row.get("install_id")
                if isinstance(install_id, str) and install_id:
                    pending.add(install_id)
            if report is not None:
                _report_pending_recovery(report, token, op, exc)
            continue  # kept: retried by the next pull
        if report is not None:
            report.recovery[:] = [e for e in report.recovery if e.get("token") != token]
        _finish_pull_op(hosted_state, token)
    return pending


def _settle_pull_op(
    manager: "SkillManager",
    hosted_state: HostedState,
    op: dict[str, Any],
    token: str,
    scope: str,
    server: str,
    row: dict[str, Any],
    created: bool,
    namespace: str,
    *,
    consent: dict[str, bool] | None = None,
) -> bool:
    """Returns True when the intent was deleted together with a recording.

    Order: the corrective install (if any) FIRST, re-stamped with the token
    so a crash leaves the row still provably this operation's; only after it
    succeeded are the kept origins recorded and the intent deleted, in one
    write. The corrective install's target (version identity and tools) is
    persisted on the intent before it runs; every later pass re-applies
    exactly that target, so recovery is idempotent and never gives a tool a
    version it was not approved for."""
    local_id = str(row.get("package_id"))
    install_id = str(row.get("install_id"))
    fresh = set(op.get("fresh_keys") or [])
    origins = [(str(kind), str(origin_id)) for kind, origin_id in op.get("origins") or []]
    snapshot = hosted_state.read()
    pulled = snapshot["pulled"]
    own_keys = {_pull_key(manager, namespace, scope, kind, oid) for kind, oid in origins}

    def _recorded(kind: str, origin_id: str) -> bool:
        # Recorded BY THIS OPERATION: the record carries its token (an
        # earlier record of the same installation - an older version, other
        # tools, an older acceptance - is no evidence).
        record = pulled.get(_pull_key(manager, namespace, scope, kind, origin_id))
        return (
            isinstance(record, dict)
            and record.get("pull_op") == token
            and record.get("install_id") == install_id
            and (
                f"{kind}|{origin_id}" not in fresh
                or isinstance(record.get("accepted_at"), str)
            )
        )

    if origins and all(_recorded(kind, origin_id) for kind, origin_id in origins):
        return False  # everything was recorded before the crash: just finish
    authorized = op.get("authorized")
    if not isinstance(authorized, dict):
        # No durable record of what the operation authorized: nothing to
        # restore from - the row is left exactly as it is.
        return False
    # A fresh acceptance already recorded for this installation stays; only
    # an UNRECORDED one is undone.
    kept = [
        (kind, origin_id) for kind, origin_id in origins
        if f"{kind}|{origin_id}" not in fresh or _recorded(kind, origin_id)
    ]
    kept_tools = {
        str(tool) for kind, oid in kept for tool in authorized.get(f"{kind}|{oid}") or []
    }
    saved = op.get("restore")
    identity: dict[str, Any] | None = None
    want_hash: Any
    want_tools: set[str]
    consented: set[str]
    if isinstance(saved, dict):
        # An earlier pass already decided (and may have half-applied) the
        # corrective install: re-apply EXACTLY that decision. Recomputing it
        # from the row as that pass left it could give a tool a version it
        # was never approved for.
        want_hash, want_tools, consented, identity = _saved_restore(op, saved)
    else:
        # Another account's record of THIS installation's current version
        # keeps the tools it provably authorizes (fed through the normal
        # restore below).
        foreign_tools = _foreign_authorized_tools(
            manager, pulled, scope, row, namespace, own_keys, snapshot["opt_outs"]
        )
        anchored = bool(kept) or bool(foreign_tools)
        wanted = kept_tools | foreign_tools
        if created and not anchored:
            # A new installation made only for an acceptance that was never
            # recorded: take it out again (trash, not delete).
            manager.remove(local_id, scope=scope, expected_install_id=install_id)
            return False
        prior_key: str | None = None
        prior: dict[str, Any] | None = None
        if not created:
            # Matched by the installation, never only by its current local
            # id: a version that renamed the skill moved the row to a new id.
            prior_key, prior = _prior_image(manager, op, row)
            if prior is None:
                # Changed in place by this op, but its pre-op state cannot be
                # identified: never shrink or downgrade it from a guess.
                raise SkillError(
                    f"the state of {local_id} before the interrupted pull cannot be identified"
                )
        prior_tools = (
            {str(tool) for tool in prior.get("targets") or []} if prior is not None else set()
        )
        if prior is not None and anchored and (
            _same_identity(row, prior) or prior_tools <= wanted
        ):
            # The operation's version stays (the same version with more
            # tools, or an update the kept origins and other accounts'
            # records alone authorize for every tool it had): every prior
            # tool plus theirs - only what existed solely for the unrecorded
            # acceptance goes.
            want_hash = row.get("content_hash")
            want_tools = wanted | prior_tools
            consented = set(prior.get("consented") or [])
        elif prior is not None:
            # Nothing that remains would have allowed the update: back to
            # the prior installation exactly (its version identity,
            # provenance and every tool it had).
            want_hash = prior.get("content_hash")
            want_tools = prior_tools
            consented = set(prior.get("consented") or [])
            identity = prior
        else:
            want_hash = row.get("content_hash")
            want_tools = wanted
            consented = set()
        if not want_tools:
            return False  # nothing authorized to restore to: left as it is
        if _needs_restore(row, want_hash, want_tools, identity):
            # Durable BEFORE the corrective install: a crash during it must
            # not let a later pass re-derive a different target from the
            # half-restored row.
            _save_restore_decision(hosted_state, op, token, {
                "to": "prior" if identity is not None else "current",
                "prior_key": prior_key if identity is not None else None,
                "content_hash": want_hash,
                "targets": sorted(want_tools),
                "consented": sorted(consented),
            })
    if not want_tools:
        return False
    if _needs_restore(row, want_hash, want_tools, identity):
        _restore_installed(
            manager, scope, row, str(want_hash), sorted(want_tools),
            consented=consented, op_consent={**(op.get("consent") or {}), **{
                name: True for name, value in (consent or {}).items() if value is True
            }},
            pull_op=token,
            identity=identity,
        )
    if not kept:
        return False
    item = _op_item(op, scope, kept)
    pull_keys = [_pull_key(manager, namespace, scope, kind, oid) for kind, oid in kept]
    return _record_origins(
        manager, hosted_state, item, pull_keys, set(pull_keys), namespace, {},
        role="managed", origin=server,
        created_install_id=install_id if created else None,
        finish_op=token,
    )


def _needs_restore(
    row: dict[str, Any],
    want_hash: Any,
    want_tools: set[str],
    identity: dict[str, Any] | None,
) -> bool:
    return (
        want_hash != row.get("content_hash")
        or want_tools != {str(tool) for tool in row.get("targets") or []}
        or (identity is not None and not _same_identity(row, identity))
    )


def _prior_image(
    manager: "SkillManager", op: dict[str, Any], row: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    """The before-image of the installation `row` is. Keyed by install id
    (which survives a move to a new local id when a version renames the
    skill); an image without an install id (an older intent) is matched by
    any id the row has been known under. Ambiguity matches nothing."""
    before = op.get("before")
    if not isinstance(before, dict):
        return None, None
    install_id = row.get("install_id")
    if isinstance(install_id, str) and install_id:
        matches = [
            (str(key), prior) for key, prior in before.items()
            if isinstance(prior, dict) and prior.get("install_id") == install_id
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None, None
    for key in manager._known_package_ids(row):
        prior = before.get(key)
        if isinstance(prior, dict) and not prior.get("install_id"):
            return key, prior
    return None, None


def _save_restore_decision(
    hosted_state: HostedState, op: dict[str, Any], token: str, decision: dict[str, Any]
) -> None:
    with hosted_state.edit() as state:
        live = state["pull_ops"].get(token)
        if not isinstance(live, dict):
            raise SkillError("the interrupted pull operation vanished during its recovery")
        existing = live.get("restore")
        if existing is not None and existing != decision:
            # Another pass already decided; the next pass replays that
            # decision instead of replacing it with one computed from a
            # row the first decision already changed.
            raise SkillError("the interrupted pull operation already has a recovery decision")
        live["restore"] = decision
    op["restore"] = decision


def _saved_restore(
    op: dict[str, Any], saved: dict[str, Any]
) -> tuple[Any, set[str], set[str], dict[str, Any] | None]:
    """The corrective install an earlier recovery pass decided on."""
    want_hash = saved.get("content_hash")
    raw_tools = saved.get("targets")
    if not isinstance(want_hash, str) or not want_hash or not isinstance(raw_tools, list):
        raise SkillError("the recorded recovery decision is malformed")
    identity: dict[str, Any] | None = None
    if saved.get("to") == "prior":
        before = op.get("before")
        prior = before.get(saved.get("prior_key")) if isinstance(before, dict) else None
        if not isinstance(prior, dict) or prior.get("content_hash") != want_hash:
            raise SkillError("the recorded recovery decision names an unknown prior state")
        identity = prior
    elif saved.get("to") != "current":
        raise SkillError("the recorded recovery decision is malformed")
    return (
        want_hash,
        {str(tool) for tool in raw_tools},
        {str(item) for item in saved.get("consented") or []},
        identity,
    )


def _same_identity(row: dict[str, Any], prior: dict[str, Any]) -> bool:
    """The row IS the prior installation's version: same bytes, same source
    identity (package, server, version) and same row provenance."""
    return (
        row.get("content_hash") == prior.get("content_hash")
        and (row.get("source") if isinstance(row.get("source"), dict) else None)
        == prior.get("source")
        and (row.get("provenance") if isinstance(row.get("provenance"), dict) else None)
        == prior.get("provenance")
    )


def _foreign_authorized_tools(
    manager: "SkillManager",
    pulled: dict[str, Any],
    scope: str,
    row: dict[str, Any],
    namespace: str,
    own_keys: set[str],
    opt_outs: dict[str, Any],
) -> set[str]:
    """Tools of `row` that ANOTHER account's records provably authorize for
    its current version. A record counts only when it is bound to this exact
    installation, package, version and content AND its origin is still
    assigned - a record marked unassigned is explicitly stale: it authorizes
    nothing and can never hold the intent pending. An optional one counts
    only under the same bound-acceptance rules `_optional_accepted` uses: a
    valid acceptance for that version, content and tools AND no applicable
    opt-out tombstone for its namespace. An assignment authorizes its own
    tools; an install request's approval cannot be proven from the local
    record, so it raises (the intent stays pending)."""
    raw_source = row.get("source")
    version_id = raw_source.get("commit") if isinstance(raw_source, dict) else None
    content_hash = row.get("content_hash")
    row_tools = {str(tool) for tool in row.get("targets") or []}
    tools: set[str] = set()
    for key, record in pulled.items():
        if (
            key in own_keys
            or not _record_in_scope(manager, record, scope)
            or record.get("namespace") == namespace
            or record.get("unassigned_since")
            or record.get("install_id") != row.get("install_id")
            # The row's earlier ids too: a version that renamed the skill
            # moved the installation to a new local id.
            or record.get("local_package_id") not in manager._known_package_ids(row)
            or record.get("version_id") != version_id
            or record.get("content_hash") != content_hash
        ):
            continue  # not a LIVE record of this installation's current version
        requested = [str(tool) for tool in record.get("targets") or []]
        if record.get("mode") == "optional" and not (
            isinstance(record.get("accepted_at"), str)
            and record.get("accepted_via") in {"prompt", "yes"}
            and _acceptance_covers(record, str(version_id), str(content_hash), requested)
            and _opt_out_key(
                manager, str(record.get("namespace")), scope,
                str(record.get("origin")), str(version_id),
            ) not in opt_outs
        ):
            continue  # an optional origin nobody accepted (or since opted out) claims nothing
        if record.get("origin") != "assignment":
            raise SkillError(
                f"another account's {record.get('origin') or 'record'} also uses this "
                "installation and its approval cannot be checked locally"
            )
        tools |= set(requested) & row_tools
    return tools


def _restore_installed(
    manager: "SkillManager",
    scope: str,
    row: dict[str, Any],
    content_hash: str,
    targets: list[str],
    *,
    consented: set[str],
    op_consent: dict[str, Any],
    pull_op: str | None = None,
    identity: dict[str, Any] | None = None,
) -> None:
    """Bring one installed row to `content_hash` with exactly `targets` in a
    SINGLE install, authorized only by durable consent: the row's pre-op
    consent receipts plus the consent this pull operation was given. A
    capability neither covers raises (the intent stays pending)."""
    from sessionfs.skills.package import load_package_record

    package_id = str(row.get("package_id"))
    omit_provenance = False
    if identity is not None:
        # Back to the prior installation: its bytes under its own source
        # identity and provenance (never the operation's).
        source = manager._row_source({"source": identity.get("source")})
        if source is None:
            raise SkillError(f"the prior source of {package_id} is unknown")
        package = load_package_record(
            manager._cached_version_dir(row, manager._validated_digest(content_hash)),
            source=source,
        )
        prior_provenance = identity.get("provenance")
        if isinstance(prior_provenance, dict):
            package = replace(package, provenance=dict(prior_provenance))
        else:
            # No provenance before: the row gets none (omitted, never an
            # empty record and never the operation version's carried over).
            omit_provenance = True
    elif content_hash == row.get("content_hash"):
        package = manager.load_installed_package(package_id, scope=scope)
    else:
        package = load_package_record(
            manager._cached_version_dir(row, manager._validated_digest(content_hash))
        )
    kwargs = {flag: False for flag, _cli in _FLAG_FOR_REQUIREMENT.values()}
    for requirement in consented:
        flag = _FLAG_FOR_REQUIREMENT.get(requirement)
        if flag is not None:
            kwargs[flag[0]] = True
    for name, value in op_consent.items():
        if name in kwargs and value is True:
            kwargs[name] = True
    plans = manager.plan(package, targets=targets, scope=scope)
    missing = sorted({
        requirement
        for plan in plans for requirement in plan.consent_requirements
        if requirement not in _FLAG_FOR_REQUIREMENT
        or not kwargs[_FLAG_FOR_REQUIREMENT[requirement][0]]
    })
    if missing:
        raise RecoveryPending(missing)
    if pull_op is not None:
        # Still provably the operation's row until its intent is deleted.
        package = replace(package, pull_op=pull_op)
    manager.install_prepared(
        package,
        plans,
        **kwargs,
        expected_current_hash=str(row.get("content_hash")),
        expected_revision=int(row.get("revision") or 0),
        replace_package_id=package_id,
        provenance_mode="omit" if omit_provenance else "keep",
        # Back to the prior installation: its own consented baseline and
        # digest restriction (never the operation version's).
        consent_context=_restore_consent_context(identity) if identity is not None else None,
    )


def _note_unassigned_targets(
    manager: "SkillManager",
    hosted_state: HostedState,
    scope: str,
    local_id: str,
    namespace: str,
    tools: list[str],
    row_tools: set[str],
) -> None:
    """Persist tools a SKIPPED version update leaves on the old version onto
    this namespace's records of the row (their `unassigned_targets`), so a
    later pull still reports them even if a record's own targets are
    rewritten before the update can go ahead. Idempotent; only a real change
    bumps the generation. Caller holds the scope and operation locks."""
    with hosted_state.edit() as state:
        changed = False
        for record in state["pulled"].values():
            if not (
                _record_in_scope(manager, record, scope, local_id)
                and record.get("namespace") == namespace
            ):
                continue
            remembered = sorted(str(tool) for tool in record.get("unassigned_targets") or [])
            updated = sorted((set(remembered) | set(tools)) & row_tools)
            if updated != remembered:
                record["unassigned_targets"] = updated
                changed = True
        if changed:
            state["pulled_generation"] = int(state.get("pulled_generation") or 0) + 1


def _record_origins(
    manager: "SkillManager",
    hosted_state: HostedState,
    item: PullItem,
    keys: list[str],
    live_keys: set[str],
    namespace: str,
    complete_sources: dict[str, bool] | None = None,
    *,
    role: str = "installer",
    origin: str,
    created_install_id: str | None = None,
    accepted: dict[str, str] | None = None,
    clear_opt_outs: dict[str, Any] | None = None,
    finish_op: str | None = None,
) -> bool:
    """Record the item's live origins against its INSTALLED row, atomically.

    Under the scope lock (the same lock a pull-initiated removal holds) the
    installed row is re-read: if it vanished meanwhile, nothing is written
    and False is returned (the caller installs again or defers) - a dangling
    reference is never recorded. Records carry the row's immutable install
    id; `role="managed"` resolves to installer only when this namespace
    already holds VALID authority over that very installation.
    """
    root = _scope_root(manager, item.scope)
    sources = complete_sources or {}
    # An older row without an install id gets one BEFORE anything is bound
    # to it (crash-safe across hosted.json and the skill state, see
    # `_migrate_install_id`). If that fails, nothing is recorded and no
    # tombstone is touched (the item is deferred).
    with _scope_lock(manager, item.scope), manager._operation_lock(item.scope):
        legacy = next(
            (
                candidate for candidate in _installed_rows(manager, item.scope)
                if _matches_item(candidate, item, origin)
            ),
            None,
        )
        if legacy is not None and not (
            isinstance(legacy.get("install_id"), str) and legacy["install_id"]
        ):
            local = str(legacy.get("package_id"))
            if not _migrate_install_id(manager, hosted_state, item.scope, local):
                raise RecordingDeferred(f"could not give {local} an install id; retried next pull")
    # scope lock -> operation lock (no install/remove can run between the row
    # re-check and the write) -> hosted-state lock.
    with (
        _scope_lock(manager, item.scope),
        manager._operation_lock(item.scope),
        hosted_state.edit() as state,
    ):
        row = next(
            (
                candidate for candidate in _installed_rows(manager, item.scope)
                if _matches_item(candidate, item, origin)
            ),
            None,
        )
        if row is None:
            return False
        local_id = str(row.get("package_id"))
        if not (isinstance(row.get("install_id"), str) and row["install_id"]):
            raise RecordingDeferred(f"could not give {local_id} an install id; retried next pull")
        install_ids = _install_ids(manager, item.scope)
        row_tools = {str(tool) for tool in row.get("targets") or []}
        if role == "managed":
            created = created_install_id is not None and row.get("install_id") == created_install_id
            role = (
                "installer"
                if created
                or local_id in _managed_ids(
                    manager, state["pulled"], item.scope, namespace, install_ids
                )
                else "reference"
            )
        for other_key, other in list(state["pulled"].items()):
            if not _record_in_scope(manager, other, item.scope, local_id):
                continue
            if "namespace" not in other and role == "installer":
                # Legacy unscoped record: adopted by this namespace now.
                state["pulled"].pop(other_key, None)
            elif (
                item.status in {"installed", "updated"}
                and other.get("namespace") == namespace
                and other_key not in live_keys
                and sources.get(str(other.get("origin"))) is True
            ):
                # A replaced version's / stale origin's record of THIS
                # namespace now points at the same package: drop it.
                state["pulled"].pop(other_key, None)
        for key, (kind, origin_id) in zip(keys, item.origins):
            existing = state["pulled"].get(key)
            inherited = isinstance(existing, dict) and _has_authority(existing, install_ids)
            origin_key = f"{kind}|{origin_id}"
            mode = item.origin_modes.get(origin_key, item.mode)
            requested = [str(tool) for tool in item.origin_targets.get(origin_key, [])]
            # Explicit optional-acceptance provenance, BOUND to what was
            # accepted (version, content, tools): a fresh acceptance is
            # stamped now; an earlier one is carried over only while it
            # still covers exactly that version and content and no wider
            # tool set - otherwise fresh consent is needed.
            acceptance: dict[str, Any] = {}
            if mode == "optional":
                if accepted and origin_key in accepted:
                    acceptance = {
                        "accepted_at": _now(),
                        "accepted_via": accepted[origin_key],
                        "accepted_version_id": item.version_id,
                        "accepted_content_hash": item.content_hash,
                        "accepted_targets": sorted(requested),
                    }
                elif (
                    isinstance(existing, dict)
                    and existing.get("mode") == "optional"
                    and _acceptance_covers(existing, item.version_id, item.content_hash, requested)
                ):
                    acceptance = {
                        name: existing[name]
                        for name in (
                            "accepted_at", "accepted_via", "accepted_version_id",
                            "accepted_content_hash", "accepted_targets",
                        )
                        if name in existing
                    }
            state["pulled"][key] = {
                "mode": mode,
                **acceptance,
                "namespace": namespace,
                "role": "installer" if role == "installer" or inherited else "reference",
                "install_id": row.get("install_id"),
                "scope": item.scope,
                "project_root": root,
                "origin": kind,
                "origin_id": origin_id,
                "version_id": item.version_id,
                "content_hash": item.content_hash,
                "targets": requested,
                # Reported-only tools no live origin asks for any more.
                "unassigned_targets": sorted(set(item.unassigned_tools) & row_tools),
                # Written by this pull operation (its recovery evidence).
                **({"pull_op": finish_op} if finish_op else {}),
                "package_id": item.package_id
                or (existing.get("package_id") if isinstance(existing, dict) else ""),
                "local_package_id": local_id,
                "installed_at": (
                    existing.get("installed_at") if isinstance(existing, dict) else None
                ) or _now(),
            }
        if finish_op:
            # The pull operation's intent ends in the SAME write as its
            # recording: no crash can leave one without the other.
            state["pull_ops"].pop(finish_op, None)
        for opt_key, generation in (clear_opt_outs or {}).items():
            tombstone = state["opt_outs"].get(opt_key)
            if isinstance(tombstone, dict) and tombstone.get("generation") == generation:
                state["opt_outs"].pop(opt_key, None)
        state["pulled_generation"] = int(state.get("pulled_generation") or 0) + 1
    return True


def _opt_out_key(
    manager: "SkillManager", namespace: str, scope: str, kind: str, version_id: str
) -> str:
    """Opt-outs are keyed by (namespace, scope/project, origin kind, version)
    - never by an assignment's row id, so recreating an identical optional
    assignment cannot defeat them. A version id identifies its package on
    the namespace's server (the package id is kept in the value)."""
    root = str(manager.project_root) if scope == "project" else "~"
    return f"{namespace}|{scope}|{root}|{kind}|{version_id}"


def _item_opt_out_keys(manager: "SkillManager", item: PullItem, namespace: str) -> list[str]:
    return list(
        dict.fromkeys(
            _opt_out_key(manager, namespace, item.scope, kind, item.version_id)
            for kind, _origin_id in item.origins
        )
    )


def _optional_accepted(
    manager: "SkillManager",
    pulled: dict[str, Any],
    keys: list[str],
    item: PullItem,
    *,
    namespace: str,
    opt_outs: dict[str, Any],
) -> bool:
    """An optional origin counts as ACCEPTED only while one of its records
    carries explicit optional-acceptance provenance (`mode: optional`,
    `accepted_at`, `accepted_via` prompt|yes) AND is bound to a currently
    installed row with the same install id. Records written while the origin
    was required, by older builds, or as plain references never count (fail
    closed). Also sets `item.opted_out` when the user explicitly removed this
    optional version (an opt-out survives assignment cleanup and
    re-creation)."""
    install_ids = _install_ids(manager, item.scope)
    records = [pulled[key] for key in keys if isinstance(pulled.get(key), dict)]
    item.opted_out = any(
        key in opt_outs for key in _item_opt_out_keys(manager, item, namespace)
    )
    if item.opted_out:
        # An applicable opt-out - also a PENDING one left by a crash between
        # writing it and the removal - voids any earlier acceptance: only an
        # explicit re-acceptance brings the origin back.
        return False

    def _bound(record: dict[str, Any]) -> bool:
        install_id = record.get("install_id")
        current = install_ids.get(str(record.get("local_package_id")))
        return isinstance(install_id, str) and bool(install_id) and install_id == current

    return any(
        record.get("mode") == "optional"
        and isinstance(record.get("accepted_at"), str)
        and record.get("accepted_via") in {"prompt", "yes"}
        and _bound(record)
        and _acceptance_covers(record, item.version_id, item.content_hash, item.targets)
        for record in records
    )


def _acceptance_covers(
    record: dict[str, Any], version_id: str, content_hash: str, targets: list[str]
) -> bool:
    """The acceptance on `record` was given for exactly this version and
    content and for at least these tools (an unbound acceptance from an
    older build covers nothing: fail closed)."""
    accepted_targets = record.get("accepted_targets")
    return (
        record.get("accepted_version_id") == version_id
        and record.get("accepted_content_hash") == content_hash
        and isinstance(accepted_targets, list)
        and {str(tool) for tool in targets} <= {str(tool) for tool in accepted_targets}
    )


def begin_opt_out(manager: "SkillManager", scope: str, local_package_id: str) -> list[str]:
    """First half of an explicit `sfs skill remove`: write PENDING opt-outs
    for every (namespace, origin kind, version) that references the package
    - BEFORE the removal, so a crash can never leave the package removed
    without its opt-out. Pull's installer roles are left alone here; they
    are retired by `finish_opt_out` only once the removal SUCCEEDED (a
    failed removal leaves the bookkeeping exactly as it was). Lock order:
    scope -> operation -> hosted state (re-entrant inside
    `SkillManager.explicit_change`)."""
    hosted_state = HostedState(manager.store_root)
    keys: list[str] = []
    with _scope_lock(manager, scope), manager._operation_lock(scope), hosted_state.edit() as state:
        # Every removal intent gets a new, monotonically increasing
        # generation (also on an opt-out that already exists): an acceptance
        # decided before it can never clear or outlive it.
        generation = int(state.get("opt_out_generation") or 0) + 1
        state["opt_out_generation"] = generation

        def _tombstone(namespace: str, version_id: str, kind: str, package_id: Any) -> None:
            key = _opt_out_key(manager, namespace, scope, kind, version_id)
            existing = state["opt_outs"].get(key)
            if isinstance(existing, dict):
                existing["generation"] = generation
            else:
                keys.append(key)
                state["opt_outs"][key] = {
                    "pending": True,
                    "package_id": package_id,
                    "local_package_id": local_package_id,
                    "version_id": version_id,
                    "created_at": _now(),
                    "generation": generation,
                }

        for record in state["pulled"].values():
            if not _record_in_scope(manager, record, scope, local_package_id):
                continue
            namespace = record.get("namespace")
            version_id = record.get("version_id")
            if not isinstance(namespace, str) or not isinstance(version_id, str):
                continue
            _tombstone(
                namespace, version_id, str(record.get("origin")), record.get("package_id")
            )
        # An install/update that crashed after stamping the row but before
        # `_record_origins` left the current version ONLY in a live pull
        # operation. Opt that version out too, from the intent's namespace,
        # version and origins - otherwise recovery discards the now-rowless
        # intent and `pull --yes` silently reinstalls it.
        for row in _installed_rows(manager, scope):
            if row.get("package_id") != local_package_id:
                continue
            token = row.get("pull_op")
            if isinstance(token, str) and token:
                op = state["pull_ops"].get(token)
                if isinstance(op, dict):
                    namespace = op.get("namespace")
                    version_id = op.get("version_id")
                    if isinstance(namespace, str) and isinstance(version_id, str):
                        for kind, _origin_id in op.get("origins") or []:
                            _tombstone(namespace, version_id, str(kind), op.get("package_id"))
            # An already-current package whose fresh optional origin added no
            # tools is never stamped: `_reconcile_targets` skips the compile,
            # so the accepted origin survives only in the live intent's
            # before-image and fresh_keys. Match that unstamped intent by the
            # row's exact install id, unchanged identity, and the version the
            # op was pulling being the one this row still has - then tombstone
            # its fresh origins. Without this, recovery discards the
            # unstamped intent and `pull --yes` reinstalls an explicit
            # removal; no recovery mutation should happen without a stamp.
            row_source = row.get("source")
            row_commit = row_source.get("commit") if isinstance(row_source, dict) else None
            for op in list(state.get("pull_ops", {}).values()):
                if not isinstance(op, dict) or op.get("scope") != scope:
                    continue
                op_version = op.get("version_id")
                if not isinstance(op_version, str) or op_version != row_commit:
                    continue
                before = op.get("before")
                prior = before.get(local_package_id) if isinstance(before, dict) else None
                if not isinstance(prior, dict):
                    continue
                if prior.get("install_id") != row.get("install_id"):
                    continue
                if not _same_identity(row, prior):
                    continue
                namespace = op.get("namespace")
                if not isinstance(namespace, str):
                    continue
                for key in op.get("fresh_keys") or []:
                    if not isinstance(key, str):
                        continue
                    kind, _, _origin_id = key.partition("|")
                    _tombstone(namespace, op_version, kind, op.get("package_id"))
        state["pulled_generation"] = int(state.get("pulled_generation") or 0) + 1
    return keys


def finish_opt_out(
    manager: "SkillManager", scope: str, keys: list[str], *, removed: bool
) -> None:
    """Second half: after a successful removal, confirm the pending
    opt-outs and retire pull's installer roles over the removed package;
    after a failed one, roll the opt-outs back and leave every role as it
    was. (A pending opt-out left by a crash still counts as an opt-out - the
    safe direction; the removed installation's id is gone, so a stale
    installer record never has authority over a later reinstall.)"""
    if not keys:
        return
    with _scope_lock(manager, scope), manager._operation_lock(scope), (
        HostedState(manager.store_root).edit()
    ) as state:
        removed_ids: set[str] = set()
        for key in keys:
            entry = state["opt_outs"].get(key)
            if not isinstance(entry, dict):
                continue
            if removed:
                entry.pop("pending", None)
                if isinstance(entry.get("local_package_id"), str):
                    removed_ids.add(entry["local_package_id"])
            else:
                state["opt_outs"].pop(key, None)
        for record in state["pulled"].values():
            if any(
                _record_in_scope(manager, record, scope, local_id) for local_id in removed_ids
            ) and _is_installer(record):
                record["role"] = "reference"
        if removed_ids:
            state["pulled_generation"] = int(state.get("pulled_generation") or 0) + 1


def clear_opt_outs(
    manager: "SkillManager",
    scope: str,
    *,
    keys: list[str] | None = None,
    local_package_id: str | None = None,
) -> int:
    """Lift opt-outs: on an explicit interactive acceptance (`keys`) or an
    explicit `sfs skill add` of that package (`local_package_id`). Never on
    assignment cleanup."""
    hosted_state = HostedState(manager.store_root)
    current = hosted_state.read()["opt_outs"]
    root = str(manager.project_root) if scope == "project" else "~"

    def _matches(key: str, entry: Any) -> bool:
        if keys is not None and key in keys:
            return True
        return (
            local_package_id is not None
            and isinstance(entry, dict)
            and entry.get("local_package_id") == local_package_id
            and f"|{scope}|{root}|" in key
        )

    if not any(_matches(key, entry) for key, entry in current.items()):
        return 0
    removed = 0
    with _scope_lock(manager, scope), manager._operation_lock(scope), hosted_state.edit() as state:
        for key, entry in list(state["opt_outs"].items()):
            if _matches(key, entry):
                state["opt_outs"].pop(key, None)
                removed += 1
    return removed


def _install_ids(manager: "SkillManager", scope: str) -> dict[str, str | None]:
    """Installed package id -> immutable installation id (None: older row)."""
    result: dict[str, str | None] = {}
    for row in _installed_rows(manager, scope):
        value = row.get("install_id")
        result[str(row.get("package_id"))] = value if isinstance(value, str) else None
    return result


def _has_authority(record: dict[str, Any], install_ids: dict[str, str | None]) -> bool:
    """An installer record holds authority only over the EXACT installation
    it was written for: a remove + re-add (new install id) voids it."""
    if not _is_installer(record):
        return False
    install_id = record.get("install_id")
    current = install_ids.get(str(record.get("local_package_id")))
    # Both must be real ids: two missing ids (None == None) never match.
    return isinstance(install_id, str) and bool(install_id) and install_id == current


def _managed_ids(
    manager: "SkillManager",
    pulled: dict[str, Any],
    scope: str,
    namespace: str,
    install_ids: dict[str, str | None] | None = None,
) -> set[str]:
    """Local package ids THIS namespace INSTALLED in `scope` and still holds
    valid authority over (installer records bound to the current install
    id), plus those of legacy unscoped records, which a live origin of this
    namespace adopts. Plain references never count."""
    ids = install_ids if install_ids is not None else _install_ids(manager, scope)
    return {
        str(record.get("local_package_id"))
        for record in pulled.values()
        if _record_in_scope(manager, record, scope)
        and record.get("namespace") in (namespace, None)
        and _has_authority(record, ids)
    }


def retire_pull_authority(
    manager: "SkillManager", scope: str, local_package_id: str, *, opt_out: bool = False
) -> int:
    """The user explicitly removed or (re)added this package: pull loses its
    authority to manage it (update it or change its tools) - installer
    records become plain references.

    With `opt_out` (an explicit REMOVE) every record of the package also
    becomes an opt-out tombstone for its (origin, version): an OPTIONAL
    assignment is then neither offered by `--yes` nor reinstalled by the
    daemon until the user opts in again interactively. Required assignments
    still reinstall.

    Lock order: scope lock -> operation lock -> hosted-state lock (re-entrant
    when the caller already holds the first two)."""
    if opt_out:
        keys = begin_opt_out(manager, scope, local_package_id)
        finish_opt_out(manager, scope, keys, removed=True)
        return len(keys)
    hosted_state = HostedState(manager.store_root)
    if not any(
        _record_in_scope(manager, record, scope, local_package_id)
        for record in hosted_state.read()["pulled"].values()
    ):
        return 0
    changed = 0
    with _scope_lock(manager, scope), manager._operation_lock(scope), hosted_state.edit() as state:
        for record in state["pulled"].values():
            if not _record_in_scope(manager, record, scope, local_package_id):
                continue
            if _is_installer(record):
                record["role"] = "reference"
                changed += 1
        state["pulled_generation"] = int(state.get("pulled_generation") or 0) + 1
    return changed


def _local_id_for(manager: "SkillManager", item: PullItem, origin: str) -> str | None:
    for row in _installed_rows(manager, item.scope):
        if _matches_item(row, item, origin):
            return str(row.get("package_id"))
    return None


# Install + record attempts per item when the package vanishes in between.
MAX_RECORD_ATTEMPTS = 2


def _package_records(
    manager: "SkillManager", pulled: dict[str, Any], scope: str, local_id: str
) -> dict[str, dict[str, Any]]:
    return {
        key: record for key, record in pulled.items()
        if _record_in_scope(manager, record, scope, local_id)
    }


def _report_unassigned(
    manager: "SkillManager",
    report: PullReport,
    scopes: tuple[str, ...],
    live_keys: set[str],
    namespace: str,
) -> None:
    """Pull NEVER uninstalls a package because its assignment or approval
    disappeared. A record of this namespace whose origin is gone from a
    COMPLETE listing is only marked `unassigned` (shown by pull and doctor;
    `sfs skill remove` removes the package). Its installer authority passes
    to a live record of this namespace for the same package, if any. Records
    are only dropped for a package that is no longer installed. An
    incomplete listing marks nothing.

    Each scope is processed under its scope and operation locks (no install
    or removal can change its rows meanwhile), then the hosted-state lock;
    the write is fenced by `pulled_generation`: if bookkeeping changed since
    the snapshot, the pass is recomputed (a few times, else left for the
    next pull)."""
    for scope in scopes:
        for _attempt in range(3):
            outcome = _report_unassigned_scope(manager, report, scope, live_keys, namespace)
            if outcome is not None:
                report.unassigned.extend(outcome)
                break
        else:
            report.note(
                f"Unassigned packages ({scope} scope) were not checked: bookkeeping kept "
                "changing; checked again next pull"
            )


def _report_unassigned_scope(
    manager: "SkillManager",
    report: PullReport,
    scope: str,
    live_keys: set[str],
    namespace: str,
) -> list[dict[str, Any]] | None:
    """One fenced pass; None when the snapshot went stale (nothing written)."""
    hosted_state = HostedState(manager.store_root)
    with _scope_lock(manager, scope), manager._operation_lock(scope):
        snapshot = hosted_state.read()
        generation = int(snapshot.get("pulled_generation") or 0)

        def _stale(key: str, record: Any) -> bool:
            return (
                _record_in_scope(manager, record, scope)
                and key not in live_keys
                and record.get("namespace") == namespace
                and report.complete_sources.get(str(record.get("origin"))) is True
            )

        stale_by_package: dict[str, list[str]] = {}
        for key, record in snapshot["pulled"].items():
            if _stale(key, record):
                stale_by_package.setdefault(str(record.get("local_package_id")), []).append(key)
        if not stale_by_package:
            return []
        try:
            # Authoritative: a read error must never look like "not installed"
            # (which would drop the records).
            rows = {
                str(row.get("package_id")): row
                for row in _installed_rows(manager, scope, strict=True)
            }
        except SkillError as exc:
            report.note(f"Unassigned packages ({scope} scope) were not checked: {exc}")
            return []
        install_ids: dict[str, str | None] = {
            package_id: (row.get("install_id") if isinstance(row.get("install_id"), str) else None)
            for package_id, row in rows.items()
        }
        entries: list[dict[str, Any]] = []
        with hosted_state.edit() as state:
            if int(state.get("pulled_generation") or 0) != generation:
                return None  # changed since the snapshot: recompute
            for local_id, stale_keys in sorted(stale_by_package.items()):
                row = rows.get(local_id)
                records = _package_records(manager, state["pulled"], scope, local_id)
                if row is None:
                    for key in stale_keys:
                        state["pulled"].pop(key, None)  # nothing installed any more
                    continue
                own_live = [
                    key for key, record in records.items()
                    if key in live_keys and record.get("namespace") == namespace
                ]
                authority = any(
                    key in records and (
                        _has_authority(records[key], install_ids)
                        # A legacy installer record still holds authority over a
                        # legacy row (no install id on either side): `_has_authority`
                        # deliberately refuses two missing ids, but the row predates
                        # install ids, so no remove+re-add could have voided it.
                        # Treating it as authoritative keeps the unassigned report
                        # from silently dropping the only evidence pull manages it.
                        or (install_ids.get(local_id) is None and _is_installer(records[key]))
                    )
                    for key in stale_keys
                )
                if own_live or not authority:
                    # Still assigned through another live origin (which takes
                    # over the installer authority), or only ever a plain
                    # reference (e.g. the user's own install): forget the
                    # stale origins - there is nothing pull manages to report.
                    if authority:
                        for key in own_live:
                            records[key]["role"] = "installer"
                            records[key]["install_id"] = row.get("install_id")
                    for key in stale_keys:
                        state["pulled"].pop(key, None)
                    continue
                for key in stale_keys:
                    if key in records:
                        records[key].setdefault("unassigned_since", _now())
                if all(record.get("unassigned_since") for record in records.values()):
                    entries.append(_unassigned_entry(scope, row))
            state["pulled_generation"] = generation + 1
        return entries


def _unassigned_entry(scope: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": scope,
        "package_id": str(row.get("package_id")),
        "name": clean_untrusted_text(row.get("name"), limit=100, keep_newlines=False),
    }


def unassigned_packages(
    manager: "SkillManager", scope: str | None = None
) -> list[dict[str, Any]]:
    """Installed packages that pull manages and that are no longer assigned
    or approved anywhere (every record of the package is marked
    `unassigned`) - for `sfs skill doctor`. Nothing is ever removed for it."""
    pulled = HostedState(manager.store_root).read()["pulled"]
    found: list[dict[str, Any]] = []
    for item_scope in (scope,) if scope else ("project", "user"):
        for row in _installed_rows(manager, item_scope):
            records = _package_records(manager, pulled, item_scope, str(row.get("package_id")))
            if records and all(record.get("unassigned_since") for record in records.values()):
                found.append(_unassigned_entry(item_scope, row))
    return found


def daemon_reconcile(
    manager: "SkillManager",
    client: HostedClient,
    *,
    device_id: str | None = None,
    deadline: float | None = None,
    cancel: Callable[[], bool] | None = None,
    on_recovery: Callable[[str], None] | None = None,
) -> PullReport:
    """Unattended reconcile: blocked quarantine + required assignments only.

    Never prompts, never installs optional items, never uninstalls anything
    (only blocked content is quarantined), never grants an elevated
    capability (packages that need one are skipped and reported), and never
    applies an update that widens access or changes executable content: it
    is reported as ``held`` and the installed version stays (marketplace v2
    D2). A text-only update of an installed version carries the consent
    given for that version forward. Works in
    user scope only - the daemon has no project context; project-scope
    blocked installs are quarantined by the next `sfs skill pull` in that
    project (and flagged by doctor until then).
    """
    return pull(
        manager,
        client,
        device_id=device_id,
        flags={},
        install_optional=None,
        include_requests=False,
        scopes=("user",),
        deadline=deadline,
        cancel=cancel,
        on_recovery=on_recovery,
    )


def is_interactive() -> bool:
    return sys.stdin.isatty()
