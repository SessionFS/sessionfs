"""Tests for the static SessionFS docs index the session helper searches.

The committed ``docs_index.json`` is built by
``python -m sessionfs.server.helper.build_docs_index`` (a package module, so it
ships to public main and in the wheel); these tests guard against it drifting
from ``site/src/content/docs`` and pin the search/lookup behaviour the
``search_sessionfs_docs`` / ``get_sessionfs_doc`` tools rely on.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from sessionfs.server.helper import build_docs_index as builder
from sessionfs.server.helper.docs import (
    _GET_MAX_CHARS,
    DocsIndex,
    _stem,
    get_docs_index,
)

DOCS_ROOT = builder.find_docs_root()
INDEX_PATH = builder.OUT_PATH


@pytest.mark.skipif(
    DOCS_ROOT is None,
    reason="site/src/content/docs is not present (installed package, not a checkout)",
)
def test_committed_index_is_not_stale(tmp_path: Path) -> None:
    """Rebuilding from the docs must reproduce the committed index exactly."""
    assert DOCS_ROOT is not None
    out = tmp_path / "docs_index.json"
    builder.build_index(DOCS_ROOT, out)
    assert out.read_text(encoding="utf-8") == INDEX_PATH.read_text(encoding="utf-8"), (
        "docs_index.json is out of date with site/src/content/docs — run "
        "`python -m sessionfs.server.helper.build_docs_index` and commit the result"
    )


def test_index_loads_and_search_jira_ranks_integrations_first() -> None:
    index = get_docs_index()
    assert isinstance(index, DocsIndex)
    results = index.search("jira", 3)
    assert results, "expected at least one hit for 'jira'"
    assert results[0]["slug"] == "integrations/jira"


def test_search_install_ranks_installation_first() -> None:
    results = get_docs_index().search("install", 3)
    assert results, "expected at least one hit for 'install'"
    assert results[0]["slug"] == "installation"


def test_get_sessionfs_doc_not_found_suggests_close_slugs() -> None:
    index = get_docs_index()
    assert index.get("jirra") is None
    suggestions = index.close_slugs("jirra", 10)
    assert "integrations/jira" in suggestions


def test_resolve_citation_validates_slug_and_anchor() -> None:
    index = get_docs_index()
    resolved = index.resolve_citation("integrations/jira", "setup")
    assert resolved is not None
    assert resolved["slug"] == "integrations/jira"
    assert resolved["anchor"] == "setup"
    assert resolved["url"] == "https://sessionfs.dev/integrations/jira/#setup"
    # Unknown anchor and unknown slug are rejected.
    assert index.resolve_citation("integrations/jira", "no-such-anchor") is None
    assert index.resolve_citation("no-such-page", None) is None


def test_build_index_strips_mdx_components_imports_and_keeps_code(tmp_path: Path) -> None:
    """MDX flattens to plain text: frontmatter/imports/components dropped, code kept."""
    page = tmp_path / "test.mdx"
    page.write_text(
        "\n".join(
            [
                "---",
                'title: "Test Page"',
                "description: ignored",
                "---",
                "",
                "import { Card } from '@astrojs/starlight/components';",
                "",
                "Intro paragraph.",
                "",
                "## Setup",
                "",
                '<Aside type="tip">',
                "Keep this text.",
                "</Aside>",
                "",
                "### Code sample",
                "",
                "```bash",
                "sfs init --token <NAME>",
                "```",
                "",
                "## Notes",
                "",
                "**Bold** stays and `upload_timeout` keeps its underscore.",
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "index.json"
    builder.build_index(tmp_path, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["pages"]) == 1
    built = data["pages"][0]
    assert built["slug"] == "test"
    assert built["title"] == "Test Page"
    assert built["url"] == "https://sessionfs.dev/test/"

    sections = {s.get("heading"): s["text"] for s in built["sections"]}
    assert sections[None] == "Intro paragraph."
    assert sections["Setup"] == "Keep this text."
    assert sections["Code sample"] == "sfs init --token <NAME>"
    assert sections["Notes"] == "Bold stays and upload_timeout keeps its underscore."


def _build_one(tmp_path: Path, lines: list[str]) -> dict:
    page = tmp_path / "docs" / "page.mdx"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("\n".join(["---", "title: Page", "---", "", *lines]), encoding="utf-8")
    out = tmp_path / "index.json"
    builder.build_index(page.parent, out)
    return json.loads(out.read_text(encoding="utf-8"))["pages"][0]


def test_duplicate_headings_get_numbered_anchors_across_levels(tmp_path: Path) -> None:
    """github-slugger/Starlight numbering: every heading level counts."""
    built = _build_one(
        tmp_path,
        [
            "## Setup",
            "one",
            "#### Commands",
            "deep heading is counted",
            "### Commands",
            "two",
            "## Setup",
            "three",
            "```bash",
            "## not a heading inside code",
            "```",
            "### Commands",
            "four",
        ],
    )
    anchors = [s["anchor"] for s in built["sections"]]
    assert anchors == ["setup", "commands-1", "setup-1", "commands-2"]


def test_real_pages_number_their_second_duplicate_heading() -> None:
    index = get_docs_index()
    cli = [s.anchor for s in index.by_slug["cli"] if s.heading == "Commands"]
    assert cli[:2] == ["commands", "commands-1"]
    git = [s.anchor for s in index.by_slug["git-integration"] if s.heading == "Setup"]
    assert git[:2] == ["setup", "setup-1"]


def test_mdx_titles_entities_and_table_separators(tmp_path: Path) -> None:
    built = _build_one(
        tmp_path,
        [
            "## Cards",
            '<Card title="Quick Start" icon="rocket">',
            "Install it.",
            "</Card>",
            '<Aside type="caution" title="`--force` takes ownership">',
            "Careful &rarr; really &amp; truly.",
            "</Aside>",
            "| Flag | Meaning |",
            "| --- | :---: |",
            "| `-v` | verbose |",
            "```bash",
            "echo a &amp;&amp; b",
            "```",
        ],
    )
    text = built["sections"][0]["text"]
    assert text.startswith("Quick Start Install it. --force takes ownership Careful → really & truly.")
    assert "---" not in text
    assert "| Flag | Meaning | | -v | verbose |" in text
    # Code blocks are verbatim: entities there are literal text.
    assert "echo a &amp;&amp; b" in text


def test_stemmer_strips_es_only_after_sibilants() -> None:
    assert _stem("files") == _stem("file")
    assert _stem("boxes") == _stem("box")
    assert _stem("matches") == _stem("match")
    assert _stem("sessions") == _stem("session")


def test_stop_words_dropped_judge_question_finds_judge_page() -> None:
    results = get_docs_index().search("what does the judge do", 3)
    assert results and results[0]["slug"] == "judge"


def test_hand_off_question_ranks_handoff_page_first() -> None:
    results = get_docs_index().search("how do I hand off a session", 3)
    assert results and results[0]["slug"] == "handoff"


def test_huge_query_is_capped_and_fast() -> None:
    index = get_docs_index()
    query = " ".join(f"word{i} session" for i in range(20_000))
    assert len(index.query_terms(query)) <= 32
    started = time.monotonic()
    index.search(query, 5)
    assert time.monotonic() - started < 5.0


def test_search_scores_only_sections_containing_a_term() -> None:
    index = get_docs_index()
    assert index.search("zzzqqqnotaword", 5) == []
    stem = _stem("jira")
    for section_id in index.postings[stem]:
        assert stem in index.sections[section_id].tokens


def test_anchor_fetch_joins_every_part_of_a_long_section() -> None:
    index = get_docs_index()
    split = next(
        (s for s in index.sections if s.part == 2 and s.anchor),
        None,
    )
    if split is None:
        pytest.skip("no multi-part section in the current docs")
    parts = [s for s in index.by_slug[split.slug] if s.anchor == split.anchor]
    doc = index.get(split.slug, split.anchor)
    assert doc is not None
    joined = " ".join(s.text for s in parts)
    assert doc["text"] == joined[:_GET_MAX_CHARS]
    assert len(doc["text"]) > len(parts[0].text) or len(parts[0].text) >= _GET_MAX_CHARS


def test_page_fetch_over_cap_lists_section_anchors() -> None:
    index = get_docs_index()
    doc = index.get("cli")
    assert doc is not None
    assert len(doc["text"]) <= _GET_MAX_CHARS
    assert "…truncated; sections: " in doc["text"]
    tail = doc["text"].rsplit("…truncated; sections: ", 1)[1]
    assert "commands-1" in tail.split(", ")
    # A short page is returned whole, without the marker.
    short = index.get("telemetry")
    assert short is not None and "truncated; sections" not in short["text"]


def test_plural_and_e_forms_share_a_stem():
    from sessionfs.server.helper.docs import _stem

    for a, b in [("releases", "release"), ("caches", "cache"), ("licenses", "license"), ("files", "file"), ("boxes", "box")]:
        assert _stem(a) == _stem(b), (a, b)


def test_every_slug_and_anchor_is_citable():
    """Citations only accept this charset, so the build must never emit anything else."""
    import json
    import re
    from pathlib import Path

    import sessionfs.server.helper as helper_pkg

    index = json.loads((Path(helper_pkg.__file__).parent / "docs_index.json").read_text())
    slug_re = re.compile(r"[a-z0-9][a-z0-9/_-]*")
    anchor_re = re.compile(r"[a-z0-9_-]+")
    pages = index["pages"] if isinstance(index, dict) else index
    for page in pages:
        assert slug_re.fullmatch(page["slug"]) or page["slug"] == "", page["slug"]
        for section in page.get("sections", []):
            anchor = section.get("anchor") or ""
            assert anchor == "" or anchor_re.fullmatch(anchor), (page["slug"], anchor)
