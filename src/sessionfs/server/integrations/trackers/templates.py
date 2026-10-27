"""Built-in Jira story templates.

Each template is an ordered list of sections. A section is either:

* ``mode="verbatim"`` — the section is filled exactly from its source field
  (acceptance criteria, completion notes, linked sessions) and is NEVER
  taken from the model; or
* ``mode="rewrite"`` — the deterministic draft fills it by joining the raw
  source fields, and the model (when available) may reshape it into
  executive prose. Every model-written paragraph must carry a citation back
  to its source span, or it is dropped.

Custom templates (a later piece, org-scoped) reuse the same section shape;
their sections are stored as JSON and validated against ``SOURCE_FIELDS``.
``guidance`` is a rewrite-section-only instruction string supplied by the
org admin and treated as untrusted data (delimited) by ``promote.py``.
``empty_text`` is a built-in-only fallback for a verbatim section with no
source content (the design template's success metric when a ticket has no
acceptance criteria).
"""

from __future__ import annotations

from dataclasses import dataclass

# The fixed source-field vocabulary a section may draw from. It maps 1:1 to
# the fields on ``TicketSources`` in ``promote.py`` and is the only set of
# fields a custom template may reference.
SOURCE_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "acceptance_criteria",
    "priority",
    "kind",
    "persona",
    "completion_notes",
    "linked_sessions",
)

# Which source fields each section mode can render (``promote.py``'s
# ``_verbatim_paragraphs`` / ``_rewrite_paragraphs``). A verbatim section copies
# list-like or note-like content exactly, so only the fields with a verbatim
# rendering are allowed; rewrite sections can draw from every field. The
# dashboard mirrors this in ``utils/jira.ts`` (kept in sync by a test).
VERBATIM_SOURCE_FIELDS: tuple[str, ...] = (
    "acceptance_criteria",
    "completion_notes",
    "linked_sessions",
)
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
    # Rewrite-only guidance (custom templates). Untrusted: promote.py wraps it
    # in delimiters and never lets it rewrite a verbatim section.
    guidance: str | None = None
    # Fallback paragraph for a verbatim section with no source content
    # (built-ins only — e.g. design's success_metric without acceptance criteria).
    empty_text: str | None = None


@dataclass(frozen=True)
class Template:
    slug: str
    name: str
    sections: tuple[TemplateSection, ...]


_AGILE = (
    TemplateSection(
        "summary", "Summary", "rewrite",
        ("title", "description", "priority", "kind"),
    ),
    TemplateSection(
        "acceptance_criteria", "Acceptance Criteria", "verbatim",
        ("acceptance_criteria",),
    ),
    TemplateSection(
        "context", "Context & Prior Work", "rewrite",
        ("description", "linked_sessions", "completion_notes"),
    ),
)

_COMPLIANCE = (
    TemplateSection(
        "summary", "Summary", "rewrite", ("title", "description"),
    ),
    TemplateSection(
        "acceptance_criteria", "Acceptance Criteria", "verbatim",
        ("acceptance_criteria",),
    ),
    TemplateSection(
        "impact", "Impact & Risk", "rewrite",
        ("description", "priority", "linked_sessions"),
    ),
    TemplateSection(
        "evidence", "Completion Notes", "verbatim", ("completion_notes",),
    ),
)

_SRE = (
    TemplateSection(
        "summary", "Summary", "rewrite", ("title", "description"),
    ),
    TemplateSection(
        "acceptance_criteria", "Acceptance Criteria", "verbatim",
        ("acceptance_criteria",),
    ),
    TemplateSection(
        "impact", "Blast Radius & Impact", "rewrite",
        ("description", "priority"),
    ),
    TemplateSection(
        "related_sessions", "Related Sessions", "verbatim",
        ("linked_sessions",),
    ),
)

_BUG = (
    TemplateSection(
        "summary", "Summary", "rewrite", ("title", "description"),
    ),
    TemplateSection(
        "acceptance_criteria", "Acceptance Criteria", "verbatim",
        ("acceptance_criteria",),
    ),
    TemplateSection(
        "reproduction", "Reproduction & Observed Behavior", "rewrite",
        ("description", "linked_sessions"),
    ),
    TemplateSection(
        "notes", "Completion Notes", "verbatim", ("completion_notes",),
    ),
)

# Design/UX stories lead with the problem and the expected outcome. The success
# metric is verbatim from the acceptance criteria (never invented); when a
# ticket has no acceptance criteria the section says so instead of hallucinating
# a metric.
_DESIGN = (
    TemplateSection(
        "user_journey", "User Journey", "rewrite", ("description",),
    ),
    TemplateSection(
        "hypothesis", "Hypothesis", "rewrite", ("description",),
    ),
    TemplateSection(
        "success_metric", "Success Metric", "verbatim",
        ("acceptance_criteria",),
        empty_text="Not specified in the source ticket",
    ),
    TemplateSection(
        "context", "Context", "rewrite", ("description", "linked_sessions"),
    ),
)

TEMPLATES: dict[str, Template] = {
    "agile": Template("agile", "Agile Standard", _AGILE),
    "compliance": Template("compliance", "Enterprise / Compliance", _COMPLIANCE),
    "sre": Template("sre", "Platform / SRE", _SRE),
    "bug": Template("bug", "Bug Report", _BUG),
    "design": Template("design", "Design / UX", _DESIGN),
}

DEFAULT_TEMPLATE = "agile"


def list_templates() -> list[Template]:
    return list(TEMPLATES.values())


def get_template(slug: str) -> Template | None:
    return TEMPLATES.get(slug)


def is_builtin_slug(slug: str) -> bool:
    return slug in TEMPLATES
