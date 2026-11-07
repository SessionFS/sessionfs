"""Unit tests for `sfs workflow …` and `--step` on the loop worker commands
(GitHub #120 W2). Network is never touched: `_api_request` is patched and
the tests assert on the method, path and body each command sends."""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_loop import loop_app
from sessionfs.cli.cmd_workflow import load_template_file, workflow_app


class _FakeApi:
    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        self.calls: list[dict] = []

    async def __call__(self, method, path, api_url, api_key, json_data=None,
                       extra_headers=None, params=None, timeout=30):
        self.calls.append({"method": method, "path": path, "json_data": json_data})
        body = self.body(method, path) if callable(self.body) else self.body
        return self.status, body, {}


def _invoke(app, fake: _FakeApi, argv: list[str]):
    with (
        patch(
            "sessionfs.cli.cmd_loop._resolve_project",
            return_value=("https://api.test", "key", "proj_x"),
        ),
        patch(
            "sessionfs.cli.cmd_workflow._resolve_project",
            return_value=("https://api.test", "key", "proj_x"),
        ),
        patch("sessionfs.cli.cmd_workflow._get_api_config", return_value=("https://api.test", "key")),
        patch("sessionfs.cli.cmd_workflow._api_request", new=fake),
        patch("sessionfs.cli.cmd_loop._api_request", new=fake),
    ):
        return CliRunner().invoke(app, argv)


YAML_TEMPLATE = """\
name: Ship a change
steps:
  - id: dispatch
    title: Split the work
    role: dispatcher
    agent: scout
  - id: implement
    title: Implement
    role: implementer
    agent: atlas
    depends_on: [dispatch]
    fan_out: 2
    brief_template: |
      Build {ticket.title}.
      {checklist}
  - id: review
    title: Review
    role: reviewer
    agent: codex-reviewer
    depends_on: [implement]
    gate: trusted_clean
"""


def test_load_yaml_and_json(tmp_path):
    y = tmp_path / "t.yaml"
    y.write_text(YAML_TEMPLATE)
    name, definition = load_template_file(str(y))
    assert name == "Ship a change"
    assert [s["id"] for s in definition["steps"]] == ["dispatch", "implement", "review"]
    j = tmp_path / "t.json"
    j.write_text(json.dumps({"definition": definition}))
    name2, definition2 = load_template_file(str(j))
    assert name2 is None and definition2 == definition


def test_create_sends_validated_definition(tmp_path):
    y = tmp_path / "t.yml"
    y.write_text(YAML_TEMPLATE)
    fake = _FakeApi(201, {"id": "wft_1", "name": "Ship a change", "version": 1})
    result = _invoke(workflow_app, fake, ["create", str(y)])
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/projects/proj_x/workflow-templates"
    assert call["json_data"]["name"] == "Ship a change"
    assert call["json_data"]["definition"]["steps"][1]["fan_out"] == 2
    assert "wft_1" in result.output


def test_create_org_template(tmp_path):
    y = tmp_path / "t.yml"
    y.write_text(YAML_TEMPLATE)
    fake = _FakeApi(201, {"id": "wft_2"})
    result = _invoke(workflow_app, fake, ["create", str(y), "--org", "org_1", "--name", "Other"])
    assert result.exit_code == 0, result.output
    assert fake.calls[0]["path"] == "/api/v1/orgs/org_1/workflow-templates"
    assert fake.calls[0]["json_data"]["name"] == "Other"


def test_bad_templates_refused_before_any_request(tmp_path):
    cases = {
        "cycle.yaml": (
            "name: x\nsteps:\n"
            "  - {id: a, title: A, role: implementer, agent: atlas, depends_on: [b]}\n"
            "  - {id: b, title: B, role: implementer, agent: atlas, depends_on: [a]}\n"
        ),
        "fanout.yaml": "name: x\nsteps:\n  - {id: a, title: A, role: implementer, agent: atlas, fan_out: 11}\n",
        "gate.yaml": "name: x\nsteps:\n  - {id: a, title: A, role: implementer, agent: atlas, gate: maybe}\n",
        "extra.yaml": "name: x\nsteps:\n  - {id: a, title: A, role: implementer, agent: atlas, run: rm}\n",
        "notyaml.yaml": "name: [unclosed\n",
        "list.json": "[1, 2]",
        "tagged.yaml": "name: !!python/object/apply:os.system ['true']\nsteps: []\n",
    }
    for fname, text in cases.items():
        f = tmp_path / fname
        f.write_text(text)
        fake = _FakeApi(201, {})
        result = _invoke(workflow_app, fake, ["create", str(f)])
        assert result.exit_code == 1, (fname, result.output)
        assert fake.calls == [], fname
        assert "Invalid workflow template" in result.output, fname


def test_create_requires_a_name(tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"steps": [{"id": "a", "title": "A", "role": "implementer", "agent": "atlas"}]}))
    fake = _FakeApi(201, {})
    result = _invoke(workflow_app, fake, ["create", str(f)])
    assert result.exit_code == 1 and fake.calls == []


def test_update_keeps_current_name(tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"steps": [{"id": "a", "title": "A", "role": "implementer", "agent": "atlas"}]}))

    def body(method, path):
        return {"id": "wft_1", "name": "Current"} if method == "GET" else {"id": "wft_2", "version": 2}

    fake = _FakeApi(200, body)
    result = _invoke(workflow_app, fake, ["update", "wft_1", str(f)])
    assert result.exit_code == 0, result.output
    assert [c["method"] for c in fake.calls] == ["GET", "PUT"]
    assert fake.calls[1]["path"] == "/api/v1/projects/proj_x/workflow-templates/wft_1"
    assert fake.calls[1]["json_data"]["name"] == "Current"


def test_disable_and_list_and_show():
    fake = _FakeApi(204, "")
    assert _invoke(workflow_app, fake, ["disable", "wft_1"]).exit_code == 0
    assert fake.calls[0]["method"] == "DELETE"
    row = {"id": "wft_1", "name": "N", "version": 1, "scope": "project", "active": True,
           "definition": {"version": 1, "steps": [{"id": "a", "role": "implementer", "agent": "atlas",
                                                    "depends_on": [], "fan_out": 1, "gate": "none",
                                                    "brief_template": "Do it"}]}}
    fake = _FakeApi(200, [row])
    result = _invoke(workflow_app, fake, ["list"])
    assert result.exit_code == 0 and "wft_1" in result.output
    fake = _FakeApi(200, row)
    result = _invoke(workflow_app, fake, ["show", "wft_1", "--org", "org_1"])
    assert result.exit_code == 0 and "Do it" in result.output
    assert fake.calls[0]["path"] == "/api/v1/orgs/org_1/workflow-templates/wft_1"


def test_show_404():
    fake = _FakeApi(404, {"detail": "Workflow template not found"})
    result = _invoke(workflow_app, fake, ["show", "wft_9"])
    assert result.exit_code == 1 and "not found" in result.output


def test_start_output_id():
    fake = _FakeApi(201, {"loop_id": "tk_loop", "steps": []})
    result = _invoke(
        workflow_app,
        fake,
        ["start", "wft_1", "--ticket", "tk_issue", "--token-budget", "5000",
         "--max-review-rounds", "3", "--reviewer-persona", "codex-reviewer", "--output-id"],
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "tk_loop"
    call = fake.calls[0]
    assert call["path"] == "/api/v1/projects/proj_x/workflows"
    assert call["json_data"] == {
        "template_id": "wft_1",
        "ticket_id": "tk_issue",
        "reviewer_persona": "codex-reviewer",
        "token_budget": 5000,
        "max_review_rounds": 3,
    }


def test_start_needs_ticket_or_title():
    fake = _FakeApi(201, {})
    result = _invoke(workflow_app, fake, ["start", "wft_1"])
    assert result.exit_code == 1 and fake.calls == []


def test_start_api_error():
    fake = _FakeApi(409, {"detail": {"error": "workflow_template_inactive", "message": "not active"}})
    result = _invoke(workflow_app, fake, ["start", "wft_1", "--title", "x"])
    assert result.exit_code == 1 and "not active" in result.output


PLAN = {
    "loop_id": "tk_loop",
    "template": {"id": "wft_1", "name": "Ship", "version": 1},
    "steps": [
        {"step_id": "dispatch", "role": "dispatcher", "agent": "scout", "gate": "none",
         "state": "done", "reason": None, "gate_status": {"passed": True, "reason": None},
         "ready_children": []},
        {"step_id": "implement", "role": "implementer", "agent": "atlas", "gate": "none",
         "state": "ready", "reason": None, "gate_status": {"passed": True, "reason": None},
         "ready_children": [{"ticket_id": "tk_c1", "index": 0, "agent": "atlas",
                             "brief_markdown": "Build the parser"}]},
        {"step_id": "review", "role": "reviewer", "agent": "codex-reviewer", "gate": "trusted_clean",
         "state": "blocked", "reason": "waiting for: implement",
         "gate_status": {"passed": False, "reason": None}, "ready_children": []},
    ],
    "ready_steps": ["implement"],
    "done": False,
}


def test_plan_prints_ready_steps_with_briefs_and_agents():
    fake = _FakeApi(200, PLAN)
    result = _invoke(workflow_app, fake, ["plan", "tk_loop"])
    assert result.exit_code == 0, result.output
    assert fake.calls[0]["path"] == "/api/v1/projects/proj_x/workflows/tk_loop/plan"
    out = result.output
    assert "Build the parser" in out
    assert "tk_c1" in out and "agent=atlas" in out
    assert "waiting" in out


def test_plan_json():
    fake = _FakeApi(200, PLAN)
    result = _invoke(workflow_app, fake, ["plan", "tk_loop", "--json"])
    assert json.loads(result.output)["ready_steps"] == ["implement"]


def test_loop_commands_forward_step():
    fake = _FakeApi(200, {"run_id": "run_1"})
    assert _invoke(loop_app, fake, ["run", "start", "tk_c1", "--step", "implement", "--output-id"]).exit_code == 0
    assert fake.calls[-1]["json_data"]["step_id"] == "implement"
    assert _invoke(loop_app, fake, ["run", "start", "tk_c1"]).exit_code == 0
    assert "step_id" not in fake.calls[-1]["json_data"]
    assert _invoke(
        loop_app, fake, ["run", "finish", "run_1", "--head-sha", "a" * 40, "--step", "implement"]
    ).exit_code == 0
    assert fake.calls[-1]["json_data"]["step_id"] == "implement"
    assert _invoke(
        loop_app, fake,
        ["review", "tk_loop", "--ticket", "tk_c1", "--round", "1", "--commit", "a" * 40,
         "--verdict", "VERIFIED-CLEAN", "--step", "review"],
    ).exit_code == 0
    assert fake.calls[-1]["json_data"]["step_id"] == "review"


def test_worker_review_loop_skill_validates_with_workflow_section():
    from pathlib import Path

    from sessionfs.skills import publishing

    root = Path(__file__).resolve().parents[2] / "catalog" / "sessionfs-workflows"
    skill = root / "skills" / "worker-review-loop"
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    assert "## 9. Follow a workflow template" in text
    for name in ("start_workflow", "get_workflow_plan", "step_id"):
        assert name in text
    assert json.loads((root / "plugin.json").read_text())["version"] == "2.1.0"
    for source in (skill, root):
        result = publishing.validate_source(source, visibility="personal")
        assert result.ok, result.problems


def test_site_workflows_page_example_is_a_valid_template(tmp_path):
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    page = (root / "site" / "src" / "content" / "docs" / "workflows.mdx").read_text(encoding="utf-8")
    example = re.search(r"```yaml\n(.*?)```", page, re.S).group(1)
    f = tmp_path / "ship.yaml"
    f.write_text(example)
    name, definition = load_template_file(str(f))
    assert name == "Ship a change"
    steps = {s["id"]: s for s in definition["steps"]}
    assert list(steps) == ["dispatch", "implement", "review", "verify", "document"]
    assert steps["implement"]["fan_out"] == 3
    assert steps["review"]["gate"] == "trusted_clean"
    assert steps["verify"]["gate"].startswith("approval_policy:")
    config = (root / "site" / "astro.config.mjs").read_text(encoding="utf-8")
    assert "slug: 'workflows'" in config


# ── Review fixes: the CLI refuses YAML the dashboard would read differently ──

import pytest  # noqa: E402

from sessionfs.workflow_definition import WorkflowDefinitionError  # noqa: E402

_STEP = (
    "name: T\nsteps:\n  - id: a\n    title: A\n    role: implementer\n    agent: atlas\n"
)


@pytest.mark.parametrize(
    "extra, needle",
    [
        ("    depends_on: &deps [b]\n", "anchors"),
        ("    gate: *g\n", "aliases"),
        ("    fan_out: !!int 2\n", "tags"),
        ("    brief_template: |2\n      x\n", "indentation indicators"),
        ("    brief_template: >\n      a\n        b\n", "more-indented"),
        ("    brief_template: |\n      a\n        \n      b\n", "trailing spaces"),
        ("    title: yes\n", "true, false or null"),
        ("    title: Off\n", "true, false or null"),
        ("    title: y\n", "true, false or null"),
        ("    title: True\n", "true, false or null"),
        ("    title: NULL\n", "true, false or null"),
        ("    fan_out: 1.5\n", "not a plain integer"),
        ("    fan_out: 0x1f\n", "not a plain integer"),
        ("    fan_out: 012\n", "leading zero"),
        ("    title: 2026-11-06\n", "not a plain integer"),
    ],
)
def test_yaml_ambiguities_refused(tmp_path, extra, needle):
    # Replace the step's own key where the extra line repeats one.
    key = extra.strip().split(":", 1)[0]
    text = "".join(
        line + "\n" for line in _STEP.splitlines() if not line.strip().startswith(f"{key}:")
    ) + extra
    f = tmp_path / "t.yaml"
    f.write_text(text)
    with pytest.raises(WorkflowDefinitionError, match=needle):
        load_template_file(str(f))


def test_yaml_unambiguous_forms_still_load(tmp_path):
    f = tmp_path / "t.yaml"
    f.write_text(
        _STEP.replace("title: A", 'title: "yes"')
        + "    fan_out: 2\n    gate: none\n    brief_template: |-\n      a\n\n        b\n"
    )
    name, definition = load_template_file(str(f))
    step = definition["steps"][0]
    assert name == "T" and step["title"] == "yes" and step["fan_out"] == 2
    assert step["brief_template"] == "a\n\n  b"


def test_plan_renders_hidden_org_template_fields():
    plan = {
        "loop_id": "tk_loop",
        "template": {"id": "wft_1", "lineage_id": "wfl_1", "name": None, "version": 1},
        "steps": [
            {
                "step_id": "implement", "title": None, "role": "implementer", "agent": None,
                "depends_on": [], "fan_out": 1, "gate": None, "state": "ready", "reason": None,
                "gate_status": {"passed": True, "reason": None}, "children": [], "reviews": [],
                "ready_children": [],
            }
        ],
        "ready_steps": ["implement"],
        "done": False,
    }
    result = _invoke(workflow_app, _FakeApi(200, plan), ["plan", "tk_loop"])
    assert result.exit_code == 0, result.output
    assert "None" not in result.output and "workflow v1" in result.output
