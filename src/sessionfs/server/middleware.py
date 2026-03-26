"""Request logging middleware."""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("sessionfs.api")


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
