"""Unit tests for the built-in Jira story templates."""

from sessionfs.server.integrations.trackers.templates import (
    DEFAULT_TEMPLATE,
    get_template,
    list_templates,
)


def test_four_builtin_templates():
    slugs = {t.slug for t in list_templates()}
    assert slugs == {"agile", "compliance", "sre", "bug"}


def test_default_template_is_agile():
    assert DEFAULT_TEMPLATE == "agile"
    assert get_template(DEFAULT_TEMPLATE) is not None


def test_get_template_returns_none_for_unknown():
    assert get_template("nope") is None


def test_every_template_has_verbatim_acceptance_criteria():
    for template in list_templates():
        sections = {s.key: s for s in template.sections}
        assert "acceptance_criteria" in sections
        assert sections["acceptance_criteria"].mode == "verbatim"
        assert sections["acceptance_criteria"].source_fields == ("acceptance_criteria",)


def test_every_template_has_a_rewrite_summary():
    for template in list_templates():
        summary = template.sections[0]
        assert summary.key == "summary"
        assert summary.mode == "rewrite"
        assert "title" in summary.source_fields


def test_section_modes_are_valid():
    for template in list_templates():
        for section in template.sections:
            assert section.mode in {"verbatim", "rewrite"}
            assert section.heading
            assert section.source_fields
