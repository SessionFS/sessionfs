"""Chart rendering for the scheduled platform sweeps (GitHub #115): one opt-in
CronJob per sweep driven by a single shared template, plus the matching
NetworkPolicy ingress rules. Skipped when ``helm`` is not on PATH."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = str(Path(__file__).resolve().parents[2] / "charts" / "sessionfs")

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

SWEEPS = [
    ("ticketNotifications", "ticket-notifications", "*/10 * * * *", "/api/v1/admin/sweeps/ticket-notifications", None),
    ("orgDigest", "org-digest", "0 4 * * *", "/api/v1/admin/sweeps/org-digest", None),
    ("kbCompile", "kb-compile", "*/15 * * * *", "/api/v1/admin/sweeps/kb-compile", None),
    ("jiraSync", "jira-sync", "* * * * *", "/api/v1/admin/sweeps/jira-sync", '{"limit":100,"budget_seconds":50}'),
    ("confluencePublish", "confluence-publish", "*/15 * * * *", "/api/v1/admin/sweeps/confluence-publish", None),
]


def _render(*extra: str) -> list[dict]:
    result = subprocess.run(
        ["helm", "template", "sessionfs", CHART_DIR, *extra], capture_output=True, text=True, check=True
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _find(docs: list[dict], kind: str, name: str) -> dict | None:
    for doc in docs:
        if doc.get("kind") == kind and doc["metadata"]["name"].endswith(name):
            return doc
    return None


def _sweep_sets(secret: str = "sweep-key") -> list[str]:
    out: list[str] = []
    for key, _, _, _, _ in SWEEPS:
        out += ["--set", f"api.{key}.enabled=true", "--set", f"api.{key}.cronJob.existingSecret={secret}"]
    return out


def test_sweep_cronjobs_disabled_by_default() -> None:
    docs = _render()
    for _, component, _, _, _ in SWEEPS:
        assert _find(docs, "CronJob", component) is None, component


def test_sweep_cronjobs_render_when_enabled() -> None:
    docs = _render(*_sweep_sets())
    for key, component, schedule, path, body in SWEEPS:
        cron = _find(docs, "CronJob", component)
        assert cron is not None, component
        assert cron["spec"]["schedule"] == schedule, component
        assert cron["spec"]["concurrencyPolicy"] == "Forbid", component
        container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
        script = container["args"][0]
        assert path in script, component
        assert "Bearer $SFS_" in script, component
        env = {e["name"]: e for e in container["env"]}
        if body is None:
            assert "-d " not in script, component
            assert "SWEEP_BODY" not in env, component
        else:
            # The body travels through the environment, never spliced into
            # the shell script, so a quote in it can't break the command.
            assert '-d "$SWEEP_BODY"' in script, component
            assert env["SWEEP_BODY"]["value"] == body, component
            assert body not in script, component
        # The key is read from the environment, never templated into argv.
        key_env = [e for e in container["env"] if "valueFrom" in e]
        assert len(key_env) == 1, component
        assert key_env[0]["valueFrom"]["secretKeyRef"]["name"] == "sweep-key", component
        assert key_env[0]["name"] not in container.get("command", []), component
        # The pod never talks to the Kubernetes API.
        pod_spec = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        assert pod_spec["automountServiceAccountToken"] is False, component


def test_sweep_cronjobs_open_networkpolicy_ingress() -> None:
    docs = _render(
        *_sweep_sets(),
        "--set", "security.networkPolicies.enabled=true",
    )
    policy = _find(docs, "NetworkPolicy", "-api")
    assert policy is not None
    sources = [
        peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
        for rule in policy["spec"]["ingress"]
        for peer in rule.get("from", [])
    ]
    for _, component, _, _, _ in SWEEPS:
        assert component in sources, component


def _cronjob_policy_components(docs: list[dict]) -> list[str]:
    policy = _find(docs, "NetworkPolicy", "-cronjobs")
    assert policy is not None, "expected a -cronjobs egress NetworkPolicy"
    return policy["spec"]["podSelector"]["matchExpressions"][0]["values"]


def test_sweep_cronjobs_egress_networkpolicy() -> None:
    docs = _render(
        *_sweep_sets(),
        "--set", "security.networkPolicies.enabled=true",
    )
    policy = _find(docs, "NetworkPolicy", "-cronjobs")
    assert policy is not None
    components = policy["spec"]["podSelector"]["matchExpressions"][0]["values"]
    for _, component, _, _, _ in SWEEPS:
        assert component in components, component
    egress = policy["spec"]["egress"]
    # DNS (UDP + TCP 53) and the API pods on the API port are the only egress.
    assert {
        "to": [{"namespaceSelector": {}}],
        "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
    } in egress, egress
    api_rules = [
        rule
        for rule in egress
        if any(
            peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
            == "api"
            for peer in rule.get("to", [])
        )
    ]
    assert api_rules, egress
    assert {"port": 8000, "protocol": "TCP"} in api_rules[0].get("ports", [])


def test_cronjob_egress_policy_includes_helper_retention_and_registry_poll() -> None:
    docs = _render(
        *_sweep_sets(),
        "--set", "security.networkPolicies.enabled=true",
        "--set", "api.helperRetention.enabled=true",
        "--set", "api.helperRetention.cronJob.existingSecret=sweep-key",
        "--set", "api.registryPolling.cronJob.enabled=true",
        "--set", "api.registryPolling.cronJob.existingSecret=sweep-key",
    )
    components = _cronjob_policy_components(docs)
    assert "helper-retention" in components
    assert "registry-poll" in components


def test_cronjob_egress_policy_absent_when_no_sweep_enabled() -> None:
    docs = _render("--set", "security.networkPolicies.enabled=true")
    assert _find(docs, "NetworkPolicy", "-cronjobs") is None


def test_sweep_cronjob_requires_a_key_secret() -> None:
    result = subprocess.run(
        ["helm", "template", "sessionfs", CHART_DIR, "--set", "api.jiraSync.enabled=true"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "existingSecret is required" in result.stderr
