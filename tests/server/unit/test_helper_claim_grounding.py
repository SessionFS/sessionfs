"""Claim-level grounding (F1) and docs availability (F11) for the session helper.

Pure tests for the evidence-id protocol (segmentation, tag conversion,
looks-factual heuristic, flags and UTF-16 offsets), the docs availability
line, and route-level turns with a scripted agent: evidence headers on tool
results, the per-sentence map in the JSON/SSE/stored answer, fallback when
the model ignores the protocol, and the ``grounding: off`` setting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import sessionfs.server.routes.helper as helper_routes
from sessionfs.judge.agent_stream import Done, TextDelta, ToolCall, ToolCallEvent, ToolResultEvent
from sessionfs.server.db.models import HelperMessage
from sessionfs.server.helper import docs as docs_mod
from sessionfs.server.helper.build_docs_index import parse_availability
from sessionfs.server.helper.grounding import (
    NOT_VERIFIED,
    EvidenceRow,
    GroundingStream,
    ground_answer,
    looks_factual,
    primary_marker,
    split_segments,
    strip_evidence_tags,
    tag_ids,
    utf16_len,
)
from sessionfs.server.helper.settings import HelperSettingsPatch, merge_settings
from tests.server.integration.test_helper_api import _fake_resolve, _hdrs, _make_user
from tests.server.unit.test_helper_coverage import _make_project, _make_ticket


def _ev(*ids: str, marker: str | None = None) -> dict[str, EvidenceRow]:
    return {i: EvidenceRow(id=i, tool="list_tickets", marker=marker) for i in ids}


# ---------------------------------------------------------------------------
# Protocol pieces
# ---------------------------------------------------------------------------


def test_tag_ids_accepts_lists_and_copied_headers():
    assert tag_ids("[e12]") == ["e12"]
    assert tag_ids("[e12, E14]") == ["e12", "e14"]
    assert tag_ids("[evidence e3]") == ["e3"]


def test_segments_keep_trailing_tags_with_their_sentence():
    segs, rest = split_segments("Ticket A is done. [e1] Next one is open [e2].\n- item [e3]\n", final=False)
    assert segs == ["Ticket A is done. [e1] ", "Next one is open [e2].\n", "- item [e3]\n"]
    assert rest == ""


def test_segments_wait_for_a_possible_trailing_tag():
    segs, rest = split_segments("Ticket A is done. [e1", final=False)
    assert segs == [] and rest == "Ticket A is done. [e1"
    segs, rest = split_segments("Version 0.29.14 shipped", final=False)
    assert segs == [] and rest == "Version 0.29.14 shipped"


def test_looks_factual():
    assert looks_factual("The run failed on 2026-11-02.")
    assert looks_factual("Ticket tk_0123456789abcdef is done.")
    assert looks_factual("Runs are dry runs.")
    assert looks_factual("It was assigned to Ada Lovelace yesterday.")
    assert not looks_factual("Here is what I found.")
    assert not looks_factual("Let me check the tickets.")
    assert not looks_factual("Do you want me to look at the runs?")
    assert not looks_factual("## Summary")
    assert not looks_factual("I didn't find it in what I checked.")


@pytest.mark.asyncio
async def test_ground_answer_flags_only_unsupported_factual_sentences():
    text = "Here is what I found. Ticket B is done [e1]. The runs are still dry runs.\n"
    out = await ground_answer(text, _ev("e1"))
    assert out.mode == "strict"
    assert "[e1]" not in out.text
    assert out.text == (
        "Here is what I found. Ticket B is done. The runs are still dry runs "
        f"{NOT_VERIFIED}.\n"
    )
    statuses = [s["status"] for s in out.sentences]
    assert statuses == ["supported", "unverified"]
    supported, unverified = out.sentences
    assert out.text[supported["start"] : supported["end"]] == "Ticket B is done."
    assert supported["evidence"] == ["e1"]
    flag = unverified["flag"]
    assert out.text[flag[0] : flag[1]] == f" {NOT_VERIFIED}"
    assert out.unverified == 1
    assert [e["id"] for e in out.evidence] == ["e1"]


@pytest.mark.asyncio
async def test_ground_answer_turns_single_subject_ids_into_markers():
    marker = "[ticket:tk_0123456789abcdef]"
    out = await ground_answer("Ticket B was resolved [e4].", _ev("e4", marker=marker))
    assert out.text == f"Ticket B was resolved {marker}."
    # A refused marker is dropped but the id still counts as evidence.

    async def refuse(_m: str) -> bool:
        return False

    out = await ground_answer("Ticket B was resolved [e4].", _ev("e4", marker=marker), marker_ok=refuse)
    assert out.text == "Ticket B was resolved."
    assert out.sentences[0]["status"] == "supported"


@pytest.mark.asyncio
async def test_unknown_ids_are_dropped_and_no_valid_id_means_fallback():
    out = await ground_answer("There are 12 open tickets [e99].", _ev("e1"))
    assert out.mode == "fallback"
    assert out.text == "There are 12 open tickets."
    assert out.sentences == [] or all(s["status"] != "unverified" for s in out.sentences)


@pytest.mark.asyncio
async def test_markers_count_as_evidence_and_code_is_never_flagged():
    text = (
        "Ticket A is open [ticket:tk_0123456789abcdef]. B passed [e1].\n"
        "```\nexit 1 on 2026-11-02\n```\n"
    )
    out = await ground_answer(text, _ev("e1"))
    assert [s["status"] for s in out.sentences] == ["supported", "supported"]
    assert NOT_VERIFIED not in out.text


@pytest.mark.asyncio
async def test_offsets_are_utf16_units():
    text = "😀 Emoji first. The run failed twice.\n"
    out = await ground_answer(text + "B passed [e1].", _ev("e1"))
    unverified = next(s for s in out.sentences if s["status"] == "unverified")
    encoded = out.text.encode("utf-16-le")
    span = encoded[unverified["start"] * 2 : unverified["end"] * 2].decode("utf-16-le")
    assert span == f"The run failed twice {NOT_VERIFIED}."
    assert utf16_len("😀") == 2


@pytest.mark.asyncio
async def test_stream_releases_whole_sentences_without_tags():
    stream = GroundingStream(_ev("e1"), None)
    out = ""
    for chunk in ["Ticket B is ", "done. [e", "1] Runs pass", "ed [e1]."]:
        out += await stream.feed(chunk)
    assert out == "Ticket B is done. "
    out += await stream.flush()
    assert out == "Ticket B is done. Runs passed."
    assert strip_evidence_tags("a [e1] b [e2, e3]") == "a b"


def test_primary_marker_only_for_single_subject_results():
    tk = "tk_0123456789abcdef"
    assert primary_marker("get_ticket", {"ticket_id": tk}, {"ticket_ids": [tk, "tk_ffffffffffffffff"]}) == f"[ticket:{tk}]"
    assert primary_marker("get_ticket", {"ticket_id": tk}, {}) is None
    assert primary_marker("list_tickets", {}, {"ticket_ids": [tk]}) is None
    assert primary_marker("get_sessionfs_doc", {}, {"doc_refs": ["quickstart#install"]}) == "[doc:quickstart#install]"


def test_grounding_setting_merges_and_resets():
    merged = merge_settings({}, HelperSettingsPatch(grounding="off"))
    assert merged == {"grounding": "off"}
    assert merge_settings(merged, HelperSettingsPatch.model_validate({"grounding": None})) == {}


def test_prompt_v23_has_evidence_rules_only_when_strict():
    assert helper_routes._HELPER_SYSTEM_PROMPT_VERSION == "v23"
    from sessionfs.server.helper.context import HelperScope

    on = helper_routes._system_prompt(HelperScope(), grounding="strict")
    off = helper_routes._system_prompt(HelperScope(), grounding="off")
    assert "[evidence e12]" in on
    assert "[evidence e12]" not in off


# ---------------------------------------------------------------------------
# F11 — docs availability
# ---------------------------------------------------------------------------


def test_parse_availability_validates_against_tiers(tmp_path: Path):
    good = "---\ntitle: X\navailability:\n  feature: session_helper\n  deployment: both\n---\nbody"
    assert parse_availability(good) == {"feature": "session_helper", "deployment": "both"}
    assert parse_availability("---\ntitle: X\n---\nbody") is None
    with pytest.raises(ValueError):
        parse_availability("---\navailability:\n  feature: not_a_feature\n---\n")
    with pytest.raises(ValueError):
        parse_availability("---\navailability:\n  deployment: moon\n---\n")


def test_availability_line():
    line = docs_mod.availability_line(
        {"feature": "session_helper", "deployment": "both"}, "team", "cloud"
    )
    assert line == (
        "Availability: Enterprise plans; your plan: Team (not available); "
        "cloud and self-hosted."
    )
    line = docs_mod.availability_line(
        {"feature": "review_loop", "deployment": "both"}, "enterprise", "cloud"
    )
    assert "Team and Enterprise plans; your plan: Enterprise (available)" in line
    assert "every plan" in docs_mod.availability_line({"feature": "meetings"}, "free", "cloud")
    line = docs_mod.availability_line({"deployment": "cloud"}, None, "self-hosted")
    assert "cloud only (not on this self-hosted server)" in line
    assert docs_mod.availability_line(None, "pro", "cloud") == (
        "Availability: not recorded for this page."
    )


def test_deployment_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SFS_DEPLOYMENT", raising=False)
    assert docs_mod.deployment_mode() == "cloud"
    monkeypatch.setenv("SFS_DEPLOYMENT", "self-hosted")
    assert docs_mod.deployment_mode() == "self-hosted"


def test_bundled_index_carries_verified_tags():
    from sessionfs.server.tiers import TIER_FEATURES

    index = docs_mod.get_docs_index()
    assert index.availability["session-helper"]["feature"] == "session_helper"
    assert index.availability["integrations/jira"]["feature"] == "jira_integration"
    assert index.availability["self-hosted"]["deployment"] == "self-hosted"
    known = set().union(*(t["features"] for t in TIER_FEATURES.values())) | {"all"}
    for slug, tag in index.availability.items():
        assert tag.get("feature", "all") in known, slug


# ---------------------------------------------------------------------------
# Route-level turns
# ---------------------------------------------------------------------------


def _agent_calling(call: ToolCall, answer: str):
    async def _fake(*, execute_tool, **kwargs):
        yield ToolCallEvent(call)
        yield ToolResultEvent(call, await execute_tool(call))
        yield TextDelta(answer)
        yield Done("stop")

    return _fake


@pytest.mark.asyncio
async def test_turn_assigns_evidence_ids_and_returns_the_map(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, owner=user)
    ticket = await _make_ticket(db_session, project, title="Enable live runs", status="done")
    call = ToolCall(id="c1", name="get_ticket", arguments={"ticket_id": ticket.id})
    monkeypatch.setattr(helper_routes, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        helper_routes,
        "run_agent",
        _agent_calling(call, "The live-runs ticket is done [e1]. Runs fail every night."),
    )
    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "q", "stream": False, "scope": {"project_id": project.id}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == (
        f"The live-runs ticket is done [ticket:{ticket.id}]. Runs fail every night "
        f"{NOT_VERIFIED}."
    )
    g = body["grounding"]
    assert g["mode"] == "strict" and g["unverified_count"] == 1
    assert g["evidence"][0]["id"] == "e1" and g["evidence"][0]["tool"] == "get_ticket"
    assert any(c["kind"] == "ticket" and c["ticket_id"] == ticket.id for c in body["citations"])

    rows = (
        await db_session.execute(
            select(HelperMessage)
            .where(HelperMessage.conversation_id == body["conversation_id"])
            .order_by(HelperMessage.created_at)
        )
    ).scalars().all()
    tool_row = next(r for r in rows if r.role == "tool")
    assert tool_row.content.startswith("[evidence e1]\n")
    assert json.loads(tool_row.citations)["evidence"]["id"] == "e1"
    answer_row = rows[-1]
    stored = json.loads(answer_row.citations)
    assert stored["grounding"]["unverified_count"] == 1

    conv = await client.get(
        f"/api/v1/helper/conversations/{body['conversation_id']}", headers=_hdrs(key)
    )
    answer_msg = conv.json()["messages"][-1]
    assert answer_msg["grounding"]["mode"] == "strict"
    assert isinstance(answer_msg["citations"], list)

    # The next turn continues the numbering and may cite the earlier result.
    call2 = ToolCall(id="c2", name="list_tickets", arguments={"project_id": project.id})
    monkeypatch.setattr(
        helper_routes, "run_agent", _agent_calling(call2, "Still done [e1]. One ticket [e2].")
    )
    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={
            "message": "again",
            "stream": False,
            "conversation_id": body["conversation_id"],
            "scope": {"project_id": project.id},
        },
    )
    g2 = resp.json()["grounding"]
    assert {e["id"] for e in g2["evidence"]} == {"e1", "e2"}
    assert g2["unverified_count"] == 0


@pytest.mark.asyncio
async def test_stream_sends_grounding_event_with_final_text(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, owner=user)
    await _make_ticket(db_session, project, title="One")
    call = ToolCall(id="c1", name="list_tickets", arguments={"project_id": project.id})
    monkeypatch.setattr(helper_routes, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        helper_routes, "run_agent", _agent_calling(call, "There is 1 ticket [e1]. It failed on Monday.")
    )
    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "q", "stream": True, "scope": {"project_id": project.id}},
    )
    text = resp.text
    assert "[e1]" not in text.split("event: grounding")[0]
    assert "event: grounding" in text
    data = next(
        json.loads(block.split("data: ", 1)[1])
        for block in text.split("\n\n")
        if block.startswith("event: grounding")
    )
    assert data["text"] == f"There is 1 ticket. It failed on Monday {NOT_VERIFIED}."


@pytest.mark.asyncio
async def test_protocol_ignored_falls_back_to_marker_checks(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, owner=user)
    await _make_ticket(db_session, project, title="One")
    call = ToolCall(id="c1", name="list_tickets", arguments={"project_id": project.id})
    monkeypatch.setattr(helper_routes, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        helper_routes, "run_agent", _agent_calling(call, "There are 12 tickets and 3 failed.")
    )
    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "q", "stream": False, "scope": {"project_id": project.id}},
    )
    body = resp.json()
    assert body["grounding"] is None
    assert NOT_VERIFIED not in body["answer"]
    assert body["answer"].endswith(helper_routes._NO_SOURCES_NOTE)


@pytest.mark.asyncio
async def test_grounding_off_shows_no_evidence_header(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, owner=user)
    put = await client.put(
        "/api/v1/settings/helper", headers=_hdrs(key), json={"settings": {"grounding": "off"}}
    )
    assert put.status_code == 200, put.text
    seen: list[str] = []
    call = ToolCall(id="c1", name="list_tickets", arguments={"project_id": project.id})

    async def agent(*, execute_tool, **kwargs):
        seen.append(kwargs["system"])
        yield ToolCallEvent(call)
        result = await execute_tool(call)
        seen.append(result.content)
        yield ToolResultEvent(call, result)
        yield TextDelta("Nothing found.")
        yield Done("stop")

    monkeypatch.setattr(helper_routes, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_routes, "run_agent", agent)
    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "q", "stream": False, "scope": {"project_id": project.id}},
    )
    body = resp.json()
    assert body["grounding"] is None
    assert "[evidence" not in seen[1]
    assert "[evidence e12]" not in seen[0]
    assert body["answer"].startswith("Nothing found.")


# ---------------------------------------------------------------------------
# Linear work on hostile model output (Shield v0.30.0)
# ---------------------------------------------------------------------------


def _min_time(fn, rounds: int = 3) -> float:
    import time

    best = float("inf")
    for _ in range(rounds):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


async def _stream(text: str, step: int) -> str:
    stream = GroundingStream({}, None)
    out = []
    for i in range(0, len(text), step):
        out.append(await stream.feed(text[i : i + step]))
    out.append(await stream.flush())
    return "".join(out)


@pytest.mark.parametrize(
    "unit",
    [" ", "a", "Done. "],
    ids=["spaces", "no-terminator", "sentences"],
)
def test_grounding_work_is_linear_in_answer_length(unit: str) -> None:
    """Whitespace runs after a terminator, or text that never ends a sentence,
    used to be scanned once per position / rescanned on every streamed delta
    (seconds of event-loop time for ~100 KB). Doubling the input must not
    quadruple the time."""
    small = "Done. " + unit * (20_000 // len(unit))
    big = "Done. " + unit * (40_000 // len(unit))

    t_small = _min_time(lambda: _run(_stream(small, 4)))
    t_big = _min_time(lambda: _run(_stream(big, 4)))
    assert t_big < max(t_small * 3.2, 0.05), (t_small, t_big)

    f_small = _min_time(lambda: _run(ground_answer(small, {})))
    f_big = _min_time(lambda: _run(ground_answer(big, {})))
    assert f_big < max(f_small * 3.2, 0.05), (f_small, f_big)


def test_tag_strip_on_long_space_run_is_fast() -> None:
    text = "x" + " " * 100_000 + "[e1]."
    assert _min_time(lambda: strip_evidence_tags(text)) < 0.5
    assert strip_evidence_tags("done [e12].") == "done."
    assert strip_evidence_tags("a  [e1]  [e2] b") == "a b"
