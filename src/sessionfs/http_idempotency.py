"""Client side of the API's ``Idempotency-Key`` support (tk_bd80efd511da4a2b).

Every POST the CLI and the MCP server send carries a fresh uuid4
``Idempotency-Key``, so a request the network retries (or that a caller
repeats with the same headers) runs at most once on the server. A caller
that retries on its own passes the same headers dict, built once with
:func:`idempotency_headers`, so the retry reuses the key.

A header the caller already set is never replaced.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import httpx

IDEMPOTENCY_HEADER = "Idempotency-Key"


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


def idempotency_headers(method: str, headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``headers`` with a fresh Idempotency-Key when ``method`` is POST."""
    out = dict(headers or {})
    if method.upper() == "POST" and not any(k.lower() == "idempotency-key" for k in out):
        out[IDEMPOTENCY_HEADER] = new_idempotency_key()
    return out


def _stamp(request: httpx.Request) -> None:
    if request.method == "POST" and IDEMPOTENCY_HEADER not in request.headers:
        request.headers[IDEMPOTENCY_HEADER] = new_idempotency_key()


async def _stamp_async(request: httpx.Request) -> None:
    _stamp(request)


def _add_hook(client: Any, hook: Any) -> Any:
    # Set after construction (not as a constructor kwarg) so a stand-in
    # client class without event hooks still works; it just sends no key.
    hooks = getattr(client, "event_hooks", None)
    if isinstance(hooks, dict):
        client.event_hooks = {**hooks, "request": [*hooks.get("request", []), hook]}
    return client


def idempotent_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` that stamps an Idempotency-Key on every POST."""
    return _add_hook(httpx.AsyncClient(*args, **kwargs), _stamp_async)


def idempotent_client(*args: Any, **kwargs: Any) -> httpx.Client:
    """``httpx.Client`` that stamps an Idempotency-Key on every POST."""
    return _add_hook(httpx.Client(*args, **kwargs), _stamp)


def idempotent_post(url: str, *, headers: Mapping[str, str] | None = None, **kwargs: Any) -> httpx.Response:
    """``httpx.post`` with an Idempotency-Key added to ``headers``."""
    return httpx.post(url, headers=idempotency_headers("POST", headers), **kwargs)
