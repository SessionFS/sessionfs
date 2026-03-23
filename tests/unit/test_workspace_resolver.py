"""Tests for workspace resolver."""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from sessionfs.workspace.resolver import ResolvedWorkspace, WorkspaceResolver


class TestNormalizeRemote:
    """Test git remote URL normalization."""

    def setup_method(self):
        self.resolver = WorkspaceResolver()

    def test_ssh_with_git_suffix(self):
        assert self.resolver._normalize_remote("git@github.com:org/repo.git") == "org/repo"

    def test_ssh_without_git_suffix(self):
        assert self.resolver._normalize_remote("git@github.com:org/repo") == "org/repo"

    def test_https_with_git_suffix(self):
        assert self.resolver._normalize_remote("https://github.com/org/repo.git") == "org/repo"

    def test_https_without_git_suffix(self):
        assert self.resolver._normalize_remote("https://github.com/org/repo") == "org/repo"

    def test_http_url(self):
        assert self.resolver._normalize_remote("http://github.com/org/repo") == "org/repo"

    def test_nested_org(self):
        assert self.resolver._normalize_remote("git@gitlab.com:group/sub/repo.git") == "group/sub/repo"

    def test_empty_string(self):
        assert self.resolver._normalize_remote("") == ""

    def test_whitespace(self):
        assert self.resolver._normalize_remote("  git@github.com:org/repo.git  ") == "org/repo"

    def test_ssh_matches_https(self):
        """SSH and HTTPS URLs for the same repo should normalize identically."""
        ssh = self.resolver._normalize_remote("git@github.com:sessionfs/sessionfs.git")
        https = self.resolver._normalize_remote("https://github.com/sessionfs/sessionfs.git")
        assert ssh == https == "sessionfs/sessionfs"


class TestRemoteMatches:
    """Test matching git config remotes."""

    def setup_method(self):
        self.resolver = WorkspaceResolver()

    def test_matches_origin(self, tmp_path: Path):
        """Finds a match when origin remote matches."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        config = configparser.ConfigParser()
        config.add_section('remote "origin"')
        config.set('remote "origin"', "url", "git@github.com:myorg/myrepo.git")
        config.set('remote "origin"', "fetch", "+refs/heads/*:refs/remotes/origin/*")
        with open(git_dir / "config", "w") as f:
            config.write(f)

        assert self.resolver._remote_matches(tmp_path, "myorg/myrepo") is True

    def test_no_match(self, tmp_path: Path):
        """Returns False when no remote matches."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        config = configparser.ConfigParser()
        config.add_section('remote "origin"')
        config.set('remote "origin"', "url", "git@github.com:other/repo.git")
        with open(git_dir / "config", "w") as f:
            config.write(f)

        assert self.resolver._remote_matches(tmp_path, "myorg/myrepo") is False

    def test_missing_git_config(self, tmp_path: Path):
        """Returns False when .git/config doesn't exist."""
        assert self.resolver._remote_matches(tmp_path, "myorg/myrepo") is False

    def test_multiple_remotes(self, tmp_path: Path):
        """Matches against any remote, not just origin."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        config = configparser.ConfigParser()
        config.add_section('remote "origin"')
        config.set('remote "origin"', "url", "git@github.com:fork/repo.git")
        config.add_section('remote "upstream"')
        config.set('remote "upstream"', "url", "https://github.com/original/repo.git")
        with open(git_dir / "config", "w") as f:
            config.write(f)

        assert self.resolver._remote_matches(tmp_path, "original/repo") is True


class TestResolve:
    """Test the full resolve flow."""

    def setup_method(self):
        self.resolver = WorkspaceResolver()

    def test_resolve_no_match(self):
        """Returns path=None when repo is not found locally."""
        result = self.resolver.resolve("git@github.com:nonexistent/repo-that-wont-exist.git")
        assert result.path is None
        assert result.branch_exists is False

    def test_resolve_empty_remote(self):
        """Handles empty remote gracefully."""
        result = self.resolver.resolve("")
        assert result.path is None

    def test_resolve_finds_repo_in_cwd(self, tmp_path: Path, monkeypatch):
        """Finds a repo when cwd contains a matching clone."""
        # Create a fake git repo in a subdirectory of cwd
        repo_dir = tmp_path / "myproject"
        repo_dir.mkdir()
        git_dir = repo_dir / ".git"
        git_dir.mkdir()

        config = configparser.ConfigParser()
        config.add_section('remote "origin"')
        config.set('remote "origin"', "url", "git@github.com:testorg/testrepo.git")
        with open(git_dir / "config", "w") as f:
            config.write(f)

        monkeypatch.chdir(tmp_path)

        result = self.resolver.resolve("https://github.com/testorg/testrepo.git")
        assert result.path == repo_dir

    def test_resolved_workspace_dataclass(self):
        """ResolvedWorkspace has expected defaults."""
        rw = ResolvedWorkspace(path=None, branch_exists=False, branch=None)
        assert rw.commits_behind == 0
        assert rw.path is None
        assert rw.branch is None
