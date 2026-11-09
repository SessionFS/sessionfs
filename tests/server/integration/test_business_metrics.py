"""Business metrics on their real code paths (GitHub #131, tk_35b319c6dac74cc9).

Each family is driven through the route or service that owns it, then read
back from the registry (counters/histograms) or a real scrape of
``GET /api/v1/admin/metrics`` (DB-derived gauges)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server import metrics
from sessionfs.server.config import ServerConfig
from sessionfs.server.db.models import Reminder, User
from sessionfs.server.metrics import reset_registry, set_tenant_labels
from sessionfs.server.routes.metrics import reset_db_gauge_cache
from sessionfs.server.services import jira_sync
from tests.server.integration import test_jira_sync as js
from tests.server.integration.approvals_helpers import hdrs, make_policy, make_world
from tests.server.integration.test_agent_run_records import (
    _create_run,
    _make_persona,
    _make_project,
    _make_user,
    _start_run,
)
from tests.server.integration.test_agent_run_records import _hdrs as run_hdrs
from tests.server.integration.test_confluence_routes import (
    _publish,
    _ready,
    fake_confluence,  # noqa: F401 — pytest fixture
)
from tests.server.integration.test_jira_sync import fake_sync_jira  # noqa: F401 — fixture
from tests.server.integration.test_loop_workers import (
    _env as loop_env,
    _finish,
    _round,
    _start_worker,
)
from tests.server.integration.test_metrics_endpoint import _metrics_key
from tests.server.integration.test_platform_sweeps_scope import (
    SCOPE as SWEEP_SCOPE,
    SWEEPS,
    _service_key,
    _sweep_key,
)

METRICS_PATH = "/api/v1/admin/metrics"


@pytest.fixture(autouse=True)
def _fresh_metrics():
    reset_registry()
    reset_db_gauge_cache()
    set_tenant_labels(False)
    yield
    reset_registry()
    reset_db_gauge_cache()
    set_tenant_labels(False)


def _sample(name: str, labels: dict[str, str] | None = None, text: str | None = None) -> float | None:
    """The value of one exposition sample with exactly ``labels``."""
    text = metrics.render_metrics() if text is None else text
    want = labels or {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        series, _, value = line.rpartition(" ")
        metric, _, raw_labels = series.partition("{")
        if metric != name:
            continue
        got = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', raw_labels))
        if got == want:
            return float(value)
    return None


async def _scrape(client: AsyncClient, db: AsyncSession) -> str:
    _, _, _, headers = await _metrics_key(db)
    reset_db_gauge_cache()
    resp = await client.get(METRICS_PATH, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.text


# ── route registries match the app ──────────────────────────────────────


def _dependency_calls(dependant) -> list:
    out = []
    for dep in dependant.dependencies:
        out.append(dep.call)
        out.extend(_dependency_calls(dep))
    return out


def test_sweep_and_webhook_route_maps_match_the_app() -> None:
    from sessionfs.server.app import create_app

    app = create_app(ServerConfig(database_url="sqlite+aiosqlite://"))
    sweeper_paths = set()
    post_paths = set()
    from sessionfs.server.route_introspection import iter_api_routes

    for entry in iter_api_routes(app):
        dependant = entry.route.dependant  # type: ignore[attr-defined]
        if "POST" in entry.methods:
            post_paths.add(entry.path)
        for call in _dependency_calls(dependant):
            if "require_platform_sweeper" in getattr(call, "__qualname__", ""):
                sweeper_paths.add(entry.path)
    assert sweeper_paths == set(metrics.SWEEP_ROUTES), sweeper_paths ^ set(metrics.SWEEP_ROUTES)
    assert set(metrics.WEBHOOK_ROUTES) <= post_paths
    # Every sweep the scope test exercises has a named (non-"other") label.
    for path, _, _ in SWEEPS:
        assert metrics.SWEEP_ROUTES[path] != metrics.OTHER


def test_tenant_label_setting_reaches_the_registry(monkeypatch) -> None:
    from sessionfs.server.app import create_app

    monkeypatch.setenv("SFS_METRICS_TENANT_LABELS", "true")
    create_app(ServerConfig(database_url="sqlite+aiosqlite://"))
    assert metrics.tenant_labels_enabled() is True
    monkeypatch.setenv("SFS_METRICS_TENANT_LABELS", "false")
    create_app(ServerConfig(database_url="sqlite+aiosqlite://"))
    assert metrics.tenant_labels_enabled() is False
    monkeypatch.delenv("SFS_METRICS_TENANT_LABELS")
    assert ServerConfig().metrics_tenant_labels is False


# ── sweeps ──────────────────────────────────────────────────────────────


async def test_every_sweep_counts_a_run_and_a_duration(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, _, headers = await _sweep_key(db_session)
    for path, body, _ in SWEEPS:
        resp = await client.post(path, headers=headers, json=body)
        assert resp.status_code == 200, (path, resp.text)
    names = {metrics.SWEEP_ROUTES[path] for path, _, _ in SWEEPS}
    for name in names:
        runs = _sample("sessionfs_sweep_runs_total", {"sweep": name, "result": "ok"})
        assert runs is not None and runs >= 1, name
        count = _sample("sessionfs_sweep_duration_seconds_count", {"sweep": name})
        assert count is not None and count >= 1, name
    # The github-webhook-deliveries sweep has two paths, one label.
    assert _sample(
        "sessionfs_sweep_runs_total", {"sweep": "github-webhook-deliveries", "result": "ok"}
    ) == 2.0
    assert _sample("sessionfs_sweep_runs_total", {"sweep": "other", "result": "ok"}) is None
    # Nothing was denied.
    assert "sessionfs_sweep_denied_total{" not in metrics.render_metrics()


async def test_sweep_lag_reports_the_oldest_due_reminder(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, _, _, headers = await _sweep_key(db_session)
    now = datetime.now(timezone.utc)
    due = now - timedelta(hours=2)
    db_session.add(
        Reminder(
            id=f"rem_{uuid.uuid4().hex[:12]}",
            owner_user_id=admin.id,
            created_by_user_id=admin.id,
            title="Follow up",
            due_at=due,
            original_due_at=due,
        )
    )
    await db_session.commit()
    resp = await client.post("/api/v1/admin/sweeps/reminders-due", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    lag = _sample("sessionfs_sweep_lag_seconds", {"sweep": "reminders-due"})
    assert lag is not None and 7100 <= lag <= 7400, lag
    # Delivered: the next run sees nothing due.
    resp = await client.post("/api/v1/admin/sweeps/reminders-due", headers=headers, json={})
    assert resp.status_code == 200
    assert _sample("sessionfs_sweep_lag_seconds", {"sweep": "reminders-due"}) == 0.0


async def test_sweep_denials_are_counted_by_reason_and_never_as_runs(
    client: AsyncClient, db_session: AsyncSession, auth_headers: dict
) -> None:
    path = "/api/v1/admin/sweeps/jira-sync"
    admin, org_id, _, _ = await _sweep_key(db_session)

    assert (await client.post(path, json={})).status_code == 401
    assert (await client.post(path, headers=auth_headers, json={})).status_code == 403
    _, wrong_scope = await _service_key(
        db_session, owner=admin, org_id=org_id, scopes=["tickets:read"], name="wrong"
    )
    assert (await client.post(path, headers=wrong_scope, json={})).status_code == 403
    _, demoted = await _service_key(
        db_session, owner=admin, org_id=org_id, scopes=[SWEEP_SCOPE], name="demoted"
    )
    minter = User(
        id=str(uuid.uuid4()),
        email=f"m-{uuid.uuid4().hex[:6]}@example.com",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(minter)
    await db_session.commit()
    _, not_admin_minter = await _service_key(
        db_session, owner=admin, org_id=org_id, scopes=[SWEEP_SCOPE], name="m", minter=minter
    )
    assert (await client.post(path, headers=not_admin_minter, json={})).status_code == 403

    denied = {
        reason: _sample("sessionfs_sweep_denied_total", {"sweep": "jira-sync", "reason": reason})
        for reason in ("unauthenticated", "not_admin", "insufficient_scope", "minter_not_admin")
    }
    assert denied == {
        "unauthenticated": 1.0,
        "not_admin": 1.0,
        "insufficient_scope": 1.0,
        "minter_not_admin": 1.0,
    }
    assert _sample("sessionfs_sweep_runs_total", {"sweep": "jira-sync", "result": "ok"}) is None
    assert "sessionfs_sweep_duration_seconds_count" not in metrics.render_metrics()

    # The demoted-key reason is the minter's current standing, too.
    await db_session.execute(update(User).where(User.id == admin.id).values(tier="team"))
    await db_session.commit()
    assert (await client.post(path, headers=demoted, json={})).status_code == 403
    assert _sample(
        "sessionfs_sweep_denied_total", {"sweep": "jira-sync", "reason": "minter_not_admin"}
    ) == 2.0


async def test_a_sweep_that_rejects_its_body_counts_as_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, _, headers = await _sweep_key(db_session)
    resp = await client.post(
        "/api/v1/admin/github-webhook-deliveries/sweep",
        headers=headers,
        json={"retention_days": 1},
    )
    assert resp.status_code == 403
    assert _sample(
        "sessionfs_sweep_runs_total",
        {"sweep": "github-webhook-deliveries", "result": "rejected"},
    ) == 1.0
    assert "sessionfs_sweep_denied_total{" not in metrics.render_metrics()


# ── webhooks ────────────────────────────────────────────────────────────


async def test_webhook_ingest_by_source_and_result(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    org = await js._org(db_session)
    assert (await js._post_webhook(client, org.id, js._webhook_payload("x")))[0] == 404
    owner, _ = await js._user(db_session)
    await js._connection(db_session, org, owner, with_secret=True)
    status, _ = await js._post_webhook(client, org.id, js._webhook_payload("x"), secret="bad")
    assert status == 401
    status, _ = await js._post_webhook(client, org.id, js._webhook_payload("other_event"))
    assert status == 200
    resp = await client.post("/api/v1/webhooks/gitlab/org_nope", content=b"{}")
    assert resp.status_code == 401
    text = metrics.render_metrics()
    assert _sample("sessionfs_webhook_ingest_total", {"source": "jira", "result": "not_found"}, text) == 1.0
    assert _sample("sessionfs_webhook_ingest_total", {"source": "jira", "result": "unauthorized"}, text) == 1.0
    assert _sample("sessionfs_webhook_ingest_total", {"source": "jira", "result": "ok"}, text) == 1.0
    assert _sample(
        "sessionfs_webhook_ingest_total", {"source": "delivery_gitlab", "result": "unauthorized"}, text
    ) == 1.0
    # No org id ever lands in a webhook label, even with tenant labels on.
    assert org.id not in text


# ── Jira ────────────────────────────────────────────────────────────────


async def test_jira_outbound_counts_each_outcome_and_lag(
    db_session: AsyncSession, fake_sync_jira  # noqa: F811
) -> None:
    org, _p, ticket, link = await js._status_fixture(db_session)
    ok = await js._pending_status_row(
        db_session, org, link, ticket,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    failing = await js._pending_status_row(db_session, org, link, ticket)
    last = await js._pending_status_row(db_session, org, link, ticket, retry_count=4)
    ok_id, failing_id, last_id = ok.id, failing.id, last.id
    assert await jira_sync.process_event(db_session, ok_id) == "done"
    fake_sync_jira["state"]["fail_transition"] = True
    assert await jira_sync.process_event(db_session, failing_id) == "failed"
    assert await jira_sync.process_event(db_session, last_id) == "exhausted"
    # Not due: not a delivery attempt, not counted.
    assert await jira_sync.process_event(db_session, ok_id) == "not_due"

    text = metrics.render_metrics()
    for result in ("done", "failed", "exhausted"):
        assert _sample(
            "sessionfs_jira_sync_total", {"direction": "outbound", "result": result}, text
        ) == 1.0, result
    assert _sample("sessionfs_jira_sync_lag_seconds_count", {"direction": "outbound"}, text) == 1.0
    lag_sum = _sample("sessionfs_jira_sync_lag_seconds_sum", {"direction": "outbound"}, text)
    assert lag_sum is not None and 25 <= lag_sum <= 120


async def test_jira_inbound_counts_applied_ignored_duplicate_with_tenant_label(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    set_tenant_labels(True)
    owner, _ = await js._user(db_session)
    org = await js._org(db_session)
    await js._connection(db_session, org, owner, with_secret=True)
    project = await js._project(db_session, owner, org)
    ticket = await js._ticket(db_session, project, owner)
    await js._link(db_session, org, ticket)
    ts = int((datetime.now(timezone.utc) - timedelta(seconds=20)).timestamp() * 1000)
    payload = js._webhook_payload(
        "jira:issue_updated",
        timestamp=ts,
        changelog={"id": "20005", "items": [{"field": "summary", "toString": "X"}]},
        issue={"key": "KEY-1", "id": "10001"},
    )
    assert (await js._post_webhook(client, org.id, payload))[1] == {"status": "ok"}
    assert (await js._post_webhook(client, org.id, payload))[1] == {"status": "duplicate"}
    unknown = js._webhook_payload(
        "comment_created",
        comment={"id": "40002", "body": "x"},
        issue={"key": "NOPE-9", "id": "99"},
    )
    assert (await js._post_webhook(client, org.id, unknown))[1]["status"] == "ignored"

    tenant = {"org_id": org.id}
    text = metrics.render_metrics()
    for result in ("applied", "duplicate", "ignored"):
        assert _sample(
            "sessionfs_jira_sync_total", {"direction": "inbound", "result": result, **tenant}, text
        ) == 1.0, result
    lag_sum = _sample(
        "sessionfs_jira_sync_lag_seconds_sum", {"direction": "inbound", **tenant}, text
    )
    assert lag_sum is not None and 15 <= lag_sum <= 120
    # Tenant labels are ids only — never names or ticket ids.
    assert ticket.id not in text and org.name not in text


# ── Confluence ──────────────────────────────────────────────────────────


async def test_confluence_publish_results(
    client: AsyncClient, db_session: AsyncSession, fake_confluence  # noqa: F811
) -> None:
    _org, _user, key, project, _page = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    assert (await _publish(client, key, project)).status_code == 200
    assert _sample("sessionfs_confluence_publish_total", {"result": "published"}) == 2.0
    # A publish the engine refuses is counted with its bounded result.
    from sessionfs.server.routes import confluence as cr

    async def _drift(db, req):
        raise cr.HTTPException(409, {"error": "confluence_page_drifted", "message": "drifted"})

    original = cr._execute_publish
    cr._execute_publish = _drift  # type: ignore[assignment]
    try:
        resp = await _publish(client, key, project)
    finally:
        cr._execute_publish = original  # type: ignore[assignment]
    assert resp.status_code == 409
    assert _sample("sessionfs_confluence_publish_total", {"result": "drifted"}) == 1.0


# ── agent runs ──────────────────────────────────────────────────────────


async def test_agent_tokens_and_runs_from_usage_complete_and_cancel(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(
        client, project, key, provider="anthropic", model="claude-opus-5-5", role="reviewer"
    )
    url = f"/api/v1/projects/{project.id}/agent-runs/{run_id}"
    resp = await client.post(
        f"{url}/usage", headers=run_hdrs(key), json={"input_tokens": 100, "output_tokens": 40}
    )
    assert resp.status_code == 200, resp.text
    await _start_run(client, project, key, run_id)
    resp = await client.post(
        f"{url}/complete",
        headers=run_hdrs(key),
        json={"status": "passed", "input_tokens": 10, "cache_read_tokens": 7, "cost_usd": 1.5},
    )
    assert resp.status_code == 200, resp.text
    # A refused report adds nothing.
    assert (
        await client.post(f"{url}/usage", headers=run_hdrs(key), json={"input_tokens": 999})
    ).status_code == 409

    other = await _create_run(client, project, key, model="mystery-model")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{other}/cancel", headers=run_hdrs(key)
    )
    assert resp.status_code == 200, resp.text

    text = metrics.render_metrics()

    def tokens(kind: str) -> float | None:
        return _sample(
            "sessionfs_agent_tokens_total", {"token_kind": kind, "model_family": "anthropic"}, text
        )

    assert tokens("input") == 110.0
    assert tokens("output") == 40.0
    assert tokens("cache_read") == 7.0
    assert tokens("cache_write") is None
    assert _sample("sessionfs_agent_runs_total", {"status": "passed", "role": "reviewer"}, text) == 1.0
    assert _sample("sessionfs_agent_runs_total", {"status": "cancelled", "role": "none"}, text) == 1.0
    # Tokens only — no dollars anywhere, no persona or model text in a label.
    assert "1.5" not in text and "atlas" not in text and "claude-opus" not in text


async def test_worker_runs_and_review_rounds_count_tokens_runs_and_loop_items(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    e = await loop_env(db_session, client, max_review_rounds=1)
    started = await _start_worker(client, e, provider="openai", model="gpt-6-astra")
    assert started.status_code == 201, started.text
    resp = await _finish(
        client, e, started.json()["run_id"],
        usage={"input_tokens": 300, "output_tokens": 30, "cost_usd": 0.5},
    )
    assert resp.status_code == 200, resp.text
    # Round 1 parks the item (round cap 1); round 2 is refused but its usage
    # is kept on an errored run.
    r1 = await _round(client, e, provider="anthropic", model="claude-sonnet-5-5",
                      usage={"input_tokens": 50})
    assert r1.status_code == 201, r1.text
    r2 = await _round(client, e, round=2, provider="anthropic", usage={"output_tokens": 5})
    assert r2.status_code == 409

    text = metrics.render_metrics()
    assert _sample(
        "sessionfs_agent_tokens_total", {"token_kind": "input", "model_family": "openai"}, text
    ) == 300.0
    assert _sample(
        "sessionfs_agent_tokens_total", {"token_kind": "input", "model_family": "anthropic"}, text
    ) == 50.0
    assert _sample(
        "sessionfs_agent_tokens_total", {"token_kind": "output", "model_family": "anthropic"}, text
    ) == 5.0
    assert _sample("sessionfs_agent_runs_total", {"status": "passed", "role": "implementer"}, text) == 1.0
    assert _sample("sessionfs_agent_runs_total", {"status": "passed", "role": "reviewer"}, text) == 1.0
    assert _sample("sessionfs_agent_runs_total", {"status": "errored", "role": "reviewer"}, text) == 1.0

    # DB-derived gauge on a real scrape: the parked child is needs_human.
    scraped = await _scrape(client, db_session)
    assert _sample("sessionfs_review_loop_items", {"status": "needs_human"}, scraped) == 1.0
    assert _sample("sessionfs_review_loop_items", {"status": "done"}, scraped) == 0.0
    assert e.child_id not in scraped and e.project.id not in scraped


async def test_review_loop_gauge_tenant_labels(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    e = await loop_env(db_session, client)
    set_tenant_labels(True)
    scraped = await _scrape(client, db_session)
    values = {
        status: _sample(
            "sessionfs_review_loop_items", {"status": status, "project_id": e.project.id}, scraped
        )
        for status in ("pending",)
    }
    assert values == {"pending": 1.0}


# ── approvals ───────────────────────────────────────────────────────────


async def test_approval_gauge_and_decision_histogram(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    w = await make_world(db_session)
    await make_policy(db_session, w.org, name="Auth changes")
    from tests.server.integration.test_approvals_api import _readiness

    rid = (await _readiness(client, w, w.owner_key))["approvals"][0]["request_id"]
    before = await _scrape(client, db_session)
    assert _sample("sessionfs_approval_requests", {"status": "pending"}, before) == 1.0
    assert _sample("sessionfs_approval_requests", {"status": "approved"}, before) == 0.0

    resp = await client.post(
        f"/api/v1/approval-requests/{rid}/decide", headers=hdrs(w.admin_key),
        json={"decision": "approve", "reason": "Looks right."},
    )
    assert resp.status_code == 200, resp.text
    after = await _scrape(client, db_session)
    assert _sample("sessionfs_approval_requests", {"status": "pending"}, after) == 0.0
    assert _sample("sessionfs_approval_requests", {"status": "approved"}, after) == 1.0
    assert _sample("sessionfs_approval_decision_seconds_count", {"decision": "approved"}, after) == 1.0
    # A refused second decision observes nothing more.
    again = await client.post(
        f"/api/v1/approval-requests/{rid}/decide", headers=hdrs(w.admin_key),
        json={"decision": "approve", "reason": "Again."},
    )
    assert again.status_code in (403, 409)
    assert _sample("sessionfs_approval_decision_seconds_count", {"decision": "approved"}) == 1.0
    # No policy name or request id in any label.
    assert "Auth changes" not in after and rid not in after


async def test_gauge_query_failure_never_fails_the_scrape(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    import sessionfs.server.business_metrics as bm

    def boom() -> object:
        raise RuntimeError("count() exploded")

    monkeypatch.setattr(bm, "m_count", boom)
    scraped = await _scrape(client, db_session)
    assert "sessionfs_sessions " in scraped


async def test_lag_query_failure_never_fails_the_sweep(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    import sessionfs.server.business_metrics as bm

    async def boom(*_a, **_k):
        raise RuntimeError("lag exploded")

    monkeypatch.setattr(bm, "_oldest_due", boom)
    _, _, _, headers = await _sweep_key(db_session)
    resp = await client.post("/api/v1/admin/sweeps/reminders-due", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    assert _sample("sessionfs_sweep_runs_total", {"sweep": "reminders-due", "result": "ok"}) == 1.0


def test_payload_is_json_serialisable_for_docs() -> None:
    # The metrics reference page is generated from these vocabularies.
    json.dumps(metrics.BUSINESS_LABEL_VOCABULARIES)
