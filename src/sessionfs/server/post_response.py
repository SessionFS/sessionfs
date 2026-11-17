"""Work that may run after the response has been sent (tk_e3eecad8c3874d92).

When the API runs with CPU allocated only while a request is in flight
(Cloud Run request-based billing), a task started on the event loop after
the response may get almost no CPU: it stalls until some later request
happens to land on the same instance, or is lost when the instance is
reclaimed. Worse, a stalled task resumes with every timer already expired,
so its outbound calls fail with spurious timeouts.

Every post-response task therefore goes through :func:`spawn`, and every one
of them is only a fast path: the job it runs is already durable (an outbox
row, a ``deferred_jobs`` row, a webhook delivery row) and an existing
scheduled sweep finishes it when the task never runs.

``SFS_POST_RESPONSE_TASKS`` (default ``on``) controls the fast path. Set it to
``off`` on a deployment whose CPU is billed only during requests: nothing is
started after the response, and the sweeps carry the work. Self-hosted
installs with always-on CPU keep the default and get the immediate path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger("sessionfs.post_response")

_tasks: set[asyncio.Task] = set()

_OFF_VALUES = frozenset({"0", "off", "false", "no", "disabled"})


def enabled() -> bool:
    """Whether post-response fast paths may start (read on every call)."""
    raw = os.environ.get("SFS_POST_RESPONSE_TASKS", "on").strip().lower()
    return raw not in _OFF_VALUES


async def _run(coro: Coroutine[Any, Any, Any]) -> None:
    await coro


def spawn(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
    background_tasks: Any = None,
    force: bool = False,
) -> asyncio.Task | None:
    """Start ``coro`` as a detached task, or drop it when the fast path is off.

    The caller must already have made the work durable: dropping the
    coroutine (or the task never getting CPU) only delays the job until the
    sweep that owns it runs. With ``background_tasks`` (a route's Starlette
    ``BackgroundTasks``) the coroutine runs there instead, right after the
    response in the same ASGI call. Returns the task, or None when no
    detached task was started.

    ``force`` starts the work even when the fast path is off. It is only for
    the fallback when recording the durable row itself failed: then this task
    is the only copy of the work, as it was before the durable rows existed.
    """
    if not enabled() and not force:
        coro.close()
        return None
    if background_tasks is not None:
        background_tasks.add_task(_run, coro)
        return None
    try:
        task = asyncio.get_running_loop().create_task(coro, name=name)
    except RuntimeError:
        coro.close()
        logger.warning("post-response task %s not started: no running loop", name)
        return None
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def drain() -> None:
    """Wait for every started post-response task (tests and shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
