"""Walk an app's routes the same way on every supported FastAPI version.

Up to FastAPI ~0.135 ``include_router`` copied every route into
``app.routes`` as an ``APIRoute`` carrying its full path. Newer releases keep
each included router as one wrapper entry in ``app.routes`` and build the
prefixed route lazily, so ``for route in app.routes: isinstance(route,
APIRoute)`` silently finds nothing and ``route.matches(scope)`` on the
wrapper says nothing about WHICH route matched.

Everything that enumerates or matches routes (the idempotency middleware,
ignored-field reporting, the contract tests) goes through this module so a
FastAPI upgrade can't quietly switch one of them off.

Newer FastAPI exposes ``fastapi.routing.iter_route_contexts`` (what its own
OpenAPI generator uses); older versions don't need it because ``app.routes``
is already flat.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Match
from starlette.types import Scope

try:  # FastAPI with lazily built included routers
    from fastapi.routing import iter_route_contexts as _iter_route_contexts
except ImportError:  # pragma: no cover - exercised on the older FastAPI pin
    _iter_route_contexts = None


@dataclass(frozen=True)
class RouteEntry:
    """One route as the app serves it.

    ``path`` is the full template including every ``include_router`` prefix;
    ``route`` is the original route object (an ``APIRoute`` for API routes,
    with its ``endpoint`` and ``dependant``); ``matcher`` answers
    ``matches(scope)`` against the full path.
    """

    path: str
    methods: frozenset[str]
    route: BaseRoute
    matcher: Any

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        return self.matcher.matches(scope)


def _routes_of(app_or_routes: Any) -> Sequence[BaseRoute]:
    if isinstance(app_or_routes, (list, tuple)):
        return app_or_routes
    router = getattr(app_or_routes, "router", app_or_routes)
    return list(getattr(router, "routes", ()))


def iter_routes(app_or_routes: Any) -> Iterator[RouteEntry]:
    """Every route in serving order (first FULL match wins, as in the router)."""
    routes = _routes_of(app_or_routes)
    if _iter_route_contexts is not None:
        for ctx in _iter_route_contexts(routes):
            path = ctx.path
            if not isinstance(path, str):
                continue
            yield RouteEntry(
                path=path,
                methods=frozenset(ctx.methods or ()),
                route=ctx.original_route,
                matcher=ctx,
            )
        return
    for route in routes:  # pragma: no cover - older FastAPI: already flat
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            continue
        yield RouteEntry(
            path=path,
            methods=frozenset(getattr(route, "methods", None) or ()),
            route=route,
            matcher=route,
        )


def iter_api_routes(app_or_routes: Any) -> Iterator[RouteEntry]:
    """Only the FastAPI ``APIRoute`` entries (``entry.route`` is the APIRoute)."""
    for entry in iter_routes(app_or_routes):
        if isinstance(entry.route, APIRoute):
            yield entry


def match_route(app_or_routes: Any, scope: Scope) -> RouteEntry | None:
    """The route that will handle ``scope`` (first FULL match), if any.

    A method-only (PARTIAL) match or no match means the request will 404/405.
    """
    for entry in iter_routes(app_or_routes):
        try:
            match, _ = entry.matches(scope)
        except Exception:  # pragma: no cover - a malformed route never blocks requests
            continue
        if match == Match.FULL:
            return entry
    return None


def flat_params(dependant: Any, kind: str) -> list[Any]:
    """``kind`` params (``body_params``, ``query_params``, ``path_params``…)
    of ``dependant`` and every sub-dependency, first occurrence of each name.

    Replaces ``fastapi.dependencies.utils.get_flat_dependant``, which newer
    FastAPI removed; only the public ``Dependant`` attributes are read.
    """
    seen: dict[str, Any] = {}
    stack = [dependant]
    visited: set[int] = set()
    while stack:
        current = stack.pop(0)
        if current is None or id(current) in visited:
            continue
        visited.add(id(current))
        for param in getattr(current, kind, None) or ():
            seen.setdefault(param.name, param)
        stack[0:0] = list(getattr(current, "dependencies", None) or ())
    return list(seen.values())
