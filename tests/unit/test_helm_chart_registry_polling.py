"""Chart rendering for server-side registry polling (GitHub #69): the poll
CronJob, credential Secret mounts, the private-range opt-in and the
NetworkPolicy rules. Skipped when ``helm`` is not on PATH."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = str(Path(__file__).resolve().parents[2] / "charts" / "sessionfs")

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


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


def test_defaults_poll_from_public_addresses_only_and_no_cronjob() -> None:
    docs = _render()
    assert _find(docs, "CronJob", "registry-poll") is None
    config = _find(docs, "ConfigMap", "-api")
    assert config is not None
    assert config["data"]["SFS_REGISTRY_POLLING_ENABLED"] == "true"
    assert "SFS_REGISTRY_POLL_ALLOWED_PRIVATE_CIDRS" not in config["data"]
    assert config["data"]["SFS_REGISTRY_POLL_MAX_PER_ORG"] == "10"
    assert config["data"]["SFS_REGISTRY_POLL_RUN_BUDGET_SECONDS"] == "1200"
    assert config["data"]["SFS_REGISTRY_POLL_TIMEOUT_SECONDS"] == "300"
    api = _find(docs, "Deployment", "-api")
    assert api is not None
    pod = api["spec"]["template"]["spec"]
    mounts = {m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert "/tmp" in mounts  # read-only root filesystem: clones go to an emptyDir


def test_cronjob_credentials_and_private_opt_in() -> None:
    docs = _render(
        "--set", "security.networkPolicies.enabled=true",
        "--set", "api.registryPolling.cronJob.enabled=true",
        "--set", "api.registryPolling.cronJob.existingSecret=poll-key",
        "--set", "api.registryPolling.allowedPrivateCidrs={10.20.0.0/16}",
        "--set", "api.registryPolling.credentialSecrets={acme-gitlab}",
    )
    cron = _find(docs, "CronJob", "registry-poll")
    assert cron is not None and cron["spec"]["concurrencyPolicy"] == "Forbid"
    container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    script = container["args"][0]
    assert "/api/v1/admin/skills/registries/poll" in script
    # Outer deadlines exceed the API's run budget (1200 s by default), which
    # stops claiming registries early enough to finish inside it.
    assert "--max-time 1320" in script
    assert cron["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 1380
    # The key is read from the environment, never templated into argv.
    assert container["env"][0]["valueFrom"]["secretKeyRef"]["name"] == "poll-key"
    assert "Bearer $SFS_REGISTRY_POLL_API_KEY" not in container.get("command", [])
    config = _find(docs, "ConfigMap", "-api")
    assert config is not None
    assert config["data"]["SFS_REGISTRY_POLL_ALLOWED_PRIVATE_CIDRS"] == "10.20.0.0/16"
    api = _find(docs, "Deployment", "-api")
    assert api is not None
    pod = api["spec"]["template"]["spec"]
    mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "registry-cred-acme-gitlab")
    assert mount["readOnly"] is True
    assert mount["mountPath"] == "/var/run/secrets/sessionfs/registry-credentials/acme-gitlab"
    policy = _find(docs, "NetworkPolicy", "-api")
    assert policy is not None
    cidrs = [
        block["ipBlock"]["cidr"]
        for rule in policy["spec"]["egress"]
        for block in rule.get("to", [])
        if "ipBlock" in block
    ]
    assert "10.20.0.0/16" in cidrs
    sources = [
        peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/component")
        for rule in policy["spec"]["ingress"]
        for peer in rule.get("from", [])
    ]
    assert "registry-poll" in sources


def test_cronjob_requires_a_key_secret() -> None:
    result = subprocess.run(
        ["helm", "template", "sessionfs", CHART_DIR, "--set", "api.registryPolling.cronJob.enabled=true"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "existingSecret is required" in result.stderr
