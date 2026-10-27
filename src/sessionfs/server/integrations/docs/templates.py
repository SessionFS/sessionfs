"""Built-in Confluence page templates.

Each template is an ordered list of sections. A section is either:

* ``mode="verbatim"`` — the section is filled exactly from its source field
  and is NEVER taken from the model; or
* ``mode="rewrite"`` — the deterministic draft fills it by joining the raw
  source fields, and the model (when available) may reshape it into prose.
  Every model-written paragraph must carry a citation back to its source
  field, or it is dropped. When no LLM key is available, rewrite sections
  fall back to the verbatim source text (publish still works).

Custom Confluence templates are out of the MVP — only built-ins exist.
``guidance``/``empty_text`` are retained on the section shape for parity with
the tracker templates, but built-in docs templates do not use them.
"""

from __future__ import annotations

from dataclasses import dataclass

# The fixed source-field vocabulary a section may draw from. It maps 1:1 to the
# fields on ``DocSources`` in ``promote.py``.
SOURCE_FIELDS: tuple[str, ...] = (
    "page_body",
    "sections",
    "claims_by_type",
    "session_evidence",
    "compile_metadata",
    "backlinks",
)

# Every field has a verbatim rendering and a rewrite rendering, so both modes
# may draw from all fields (unlike the tracker templates, where list/note
# fields are verbatim-only). A rewrite section falls back to the same verbatim
# text when no LLM key is available.
VERBATIM_SOURCE_FIELDS: tuple[str, ...] = SOURCE_FIELDS
REWRITE_SOURCE_FIELDS: tuple[str, ...] = SOURCE_FIELDS
SOURCE_FIELDS_BY_MODE: dict[str, tuple[str, ...]] = {
    "verbatim": VERBATIM_SOURCE_FIELDS,
    "rewrite": REWRITE_SOURCE_FIELDS,
}


@dataclass(frozen=True)
class TemplateSection:
    key: str
    heading: str
    mode: str  # "verbatim" | "rewrite"
    source_fields: tuple[str, ...]
    guidance: str | None = None
    empty_text: str | None = None


@dataclass(frozen=True)
class Template:
    slug: str
    name: str
    sections: tuple[TemplateSection, ...]


_REFERENCE = (
    TemplateSection("overview", "Overview", "rewrite", ("page_body",)),
    TemplateSection("details", "Details", "rewrite", ("page_body", "sections")),
    TemplateSection("claims", "Claims", "verbatim", ("claims_by_type",)),
    TemplateSection("evidence", "Session Evidence", "verbatim", ("session_evidence",)),
    TemplateSection("related", "Related", "verbatim", ("backlinks",)),
)

_ARCHITECTURE = (
    TemplateSection("overview", "Overview", "rewrite", ("page_body", "sections")),
    TemplateSection("components", "Components", "rewrite", ("sections",)),
    TemplateSection("decisions", "Key Decisions", "verbatim", ("claims_by_type",)),
    TemplateSection("related", "Related", "verbatim", ("backlinks",)),
)

_RUNBOOK = (
    TemplateSection("overview", "Overview", "rewrite", ("page_body",)),
    TemplateSection("steps", "Steps", "verbatim", ("page_body", "sections")),
    TemplateSection("known_issues", "Known Issues & Workarounds", "verbatim", ("claims_by_type",)),
    TemplateSection("evidence", "Session Evidence", "verbatim", ("session_evidence",)),
)

_ONBOARDING = (
    TemplateSection("overview", "Overview", "rewrite", ("page_body", "sections")),
    TemplateSection("conventions", "Coding Conventions", "verbatim", ("claims_by_type",)),
    TemplateSection("evidence", "Session Evidence", "verbatim", ("session_evidence",)),
    TemplateSection("related", "Related", "verbatim", ("backlinks",)),
)

_CONCEPT = (
    TemplateSection("overview", "Overview", "rewrite", ("page_body",)),
    TemplateSection("context", "Context", "rewrite", ("sections", "compile_metadata")),
    TemplateSection("related", "Related", "verbatim", ("backlinks",)),
)

TEMPLATES: dict[str, Template] = {
    "reference": Template("reference", "Reference", _REFERENCE),
    "architecture": Template("architecture", "Architecture", _ARCHITECTURE),
    "runbook": Template("runbook", "Runbook", _RUNBOOK),
    "onboarding": Template("onboarding", "Onboarding", _ONBOARDING),
    "concept": Template("concept", "Concept", _CONCEPT),
}

DEFAULT_TEMPLATE = "reference"


def list_templates() -> list[Template]:
    return list(TEMPLATES.values())


def get_template(slug: str) -> Template | None:
    return TEMPLATES.get(slug)


def is_builtin_slug(slug: str) -> bool:
    return slug in TEMPLATES
