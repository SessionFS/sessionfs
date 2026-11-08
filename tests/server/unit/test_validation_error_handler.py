"""The global RequestValidationError handler never echoes submitted values.

Pydantic v2 errors carry ``input`` (the rejected value, sometimes the whole
request body) and ``ctx``. Returning them can hand a secret, or another
field's contents, back in an error body that clients log.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from sessionfs.server.errors import register_exception_handlers

SECRET = "sk_sfs_" + "a" * 40


class _Body(BaseModel):
    name: str = Field(max_length=8)
    amount: float = Field(ge=0)


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/echo")
    async def echo(body: _Body) -> dict:  # pragma: no cover - never reached
        return {}

    return app


@pytest.mark.asyncio
async def test_validation_error_omits_input_and_ctx():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        resp = await client.post("/echo", json={"name": SECRET, "amount": -1})
    assert resp.status_code == 422
    assert SECRET not in resp.text
    errors = resp.json()["error"]["details"]["errors"]
    assert {tuple(e["loc"]) for e in errors} == {("body", "name"), ("body", "amount")}
    for err in errors:
        assert set(err) <= {"type", "loc", "msg"}, err
        assert err["msg"]


@pytest.mark.asyncio
async def test_validation_error_non_finite_number_still_serializes():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/echo",
            content='{"name": "ok", "amount": NaN}',
            headers={"content-type": "application/json"},
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "422"
