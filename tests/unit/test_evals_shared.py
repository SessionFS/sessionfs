"""Evaluations + lineage: the shared definition module, the URI validator,
the CLI file loaders and commands, and the MCP tool handlers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sessionfs.artifact_uri import ArtifactUriError, link_url, validate_uri
from sessionfs.eval_definition import (
    EvalDefinitionError,
    case_content_sha256,
    parse_case,
    parse_definition,
    parse_result,
)


def _suite(**over) -> dict:
    base = {"slug": "clinical", "name": "Clinical", "grader": {"kind": "llm_judge", "persona": "grader"}}
    base.update(over)
    return base


def test_definition_defaults_and_strictness():
    d = parse_definition(_suite())
    assert d.acceptance.require_all_cases is True
    assert d.acceptance.no_regression_vs == "none"
    assert d.acceptance.regression_tags == ["critical"]
    assert d.case_storage == "inline"
    for bad in (
        _suite(slug="Not A Slug"),
        _suite(grader={"kind": "oracle", "persona": "g"}),
        _suite(grader={"kind": "human", "persona": "bad persona!"}),
        _suite(acceptance={"min_pass_rate": 1.5}),
        _suite(acceptance={"min_pass_rate": True}),
        _suite(acceptance={"max_failures_by_tag": {"critical": -1}}),
        _suite(acceptance={"no_regression_vs": "yesterday"}),
        _suite(acceptance={"unknown": 1}),
        _suite(case_bank_artifact_id="art_1"),
        _suite(grader={"kind": "llm_judge", "persona": "g", "rubric": "x" * 9000}),
        {**_suite(), "extra": 1},
    ):
        with pytest.raises(EvalDefinitionError):
            parse_definition(bad)
    ok = parse_definition(_suite(acceptance={"no_regression_vs": "evr_0123456789abcdef"}))
    assert ok.acceptance.no_regression_vs == "evr_0123456789abcdef"


def test_case_and_result_specs():
    h = case_content_sha256("in", "out")
    assert h == case_content_sha256("in", "out") and h != case_content_sha256("in", None)
    assert parse_case({"case_key": "c1", "tags": ["b", "a", "a"]}).tags == ["a", "b"]
    assert parse_case({"case_key": "c1", "content_sha256": "SHA256:" + h.upper()}).content_sha256 == h
    for bad in ({"case_key": "C 1"}, {"case_key": "c", "tags": ["Bad Tag"]}, {"case_key": "c", "content_sha256": "zz"}):
        with pytest.raises(EvalDefinitionError):
            parse_case(bad)
    assert parse_result({"case_key": "c1", "verdict": "pass", "score": 1}).score == 1.0
    for bad in (
        {"case_key": "c1", "verdict": "ok"},
        {"case_key": "c1", "verdict": "pass", "score": 2},
        {"case_key": "c1", "verdict": "pass", "score": False},
        {"case_key": "c1", "verdict": "pass", "output": "x" * 17000},
    ):
        with pytest.raises(EvalDefinitionError):
            parse_result(bad)


@pytest.mark.parametrize(
    "uri,code",
    [
        ("ftp://h/x", "invalid_uri_scheme"),
        ("javascript:alert(1)", "invalid_uri_scheme"),
        ("https://u:p@h/x", "uri_has_credentials"),
        ("https://h/x?X-Amz-Credential=a", "uri_has_credentials"),
        ("https://h/x?sv=1&se=2&sig=3", "uri_has_credentials"),
        ("https://h/x?access_token=a", "uri_has_credentials"),
        ("https://h/x?apiKey=a", "uri_has_credentials"),
        ("https://h/a b", "invalid_uri"),
        ("https:///path", "invalid_uri"),
        ("s3:", "invalid_uri"),
    ],
)
def test_uri_refusals(uri, code):
    with pytest.raises(ArtifactUriError) as exc:
        validate_uri(uri)
    assert exc.value.code == code


def test_uri_accepts_and_links():
    assert validate_uri("S3://bucket/key") == "s3://bucket/key"
    assert validate_uri("models:/clin/3") == "models:/clin/3"
    assert validate_uri("runs:/abc123/model") == "runs:/abc123/model"
    assert validate_uri("https://h/x?revision=main") == "https://h/x?revision=main"
    assert link_url("https://h/x") == "https://h/x"
    assert link_url("hf://acme/model") == "https://huggingface.co/acme/model"
    assert link_url("hf://datasets/acme/notes") == "https://huggingface.co/datasets/acme/notes"
    assert link_url("hf://acme/../x") is None
    assert link_url("s3://b/k") is None
    assert link_url("gs://b/k") is None


# ── CLI ──


def test_load_suite_file_yaml_and_reference_hashing(tmp_path: Path):
    from sessionfs.cli.cmd_eval import load_suite_file

    f = tmp_path / "suite.yaml"
    f.write_text(
        "slug: phi\nname: PHI notes\ngrader:\n  kind: deterministic\n  persona: ci-grader\n"
        "case_storage: reference\ncases:\n  - case_key: p1\n    input: secret note\n    expected: out\n",
        encoding="utf-8",
    )
    definition, cases = load_suite_file(str(f))
    assert definition["case_storage"] == "reference"
    # The text never leaves the machine; only its hash is sent.
    assert cases == [{"case_key": "p1", "name": "", "rubric_notes": "", "tags": [], "content_sha256": case_content_sha256("secret note", "out")}]

    bad = tmp_path / "bad.yaml"
    bad.write_text("slug: x\nname: y\ngrader: &a\n  kind: human\n  persona: p\n", encoding="utf-8")
    with pytest.raises(EvalDefinitionError):
        load_suite_file(str(bad))
    j = tmp_path / "s.json"
    j.write_text(json.dumps({**_suite(), "acceptance": {"vibes": 1}}), encoding="utf-8")
    with pytest.raises(EvalDefinitionError):
        load_suite_file(str(j))


def test_cli_compare_exits_1_on_regression(monkeypatch):
    from sessionfs.cli import cmd_eval
    from sessionfs.cli.main import app

    monkeypatch.setattr(
        cmd_eval, "_call", lambda method, tail, **kw: {"regression_count": 1, "fix_count": 0, "regressions": ["c1"], "warnings": []}
    )
    result = CliRunner().invoke(app, ["eval", "compare", "evr_a", "evr_b"])
    assert result.exit_code == 1 and "c1" in result.output
    monkeypatch.setattr(cmd_eval, "_call", lambda method, tail, **kw: {"regression_count": 0, "fix_count": 2})
    assert CliRunner().invoke(app, ["eval", "compare", "evr_a", "evr_b"]).exit_code == 0


def test_cli_submit_validates_locally(tmp_path: Path, monkeypatch):
    from sessionfs.cli import cmd_eval
    from sessionfs.cli.main import app

    calls: list = []
    monkeypatch.setattr(cmd_eval, "_call", lambda method, tail, **kw: calls.append((method, tail, kw)) or {"recorded": 1})
    f = tmp_path / "r.jsonl"
    f.write_text('{"case_key": "c1", "verdict": "maybe"}\n', encoding="utf-8")
    result = CliRunner().invoke(app, ["eval", "run", "submit", "evr_1", "--from", str(f)])
    assert result.exit_code == 1 and calls == []
    f.write_text('{"case_key": "c1", "verdict": "pass"}\n\n{"case_key": "c2", "verdict": "fail"}\n', encoding="utf-8")
    result = CliRunner().invoke(app, ["eval", "run", "submit", "evr_1", "--from", str(f)])
    assert result.exit_code == 0, result.output
    assert calls[0][1] == "eval-runs/evr_1/results"
    assert [r["case_key"] for r in calls[0][2]["body"]["results"]] == ["c1", "c2"]


def test_cli_artifact_register_refuses_presigned_uri(monkeypatch):
    from sessionfs.cli import cmd_eval
    from sessionfs.cli.main import app

    monkeypatch.setattr(cmd_eval, "_call", lambda *a, **k: pytest.fail("no request expected"))
    result = CliRunner().invoke(
        app,
        ["artifact", "register", "--kind", "model", "--name", "m", "--version", "1", "--uri", "https://h/x?X-Amz-Signature=s"],
    )
    assert result.exit_code == 1


# ── MCP ──


@pytest.mark.asyncio
async def test_mcp_record_results_then_finish(monkeypatch):
    from sessionfs.mcp import server

    calls: list = []

    async def fake(args, method, path, **kw):
        calls.append((method, path, kw.get("body")))
        return {"recorded": 2} if path.endswith("/results") else {"id": "evr_1", "verdict": "passed"}

    monkeypatch.setattr(server, "_eval_call", fake)
    out = await server._handle_record_eval_results(
        {"run_id": "evr_1", "results": [{"case_key": "c1", "verdict": "pass"}], "finish": True, "input_tokens": 5}
    )
    assert out["run"]["verdict"] == "passed"
    assert [c[1] for c in calls] == ["eval-runs/evr_1/results", "eval-runs/evr_1/finish"]
    # Usage goes on the finish call only, so it is never counted twice.
    assert "usage" not in calls[0][2] and calls[1][2] == {"usage": {"input_tokens": 5}}
    assert (await server._handle_record_eval_results({"run_id": "evr_1"}))["error"]
    assert (await server._handle_record_eval_results({"run_id": "evr_1", "results": "x"}))["error"]
    assert (await server._handle_start_eval_run({"suite": "s"}))["error"]
    assert (await server._handle_get_artifact_lineage({"artifact_id": "a", "depth": 99}))["error"]
