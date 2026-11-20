"""Unit tests for release_gate.py history checks.

The history checks refuse a release when any commit since ``origin/main``
tracks a private path, or when a main push carries more than one (squash)
commit. They run in a throwaway git repo, so nothing here touches the real
checkout. The module skips when ``.release/release_gate.py`` is absent (e.g.
a Community-only export).
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / ".release" / "release_gate.py"


def _load_gate():
    if not GATE.is_file():
        pytest.skip("release_gate.py not present (stripped from public main)", allow_module_level=True)
    spec = importlib.util.spec_from_file_location("release_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, rel: str, content: str, message: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_outgoing_commits_lists_only_new_commits(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "README.md", "hi\n", "base")
    c1 = _commit(repo, "a.py", "1\n", "first")
    c2 = _commit(repo, "b.py", "2\n", "second")
    assert gate.outgoing_commits(base, c2, repo=repo) == [c1, c2]


def test_history_problems_flags_private_path_anywhere_in_history(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "README.md", "hi\n", "base")
    _commit(repo, "ee/secret.py", "x = 1\n", "add enterprise code")
    # Remove it in a later commit so the tip is clean but the history is not;
    # the gate must still refuse the push on the intermediate leak.
    _git(repo, "rm", "-q", "ee/secret.py")
    _git(repo, "commit", "-q", "-m", "remove enterprise code")
    head = _git(repo, "rev-parse", "HEAD")
    problems = gate.history_problems(head, base, repo=repo, private=["ee/"])
    assert len(problems) == 1
    assert "tracks private paths" in problems[0]
    assert "ee/secret.py" in problems[0]


def test_history_problems_clean_history_passes(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "README.md", "hi\n", "base")
    head = _commit(repo, "src/a.py", "1\n", "clean")
    assert gate.history_problems(head, base, repo=repo, private=["ee/"]) == []


def test_squash_commit_problem_allows_one_commit(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "README.md", "hi\n", "base")
    head = _commit(repo, "src/a.py", "1\n", "squash")
    assert gate.squash_commit_problem(base, head, "refs/heads/main", repo=repo) is None


def test_squash_commit_problem_refuses_multiple_commits(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "README.md", "hi\n", "base")
    _commit(repo, "src/a.py", "1\n", "first")
    head = _commit(repo, "src/b.py", "2\n", "second")
    problem = gate.squash_commit_problem(base, head, "refs/heads/main", repo=repo)
    assert problem is not None
    assert "2 outgoing commits" in problem


def test_squash_commit_problem_allows_tag_at_zero_or_one(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "README.md", "hi\n", "base")
    # A tag directly on main HEAD has zero outgoing commits.
    assert gate.squash_commit_problem(base, base, "refs/tags/v1.0.0", repo=repo) is None
    head1 = _commit(repo, "src/a.py", "1\n", "one ahead")
    assert gate.squash_commit_problem(base, head1, "refs/tags/v1.0.0", repo=repo) is None
    head2 = _commit(repo, "src/b.py", "2\n", "two ahead")
    assert gate.squash_commit_problem(base, head2, "refs/tags/v1.0.0", repo=repo) is not None


# --- remote-main resolution + ancestry (finding 1) ---------------------------


class _FakeResult:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


def test_resolve_remote_sha_asks_remote_not_local_tracking_ref():
    """The gate must resolve main via `git ls-remote`, so a stale local
    `origin/main` tracking ref cannot pass an old commit as the history base."""
    calls: list[list[str]] = []

    def fake_ls_remote(argv, **kwargs):
        calls.append(argv)
        return _FakeResult(0, f"{'1' * 40}\trefs/heads/main\n")

    assert gate.resolve_remote_sha(ls_remote=fake_ls_remote) == "1" * 40
    assert calls == [["git", "ls-remote", "origin", "refs/heads/main"]]


def test_resolve_remote_sha_empty_when_unreachable():
    assert gate.resolve_remote_sha(ls_remote=lambda argv, **kw: _FakeResult(1, "")) == ""


def test_resolve_remote_sha_empty_when_main_absent():
    stdout = f"{'a' * 40}\trefs/heads/develop\n"
    assert gate.resolve_remote_sha(ls_remote=lambda argv, **kw: _FakeResult(0, stdout)) == ""


def test_is_zero_sha():
    assert gate.is_zero_sha("0" * 40) is True
    assert gate.is_zero_sha("0" * 64) is True
    assert gate.is_zero_sha("0" * 39) is False
    assert gate.is_zero_sha("abc") is False


def test_cmd_check_refuses_when_remote_unresolvable(monkeypatch):
    """Fail closed: with no resolvable remote main, check must refuse the push."""
    monkeypatch.setattr(gate, "resolve_remote_sha", lambda: "")
    monkeypatch.setattr(gate, "evidence_problems", lambda sha: [])
    monkeypatch.setattr(gate, "tree_problems", lambda sha: [])
    args = argparse.Namespace(rev="HEAD", sha=None, remote_sha=None, ref="")
    assert gate.cmd_check(args) == 1


def test_remote_history_problems_refuses_non_ancestor(tmp_path):
    """A force-push or orphan commit (remote main not an ancestor) is refused."""
    repo = _init_repo(tmp_path)
    base = _commit(repo, "README.md", "hi\n", "base")
    _git(repo, "checkout", "-q", "--orphan", "other")
    _git(repo, "rm", "-rf", "-q", ".")
    orphan = _commit(repo, "orphan.py", "x\n", "orphan")
    problems = gate.remote_history_problems(orphan, base, "refs/heads/main", repo=repo)
    assert len(problems) == 1
    assert "does not descend from remote main" in problems[0]


def test_remote_history_problems_clean_single_squash_passes(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "README.md", "hi\n", "base")
    head = _commit(repo, "src/a.py", "1\n", "squash")
    assert gate.remote_history_problems(head, base, "refs/heads/main", repo=repo) == []


def test_remote_history_problems_tag_on_main_passes(tmp_path):
    repo = _init_repo(tmp_path)
    base = _commit(repo, "README.md", "hi\n", "base")
    head = _commit(repo, "src/a.py", "1\n", "tag target")
    # A tag on main HEAD has zero outgoing commits but still descends from main.
    assert gate.remote_history_problems(head, head, "refs/tags/v1.0.0", repo=repo) == []
    assert gate.remote_history_problems(head, base, "refs/tags/v1.0.0", repo=repo) == []


# --- evidence command matching (finding 6) ----------------------------------


def test_evidence_command_problem_refuses_stub_community_command():
    problem = gate.evidence_command_problem("tests-community", {"command": ["true"], "sha": ""})
    assert problem is not None
    assert ".release/community_tests.sh" in problem


def test_evidence_command_problem_accepts_canonical_community():
    assert (
        gate.evidence_command_problem(
            "tests-community",
            {"command": ["/bin/bash", ".release/community_tests.sh", "HEAD"], "sha": "a" * 40},
        )
        is None
    )


def test_evidence_command_problem_community_requires_head_or_sha():
    assert (
        gate.evidence_command_problem(
            "tests-community",
            {"command": ["bash", ".release/community_tests.sh"], "sha": ""},
        )
        is not None
    )


def test_evidence_command_problem_mypy_substitution_refused():
    assert gate.evidence_command_problem("mypy-py312", {"command": ["mypy", "src/"], "sha": ""}) is None
    assert gate.evidence_command_problem("mypy-py312", {"command": ["true"], "sha": ""}) is not None


def test_evidence_command_problem_pg_requires_url_and_pytest():
    assert (
        gate.evidence_command_problem(
            "tests-pg",
            {"command": ["bash", "-c", "SFS_TEST_PG_URL=x pytest"], "sha": ""},
        )
        is None
    )
    assert gate.evidence_command_problem("tests-pg", {"command": ["pytest"], "sha": ""}) is not None


def test_evidence_command_problem_py310_requires_that_interpreter():
    assert (
        gate.evidence_command_problem(
            "tests-py310",
            {"command": ["bash", "-c", "python3.10 -m pytest"], "sha": ""},
        )
        is None
    )
    assert gate.evidence_command_problem("tests-py310", {"command": ["python3.12", "-m", "pytest"], "sha": ""}) is not None


def test_evidence_command_problem_attested_steps_unchanged():
    assert gate.evidence_command_problem("shield-sr", {"command": ["true"], "sha": ""}) is None
    assert gate.evidence_command_problem("scribe-site", {"command": ["true"], "sha": ""}) is None


def test_community_evidence_refuses_partial_or_shell_string_runs():
    sha = "b" * 40
    for command in (
        ["bash", ".release/community_tests.sh", "HEAD", "tests/unit/test_features.py"],
        ["bash", "-c", "true # .release/community_tests.sh HEAD"],
        ["bash", ".release/community_tests.sh", "c" * 40],
    ):
        problem = gate.evidence_command_problem("tests-community", {"command": command, "sha": sha})
        assert problem is not None, command
    assert gate.evidence_command_problem(
        "tests-community", {"command": ["/bin/bash", ".release/community_tests.sh", sha], "sha": sha}
    ) is None


def test_first_push_inspects_the_whole_history(tmp_path):
    """The all-zero remote (first-ever push) must not skip the history check:
    every reachable commit is outgoing, so a private path anywhere is found."""
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / ".agents").mkdir()
    (repo / ".agents" / "x.md").write_text("secret")
    run("add", "-A")
    run("commit", "-qm", "private")
    run("rm", "-rq", ".agents")
    (repo / "README.md").write_text("hi")
    run("add", "-A")
    run("commit", "-qm", "clean tip")
    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert len(gate.outgoing_commits("0" * 40, tip, repo=repo)) == 2
    problems = gate.history_problems(tip, "0" * 40, repo=repo, private=[".agents/"])
    assert problems and ".agents/x.md" in problems[0]


def test_first_push_must_be_a_single_root_commit(tmp_path):
    repo = tmp_path / "r2"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("1")
    run("add", "-A")
    run("commit", "-qm", "one")
    one = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert gate.first_push_problem(one, repo=repo) is None
    (repo / "a.txt").write_text("2")
    run("commit", "-qam", "two")
    two = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert gate.first_push_problem(two, repo=repo) is not None


def test_community_evidence_refuses_a_lookalike_bash():
    sha = "d" * 40
    for exe in ("/tmp/bash", "./bash", "bash"):
        assert gate.evidence_command_problem(
            "tests-community", {"command": [exe, ".release/community_tests.sh", sha], "sha": sha}
        ) is not None
