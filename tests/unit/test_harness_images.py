"""Static checks for the works-in harness images (deploy/harness/).

Every tool in TOOL_PLANS either has an image definition with an exact,
lockfile-backed version, or is explicitly excluded with a reason; base
images are pinned by digest; nothing floats on ``latest``; the CI matrix and
the image contract agree with the harness code.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from sessionfs.skills.works_in_harness import DEFAULT_TOOL_USER, TOOL_PLANS

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "deploy" / "harness"
MANIFEST = json.loads((HARNESS / "tools.json").read_text())
DOCKERFILE = (HARNESS / "Dockerfile").read_text()
WORKFLOW = ROOT / ".github" / "workflows" / "harness-images.yml"
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "harness-image-build.yml"
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
INCLUDED = sorted(MANIFEST["tools"])


def test_every_harness_tool_has_an_image_or_a_documented_exclusion() -> None:
    included = set(MANIFEST["tools"])
    excluded = set(MANIFEST["excluded"])
    assert not included & excluded
    assert included | excluded == set(TOOL_PLANS)
    for tool in excluded:
        assert len(MANIFEST["excluded"][tool]) > 40, f"{tool}: exclusion needs a real reason"
    for tool in included:
        plan = TOOL_PLANS[tool]
        # An image for a tool the harness always skips would prove nothing.
        assert plan.steps and plan.skipped_reason is None, tool
    for tool, plan in TOOL_PLANS.items():
        if plan.skipped_reason:
            assert tool in excluded, f"{tool} is skipped by the harness: list it as excluded"


@pytest.mark.parametrize("tool", INCLUDED)
def test_tool_entry_is_pinned_and_sourced(tool: str) -> None:
    entry = MANIFEST["tools"][tool]
    assert entry["binary"] == TOOL_PLANS[tool].binary
    assert EXACT_VERSION.match(entry["version"]), f"{tool}: {entry['version']!r} is not an exact version"
    assert entry["image"] == f"sfs-harness-{tool}"
    assert entry["sources"] and all(url.startswith("https://") for url in entry["sources"])
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", entry["checked"])
    if entry["npm_scripts"]:
        assert entry.get("scripts_reason"), f"{tool}: say why install scripts must run"
    for rel in entry.get("prune", []):
        assert rel.startswith("node_modules/") and ".." not in rel.split("/")


@pytest.mark.parametrize("tool", INCLUDED)
def test_package_and_lockfile_match_the_pin(tool: str) -> None:
    entry = MANIFEST["tools"][tool]
    directory = HARNESS / "tools" / tool
    package = json.loads((directory / "package.json").read_text())
    assert package["dependencies"] == {entry["npm_package"]: entry["version"]}
    assert "devDependencies" not in package and "scripts" not in package
    lock = json.loads((directory / "package-lock.json").read_text())
    assert lock["lockfileVersion"] >= 2
    assert lock["packages"][""]["dependencies"] == {entry["npm_package"]: entry["version"]}
    assert lock["packages"][f"node_modules/{entry['npm_package']}"]["version"] == entry["version"]
    for key, meta in lock["packages"].items():
        if not key:
            continue
        # npm ci verifies every tarball against these: nothing unpinned.
        assert meta.get("resolved", "").startswith("https://registry.npmjs.org/"), key
        assert meta.get("integrity", "").startswith("sha512-"), key
        assert "latest" not in meta.get("version", ""), key


@pytest.mark.parametrize("tool", INCLUDED)
def test_system_config_files_are_valid_json(tool: str) -> None:
    for path in (HARNESS / "tools" / tool).glob("etc/**/*"):
        if path.is_file():
            json.loads(path.read_text())


def test_base_images_are_pinned_by_digest() -> None:
    froms = re.findall(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", DOCKERFILE, flags=re.MULTILINE)
    assert {stage for _, stage in froms} == {"tool", "sfs", "runtime"}
    for image, _ in froms:
        assert re.match(r"^[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}$", image), image
        assert ":latest" not in image
    stages = dict((stage, image) for image, stage in froms)
    # site-packages is copied between them: the python stages must match.
    assert stages["sfs"] == stages["runtime"]
    python_minor = re.search(r"python:(\d+\.\d+)\.", stages["runtime"]).group(1)
    assert f"/usr/local/lib/python{python_minor}/site-packages" in DOCKERFILE


def test_image_contract_matches_the_harness() -> None:
    # The harness must run as root so tool commands can drop to the tool user.
    assert not re.search(r"^USER\s", DOCKERFILE, flags=re.MULTILINE)
    assert "useradd" in DOCKERFILE and f" {DEFAULT_TOOL_USER}\n" in DOCKERFILE
    assert f"SFS_HARNESS_TOOL_USER={DEFAULT_TOOL_USER}" in DOCKERFILE
    # Opt-in and credentials come from the job, never the image.
    for name in ("SFS_WORKS_IN_HARNESS=", "SFS_HARNESS_API_KEY=", "SFS_HARNESS_API_URL=", "SESSIONFS_API_KEY="):
        assert name not in DOCKERFILE
    assert "latest" not in DOCKERFILE.replace("--no-install-recommends", "")
    entrypoint = (HARNESS / "entrypoint.sh").read_text()
    assert "sfs catalog harness run --tool" in entrypoint
    # Only the files the image needs leave the repository.
    ignore = [line for line in (HARNESS / "Dockerfile.dockerignore").read_text().splitlines()
              if line and not line.startswith("#")]
    assert ignore[0] == "*" and "!src/sessionfs/" in ignore


def test_workflow_builds_every_included_tool_privately() -> None:
    text = WORKFLOW.read_text()
    build_text = BUILD_WORKFLOW.read_text()
    workflow = yaml.safe_load(text)
    build = yaml.safe_load(build_text)
    jobs = workflow["jobs"]
    assert set(jobs) == {"plan", "verify", "publish"}
    # The matrix comes from tools.json, so a new tool is built automatically.
    assert 'json.load(open("deploy/harness/tools.json"))["tools"]' in text
    assert "deploy/harness/smoke.sh" in build_text
    for raw in (text, build_text):
        source = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))
        # Proprietary tool CLIs: private Artifact Registry only. Never GHCR,
        # never an Actions artifact, never a layer cache a fork could restore.
        assert "ghcr.io" not in source
        assert "upload-artifact" not in source and "download-artifact" not in source
        assert "docker save" not in source
        assert "cache-from" not in source and "cache-to" not in source
        assert ":latest" not in source
        assert "github.sha" not in source.split("on:", 1)[1].replace("|| github.sha }}", "")
    assert "us-central1-docker.pkg.dev/sessionfs-prod/sessionfs-harness" in build_text
    # Only the publish job can mint a GCP credential; the reusable build
    # declares nothing and inherits exactly what its caller grants.
    assert workflow["permissions"] == {"contents": "read"}
    assert jobs["publish"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert jobs["verify"]["permissions"] == {"contents": "read"}
    assert "permissions" not in build and "permissions" not in build["jobs"]["image"]
    assert jobs["verify"]["with"]["publish"] is False
    assert jobs["publish"]["with"]["publish"] is True
    # Tags and labels use the checked-out commit, which must be the SHA the
    # plan resolved; one tag per image (no bare SHA tag), never overwritten.
    assert "git rev-parse HEAD" in build_text
    assert "org.opencontainers.image.revision=${{ steps.meta.outputs.revision }}" in build_text
    assert '"${IMAGE}:${REV}"' not in build_text
    assert "refusing to overwrite" in build_text
    for job in ("verify", "publish"):
        assert jobs[job]["with"]["sha"] == "${{ needs.plan.outputs.sha }}"
    # Releases build the commit the Release run used, not the tag name.
    plan_checkout = jobs["plan"]["steps"][0]
    assert plan_checkout["with"]["ref"].startswith("${{ github.event.workflow_run.head_sha ||")
    # Publishing is limited to v* tags and commits on main.
    plan_script = jobs["plan"]["steps"][1]["run"]
    assert 'git merge-base --is-ancestor "$SHA" origin/main' in plan_script
    # Build records are Actions artifacts by default: off.
    steps = build["jobs"]["image"]["steps"]
    build_step = next(step for step in steps if step.get("name") == "Build image")
    assert build_step["env"]["DOCKER_BUILD_RECORD_UPLOAD"] == "false"
    # Credentials come LAST: once the first auth step appears, every later
    # step is publish-only auth / gcloud / push, and nothing repo-defined
    # (smoke, build) runs after it.
    first_auth = next(i for i, step in enumerate(steps) if "google-github-actions/auth" in str(step.get("uses")))
    tail = steps[first_auth:]
    assert all(step.get("if") == "inputs.publish" for step in tail)
    assert [step.get("name") or step["uses"].split("@")[0] for step in tail] == [
        "google-github-actions/auth", "google-github-actions/setup-gcloud", "Configure Docker", "Push image",
    ]
    gate = next(step for step in steps if step.get("name") == "Vulnerability gate (fixable CRITICAL/HIGH)")
    assert gate.get("if") == "inputs.publish"


def _all_uses(node: object) -> list[str]:
    if isinstance(node, dict):
        found = [node["uses"]] if isinstance(node.get("uses"), str) else []
        return found + [use for value in node.values() for use in _all_uses(value)]
    if isinstance(node, list):
        return [use for value in node for use in _all_uses(value)]
    return []


def test_every_action_in_the_harness_workflows_is_sha_pinned() -> None:
    for path in (WORKFLOW, BUILD_WORKFLOW):
        for use in _all_uses(yaml.safe_load(path.read_text())):
            if use.startswith("./"):
                continue
            assert re.match(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$", use), f"{path.name}: {use} is not SHA-pinned"


def _plan_selector() -> str:
    match = re.search(r"SELECTOR=\"\$\(cat <<'EOF'\n(.*?)\n\s*EOF\n", WORKFLOW.read_text(), flags=re.DOTALL)
    assert match
    return textwrap.dedent(match.group(1))


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["deploy/harness/tools/gemini/package.json"], ["gemini"]),
        (["deploy/harness/tools/gemini/package-lock.json", "deploy/harness/tools/copilot/package.json"],
         ["copilot", "gemini"]),
        (["deploy/harness/tools/gemini/etc/gemini-cli/settings.json"], ["gemini"]),
        # Anything outside one included tool's directory rebuilds every tool.
        (["deploy/harness/tools/gemini/package.json", "deploy/harness/smoke.sh"], INCLUDED),
        (["deploy/harness/tools.json"], INCLUDED),
        (["deploy/harness/Dockerfile"], INCLUDED),
        (["deploy/harness/smoke.sh"], INCLUDED),
        (["src/sessionfs/skills/works_in_harness.py"], INCLUDED),
        (["deploy/harness/tools/unknown-tool/package.json"], INCLUDED),
        (["deploy/harness/tools/gemini"], INCLUDED),
        (["deploy/harness/tools/gemini/name with\nnewline"], ["gemini"]),
        ([], INCLUDED),
    ],
)
def test_pr_matrix_follows_changed_tools(changed: list[str], expected: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _plan_selector()],
        cwd=ROOT, input="\0".join(changed).encode() + (b"\0" if changed else b""),
        capture_output=True, check=True,
    )
    assert json.loads(result.stdout) == expected
