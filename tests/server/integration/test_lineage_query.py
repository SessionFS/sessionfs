"""Lineage: the ``distilled_from`` edge and the documented query "which
dataset trained the model in production" (site/src/content/docs/lineage.mdx).

The query runs exactly as the docs describe it, through the real routes:
over HTTP, through the MCP handlers (forwarded to the same app) and through
the CLI command bodies (their API calls captured and answered by the app).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.server.integration.test_evals_api import _artifact, _code, _env, _pp
from tests.server.integration.test_loops_api import _hdrs


async def _link(client: AsyncClient, e, parent: str, child: str, relation: str):
    return await client.post(
        _pp(e, "artifact-edges"),
        headers=_hdrs(e.producer_key),
        json={"parent_id": parent, "child_id": child, "relation": relation},
    )


async def _distilled_world(client: AsyncClient, e) -> dict[str, dict]:
    """teacher (trained on notes v3) -> distilled into clin 2.3 (trained on
    the distill set), and clin 2.3 promoted to production."""
    notes = await _artifact(client, e, kind="dataset", name="notes", version="v3", external_uri="s3://d/notes/v3")
    distill = await _artifact(client, e, kind="dataset", name="distill-set", version="v1", external_uri="s3://d/distill/v1")
    teacher = await _artifact(client, e, name="teacher", version="70b", external_uri="hf://acme/teacher-70b")
    student = await _artifact(client, e)
    assert (await _link(client, e, notes["id"], teacher["id"], "trained_on")).status_code == 201
    r = await _link(client, e, teacher["id"], student["id"], "distilled_from")
    assert r.status_code == 201, r.text
    assert r.json()["relation"] == "distilled_from"
    assert (await _link(client, e, distill["id"], student["id"], "trained_on")).status_code == 201
    promoted = await client.post(
        _pp(e, f"artifacts/{student['id']}/promote"),
        headers=_hdrs(e.owner_key),
        json={"to_stage": "production"},
    )
    assert promoted.status_code == 200, promoted.text
    return {"notes": notes, "distill": distill, "teacher": teacher, "student": student}


def _datasets_that_trained(production: dict, lineage: dict) -> tuple[set[str], set[str]]:
    """The documented answer: (datasets that trained the production model
    directly, every dataset that trained anything in its upstream lineage)."""
    kinds = {n["id"]: n["kind"] for n in lineage["nodes"]}
    trained = [e for e in lineage["edges"] if e["relation"] == "trained_on" and kinds.get(e["parent_id"]) == "dataset"]
    direct = {e["parent_id"] for e in trained if e["child_id"] == production["id"]}
    return direct, {e["parent_id"] for e in trained}


@pytest.mark.asyncio
async def test_distilled_from_create_list_and_trace(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    w = await _distilled_world(client, e)

    dup = await _link(client, e, w["teacher"]["id"], w["student"]["id"], "distilled_from")
    assert dup.status_code == 409
    cyc = await _link(client, e, w["student"]["id"], w["teacher"]["id"], "distilled_from")
    assert cyc.status_code == 422 and _code(cyc) == "lineage_cycle"
    bad = await _link(client, e, w["teacher"]["id"], w["student"]["id"], "copied_from")
    assert bad.status_code == 422

    # Same direction as fine_tuned_from: the teacher is upstream of the student.
    up = (await client.get(_pp(e, f"artifacts/{w['student']['id']}/lineage?direction=up"), headers=_hdrs(e.member_key))).json()
    assert {n["id"] for n in up["nodes"]} == {w[k]["id"] for k in ("student", "teacher", "notes", "distill")}
    assert {(x["parent_id"], x["relation"]) for x in up["edges"]} >= {(w["teacher"]["id"], "distilled_from")}
    down = (await client.get(_pp(e, f"artifacts/{w['teacher']['id']}/lineage?direction=down"), headers=_hdrs(e.member_key))).json()
    assert {n["id"] for n in down["nodes"]} == {w["teacher"]["id"], w["student"]["id"]}
    assert [x["relation"] for x in down["edges"]] == ["distilled_from"]

    shown = (await client.get(_pp(e, f"artifacts/{w['student']['id']}"), headers=_hdrs(e.member_key))).json()
    assert shown["stage"] == "production"


@pytest.mark.asyncio
async def test_which_dataset_trained_the_production_model_over_http(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    w = await _distilled_world(client, e)

    # Step 1: the production model.
    listed = await client.get(_pp(e, "artifacts?kind=model&stage=production"), headers=_hdrs(e.member_key))
    assert listed.status_code == 200
    prod = listed.json()["artifacts"]
    assert [a["id"] for a in prod] == [w["student"]["id"]]
    # Step 2: its upstream lineage. Step 3: the trained_on edges from datasets.
    lineage = await client.get(_pp(e, f"artifacts/{prod[0]['id']}/lineage?direction=up"), headers=_hdrs(e.member_key))
    assert lineage.status_code == 200
    direct, upstream = _datasets_that_trained(prod[0], lineage.json())
    assert direct == {w["distill"]["id"]}
    assert upstream == {w["distill"]["id"], w["notes"]["id"]}


@pytest.mark.asyncio
async def test_which_dataset_trained_the_production_model_over_mcp(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.mcp import server as mcp

    e = await _env(db_session)
    w = await _distilled_world(client, e)

    async def resolve(_git_remote: str):
        return "http://test", e.member_key, e.project.id

    @contextlib.asynccontextmanager
    async def app_client(**_kw):
        yield client

    monkeypatch.setattr(mcp, "_resolve_project_id", resolve)
    monkeypatch.setattr(mcp, "idempotent_async_client", app_client)

    listed = await mcp._EVAL_HANDLERS["list_artifacts"]({"kind": "model", "stage": "production"})
    prod = listed["artifacts"]
    assert [a["id"] for a in prod] == [w["student"]["id"]]
    lineage = await mcp._EVAL_HANDLERS["get_artifact_lineage"]({"artifact_id": prod[0]["id"], "direction": "up"})
    direct, upstream = _datasets_that_trained(prod[0], lineage)
    assert direct == {w["distill"]["id"]}
    assert upstream == {w["distill"]["id"], w["notes"]["id"]}


@pytest.mark.asyncio
async def test_which_dataset_trained_the_production_model_over_cli(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, capsys
):
    from sessionfs.cli import cmd_eval

    e = await _env(db_session)
    w = await _distilled_world(client, e)

    # The CLI commands run their own event loop, so each captured call is
    # answered by the app on a worker thread with its own loop.
    async def answer(method: str, tail: str) -> Any:
        r = await client.request(method, _pp(e, tail), headers=_hdrs(e.member_key))
        assert r.status_code == 200, r.text
        return r.json()

    loop = asyncio.get_running_loop()
    tails: list[str] = []

    def fake_call(method: str, tail: str, **_kw) -> Any:
        tails.append(tail)
        return asyncio.run_coroutine_threadsafe(answer(method, tail), loop).result(timeout=30)

    monkeypatch.setattr(cmd_eval, "_call", fake_call)

    await asyncio.to_thread(cmd_eval.artifact_list, kind="model", name=None, stage="production", tag=None, as_json=True)

    prod = json.loads(capsys.readouterr().out)
    assert [a["id"] for a in prod] == [w["student"]["id"]]
    await asyncio.to_thread(cmd_eval.artifact_lineage, artifact_id=prod[0]["id"], direction="up", depth=25, as_json=True)
    direct, upstream = _datasets_that_trained(prod[0], json.loads(capsys.readouterr().out))
    assert direct == {w["distill"]["id"]}
    assert upstream == {w["distill"]["id"], w["notes"]["id"]}
    assert tails == ["artifacts?kind=model&stage=production", f"artifacts/{prod[0]['id']}/lineage?direction=up&depth=25"]
