"""Unit tests for org-custom Jira template validation."""

import pytest

from sessionfs.server.integrations.trackers.custom_templates import (
    MAX_GUIDANCE_CHARS,
    MAX_HEADING_CHARS,
    MAX_SECTIONS,
    MAX_TEMPLATES_PER_ORG,
    ResolvedTemplate,
    TemplateValidationError,
    builtin_resolved,
    sections_to_template,
    MAX_DESCRIPTION_CHARS,
    validate_description,
    validate_name,
    validate_sections,
    validate_slug,
)
from sessionfs.server.integrations.trackers.templates import (
    REWRITE_SOURCE_FIELDS,
    SOURCE_FIELDS,
    VERBATIM_SOURCE_FIELDS,
    get_template,
)


def _section(**over) -> dict:
    section = {
        "key": "summary",
        "heading": "Summary",
        "mode": "rewrite",
        "source_fields": ["title", "description"],
        "guidance": None,
    }
    section.update(over)
    return section


def test_valid_sections_normalize_and_drop_unknown_keys():
    sections = validate_sections(
        [_section(extra="ignored"), _section(key="ac", mode="verbatim", source_fields=["acceptance_criteria"])]
    )
    assert sections == [
        {
            "key": "summary",
            "heading": "Summary",
            "mode": "rewrite",
            "source_fields": ["title", "description"],
            "guidance": None,
        },
        {
            "key": "ac",
            "heading": "Summary",
            "mode": "verbatim",
            "source_fields": ["acceptance_criteria"],
            "guidance": None,
        },
    ]


def test_slug_validation():
    assert validate_slug("design-lite") == "design-lite"
    assert validate_slug("a1") == "a1"
    with pytest.raises(TemplateValidationError) as exc:
        validate_slug("")
    assert exc.value.field == "slug"
    with pytest.raises(TemplateValidationError):
        validate_slug("Bad_Slug!")
    with pytest.raises(TemplateValidationError):
        validate_slug("agile")  # built-in collision
    with pytest.raises(TemplateValidationError):
        validate_slug("-leading")


def test_name_validation():
    assert validate_name("  Design Lite  ") == "Design Lite"
    with pytest.raises(TemplateValidationError):
        validate_name("")
    with pytest.raises(TemplateValidationError):
        validate_name("x" * 121)


def test_sections_requires_a_non_empty_list():
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections([])
    assert exc.value.field == "sections"
    with pytest.raises(TemplateValidationError):
        validate_sections(None)


def test_sections_respect_the_section_count_limit():
    too_many = [
        _section(key=f"section_{i}") for i in range(MAX_SECTIONS + 1)
    ]
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections(too_many)
    assert exc.value.field == "sections"


def test_duplicate_section_keys_are_rejected():
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections([_section(), _section()])
    assert exc.value.field == "sections[1].key"


def test_invalid_mode_is_rejected():
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections([_section(mode="magic")])
    assert exc.value.field == "sections[0].mode"


def test_unknown_source_field_is_rejected():
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections([_section(source_fields=["description", "made_up"])])
    assert exc.value.field == "sections[0].source_fields[1]"


def test_empty_and_repeated_source_fields_are_rejected():
    with pytest.raises(TemplateValidationError):
        validate_sections([_section(source_fields=[])])
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections([_section(source_fields=["title", "title"])])
    assert exc.value.field == "sections[0].source_fields"


def test_heading_limit_is_enforced():
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections([_section(heading="H" * (MAX_HEADING_CHARS + 1))])
    assert exc.value.field == "sections[0].heading"


def test_guidance_is_rewrite_only():
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections(
            [_section(mode="verbatim", source_fields=["acceptance_criteria"], guidance="do X")]
        )
    assert exc.value.field == "sections[0].guidance"


def test_guidance_length_limit_and_blank_normalization():
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections([_section(guidance="x" * (MAX_GUIDANCE_CHARS + 1))])
    assert exc.value.field == "sections[0].guidance"

    sections = validate_sections([_section(guidance="   ")])
    assert sections[0]["guidance"] is None


def test_sections_to_template_round_trips_guidance():
    sections = validate_sections(
        [_section(key="summary", guidance="Lead with the why.")]
    )
    template = sections_to_template("design-lite", "Design Lite", sections)
    assert template.slug == "design-lite"
    assert template.name == "Design Lite"
    assert template.sections[0].guidance == "Lead with the why."
    assert template.sections[0].source_fields == ("title", "description")


def test_builtin_resolved_has_no_version():
    resolved = builtin_resolved("agile")
    assert isinstance(resolved, ResolvedTemplate)
    assert resolved.builtin is True
    assert resolved.version is None
    assert resolved.template is get_template("agile")


def test_source_field_vocabulary_is_exposed():
    assert len(SOURCE_FIELDS) == 8
    assert MAX_TEMPLATES_PER_ORG == 50


# ── per-mode source-field vocabulary (what promote.py can render) ──


@pytest.mark.parametrize("field", ["title", "description", "priority", "kind", "persona"])
def test_verbatim_section_refuses_fields_without_a_verbatim_rendering(field):
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections(
            [_section(mode="verbatim", source_fields=["acceptance_criteria", field])]
        )
    # The 422 names the exact offending entry.
    assert exc.value.field == "sections[0].source_fields[1]"
    assert "verbatim" in exc.value.message


def test_verbatim_and_rewrite_vocabularies_render_every_allowed_field():
    from sessionfs.server.integrations.trackers.promote import (
        TicketSources,
        _rewrite_paragraphs,
        _verbatim_paragraphs,
    )

    sources = TicketSources(
        title="T",
        description="d1\nd2",
        description_lines=["d1", "d2"],
        acceptance_criteria=["ac one"],
        priority="high",
        kind="task",
        persona="atlas",
        completion_notes="done",
        linked_sessions=["ses_1 — Session"],
    )
    for field in VERBATIM_SOURCE_FIELDS:
        assert _verbatim_paragraphs(field, sources), field
    for field in REWRITE_SOURCE_FIELDS:
        assert _rewrite_paragraphs(field, sources), field
    # A rewrite section may draw from acceptance criteria (cited per item).
    ac = _rewrite_paragraphs("acceptance_criteria", sources)
    assert ac[0].citations == ["acceptance_criteria[0]"]
    assert set(REWRITE_SOURCE_FIELDS) == set(SOURCE_FIELDS)


def test_dashboard_vocabulary_matches_the_server():
    """utils/jira.ts mirrors the per-mode vocabulary; keep them in sync."""
    import re
    from pathlib import Path

    ts = (
        Path(__file__).resolve().parents[3] / "dashboard" / "src" / "utils" / "jira.ts"
    ).read_text(encoding="utf-8")

    def ts_list(name: str) -> list[str]:
        match = re.search(rf"export const {name}[^=]*=\s*\[(.*?)\]", ts, re.S)
        assert match, name
        return re.findall(r"'([a-z_]+)'", match.group(1))

    assert ts_list("JIRA_VERBATIM_SOURCE_FIELDS") == list(VERBATIM_SOURCE_FIELDS)
    assert ts_list("JIRA_REWRITE_SOURCE_FIELDS") == list(REWRITE_SOURCE_FIELDS)


# ── description + control/format characters ──


def test_description_limit_and_blank_normalization():
    assert validate_description(None) is None
    assert validate_description("   ") is None
    assert validate_description("line one\n\tline two") == "line one\n\tline two"
    assert validate_description("x" * MAX_DESCRIPTION_CHARS) == "x" * MAX_DESCRIPTION_CHARS
    with pytest.raises(TemplateValidationError) as exc:
        validate_description("x" * (MAX_DESCRIPTION_CHARS + 1))
    assert exc.value.field == "description"


@pytest.mark.parametrize("bad", ["\u202e", "\u200b", "\x00", "\x1b[31m"])
def test_control_and_format_characters_are_rejected(bad):
    with pytest.raises(TemplateValidationError) as exc:
        validate_name(f"Design{bad}Lite")
    assert exc.value.field == "name"
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections([_section(heading=f"Sum{bad}mary")])
    assert exc.value.field == "sections[0].heading"
    with pytest.raises(TemplateValidationError) as exc:
        validate_sections([_section(guidance=f"Lead{bad} with the why")])
    assert exc.value.field == "sections[0].guidance"
    with pytest.raises(TemplateValidationError) as exc:
        validate_description(f"About{bad}this")
    assert exc.value.field == "description"


def test_newlines_and_tabs_are_only_allowed_in_multiline_fields():
    sections = validate_sections([_section(guidance="Lead with\n\tthe why")])
    assert sections[0]["guidance"] == "Lead with\n\tthe why"
    with pytest.raises(TemplateValidationError):
        validate_name("Design\nLite")
    with pytest.raises(TemplateValidationError):
        validate_sections([_section(heading="Sum\tmary")])
