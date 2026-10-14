"""Chart rendering tests for the ArgoCD migration-hook fix (GitHub #66).

Under ArgoCD the migration Job must finish before the api/mcp Deployments roll
to new code, and on a fresh install it must not run before the database exists.
These tests render the chart with ``helm template`` and assert the sync-wave
ordering (database at the default wave 0 → migration Job wave 1 → api/mcp wave
2), that the database resources are NOT turned into hooks, and that plain Helm
keeps its pre-install/pre-upgrade hook. Skipped when ``helm`` is not on PATH (lightweight CI).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

# Resolve relative to this file so the test works regardless of pytest's cwd.
CHART_DIR = str(Path(__file__).resolve().parents[2] / "charts" / "sessionfs")

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm not installed — cannot render the chart",
)


def _render(*extra: str) -> list[dict]:
    cmd = ["helm", "template", "sessionfs", CHART_DIR, *extra]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _doc(docs: list[dict], kind: str, name_contains: str) -> dict:
    for doc in docs:
        if doc.get("kind") == kind and name_contains in doc["metadata"]["name"]:
            return doc
    raise AssertionError(f"no {kind} with name containing {name_contains!r}")


def _annotations(doc: dict) -> dict:
    return doc["metadata"].get("annotations", {})


def test_migration_job_runs_between_the_database_and_the_api():
    job = _doc(_render(), "Job", "migrate")
    ann = _annotations(job)

    # `helm template` renders as a fresh install: no Helm hook (a pre-install
    # hook would run before its ServiceAccount/Secret/DB exist).
    assert "helm.sh/hook" not in ann
    # ArgoCD: a Sync-phase hook in wave 1 (PreSync would run before the DB exists).
    assert ann["argocd.argoproj.io/hook"] == "Sync"
    assert ann["argocd.argoproj.io/sync-wave"] == "1"
    assert "BeforeHookCreation" in ann["argocd.argoproj.io/hook-delete-policy"]
    assert "HookSucceeded" in ann["argocd.argoproj.io/hook-delete-policy"]

    # The job retries while the bundled DB starts instead of failing once.
    args = job["spec"]["template"]["spec"]["containers"][0]["args"][0]
    assert "until alembic upgrade head" in args
    assert "migrations complete" in args


def test_api_and_mcp_roll_out_after_the_migration_wave():
    docs = _render()
    for name in ("-api", "-mcp"):
        ann = _annotations(_doc(docs, "Deployment", name))
        assert ann["argocd.argoproj.io/sync-wave"] == "3"


def test_database_resources_stay_normal_managed_resources():
    # Hook resources are not tracked, pruned or self-healed by ArgoCD and can be
    # deleted by a delete policy — the database must never be one.
    docs = _render()
    for kind, name in (
        ("StatefulSet", "-postgresql"),
        ("Service", "-postgresql"),
        ("Secret", "-postgresql"),
        ("ServiceAccount", "sessionfs"),
    ):
        ann = _annotations(_doc(docs, kind, name))
        assert "argocd.argoproj.io/hook" not in ann, (kind, name)
        assert "helm.sh/hook" not in ann, (kind, name)


def test_common_annotations_still_apply_to_deployments():
    docs = _render("--set", "commonAnnotations.team=platform")
    ann = _annotations(_doc(docs, "Deployment", "-api"))
    assert ann["team"] == "platform"
    assert ann["argocd.argoproj.io/sync-wave"] == "3"


def test_license_seed_runs_between_migrations_and_the_api():
    docs = _render("--set", "license.key=sfs_trial_test", "--set", "license.mode=local")
    seed = _doc(docs, "Job", "seed-license")
    ann = _annotations(seed)

    assert ann["argocd.argoproj.io/hook"] == "Sync"
    assert ann["argocd.argoproj.io/sync-wave"] == "2"
    assert "BeforeHookCreation" in ann["argocd.argoproj.io/hook-delete-policy"]
    assert "HookSucceeded" in ann["argocd.argoproj.io/hook-delete-policy"]


def test_local_license_mode_has_no_self_referential_init_check():
    """In local mode the API can't validate its license by calling itself
    before it has started; the init check is for cloud mode only."""
    local = _doc(
        _render("--set", "license.key=sfs_trial_test", "--set", "license.mode=local"),
        "Deployment",
        "-api",
    )
    assert not local["spec"]["template"]["spec"].get("initContainers")

    cloud = _doc(
        _render("--set", "license.key=sfs_helm_test", "--set", "license.mode=cloud"),
        "Deployment",
        "-api",
    )
    names = [c["name"] for c in cloud["spec"]["template"]["spec"]["initContainers"]]
    assert "license-check" in names


def test_api_readiness_probe_waits_for_the_schema():
    deployment = _doc(_render(), "Deployment", "-api")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    from sessionfs.server.db.migration_check import READINESS_DB_TIMEOUT_SECONDS

    # The handler must answer before Kubernetes abandons the probe.
    assert container["readinessProbe"]["timeoutSeconds"] >= READINESS_DB_TIMEOUT_SECONDS + 3
    # Liveness stays a pure process check so a schema gap never restarts pods.
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"


def test_upgrades_keep_the_pre_upgrade_hook():
    job = _doc(_render("--is-upgrade"), "Job", "migrate")
    ann = _annotations(job)
    assert ann["helm.sh/hook"] == "pre-upgrade"
    assert "before-hook-creation" in ann["helm.sh/hook-delete-policy"]
