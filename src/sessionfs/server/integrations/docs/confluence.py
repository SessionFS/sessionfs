"""Confluence Cloud REST client + a small Markdown → storage-format converter.

The site URL is the SSRF control: it must be ``https://<site>.atlassian.net``
(lowercase alphanumerics and hyphens only), redirects are never followed, and
every request times out after 20 s. Errors are sanitized before they leave
this module — the API token and request headers are never logged or returned.

The converter is a strict allowlist: headings, paragraphs, bullet/numbered
lists, fenced code blocks, tables, links, emphasis and inline code. Anything
else is emitted as escaped text, so source Markdown can never inject raw
Confluence markup or macros.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import xml.sax.saxutils
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import httpx

from sessionfs.server.integrations.docs.base import (
    DocChildPage,
    DocPage,
    DocSpace,
    DocSpacePage,
    DocumentationError,
    DocumentationNotFound,
)

# Strict host allowlist: only Confluence Cloud sites, no ports, no paths, no
# userinfo, no redirects. This is also the anti-SSRF boundary (same as Jira).
_SITE_URL_RE = re.compile(r"^https://[a-z0-9-]+\.atlassian\.net$")

# Only safe URL schemes may become link hrefs; ``javascript:``/``data:``/
# anything else is emitted as plain text instead of a clickable link.
_LINK_SCHEME_RE = re.compile(r"^(?:https?|mailto):", re.IGNORECASE)

_TIMEOUT = 20.0

# Cap response bodies so a hostile or misbehaving response cannot balloon
# memory; anything larger is a DocumentationError.
_MAX_BODY_BYTES = 2 * 1024 * 1024

# Space/page picker bounds, mirroring the Jira project picker.
_SEARCH_MAX_LIMIT = 100
_SEARCH_DEFAULT_LIMIT = 50


def validate_site_url(site_url: str) -> str:
    """Return the normalized site URL, or raise ``DocumentationError``."""
    url = (site_url or "").strip().rstrip("/")
    if not _SITE_URL_RE.match(url):
        raise DocumentationError(
            "site_url must be a Confluence Cloud URL like https://<site>.atlassian.net"
        )
    return url


def sessionfs_label(project_id: str, kind: str, key: str) -> str:
    """The ``sessionfs-<project>-<kind>-<key>-<hash>`` label stamped on a page.

    This is the adoption key: a publish whose outcome is unconfirmed leaves
    the link ``unknown`` and the next publish searches Confluence with CQL
    ``label = "<this>"`` to adopt the page instead of creating a duplicate.

    Confluence lowercases labels and refuses spaces and most punctuation, so
    every part is reduced to ``[a-z0-9_-]``; the short hash of the raw parts
    keeps two sources that normalize alike (``Home`` vs ``home``) distinct.
    """

    def part(value: str, fallback: str, cap: int) -> str:
        cleaned = re.sub(r"[^a-z0-9_-]+", "-", (value or "").strip().lower()).strip("-")
        return cleaned[:cap] or fallback

    digest = hashlib.sha256(
        f"{project_id}\x00{kind}\x00{key}".encode()
    ).hexdigest()[:8]
    return (
        f"sessionfs-{part(project_id, 'project', 64)}-{part(kind, 'page', 20)}-"
        f"{part(key, 'source', 80)}-{digest}"
    )


# ── Markdown → Confluence storage format ──


_INLINE_RE = re.compile(
    r"\[(?P<link_text>[^\]\n]{1,500})\]\((?P<link_url>(?:[^()\s]|\([^()\s]*\))+)\)"
    r"|\[\[(?P<backlink>[^\]\n]{1,200})\]\]"
    r"|\*\*(?P<bold>[^*]+)\*\*"
    r"|\*(?P<italic>[^*]+)\*"
    r"|`(?P<code>[^`]+)`"
)

_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^```")
# A ``|---|:--:|`` separator row: only whitespace, colons, dashes and pipes,
# with a pipe after the first character. Checked without a backtracking
# pattern (the old ``\|?[\s:|-]+\|[\s:|-]*`` regex was quadratic on long
# runs of pipes and stalled the event loop).
_TABLE_SEP_CHARS_RE = re.compile(r"[\s:|-]+")


def _is_table_separator(line: str) -> bool:
    if _TABLE_SEP_CHARS_RE.fullmatch(line) is None:
        return False
    return "|" in line[1:]


def _escape(text: str) -> str:
    return xml.sax.saxutils.escape(text, {'"': "&quot;"})


def escape_text(text: str) -> str:
    """Escape plain text for storage XHTML (``&``, ``<``, ``>``, ``"``)."""
    return _escape(text or "")


def _inline(text: str, backlink_resolver: Callable[[str], str | None] | None) -> str:
    """Parse a single line's inline marks into escaped storage-format HTML."""
    out: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(_escape(text[pos : m.start()]))
        if m.group("link_text") is not None:
            href = m.group("link_url")
            if _LINK_SCHEME_RE.match(href):
                out.append(
                    f'<a href="{_escape(href)}">{_escape(m.group("link_text"))}</a>'
                )
            else:
                out.append(_escape(m.group("link_text")))
        elif m.group("backlink") is not None:
            slug = m.group("backlink").strip()
            target = backlink_resolver(slug) if backlink_resolver else None
            if target:
                # A linked wiki page → a Confluence link; an unlinked target is
                # plain text (never a broken ``/wiki/spaces/...`` link).
                out.append(f'<a href="{_escape(target)}">{_escape(slug)}</a>')
            else:
                out.append(_escape(slug))
        elif m.group("bold") is not None:
            out.append(f"<strong>{_escape(m.group('bold'))}</strong>")
        elif m.group("italic") is not None:
            out.append(f"<em>{_escape(m.group('italic'))}</em>")
        elif m.group("code") is not None:
            out.append(f"<code>{_escape(m.group('code'))}</code>")
        pos = m.end()
    if pos < len(text):
        out.append(_escape(text[pos:]))
    return "".join(out) or ""


def _inline_multiline(
    text: str, backlink_resolver: Callable[[str], str | None] | None
) -> str:
    """Parse inline marks across multiple lines, joined with ``<br/>``."""
    lines = [_inline(line, backlink_resolver) for line in text.split("\n")]
    return "<br/>".join(lines)


def _is_block_start(line: str) -> bool:
    s = line.strip()
    return bool(
        not s
        or _FENCE_RE.match(s)
        or _HEADING_RE.match(s)
        or _BULLET_RE.match(s)
        or _NUMBERED_RE.match(s)
    )


def _escape_cdata(text: str) -> str:
    """Escape ``]]>`` inside a code block's CDATA section."""
    return text.replace("]]>", "]]]]><![CDATA[>")


def _split_table_row(line: str) -> list[str]:
    s = line.strip().strip("|")
    return [cell.strip() for cell in s.split("|")]


def _table_is_data_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and "|" in s[1:]


def markdown_to_storage(
    text: str,
    *,
    backlink_resolver: Callable[[str], str | None] | None = None,
) -> str:
    """Convert Markdown into Confluence storage-format XHTML.

    Strict allowlist (headings, paragraphs, bullet/numbered lists, fenced code
    blocks, tables, links, emphasis, inline code). Unknown constructs fall back
    to escaped paragraph text; ``[[slug]]`` backlinks resolve through
    ``backlink_resolver`` (a linked page becomes an ``<a>``, otherwise the slug
    is plain text).
    """
    lines = (text or "").splitlines()
    blocks: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        if _FENCE_RE.match(stripped):
            code_lines: list[str] = []
            i += 1
            while i < n and not _FENCE_RE.match(lines[i].strip()):
                code_lines.append(lines[i])
                i += 1
            i += 1  # closing fence
            code = _escape_cdata("\n".join(code_lines)) or " "
            blocks.append(
                '<ac:structured-macro ac:name="code" '
                'xmlns:ac="http://atlassian.com/content">'
                "<ac:plain-text-body>"
                f"<![CDATA[{code}]]>"
                "</ac:plain-text-body></ac:structured-macro>"
            )
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            level = min(len(heading.group(1)), 6)
            blocks.append(
                f"<h{level}>{_inline(heading.group(2), backlink_resolver)}</h{level}>"
            )
            i += 1
            continue

        if _table_is_data_row(stripped):
            # Collect a table run: header row, separator row, body rows.
            rows: list[list[str]] = [_split_table_row(stripped)]
            i += 1
            if i < n and _is_table_separator(lines[i]):
                i += 1  # skip the ``|---|---|`` separator
            while i < n and _table_is_data_row(lines[i].strip()):
                rows.append(_split_table_row(lines[i].strip()))
                i += 1
            body = "".join(
                "<tr>"
                + "".join(
                    f"<td>{_inline(cell, backlink_resolver)}</td>" for cell in row
                )
                + "</tr>"
                for row in rows
            )
            blocks.append(f"<table><tbody>{body}</tbody></table>")
            continue

        bullet = _BULLET_RE.match(stripped)
        if bullet:
            items: list[str] = []
            while i < n:
                m = _BULLET_RE.match(lines[i].strip())
                if not m:
                    break
                text = m.group(1)
                i += 1
                while i < n:
                    nxt = lines[i]
                    if not nxt.strip():
                        break
                    if nxt.startswith((" ", "\t")):
                        text += "\n" + nxt.lstrip()
                        i += 1
                    else:
                        break
                items.append(text)
            blocks.append(
                "<ul>"
                + "".join(
                    f"<li>{_inline_multiline(item, backlink_resolver)}</li>"
                    for item in items
                )
                + "</ul>"
            )
            continue

        numbered = _NUMBERED_RE.match(stripped)
        if numbered:
            items = []
            while i < n:
                m = _NUMBERED_RE.match(lines[i].strip())
                if not m:
                    break
                text = m.group(1)
                i += 1
                while i < n:
                    nxt = lines[i]
                    if not nxt.strip():
                        break
                    if nxt.startswith((" ", "\t")):
                        text += "\n" + nxt.lstrip()
                        i += 1
                    else:
                        break
                items.append(text)
            blocks.append(
                "<ol>"
                + "".join(
                    f"<li>{_inline_multiline(item, backlink_resolver)}</li>"
                    for item in items
                )
                + "</ol>"
            )
            continue

        para_lines = [stripped]
        i += 1
        while i < n and not _is_block_start(lines[i]):
            para_lines.append(lines[i].strip())
            i += 1
        blocks.append(
            f"<p>{_inline(' '.join(para_lines), backlink_resolver)}</p>"
        )

    return "".join(blocks)


_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|h[1-6]|li|tr|table|ul|ol|br|ac:structured-macro|ac:plain-text-body)\b[^>]*>",
    re.IGNORECASE,
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def storage_to_text(storage: str) -> str:
    """Reduce storage XHTML to plain text lines (for drift diffs only).

    Block tags become line breaks, CDATA bodies are kept, every other tag is
    dropped and entities are unescaped. Confluence re-serializes storage on
    save (attribute order, macro ids), so diffing the text form shows what a
    human actually changed rather than markup churn.
    """
    text = _CDATA_RE.sub(lambda m: "\n" + m.group(1) + "\n", storage or "")
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub("", text)
    text = xml.sax.saxutils.unescape(text, {"&quot;": '"', "&#39;": "'", "&nbsp;": " "})
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def storage_text_length(storage: str) -> int:
    """Length of the storage-format body (the number Confluence's cap counts).

    Confluence Cloud caps a page body near 2,000,000 characters; the route
    compares this against its own 1 MiB guard before any outbound call.
    """
    return len(storage or "")


# ── Confluence Cloud client ──


# Confluence page/space ids are numeric (the v2 path parameters are int64).
# Anything else is refused before it can reach a URL path.
_NUMERIC_ID_RE = re.compile(r"^[0-9]{1,30}$")

# Space keys as the routes validate them; re-checked here before a key is
# interpolated into CQL.
_SPACE_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,50}$")

# ``GET /wiki/api/v2/spaces`` has no text filter, so the space picker lists
# spaces (250 per page, the documented maximum) and filters by key/name. The
# scan is bounded so a site with thousands of spaces can't turn one picker
# keystroke into an unbounded crawl.
_SPACE_PAGE_SIZE = 250
_SPACE_SCAN_PAGES = 4


def _require_numeric_id(value: str, what: str) -> str:
    text = str(value or "").strip()
    if not _NUMERIC_ID_RE.match(text):
        raise DocumentationError(f"{what} must be a numeric Confluence id")
    return text


def _cql_string(value: str) -> str:
    """Quote ``value`` as a CQL string literal (backslash and quote escaped)."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _next_cursor(data: dict) -> str | None:
    """The ``cursor`` of a v2 ``_links.next`` URL (never followed directly)."""
    links = data.get("_links")
    if not isinstance(links, dict):
        return None
    nxt = links.get("next")
    if not isinstance(nxt, str) or not nxt:
        return None
    values = parse_qs(urlsplit(nxt).query).get("cursor")
    return values[0] if values else None


def _version_number(data: dict) -> int | None:
    version = data.get("version")
    number = version.get("number") if isinstance(version, dict) else None
    if isinstance(number, bool):
        return None
    if isinstance(number, int):
        return number
    if isinstance(number, str) and number.isdigit():
        return int(number)
    return None


def _page_from_v2(data: dict, fallback_id: str = "", fallback_title: str = "") -> DocPage:
    body = data.get("body")
    storage = None
    if isinstance(body, dict):
        rep = body.get("storage")
        if isinstance(rep, dict) and isinstance(rep.get("value"), str):
            storage = rep["value"]
    return DocPage(
        id=str(data.get("id") or fallback_id),
        version=_version_number(data) or 1,
        title=str(data.get("title") or fallback_title),
        space_id=str(data["spaceId"]) if data.get("spaceId") is not None else None,
        parent_id=str(data["parentId"]) if data.get("parentId") is not None else None,
        author_id=str(data["authorId"]) if data.get("authorId") is not None else None,
        created_at=str(data["createdAt"]) if data.get("createdAt") else None,
        storage=storage,
    )


class ConfluenceClient:
    """Async Confluence Cloud client using Atlassian API-token Basic auth.

    Pages and spaces go through REST v2 (``/wiki/api/v2``). Three things v2
    does not offer use documented v1 endpoints: the current user
    (``/wiki/rest/api/user/current``), CQL search (``/wiki/rest/api/search``)
    and adding a label (``/wiki/rest/api/content/{id}/label``).

    Instances are async context managers; close with ``await client.aclose()``
    or ``async with``. A ``transport`` may be injected for tests (never a real
    Confluence call).
    """

    def __init__(
        self,
        site_url: str,
        account_email: str,
        api_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.site_url = validate_site_url(site_url)
        self.account_email = account_email
        self.api_token = api_token
        self._client = httpx.AsyncClient(
            base_url=self.site_url,
            timeout=_TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> "ConfluenceClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _auth_headers(self, *, with_body: bool) -> dict[str, str]:
        raw = base64.b64encode(
            f"{self.account_email}:{self.api_token}".encode()
        ).decode()
        headers = {"Authorization": f"Basic {raw}", "Accept": "application/json"}
        if with_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _sanitize(self, message: str) -> str:
        """Scrub any trace of the credential before the message escapes."""
        out = message
        if self.api_token:
            out = out.replace(self.api_token, "[REDACTED]")
        return out[:300]

    async def _read_body(self, resp: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_BODY_BYTES:
                    raise DocumentationError(
                        "Confluence response exceeded the 2 MiB size limit"
                    )
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise DocumentationError(
                self._sanitize("Could not read the Confluence response")
            ) from exc
        return b"".join(chunks)

    def _parse_json(self, resp: httpx.Response, body: bytes) -> dict:
        if not body.strip():
            return {}
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DocumentationError(
                self._sanitize(_response_detail(resp, body))
            ) from exc
        if not isinstance(data, dict):
            raise DocumentationError(
                self._sanitize("Confluence returned an unexpected response shape")
            )
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | list | None = None,
        params: dict | None = None,
    ) -> dict:
        """The one guarded HTTP path every call goes through.

        Relative paths only (the base URL is the validated site), redirects
        refused, 2 MiB streamed body cap, 20 s timeout, sanitized errors.
        """
        if not path.startswith("/wiki/"):
            raise DocumentationError("refusing a non-Confluence request path")
        try:
            async with self._client.stream(
                method,
                path,
                json=json_body,
                params=params,
                headers=self._auth_headers(with_body=json_body is not None),
            ) as resp:
                body = await self._read_body(resp)
        except httpx.HTTPError as exc:
            raise DocumentationError(
                self._sanitize(f"Could not reach Confluence Cloud at {self.site_url}")
            ) from exc

        if 300 <= resp.status_code < 400:
            raise DocumentationError(
                self._sanitize(
                    "Confluence returned a redirect; site_url must be the canonical "
                    "https://<site>.atlassian.net host"
                )
            )
        if resp.status_code in (401, 403):
            raise DocumentationError(
                self._sanitize(
                    "Confluence authentication failed — check the account email "
                    "and API token"
                ),
                status_code=resp.status_code,
            )
        if resp.status_code == 404:
            raise DocumentationNotFound(
                self._sanitize(_response_detail(resp, body)), status_code=404
            )
        if resp.status_code >= 400:
            raise DocumentationError(
                self._sanitize(_response_detail(resp, body)),
                status_code=resp.status_code,
            )
        return self._parse_json(resp, body)

    async def validate_connection(self) -> dict:
        """Probe the credentials with v1 ``GET /wiki/rest/api/user/current``."""
        data = await self._request("GET", "/wiki/rest/api/user/current")
        if not data.get("accountId"):
            raise DocumentationError(
                "Confluence did not identify the account; check the email and API token"
            )
        return {
            "account_id": str(data.get("accountId")),
            "display_name": data.get("displayName") or data.get("publicName"),
            "email": data.get("email"),
        }

    async def get_space(self, space_key: str) -> DocSpace:
        """Resolve a space key to its numeric id + homepage id.

        v2 space paths take the numeric id, so every space-scoped call starts
        here (``GET /wiki/api/v2/spaces?keys=KEY``).
        """
        if not _SPACE_KEY_RE.match(space_key or ""):
            raise DocumentationError("invalid Confluence space key")
        data = await self._request(
            "GET", "/wiki/api/v2/spaces", params={"keys": space_key, "limit": 1}
        )
        values = data.get("results", [])
        if isinstance(values, list):
            for s in values:
                if isinstance(s, dict) and s.get("key") == space_key and s.get("id"):
                    home = s.get("homepageId")
                    return DocSpace(
                        key=str(s["key"]),
                        name=str(s.get("name") or ""),
                        id=str(s["id"]),
                        homepage_id=str(home) if home else None,
                    )
        raise DocumentationError(
            f"Confluence space {space_key} was not found or is not visible to this account"
        )

    async def search_spaces(self, query: str | None, limit: int) -> DocSpacePage:
        """Spaces whose key or name contains ``query`` (case-insensitive).

        v2 has no text filter for spaces and CQL cannot search spaces, so
        this lists spaces (cursor pages of 250) and filters locally, scanning
        at most ``_SPACE_SCAN_PAGES`` pages. ``has_more`` is true when more
        matches exist or the scan stopped before the end of the listing.
        """
        limit = max(1, min(int(limit), _SEARCH_MAX_LIMIT))
        needle = (query or "").strip().lower()
        matched: list[DocSpace] = []
        cursor: str | None = None
        exhausted = False
        for _ in range(_SPACE_SCAN_PAGES):
            params: dict[str, str | int] = {
                "limit": _SPACE_PAGE_SIZE,
                "status": "current",
                "sort": "name",
            }
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/wiki/api/v2/spaces", params=params)
            values = data.get("results", [])
            if not isinstance(values, list):
                values = []
            for s in values:
                if not (isinstance(s, dict) and s.get("key")):
                    continue
                key = str(s["key"])
                name = str(s.get("name") or "")
                if needle and needle not in key.lower() and needle not in name.lower():
                    continue
                matched.append(
                    DocSpace(
                        key=key,
                        name=name,
                        id=str(s.get("id") or ""),
                        homepage_id=str(s["homepageId"]) if s.get("homepageId") else None,
                    )
                )
            cursor = _next_cursor(data)
            if cursor is None:
                exhausted = True
                break
            if len(matched) > limit:
                break
        has_more = len(matched) > limit or not exhausted
        return DocSpacePage(spaces=matched[:limit], has_more=has_more)

    async def search_pages(
        self, space_key: str, query: str | None, limit: int
    ) -> list[DocChildPage]:
        """Pages in ``space_key`` for the parent picker.

        With a query: CQL ``type = page AND space = KEY AND title ~ "q"``
        through v1 search (v2's ``title`` filter is exact-match only). Without
        one: v2 ``GET /spaces/{id}/pages``.
        """
        limit = max(1, min(int(limit), _SEARCH_MAX_LIMIT))
        if not _SPACE_KEY_RE.match(space_key or ""):
            raise DocumentationError("invalid Confluence space key")
        pages: list[DocChildPage] = []
        if query:
            cql = (
                f"type = page AND space = {_cql_string(space_key)} "
                f"AND title ~ {_cql_string(query)}"
            )
            data = await self._request(
                "GET", "/wiki/rest/api/search", params={"cql": cql, "limit": limit}
            )
            values = data.get("results", [])
            if not isinstance(values, list):
                values = []
            for raw in values:
                content = raw.get("content") if isinstance(raw, dict) else None
                if isinstance(content, dict) and content.get("id"):
                    pages.append(
                        DocChildPage(
                            id=str(content["id"]), title=str(content.get("title") or "")
                        )
                    )
            return pages[:limit]

        space = await self.get_space(space_key)
        data = await self._request(
            "GET",
            f"/wiki/api/v2/spaces/{_require_numeric_id(space.id, 'space id')}/pages",
            params={"limit": limit, "status": "current"},
        )
        values = data.get("results", [])
        if not isinstance(values, list):
            values = []
        for p in values:
            if isinstance(p, dict) and p.get("id"):
                pages.append(DocChildPage(id=str(p["id"]), title=str(p.get("title") or "")))
        return pages[:limit]

    async def get_page(self, page_id: str, *, include_body: bool = False) -> DocPage:
        """``GET /wiki/api/v2/pages/{id}`` — version, space, parent, author."""
        pid = _require_numeric_id(page_id, "page id")
        params: dict[str, str] = {}
        if include_body:
            params["body-format"] = "storage"
        data = await self._request(
            "GET", f"/wiki/api/v2/pages/{pid}", params=params or None
        )
        return _page_from_v2(data, fallback_id=pid)

    async def create_page(
        self,
        space_id: str,
        parent_page_id: str,
        title: str,
        storage: str,
    ) -> DocPage:
        """``POST /wiki/api/v2/pages`` under ``parent_page_id`` in ``space_id``.

        v2 takes the numeric ``spaceId``; the create body cannot carry labels
        or properties, so the caller labels the page with ``add_label`` next.
        """
        body = {
            "spaceId": _require_numeric_id(space_id, "space id"),
            "status": "current",
            "title": title,
            "parentId": _require_numeric_id(parent_page_id, "parent page id"),
            "body": {"representation": "storage", "value": storage},
        }
        data = await self._request("POST", "/wiki/api/v2/pages", json_body=body)
        page = _page_from_v2(data, fallback_title=title)
        if not page.id:
            raise DocumentationError("Confluence did not return the new page id")
        return page

    async def add_label(self, page_id: str, label: str) -> None:
        """Add a global label (v1 ``POST /wiki/rest/api/content/{id}/label``)."""
        pid = _require_numeric_id(page_id, "page id")
        await self._request(
            "POST",
            f"/wiki/rest/api/content/{pid}/label",
            json_body=[{"prefix": "global", "name": label}],
        )

    async def update_page(
        self,
        page_id: str,
        current_version: int,
        title: str,
        storage: str,
    ) -> DocPage:
        """``PUT /wiki/api/v2/pages/{id}``; sends ``version.number = current + 1``."""
        pid = _require_numeric_id(page_id, "page id")
        body = {
            "id": pid,
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": storage},
            "version": {"number": int(current_version) + 1, "message": "SessionFS sync"},
        }
        data = await self._request("PUT", f"/wiki/api/v2/pages/{pid}", json_body=body)
        page = _page_from_v2(data, fallback_id=pid, fallback_title=title)
        if _version_number(data) is None:
            page.version = int(current_version) + 1
        return page

    async def find_page_by_label(self, space_key: str, label: str) -> str | None:
        """Id of the page in ``space_key`` carrying ``label``, or None.

        CQL ``type = page AND space = "KEY" AND label = "<label>"``.
        """
        if not _SPACE_KEY_RE.match(space_key or ""):
            raise DocumentationError("invalid Confluence space key")
        cql = (
            f"type = page AND space = {_cql_string(space_key)} "
            f"AND label = {_cql_string(label)}"
        )
        data = await self._request(
            "GET", "/wiki/rest/api/search", params={"cql": cql, "limit": 2}
        )
        results = data.get("results", [])
        if not isinstance(results, list):
            return None
        for raw in results:
            content = raw.get("content") if isinstance(raw, dict) else None
            if isinstance(content, dict) and content.get("id"):
                return str(content["id"])
        return None

    async def find_page_by_title(self, space_id: str, title: str) -> str | None:
        """Id of the current page titled exactly ``title`` in ``space_id``."""
        data = await self._request(
            "GET",
            "/wiki/api/v2/pages",
            params={
                "space-id": _require_numeric_id(space_id, "space id"),
                "title": title,
                "status": "current",
                "limit": 2,
            },
        )
        results = data.get("results", [])
        if not isinstance(results, list):
            return None
        for raw in results:
            if isinstance(raw, dict) and raw.get("id"):
                return str(raw["id"])
        return None


# C0 control characters (plus DEL) that must never be echoed back from a
# remote error body — a raw gateway page can smuggle them into logs / JSON.
_CONTROL_STRIP_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_controls(text: str) -> str:
    return _CONTROL_STRIP_RE.sub("", text)


def _response_detail(resp: httpx.Response, body: bytes) -> str:
    """Extract a short, safe reason from a Confluence error response body.

    Only structured ``message`` values are surfaced, stripped of control
    characters and truncated to 300 chars. A non-JSON body (e.g. an HTML
    gateway error page) is never echoed verbatim.
    """
    detail = ""
    try:
        data = json.loads(body)
    except Exception:
        data = None
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str):
            detail = _strip_controls(message)[:300]
    if not detail:
        return f"Confluence API {resp.status_code}: the request was rejected"
    return f"Confluence API {resp.status_code}: {detail}"
