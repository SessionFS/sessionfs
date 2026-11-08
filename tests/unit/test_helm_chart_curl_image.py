"""Every curl container the chart renders is pinned by sha256 digest.

The sweep, helper-retention and registry-poll CronJobs hold a service key in
their environment, and the license-check init container holds the license
key. A tag can be re-pushed; a digest can't. Skipped when ``helm`` is not on
PATH.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART_DIR = Path(__file__).resolve().parents[2] / "charts" / "sessionfs"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")

DIGEST_REF = re.compile(r"^[a-z0-9./-]+@sha256:[0-9a-f]{64}$")

_SWEEP_KEYS = [
    "ticketNotifications",
    "orgDigest",
    "kbCompile",
    "jiraSync",
    "confluencePublish",
    "meetingRetention",
    "remindersDue",
    "helperRetention",
]


def _render(*extra: str) -> list[dict]:
    result = subprocess.run(
        ["helm", "template", "sessionfs", str(CHART_DIR), *extra],
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _all_enabled() -> list[str]:
    out: list[str] = ["--set", "license.key=lic_test"]
    for key in _SWEEP_KEYS:
        out += [
            "--set", f"api.{key}.enabled=true",
            "--set", f"api.{key}.cronJob.existingSecret=sweep-key",
        ]
    out += [
        "--set", "api.registryPolling.cronJob.enabled=true",
        "--set", "api.registryPolling.cronJob.existingSecret=poll-key",
    ]
    return out


def _containers(doc: dict) -> list[dict]:
    spec = doc.get("spec", {})
    if doc.get("kind") == "CronJob":
        spec = spec["jobTemplate"]["spec"]["template"]["spec"]
    elif doc.get("kind") in ("Deployment", "Job"):
        spec = spec["template"]["spec"]
    return list(spec.get("containers", [])) + list(spec.get("initContainers", []))


def _curl_images(docs: list[dict]) -> list[str]:
    return [
        c["image"]
        for doc in docs
        for c in _containers(doc)
        if "curl" in c.get("image", "")
    ]


def test_every_default_curl_image_is_digest_pinned() -> None:
    docs = _render(*_all_enabled())
    images = _curl_images(docs)
    # 7 shared sweeps + helper retention + registry poll + license check
    # (+ the helm test pod).
    assert len(images) >= 10, images
    for image in images:
        assert DIGEST_REF.match(image), image
    assert len(set(images)) == 1, set(images)


def test_values_defaults_match_the_helper_and_keep_the_tag_comment() -> None:
    values = (CHART_DIR / "values.yaml").read_text()
    helper = (CHART_DIR / "templates" / "_helpers.tpl").read_text()
    pinned = set(re.findall(r"image: (curlimages/curl@sha256:[0-9a-f]{64})  # curlimages/curl:\S+", values))
    assert len(pinned) == 1, pinned
    assert pinned.pop() in helper
    # No tag-only curl reference is left anywhere in the chart.
    for path in CHART_DIR.rglob("*.yaml"):
        for line in path.read_text().splitlines():
            code = line.split("#", 1)[0]
            assert not re.search(r"curlimages/curl:[\w.-]+", code), (path, line)


def test_cronjob_image_override_for_air_gapped_installs() -> None:
    mirror = "registry.example.com/mirror/curl@sha256:" + "a" * 64
    docs = _render(
        "--set", "api.kbCompile.enabled=true",
        "--set", "api.kbCompile.cronJob.existingSecret=sweep-key",
        "--set", f"api.kbCompile.cronJob.image={mirror}",
    )
    cron = next(
        d for d in docs
        if d.get("kind") == "CronJob" and d["metadata"]["name"].endswith("kb-compile")
    )
    assert _containers(cron)[0]["image"] == mirror
