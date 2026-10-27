"""Unit tests for the Confluence Cloud client + Markdown → storage converter.

Every transport path uses ``httpx.MockTransport`` — the client never makes a
real network call. Focus: SSRF allowlist, Basic-auth header shape, the strict
storage-format converter (code-block CDATA, link/backlink resolution, no raw
markup injection), and error sanitization (the token must never escape in a
``DocumentationError``).
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from sessionfs.server.integrations.docs.base import DocumentationError
from sessionfs.server.integrations.docs.confluence import (
    ConfluenceClient,
    markdown_to_storage,
    sessionfs_label,
    storage_text_length,
    storage_to_text,
    validate_site_url,
)

_SITE = "https://acme.atlassian.net"
_EMAIL = "docs@acme.com"
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
    with pytest.raises(DocumentationError):
        validate_site_url(url)


# ── adoption label ──


def test_sessionfs_label_is_lowercase_label_safe_and_distinct():
    label = sessionfs_label("prj/With space", "context_section", "C4 & D")
    assert label.startswith("sessionfs-prj-with-space-context_section-c4-d-")
    assert label == label.lower()
    assert all(c.isalnum() or c in "-_" for c in label)
    assert len(label) <= 255
    # Two sources that normalize alike still get distinct labels.
    assert sessionfs_label("p", "wiki_page", "Home") != sessionfs_label("p", "wiki_page", "home")
    # Deterministic.
    assert sessionfs_label("p", "wiki_page", "a") == sessionfs_label("p", "wiki_page", "a")
    assert len(sessionfs_label("p" * 64, "wiki_page", "k" * 100)) <= 255


# ── auth header ──


def test_auth_headers_are_basic_and_never_plaintext():
    client = ConfluenceClient(_SITE, _EMAIL, _TOKEN)
    headers = client._auth_headers(with_body=False)
    assert headers["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(headers["Authorization"][6:]).decode()
    assert decoded == f"{_EMAIL}:{_TOKEN}"
    assert "Content-Type" not in headers
    assert headers["Accept"] == "application/json"
    assert _TOKEN not in headers["Authorization"]
    for value in headers.values():
        assert _TOKEN not in value


def test_auth_headers_add_content_type_for_body():
    client = ConfluenceClient(_SITE, _EMAIL, _TOKEN)
    assert client._auth_headers(with_body=True)["Content-Type"] == "application/json"


# ── storage-format converter ──


def test_heading():
    assert markdown_to_storage("# Title") == "<h1>Title</h1>"


def test_paragraph_joins_wrapped_lines():
    assert markdown_to_storage("line one\nline two") == "<p>line one line two</p>"


def test_emphasis_and_inline_code():
    assert markdown_to_storage("**bold** and *em* and `x`") == (
        "<p><strong>bold</strong> and <em>em</em> and <code>x</code></p>"
    )


def test_bullet_and_numbered_lists():
    assert markdown_to_storage("- a\n- b") == "<ul><li>a</li><li>b</li></ul>"
    assert markdown_to_storage("1. a\n2. b") == "<ol><li>a</li><li>b</li></ol>"


def test_fenced_code_block_uses_structured_macro_and_cdata():
    out = markdown_to_storage("```python\nx = 1\n```")
    assert '<ac:structured-macro ac:name="code"' in out
    assert "<ac:plain-text-body><![CDATA[x = 1]]></ac:plain-text-body>" in out


def test_code_block_escapes_cdata_closer():
    out = markdown_to_storage("```\n]]>\n```")
    assert "]]]]><![CDATA[>" in out


def test_table():
    out = markdown_to_storage("| a | b |\n|---|---|\n| 1 | 2 |")
    assert out == "<table><tbody><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></tbody></table>"


def test_table_separator_check_is_linear_on_long_pipe_runs():
    """A long run of pipes after a table row used to backtrack quadratically
    (seconds per line) and stall the event loop during a publish."""
    import time

    hostile = "|a|b|\n" + "|" * 20000 + "x\n" + "| " * 20000 + "x"
    started = time.monotonic()
    markdown_to_storage(hostile)
    assert time.monotonic() - started < 1.0
    assert "<tr><td>a</td><td>b</td></tr>" in markdown_to_storage("|a|b|\n| :-- | --: |\n|1|2|")


def test_link_allowlist_only_http_mailto():
    assert markdown_to_storage("[x](https://a.b)") == '<p><a href="https://a.b">x</a></p>'
    # A non-http scheme is emitted as plain text, never a clickable link.
    assert markdown_to_storage("[x](javascript:alert(1))") == "<p>x</p>"


def test_backlink_resolves_to_link_or_plain_text():
    resolver = {"guide": "https://acme.atlassian.net/wiki/spaces/DEV/pages/1"}
    assert markdown_to_storage("see [[guide]]", backlink_resolver=resolver.get) == (
        '<p>see <a href="https://acme.atlassian.net/wiki/spaces/DEV/pages/1">guide</a></p>'
    )
    assert markdown_to_storage("see [[missing]]", backlink_resolver=resolver.get) == (
        "<p>see missing</p>"
    )


def test_raw_markup_is_escaped_not_injected():
    out = markdown_to_storage("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_storage_text_length():
    assert storage_text_length("<p>hi</p>") == len("<p>hi</p>")


# ── escaping regressions (no markup / macro injection) ──


@pytest.mark.parametrize(
    "hostile",
    [
        "<ac:structured-macro ac:name=\"html\"><ac:plain-text-body>x</ac:plain-text-body></ac:structured-macro>",
        "]]><script>alert(1)</script>",
        "Tom & Jerry <b>bold</b>",
    ],
)
def test_hostile_text_in_headings_links_and_tables_is_escaped(hostile):
    for md in (f"# {hostile}", f"[{hostile}](https://a.b)", f"| {hostile} | x |", f"- {hostile}"):
        out = markdown_to_storage(md)
        assert "<ac:" not in out, md
        assert "<script>" not in out, md
        assert "<b>" not in out, md
        assert "]]><" not in out, md


def test_code_block_cdata_split_keeps_body_inside_cdata():
    out = markdown_to_storage("```\na]]>b]]>c\n```")
    assert out.count("<![CDATA[") == 3
    # Every closer is immediately reopened except the final one.
    assert "]]]]><![CDATA[>" in out
    assert out.endswith("]]></ac:plain-text-body></ac:structured-macro>")


def test_link_href_quotes_are_escaped():
    out = markdown_to_storage('[x](https://a.b/"onmouseover=alert(1))')
    assert 'onmouseover' not in out or "&quot;" in out
    assert '"onmouseover' not in out


def test_storage_to_text_strips_markup_and_keeps_cdata():
    text = storage_to_text(
        "<h2>T &amp; U</h2><p>one</p><ac:structured-macro ac:name=\"code\">"
        "<ac:plain-text-body><![CDATA[x < 1]]></ac:plain-text-body></ac:structured-macro>"
    )
    assert text.splitlines() == ["T & U", "one", "x < 1"]


# ── client methods against a mock transport ──


def _client(handler) -> ConfluenceClient:
    return ConfluenceClient(
        _SITE, _EMAIL, _TOKEN, transport=httpx.MockTransport(handler)
    )


def _ok(data) -> httpx.Response:
    return httpx.Response(200, json=data)


def _run(handler, fn):
    async def go():
        client = _client(handler)
        try:
            return await fn(client)
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_validate_connection_uses_v1_current_user():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/wiki/rest/api/user/current"
        return _ok({"accountId": "aid1", "displayName": "Docs Bot", "email": _EMAIL})

    who = _run(handler, lambda c: c.validate_connection())
    assert who == {"account_id": "aid1", "display_name": "Docs Bot", "email": _EMAIL}


def test_validate_connection_without_account_fails():
    with pytest.raises(DocumentationError):
        _run(lambda r: _ok({}), lambda c: c.validate_connection())


def test_get_space_resolves_key_to_numeric_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wiki/api/v2/spaces"
        assert request.url.params["keys"] == "DEV"
        return _ok({"results": [{"id": 65538, "key": "DEV", "name": "Dev", "homepageId": "1001"}]})

    space = _run(handler, lambda c: c.get_space("DEV"))
    assert (space.key, space.id, space.homepage_id) == ("DEV", "65538", "1001")


def test_get_space_missing_raises():
    with pytest.raises(DocumentationError):
        _run(lambda r: _ok({"results": []}), lambda c: c.get_space("DEV"))


def test_search_spaces_filters_locally_and_follows_cursor_param_only():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wiki/api/v2/spaces"
        assert request.url.host == "acme.atlassian.net"
        calls.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return _ok({
                "results": [{"id": 1, "key": "OPS", "name": "Operations"}],
                # An absolute next link on another host must never be followed.
                "_links": {"next": "https://evil.example.com/wiki/api/v2/spaces?cursor=abc"},
            })
        assert request.url.params["cursor"] == "abc"
        return _ok({"results": [{"id": 2, "key": "DEV", "name": "Developers"}], "_links": {}})

    page = _run(handler, lambda c: c.search_spaces("dev", 10))
    assert [s.key for s in page.spaces] == ["DEV"]
    assert page.has_more is False
    assert calls[0]["limit"] == "250"


def test_search_spaces_scan_is_bounded():
    count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        return _ok({
            "results": [{"id": count["n"], "key": f"S{count['n']}", "name": "x"}],
            "_links": {"next": f"/wiki/api/v2/spaces?cursor=c{count['n']}"},
        })

    page = _run(handler, lambda c: c.search_spaces(None, 100))
    assert count["n"] == 4
    assert page.has_more is True


def test_search_pages_with_query_uses_cql_title_search():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wiki/rest/api/search"
        assert request.url.params["cql"] == (
            'type = page AND space = "DEV" AND title ~ "Run \\"book\\""'
        )
        return _ok({"results": [{"content": {"id": "7", "title": "Runbook"}}]})

    pages = _run(handler, lambda c: c.search_pages("DEV", 'Run "book"', 10))
    assert [(p.id, p.title) for p in pages] == [("7", "Runbook")]


def test_search_pages_without_query_uses_space_id_path():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wiki/api/v2/spaces":
            return _ok({"results": [{"id": 65538, "key": "DEV", "name": "Dev"}]})
        assert request.url.path == "/wiki/api/v2/spaces/65538/pages"
        return _ok({"results": [{"id": "2001", "title": "Child"}]})

    pages = _run(handler, lambda c: c.search_pages("DEV", None, 10))
    assert [(p.id, p.title) for p in pages] == [("2001", "Child")]


def test_get_page_parses_v2_shape_and_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wiki/api/v2/pages/42"
        assert request.url.params["body-format"] == "storage"
        return _ok({
            "id": "42", "version": {"number": 3}, "title": "T", "spaceId": "65538",
            "parentId": "2001", "authorId": "aid1", "createdAt": "2026-10-01T00:00:00.000Z",
            "body": {"storage": {"value": "<p>x</p>", "representation": "storage"}},
        })

    page = _run(handler, lambda c: c.get_page("42", include_body=True))
    assert (page.id, page.version, page.space_id, page.parent_id, page.author_id) == (
        "42", 3, "65538", "2001", "aid1",
    )
    assert page.storage == "<p>x</p>"


def test_get_page_refuses_non_numeric_id_without_a_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected")

    with pytest.raises(DocumentationError):
        _run(handler, lambda c: c.get_page("../../admin"))


def test_create_page_sends_v2_body_with_numeric_space_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/wiki/api/v2/pages"
        seen["body"] = json.loads(request.content)
        return _ok({"id": "9", "version": {"number": 1}, "title": "P", "spaceId": "65538"})

    page = _run(handler, lambda c: c.create_page("65538", "2001", "P", "<p>x</p>"))
    assert page.id == "9"
    assert seen["body"] == {
        "spaceId": "65538",
        "status": "current",
        "title": "P",
        "parentId": "2001",
        "body": {"representation": "storage", "value": "<p>x</p>"},
    }


def test_add_label_posts_global_label_array():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/wiki/rest/api/content/9/label"
        seen["body"] = json.loads(request.content)
        return _ok({"results": [{"prefix": "global", "name": "sessionfs-x"}]})

    _run(handler, lambda c: c.add_label("9", "sessionfs-x"))
    assert seen["body"] == [{"prefix": "global", "name": "sessionfs-x"}]


def test_update_page_sends_current_plus_one():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/wiki/api/v2/pages/9"
        seen["body"] = json.loads(request.content)
        return _ok({"id": "9", "version": {"number": 4}, "title": "P"})

    page = _run(handler, lambda c: c.update_page("9", 3, "P", "<p>x</p>"))
    assert page.version == 4
    assert seen["body"]["id"] == "9"
    assert seen["body"]["status"] == "current"
    assert seen["body"]["version"]["number"] == 4
    assert seen["body"]["body"] == {"representation": "storage", "value": "<p>x</p>"}


def test_find_page_by_label_uses_label_cql():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wiki/rest/api/search"
        assert request.url.params["cql"] == (
            'type = page AND space = "DEV" AND label = "sessionfs-p-wiki_page-a-1234abcd"'
        )
        return _ok({"results": [{"content": {"id": "7", "title": "T"}}]})

    found = _run(handler, lambda c: c.find_page_by_label("DEV", "sessionfs-p-wiki_page-a-1234abcd"))
    assert found == "7"


def test_find_page_by_title_uses_v2_exact_title_filter():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/wiki/api/v2/pages"
        assert request.url.params["space-id"] == "65538"
        assert request.url.params["title"] == "Home"
        return _ok({"results": [{"id": "8"}]})

    assert _run(handler, lambda c: c.find_page_by_title("65538", "Home")) == "8"


def test_redirect_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example.com"})

    with pytest.raises(DocumentationError) as exc:
        _run(handler, lambda c: c.get_page("1"))
    assert _TOKEN not in str(exc.value)


def test_auth_failure_never_leaks_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": f"bad {_TOKEN}"})

    with pytest.raises(DocumentationError) as exc:
        _run(handler, lambda c: c.get_page("1"))
    assert _TOKEN not in str(exc.value)


def test_body_over_two_mib_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + b" " * (2 * 1024 * 1024 + 10) + b"}")

    with pytest.raises(DocumentationError) as exc:
        _run(handler, lambda c: c.get_page("1"))
    assert "2 MiB" in str(exc.value)


def test_timeout_becomes_sanitized_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out with {_TOKEN}", request=request)

    with pytest.raises(DocumentationError) as exc:
        _run(handler, lambda c: c.get_page("1"))
    assert _TOKEN not in str(exc.value)
    assert "Could not reach Confluence" in str(exc.value)


def test_client_timeout_is_twenty_seconds():
    client = ConfluenceClient(_SITE, _EMAIL, _TOKEN)
    try:
        assert client._client.timeout.read == 20.0
        assert client._client.follow_redirects is False
    finally:
        asyncio.run(client.aclose())


def test_non_json_error_body_is_never_echoed():
    secret_page = "<html><body>internal gateway 10.0.0.5 SECRET-STACK</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=secret_page.encode())

    with pytest.raises(DocumentationError) as exc:
        _run(handler, lambda c: c.get_page("1"))
    assert "SECRET-STACK" not in str(exc.value)
    assert "10.0.0.5" not in str(exc.value)
    assert str(exc.value) == "Confluence API 502: the request was rejected"


def test_non_json_success_body_is_never_echoed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>SECRET-STACK</html>")

    with pytest.raises(DocumentationError) as exc:
        _run(handler, lambda c: c.get_page("1"))
    assert "SECRET-STACK" not in str(exc.value)


def test_404_raises_not_found_subclass():
    from sessionfs.server.integrations.docs.base import DocumentationNotFound

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "No content found with id 9"})

    with pytest.raises(DocumentationNotFound):
        _run(handler, lambda c: c.get_page("9"))
