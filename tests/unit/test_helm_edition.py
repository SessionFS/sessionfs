"""Chart rendering of edition → SFS_EDITION (tk_6a6f1c54d9224c0b).
Skipped when ``helm`` is not on PATH."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART_DIR = str(ROOT / "charts" / "sessionfs")

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _docs(*extra: str) -> list[dict]:
    result = subprocess.run(
        ["helm", "template", "sessionfs", CHART_DIR, *extra],
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _api_config(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-api"):
            return doc["data"]
    raise AssertionError("api ConfigMap not rendered")


def _mcp_env(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("-mcp"):
            container = doc["spec"]["template"]["spec"]["containers"][0]
            return {item["name"]: item["value"] for item in container["env"]}
    raise AssertionError("mcp Deployment not rendered")


def test_default_edition_is_community() -> None:
    docs = _docs()
    assert _api_config(docs)["SFS_EDITION"] == "community"
    assert _mcp_env(docs)["SFS_EDITION"] == "community"


def test_edition_enterprise_renders_to_api_and_mcp() -> None:
    docs = _docs("--set", "edition=enterprise")
    assert _api_config(docs)["SFS_EDITION"] == "enterprise"
    assert _mcp_env(docs)["SFS_EDITION"] == "enterprise"


def test_licensed_install_defaults_to_enterprise() -> None:
    """A licensed self-hosted install upgrading the chart must keep its plan,
    so a licence key with no explicit edition renders enterprise."""
    docs = _docs("--set", "license.key=sfs_helm_example")
    assert _api_config(docs)["SFS_EDITION"] == "enterprise"
    assert _mcp_env(docs)["SFS_EDITION"] == "enterprise"


def test_explicit_community_wins_over_a_key() -> None:
    docs = _docs("--set", "license.key=sfs_helm_example", "--set", "edition=community")
    assert _api_config(docs)["SFS_EDITION"] == "community"


def test_unknown_edition_fails_to_render() -> None:
    result = subprocess.run(
        ["helm", "template", "sessionfs", CHART_DIR, "--set", "edition=platinum"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "edition must be community or enterprise" in result.stderr
