"""Route walking that works on every supported FastAPI version.

Newer FastAPI keeps each included router as a single wrapper in
``app.routes``; older releases flatten them. These tests pin the behaviour
the idempotency middleware, ignored-field reporting and the metrics labels
rely on.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, Depends, FastAPI  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from sessionfs.server.route_introspection import (  # noqa: E402
    flat_params,
    iter_api_routes,
    match_route,
)


class _Body(BaseModel):
    name: str


def _dep(q: str | None = None) -> None:
    return None


def _app() -> FastAPI:
    inner = APIRouter(prefix="/inner")

    @inner.post("/{item_id}/things")
    async def create_thing(item_id: str, body: _Body, _: None = Depends(_dep)) -> dict:
        return {}

    @inner.get("/{item_id}/things")
    async def list_things(item_id: str) -> dict:
        return {}

    outer = APIRouter(prefix="/outer")
    outer.include_router(inner)

    app = FastAPI()
    app.include_router(outer, prefix="/api")

    @app.post("/direct")
    async def direct() -> dict:
        return {}

    return app


def _scope(app: FastAPI, method: str, path: str) -> dict:
    return {"type": "http", "method": method, "path": path, "root_path": "", "app": app}


def test_nested_included_routes_carry_their_full_path() -> None:
    paths = {(m, e.path) for e in iter_api_routes(_app()) for m in e.methods}
    assert ("POST", "/api/outer/inner/{item_id}/things") in paths
    assert ("GET", "/api/outer/inner/{item_id}/things") in paths
    assert ("POST", "/direct") in paths


def test_match_route_returns_the_full_template_for_the_method() -> None:
    app = _app()
    entry = match_route(app, _scope(app, "POST", "/api/outer/inner/abc/things"))
    assert entry is not None
    assert entry.path == "/api/outer/inner/{item_id}/things"
    assert "POST" in entry.methods
    assert match_route(app, _scope(app, "DELETE", "/api/outer/inner/abc/things")) is None
    assert match_route(app, _scope(app, "POST", "/api/nope")) is None
    direct = match_route(app, _scope(app, "POST", "/direct"))
    assert direct is not None and direct.path == "/direct"


def test_flat_params_walks_sub_dependencies() -> None:
    entry = next(
        e for e in iter_api_routes(_app()) if "POST" in e.methods and e.path.endswith("/things")
    )
    dependant = entry.route.dependant  # type: ignore[attr-defined]
    assert [p.name for p in flat_params(dependant, "body_params")] == ["body"]
    assert {p.name for p in flat_params(dependant, "path_params")} == {"item_id"}
    assert {p.alias for p in flat_params(dependant, "query_params")} == {"q"}


def test_app_routes_declare_their_full_path_on_the_route() -> None:
    """``scope["route"].path`` labels metrics and sweep/webhook observation.
    Newer FastAPI puts the ORIGINAL route in ``scope["route"]``, so a prefix
    given to ``include_router`` would be missing from it: every router must
    carry its prefix itself."""
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    app = create_app(ServerConfig(dashboard_dir="/nonexistent-sessionfs-dashboard"))
    mismatched = sorted(
        (entry.path, entry.route.path)  # type: ignore[attr-defined]
        for entry in iter_api_routes(app)
        if entry.route.path != entry.path  # type: ignore[attr-defined]
    )
    assert not mismatched, mismatched
