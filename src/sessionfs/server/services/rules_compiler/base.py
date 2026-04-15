"""Base compiler interface + shared helpers.

A compiler produces deterministic output: same inputs always produce the
same bytes and therefore the same content hash. No timestamps, no randomness,
no floating-point serialization.

The managed marker is a tool-neutral comment block that includes:
- the SessionFS tag
- the canonical rules version
- the content hash (sha256 hex, first 16 chars)
- the canonical source command (sfs rules edit)

Tools each use their own comment syntax (HTML comment vs. `#` vs. `//`),
but all markers are machine-readable via a simple regex.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol

# Marker regex — tolerant of any comment style around the tokens.
MARKER_RE = re.compile(
    r"sessionfs[-_]managed.*?version[:=]\s*(\d+).*?hash[:=]\s*([0-9a-f]{8,64})",
    re.IGNORECASE | re.DOTALL,
)

MANAGED_TAG = "sessionfs-managed"
CANONICAL_SOURCE = "sfs rules edit"


@dataclass
class KnowledgeClaim:
    """A knowledge entry as seen by compilers — only the fields we inject."""

    entry_type: str
    content: str
    entity_ref: str | None = None


@dataclass
class CompileContext:
    """Inputs shared across all compilers for a single compile pass."""

    static_rules: str
    knowledge_claims: list[KnowledgeClaim] = field(default_factory=list)
    context_sections: dict[str, str] = field(default_factory=dict)
    tool_overrides: dict[str, dict] = field(default_factory=dict)
    version: int = 1


@dataclass
class CompileResult:
    """One compiler's output."""

    tool: str
    filename: str
    content: str
    content_hash: str
    token_count: int


def estimate_tokens(text: str) -> int:
    """Rough token estimate — 4 chars per token is the common approximation."""
    return max(1, len(text) // 4)


def compute_output_hash(content: str) -> str:
    """SHA-256 hex of the compiled content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def aggregate_hash(outputs: Iterable[CompileResult]) -> str:
    """Hash covering *all* compiled outputs in deterministic order.

    Used to decide whether a new rules_versions row is needed.
    """
    h = hashlib.sha256()
    for r in sorted(outputs, key=lambda o: o.tool):
        h.update(r.tool.encode())
        h.update(b"\x00")
        h.update(r.content_hash.encode())
        h.update(b"\x00")
    return h.hexdigest()


def is_managed_content(content: str) -> bool:
    """True if the text contains a SessionFS managed marker."""
    if not content:
        return False
    return MANAGED_TAG in content.lower() and MARKER_RE.search(content) is not None


def parse_managed_marker(content: str) -> tuple[int, str] | None:
    """Extract (version, hash) from a managed marker, or None."""
    if not content:
        return None
    m = MARKER_RE.search(content)
    if not m:
        return None
    try:
        return int(m.group(1)), m.group(2).lower()
    except (ValueError, IndexError):
        return None


def _condense_knowledge(
    claims: list[KnowledgeClaim], max_chars: int
) -> list[KnowledgeClaim]:
    """Drop entries from the tail until we fit under `max_chars` (by content)."""
    if max_chars <= 0:
        return []
    total = 0
    kept: list[KnowledgeClaim] = []
    for c in claims:
        line_len = len(c.content) + 20  # account for type tag + framing
        if total + line_len > max_chars:
            break
        kept.append(c)
        total += line_len
    return kept


def _condense_context(
    sections: dict[str, str], max_chars: int
) -> dict[str, str]:
    """Trim sections so aggregate content fits under `max_chars`."""
    if max_chars <= 0:
        return {}
    out: dict[str, str] = {}
    total = 0
    # Preserve caller-provided ordering.
    for name, text in sections.items():
        if total >= max_chars:
            break
        remaining = max_chars - total
        if len(text) > remaining:
            out[name] = text[: max(0, remaining - 1)].rstrip() + "…"
            total = max_chars
        else:
            out[name] = text
            total += len(text)
    return out


def format_knowledge_block(claims: list[KnowledgeClaim]) -> str:
    """Descriptive (not prescriptive) knowledge block.

    "Project fact: …" framing per key design rule #5.
    """
    if not claims:
        return ""
    lines = ["## Project Facts (from knowledge base)", ""]
    for c in claims:
        suffix = f" _({c.entry_type}"
        if c.entity_ref:
            suffix += f" — `{c.entity_ref}`"
        suffix += ")_"
        lines.append(f"- Project fact: {c.content.strip()}{suffix}")
    lines.append("")
    return "\n".join(lines)


def format_context_block(sections: dict[str, str]) -> str:
    """Render project context sections as markdown."""
    if not sections:
        return ""
    parts = ["## Project Context", ""]
    for name, text in sections.items():
        heading = name.strip().title() if name else "Context"
        parts.append(f"### {heading}")
        parts.append("")
        parts.append(text.rstrip())
        parts.append("")
    return "\n".join(parts)


class RuleCompiler(Protocol):
    """Interface every tool compiler implements."""

    tool: str
    filename: str
    token_ceiling: int
    comment_style: str  # "html", "hash", "slash"

    def compile(self, ctx: CompileContext) -> CompileResult:
        """Produce a CompileResult — deterministic."""
        ...


# ---------------------------------------------------------------------------
# Marker formatters per comment style. Keep lightweight + tool-compatible.
# ---------------------------------------------------------------------------


def _marker_lines(version: int, content_hash: str) -> list[str]:
    return [
        f"{MANAGED_TAG}: version={version} hash={content_hash[:16]}",
        f"Canonical source: {CANONICAL_SOURCE}  (do not edit by hand — "
        "changes will be overwritten on the next `sfs rules compile`)",
    ]


def format_marker(style: str, version: int, content_hash: str) -> str:
    lines = _marker_lines(version, content_hash)
    if style == "html":
        body = "\n".join(lines)
        return f"<!--\n{body}\n-->"
    if style == "slash":
        return "\n".join(f"// {line}" for line in lines)
    # default: hash comments
    return "\n".join(f"# {line}" for line in lines)


# ---------------------------------------------------------------------------
# Base class used by the 5 concrete compilers.
# ---------------------------------------------------------------------------


@dataclass
class _BaseCompiler:
    """Concrete helper that the 5 tool compilers inherit via composition."""

    tool: str
    filename: str
    token_ceiling: int
    comment_style: str  # "html" | "hash" | "slash"

    def _render(self, ctx: CompileContext) -> str:
        """Render everything *except* the marker (which needs the hash)."""
        # Token budget: reserve 20% for static_rules + overrides, split remainder
        # between knowledge and context.
        total_chars = max(512, self.token_ceiling * 4)
        static_budget = int(total_chars * 0.4)
        knowledge_budget = int(total_chars * 0.3)
        context_budget = int(total_chars * 0.3)

        static_text = ctx.static_rules.strip()
        if len(static_text) > static_budget:
            static_text = static_text[: max(0, static_budget - 1)].rstrip() + "…"

        # Tool-specific overrides — simple "extra" string only for v0.9.9.
        override = ""
        ov_obj = ctx.tool_overrides.get(self.tool) or {}
        if isinstance(ov_obj, dict):
            extra = ov_obj.get("extra")
            if isinstance(extra, str) and extra.strip():
                override = extra.strip()

        knowledge_claims = _condense_knowledge(
            ctx.knowledge_claims, knowledge_budget
        )
        context_sections = _condense_context(ctx.context_sections, context_budget)

        knowledge_block = format_knowledge_block(knowledge_claims)
        context_block = format_context_block(context_sections)

        parts: list[str] = []
        if static_text:
            parts.append("## Project Preferences")
            parts.append("")
            parts.append(static_text)
            parts.append("")
        if override:
            parts.append(f"## Notes for {self.tool}")
            parts.append("")
            parts.append(override)
            parts.append("")
        if knowledge_block:
            parts.append(knowledge_block)
        if context_block:
            parts.append(context_block)
        if not parts:
            parts.append("<!-- No project rules configured. Run `sfs rules edit`. -->")

        return "\n".join(parts).rstrip() + "\n"

    def compile(self, ctx: CompileContext) -> CompileResult:
        body = self._render(ctx)
        # content_hash covers the rendered *body only* — not the marker —
        # so no-op detection doesn't break when the version number changes.
        # The marker embeds this hash so on-disk readers can still verify.
        body_hash = compute_output_hash(body)
        marker = format_marker(self.comment_style, ctx.version, body_hash)
        full = f"{marker}\n\n{body}"
        return CompileResult(
            tool=self.tool,
            filename=self.filename,
            content=full,
            content_hash=body_hash,
            token_count=estimate_tokens(full),
        )
