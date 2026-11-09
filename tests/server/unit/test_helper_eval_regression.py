"""Regression harness for a real helper hallucination (synthetic fixture).

A ticket description says "until then runs are dry runs" and names a ticket
that was finished weeks earlier; one agent run failed on 11-02 and later runs
passed; the failure is explained in an early comment of a long thread.  The
helper once answered that the reviews were dry runs and the failure was never
explained.

These tests need no model: they check what the tools now return for that
fixture (dated description, referenced ticket status, truncated comment
thread, dated runs, evidence ids) and that the live eval script
(scripts/helper_eval_afromarket.py) runs end to end with a fake provider.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from sessionfs.judge.agent_stream import Done, TextDelta, ToolCall, ToolCallEvent, ToolResultEvent
from sessionfs.server.helper.grounding import NOT_VERIFIED
from tests.server.unit.test_helper_coverage import RecordingSink, _ctx, _run

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "helper_eval_afromarket.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("helper_eval_afromarket", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evalmod = _load_script()


@pytest.mark.asyncio
async def test_ticket_shows_dated_description_and_done_dependency(db_session, test_user):
    fx = await evalmod.seed_fixture(db_session, test_user)
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    out = (await _run(ctx, "get_ticket", {"ticket_id": fx.ticket_a})).content
    # The stale prose is labelled with when it was written.
    assert "description (written" in out and "may be out of" in out
    assert "dry runs" in out
    # The ticket it waits on is shown with its CURRENT status and date.
    ref = next(line for line in out.splitlines() if line.startswith(f"- [ticket:{fx.ticket_b}]"))
    assert "status: done" in ref and "resolved" in ref
    # The long thread is marked truncated, so "never explained" is not a
    # complete-search conclusion.
    assert "truncated — older comments are not shown" in out
    assert fx.explanation not in out


@pytest.mark.asyncio
async def test_agent_runs_show_the_failure_and_later_passes_with_dates(db_session, test_user):
    fx = await evalmod.seed_fixture(db_session, test_user)
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    out = (
        await _run(ctx, "list_agent_runs", {"project_id": fx.project_id, "ticket_id": fx.ticket_a})
    ).content
    assert out.count("failed") >= 1
    assert "passed" in out
    assert fx.failure_day.date().isoformat() in out


@pytest.mark.asyncio
async def test_short_thread_is_marked_complete(db_session, test_user):
    from tests.server.unit.test_helper_coverage import _make_project, _make_ticket

    project = await _make_project(db_session, owner=test_user)
    ticket = await _make_ticket(db_session, project, title="Short")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    out = (await _run(ctx, "get_ticket", {"ticket_id": ticket.id})).content
    assert "comments: none" in out


def test_score_answer_flags_the_original_hallucination():
    bad = (
        "Yes — the daily probe reviews are dry runs, so nothing is sent. "
        "The 11-02 failure was never explained."
    )
    s = evalmod.score_answer(bad, None, ["comments (last 10 of 43; truncated — older comments are not shown):"])
    assert s["claims_dry_runs"] and s["absence_without_complete_search"]
    assert s["state_without_dated_source"]
    good = (
        "No: the description saying runs are dry runs was written before tk_b was "
        "done, and that ticket was resolved on 2026-10-12 [ticket:tk_0123456789abcdef]."
    )
    g = evalmod.score_answer(good, None, [])
    assert not g["claims_dry_runs"] and not g["absence_without_complete_search"]


def test_missing_key_exits_with_a_message(monkeypatch: pytest.MonkeyPatch, capsys):
    for name in ("SFS_EVAL_API_KEY", "SFS_EVAL_MODEL", "SFS_EVAL_BASE_URL", "SFS_EVAL_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    assert evalmod.main(["--runs", "1"]) == 2
    assert "SFS_EVAL_API_KEY" in capsys.readouterr().err
    resolved = evalmod.resolved_from_env(
        {"SFS_EVAL_API_KEY": "k", "SFS_EVAL_MODEL": "m", "SFS_EVAL_BASE_URL": "https://x.example/v1"}
    )
    assert resolved.provider == "openai" and resolved.base_url == "https://x.example/v1"


@pytest.mark.asyncio
async def test_eval_runs_end_to_end_with_a_fake_provider():
    """The whole script path: seed → API turn → stored tool rows → score."""
    from sessionfs.server.helper.keys import ResolvedLLM

    def fake_agent(answer_for):
        async def _agent(*, execute_tool, messages, **kwargs):
            question = messages[-1]["content"] if isinstance(messages[-1].get("content"), str) else ""
            assert "dry runs" in str(question)
            scope_line = next(
                line for line in kwargs["system"].splitlines() if "proj_" in line
            )
            project_id = "proj_" + scope_line.split("proj_", 1)[1][:16]
            listing = ToolCall(id="c1", name="list_tickets", arguments={"project_id": project_id})
            yield ToolCallEvent(listing)
            first = await execute_tool(listing)
            yield ToolResultEvent(listing, first)
            assert first.content.startswith("[evidence e")
            ticket_id = first.content.split("[ticket:", 2)[1][:19]
            detail = ToolCall(id="c2", name="get_ticket", arguments={"ticket_id": ticket_id})
            yield ToolCallEvent(detail)
            yield ToolResultEvent(detail, await execute_tool(detail))
            yield TextDelta(answer_for())
            yield Done("stop")

        return _agent

    resolved = ResolvedLLM(
        provider="openai", model="fake", api_key="sk-fake", base_url=None, key_source="user"
    )
    answers = iter(
        [
            "The reviews are live now; the ticket that enabled live runs is done [e2]. "
            "The failure is still dry runs.",
            "They are dry runs. The 11-02 failure was never explained.",
        ]
    )
    rows = await evalmod.run_eval(2, resolved, agent=fake_agent(lambda: next(answers)))
    assert len(rows) == 2
    first, second = rows
    assert NOT_VERIFIED in first["answer"]
    assert 0 < first["evidence_share"] < 1
    assert second["claims_dry_runs"] and second["absence_without_complete_search"]
    table = evalmod.format_table(rows)
    assert "dry-run claim" in table and "of 2 answers had a bad claim" in table


# Real answers from the fixed helper on deepseek-v4-pro (synthetic fixture).
# Each one is correct: it reports the stale description, says the dry-run
# period ended, and admits the comment thread was truncated. The scorer must
# not flag them.
_GOOD_LIVE_ANSWERS = [
    (
        "## Dry runs?\n\nNot anymore. The \"Daily probe reviews\" ticket description says runs are "
        "dry runs only \"until tk_ca152b3dda974c9c lands\" [ticket:tk_07dc09acbef94e7b]. That ticket "
        "is now done, resolved 2026-10-12 [ticket:tk_ca152b3dda974c9c]. So currently they're live "
        "probes, not dry runs (not verified).\n\n## The 11-02 failure\n\nI found no further "
        "explanation anywhere. Note the ticket has 43 comments and only the last 10 are shown "
        "(older ones truncated), so I can't rule out an explanation buried in earlier comments."
    ),
    (
        "The description (written 2026-09-18) says: *\"Until tk_ca152b3dda974c9c lands, runs are dry "
        "runs\"* [ticket:tk_07dc09acbef94e7b]. That enabling ticket is now done, resolved 2026-10-12 "
        "[ticket:tk_ca152b3dda974c9c].\n\nI found no explanation of the failure beyond that one-line "
        "symptom in anything I checked. The ticket has 43 comments but only the last 10 are shown "
        "to me, so if an explanation was written in an earlier comment, I can't see it."
    ),
]


@pytest.mark.parametrize("answer", _GOOD_LIVE_ANSWERS)
def test_score_answer_accepts_correct_live_answers(answer: str):
    tools = ["comments (last 10 of 43; truncated — older comments are not shown):"]
    s = evalmod.score_answer(answer, None, tools)
    assert not s["claims_dry_runs"], s
    assert not s["absence_without_complete_search"], s
    assert not s["state_without_dated_source"], s
