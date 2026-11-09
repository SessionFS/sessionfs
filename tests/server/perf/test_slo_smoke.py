"""SLO perf smoke: the operations behind the published SLOs stay fast.

Drives the in-process app on SQLite (the same fixtures as the integration
suite) for a fixed number of iterations of each SLO'd operation and fails
when the p95 regresses past a budget. Excluded from the default run by the
``slo_smoke`` marker; CI runs it as its own job:

    pytest tests/server/perf -m slo_smoke -p no:randomly -s

Flake resistance
----------------
* Each operation is warmed up, then measured in ``ROUNDS`` rounds of
  ``ITERATIONS`` calls. The verdict uses the BEST round's p95 (min of
  rounds): a noisy neighbour on a shared CI runner can slow one round, but
  a real regression slows all of them.
* Budgets are roughly 10x the p95 measured on a developer laptop and still
  at or under the production SLO thresholds (see
  ``site/src/content/docs/slos.mdx``). They catch an accidental O(n) query
  or a blocking call on the event loop, not a 10 % slowdown.

Budgets (seconds, p95 of the best round; laptop p95 in brackets)
----------------------------------------------------------------
* ticket comment write (POST .../tickets/{ticket_id}/comments): 0.30 [~0.03]
* session push of a small archive (PUT /sessions/{id}/sync):      0.50 [~0.035]
* 50-line transcript extraction, provider stubbed to answer
  instantly (POST /meetings/{id}/extract), server time only:       0.60 [~0.05]
* reminders-due sweep delivering 5 due reminders:                  0.50 [~0.035]

The module takes about 25 s locally including fixture setup.
"""

from __future__ import annotations

import io
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server import metrics
from sessionfs.server.db.models import Reminder, User
from tests.server.integration.test_meeting_actions_api import (
    _headers as meeting_headers,
    _install_extract_mocks,
    _meeting,
    _project,
)
from tests.server.integration.test_platform_sweeps_scope import _sweep_key
from tests.server.integration.test_ticket_comments_api import (
    _hdrs,
    _make_project,
    _make_user,
)
from tests.utils.synthetic_session import build_session

pytestmark = pytest.mark.slo_smoke

WARMUP = 3
ROUNDS = 3
ITERATIONS = 20

BUDGET_COMMENT_S = 0.30
BUDGET_PUSH_S = 0.50
BUDGET_EXTRACT_S = 0.60
BUDGET_SWEEP_S = 0.50

COMMENT_ROUTE = "/api/v1/projects/{project_id}/tickets/{ticket_id}/comments"
PUSH_ROUTE = "/api/v1/sessions/{session_id}/sync"


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


async def _measure(
    name: str,
    prepare: Callable[[], Awaitable[object]],
    op: Callable[[object], Awaitable[None]],
    budget: float,
) -> float:
    """Best-round p95 of ``op``; ``prepare`` runs untimed before each call."""
    for _ in range(WARMUP):
        await op(await prepare())
    round_p95s: list[float] = []
    for _ in range(ROUNDS):
        samples: list[float] = []
        for _ in range(ITERATIONS):
            arg = await prepare()
            t0 = time.perf_counter()
            await op(arg)
            samples.append(time.perf_counter() - t0)
        round_p95s.append(_p95(samples))
    best = min(round_p95s)
    print(
        f"\nSLO smoke {name}: best-round p95 {best * 1000:.1f} ms "
        f"(rounds {', '.join(f'{p * 1000:.1f}' for p in round_p95s)} ms; "
        f"budget {budget * 1000:.0f} ms)"
    )
    assert best <= budget, (
        f"{name}: p95 {best * 1000:.1f} ms over the {budget * 1000:.0f} ms budget "
        f"in every round ({round_p95s})"
    )
    return best


def _route_recorded(route: str, method: str) -> bool:
    needle = f'method="{method}",route="{route}",status_class="2xx"'
    return any(
        line.startswith("http_request_duration_seconds_count{") and needle in line
        for line in metrics.render_metrics().splitlines()
    )


async def _nothing() -> None:
    return None


async def test_ticket_comment_write_p95(client: AsyncClient, db_session: AsyncSession) -> None:
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    headers = _hdrs(key)
    created = await client.post(
        f"/api/v1/projects/{project.id}/tickets", headers=headers, json={"title": "slo smoke"}
    )
    assert created.status_code == 201, created.text
    url = f"/api/v1/projects/{project.id}/tickets/{created.json()['id']}/comments"

    async def op(_: object) -> None:
        resp = await client.post(url, headers=headers, json={"content": "Progress update."})
        assert resp.status_code == 201, resp.text

    await _measure("ticket comment write", _nothing, op, BUDGET_COMMENT_S)
    # The SLO's PromQL selects exactly this series.
    assert _route_recorded(COMMENT_ROUTE, "POST")


async def test_session_push_p95(client: AsyncClient, auth_headers: dict) -> None:
    async def prepare() -> tuple[str, bytes]:
        session_id = f"ses_{uuid.uuid4().hex[:16]}"
        return session_id, build_session(session_id, scale=0.01)

    async def op(arg: object) -> None:
        session_id, archive = arg  # type: ignore[misc]
        resp = await client.put(
            f"/api/v1/sessions/{session_id}/sync",
            headers=auth_headers,
            files={"file": ("s.tar.gz", io.BytesIO(archive), "application/gzip")},
        )
        assert resp.status_code in (200, 201), resp.text[:300]

    await _measure("session push", prepare, op, BUDGET_PUSH_S)
    assert _route_recorded(PUSH_ROUTE, "PUT")


def _fifty_line_transcript() -> str:
    speakers = ("Alice", "Bob", "Carol", "Dan")
    return "\n".join(
        f"{speakers[i % 4]}: item {i} - we should follow up on the release checklist"
        for i in range(50)
    )


_EXTRACT = json.dumps(
    {
        "participants": [{"name": "Alice"}, {"name": "Bob"}, {"name": "Carol"}],
        "items": [
            {
                "kind": "action",
                "description": f"Follow up on item {n}.",
                "owner_name": "Alice",
                "line_start": n,
                "line_end": n + 1,
            }
            for n in range(1, 40, 4)
        ],
    }
)


async def test_extraction_server_time_p95(
    client: AsyncClient,
    db_session: AsyncSession,
    test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = await _project(db_session, test_user)
    headers = await meeting_headers(db_session, test_user)
    await _install_extract_mocks(monkeypatch, _EXTRACT)
    content = _fifty_line_transcript()

    async def prepare() -> str:
        return (await _meeting(db_session, project=project, content=content)).id

    async def op(meeting_id: object) -> None:
        resp = await client.post(f"/api/v1/meetings/{meeting_id}/extract", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["extraction_status"] == "done", resp.text

    await _measure("50-line extraction (provider stubbed)", prepare, op, BUDGET_EXTRACT_S)
    assert any(
        line.startswith('sessionfs_extraction_server_seconds_count{kind="meeting",result="done"}')
        for line in metrics.render_metrics().splitlines()
    )


async def test_reminders_sweep_p95(client: AsyncClient, db_session: AsyncSession) -> None:
    admin, _, _, headers = await _sweep_key(db_session)
    admin_id = admin.id

    async def prepare() -> None:
        due = datetime.now(timezone.utc) - timedelta(minutes=10)
        for _ in range(5):
            db_session.add(
                Reminder(
                    id=f"rem_{uuid.uuid4().hex[:12]}",
                    owner_user_id=admin_id,
                    created_by_user_id=admin_id,
                    title="Follow up",
                    due_at=due,
                    original_due_at=due,
                )
            )
        await db_session.commit()

    async def op(_: object) -> None:
        resp = await client.post("/api/v1/admin/sweeps/reminders-due", headers=headers, json={})
        assert resp.status_code == 200, resp.text

    await _measure("reminders-due sweep (5 due)", prepare, op, BUDGET_SWEEP_S)
    # Every reminder was delivered: the lag gauge the SLO reads is back to 0.
    resp = await client.post("/api/v1/admin/sweeps/reminders-due", headers=headers, json={})
    assert resp.status_code == 200
    assert 'sessionfs_sweep_lag_seconds{sweep="reminders-due"} 0.0' in metrics.render_metrics()
