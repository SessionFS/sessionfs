"""Jira Cloud REST v3 client + a small Markdown → ADF converter.

The site URL is the SSRF control: it must be ``https://<site>.atlassian.net``
(lowercase alphanumerics and hyphens only), redirects are never followed,
and every request times out after 20 s. Errors are sanitized before they
leave this module — the API token and request headers are never logged or
returned.
"""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote

import httpx

from sessionfs.server.integrations.trackers.base import (
    TrackerError,
    TrackerIssue,
    TrackerIssueType,
    TrackerProject,
    TrackerProjectPage,
)

# Strict host allowlist: only Jira Cloud sites, no ports, no paths, no
# userinfo, no redirects. This is also the anti-SSRF boundary.
_SITE_URL_RE = re.compile(r"^https://[a-z0-9-]+\.atlassian\.net$")

# Jira project keys are all-uppercase project keys (``PROJ-123``); the numeric
# part is the issue sequence. Keys are validated before being interpolated into
# any URL path so a malformed remote key can never escape the browse link.
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")

# Only safe URL schemes may become ADF link marks; ``javascript:``/``data:``/
# anything else is emitted as plain text instead of a clickable link.
_LINK_SCHEME_RE = re.compile(r"^(?:https?|mailto):", re.IGNORECASE)

_TIMEOUT = 20.0

# ``project/search`` returns at most 50 projects per page; listing without a
# search query stops after ``_LIST_PROJECTS_CAP`` projects and reports that it
# was cut short instead of paging through a huge site.
_PROJECT_PAGE_SIZE = 50
_LIST_PROJECTS_CAP = 500
_MAX_PROJECT_PAGES = _LIST_PROJECTS_CAP // _PROJECT_PAGE_SIZE + 1

# Cap response bodies so a hostile or misbehaving Jira response cannot balloon
# memory; anything larger is a TrackerError.
_MAX_BODY_BYTES = 2 * 1024 * 1024


def validate_issue_key(issue_key: str) -> str:
    """Return the validated issue key, or raise ``TrackerError``.

    A Jira issue key must look like ``PROJ-123``. The client validates every
    key it receives before using it in a request path or a browse URL.
    """
    key = (issue_key or "").strip()
    if not _ISSUE_KEY_RE.match(key):
        raise TrackerError(f"invalid Jira issue key: {issue_key!r}")
    return key


def validate_site_url(site_url: str) -> str:
    """Return the normalized site URL, or raise ``TrackerError``."""
    url = (site_url or "").strip().rstrip("/")
    if not _SITE_URL_RE.match(url):
        raise TrackerError(
            "site_url must be a Jira Cloud URL like https://<site>.atlassian.net"
        )
    return url


def sessionfs_label(ticket_id: str) -> str:
    """Build the ``sessionfs-<ticket_id>`` label stamped on a created issue.

    Jira labels cannot contain spaces; ticket ids are already ``tk_<hex>`` but
    any other character is collapsed to ``-`` defensively so a weird id can
    never produce an invalid label. The label is the search key used to adopt a
    timed-out (but actually created) issue on the next publish.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", (ticket_id or "").strip())
    safe = safe.strip("-") or "ticket"
    return f"sessionfs-{safe}"


# ── Markdown → Atlassian Document Format ──


_INLINE_RE = re.compile(
    # The link URL is a run of either single non-paren characters or single
    # ``(...)`` groups, so ``https://example.com/foo(1).png`` is not truncated
    # at the first ``)``.  Each alternative consumes a fixed shape (never a
    # nested ``+`` inside ``+``) so an unclosed ``[x](aaaa…`` fails in linear
    # time instead of backtracking exponentially; the link text is bounded so
    # a line of many ``[`` cannot go quadratic.
    r"\[(?P<link_text>[^\]\n]{1,500})\]\((?P<link_url>(?:[^()\s]|\([^()\s]*\))+)\)"
    r"|\*\*(?P<bold>[^*]+)\*\*"
    r"|\*(?P<italic>[^*]+)\*"
    r"|`(?P<code>[^`]+)`"
)

_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\d+[.)]\s+(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^```")


def _inline(text: str) -> list[dict]:
    """Parse a single line's inline marks into ADF text nodes."""
    nodes: list[dict] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            nodes.append({"type": "text", "text": text[pos:m.start()]})
        if m.group("link_text") is not None:
            href = m.group("link_url")
            if _LINK_SCHEME_RE.match(href):
                nodes.append(
                    {
                        "type": "text",
                        "text": m.group("link_text"),
                        "marks": [{"type": "link", "attrs": {"href": href}}],
                    }
                )
            else:
                # Disallowed scheme (e.g. ``javascript:``) — keep the visible
                # text but drop the link mark entirely.
                nodes.append({"type": "text", "text": m.group("link_text")})
        elif m.group("bold") is not None:
            nodes.append(
                {"type": "text", "text": m.group("bold"), "marks": [{"type": "strong"}]}
            )
        elif m.group("italic") is not None:
            nodes.append(
                {"type": "text", "text": m.group("italic"), "marks": [{"type": "em"}]}
            )
        elif m.group("code") is not None:
            nodes.append(
                {"type": "text", "text": m.group("code"), "marks": [{"type": "code"}]}
            )
        pos = m.end()
    if pos < len(text):
        nodes.append({"type": "text", "text": text[pos:]})
    if not nodes:
        nodes.append({"type": "text", "text": ""})
    return nodes


def _inline_multiline(text: str) -> list[dict]:
    """Parse inline marks across multiple lines into ADF text nodes.

    Each line's inline marks are parsed separately and joined with a
    ``hardBreak`` node, so a multi-line list item keeps its full text without an
    invalid ``\\n`` inside an ADF text node.
    """
    nodes: list[dict] = []
    for idx, line in enumerate(text.split("\n")):
        if idx:
            nodes.append({"type": "hardBreak"})
        nodes.extend(_inline(line))
    return nodes


def _is_block_start(line: str) -> bool:
    s = line.strip()
    return bool(
        not s
        or _FENCE_RE.match(s)
        or _HEADING_RE.match(s)
        or _BULLET_RE.match(s)
        or _NUMBERED_RE.match(s)
    )


def markdown_to_adf(text: str) -> dict:
    """Convert a Markdown string into an Atlassian Document Format ``doc``.

    Supports ATX headings, paragraphs, bullet/numbered lists, fenced code
    blocks, bold, italic, inline code and links. Unknown constructs fall back
    to plain paragraph text.
    """
    lines = (text or "").splitlines()
    blocks: list[dict] = []
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
            blocks.append(
                {
                    "type": "codeBlock",
                    # ADF text nodes must be non-empty; an empty fenced block
                    # emits a single space instead of an invalid empty node.
                    "content": [{"type": "text", "text": "\n".join(code_lines) or " "}],
                }
            )
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": len(heading.group(1))},
                    "content": _inline(heading.group(2)),
                }
            )
            i += 1
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
                # Indented non-blank lines continue the current list item so a
                # multi-line criterion stays one item rather than leaking out.
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
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": _inline_multiline(item)}
                            ],
                        }
                        for item in items
                    ],
                }
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
                {
                    "type": "orderedList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": _inline_multiline(item)}
                            ],
                        }
                        for item in items
                    ],
                }
            )
            continue

        para_lines = [stripped]
        i += 1
        while i < n and not _is_block_start(lines[i]):
            para_lines.append(lines[i].strip())
            i += 1
        blocks.append(
            {"type": "paragraph", "content": _inline(" ".join(para_lines))}
        )

    return {"type": "doc", "version": 1, "content": blocks}


def adf_to_text(adf: dict) -> str:
    """Flatten an ADF ``doc``/node into plain text (loose inverse of
    ``markdown_to_adf``).

    Jira comments arrive as ADF. This keeps the readable text for the mirrored
    ticket comment: paragraphs/headings become lines, list items are bulleted,
    ``hardBreak`` becomes a newline, and ``mention``/``emoji``/``inlineCard``
    render their display text. Unknown nodes just yield their ``text`` node.
    """
    if not isinstance(adf, dict):
        return ""

    def inline(node: dict) -> str:
        ntype = node.get("type")
        content = node.get("content")
        if ntype == "text":
            return str(node.get("text", ""))
        if ntype == "hardBreak":
            return "\n"
        if ntype == "mention":
            attrs = node.get("attrs") or {}
            return str(attrs.get("text") or attrs.get("id") or "")
        if ntype == "emoji":
            attrs = node.get("attrs") or {}
            return str(attrs.get("text") or attrs.get("shortName") or "")
        if ntype == "inlineCard":
            attrs = node.get("attrs") or {}
            return str(attrs.get("url") or "")
        if isinstance(content, list):
            return "".join(inline(c) for c in content if isinstance(c, dict))
        return ""

    lines: list[str] = []

    def blocks(node: dict) -> None:
        content = node.get("content")
        if not isinstance(content, list):
            return
        for child in content:
            if not isinstance(child, dict):
                continue
            ctype = child.get("type")
            if ctype in {"paragraph", "heading"}:
                lines.append(inline(child).strip())
            elif ctype in {"bulletList", "orderedList"}:
                for item in child.get("content") or []:
                    if isinstance(item, dict):
                        lines.append("- " + inline(item).strip())
            elif ctype == "codeBlock":
                lines.append(inline(child).strip())
            else:
                text = inline(child).strip()
                if text:
                    lines.append(text)

    blocks(adf)
    return "\n".join(lines).strip()


# ── Jira Cloud client ──

# Comments read when looking for a SessionFS outbox reference before posting.
_MARKER_LOOKUP_PAGE = 20


class JiraClient:
    """Async Jira Cloud REST v3 client using Atlassian API-token Basic auth.

    Instances are async context managers; close with ``await client.aclose()``
    or ``async with``. A ``transport`` may be injected for tests (never a real
    Jira call).
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

    async def __aenter__(self) -> "JiraClient":
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

    def _browse_url(self, key: str) -> str:
        return f"{self.site_url}/browse/{validate_issue_key(key)}"

    def _sanitize(self, message: str) -> str:
        """Scrub any trace of the credential before the message escapes."""
        out = message
        if self.api_token:
            out = out.replace(self.api_token, "[REDACTED]")
        return out[:300]

    async def _read_body(self, resp: httpx.Response) -> bytes:
        """Stream-read the body, refusing anything over ``_MAX_BODY_BYTES``."""
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_BODY_BYTES:
                    raise TrackerError(
                        "Jira response exceeded the 2 MiB size limit"
                    )
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise TrackerError(
                self._sanitize("Could not read the Jira response")
            ) from exc
        return b"".join(chunks)

    def _parse_json(self, resp: httpx.Response, body: bytes) -> dict:
        """Parse a 2xx body as a JSON object.

        An empty body (e.g. a 204 No Content) is treated as ``{}``. A body
        that is not JSON, or parses to a non-object, is a sanitized
        ``TrackerError`` rather than a half-typed result.
        """
        if not body.strip():
            return {}
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise TrackerError(
                self._sanitize(_response_detail(resp, body))
            ) from exc
        if not isinstance(data, dict):
            raise TrackerError(
                self._sanitize("Jira returned an unexpected response shape")
            )
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        # ``stream()`` keeps the response body on the wire while ``_read_body``
        # enforces the 2 MiB cap, instead of httpx buffering the whole body up
        # front (the default ``stream=False`` path). A hostile/oversized Jira
        # response is rejected as it arrives, not after it is fully buffered.
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
            raise TrackerError(
                self._sanitize(f"Could not reach Jira Cloud at {self.site_url}")
            ) from exc

        if 300 <= resp.status_code < 400:
            raise TrackerError(
                self._sanitize(
                    "Jira returned a redirect; site_url must be the canonical "
                    "https://<site>.atlassian.net host"
                )
            )
        if resp.status_code in (401, 403):
            raise TrackerError(
                self._sanitize(
                    "Jira authentication failed — check the account email "
                    "and API token"
                )
            )
        if resp.status_code >= 400:
            raise TrackerError(self._sanitize(_response_detail(resp, body)))
        return self._parse_json(resp, body)

    async def validate_connection(self) -> dict:
        data = await self._request("GET", "/rest/api/3/myself")
        return {
            "account_id": data.get("accountId"),
            "display_name": data.get("displayName"),
            "email": data.get("emailAddress"),
        }

    async def myself_account_id(self) -> str:
        """The connection's own Atlassian ``accountId`` (from ``/myself``)."""
        data = await self._request("GET", "/rest/api/3/myself")
        account_id = data.get("accountId") if isinstance(data, dict) else None
        return str(account_id) if account_id else ""

    async def list_projects(self) -> list[TrackerProject]:
        """The first ``_LIST_PROJECTS_CAP`` projects the credentials can see.

        Kept for callers that want a plain list; ``search_projects`` also
        reports whether the cap cut the list short.
        """
        page = await self.search_projects(None, _LIST_PROJECTS_CAP)
        return page.projects

    async def search_projects(
        self, query: str | None, limit: int
    ) -> TrackerProjectPage:
        """Up to ``limit`` projects, filtered by Jira when ``query`` is set.

        ``project/search`` matches ``query`` case-insensitively against the
        project key and name on Jira's side, so a site with thousands of
        projects never has to be listed in full. Atlassian caps
        ``maxResults`` at 50, so this pages via ``startAt``/``isLast`` until
        ``limit`` projects are collected. ``has_more`` is true when Jira has
        more matches than were returned. Issue types are intentionally left
        empty — callers fetch them lazily via ``list_issue_types``.
        """
        limit = max(1, min(int(limit), _LIST_PROJECTS_CAP))
        projects: list[TrackerProject] = []
        start_at = 0
        # Ends as False only when Jira reports the last page. Running out of
        # page budget (e.g. pages full of malformed entries) counts as more.
        has_more = True
        for _ in range(_MAX_PROJECT_PAGES):
            params: dict[str, str | int] = {
                "startAt": start_at,
                "maxResults": min(_PROJECT_PAGE_SIZE, limit - len(projects)),
            }
            if query:
                params["query"] = query
            data = await self._request(
                "GET", "/rest/api/3/project/search", params=params
            )
            values = data.get("values", [])
            if not isinstance(values, list):
                values = []
            for p in values:
                if isinstance(p, dict) and p.get("key"):
                    projects.append(
                        TrackerProject(
                            key=str(p["key"]),
                            name=str(p.get("name") or ""),
                            issue_types=[],
                        )
                    )
            if data.get("isLast") is True or not values:
                has_more = False
                break
            start_at += len(values)
            if len(projects) >= limit:
                break
        if len(projects) > limit:
            projects = projects[:limit]
            has_more = True
        return TrackerProjectPage(projects=projects, has_more=has_more)

    async def list_issue_types(self, project_key: str) -> list[TrackerIssueType]:
        # ``GET /rest/api/3/issue/createmeta/{projectIdOrKey}/issuetypes`` is
        # the v3 endpoint for the issue types creatable in a project; it
        # returns ``{"values": [{"id", "name", ...}]}``. The broader
        # ``/rest/api/3/issuetype/project?projectId=`` lists every type on the
        # project, but createmeta matches the "creatable" semantics we want.
        path = f"/rest/api/3/issue/createmeta/{quote(project_key)}/issuetypes"
        data = await self._request("GET", path)
        values = data.get("values", []) if isinstance(data, dict) else []
        if not isinstance(values, list):
            values = []
        return [
            TrackerIssueType(name=it.get("name", ""), id=it.get("id"))
            for it in values
            if isinstance(it, dict) and it.get("name")
        ]

    async def create_issue(
        self,
        project_key: str,
        issue_type: str,
        summary: str,
        description_adf: dict,
        *,
        labels: list[str] | None = None,
    ) -> TrackerIssue:
        fields: dict = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
            "description": description_adf,
        }
        if labels:
            fields["labels"] = labels
        body = {"fields": fields}
        data = await self._request("POST", "/rest/api/3/issue", json_body=body)
        key = validate_issue_key(data.get("key", ""))
        return TrackerIssue(
            key=key,
            url=self._browse_url(key),
            status="",
            id=str(data.get("id")) if data.get("id") is not None else None,
        )

    async def search_issue_by_label(
        self, project_key: str, label: str
    ) -> TrackerIssue | None:
        """Find the issue in ``project_key`` bearing ``label``, or None.

        Used to adopt an issue that a timed-out create actually produced: the
        create stamps ``sessionfs-<ticket_id>`` as a label, and the next publish
        searches by it so a retry adopts the existing issue instead of creating
        a duplicate. The response shape is validated — a non-list ``issues``
        field is a sanitized ``TrackerError`` rather than a half-typed result.
        """
        jql = f'project = "{project_key}" AND labels = "{label}"'
        data = await self._request(
            "GET",
            "/rest/api/3/search/jql",
            params={
                "jql": jql,
                "fields": "key,id,project,creator,reporter,created",
                "maxResults": 1,
            },
        )
        issues = data.get("issues") if isinstance(data, dict) else None
        if not isinstance(issues, list):
            raise TrackerError(
                self._sanitize("Jira returned an unexpected search response shape")
            )
        for raw in issues:
            if not isinstance(raw, dict):
                continue
            key = raw.get("key")
            if not isinstance(key, str) or not _ISSUE_KEY_RE.match(key.strip()):
                continue
            key = key.strip()
            raw_fields = raw.get("fields")
            fields = raw_fields if isinstance(raw_fields, dict) else {}
            project = fields.get("project")
            creator = fields.get("creator")
            reporter = fields.get("reporter")
            project_key_value = project.get("key") if isinstance(project, dict) else None
            created_at = fields.get("created")
            return TrackerIssue(
                key=key,
                url=self._browse_url(key),
                status="",
                id=str(raw["id"]) if raw.get("id") is not None else None,
                project_key=project_key_value if isinstance(project_key_value, str) else None,
                creator_account_id=(
                    creator.get("accountId")
                    if isinstance(creator, dict) and isinstance(creator.get("accountId"), str)
                    else None
                ),
                reporter_account_id=(
                    reporter.get("accountId")
                    if isinstance(reporter, dict) and isinstance(reporter.get("accountId"), str)
                    else None
                ),
                created_at=created_at if isinstance(created_at, str) else None,
            )
        return None

    async def update_issue(
        self,
        issue_key: str,
        summary: str,
        description_adf: dict,
    ) -> TrackerIssue:
        key = validate_issue_key(issue_key)
        body = {
            "fields": {
                "summary": summary,
                "description": description_adf,
            }
        }
        await self._request(
            "PUT", f"/rest/api/3/issue/{quote(key)}", json_body=body
        )
        return TrackerIssue(key=key, url=self._browse_url(key), status="")

    async def get_issue(self, issue_key: str) -> TrackerIssue:
        key = validate_issue_key(issue_key)
        data = await self._request(
            "GET",
            f"/rest/api/3/issue/{quote(key)}",
            params={"fields": "status,updated"},
        )
        fields = data.get("fields") if isinstance(data, dict) else {}
        status: str | None = None
        updated: str | None = None
        if isinstance(fields, dict):
            raw_status = fields.get("status")
            # A non-dict/missing status (a malformed or partially-filled issue)
            # yields None rather than crashing on ``.get``.
            status = raw_status.get("name", "") if isinstance(raw_status, dict) else None
            updated = fields.get("updated")
        raw_key = data.get("key", key) if isinstance(data, dict) else key
        if _ISSUE_KEY_RE.match(raw_key or ""):
            key = raw_key
        return TrackerIssue(
            key=key,
            url=self._browse_url(key),
            status=status,
            updated_at=updated,
            id=str(data.get("id")) if data.get("id") is not None else None,
        )

    async def find_transition(self, issue_key: str, transition_name: str) -> dict | None:
        """The transition named ``transition_name`` (exact, then
        case-insensitive) that the issue's workflow offers right now, or None.

        Returns ``{"id", "name", "to_name", "to_category"}``: ``to_*`` is the
        status the transition leads to (Jira's transitions API includes it), so
        a caller can tell whether the issue already reached the target even
        when the transition and status names differ (e.g. "Resolve" →
        "Resolved").
        """
        key = validate_issue_key(issue_key)
        name = (transition_name or "").strip()
        if not name:
            raise TrackerError("a non-empty transition name is required")
        data = await self._request(
            "GET", f"/rest/api/3/issue/{quote(key)}/transitions"
        )
        transitions = data.get("transitions", []) if isinstance(data, dict) else []
        if not isinstance(transitions, list):
            transitions = []
        candidates = [t for t in transitions if isinstance(t, dict) and t.get("id") is not None]
        chosen = next((t for t in candidates if t.get("name") == name), None)
        if chosen is None:
            chosen = next(
                (t for t in candidates if str(t.get("name", "")).lower() == name.lower()),
                None,
            )
        if chosen is None:
            return None
        raw_to = chosen.get("to")
        to: dict = raw_to if isinstance(raw_to, dict) else {}
        raw_category = to.get("statusCategory")
        category: dict = raw_category if isinstance(raw_category, dict) else {}
        to_name = to.get("name")
        to_category = category.get("key")
        return {
            "id": str(chosen["id"]),
            "name": chosen.get("name"),
            "to_name": to_name if isinstance(to_name, str) and to_name else None,
            "to_category": to_category if isinstance(to_category, str) and to_category else None,
        }

    async def apply_transition(self, issue_key: str, transition_id: str) -> None:
        """POST one transition by id."""
        key = validate_issue_key(issue_key)
        await self._request(
            "POST",
            f"/rest/api/3/issue/{quote(key)}/transitions",
            json_body={"transition": {"id": transition_id}},
        )

    async def transition_issue(self, issue_key: str, transition_name: str) -> dict:
        """Drive ``issue_key`` through the Jira workflow transition named
        ``transition_name`` (by display name, matched case-insensitively).

        Returns the transition dict that was applied. Raises ``TrackerError``
        when the transition is not available on the issue (its workflow
        doesn't offer that name) — the two-way sync records that as a distinct
        error event rather than forcing a different transition.
        """
        key = validate_issue_key(issue_key)
        chosen = await self.find_transition(key, transition_name)
        if chosen is None:
            raise TrackerError(
                f"no transition named {transition_name.strip()!r} is available for issue {key}"
            )
        await self.apply_transition(key, chosen["id"])
        return chosen

    async def find_comment_with_marker(self, issue_key: str, marker: str) -> str | None:
        """Id of a recent comment on ``issue_key`` whose text contains
        ``marker``, or None. Reads one small page of the newest comments —
        enough to recognise a post SessionFS itself made moments ago, and small
        enough that an issue full of long evidence comments stays well under
        the response cap."""
        key = validate_issue_key(issue_key)
        if not marker:
            return None
        data = await self._request(
            "GET",
            f"/rest/api/3/issue/{quote(key)}/comment",
            params={"orderBy": "-created", "maxResults": str(_MARKER_LOOKUP_PAGE)},
        )
        comments = data.get("comments") if isinstance(data, dict) else None
        if not isinstance(comments, list):
            return None
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            text = adf_to_text(body) if isinstance(body, dict) else str(body or "")
            if marker in text and comment.get("id") is not None:
                return str(comment["id"])
        return None

    async def add_comment(self, issue_key: str, adf_body: dict) -> str:
        """Post an ADF comment to ``issue_key``; returns the new comment id."""
        key = validate_issue_key(issue_key)
        data = await self._request(
            "POST",
            f"/rest/api/3/issue/{quote(key)}/comment",
            json_body={"body": adf_body},
        )
        comment_id = data.get("id") if isinstance(data, dict) else None
        return str(comment_id) if comment_id is not None else ""


# C0 control characters (plus DEL) that must never be echoed back from a
# remote error body — a raw gateway page can smuggle them into logs / JSON.
_CONTROL_STRIP_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_controls(text: str) -> str:
    return _CONTROL_STRIP_RE.sub("", text)


def _error_value_text(value: object) -> str:
    """Flatten a Jira ``errors`` value (often a list) into a single string."""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item) for item in value)
    return str(value)


def _response_detail(resp: httpx.Response, body: bytes) -> str:
    """Extract a short, safe reason from a Jira error response body.

    Only structured ``errorMessages`` / ``errors`` values are surfaced, each
    stripped of control characters and truncated to 300 chars.  A non-JSON
    body (e.g. an HTML gateway error page) is never echoed verbatim.
    """
    detail = ""
    try:
        data = json.loads(body)
    except Exception:
        data = None
    if isinstance(data, dict):
        messages = data.get("errorMessages")
        if isinstance(messages, list):
            cleaned = [
                _strip_controls(str(m))[:300] for m in messages[:3] if m is not None
            ]
            detail = "; ".join(m for m in cleaned if m)
        if not detail and isinstance(data.get("errors"), dict):
            errors = data.get("errors") or {}
            cleaned = [
                f"{_strip_controls(str(k))}: {_strip_controls(_error_value_text(v))[:300]}"
                for k, v in list(errors.items())[:3]
            ]
            detail = "; ".join(cleaned)
    if not detail:
        return f"Jira API {resp.status_code}: the request was rejected"
    return f"Jira API {resp.status_code}: {_strip_controls(detail)[:300]}"
