"""Message-text extraction and relevance-ranked transcript excerpts.

Server-side text matching only — no embeddings in v1 (per the design).
Message indexes are 0-based positions in the archive's ``messages.jsonl``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from sessionfs.server.helper.context import RetrievalBudget

# Per-call token budget for excerpts: default 3000, hard cap 6000.
DEFAULT_EXCERPT_TOKENS = 3000
MAX_EXCERPT_TOKENS = 6000

# Cost bounds for query-driven ranking: a query is capped at 512 chars and at
# most 32 unique terms (lowercased, length >= 2); ranking scans at most the
# 5000 most recent messages.
MAX_QUERY_CHARS = 512
MAX_QUERY_TERMS = 32
MAX_SCAN_MESSAGES = 5000

# Search-snippet window (chars), lead before the match.
SNIPPET_WINDOW = 200
SNIPPET_LEAD = 40

_QUERY_TERM_RE = re.compile(r"[A-Za-z0-9_./-]+")


def _query_terms(query: str) -> list[str]:
    if not query:
        return []
    query = query[:MAX_QUERY_CHARS]
    terms: list[str] = []
    seen: set[str] = set()
    for raw in _QUERY_TERM_RE.findall(query):
        term = raw.lower()
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return terms


def _tool_use_text(name: str, inp: object) -> str:
    bits = [f"[{name}]"] if name else ["[tool]"]
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "pattern"):
            val = inp.get(key)
            if isinstance(val, str) and val:
                bits.append(val)
    return " ".join(bits)


def _tool_result_text(rc: object) -> str:
    if isinstance(rc, str):
        return rc
    if isinstance(rc, list):
        return "\n".join(
            b.get("text", "")
            for b in rc
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(rc) if rc is not None else ""


def message_text(msg: dict) -> str:
    """Plain-text rendering of one message, for ranking and display.

    Includes text blocks, tool-use commands/paths, and tool-result output.
    ``thinking`` blocks are intentionally excluded (matches the search index).
    """
    content = msg.get("content", "")
    if isinstance(content, str):
        return content

    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                name = block.get("name", block.get("tool_name", ""))
                parts.append(_tool_use_text(name, block.get("input", {})))
            elif btype == "tool_result":
                parts.append(_tool_result_text(block.get("content", "")))
    return "\n".join(p for p in parts if p)


def message_file_paths(msg: dict) -> list[str]:
    """File paths referenced by a message's tool_use inputs."""
    content = msg.get("content", [])
    paths: list[str] = []
    if not isinstance(content, list):
        return paths
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        inp = block.get("input", {})
        if not isinstance(inp, dict):
            continue
        for key in ("file_path", "path", "pattern", "filename", "directory"):
            val = inp.get(key)
            if isinstance(val, str) and val:
                paths.append(val)
    return paths


def workspace_paths(workspace: dict | None) -> list[str]:
    """File paths captured in the workspace manifest."""
    paths: list[str] = []
    if not isinstance(workspace, dict):
        return paths
    files = workspace.get("files")
    if isinstance(files, list):
        for f in files:
            if isinstance(f, dict) and isinstance(f.get("path"), str) and f["path"]:
                paths.append(f["path"])
    root = workspace.get("root_path")
    if isinstance(root, str) and root:
        paths.append(root)
    return paths


def normalize_path(path: str) -> str:
    """Normalize a captured path for comparison.

    Strips leading ``./``, collapses duplicate separators, converts ``\\`` to
    ``/``, and drops a trailing slash.  Absolute paths stay absolute; callers
    relativize them under the workspace root first (:func:`relativize_path`).
    """
    p = path.strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    return p.rstrip("/")


def relativize_path(path: str, root: str | None) -> str:
    """Make an absolute ``path`` under ``root`` relative to it.

    ``touches_path`` compares path segments, so an absolute path that lives
    under the workspace root must be turned back into a relative one first —
    otherwise ``/repo/src/auth`` would never equal the queried ``src/auth``.
    """
    p = normalize_path(path)
    if not p.startswith("/") or not root:
        return p
    r = normalize_path(root)
    if not r:
        return p
    if p == r:
        return ""
    if p.startswith(r + "/"):
        return p[len(r) + 1 :]
    return p


def touches_path(paths: list[str], prefix: str) -> bool:
    """True when any captured path lies under (or equals) ``prefix``.

    A segment-based prefix match on normalized paths: ``src/auth`` matches
    ``src/auth/login.ts`` but not ``lib/src/authz``.
    """
    prefix_segs = [s for s in normalize_path(prefix).split("/") if s]
    if not prefix_segs:
        return False
    for path in paths:
        segs = [s for s in normalize_path(path).split("/") if s]
        if segs[: len(prefix_segs)] == prefix_segs:
            return True
    return False


@dataclass(frozen=True)
class Excerpt:
    index: int
    text: str


def rank_excerpts(
    messages: list[dict],
    query: str,
    max_tokens: int = DEFAULT_EXCERPT_TOKENS,
    limit: int | None = None,
    indexes: list[int] | None = None,
    redactor: Callable[[str], str] | None = None,
) -> list[Excerpt]:
    """Relevance-ranked excerpts (term overlap with ``query``) within a token
    budget.  ``max_tokens`` is clamped to ``MAX_EXCERPT_TOKENS``.

    ``indexes`` is the 0-based ``messages.jsonl`` line number of each message
    (parallel to ``messages``; see :func:`extract_archive`).  ``redactor`` is
    applied to each message's FULL text before scoring/truncation, so a secret
    can never be cut in half and leaked by a later truncation.
    """
    max_tokens = max(0, min(max_tokens, MAX_EXCERPT_TOKENS))
    terms = _query_terms(query)

    if indexes is None:
        indexes = list(range(len(messages)))
    # Cost bound: only the most recent messages are scanned.
    if len(messages) > MAX_SCAN_MESSAGES:
        messages = messages[-MAX_SCAN_MESSAGES:]
        indexes = indexes[-MAX_SCAN_MESSAGES:]

    scored: list[tuple[int, int, str]] = []
    for index, msg in zip(indexes, messages):
        text = message_text(msg)
        if redactor is not None:
            text = redactor(text)
        if not text.strip():
            continue
        score = _overlap_score(text, terms)
        scored.append((score, index, text))

    if terms:
        # Higher score first; ties break by ascending message index.
        scored.sort(key=lambda t: (-t[0], t[1]))

    excerpts: list[Excerpt] = []
    tokens_used = 0
    for _score, index, text in scored:
        cost = RetrievalBudget.estimate_tokens(text)
        remaining = max_tokens - tokens_used
        if remaining <= 0:
            break
        if cost > remaining:
            text = _truncate_to_tokens(text, remaining)
            cost = RetrievalBudget.estimate_tokens(text)
        excerpts.append(Excerpt(index=index, text=text))
        tokens_used += cost
        if limit is not None and len(excerpts) >= limit:
            break
    return excerpts


def _overlap_score(text: str, terms: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term in lowered)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    keep = max_tokens * 4
    if len(text) <= keep:
        return text
    cut = text[:keep]
    # Last guard: drop a trailing partial token so no word is cut mid-way.
    boundary = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    if boundary > keep // 2:
        cut = cut[:boundary]
    return cut + "…"


def redacted_snippet(full_text: str, query: str, redactor: Callable[[str], str]) -> str:
    """A query-centred snippet built from the *redacted* full text.

    The full text is redacted before any window is taken, so a secret that
    straddles the window boundary is redacted in place instead of being cut
    in half and leaked by the SQL-side ``substr`` snippet.
    """
    text = redactor(full_text or "")
    if not text.strip():
        return ""
    needle = (query or "").strip().lower()
    lowered = text.lower()
    pos = lowered.find(needle) if needle else -1
    start = 0 if pos < 0 else max(0, pos - SNIPPET_LEAD)
    window = text[start : start + SNIPPET_WINDOW]
    if start > 0:
        window = "…" + window
    if start + SNIPPET_WINDOW < len(text):
        window = window + "…"
    return window
