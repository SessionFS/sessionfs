"""Request logging middleware."""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from sessionfs.server.metrics import HTTP_REQUEST_DURATION, method_label, status_class

logger = logging.getLogger("sessionfs.api")

# Route templates are unbounded in the aggregate (one per route × HTTP verb).
# Requests that never reached a handler (404/405) still match a template in
# Starlette, so recording the template for those would let an anonymous caller
# mint a series for every route × every verb. Collapse them to "unmatched".
_UNMATCHED_STATUSES = (404, 405)


def _matched_route(request: Request) -> str:
    """The matched route template, or ``"unmatched"`` when none was matched."""
    route = request.scope.get("route")
    if route is None:
        return "unmatched"
    return getattr(route, "path", None) or "unmatched"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log all 4xx responses with request context."""

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start) * 1000

        if 400 <= response.status_code < 500:
            client_ip = request.client.host if request.client else "unknown"
            logger.warning(
                "%s %s -> %d (%.1fms) client=%s",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
                client_ip,
            )

        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """Record ``http_request_duration_seconds`` for the operator metrics endpoint.

    ``route`` is the matched route TEMPLATE (``request.scope["route"].path``),
    read AFTER ``call_next`` so the router has populated it. Requests that
    never reached a handler (404/405) use ``"unmatched"``; a request whose
    handler raised records ``status_class="5xx"`` with the template when known
    and is re-raised. Never record raw paths or identifiers.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        method = method_label(request.method)
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled exception propagates past call_next with no
            # response to inspect; record the crash as a 5xx and re-raise.
            HTTP_REQUEST_DURATION.observe(
                time.monotonic() - start,
                method=method,
                route=_matched_route(request),
                status_class="5xx",
            )
            raise

        route_label = "unmatched"
        if response.status_code not in _UNMATCHED_STATUSES:
            route_label = _matched_route(request)
        HTTP_REQUEST_DURATION.observe(
            time.monotonic() - start,
            method=method,
            route=route_label,
            status_class=status_class(response.status_code),
        )
        return response
