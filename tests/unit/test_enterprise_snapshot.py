"""The Enterprise snapshot is the public tag plus ee/ and nothing else.

Builds real git trees in a throwaway repo. Skips in a public checkout, which
has no ``.release/`` directory.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".release" / "enterprise_snapshot.py"
if not SCRIPT.is_file():
    pytest.skip("no .release/ in this checkout", allow_module_level=True)

spec = importlib.util.spec_from_file_location("enterprise_snapshot", SCRIPT)
assert spec and spec.loader
snap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snap)

PRIVATE = ["ee/", "dashboard/src/ee/", ".agents/", "docs/design/", "CLAUDE.md", ".release/"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    _git(repo, "rm", "-rq", "--cached", "--ignore-unmatch", ".")
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        _git(repo, "add", "-f", rel)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg, "--allow-empty")
    return _git(repo, "rev-parse", "HEAD")


PUBLIC = {
    "pyproject.toml": 'version = "1.2.3"\n',
    "src/sessionfs/app.py": "x = 1\n",
    "dashboard/src/ee.tsx": "export {}\n",
    ".github/workflows/deploy-api.yml": "on: push\n",
    ".github/workflows/release.yml": "on: push\n",
}
DEVELOP = {
    **PUBLIC,
    "src/sessionfs/app.py": "x = 2  # newer than the tag\n",
    "ee/sessionfs_ee/__init__.py": "# SESSIONFS-ENTERPRISE-ONLY\n",
    "ee/ci/enterprise-images.yml": "on: push\n",
    "dashboard/src/ee/routes.tsx": "// SESSIONFS-ENTERPRISE-ONLY\n",
    ".agents/atlas.md": "internal\n",
    "docs/design/editions.md": "internal\n",
    "CLAUDE.md": "internal\n",
    ".release/private-files.txt": "ee/\n",
}


@pytest.fixture()
def repo(tmp_path: Path):
    _git(tmp_path, "init", "-q")
    public_rev = _commit(tmp_path, PUBLIC, "public")
    develop_rev = _commit(tmp_path, DEVELOP, "develop")
    return tmp_path, public_rev, develop_rev


def _snapshot(repo):
    path, public_rev, develop_rev = repo
    public = snap.ls_tree(public_rev, repo=path)
    ee = snap.ls_tree(develop_rev, repo=path)
    return public, ee, snap.build_entries(public, ee)


def test_snapshot_is_public_tree_plus_ee(repo):
    public, ee, snapshot = _snapshot(repo)
    assert snap.check_entries(snapshot, public, ee, PRIVATE) == []
    assert set(snapshot) == {
        "pyproject.toml", "src/sessionfs/app.py", "dashboard/src/ee.tsx",
        "ee/sessionfs_ee/__init__.py", "ee/ci/enterprise-images.yml",
        "dashboard/src/ee/routes.tsx", snap.WORKFLOW_DST,
    }
    # Non-ee files come from the public tag, never from develop.
    assert snapshot["src/sessionfs/app.py"] == public["src/sessionfs/app.py"]


def test_internal_files_and_public_workflows_never_ship(repo):
    _, _, snapshot = _snapshot(repo)
    for leaked in (".agents/atlas.md", "docs/design/editions.md", "CLAUDE.md",
                   ".release/private-files.txt", ".github/workflows/deploy-api.yml"):
        assert leaked not in snapshot
    workflows = [p for p in snapshot if p.startswith(".github/workflows/")]
    assert workflows == [snap.WORKFLOW_DST]


def test_check_refuses_private_path_outside_ee(repo):
    public, ee, snapshot = _snapshot(repo)
    snapshot["CLAUDE.md"] = ee["CLAUDE.md"]
    problems = snap.check_entries(snapshot, public, ee, PRIVATE)
    assert any("private path outside ee/: CLAUDE.md" in p for p in problems)


def test_check_refuses_drift_from_public_tag(repo):
    public, ee, snapshot = _snapshot(repo)
    snapshot["src/sessionfs/app.py"] = ee["src/sessionfs/app.py"]
    assert any("differs from the public tag" in p
               for p in snap.check_entries(snapshot, public, ee, PRIVATE))


def test_check_refuses_public_tag_that_already_has_ee(repo):
    public, ee, snapshot = _snapshot(repo)
    public = {**public, "ee/x.py": ee["ee/sessionfs_ee/__init__.py"]}
    assert any("public tag already contains" in p
               for p in snap.check_entries(snapshot, public, ee, PRIVATE))


def test_check_refuses_leftover_public_workflow(repo):
    public, ee, snapshot = _snapshot(repo)
    snapshot[".github/workflows/release.yml"] = public[".github/workflows/release.yml"]
    assert any("public workflow left" in p
               for p in snap.check_entries(snapshot, public, ee, PRIVATE))


def test_written_tree_round_trips(repo):
    path, _, _ = repo
    public, ee, snapshot = _snapshot(repo)
    tree = snap.write_tree(snapshot, repo=path)
    commit = snap.commit_tree(tree, None, "Enterprise 1.2.3", repo=path)
    assert snap.ls_tree(commit, repo=path) == snapshot
    assert _git(path, "log", "-1", "--format=%an <%ae>", commit) == "sessionfsbot <bot@sessionfs.dev>"
    # Plumbing only: the working tree and HEAD are untouched.
    assert _git(path, "rev-parse", "HEAD") == repo[2]


def test_remote_must_be_the_private_repo(repo):
    path, _, _ = repo
    _git(path, "remote", "add", "enterprise", "https://github.com/SessionFS/sessionfs.git")
    problems = snap.remote_problems(repo=path, gh_private=lambda: True)
    assert any("expected the private repo" in p for p in problems)


def test_remote_refused_unless_github_says_private(repo):
    path, _, _ = repo
    _git(path, "remote", "add", "enterprise", "https://github.com/SessionFS/sessionfs-enterprise.git")
    assert snap.remote_problems(repo=path, gh_private=lambda: True) == []
    assert snap.remote_problems(repo=path, gh_private=lambda: None)
    assert snap.remote_problems(repo=path, gh_private=lambda: False)


def test_remote_refused_with_a_second_push_url(repo):
    path, _, _ = repo
    good = "https://github.com/SessionFS/sessionfs-enterprise.git"
    _git(path, "remote", "add", "enterprise", good)
    _git(path, "remote", "set-url", "--add", "--push", "enterprise", good)
    _git(path, "remote", "set-url", "--add", "--push", "enterprise",
         "https://github.com/SessionFS/sessionfs.git")
    problems = snap.remote_problems(repo=path, gh_private=lambda: True)
    assert any("2 push URLs" in p for p in problems)


def test_remote_refused_with_a_second_fetch_url(repo):
    path, _, _ = repo
    good = "https://github.com/SessionFS/sessionfs-enterprise.git"
    _git(path, "remote", "add", "enterprise", good)
    _git(path, "config", "--add", "remote.enterprise.url", "https://github.com/SessionFS/sessionfs.git")
    assert snap.remote_problems(repo=path, gh_private=lambda: True)


def test_remote_refused_when_insteadof_rewrites_to_public(repo):
    path, _, _ = repo
    _git(path, "remote", "add", "enterprise", "https://github.com/SessionFS/sessionfs-enterprise.git")
    _git(path, "config", "url.https://github.com/SessionFS/sessionfs.git.pushInsteadOf",
         "https://github.com/SessionFS/sessionfs-enterprise.git")
    problems = snap.remote_problems(repo=path, gh_private=lambda: True)
    assert any("push URL is" in p for p in problems)
