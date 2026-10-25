"""Unit tests for the in-process Prometheus registry (GitHub #90).

These cover the pure machinery: label allowlists, series-cap cardinality
guarding, text-format rendering (escaping + histogram buckets), and the bounded
label-vocabulary helpers used on the hot path."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sessionfs.server import metrics
from sessionfs.server.metrics import (
    AUTH_FAILURES_TOTAL,
    HTTP_REQUEST_DURATION,
    SYNC_PULL_TOTAL,
    Counter,
    Histogram,
    action_label,
    method_label,
    push_outcome,
    push_size_bytes,
    severity_label,
    size_bucket,
    status_class,
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    metrics.reset_registry()
    yield
    metrics.reset_registry()


def test_status_class_and_size_bucket_bounds() -> None:
    assert status_class(200) == "2xx"
    assert status_class(404) == "4xx"
    assert status_class(503) == "5xx"

    mb = 1024 * 1024
    assert size_bucket(0) == "<1MB"
    assert size_bucket(mb - 1) == "<1MB"
    assert size_bucket(mb) == "1-10MB"
    assert size_bucket(10 * mb) == "10-50MB"
    assert size_bucket(50 * mb) == "50-100MB"
    assert size_bucket(100 * mb) == ">100MB"


def test_push_outcome_maps_status_and_dlp_envelope() -> None:
    assert push_outcome(200) == "ok"
    assert push_outcome(201) == "ok"
    assert push_outcome(409) == "conflict"
    assert push_outcome(413) == "rejected_quota"
    assert push_outcome(403, {"error": "dlp_blocked"}) == "rejected_dlp"
    assert push_outcome(400, {"error": "dlp_uninspectable"}) == "rejected_dlp"
    # A 403 that is not a DLP block is a plain error.
    assert push_outcome(403, {"error": "something_else"}) == "error"
    assert push_outcome(500) == "error"


def test_counter_renders_help_type_and_value() -> None:
    SYNC_PULL_TOTAL.inc()
    SYNC_PULL_TOTAL.inc()
    out = metrics.render_metrics()
    assert "# HELP sessionfs_sync_pull_total Session pulls." in out
    assert "# TYPE sessionfs_sync_pull_total counter" in out
    assert "sessionfs_sync_pull_total 2.0" in out


def test_label_values_are_escaped() -> None:
    AUTH_FAILURES_TOTAL.inc(reason='bad"reason\\path\nnewline')
    out = metrics.render_metrics()
    assert 'reason="bad\\"reason\\\\path\\nnewline"' in out


def test_label_allowlist_rejects_unknown_and_missing_labels() -> None:
    with pytest.raises(ValueError):
        HTTP_REQUEST_DURATION.observe(
            0.1, method="GET", route="/x", status_class="2xx", user_id="u-1"
        )
    with pytest.raises(ValueError):
        HTTP_REQUEST_DURATION.observe(0.1, method="GET", route="/x")


def test_histogram_renders_cumulative_buckets_sum_and_count() -> None:
    HTTP_REQUEST_DURATION.observe(0.003, method="GET", route="/x", status_class="2xx")
    HTTP_REQUEST_DURATION.observe(0.006, method="GET", route="/x", status_class="2xx")
    HTTP_REQUEST_DURATION.observe(2.0, method="GET", route="/x", status_class="2xx")
    out = metrics.render_metrics()

    # Three observations, all <= 0.005, 0.01 and above; only two <= 0.005.
    assert 'http_request_duration_seconds_bucket{method="GET",route="/x",status_class="2xx",le="0.005"} 1.0' in out
    assert 'http_request_duration_seconds_bucket{method="GET",route="/x",status_class="2xx",le="0.01"} 2.0' in out
    assert 'http_request_duration_seconds_bucket{method="GET",route="/x",status_class="2xx",le="10.0"} 3.0' in out
    assert 'http_request_duration_seconds_bucket{method="GET",route="/x",status_class="2xx",le="+Inf"} 3.0' in out
    assert 'http_request_duration_seconds_sum{method="GET",route="/x",status_class="2xx"} 2.009' in out
    assert 'http_request_duration_seconds_count{method="GET",route="/x",status_class="2xx"} 3.0' in out


def test_histogram_sorts_buckets() -> None:
    h = Histogram("test_h", "help", buckets=(1.0, 0.5, 0.25), label_names=())
    h.observe(0.1)
    out = "\n".join(h.render())
    # Buckets must be emitted in ascending order regardless of declaration.
    assert out.index('test_h_bucket{le="0.25"} 1.0') < out.index('test_h_bucket{le="0.5"} 1.0')
    assert out.index('test_h_bucket{le="0.5"} 1.0') < out.index('test_h_bucket{le="1.0"} 1.0')


def test_cardinality_guard_drops_series_past_the_cap() -> None:
    c = Counter("test_capped", "help", label_names=("k",), max_series=2)
    c.inc(k="a")
    c.inc(k="b")
    c.inc(k="c")  # past the cap: dropped
    rendered = "\n".join(c.render())
    assert 'test_capped{k="a"} 1.0' in rendered
    assert 'test_capped{k="b"} 1.0' in rendered
    assert 'test_capped{k="c"}' not in rendered


def test_help_text_escapes_backslash_and_newline() -> None:
    # HELP text is freeform: only backslash and newline need escaping, quotes
    # do not (they'd break nothing on a single HELP line).
    c = Counter("test_help", 'line one\nline two \\ tail', label_names=())
    rendered = "\n".join(c.render())
    assert '# HELP test_help line one\\nline two \\\\ tail' in rendered


def test_method_label_clamps_unknown_methods() -> None:
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        assert method_label(method) == method
    assert method_label("PROPFIND") == "other"
    assert method_label("TRACE") == "other"
    assert method_label("") == "other"


def test_severity_and_action_labels_clamp_to_known_sets() -> None:
    for severity in ("critical", "high", "medium", "low"):
        assert severity_label(severity) == severity
    # Tenant-defined custom-pattern text must not become an unbounded label.
    assert severity_label("zzz") == "other"
    assert severity_label("CRITICAL") == "other"

    for action in ("block", "redact", "warn"):
        assert action_label(action) == action
    assert action_label("zzz") == "other"


def test_push_size_bytes_falls_back_to_content_length() -> None:
    assert push_size_bytes(123, None) == 123
    assert push_size_bytes(None, "60000000") == 60000000
    assert push_size_bytes(None, None) == 0
    assert push_size_bytes(None, "") == 0
    assert push_size_bytes(None, "not-a-number") == 0
    assert push_size_bytes(None, "-5") == 0


def test_unlabelled_counters_render_zero_from_start() -> None:
    out = metrics.render_metrics()
    assert "sessionfs_sync_pull_total 0.0" in out
    assert "sessionfs_handoff_create_total 0.0" in out
    assert "sessionfs_handoff_claim_total 0.0" in out
    assert "sessionfs_metrics_series_dropped_total 0.0" in out


def test_dropped_counter_increments_when_cap_trips() -> None:
    c = Counter("test_capped", "help", label_names=("k",), max_series=2)
    c.inc(k="a")
    c.inc(k="b")
    c.inc(k="c")  # past the cap: dropped
    out = metrics.render_metrics()
    assert "sessionfs_metrics_series_dropped_total 1.0" in out


def _emitted_metric_names() -> set[str]:
    names: set[str] = set()
    for metric in metrics._REGISTRY:
        names.add(metric.name)
        if isinstance(metric, Histogram):
            names.add(f"{metric.name}_bucket")
            names.add(f"{metric.name}_sum")
            names.add(f"{metric.name}_count")
    return names


def _dashboard_exprs() -> list[str]:
    path = Path(__file__).resolve().parents[2] / "docs" / "observability" / "sessionfs-ops.json"
    data = json.loads(path.read_text())
    return [
        target["expr"]
        for panel in data["panels"]
        for target in panel.get("targets", [])
    ]


def test_grafana_dashboard_is_valid_and_references_emitted_metrics() -> None:
    exprs = _dashboard_exprs()
    assert exprs, "dashboard has no expressions"

    emitted = _emitted_metric_names()
    token = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
    for expr in exprs:
        for name in token.findall(expr):
            if name.startswith("sessionfs_") or name.startswith("http_request_duration_seconds"):
                assert name in emitted, f"{expr!r} references unknown metric {name!r}"


def test_grafana_uses_max_for_db_wide_gauges() -> None:
    exprs = _dashboard_exprs()
    # Active users and total sessions are identical on every replica; summing
    # them would over-count by the replica count, so the dashboard takes max.
    assert "max by (window) (sessionfs_active_users)" in exprs
    assert "max(sessionfs_sessions)" in exprs
    assert "sum by (window) (sessionfs_active_users)" not in exprs
    assert "sessionfs_sessions_total" not in "\n".join(exprs)
    # Each replica has its own DB pool, so capacity is summed like usage.
    assert "sum(sessionfs_db_pool_size)" in exprs
    assert "max(sessionfs_db_pool_size)" not in exprs
