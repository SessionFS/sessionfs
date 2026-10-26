"""Build the static docs index the session helper searches.

Reads every ``.mdx`` page under the product site's docs directory
(``site/src/content/docs`` in a source checkout) and writes a deterministic
JSON index to ``docs_index.json`` next to this module.  The index is committed
and shipped in the wheel, so the helper can answer "how do I set up Jira?" from
the official docs without the site.  This module is only needed at build time;
the server reads the committed JSON through :mod:`sessionfs.server.helper.docs`
and never touches ``site/``.

Each page records its slug, frontmatter title and URL; the page body is split
into sections at ``##`` / ``###`` headings, each with a GitHub-style anchor
(duplicate headings on a page get ``-1``, ``-2`` … like github-slugger and
Starlight), a plain-text body (frontmatter, import/export lines, HTML comments,
MDX components, table separator rows and emphasis stripped; component titles
kept; HTML entities decoded; code blocks kept as text) and — when a section
exceeds ``SECTION_CAP`` characters — numbered parts.

Run from a source checkout:

    python -m sessionfs.server.helper.build_docs_index [DOCS_DIR] [--out PATH]
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

OUT_PATH = Path(__file__).with_name("docs_index.json")
_DOCS_SUBDIR = Path("site") / "src" / "content" / "docs"

BASE_URL = "https://sessionfs.dev"
SECTION_CAP = 6000

# Frontmatter block: ``---`` ... ``---``.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)
_TITLE_RE = re.compile(r"(?m)^title:[ \t]*(.+?)[ \t]*$")

# HTML comments (single- or multi-line).
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Any markdown heading (``#`` … ``######``).  Every level feeds the per-page
# duplicate-slug counter; only ``##`` / ``###`` start a new section.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_SECTION_LEVELS = (2, 3)

# MDX component tags to strip, keeping their text children. A whitelist rather
# than any ``<...>`` tag so placeholder text inside code (``<NAME>``,
# ``<ORG>``, ``<SessionFS_API_key>``) survives intact.
_COMPONENT_RE = re.compile(
    r"</?(?P<name>Aside|Tabs|TabItem|Card|CardGrid|LinkCard|Steps|Code|FileTree|Badge|"
    r"Icon|PackageManagers)(?P<attrs>(?:[ \t]+[^<>]*?)?)[ \t]*/?>"
)
# Components whose ``title="…"`` is visible text on the page.
_TITLED_COMPONENTS = frozenset({"Aside", "Card", "Tabs", "TabItem", "LinkCard"})
_TITLE_ATTR_RE = re.compile(r"""(?:^|[ \t])title=(?:"([^"]*)"|'([^']*)')""")

# A markdown table separator row: ``| --- | :---: |`` (pipes, dashes, colons).
_TABLE_SEPARATOR_RE = re.compile(r"^\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?$")

# Inline code, links and images.
_CODE_SPAN_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")

# Placeholders used to protect inline-code text from emphasis stripping.
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")

# Blockquote and list markers at the start of a prose line (body text only —
# never code blocks, which are handled before inline cleaning).
_LIST_PREFIX_RE = re.compile(r"^(?:>[ \t]*)+|^[-*+][ \t]+|^\d{1,2}[.)][ \t]+")

# Characters GitHub's slugger drops from an anchor (general punctuation ranges
# plus the ASCII punctuation set); ``-`` and ``_`` are preserved.
_SLUG_REMOVE_RE = re.compile(
    r"[\u2000-\u206f\u2e00-\u2e7f\\'!\"#$%&()*+,./:;<=>?@\[\]^`{|}~]"
)


def find_docs_root(start: Path | None = None) -> Path | None:
    """Return the repo's ``site/src/content/docs`` by walking up from this
    package, or ``None`` when there is no such directory (an installed wheel
    has no ``site/``)."""
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / _DOCS_SUBDIR
        if candidate.is_dir():
            return candidate
    return None


def github_slug(text: str) -> str:
    """Return a GitHub-style heading anchor (lowercase, punctuation dropped,
    spaces become ``-``)."""
    return _SLUG_REMOVE_RE.sub("", text.lower()).replace(" ", "-")


class Slugger:
    """Per-page unique slugs, as github-slugger (and so Starlight) assigns
    them: a repeated slug gets ``-1``, ``-2`` … across every heading level."""

    def __init__(self) -> None:
        self._occurrences: dict[str, int] = {}

    def slug(self, text: str) -> str:
        original = github_slug(text)
        result = original
        while result in self._occurrences:
            self._occurrences[original] += 1
            result = f"{original}-{self._occurrences[original]}"
        self._occurrences[result] = 0
        return result


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'`" and value[-1] == value[0]:
        return value[1:-1]
    return value


def _strip_frontmatter(raw: str) -> tuple[str, str]:
    """Return ``(body, title)`` with the YAML frontmatter removed."""
    title = ""
    body = raw
    match = _FRONTMATTER_RE.match(raw)
    if match:
        body = raw[match.end() :]
        frontmatter = match.group(1)
        title_match = _TITLE_RE.search(frontmatter)
        if title_match:
            title = _unquote(title_match.group(1))
    return body, title


def _sub_component(match: re.Match[str]) -> str:
    """Drop an MDX component tag, keeping a visible ``title="…"`` as text."""
    if match.group("name") in _TITLED_COMPONENTS:
        title = _TITLE_ATTR_RE.search(match.group("attrs") or "")
        if title:
            return f" {title.group(1) if title.group(1) is not None else title.group(2)} "
    return ""


def _clean_inline(text: str) -> str:
    """Strip MDX components, links/images and bold markers from one line,
    keeping inline-code content verbatim and decoding HTML entities."""
    codes: list[str] = []

    def _sub_code(match: re.Match[str]) -> str:
        codes.append(match.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE_SPAN_RE.sub(_sub_code, text)
    text = _IMAGE_RE.sub("", text)
    text = _COMPONENT_RE.sub(_sub_component, text)
    text = _LINK_RE.sub(r"\1", text)
    # Bold markers only: single ``*`` / ``_`` stay (identifiers like
    # ``upload_timeout`` must not lose their underscore).
    text = text.replace("**", "").replace("__", "")
    text = html.unescape(text)

    def _restore(match: re.Match[str]) -> str:
        return codes[int(match.group(1))]

    return " ".join(_PLACEHOLDER_RE.sub(_restore, text).split())


def _is_module_line(stripped: str) -> bool:
    """True for a top-level MDX ``import`` / ``export`` line (not a shell or
    Python command inside a code fence — those are handled by ``in_code``)."""
    if stripped.startswith("import "):
        return " from " in stripped or stripped.startswith("import(")
    return bool(
        re.match(
            r"^export[ \t]+(?:const|let|var|function|default|async|class|type|interface)\b",
            stripped,
        )
    )


def _split_sections(body: str) -> list[dict]:
    """Split a cleaned page body into ``{heading, anchor, lines}`` sections."""
    sections: list[dict] = []
    current: dict = {"heading": None, "anchor": None, "lines": []}
    slugger = Slugger()
    in_code = False

    for raw_line in body.splitlines():
        stripped = raw_line.strip()

        if not in_code:
            heading = _HEADING_RE.match(stripped)
            if heading:
                text = _clean_inline(heading.group(2))
                anchor = slugger.slug(text)
                if len(heading.group(1)) in _SECTION_LEVELS:
                    if current["heading"] is not None or current["lines"]:
                        sections.append(current)
                    current = {"heading": text, "anchor": anchor, "lines": []}
                elif text:
                    # Other levels stay in the body as text (still counted).
                    current["lines"].append(text)
                continue
            if _is_module_line(stripped):
                continue

        if stripped.startswith("```"):
            in_code = not in_code
            continue

        if not stripped:
            continue

        if in_code:
            # Code blocks are kept as text: commands matter.
            current["lines"].append(raw_line.rstrip())
        else:
            if _TABLE_SEPARATOR_RE.match(stripped):
                continue
            cleaned = _LIST_PREFIX_RE.sub("", _clean_inline(stripped)).strip()
            if cleaned:
                current["lines"].append(cleaned)

    if current["heading"] is not None or current["lines"]:
        sections.append(current)
    return sections


def _finalize_sections(sections: list[dict]) -> list[dict]:
    """Turn raw sections into index entries, splitting long text into parts."""
    out: list[dict] = []
    for section in sections:
        text = " ".join("\n".join(section["lines"]).split())
        if not text and section["heading"] is None:
            continue
        heading = section["heading"]
        anchor = section["anchor"] if heading else None
        if len(text) <= SECTION_CAP:
            parts = [text]
        else:
            parts = [text[i : i + SECTION_CAP] for i in range(0, len(text), SECTION_CAP)]
        for index, part in enumerate(parts, start=1):
            entry = {"anchor": anchor, "heading": heading, "text": part}
            if len(parts) > 1:
                entry["part"] = index
            out.append(entry)
    return out


def _file_slug(rel: Path) -> str:
    parts = list(rel.parts[:-1]) + [rel.stem]
    return "/".join(parts)


def _page_url(slug: str) -> str:
    if slug == "index":
        return f"{BASE_URL}/"
    return f"{BASE_URL}/{slug}/"


def build_index(docs_root: Path, out: Path = OUT_PATH) -> None:
    """Index every ``.mdx`` page under ``docs_root`` into ``out``."""
    pages: list[dict] = []
    for path in sorted(docs_root.rglob("*.mdx")):
        slug = _file_slug(path.relative_to(docs_root))
        raw = path.read_text(encoding="utf-8")
        body, title = _strip_frontmatter(raw)
        body = _HTML_COMMENT_RE.sub("", body)
        sections = _finalize_sections(_split_sections(body))
        pages.append(
            {
                "slug": slug,
                "title": title or slug.rsplit("/", 1)[-1],
                "url": _page_url(slug),
                "sections": sections,
            }
        )
    pages.sort(key=lambda page: page["slug"])
    data = {"pages": pages}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sessionfs.server.helper.build_docs_index",
        description="Build the session-helper docs index.",
    )
    parser.add_argument(
        "docs_root",
        nargs="?",
        type=Path,
        help="Site docs directory (default: the repo's site/src/content/docs).",
    )
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args(argv)
    docs_root = args.docs_root or find_docs_root()
    if docs_root is None or not docs_root.is_dir():
        parser.error(
            "site docs directory not found; pass it explicitly "
            "(this needs a source checkout with site/src/content/docs)"
        )
    build_index(docs_root, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
