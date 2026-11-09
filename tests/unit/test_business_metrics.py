"""Business metric families (GitHub #131, tk_35b319c6dac74cc9).

Pure checks: closed label vocabularies, the worst-case cardinality of every
new family against the per-metric series cap, the per-tenant label switch,
the bounded label mappers, and that no recording helper can raise."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from sessionfs.server import business_metrics as bm
from sessionfs.server import metrics
from sessionfs.server.metrics import (
    AGENT_TOKENS_TOTAL,
    BUSINESS_LABEL_VOCABULARIES,
    DEFAULT_MAX_SERIES,
    JIRA_SYNC_TOTAL,
    OTHER,
    REVIEW_LOOP_ITEMS,
    Counter,
    Gauge,
    clamp_label,
    model_family,
    sweep_result,
    webhook_result,
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    metrics.reset_registry()
    metrics.set_tenant_labels(False)
    yield
    metrics.reset_registry()
    metrics.set_tenant_labels(False)


def _by_name() -> dict[str, metrics._Metric]:
    return {m.name: m for m in metrics._REGISTRY}


# ── cardinality ──────────────────────────────────────────────────────────


def test_every_business_family_is_registered_with_its_declared_labels() -> None:
    registered = _by_name()
    for name, vocab in BUSINESS_LABEL_VOCABULARIES.items():
        assert name in registered, name
        assert set(registered[name].base_label_names) == set(vocab), name


def test_worst_case_cardinality_fits_the_series_cap() -> None:
    """Worst case = the cross product of each label's closed vocabulary, plus
    the ``other`` fallback ``clamp_label`` can add. Tenant labels are off by
    default and excluded here."""
    total = 0
    for name, vocab in BUSINESS_LABEL_VOCABULARIES.items():
        sizes = [len(set(values) | {OTHER}) for values in vocab.values()]
        series = math.prod(sizes)
        assert series <= DEFAULT_MAX_SERIES, (name, series)
        total += series
    # Every new family together is still far below one metric's cap.
    assert total <= DEFAULT_MAX_SERIES, total


def test_no_label_carries_a_user_typed_name_or_an_id() -> None:
    forbidden = re.compile(
        r"(persona|name|slug|template|suite|policy|step|title|email|"
        r"user|ticket|meeting|session|run_id|loop_id|key)"
    )
    for metric in metrics._REGISTRY:
        for label in metric.base_label_names:
            assert not forbidden.search(label), (metric.name, label)
            # Ids are only ever the optional tenant labels.
            assert label not in metrics.TENANT_LABEL_NAMES, (metric.name, label)
    for name, vocab in BUSINESS_LABEL_VOCABULARIES.items():
        for label, values in vocab.items():
            for value in values:
                assert re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", value), (name, label, value)


def test_no_dollar_metric_exists() -> None:
    for metric in metrics._REGISTRY:
        assert "usd" not in metric.name and "cost" not in metric.name, metric.name
        assert "dollar" not in metric.help.lower(), metric.name


def test_sweep_and_webhook_vocabularies_are_closed() -> None:
    assert OTHER in metrics.SWEEP_NAMES
    assert len(metrics.SWEEP_NAMES) == len(set(metrics.SWEEP_NAMES))
    assert set(metrics.WEBHOOK_SOURCES) == {
        "github", "gitlab", "jira", "delivery_github", "delivery_gitlab", "stripe"
    }


# ── tenant labels ───────────────────────────────────────────────────────


def _lines(name: str) -> list[str]:
    return [line for line in metrics.render_metrics().splitlines() if line.startswith(name)]


def test_tenant_labels_off_aggregates_across_tenants() -> None:
    JIRA_SYNC_TOTAL.inc(direction="outbound", result="done", org_id="org_a")
    JIRA_SYNC_TOTAL.inc(direction="outbound", result="done", org_id="org_b")
    lines = _lines("sessionfs_jira_sync_total{")
    assert lines == ['sessionfs_jira_sync_total{direction="outbound",result="done"} 2.0']


def test_tenant_labels_on_split_by_org_and_project_ids() -> None:
    metrics.set_tenant_labels(True)
    AGENT_TOKENS_TOTAL.inc(
        10, token_kind="input", model_family="anthropic", org_id="org_a", project_id="proj_1"
    )
    AGENT_TOKENS_TOTAL.inc(5, token_kind="input", model_family="anthropic", org_id="org_b")
    AGENT_TOKENS_TOTAL.inc(1, token_kind="input", model_family="anthropic")
    lines = set(_lines("sessionfs_agent_tokens_total{"))
    assert lines == {
        'sessionfs_agent_tokens_total{token_kind="input",model_family="anthropic"} 1.0',
        'sessionfs_agent_tokens_total{token_kind="input",model_family="anthropic",'
        'org_id="org_a",project_id="proj_1"} 10.0',
        'sessionfs_agent_tokens_total{token_kind="input",model_family="anthropic",'
        'org_id="org_b"} 5.0',
    }


def test_tenant_label_values_must_be_ids() -> None:
    metrics.set_tenant_labels(True)
    JIRA_SYNC_TOTAL.inc(direction="inbound", result="applied", org_id='Acme "Corp"\n')
    JIRA_SYNC_TOTAL.inc(direction="inbound", result="applied", org_id="x" * 65)
    assert _lines("sessionfs_jira_sync_total{") == [
        'sessionfs_jira_sync_total{direction="inbound",result="applied"} 2.0'
    ]


def test_tenant_labels_only_on_families_that_declare_them() -> None:
    with pytest.raises(ValueError):
        metrics.SWEEP_RUNS_TOTAL.inc(sweep="jira-sync", result="ok", org_id="org_a")


def test_gauge_clear_drops_every_series() -> None:
    REVIEW_LOOP_ITEMS.set(3, status="needs_human")
    REVIEW_LOOP_ITEMS.clear()
    assert _lines("sessionfs_review_loop_items{") == []


# ── bounded mappers ─────────────────────────────────────────────────────


def test_clamp_label() -> None:
    assert clamp_label("done", ("done",)) == "done"
    assert clamp_label("My Persona", ("done",)) == OTHER
    assert clamp_label(None, ("done",)) == OTHER


def test_model_family_uses_the_provider_registry() -> None:
    assert model_family("anthropic", "whatever") == "anthropic"
    assert model_family("OpenAI", None) == "openai"
    assert model_family(None, "claude-sonnet-5-5") == "anthropic"
    assert model_family("p1", "gpt-6-astra") == "openai"
    assert model_family(None, "gemini-3.6-flash") == "google"
    assert model_family(None, "deepseek/deepseek-chat") == "deepseek"
    assert model_family("my-own-gateway", "acme-secret-model") == OTHER
    assert model_family(None, "acme/custom") == OTHER
    assert model_family(None, None) == OTHER
    assert set(metrics.MODEL_FAMILIES) >= {"anthropic", "openai", "google", OTHER}


def test_status_mappers() -> None:
    assert sweep_result(200) == "ok"
    assert sweep_result(403) == "rejected"
    assert sweep_result(500) == "error"
    assert sweep_result(None) == "error"
    assert webhook_result(200) == "ok"
    assert webhook_result(401) == "unauthorized"
    assert webhook_result(404) == "not_found"
    assert webhook_result(413) == "too_large"
    assert webhook_result(400) == "rejected"
    assert webhook_result(503) == "unavailable"
    assert webhook_result(500) == "error"
    assert webhook_result(None) == "error"


def test_sweep_denial_reasons() -> None:
    assert bm.sweep_denial_reason(401, None, stage="auth") == "unauthenticated"
    assert bm.sweep_denial_reason(429, None, stage="auth") == "rate_limited"
    assert bm.sweep_denial_reason(403, None, stage="auth") == "auth_rejected"
    assert bm.sweep_denial_reason(403, "x", stage="admin") == "not_admin"
    assert bm.sweep_denial_reason(403, {}, stage="scope") == "insufficient_scope"
    assert bm.sweep_denial_reason(403, {"reason": "minter_not_admin"}, stage="minter") == (
        "minter_not_admin"
    )
    assert bm.sweep_denial_reason(403, {"reason": "free text"}, stage="minter") == OTHER


def test_sweep_name_for_unknown_path_is_other() -> None:
    assert bm.sweep_name_for_path("/api/v1/admin/sweeps/reminders-due") == "reminders-due"
    assert bm.sweep_name_for_path("/api/v1/admin/sweeps/{anything}") == OTHER
    assert bm.sweep_name_for_path(None) == OTHER


def test_confluence_result_for_exception() -> None:
    def r(status, detail=None):
        return bm.confluence_result_for_exception(HTTPException(status, detail))

    assert r(409, {"error": "publish_in_progress"}) == "in_progress"
    assert r(409, {"error": "confluence_page_drifted"}) == "drifted"
    assert r(422, {"error": "content_too_large_for_confluence"}) == "too_large"
    assert r(504, {"error": "confluence_timeout"}) == "timeout"
    assert r(409, {"error": "confluence_title_conflict"}) == "conflict"
    assert r(403, "nope") == "rejected"
    assert r(502, "upstream") == "error"
    assert bm.confluence_result_for_exception(RuntimeError("x")) == "error"


def test_jira_inbound_lag_is_bounded() -> None:
    now = datetime.now(timezone.utc)
    ts = int((now - timedelta(seconds=12)).timestamp() * 1000)
    lag = bm.jira_inbound_lag_seconds({"timestamp": ts}, now)
    assert lag is not None and 11 <= lag <= 13
    assert bm.jira_inbound_lag_seconds({"timestamp": 1700000000000}, now) is None  # > 30 days
    future = int((now + timedelta(minutes=5)).timestamp() * 1000)
    assert bm.jira_inbound_lag_seconds({"timestamp": future}, now) is None
    assert bm.jira_inbound_lag_seconds({"timestamp": True}, now) is None
    assert bm.jira_inbound_lag_seconds({"timestamp": "soon"}, now) is None
    assert bm.jira_inbound_lag_seconds(None, now) is None


def test_agent_tokens_count_only_positive_ints_per_kind() -> None:
    bm.record_agent_tokens(
        provider=None, model="claude-opus-5-5",
        input_tokens=100, output_tokens=0, cache_read_tokens=None, cache_write_tokens=True,
    )
    assert _lines("sessionfs_agent_tokens_total{") == [
        'sessionfs_agent_tokens_total{token_kind="input",model_family="anthropic"} 100.0'
    ]


def test_agent_runs_clamp_role_and_skip_non_terminal_status() -> None:
    bm.record_agent_run_finished(status="running", role="implementer")
    bm.record_agent_run_finished(status="passed", role="Persona Atlas")
    bm.record_agent_run_finished(status="errored", role=None)
    assert set(_lines("sessionfs_agent_runs_total{")) == {
        'sessionfs_agent_runs_total{status="passed",role="other"} 1.0',
        'sessionfs_agent_runs_total{status="errored",role="none"} 1.0',
    }


def test_approval_decision_only_for_approved_or_rejected() -> None:
    t0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    bm.observe_approval_decision(state="expired", requested_at=t0, decided_at=t0)
    bm.observe_approval_decision(
        state="approved", requested_at=t0.replace(tzinfo=None), decided_at=t0 + timedelta(hours=2)
    )
    assert 'sessionfs_approval_decision_seconds_count{decision="approved"} 1.0' in (
        metrics.render_metrics()
    )
    assert 'sessionfs_approval_decision_seconds_sum{decision="approved"} 7200.0' in (
        metrics.render_metrics()
    )
    assert "expired" not in "\n".join(_lines("sessionfs_approval_decision_seconds"))


def test_extraction_server_time_excludes_the_provider_and_clamps_labels() -> None:
    bm.observe_extraction_server_time(
        kind="chat", result="done", total_seconds=31.5, provider_seconds=31.25
    )
    # A provider span longer than the wall time (clock noise) never goes negative.
    bm.observe_extraction_server_time(
        kind="weird", result="exploded", total_seconds=1.0, provider_seconds=2.0
    )
    text = metrics.render_metrics()
    assert 'sessionfs_extraction_server_seconds_sum{kind="chat",result="done"} 0.25' in text
    assert 'sessionfs_extraction_server_seconds_sum{kind="other",result="other"} 0.0' in text


# ── fail-safe ────────────────────────────────────────────────────────────


def test_recording_helpers_never_raise(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("registry exploded")

    for cls in (Counter, Gauge, metrics.Histogram):
        for attr in ("inc", "set", "observe"):
            if hasattr(cls, attr):
                monkeypatch.setattr(cls, attr, boom)

    bm.record_sweep_denied("jira-sync", "not_admin")
    bm.record_sweep_run("jira-sync", 200, 1.0)
    bm.record_webhook_ingest("/webhooks/github", 200)
    bm.record_jira_sync("outbound", "done", org_id="org_a", lag_seconds=3)
    bm.record_confluence_publish("published")
    bm.record_agent_tokens(provider="openai", model=None, input_tokens=5)
    bm.record_agent_run_finished(status="passed", role="reviewer")
    t0 = datetime.now(timezone.utc)
    bm.observe_approval_decision(state="approved", requested_at=t0, decided_at=t0)
    bm.observe_extraction_server_time(
        kind="meeting", result="done", total_seconds=1.0, provider_seconds=0.5
    )

    class _Req:
        scope = {"route": None}

    bm.observe_route(_Req(), 200, 1.0)
    bm.observe_route(object(), 200, 1.0)  # no scope at all


# ── docs ─────────────────────────────────────────────────────────────────


def test_grafana_dashboard_and_reference_page_cover_every_family() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    dashboard = json.loads((root / "docs" / "observability" / "sessionfs-ops.json").read_text())
    exprs = "\n".join(
        t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])
    )
    for name in BUSINESS_LABEL_VOCABULARIES:
        assert name in exprs, f"no Grafana panel for {name}"
    reference = (root / "site" / "src" / "content" / "docs" / "metrics.mdx").read_text()
    for metric in metrics._REGISTRY:
        assert f"`{metric.name}`" in reference, f"{metric.name} missing from the reference"
    # The hosted service's billing webhook source is real but deliberately not
    # named on the public site (operator billing internals stay private).
    hosted_only = {("sessionfs_webhook_ingest_total", "stripe")}
    for name, vocab in BUSINESS_LABEL_VOCABULARIES.items():
        for values in vocab.values():
            for value in values:
                if (name, value) in hosted_only:
                    continue
                assert f"`{value}`" in reference, (name, value)
    assert "SFS_METRICS_TENANT_LABELS" in reference
