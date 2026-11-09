"""API contract (tk_3855ef8b11f94ee2): loop-create naming, ignored-field
reporting, strict request bodies and deprecation headers."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import Depends, FastAPI, Response
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.api_contract import (
    DEPRECATED_FIELDS_HEADER,
    IGNORED_FIELDS_HEADER,
    StrictRequestModel,
    deprecated_route,
    install_ignored_fields_reporting,
    mark_field_deprecated,
)
from tests.server.integration.test_loops_api import (
    _hdrs,
    _loop_body,
    _make_project,
    _make_user,
    _start_loop,
)

# ── loop create: ticket_id alias ─────────────────────────────────


async def _issue(client: AsyncClient, project, key: str, title: str = "Loop parent") -> str:
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(key),
        json={"title": title, "kind": "issue"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_loop_create_accepts_ticket_id_alias(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    parent_id = await _issue(client, project, key)

    resp = await _start_loop(
        client, project, key, _loop_body([{"title": "child"}], ticket_id=parent_id)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["loop_id"] == parent_id
    # ticket_id is a real field now, so nothing is reported as ignored.
    assert IGNORED_FIELDS_HEADER.lower() not in {k.lower() for k in resp.headers}


@pytest.mark.asyncio
async def test_loop_create_same_value_under_both_names(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    parent_id = await _issue(client, project, key)

    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body([{"title": "child"}], ticket_id=parent_id, from_ticket_id=parent_id),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["loop_id"] == parent_id


@pytest.mark.asyncio
async def test_loop_create_conflicting_ticket_ids_422(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)
    a = await _issue(client, project, key, "A")
    b = await _issue(client, project, key, "B")

    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body([{"title": "child"}], ticket_id=a, from_ticket_id=b),
    )
    assert resp.status_code == 422, resp.text
    text = resp.text
    assert "from_ticket_id" in text and "ticket_id" in text and "different values" in text


@pytest.mark.asyncio
async def test_loop_create_missing_title_names_the_fields(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(client, project, key, _loop_body([{"title": "child"}]))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["error"]["message"]
    assert "title is missing or blank" in detail
    assert "from_ticket_id" in detail and "ticket_id" in detail


@pytest.mark.asyncio
async def test_loop_create_reports_ignored_fields(client: AsyncClient, db_session: AsyncSession):
    """The reported 422 came from a field the route silently dropped. The
    dropped field is now named in the response header."""
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(
        client,
        project,
        key,
        _loop_body([{"title": "child"}], name="My loop", parent="tk_nope"),
    )
    assert resp.status_code == 422
    assert resp.headers[IGNORED_FIELDS_HEADER] == "name,parent"


@pytest.mark.asyncio
async def test_no_ignored_fields_header_on_a_clean_body(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await _start_loop(client, project, key, _loop_body([{"title": "child"}], title="Loop"))
    assert resp.status_code == 201, resp.text
    assert IGNORED_FIELDS_HEADER not in resp.headers


@pytest.mark.asyncio
async def test_raw_body_route_reports_ignored_fields(client: AsyncClient, db_session: AsyncSession):
    """Meeting upload reads its body itself (streamed cap); the declared
    model still drives the ignored-fields report."""
    owner, key = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/meetings",
        headers=_hdrs(key),
        json={"title": "Standup", "transcript": "Alice: hi"},
    )
    # ``transcript`` is not a field (the route reads ``content``), so it is
    # reported, and the missing ``content`` fails validation.
    assert resp.status_code == 422, resp.text
    assert resp.headers[IGNORED_FIELDS_HEADER] == "transcript"


# ── mechanism on a bare app ──────────────────────────────────────


class _Lenient(BaseModel):
    a: int
    b: str = ""


class _Strict(StrictRequestModel):
    a: int


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/lenient")
    async def lenient(body: _Lenient) -> dict:
        return {"a": body.a}

    @app.post("/strict")
    async def strict(body: _Strict) -> dict:
        return {"a": body.a}

    @app.get("/read")
    async def read() -> dict:
        return {}

    @app.post(
        "/old",
        deprecated=True,
        dependencies=[Depends(deprecated_route(sunset=date(2027, 6, 1), since=date(2026, 11, 1)))],
    )
    async def old() -> dict:
        return {}

    @app.post("/field")
    async def field(body: _Lenient, response: Response) -> dict:
        if body.b:
            mark_field_deprecated(response, "b", sunset=date(2027, 6, 1))
        return {}

    assert install_ignored_fields_reporting(app) == 2  # /lenient and /field
    assert install_ignored_fields_reporting(app) == 0  # idempotent
    return app


async def _bare() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t")


@pytest.mark.asyncio
async def test_lenient_route_header_present_and_absent():
    async with await _bare() as c:
        resp = await c.post("/lenient", json={"a": 1, "zz": 1, "extra": 2})
        assert resp.status_code == 200
        assert resp.headers[IGNORED_FIELDS_HEADER] == "extra,zz"

        clean = await c.post("/lenient", json={"a": 1, "b": "x"})
        assert IGNORED_FIELDS_HEADER not in clean.headers

        # Not JSON, not an object: nothing to report, the route answers as before.
        assert IGNORED_FIELDS_HEADER not in (await c.post("/lenient", json=[1])).headers
        assert IGNORED_FIELDS_HEADER not in (await c.get("/read")).headers


@pytest.mark.asyncio
async def test_ignored_field_names_are_header_safe():
    async with await _bare() as c:
        resp = await c.post("/lenient", json={"a": 1, "bad,naïme": 1, "x" * 200: 1})
        value = resp.headers[IGNORED_FIELDS_HEADER]
        assert value.isascii()
        assert all(len(part) <= 64 for part in value.split(","))
        assert "badname" in value.split(",")


@pytest.mark.asyncio
async def test_strict_base_rejects_unknown_fields():
    async with await _bare() as c:
        resp = await c.post("/strict", json={"a": 1, "zz": 1})
        assert resp.status_code == 422
        assert "zz" in resp.text
        assert IGNORED_FIELDS_HEADER not in resp.headers


@pytest.mark.asyncio
async def test_deprecated_route_headers():
    async with await _bare() as c:
        resp = await c.post("/old")
        assert resp.status_code == 200
        assert resp.headers["Deprecation"] == "@1793491200"
        assert resp.headers["Sunset"] == "Tue, 01 Jun 2027 00:00:00 GMT"


@pytest.mark.asyncio
async def test_deprecated_field_headers():
    async with await _bare() as c:
        used = await c.post("/field", json={"a": 1, "b": "old"})
        assert used.headers["Deprecation"] == "true"
        assert used.headers["Sunset"] == "Tue, 01 Jun 2027 00:00:00 GMT"
        assert used.headers[DEPRECATED_FIELDS_HEADER] == "b"

        unused = await c.post("/field", json={"a": 1})
        assert "Deprecation" not in unused.headers
