"""API contract helpers: strict request bodies, ignored-field reporting, deprecation.

Three pieces live here so every route shares one mechanism:

* :class:`StrictRequestModel` — the base for request bodies on NEW routes.
  ``extra="forbid"`` turns an unknown field into a 422 that names it, instead
  of a silent drop.

* :func:`install_ignored_fields_reporting` — existing routes keep accepting
  unknown top-level fields (refusing them would break older clients), but the
  response now says which ones were dropped in the
  ``X-SessionFS-Ignored-Fields`` header. It wraps each route's handler once at
  startup and compares the JSON body's top-level keys with the fields (and
  aliases) of the endpoint's body model, so no model has to change.

* :func:`mark_deprecated` / :func:`deprecated_route` / :func:`mark_field_deprecated`
  — emit ``Deprecation`` (RFC 9745) and ``Sunset`` (RFC 8594) headers so a
  client learns about a removal at least one minor release ahead of it.

See the public "API contract and versioning" docs page.
"""

from __future__ import annotations

import json
import logging
import types
import typing
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, datetime, time, timezone
from email.utils import format_datetime
from typing import Any

from fastapi import FastAPI, Response
from fastapi.routing import APIRoute
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict

from sessionfs.server.route_introspection import flat_params, iter_api_routes

logger = logging.getLogger("sessionfs.api_contract")

#: Response header listing the top-level request-body fields the server ignored.
IGNORED_FIELDS_HEADER = "X-SessionFS-Ignored-Fields"
#: Response header listing deprecated request-body fields the caller sent.
DEPRECATED_FIELDS_HEADER = "X-SessionFS-Deprecated-Fields"

#: Headers a cross-origin caller must be able to read (added to CORS
#: ``expose_headers``).
CONTRACT_EXPOSE_HEADERS = (
    IGNORED_FIELDS_HEADER,
    DEPRECATED_FIELDS_HEADER,
    "Deprecation",
    "Sunset",
)

# Bodies larger than this are not re-parsed for the ignored-fields check (the
# route itself still validates them). Large bodies are transcripts and
# archives, whose models are small and well known.
_MAX_INSPECTED_BODY = 4 * 1024 * 1024
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})


class StrictRequestModel(BaseModel):
    """Base class for request bodies on new routes: unknown fields are a 422."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Body model introspection (shared with the contract tests)
# ---------------------------------------------------------------------------


def _alias_strings(alias: Any) -> set[str]:
    if alias is None:
        return set()
    if isinstance(alias, str):
        return {alias}
    if isinstance(alias, AliasChoices):
        out: set[str] = set()
        for choice in alias.choices:
            out |= _alias_strings(choice)
        return out
    if isinstance(alias, AliasPath):
        first = alias.path[0] if alias.path else None
        return {first} if isinstance(first, str) else set()
    return set()


def model_input_keys(model: type[BaseModel]) -> set[str]:
    """Top-level JSON keys ``model`` reads: field names plus validation aliases."""
    keys: set[str] = set()
    for name, info in model.model_fields.items():
        keys.add(name)
        keys |= _alias_strings(info.alias)
        keys |= _alias_strings(info.validation_alias)
    return keys


def union_models(annotation: Any) -> list[type[BaseModel]] | None:
    """The BaseModel members of ``annotation`` (a model, Optional or Union)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return union_models(typing.get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType):
        models: list[type[BaseModel]] = []
        for arg in typing.get_args(annotation):
            if arg is type(None):
                continue
            sub = union_models(arg)
            if sub is None:
                return None
            models.extend(sub)
        return models or None
    return None


def _body_params(route: APIRoute) -> list[Any]:
    return flat_params(route.dependant, "body_params")


def route_body_models(route: APIRoute) -> list[type[BaseModel]] | None:
    """The pydantic model(s) a route reads its whole JSON body into.

    ``None`` when the body is not a single top-level model (no body, a
    ``dict``/``Any`` body, or several embedded body params).
    """
    params = _body_params(route)
    if len(params) != 1:
        return None
    param = params[0]
    if getattr(param.field_info, "embed", None):
        return None
    return union_models(param.field_info.annotation)


def route_body_keys(route: APIRoute) -> set[str] | None:
    """Every top-level JSON key a route's body accepts, or ``None`` if unknown.

    Embedded body params (several ``Body()`` arguments) accept their param
    names (or aliases) as top-level keys.
    """
    params = _body_params(route)
    if not params:
        return None
    if len(params) > 1 or getattr(params[0].field_info, "embed", None):
        keys: set[str] = set()
        for param in params:
            keys.add(param.name)
            keys |= _alias_strings(param.field_info.alias)
        return keys
    models = route_body_models(route)
    if models is None:
        return None
    keys = set()
    for model in models:
        keys |= model_input_keys(model)
    return keys


def _model_ignores_extra(model: type[BaseModel]) -> bool:
    extra = model.model_config.get("extra")
    return extra in (None, "ignore")


_RAW_BODY_ATTR = "__sfs_raw_body_model__"


def raw_json_body(model: type[BaseModel]) -> Callable[[Any], Any]:
    """Declare the model a route validates its streamed JSON body with.

    For routes that read ``Request`` themselves (to cap the body before
    parsing) FastAPI can't see the model. Apply this under the router
    decorator so ignored-field reporting and the contract tests still know
    which top-level fields the route reads; pass
    ``openapi_extra=raw_json_body_openapi(model)`` to the router decorator to
    document it in the OpenAPI snapshot.
    """

    def deco(endpoint: Any) -> Any:
        setattr(endpoint, _RAW_BODY_ATTR, model)
        return endpoint

    return deco


def raw_json_body_openapi(model: type[BaseModel]) -> dict[str, Any]:
    """``openapi_extra`` documenting a :func:`raw_json_body` request body."""
    schema = model.model_json_schema()
    if "$defs" in schema:
        raise ValueError(f"{model.__name__}: nested models can't be inlined as a raw body schema")
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


def raw_body_model(route: APIRoute) -> type[BaseModel] | None:
    model = getattr(route.endpoint, _RAW_BODY_ATTR, None)
    return model if isinstance(model, type) and issubclass(model, BaseModel) else None


def lenient_body_keys(route: APIRoute) -> set[str] | None:
    """Keys accepted by a route whose body silently drops unknown fields.

    ``None`` when the route needs no reporting: not a write method, no single
    model body, or every body model forbids (or keeps) extra fields.
    """
    if not (set(route.methods or ()) & _WRITE_METHODS):
        return None
    params = _body_params(route)
    if not params:
        raw = raw_body_model(route)
        if raw is not None and _model_ignores_extra(raw):
            return model_input_keys(raw)
        return None
    if len(params) > 1 or getattr(params[0].field_info, "embed", None):
        # FastAPI builds the embedded wrapper itself and ignores extras.
        return route_body_keys(route)
    models = route_body_models(route)
    if not models or not any(_model_ignores_extra(m) for m in models):
        return None
    return route_body_keys(route)


# ---------------------------------------------------------------------------
# Ignored-field reporting
# ---------------------------------------------------------------------------

ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]


def ignored_top_level_keys(raw: bytes, accepted: Iterable[str]) -> list[str]:
    """Top-level keys of the JSON object in ``raw`` that ``accepted`` lacks."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    allowed = set(accepted)
    return sorted(k for k in data if isinstance(k, str) and k not in allowed)


def _header_value(keys: list[str]) -> bytes:
    # Header-safe: field names are client-supplied, so keep printable ASCII
    # without commas or control characters and cap the total length.
    safe: list[str] = []
    total = 0
    for key in keys:
        clean = "".join(ch for ch in key if 0x21 <= ord(ch) <= 0x7E and ch != ",")[:64]
        if not clean:
            continue
        total += len(clean) + 1
        if total > 512 or len(safe) >= 20:
            safe.append("...")
            break
        safe.append(clean)
    return ",".join(safe).encode("ascii")


def _wrap_ignored_fields(inner: ASGIApp, accepted: frozenset[str], path: str) -> ASGIApp:
    async def app(scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
        if scope.get("type") != "http" or scope.get("method") not in _WRITE_METHODS:
            await inner(scope, receive, send)
            return
        content_type = ""
        for name, value in scope.get("headers") or ():
            if name == b"content-type":
                content_type = value.decode("latin-1").lower()
                break
        if content_type and "json" not in content_type:
            await inner(scope, receive, send)
            return

        # Keep a copy of the body as the route reads it. FastAPI reads the
        # whole body before the endpoint runs, so it is complete by the time
        # the response starts; nothing is buffered ahead of the route.
        chunks: list[bytes] = []
        state = {"size": 0, "complete": False, "too_big": False}

        async def tee_receive() -> dict:
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if not state["too_big"]:
                    state["size"] += len(body)
                    if state["size"] > _MAX_INSPECTED_BODY:
                        state["too_big"] = True
                        chunks.clear()
                    else:
                        chunks.append(body)
                if not message.get("more_body", False):
                    state["complete"] = True
            return message

        async def tagging_send(message: dict) -> None:
            if (
                message.get("type") == "http.response.start"
                and state["complete"]
                and not state["too_big"]
            ):
                ignored = ignored_top_level_keys(b"".join(chunks), accepted)
                if ignored:
                    value = _header_value(ignored)
                    if value:
                        headers = list(message.get("headers") or [])
                        headers.append((IGNORED_FIELDS_HEADER.lower().encode("ascii"), value))
                        message = {**message, "headers": headers}
                        logger.debug(
                            "ignored request fields",
                            extra={"path": path, "ignored_fields": value.decode("ascii")},
                        )
            await send(message)

        await inner(scope, tee_receive, tagging_send)

    return app


def install_ignored_fields_reporting(app: FastAPI) -> int:
    """Wrap every lenient write route so dropped fields are reported.

    Call once after all routers are included. Returns how many routes were
    wrapped. Idempotent per route.

    The wrap goes on the route's ``handle`` (what the router calls once the
    route matched, on every FastAPI version) rather than ``route.app``: newer
    FastAPI serves included routes through a per-inclusion app it builds
    itself, so a replaced ``route.app`` would never run.
    """
    wrapped = 0
    for entry in iter_api_routes(app):
        route = entry.route
        if not isinstance(route, APIRoute) or getattr(route, "_sfs_ignored_fields", False):
            continue
        keys = lenient_body_keys(route)
        if keys is None:
            continue
        route.handle = _wrap_ignored_fields(  # type: ignore[method-assign]
            route.handle, frozenset(keys), entry.path
        )
        route._sfs_ignored_fields = True  # type: ignore[attr-defined]
        wrapped += 1
    return wrapped


# ---------------------------------------------------------------------------
# Deprecation headers (RFC 9745 Deprecation, RFC 8594 Sunset)
# ---------------------------------------------------------------------------


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.combine(value, time(0, 0), tzinfo=timezone.utc)


def mark_deprecated(
    response: Response,
    *,
    sunset: date | datetime,
    since: date | datetime | None = None,
    link: str | None = None,
) -> None:
    """Set ``Deprecation`` and ``Sunset`` on ``response``.

    ``Deprecation`` is ``@<unix seconds>`` when ``since`` is given (RFC 9745)
    and ``true`` otherwise; ``Sunset`` is an HTTP-date (RFC 8594). ``link``
    adds ``Link: <url>; rel="deprecation"``.
    """
    if since is not None:
        response.headers["Deprecation"] = f"@{int(_as_datetime(since).timestamp())}"
    else:
        response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = format_datetime(_as_datetime(sunset), usegmt=True)
    if link:
        existing = response.headers.get("Link")
        value = f'<{link}>; rel="deprecation"'
        response.headers["Link"] = f"{existing}, {value}" if existing else value


def mark_field_deprecated(
    response: Response,
    field: str,
    *,
    sunset: date | datetime,
    since: date | datetime | None = None,
    link: str | None = None,
) -> None:
    """Report that the caller sent deprecated body field ``field``."""
    mark_deprecated(response, sunset=sunset, since=since, link=link)
    existing = response.headers.get(DEPRECATED_FIELDS_HEADER)
    fields = [f for f in (existing.split(",") if existing else []) if f]
    if field not in fields:
        fields.append(field)
    response.headers[DEPRECATED_FIELDS_HEADER] = ",".join(fields)


def deprecated_route(
    *,
    sunset: date | datetime,
    since: date | datetime | None = None,
    link: str | None = None,
) -> Callable[[Response], None]:
    """Dependency marking a whole route deprecated.

    Usage: ``@router.get(..., deprecated=True,
    dependencies=[Depends(deprecated_route(sunset=date(2027, 6, 1)))])``.
    ``deprecated=True`` also flags the operation in the OpenAPI document.
    """

    def _dep(response: Response) -> None:
        mark_deprecated(response, sunset=sunset, since=since, link=link)

    return _dep
