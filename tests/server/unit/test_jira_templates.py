"""Unit tests for the built-in Jira story templates."""

from sessionfs.server.integrations.trackers.templates import (
    DEFAULT_TEMPLATE,
    SOURCE_FIELDS,
    get_template,
    is_builtin_slug,
    list_templates,
)


def test_builtin_template_slugs():
    slugs = {t.slug for t in list_templates()}
    assert slugs == {"agile", "compliance", "sre", "bug", "design"}


def test_default_template_is_agile():
    assert DEFAULT_TEMPLATE == "agile"
    assert get_template(DEFAULT_TEMPLATE) is not None


def test_get_template_returns_none_for_unknown():
    assert get_template("nope") is None


def test_is_builtin_slug():
    assert is_builtin_slug("agile")
    assert not is_builtin_slug("custom")


def test_every_engineered_template_has_verbatim_acceptance_criteria():
    for template in list_templates():
        if template.slug == "design":
            continue
        sections = {s.key: s for s in template.sections}
        assert "acceptance_criteria" in sections
        assert sections["acceptance_criteria"].mode == "verbatim"
        assert sections["acceptance_criteria"].source_fields == ("acceptance_criteria",)


def test_every_engineered_template_has_a_rewrite_summary():
    for template in list_templates():
        if template.slug == "design":
            continue
        summary = template.sections[0]
        assert summary.key == "summary"
        assert summary.mode == "rewrite"
        assert "title" in summary.source_fields


def test_design_template_sections():
    design = get_template("design")
    assert design is not None
    assert design.name == "Design / UX"
    sections = {s.key: s for s in design.sections}
    assert list(sections) == ["user_journey", "hypothesis", "success_metric", "context"]

    assert sections["user_journey"].mode == "rewrite"
    assert sections["user_journey"].source_fields == ("description",)

    assert sections["hypothesis"].mode == "rewrite"
    assert sections["hypothesis"].source_fields == ("description",)

    # The success metric is verbatim from the acceptance criteria, never
    # invented, and falls back to an explicit empty_text when absent.
    assert sections["success_metric"].mode == "verbatim"
    assert sections["success_metric"].source_fields == ("acceptance_criteria",)
    assert sections["success_metric"].empty_text == "Not specified in the source ticket"

    assert sections["context"].mode == "rewrite"
    assert sections["context"].source_fields == ("description", "linked_sessions")


def test_section_modes_are_valid():
    for template in list_templates():
        for section in template.sections:
            assert section.mode in {"verbatim", "rewrite"}
            assert section.heading
            assert section.source_fields
            # Every source field is drawn from the fixed vocabulary.
            assert set(section.source_fields) <= set(SOURCE_FIELDS)


def test_source_fields_vocabulary_is_the_canonical_set():
    assert SOURCE_FIELDS == (
        "title",
        "description",
        "acceptance_criteria",
        "priority",
        "kind",
        "persona",
        "completion_notes",
        "linked_sessions",
    )


def test_guidance_and_empty_text_default_to_none():
    for template in list_templates():
        for section in template.sections:
            assert section.guidance is None
