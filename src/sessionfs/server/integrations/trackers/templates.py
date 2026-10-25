"""Built-in Jira story templates.

Each template is an ordered list of sections. A section is either:

* ``mode="verbatim"`` — the section is filled exactly from its source field
  (acceptance criteria, completion notes, linked sessions) and is NEVER
  taken from the model; or
* ``mode="rewrite"`` — the deterministic draft fills it by joining the raw
  source fields, and the model (when available) may reshape it into
  executive prose. Every model-written paragraph must carry a citation back
  to its source span, or it is dropped.

Custom templates are a later piece; the org admin picks one of these slugs
when connecting (no silent default), and a project mapping may override it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateSection:
    key: str
    heading: str
    mode: str  # "verbatim" | "rewrite"
    source_fields: tuple[str, ...]


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

TEMPLATES: dict[str, Template] = {
    "agile": Template("agile", "Agile Standard", _AGILE),
    "compliance": Template("compliance", "Enterprise / Compliance", _COMPLIANCE),
    "sre": Template("sre", "Platform / SRE", _SRE),
    "bug": Template("bug", "Bug Report", _BUG),
}

DEFAULT_TEMPLATE = "agile"


def list_templates() -> list[Template]:
    return list(TEMPLATES.values())


def get_template(slug: str) -> Template | None:
    return TEMPLATES.get(slug)
