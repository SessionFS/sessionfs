"""Unit tests for the Jira Cloud client + Markdown→ADF converter.

Every transport path uses ``httpx.MockTransport`` — the client never makes a
real network call. Focus: SSRF allowlist, Basic-auth header shape, the ADF
converter, and error sanitization (the token and request headers must never
escape in a ``TrackerError``).
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from sessionfs.server.integrations.trackers.base import TrackerError
from sessionfs.server.integrations.trackers.jira import (
    JiraClient,
    _response_detail,
    markdown_to_adf,
    sessionfs_label,
    validate_site_url,
)

_SITE = "https://acme.atlassian.net"
_EMAIL = "jira@acme.com"
_TOKEN = "super-secret-api-token"


# ── site URL allowlist ──


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://acme.atlassian.net", "https://acme.atlassian.net"),
        ("https://acme.atlassian.net/", "https://acme.atlassian.net"),
        ("https://acme123-hyphen.atlassian.net", "https://acme123-hyphen.atlassian.net"),
    ],
)
def test_validate_site_url_accepts_cloud_hosts(url, expected):
    assert validate_site_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "http://acme.atlassian.net",  # scheme
        "https://ACME.atlassian.net",  # uppercase host
        "https://acme.atlassian.net/path",  # path
        "https://acme.atlassian.net:443",  # port
        "https://user@acme.atlassian.net",  # userinfo
        "https://acme.example.com",  # wrong domain
        "https://acme.atlassian.net.evil.com",  # suffix spoof
    ],
)
def test_validate_site_url_rejects(url):
    with pytest.raises(TrackerError):
        validate_site_url(url)


# ── auth header ──


def test_auth_headers_are_basic_and_never_plaintext():
    client = JiraClient(_SITE, _EMAIL, _TOKEN)
    headers = client._auth_headers(with_body=False)
    assert headers["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(headers["Authorization"][6:]).decode()
    assert decoded == f"{_EMAIL}:{_TOKEN}"
    assert "Content-Type" not in headers
    assert headers["Accept"] == "application/json"
    # The raw token never appears in any header value.
    assert _TOKEN not in headers["Authorization"]
    for value in headers.values():
        assert _TOKEN not in value


def test_auth_headers_add_content_type_for_body():
    client = JiraClient(_SITE, _EMAIL, _TOKEN)
    headers = client._auth_headers(with_body=True)
    assert headers["Content-Type"] == "application/json"


def _client(handler) -> JiraClient:
    return JiraClient(
        _SITE, _EMAIL, _TOKEN, transport=httpx.MockTransport(handler)
    )


# ── ADF conversion ──


def _blocks(text: str) -> list[dict]:
    return markdown_to_adf(text)["content"]


def test_adf_doc_shape():
    doc = markdown_to_adf("hello")
    assert doc["type"] == "doc"
    assert doc["version"] == 1
    assert isinstance(doc["content"], list)


def test_adf_heading():
    (block,) = _blocks("# Title")
    assert block["type"] == "heading"
    assert block["attrs"]["level"] == 1
    assert block["content"][0]["text"] == "Title"


def test_adf_paragraph_joins_wrapped_lines():
    (block,) = _blocks("line one\nline two")
    assert block["type"] == "paragraph"
    assert block["content"][0]["text"] == "line one line two"


def test_adf_bullet_list():
    (block,) = _blocks("- a\n- b")
    assert block["type"] == "bulletList"
    texts = [
        item["content"][0]["content"][0]["text"]
        for item in block["content"]
    ]
    assert texts == ["a", "b"]


def test_adf_numbered_list():
    (block,) = _blocks("1. a\n2. b")
    assert block["type"] == "orderedList"
    texts = [
        item["content"][0]["content"][0]["text"]
        for item in block["content"]
    ]
    assert texts == ["a", "b"]


def test_adf_multiline_list_item_stays_one_item():
    # Indented continuation lines belong to their list item, not a new paragraph.
    (block,) = _blocks("- first line\n  second line\n  third line\n- next")
    assert block["type"] == "bulletList"
    assert len(block["content"]) == 2
    content = block["content"][0]["content"][0]["content"]
    texts = [n["text"] for n in content if n["type"] == "text"]
    assert texts == ["first line", "second line", "third line"]
    assert block["content"][1]["content"][0]["content"][0]["text"] == "next"


def test_adf_fenced_code_block():
    (block,) = _blocks("```\nprint('x')\n```")
    assert block["type"] == "codeBlock"
    assert block["content"][0]["text"] == "print('x')"


def test_adf_inline_marks():
    (block,) = _blocks("**bold** *italic* `code` [link](https://x.y)")
    nodes = block["content"]
    marks = {tuple(m["type"] for m in n.get("marks", [])): n["text"] for n in nodes}
    assert ("strong",) in marks and marks[("strong",)] == "bold"
    assert ("em",) in marks and marks[("em",)] == "italic"
    assert ("code",) in marks and marks[("code",)] == "code"
    assert ("link",) in marks and marks[("link",)] == "link"


def test_adf_empty_text():
    assert markdown_to_adf("")["content"] == []
    assert markdown_to_adf(None)["content"] == []  # type: ignore[arg-type]


def test_unclosed_link_does_not_backtrack_exponentially():
    """An unclosed ``[x](aaaa…`` used to take seconds per extra 4 chars."""
    import time

    start = time.monotonic()
    markdown_to_adf("[x](" + "a" * 5000 + " ")
    markdown_to_adf("[" * 10000)
    assert time.monotonic() - start < 2.0


def test_link_url_with_parenthesised_segment_still_parses():
    nodes = markdown_to_adf("see [img](https://e.com/foo(1).png) ok")["content"][0]["content"]
    links = [n for n in nodes if n.get("marks")]
    assert links[0]["marks"][0]["attrs"]["href"] == "https://e.com/foo(1).png"


# ── error sanitization ──


@pytest.mark.asyncio
async def test_validate_connection_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/rest/api/3/myself"
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json={"accountId": "a1", "displayName": "N", "emailAddress": "e"})

    client = _client(handler)
    assert await client.validate_connection() == {
        "account_id": "a1", "display_name": "N", "email": "e",
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_401_scrubs_token_and_reports_auth_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorMessages": [f"token {_TOKEN} rejected"]})

    client = _client(handler)
    with pytest.raises(TrackerError) as exc:
        await client.validate_connection()
    await client.aclose()
    assert "authentication failed" in str(exc.value)
    assert _TOKEN not in str(exc.value)


@pytest.mark.asyncio
async def test_400_error_messages_are_returned_but_scrubbed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errorMessages": ["bad field", f"leak {_TOKEN}"]})

    client = _client(handler)
    with pytest.raises(TrackerError) as exc:
        await client.validate_connection()
    await client.aclose()
    assert "bad field" in str(exc.value)
    assert _TOKEN not in str(exc.value)
    assert "Jira API 400" in str(exc.value)


def test_response_detail_never_echoes_raw_html():
    """A non-JSON body (HTML gateway page) is reduced to a generic message."""
    resp = httpx.Response(502, headers={"content-type": "text/html"})
    body = b"<html><body><h1>502 Bad Gateway</h1>nginx</body></html>"
    detail = _response_detail(resp, body)
    assert detail == "Jira API 502: the request was rejected"
    assert "<html" not in detail
    assert "nginx" not in detail


def test_response_detail_strips_controls_and_truncates_to_300():
    resp = httpx.Response(400)
    body = json.dumps({"errorMessages": ["bad\x00field\x1f" + "x" * 400]}).encode()
    detail = _response_detail(resp, body)
    assert "\x00" not in detail
    assert "\x1f" not in detail
    assert detail.startswith("Jira API 400: badfield")
    # message capped at 300 chars after the fixed "Jira API 400: " prefix
    assert len(detail) == len("Jira API 400: ") + 300


def test_response_detail_uses_errors_dict_values():
    resp = httpx.Response(400)
    body = json.dumps({"errors": {"summary": ["too long"], "project": "missing"}}).encode()
    detail = _response_detail(resp, body)
    assert "summary: too long" in detail
    assert "project: missing" in detail
    assert detail.startswith("Jira API 400: ")


@pytest.mark.asyncio
async def test_redirect_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://elsewhere.example.com"})

    client = _client(handler)
    with pytest.raises(TrackerError) as exc:
        await client.validate_connection()
    await client.aclose()
    assert "redirect" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_network_error_is_scrubbed_and_mentions_site():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client(handler)
    with pytest.raises(TrackerError) as exc:
        await client.validate_connection()
    await client.aclose()
    assert "Could not reach Jira Cloud" in str(exc.value)
    assert _TOKEN not in str(exc.value)


# ── issue CRUD shapes ──


@pytest.mark.asyncio
async def test_create_issue_posts_adf_and_returns_browse_url():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"key": "ACME-1"})

    client = _client(handler)
    issue = await client.create_issue("ACME", "Story", "Title", {"type": "doc", "version": 1, "content": []})
    await client.aclose()
    assert issue.key == "ACME-1"
    assert issue.url == f"{_SITE}/browse/ACME-1"
    fields = captured["body"]["fields"]
    assert fields["summary"] == "Title"
    assert fields["project"] == {"key": "ACME"}
    assert fields["issuetype"] == {"name": "Story"}
    assert fields["description"]["type"] == "doc"


def test_sessionfs_label_sanitises_ticket_ids():
    assert sessionfs_label("tk_abc123") == "sessionfs-tk_abc123"
    assert sessionfs_label("weird id with spaces!") == "sessionfs-weird-id-with-spaces"
    assert sessionfs_label("") == "sessionfs-ticket"


@pytest.mark.asyncio
async def test_create_issue_stamps_labels_when_provided():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"key": "ACME-1"})

    client = _client(handler)
    await client.create_issue(
        "ACME", "Story", "Title", {"type": "doc", "version": 1, "content": []},
        labels=["sessionfs-tk_abc123"],
    )
    await client.aclose()
    assert captured["body"]["fields"]["labels"] == ["sessionfs-tk_abc123"]


@pytest.mark.asyncio
async def test_create_issue_omits_labels_when_none():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"key": "ACME-1"})

    client = _client(handler)
    await client.create_issue("ACME", "Story", "Title", {"type": "doc", "version": 1, "content": []})
    await client.aclose()
    assert "labels" not in captured["body"]["fields"]


@pytest.mark.asyncio
async def test_search_issue_by_label_returns_found_issue():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["jql"] == 'project = "ACME" AND labels = "sessionfs-tk_x"'
        assert request.url.params["maxResults"] == "1"
        return httpx.Response(200, json={
            "issues": [{"key": "ACME-42", "id": "10042"}],
        })

    client = _client(handler)
    issue = await client.search_issue_by_label("ACME", "sessionfs-tk_x")
    await client.aclose()
    assert issue is not None
    assert issue.key == "ACME-42"
    assert issue.url == f"{_SITE}/browse/ACME-42"


@pytest.mark.asyncio
async def test_search_issue_by_label_returns_none_with_no_match():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issues": []})

    client = _client(handler)
    issue = await client.search_issue_by_label("ACME", "sessionfs-tk_x")
    await client.aclose()
    assert issue is None


@pytest.mark.asyncio
async def test_search_issue_by_label_rejects_malformed_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issues": "not-a-list"})

    client = _client(handler)
    with pytest.raises(TrackerError) as exc:
        await client.search_issue_by_label("ACME", "sessionfs-tk_x")
    await client.aclose()
    assert "unexpected search response shape" in str(exc.value)


@pytest.mark.asyncio
async def test_update_issue_never_sends_status():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(204)

    client = _client(handler)
    issue = await client.update_issue("ACME-1", "New title", {"type": "doc", "version": 1, "content": []})
    await client.aclose()
    assert issue.key == "ACME-1"
    fields = captured["body"]["fields"]
    assert "status" not in fields
    assert fields["summary"] == "New title"
    assert "project" not in fields  # update never re-targets the project


@pytest.mark.asyncio
async def test_get_issue_reads_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fields"] == "status,updated"
        return httpx.Response(200, json={
            "key": "ACME-1",
            "fields": {"status": {"name": "In Progress"}, "updated": "2026-01-01T00:00:00Z"},
        })

    client = _client(handler)
    issue = await client.get_issue("ACME-1")
    await client.aclose()
    assert issue.status == "In Progress"
    assert issue.key == "ACME-1"
    assert issue.url == f"{_SITE}/browse/ACME-1"


@pytest.mark.asyncio
async def test_get_issue_tolerates_non_dict_status():
    # A malformed or partially-filled remote issue can carry a bare string
    # status; the client must yield ``status=None`` rather than crash on ``.get``.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "key": "ACME-1",
            "fields": {"status": "In Progress", "updated": "2026-01-01T00:00:00Z"},
        })

    client = _client(handler)
    issue = await client.get_issue("ACME-1")
    await client.aclose()
    assert issue.status is None
    assert issue.key == "ACME-1"


# ── link URL scheme + ``)`` handling ──


def test_adf_link_url_with_parens_is_not_truncated():
    (block,) = _blocks("[docs](https://example.com/foo(1).png)")
    node = block["content"][0]
    assert node["marks"] == [{"type": "link", "attrs": {"href": "https://example.com/foo(1).png"}}]


def test_adf_unsafe_link_scheme_drops_mark():
    (block,) = _blocks("[x](javascript:alert(1))")
    node = block["content"][0]
    assert node["text"] == "x"
    assert "marks" not in node


def test_adf_mailto_link_is_allowed():
    (block,) = _blocks("[mail](mailto:a@b.co)")
    node = block["content"][0]
    assert node["marks"][0]["attrs"]["href"] == "mailto:a@b.co"


def test_adf_empty_fenced_code_block_has_no_empty_text_node():
    (block,) = _blocks("```\n```")
    assert block["type"] == "codeBlock"
    assert block["content"][0]["text"] != ""


# ── issue key validation ──


@pytest.mark.asyncio
async def test_invalid_issue_key_raises_before_request():
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("no request should be made for an invalid issue key")

    client = _client(handler)
    with pytest.raises(TrackerError):
        await client.get_issue("../../etc/passwd")
    await client.aclose()


@pytest.mark.asyncio
async def test_update_issue_validates_and_uses_key():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(204)

    client = _client(handler)
    await client.update_issue("ACME-1", "t", {"type": "doc", "version": 1, "content": []})
    await client.aclose()
    assert seen == ["/rest/api/3/issue/ACME-1"]


# ── response shape + size guards ──


@pytest.mark.asyncio
async def test_non_json_2xx_raises_tracker_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    client = _client(handler)
    with pytest.raises(TrackerError):
        await client.validate_connection()
    await client.aclose()


@pytest.mark.asyncio
async def test_json_list_2xx_raises_tracker_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    client = _client(handler)
    with pytest.raises(TrackerError):
        await client.validate_connection()
    await client.aclose()


@pytest.mark.asyncio
async def test_oversized_response_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))

    client = _client(handler)
    with pytest.raises(TrackerError):
        await client.validate_connection()
    await client.aclose()


class _CountingStream(httpx.AsyncByteStream):
    """Yields fixed-size chunks, counting each one as it is handed out."""

    def __init__(self, chunk: bytes, total: int, counter: dict) -> None:
        self._chunk = chunk
        self._total = total
        self._counter = counter

    async def __aiter__(self):
        for _ in range(self._total):
            self._counter["yielded"] += 1
            yield self._chunk


class _StreamingTransport(httpx.AsyncBaseTransport):
    def __init__(self, stream: httpx.AsyncByteStream) -> None:
        self._stream = stream

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=self._stream)


@pytest.mark.asyncio
async def test_oversized_streaming_body_is_capped_while_reading():
    counter: dict = {"yielded": 0}
    chunk = b"x" * (1024 * 1024)  # 1 MiB
    transport = _StreamingTransport(_CountingStream(chunk, total=10, counter=counter))
    client = JiraClient(_SITE, _EMAIL, _TOKEN, transport=transport)
    with pytest.raises(TrackerError):
        await client.validate_connection()
    await client.aclose()
    # The 2 MiB cap fires the moment the third chunk pushes the total over —
    # only 3 of the 10 chunks are read, proving the body is not buffered up
    # front (the old non-streaming path would have consumed all 10).
    assert counter["yielded"] == 3


# ── project pagination + lazy issue types ──


@pytest.mark.asyncio
async def test_list_projects_follows_pagination_and_skips_issue_types():
    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("startAt", "0"))
        if start == 0:
            return httpx.Response(200, json={
                "startAt": 0, "maxResults": 50, "total": 2, "isLast": False,
                "values": [{"key": "A", "name": "Alpha"}],
            })
        return httpx.Response(200, json={
            "startAt": 1, "maxResults": 50, "total": 2, "isLast": True,
            "values": [{"key": "B", "name": "Beta"}],
        })

    client = _client(handler)
    projects = await client.list_projects()
    await client.aclose()
    assert [p.key for p in projects] == ["A", "B"]
    assert all(p.issue_types == [] for p in projects)
