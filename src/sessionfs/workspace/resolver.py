"""Resolve the recipient's local clone of a repo when pulling a handoff."""

from __future__ import annotations

import configparser
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Directories to search for git repos (relative to home)
_SEARCH_DIRS = [
    "Documents",
    "projects",
    "repos",
    "code",
    "src",
    "dev",
    "Developer",
]

_MAX_DEPTH = 3


@dataclass
class ResolvedWorkspace:
    """Result of resolving a workspace from a git remote."""

    path: Path | None
    branch_exists: bool
    branch: str | None
    commits_behind: int = 0


class WorkspaceResolver:
    """Find the recipient's local clone of a repository."""

    def resolve(
        self, git_remote: str, git_branch: str | None = None,
    ) -> ResolvedWorkspace:
        """Resolve a local workspace matching the given git remote.

        Strategy 1: Search common directories for .git dirs whose remote matches.
        Strategy 2: Return path=None if nothing found.
        """
        target = self._normalize_remote(git_remote)
        if not target:
            return ResolvedWorkspace(path=None, branch_exists=False, branch=git_branch)

        # Build search roots
        home = Path.home()
        search_roots: list[Path] = []
        for name in _SEARCH_DIRS:
            d = home / name
            if d.is_dir():
                search_roots.append(d)

        # Also search cwd
        cwd = Path.cwd()
        if cwd not in search_roots:
            search_roots.append(cwd)

        # Walk each root up to _MAX_DEPTH looking for .git dirs
        for root in search_roots:
            match = self._search_dir(root, target, depth=0)
            if match is not None:
                branch_ok = False
                behind = 0
                if git_branch:
                    branch_ok = self._branch_exists(match, git_branch)
                    if branch_ok:
                        behind = self._commits_behind(match, git_branch)
                return ResolvedWorkspace(
                    path=match,
                    branch_exists=branch_ok,
                    branch=git_branch,
                    commits_behind=behind,
                )

        return ResolvedWorkspace(path=None, branch_exists=False, branch=git_branch)

    def _search_dir(self, directory: Path, target: str, depth: int) -> Path | None:
        """Recursively search for a git repo matching the target remote."""
        if depth > _MAX_DEPTH:
            return None

        if (directory / ".git").exists():
            if self._remote_matches(directory, target):
                return directory
            return None  # Don't recurse into git repos

        try:
            entries = sorted(directory.iterdir())
        except PermissionError:
            return None

        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            result = self._search_dir(entry, target, depth + 1)
            if result is not None:
                return result

        return None

    def _normalize_remote(self, url: str) -> str:
        """Normalize a git remote URL to org/repo form.

        git@github.com:org/repo.git -> org/repo
        https://github.com/org/repo.git -> org/repo
        https://github.com/org/repo -> org/repo
        """
        url = url.strip()
        if not url:
            return ""

        # SSH: git@host:org/repo.git
        ssh_match = re.match(r"^[\w.-]+@[\w.-]+:(.*?)(?:\.git)?$", url)
        if ssh_match:
            return ssh_match.group(1)

        # HTTPS: https://host/org/repo.git or https://host/org/repo
        https_match = re.match(r"^https?://[^/]+/(.*?)(?:\.git)?$", url)
        if https_match:
            return https_match.group(1)

        return url

    def _remote_matches(self, repo_dir: Path, target_remote: str) -> bool:
        """Check if any remote in a git repo matches the target."""
        git_config = repo_dir / ".git" / "config"
        if not git_config.exists():
            return False

        try:
            parser = configparser.ConfigParser()
            parser.read(str(git_config))
            for section in parser.sections():
                if section.startswith('remote "'):
                    url = parser.get(section, "url", fallback="")
                    if self._normalize_remote(url) == target_remote:
                        return True
        except (configparser.Error, OSError):
            return False

        return False

    def _branch_exists(self, repo_dir: Path, branch: str) -> bool:
        """Check if a branch exists in the local repo."""
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_dir), "branch", "--list", branch],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return bool(result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    def _commits_behind(
        self, repo_dir: Path, branch: str, target_commit: str | None = None,
    ) -> int:
        """Count how many commits the local branch is behind."""
        if target_commit:
            rev_range = f"HEAD..{target_commit}"
        else:
            rev_range = f"HEAD..{branch}"

        try:
            result = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-list", "--count", rev_range],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return int(result.stdout.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
            pass

        return 0
