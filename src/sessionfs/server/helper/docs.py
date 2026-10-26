"""Static SessionFS docs index for the session helper.

Loads the bundled ``docs_index.json`` (built by
``python -m sessionfs.server.helper.build_docs_index``) once and provides a small, dependency-free
BM25 search over page title, heading and section text, plus exact lookups used
by the ``search_sessionfs_docs`` / ``get_sessionfs_doc`` tools and by doc
citation validation.

Tokenization is a lowercase word split with a tiny iterative suffix stemmer;
ranking is classic BM25 (k1=1.2, b=0.75) with the page title and heading
repeated so they weigh more than the body text.  English stop words are
dropped from queries, adjacent query words are also tried joined
(``hand off`` → ``handoff``), and queries are capped (``MAX_QUERY_CHARS`` /
``MAX_QUERY_TERMS``) so a huge query can't make a search expensive; only
sections that contain a query term are scored (inverted index built at load).
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_INDEX_PATH = Path(__file__).with_name("docs_index.json")

# BM25 constants.
_K1 = 1.2
_B = 0.75
# Field boosts: repeat title/heading tokens in the indexed text so they
# contribute more than body tokens.
_TITLE_REPEAT = 3
_HEADING_REPEAT = 2

# How long an excerpt / page fetch can be (characters).
_EXCERPT_CHARS = 600
_GET_MAX_CHARS = 8000

# Query caps: characters considered, and unique (stemmed) terms scored.
MAX_QUERY_CHARS = 500
MAX_QUERY_TERMS = 32

# Small English stop-word list, dropped from queries only (the index keeps
# every word so section lengths stay comparable).
_STOP_WORDS = frozenset(
    """
    a an and are as at be but by can could do does did doing for from had has
    have how i if in into is it its me my no not of on or our should so than
    that the their them then there these they this to up us was we were what
    when where which who why will with would you your
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9]+")

# Longest/specific suffixes first; applied iteratively so ``sessions`` and
# ``session`` land on the same stem.  ``ies`` is handled separately (→ ``y``).
_SUFFIXES = (
    "ization",
    "ation",
    "ition",
    "tional",
    "ingly",
    "ing",
    "ion",
    "ed",
    "es",  # only after ss/x/z/ch/sh; otherwise ``s`` alone is stripped
    "ment",
    "ness",
    "ity",
    "ly",
    "er",
    "or",
    "al",
    "s",
)


_ES_STEMS = ("ss", "x", "z", "ch", "sh")


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _stem(token: str) -> str:
    """A tiny, conservative iterative suffix stemmer (no new dependencies)."""
    for _ in range(4):
        if len(token) <= 3:
            return token
        if token.endswith("ies") and len(token) >= 5:
            token = token[:-3] + "y"
            continue
        changed = False
        for suffix in _SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                if suffix == "es" and not token[:-2].endswith(_ES_STEMS):
                    continue  # ``files`` → ``file``, not ``fil``
                token = token[: -len(suffix)]
                changed = True
                break
        if not changed:
            break
    # Drop a final ``e`` so ``cache``/``caches`` and ``release``/``releases``
    # share a stem.
    if len(token) > 4 and token.endswith("e"):
        token = token[:-1]
    return token


@dataclass
class _Section:
    slug: str
    page_title: str
    url: str  # page URL, no fragment
    anchor: str | None
    heading: str | None
    text: str
    part: int
    # term -> term frequency over the boosted text (title/heading repeated).
    tokens: dict[str, int]
    length: int  # boosted text length in tokens


@dataclass
class DocsIndex:
    sections: list[_Section]
    df: dict[str, int]
    n: int
    avg_length: float
    by_slug: dict[str, list[_Section]]
    # stem -> ids (positions in ``sections``) of the sections containing it.
    postings: dict[str, list[int]]

    @classmethod
    def load(cls, path: Path | None = None) -> "DocsIndex":
        path = path or _INDEX_PATH
        data = json.loads(path.read_text(encoding="utf-8"))
        sections: list[_Section] = []
        df: dict[str, int] = {}
        by_slug: dict[str, list[_Section]] = {}
        postings: dict[str, list[int]] = {}
        total_length = 0
        for page in data["pages"]:
            slug = page["slug"]
            title = page["title"]
            url = page["url"]
            page_sections: list[_Section] = []
            for raw in page["sections"]:
                text = raw["text"]
                heading = raw.get("heading")
                anchor = raw.get("anchor")
                boosted = " ".join([title] * _TITLE_REPEAT + ([heading] * _HEADING_REPEAT if heading else []) + [text])
                token_counts: dict[str, int] = {}
                for token in _tokenize(boosted):
                    stem = _stem(token)
                    token_counts[stem] = token_counts.get(stem, 0) + 1
                length = sum(token_counts.values())
                section = _Section(
                    slug=slug,
                    page_title=title,
                    url=url,
                    anchor=anchor,
                    heading=heading,
                    text=text,
                    part=raw.get("part", 1),
                    tokens=token_counts,
                    length=length,
                )
                for stem in token_counts:
                    df[stem] = df.get(stem, 0) + 1
                    postings.setdefault(stem, []).append(len(sections))
                total_length += length
                sections.append(section)
                page_sections.append(section)
            by_slug[slug] = page_sections
        return cls(
            sections=sections,
            df=df,
            n=len(sections),
            avg_length=(total_length / len(sections)) if sections else 0.0,
            by_slug=by_slug,
            postings=postings,
        )

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def _bm25(self, tf: int, length: int, term: str) -> float:
        denom = tf + _K1 * (1 - _B + _B * (length / self.avg_length if self.avg_length else 1.0))
        return self._idf(term) * (tf * (_K1 + 1)) / denom

    def query_terms(self, query: str) -> list[str]:
        """Stemmed, de-duplicated query terms, capped at ``MAX_QUERY_TERMS``.

        Stop words are dropped (unless the query is nothing but stop words),
        and each pair of adjacent words is also tried joined when the joined
        stem is in the index, so "hand off" finds "handoff".
        """
        words = _tokenize(query[:MAX_QUERY_CHARS])
        content = [w for w in words if w not in _STOP_WORDS] or words
        terms: list[str] = []
        for i, word in enumerate(content):
            terms.append(_stem(word))
            if i + 1 < len(content):
                joined = _stem(word + content[i + 1])
                if joined in self.postings:
                    terms.append(joined)
        return list(dict.fromkeys(terms))[:MAX_QUERY_TERMS]

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Rank sections by BM25 and return the top ``limit`` with excerpts."""
        unique_terms = self.query_terms(query)
        if not unique_terms:
            return []
        weights = {t: self._idf(t) for t in unique_terms}
        candidates: set[int] = set()
        for term in unique_terms:
            candidates.update(self.postings.get(term, ()))
        scored: list[tuple[float, str, str | None, int, _Section]] = []
        for section_id in candidates:
            section = self.sections[section_id]
            score = sum(
                self._bm25(section.tokens.get(term, 0), section.length, term)
                for term in unique_terms
            )
            if score > 0:
                scored.append((score, section.slug, section.anchor or "", section.part, section))
        scored.sort(key=lambda row: (-row[0], row[1], row[2], row[3]))
        results: list[dict[str, Any]] = []
        for _, _, _, _, section in scored[:limit]:
            anchor_suffix = f"#{section.anchor}" if section.anchor else ""
            results.append(
                {
                    "slug": section.slug,
                    "page_title": section.page_title,
                    "anchor": section.anchor,
                    "heading": section.heading,
                    "url": f"{section.url}{anchor_suffix}",
                    "excerpt": self._excerpt(section.text, unique_terms, weights),
                }
            )
        return results

    def _excerpt(
        self, text: str, terms: list[str], weights: dict[str, float], width: int = _EXCERPT_CHARS
    ) -> str:
        if len(text) <= width:
            return text
        lower = text.lower()
        positions: list[tuple[int, float]] = []
        for term in terms:
            weight = weights.get(term, 1.0)
            start = 0
            while True:
                idx = lower.find(term, start)
                if idx < 0:
                    break
                positions.append((idx, weight))
                start = idx + 1
        if not positions:
            return text[:width]
        positions.sort()
        best_start = 0
        best_weight = -1.0
        j = 0
        for i in range(len(positions)):
            while j < len(positions) and positions[j][0] - positions[i][0] <= width:
                j += 1
            weight = sum(w for _, w in positions[i:j])
            if weight > best_weight:
                best_weight = weight
                span_start = positions[i][0]
                span_end = positions[j - 1][0]
                center = (span_start + span_end) // 2
                best_start = max(0, min(center - width // 2, len(text) - width))
        excerpt = text[best_start : best_start + width].strip()
        if best_start > 0:
            excerpt = excerpt[excerpt.find(" ") + 1 if " " in excerpt else 0 :]
        return excerpt

    def get(self, slug: str, anchor: str | None = None) -> dict[str, Any] | None:
        """Return one doc (page or section), capped for the model.  ``None``
        when the slug is unknown; an unknown anchor falls back to the page.

        A section fetch joins every part of that section; a page fetch that
        exceeds the cap ends with the list of section anchors so the model can
        fetch the rest one section at a time.
        """
        sections = self.by_slug.get(slug)
        if sections is None:
            return None
        if anchor is not None:
            matching = [s for s in sections if s.anchor == anchor]
            if matching:
                first = matching[0]
                return {
                    "slug": slug,
                    "page_title": first.page_title,
                    "anchor": first.anchor,
                    "heading": first.heading,
                    "url": f"{first.url}#{anchor}",
                    "text": " ".join(s.text for s in matching)[:_GET_MAX_CHARS],
                }
        # Whole page: heading-marked sections so the model keeps the structure.
        # Parts of one long section are joined under a single heading.
        blocks: list[str] = []
        previous: _Section | None = None
        for section in sections:
            continues = (
                previous is not None
                and section.part > 1
                and section.anchor == previous.anchor
            )
            if continues:
                blocks[-1] += " " + section.text
            elif section.heading:
                blocks.append(f"## {section.heading}\n{section.text}")
            else:
                blocks.append(section.text)
            previous = section
        text = "\n\n".join(blocks)
        if len(text) > _GET_MAX_CHARS:
            anchors = list(dict.fromkeys(s.anchor for s in sections if s.anchor))
            suffix = "\n\n…truncated; sections: " + ", ".join(anchors)
            keep = max(_GET_MAX_CHARS - len(suffix), _GET_MAX_CHARS // 2)
            text = text[:keep].rstrip() + suffix
        page = sections[0]
        return {
            "slug": slug,
            "page_title": page.page_title,
            "anchor": None,
            "heading": None,
            "url": page.url,
            "text": text,
        }

    def close_slugs(self, slug: str, limit: int = 10) -> list[str]:
        """Nearby slugs for a not-found suggestion.

        Compares against both the full slug and its final path segment so a
        typo like ``jirra`` still finds ``integrations/jira``.
        """
        slugs = list(self.by_slug.keys())
        needle = slug.lower()
        scored: list[tuple[float, str]] = []
        for candidate in slugs:
            lower = candidate.lower()
            segment = lower.rsplit("/", 1)[-1]
            ratio = max(
                difflib.SequenceMatcher(None, needle, lower).ratio(),
                difflib.SequenceMatcher(None, needle, segment).ratio(),
            )
            scored.append((ratio, candidate))
        scored.sort(key=lambda row: (-row[0], row[1]))
        matches = [candidate for ratio, candidate in scored if ratio >= 0.5]
        if matches:
            return matches[:limit]
        return sorted(c for c in slugs if needle in c.lower())[:limit]

    def resolve_citation(self, slug: str, anchor: str | None) -> dict[str, Any] | None:
        """Validate a ``[doc:<slug>#<anchor>]`` marker; ``None`` if not in the
        index.  Returns ``{slug, anchor, title, url}`` for the citation event."""
        sections = self.by_slug.get(slug)
        if sections is None:
            return None
        if anchor is not None and not any(s.anchor == anchor for s in sections):
            return None
        page = sections[0]
        return {
            "slug": slug,
            "anchor": anchor,
            "title": page.page_title,
            "url": f"{page.url}#{anchor}" if anchor else page.url,
        }


@lru_cache(maxsize=1)
def get_docs_index() -> DocsIndex:
    """The cached, process-wide docs index."""
    return DocsIndex.load()


def warm_docs_index_in_background() -> threading.Thread:
    """Load the docs index on a daemon thread so the first helper turn that
    searches the docs doesn't pay for parsing it.  Never raises: a failure is
    logged and the index loads lazily on first use instead."""

    def _load() -> None:
        try:
            get_docs_index()
        except Exception:  # pragma: no cover - defensive
            logger.warning("Could not preload the helper docs index", exc_info=True)

    thread = threading.Thread(target=_load, name="helper-docs-index-warm", daemon=True)
    thread.start()
    return thread
