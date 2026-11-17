"""Chart rendering of api.trustedProxies → SFS_TRUSTED_PROXIES (tk_32e88a6ed5434fcf).
Skipped when ``helm`` is not on PATH."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from sessionfs.server.client_ip import parse_trusted_proxies

ROOT = Path(__file__).resolve().parents[2]
CHART_DIR = str(ROOT / "charts" / "sessionfs")

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _api_config(*extra: str) -> dict:
    result = subprocess.run(
        ["helm", "template", "sessionfs", CHART_DIR, *extra], capture_output=True, text=True, check=True
    )
    for doc in yaml.safe_load_all(result.stdout):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-api"):
            return doc["data"]
    raise AssertionError("api ConfigMap not rendered")


def test_default_trusts_private_ranges() -> None:
    value = _api_config()["SFS_TRUSTED_PROXIES"]
    assert value == "private"
    assert parse_trusted_proxies(value)


def test_list_is_joined() -> None:
    value = _api_config("--set-json", 'api.trustedProxies=["gcp-lb","203.0.113.10","10.244.0.0/16"]')[
        "SFS_TRUSTED_PROXIES"
    ]
    assert value == "gcp-lb,203.0.113.10,10.244.0.0/16"
    assert parse_trusted_proxies(value)


def test_string_and_empty() -> None:
    assert _api_config("--set", r"api.trustedProxies=cloud-run\,8.233.150.3")["SFS_TRUSTED_PROXIES"] == (
        "cloud-run,8.233.150.3"
    )
    assert _api_config("--set-json", "api.trustedProxies=[]")["SFS_TRUSTED_PROXIES"] == ""


def test_api_image_disables_uvicorn_proxy_headers() -> None:
    cmd = next(line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("CMD"))
    assert "--no-proxy-headers" in cmd
