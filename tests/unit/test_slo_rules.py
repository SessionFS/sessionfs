"""The published SLO rules point at series the API really emits.

Pure checks on the Prometheus rules file (shipped in the Helm chart and in
docs/observability), the SLO page and the Helm PrometheusRule template: a
renamed route, metric or histogram bucket would otherwise leave an SLO that
silently measures nothing."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from sessionfs.server import metrics

ROOT = Path(__file__).resolve().parents[2]
CHART_RULES = ROOT / "charts" / "sessionfs" / "files" / "slo-rules.yaml"
DOCS_RULES = ROOT / "docs" / "observability" / "sessionfs-slo-rules.yaml"
SLO_PAGE = ROOT / "site" / "src" / "content" / "docs" / "slos.mdx"
CHART_DIR = ROOT / "charts" / "sessionfs"


def _rules() -> list[dict]:
    doc = yaml.safe_load(CHART_RULES.read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


def _exprs() -> str:
    return "\n".join(rule["expr"] for rule in _rules())


def test_the_two_copies_are_identical() -> None:
    assert CHART_RULES.read_text() == DOCS_RULES.read_text(), (
        "cp charts/sessionfs/files/slo-rules.yaml docs/observability/sessionfs-slo-rules.yaml"
    )


def test_every_rule_is_a_record_or_an_alert_with_severity() -> None:
    names = set()
    for rule in _rules():
        assert ("record" in rule) != ("alert" in rule), rule
        name = rule.get("record") or rule["alert"]
        assert name not in names, name
        names.add(name)
        if "alert" in rule:
            assert rule["labels"]["severity"] in ("page", "ticket"), rule["alert"]
            assert rule["labels"]["slo"], rule["alert"]
            assert rule["annotations"]["summary"], rule["alert"]
    # Every alert reads only recording rules defined here or raw metrics.
    for ref in re.findall(r"sessionfs:[a-z_:0-9]+", _exprs()):
        assert ref in names, ref


def test_every_metric_in_the_rules_is_registered() -> None:
    registered = {m.name for m in metrics._REGISTRY}
    for name in set(re.findall(r"\b((?:http|sessionfs)_[a-z_]+?)(?:_bucket|_count|_sum)?\{", _exprs())):
        assert name in registered, name
    for name in re.findall(r"\b(sessionfs_[a-z_]+?)(?:_bucket|_count)\[", _exprs()):
        assert name in registered, name


def test_le_thresholds_are_real_bucket_bounds() -> None:
    by_name = {m.name: m for m in metrics._REGISTRY}
    # Label values (route templates) contain braces: match quoted strings whole.
    pairs = re.findall(r'\b([a-z_]+)_bucket\{((?:[^}"]|"[^"]*")*)\}', _exprs())
    assert pairs
    for metric, labels in pairs:
        le = re.search(r'le="([^"]+)"', labels)
        assert le, (metric, labels)
        bounds = {metrics._float_text(b) for b in by_name[metric]._buckets}
        assert le.group(1) in bounds, (metric, le.group(1), sorted(bounds))


def test_routes_in_the_rules_are_app_route_templates() -> None:
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    app = create_app(ServerConfig(database_url="sqlite+aiosqlite://"))
    from sessionfs.server.route_introspection import iter_routes

    by_path: dict[str, set[str]] = {}
    for entry in iter_routes(app):
        if entry.methods:
            by_path.setdefault(entry.path, set()).update(entry.methods)
    pairs = set(re.findall(r'method="([A-Z]+)",route="([^"]+)"', _exprs()))
    assert pairs, "no route selectors found"
    for method, path in pairs:
        assert method in by_path.get(path, set()), (method, path)


def test_slo_page_shows_each_slo_query() -> None:
    page = SLO_PAGE.read_text()
    for needle in (
        'route="/api/v1/projects/{project_id}/tickets/{ticket_id}/comments"',
        'route="/api/v1/sessions/{session_id}/sync"',
        "sessionfs_extraction_server_seconds_bucket",
        'sessionfs_sweep_lag_seconds{sweep="reminders-due"}',
        "28 days",
        "api.metrics.prometheusRule.enabled",
    ):
        assert needle in page, needle


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_helm_prometheus_rule_is_off_by_default_and_renders_the_rules() -> None:
    def render(*extra: str) -> list[dict]:
        out = subprocess.run(
            ["helm", "template", "sessionfs", str(CHART_DIR), *extra],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "PrometheusRule"]

    assert render() == []
    (rule,) = render(
        "--set", "api.metrics.prometheusRule.enabled=true",
        "--set", "api.metrics.prometheusRule.labels.release=kps",
    )
    assert rule["metadata"]["labels"]["release"] == "kps"
    assert rule["spec"]["groups"] == yaml.safe_load(CHART_RULES.read_text())["groups"]
    (without,) = render(
        "--set", "api.metrics.prometheusRule.enabled=true",
        "--set", "api.metrics.prometheusRule.reminderSweep=false",
    )
    assert [g["name"] for g in without["spec"]["groups"]] == ["sessionfs-slo-latency"]
