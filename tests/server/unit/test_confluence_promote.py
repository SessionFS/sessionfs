"""Unit tests for the Confluence page builder (``integrations/docs/promote``).

Focus: the storage renderer is the injection boundary — section headings,
paragraph text, link text and the CDATA split must never let raw storage
markup or a macro through; DLP redaction of the final sections; dropped
paragraph reporting; and the source hash the sweep uses to skip unchanged
pages.
"""

from __future__ import annotations

import json

import pytest

from sessionfs.server.db.models import Organization
from sessionfs.server.integrations.docs.promote import (
    DocSources,
    _extract_rewrites,
    redact_sections_for_publish,
    sections_to_storage,
    source_hash,
)
from sessionfs.server.integrations.docs.templates import get_template

_GH_TOKEN = "ghp_" + "a" * 36

_HOSTILE = [
    '<ac:structured-macro ac:name="html"><ac:plain-text-body>x</ac:plain-text-body></ac:structured-macro>',
    "]]><script>alert(1)</script>",
    "A & B < C > D",
    '<ri:page ri:content-title="x"/>',
]


def _sections(heading: str, text: str) -> list[dict]:
    return [{"key": "overview", "heading": heading, "paragraphs": [{"text": text, "citations": []}]}]


@pytest.mark.parametrize("hostile", _HOSTILE)
def test_section_heading_is_escaped(hostile):
    out = sections_to_storage(_sections(hostile, "body"), None)
    assert "<ac:" not in out
    assert "<ri:" not in out
    assert "<script>" not in out
    assert out.startswith("<h2>")
    assert "&lt;" in out or "&amp;" in out


@pytest.mark.parametrize("hostile", _HOSTILE)
def test_paragraph_heading_and_link_text_are_escaped(hostile):
    for text in (f"## {hostile}", f"[{hostile}](https://a.b)", hostile, f"| {hostile} |"):
        out = sections_to_storage(_sections("H", text), None)
        assert "<ac:" not in out, text
        assert "<ri:" not in out, text
        assert "<script>" not in out, text


def test_ampersand_is_escaped_once():
    out = sections_to_storage(_sections("Q&A", "Tom & Jerry"), None)
    assert "<h2>Q&amp;A</h2>" in out
    assert "Tom &amp; Jerry" in out


def test_cdata_closer_in_code_is_split():
    out = sections_to_storage(_sections("H", "```\nend ]]> more\n```"), None)
    assert "]]]]><![CDATA[>" in out
    assert "]]> more" not in out.replace("]]]]><![CDATA[>", "")


def test_backlink_text_escaped_even_when_linked():
    out = sections_to_storage(
        _sections("H", "see [[a&b]]"),
        {"a&b": "https://acme.atlassian.net/wiki/spaces/DEV/pages/1?x=1&y=2"}.get,
    )
    assert '<a href="https://acme.atlassian.net/wiki/spaces/DEV/pages/1?x=1&amp;y=2">a&amp;b</a>' in out


def test_footer_link_built_through_converter_is_escaped():
    from sessionfs.server.integrations.docs.confluence import markdown_to_storage
    from sessionfs.server.routes.confluence import _footer_text, _view_source_url

    url = _view_source_url("https://app.example", "proj_1", "wiki_page", 'a"b<c>')
    footer = markdown_to_storage(
        _footer_text(
            template_name="<b>Ref</b>",
            version=None,
            claims_current=1,
            claims_aging=2,
            claims_stale=0,
            last_compile_ts=None,
            view_url=url,
        )
    )
    assert "<b>" not in footer
    assert "Shaped by 3 active claims (1 current, 2 aging, 0 stale)" in footer
    assert 'a"b' not in footer


def test_redact_sections_for_publish_applies_secret_redaction():
    org = Organization(id="org_x", name="X", slug="x", tier="enterprise", settings=json.dumps({}))
    out = redact_sections_for_publish(
        _sections(f"key {_GH_TOKEN}", f"token {_GH_TOKEN} here"), org
    )
    assert _GH_TOKEN not in json.dumps(out)
    assert "[REDACTED:github_token]" in out[0]["paragraphs"][0]["text"]
    assert "[REDACTED:github_token]" in out[0]["heading"]


def test_extract_rewrites_reports_dropped_paragraphs():
    template = get_template("reference")
    assert template is not None
    sources = DocSources(
        page_body="body", sections=[], claims_by_type={}, session_evidence=[],
        compile_metadata="", backlinks=[],
    )
    dropped: list[dict] = []
    rewrites = _extract_rewrites(
        {
            "sections": [
                {
                    "key": "overview",
                    "paragraphs": [
                        {"text": "kept", "citations": ["page_body"]},
                        {"text": "uncited", "citations": []},
                        {"text": "bogus", "citations": ["session_evidence"]},
                    ],
                }
            ]
        },
        template,
        sources,
        dropped,
    )
    assert [p.text for p in rewrites["overview"]] == ["kept"]
    assert dropped == [
        {"section": "overview", "text": "uncited", "reason": "no_citation"},
        {"section": "overview", "text": "bogus", "reason": "unresolvable_citation"},
    ]


def test_source_hash_changes_with_source_and_template_only():
    s1 = DocSources("body", [], {}, [], "", [])
    s2 = DocSources("body2", [], {}, [], "", [])
    assert source_hash("T", s1, "reference") == source_hash("T", s1, "reference")
    assert source_hash("T", s1, "reference") != source_hash("T", s2, "reference")
    assert source_hash("T", s1, "reference") != source_hash("T", s1, "runbook")
    assert source_hash("T", s1, "reference") != source_hash("U", s1, "reference")
