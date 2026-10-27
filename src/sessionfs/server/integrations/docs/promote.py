"""Build a Confluence page from a SessionFS source — translator, not generator.

The source (a wiki page or the compiled project context) is the source of
truth. The deterministic draft fills every template section from its source
fields (verbatim sections copied exactly). When the org/user has an LLM key,
only ``rewrite`` sections are sent to the model, which must return strict JSON
whose paragraphs each carry a citation back to a real source field; invalid
paragraphs are dropped and sections fall back to the deterministic draft.

The model output is untrusted: unknown section keys are dropped, JSON that
fails to parse is discarded, and every citation must resolve to a non-empty
source field. Verbatim sections are never taken from the model. With no LLM
key, rewrite sections fall back to the verbatim source text (publish still
works).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import User
from sessionfs.server.helper.keys import ResolvedLLM, resolve_helper_llm
from sessionfs.server.integrations.docs.templates import Template

if TYPE_CHECKING:
    from sessionfs.server.db.models import Organization

logger = logging.getLogger("sessionfs.api")


class ContentTooLarge(Exception):
    """A section's content cannot fit the publish caps.

    Raised instead of silently truncating/dropping paragraphs. ``section``
    names the offending section so the route can return a 422 that tells the
    caller exactly what to shorten.
    """

    def __init__(self, section: str) -> None:
        self.section = section
        super().__init__(f"section {section!r} content exceeds the publish limits")


# ── Source collection ──


@dataclass
class DocSources:
    page_body: str
    sections: list[tuple[str, str]]  # (heading, body) pairs
    claims_by_type: dict[str, list[str]]  # entry_type -> claim texts
    session_evidence: list[str]
    compile_metadata: str
    backlinks: list[str]


# ── Citation grammar ──

_NAMED_FIELDS = {
    "page_body",
    "sections",
    "claims_by_type",
    "session_evidence",
    "compile_metadata",
    "backlinks",
}


def _named_field_text(field: str, sources: DocSources) -> str:
    if field == "page_body":
        return sources.page_body
    if field == "sections":
        return "\n\n".join(
            f"## {heading}\n\n{body}" for heading, body in sources.sections
        )
    if field == "claims_by_type":
        return "\n\n".join(
            f"## {entry_type}\n\n"
            + "\n".join(f"- {claim}" for claim in claims)
            for entry_type, claims in sources.claims_by_type.items()
        )
    if field == "session_evidence":
        return "\n".join(f"- {s}" for s in sources.session_evidence)
    if field == "compile_metadata":
        return sources.compile_metadata
    if field == "backlinks":
        return "\n".join(f"- {b}" for b in sources.backlinks)
    return ""


def _validate_citation(citation: str, sources: DocSources) -> bool:
    if citation not in _NAMED_FIELDS:
        return False
    return bool(_named_field_text(citation, sources))


# ── Deterministic draft ──


@dataclass
class SectionParagraph:
    text: str
    citations: list[str]

    def to_dict(self) -> dict:
        return {"text": self.text, "citations": self.citations}


def _field_paragraphs(field: str, sources: DocSources) -> list[SectionParagraph]:
    text = _named_field_text(field, sources)
    if not text:
        return []
    return [SectionParagraph(text=text, citations=[field])]


def _section_dict(section, paragraphs: list[SectionParagraph]) -> dict:
    return {
        "key": section.key,
        "heading": section.heading,
        "mode": section.mode,
        "paragraphs": [p.to_dict() for p in paragraphs],
    }


def build_deterministic(template: Template, sources: DocSources) -> list[dict]:
    sections: list[dict] = []
    for section in template.sections:
        paragraphs: list[SectionParagraph] = []
        for field in section.source_fields:
            paragraphs.extend(_field_paragraphs(field, sources))
        sections.append(_section_dict(section, paragraphs))
    return sections


# ── LLM rewrite ──

_SYSTEM_PROMPT = (
    "You are a technical writer converting SessionFS knowledge into a concise "
    "Confluence page. You may reshape prose but must NOT add facts, claims, "
    "evidence or requirements. Every paragraph you write MUST cite a source "
    "field from the provided material. Return strict JSON only — no prose, no "
    "markdown fences."
)


# Cap the source material sent to the LLM so a huge page can't blow out the
# prompt. page_body is the big field (8,000 chars); every other field is capped
# at 2,000, and the combined presentation at 16,000.
_MAX_PAGE_BODY_CHARS = 8000
_MAX_OTHER_FIELD_CHARS = 2000
_MAX_TOTAL_SOURCE_CHARS = 16000

# Publish-shape caps, mirrored by the route's Pydantic models so malformed
# bodies are rejected with 422 rather than silently truncated.
_MAX_SECTIONS = 20
_MAX_PARAGRAPHS = 50
_MAX_PARAGRAPH_CHARS = 10000


def _cap_field(text: str, limit: int = _MAX_OTHER_FIELD_CHARS) -> str:
    if not text or len(text) <= limit:
        return text
    return text[:limit]


def _present_source(sources: DocSources) -> str:
    lines: list[str] = []
    if sources.page_body:
        lines.append("page_body:")
        lines.append(_cap_field(sources.page_body, _MAX_PAGE_BODY_CHARS))
    if sources.sections:
        lines.append("sections:")
        for heading, body in sources.sections:
            lines.append(f"  ## {_cap_field(heading)}")
            lines.append(_cap_field(body))
    if sources.claims_by_type:
        lines.append("claims_by_type:")
        for entry_type, claims in sources.claims_by_type.items():
            for claim in claims:
                lines.append(f"  [{entry_type}] {_cap_field(claim)}")
    if sources.session_evidence:
        lines.append("session_evidence:")
        for s in sources.session_evidence:
            lines.append(f"  - {_cap_field(s)}")
    if sources.compile_metadata:
        lines.append(f"compile_metadata: {_cap_field(sources.compile_metadata)}")
    if sources.backlinks:
        lines.append("backlinks:")
        for b in sources.backlinks:
            lines.append(f"  - {_cap_field(b)}")
    return _cap_field("\n".join(lines), _MAX_TOTAL_SOURCE_CHARS)


def _redact_for_llm(text: str, org: "Organization") -> str:
    from sessionfs.server.dlp import get_org_dlp_policy, redact_text, scan_dlp_full

    policy = get_org_dlp_policy(org)
    categories = {"secrets"}
    custom_patterns: list[dict] | None = None
    if policy is not None:
        categories |= set(policy.get("categories") or [])
        custom_patterns = policy.get("custom_patterns") or None
    findings = scan_dlp_full(
        text, categories=sorted(categories), custom_patterns=custom_patterns
    )
    if not findings:
        return text
    return redact_text(text, findings, custom_patterns)


def _redact_sources(sources: DocSources, org: "Organization") -> DocSources:
    """Redact every source field in full before it is capped for the prompt."""
    return DocSources(
        page_body=_redact_for_llm(sources.page_body, org),
        sections=[
            (_redact_for_llm(heading, org), _redact_for_llm(body, org))
            for heading, body in sources.sections
        ],
        claims_by_type={
            entry_type: [_redact_for_llm(claim, org) for claim in claims]
            for entry_type, claims in sources.claims_by_type.items()
        },
        session_evidence=[_redact_for_llm(s, org) for s in sources.session_evidence],
        compile_metadata=_redact_for_llm(sources.compile_metadata, org),
        backlinks=[_redact_for_llm(b, org) for b in sources.backlinks],
    )


def _parse_llm_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _build_prompt(template: Template, source_text: str) -> str:
    rewrite_keys = [s.key for s in template.sections if s.mode == "rewrite"]
    return (
        "Source material:\n"
        f"{source_text}\n\n"
        f"Rewrite these sections (ONLY these keys): {', '.join(rewrite_keys)}.\n\n"
        "Citation grammar: page_body, sections, claims_by_type, session_evidence, "
        "compile_metadata, backlinks.\n\n"
        "Respond with JSON shaped exactly like:\n"
        '{"sections": [{"key": "<key>", "paragraphs": [{"text": "...", '
        '"citations": ["page_body"]}]}]}'
    )


_DROPPED_TEXT_CHARS = 500
_MAX_DROPPED = 50


def _dropped(section: str, text: str, reason: str) -> dict:
    return {"section": section, "text": text[:_DROPPED_TEXT_CHARS], "reason": reason}


def _extract_rewrites(
    data: dict,
    template: Template,
    sources: DocSources,
    dropped: list[dict] | None = None,
) -> dict[str, list[SectionParagraph]]:
    """Valid model paragraphs per rewrite section key.

    Every paragraph the model wrote that is discarded (no citation, or a
    citation that does not resolve to a non-empty source field) is appended
    to ``dropped`` so the preview can show the reviewer what was cut.
    """
    raw_sections = data.get("sections")
    if not isinstance(raw_sections, list):
        return {}
    rewrite_keys = {s.key for s in template.sections if s.mode == "rewrite"}
    sink: list[dict] = dropped if dropped is not None else []

    out: dict[str, list[SectionParagraph]] = {}
    for raw_section in raw_sections:
        if not isinstance(raw_section, dict):
            continue
        key = raw_section.get("key")
        if key not in rewrite_keys:
            continue  # unknown or verbatim keys are dropped
        raw_paragraphs = raw_section.get("paragraphs")
        if not isinstance(raw_paragraphs, list):
            continue

        valid: list[SectionParagraph] = []
        for raw_para in raw_paragraphs:
            if not isinstance(raw_para, dict):
                continue
            text = raw_para.get("text")
            citations = raw_para.get("citations")
            if not isinstance(text, str) or not text.strip():
                continue
            if not isinstance(citations, list) or not citations:
                if len(sink) < _MAX_DROPPED:
                    sink.append(_dropped(key, text, "no_citation"))
                continue
            resolved = [
                c
                for c in citations
                if isinstance(c, str) and _validate_citation(c, sources)
            ]
            if not resolved:
                if len(sink) < _MAX_DROPPED:
                    sink.append(_dropped(key, text, "unresolvable_citation"))
                continue
            valid.append(SectionParagraph(text=text, citations=resolved))
        if valid:
            out[key] = valid
    return out


async def _rewrite(
    template: Template,
    sources: DocSources,
    llm: ResolvedLLM,
    org: "Organization",
    dropped: list[dict] | None = None,
) -> dict[str, list[SectionParagraph]]:
    from sessionfs.judge.providers import call_llm

    source_text = _present_source(_redact_sources(sources, org))
    prompt = _build_prompt(template, source_text)
    raw = await call_llm(
        model=llm.model,
        system=_SYSTEM_PROMPT,
        prompt=prompt,
        api_key=llm.api_key,
        provider=llm.provider,
        temperature=0,
        base_url=llm.base_url,
    )
    data = _parse_llm_json(raw)
    if data is None:
        return {}
    return _extract_rewrites(data, template, sources, dropped)


# ── Preview ──


@dataclass
class Preview:
    title: str
    sections: list[dict]
    citations: list[str]
    llm_used: bool
    key_source: str
    dropped: list[dict] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "sections": self.sections,
            "citations": self.citations,
            "llm_used": self.llm_used,
            "key_source": self.key_source,
            "dropped": self.dropped,
            "notes": self.notes,
        }


def _merge_rewrites(
    sections: list[dict], rewrites: dict[str, list[SectionParagraph]]
) -> list[dict]:
    merged: list[dict] = []
    for section in sections:
        paragraphs = rewrites.get(section["key"])
        if paragraphs:
            merged.append(
                {
                    "key": section["key"],
                    "heading": section["heading"],
                    "mode": section["mode"],
                    "paragraphs": [p.to_dict() for p in paragraphs],
                }
            )
        else:
            merged.append(section)
    return merged


def _all_citations(sections: list[dict]) -> list[str]:
    seen: list[str] = []
    for section in sections:
        for para in section.get("paragraphs", []):
            for citation in para.get("citations", []):
                if citation not in seen:
                    seen.append(citation)
    return seen


def _split_long_text(text: str, limit: int = _MAX_PARAGRAPH_CHARS) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        if len(para) <= limit:
            chunks.append(para)
            continue
        # Split a long paragraph on line boundaries; a single unbroken line is
        # split mid-line (nothing is dropped).
        if "\n" in para:
            buf = ""
            for line in para.splitlines(keepends=True):
                if buf and len(buf) + len(line) > limit:
                    chunks.append(buf)
                    buf = line
                else:
                    buf += line
            if buf:
                chunks.append(buf)
        else:
            chunks.extend(para[i : i + limit] for i in range(0, len(para), limit))
    return chunks


def _fit_sections_to_caps(sections: list[dict]) -> list[dict]:
    fitted: list[dict] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        key = section.get("key") or "section"
        if len(fitted) >= _MAX_SECTIONS:
            raise ContentTooLarge(key)
        section = dict(section)
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list):
            paragraphs = []
        kept: list[dict] = []
        for para in paragraphs:
            if not isinstance(para, dict):
                continue
            text = para.get("text")
            if not isinstance(text, str):
                continue
            for chunk in _split_long_text(text):
                kept.append({**para, "text": chunk})
        if len(kept) > _MAX_PARAGRAPHS:
            raise ContentTooLarge(key)
        for p in kept:
            if len(p.get("text") or "") > _MAX_PARAGRAPH_CHARS:
                raise ContentTooLarge(key)
        section["paragraphs"] = kept
        fitted.append(section)
    return fitted


async def build_preview(
    *,
    title: str,
    template: Template,
    sources: DocSources,
    org: "Organization",
    user: User,
    db: AsyncSession,
    allow_user_key: bool = True,
) -> Preview:
    sections = build_deterministic(template, sources)

    llm = await resolve_helper_llm(user, org.id, db, skip_user_key=not allow_user_key)
    rewrites: dict[str, list[SectionParagraph]] = {}
    dropped: list[dict] = []
    notes: list[str] = []
    has_rewrite = any(s.mode == "rewrite" for s in template.sections)
    if llm is not None and has_rewrite:
        try:
            rewrites = await _rewrite(template, sources, llm, org, dropped)
        except Exception:
            logger.warning(
                "confluence page rewrite failed; falling back to deterministic draft",
                exc_info=True,
            )
            notes.append(
                "The model rewrite failed; rewrite sections use the source text verbatim."
            )
    elif has_rewrite:
        notes.append(
            "No model key is configured; rewrite sections use the source text verbatim."
        )

    llm_used = bool(rewrites)
    if llm is not None and has_rewrite and not llm_used and not notes:
        notes.append(
            "The model returned no usable cited paragraphs; rewrite sections use "
            "the source text verbatim."
        )
    sections = _fit_sections_to_caps(_merge_rewrites(sections, rewrites))

    return Preview(
        title=title,
        sections=sections,
        citations=_all_citations(sections),
        llm_used=llm_used,
        key_source=(llm.key_source if llm is not None and llm_used else "none"),
        dropped=dropped,
        notes=notes,
    )


# ── DLP before publish ──


def redact_for_publish(text: str, org: "Organization | None") -> str:
    """Org DLP redaction applied to every outbound Confluence string.

    The same scan + redact the model path uses (``_redact_for_llm``): secrets
    are always redacted, plus the org's DLP categories and custom patterns.
    """
    if not text or org is None:
        return text
    return _redact_for_llm(text, org)


def redact_sections_for_publish(
    sections: list[dict], org: "Organization | None"
) -> list[dict]:
    """Redact headings and paragraph text of the final sections (copy)."""
    out: list[dict] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        copy = dict(section)
        copy["heading"] = redact_for_publish(str(section.get("heading") or ""), org)
        paragraphs = section.get("paragraphs")
        copy["paragraphs"] = [
            {**para, "text": redact_for_publish(str(para.get("text") or ""), org)}
            for para in (paragraphs if isinstance(paragraphs, list) else [])
            if isinstance(para, dict)
        ]
        out.append(copy)
    return out


# ── Storage rendering ──


def sections_to_storage(
    sections: list[dict],
    backlink_resolver,
    footer: str = "",
) -> str:
    """Render the (possibly edited) sections into storage-format XHTML.

    Every paragraph runs through the strict Markdown converter, which only
    allows headings, paragraphs, lists, code blocks, tables, links, emphasis
    and inline code — everything else is escaped text. This is the safety
    boundary: a model (or a wiki editor) can never inject raw Confluence markup.
    """
    from sessionfs.server.integrations.docs.confluence import (
        escape_text,
        markdown_to_storage,
    )

    if len(sections) > _MAX_SECTIONS:
        raise ContentTooLarge("sections")

    blocks: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        key = section.get("key") or "section"
        heading = (section.get("heading") or section.get("key") or "").strip()
        if heading:
            # Headings are plain text: escaped like any inline text so a
            # heading can never carry raw storage markup or a macro.
            blocks.append(f"<h2>{escape_text(heading[:200])}</h2>")
        paragraphs = section.get("paragraphs")
        if not isinstance(paragraphs, list):
            continue
        if len(paragraphs) > _MAX_PARAGRAPHS:
            raise ContentTooLarge(key)
        for para in paragraphs:
            if not isinstance(para, dict):
                continue
            text = (para.get("text") or "").strip()
            if not text:
                continue
            blocks.append(markdown_to_storage(text, backlink_resolver=backlink_resolver))
    if footer:
        blocks.append(footer)
    return "".join(blocks)


def published_text(sections: list[dict]) -> str:
    """The canonical text of a published draft (used for the re-publish diff)."""
    lines: list[str] = []
    for section in sections:
        heading = (section.get("heading") or section.get("key") or "").strip()
        if heading:
            lines.append("")
            lines.append(f"## {heading}")
        for para in section.get("paragraphs", []):
            text = (para.get("text") or "").strip()
            if text:
                lines.append(text)
    return "\n".join(lines).strip()


def payload_hash(sections: list[dict]) -> str:
    canonical = json.dumps({"sections": sections}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def source_hash(title: str, sources: DocSources, template_slug: str) -> str:
    """Hash of the SessionFS source a page is built from (+ template).

    Stored on the link as ``last_content_hash`` by both the interactive
    publish and the sweep, so the sweep can skip a page whose source has not
    changed since its last publish without any outbound call.
    """
    canonical = json.dumps(
        {
            "title": title,
            "template": template_slug,
            "page_body": sources.page_body,
            "sections": [list(pair) for pair in sources.sections],
            "claims_by_type": sources.claims_by_type,
            "session_evidence": sources.session_evidence,
            "compile_metadata": sources.compile_metadata,
            "backlinks": sources.backlinks,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
