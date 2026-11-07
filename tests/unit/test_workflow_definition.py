"""Workflow template definition v1: validation matrix and brief rendering."""

from __future__ import annotations

import pytest

from sessionfs.server.services.workflows import (
    WorkflowDefinitionError,
    parse_definition,
    render_brief,
)


def _step(sid: str, **over) -> dict:
    return {"id": sid, "title": sid.title(), "role": "implementer", "agent": "atlas", **over}


def test_valid_dag_parses():
    d = parse_definition(
        {
            "steps": [
                _step("dispatch", role="dispatcher"),
                _step("implement", depends_on=["dispatch"], fan_out=3),
                _step("review", role="reviewer", depends_on=["implement"], gate="trusted_clean"),
                _step("verify", role="verifier", depends_on=["review"], gate="approval_policy:sec_1"),
            ]
        }
    )
    assert [s.id for s in d.steps] == ["dispatch", "implement", "review", "verify"]
    assert d.step("implement").fan_out == 3


@pytest.mark.parametrize(
    "steps, needle",
    [
        ([_step("a", depends_on=["b"]), _step("b", depends_on=["a"])], "cycle"),
        (
            [_step("a", depends_on=["c"]), _step("b", depends_on=["a"]), _step("c", depends_on=["b"])],
            "cycle",
        ),
        ([_step("a", depends_on=["a"])], "itself"),
        ([_step("a", depends_on=["zzz"])], "unknown"),
        ([_step("a"), _step("a")], "duplicate"),
        ([_step("a", depends_on=[]), _step("b", depends_on=["a", "a"])], "twice"),
        ([_step(f"s{i}") for i in range(31)], "30"),
        ([], "at least 1"),
        ([_step("a", fan_out=11)], "10"),
        ([_step("a", fan_out=0)], "1"),
        ([_step("a", fan_out=True)], "integer"),
        ([_step("a", gate="maybe")], "gate"),
        ([_step("a", gate="approval_policy:")], "gate"),
        ([_step("a", gate="approval_policy:x y")], "gate"),
        ([_step("a", role="boss")], "role"),
        ([_step("Bad_ID")], "slug"),
        ([_step("a", agent="no spaces")], "persona"),
        ([_step("a", brief_template="x" * (8 * 1024 + 1))], "8192"),
        ([_step("a", extra="nope")], "extra"),
        ([_step("a"), _step("r", role="reviewer", depends_on=["a"], fan_out=2)], "fan_out"),
    ],
)
def test_invalid_definitions(steps, needle):
    with pytest.raises(WorkflowDefinitionError) as exc:
        parse_definition({"steps": steps})
    assert needle.lower() in str(exc.value).lower()


def test_definition_size_cap():
    steps = [_step(f"s{i}", brief_template="x" * 8000) for i in range(5)]
    with pytest.raises(WorkflowDefinitionError, match="32768"):
        parse_definition({"steps": steps})


def test_unknown_version_and_bad_json():
    with pytest.raises(WorkflowDefinitionError):
        parse_definition({"version": 2, "steps": [_step("a")]})
    with pytest.raises(WorkflowDefinitionError, match="JSON"):
        parse_definition("{not json")


def test_brief_secrets_redacted_and_lines_kept():
    d = parse_definition(
        {"steps": [_step("a", brief_template="line one\nkey AKIAIOSFODNN7EXAMPLE\n\0end")]}
    )
    brief = d.steps[0].brief_template
    assert "AKIAIOSFODNN7EXAMPLE" not in brief
    assert "\n" in brief and "\0" not in brief


def test_render_brief_is_data_only():
    out = render_brief(
        "Do {ticket.title}. {unknown} {ticket.description}\n{checklist}",
        ticket_title="T {checklist}",
        ticket_description="D {{x}} {ticket.title}",
        checklist_items=[{"title": "Trap", "check": "look"}],
    )
    # Inserted values are never re-expanded; unknown placeholders stay literal.
    assert out.startswith("Do T {checklist}. {unknown} D {{x}} {ticket.title}")
    assert "- **Trap** — look" in out
    assert "Known traps" not in out


def test_render_brief_appends_checklist_and_defaults():
    out = render_brief(
        "",
        ticket_title="T",
        ticket_description="Desc",
        checklist_items=[{"title": "Trap", "check": "look"}],
    )
    assert out == "Desc\n\n## Known traps\n- **Trap** — look"
    assert render_brief("x", ticket_title="", ticket_description="", checklist_items=[]) == "x"
