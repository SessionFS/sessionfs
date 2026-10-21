"""Public GitHub mirror of the native public catalog (marketplace v2 D4b).

``SFS_PUBLIC_CATALOG_MIRROR`` names a public GitHub repository
(``owner/repo``, e.g. ``SessionFS/marketplace``) that a scheduled
``sfs catalog export`` keeps identical to the public catalog. When it is
set, the public catalog's add commands lead with the keyless GitHub form
each tool documents (vendor docs checked 2026-10-21, ``MIRROR_VENDOR_SOURCES``)
and the SessionFS-hosted URL stays as the alternative.

Gemini CLI per-plugin repositories are not mirrored (they stay on the
SessionFS host).
"""

from __future__ import annotations

import re
from pathlib import Path

# GitHub account names: alphanumerics and single hyphens, no leading or
# trailing hyphen, at most 39 characters. Repository names: letters, digits,
# ".", "_" and "-", at most 100 characters.
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

GITHUB_KNOWN_HOSTS = Path(__file__).with_name("data") / "github-ssh-known-hosts.txt"

MIRROR_DOCS_CHECKED = "2026-10-21"
# Pages that document adding a marketplace from a GitHub owner/repo or a
# GitHub repository URL, per tool (checked MIRROR_DOCS_CHECKED).
MIRROR_VENDOR_SOURCES: dict[str, str] = {
    # "/plugin marketplace add your-org/your-marketplace" (owner/repo means github.com)
    "claude-code": "https://code.claude.com/docs/en/plugins/host-marketplace",
    # "copilot plugin marketplace add octo-org/octo-repo"
    "copilot": "https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/plugins-marketplace",
    # "GitHub shorthand (owner/repo or owner/repo@ref)": codex plugin marketplace add owner/repo
    "codex": "https://developers.openai.com/codex/plugins/build.md",
    # Dashboard -> Plugins & MCPs -> Team Marketplaces -> Add Marketplace ->
    # Import from Repo (GitHub, GitLab, Bitbucket and Azure DevOps URLs)
    "cursor": "https://cursor.com/docs/plugins",
}


class MirrorConfigError(ValueError):
    """``SFS_PUBLIC_CATALOG_MIRROR`` is not a GitHub ``owner/repo``."""


def parse_mirror(value: str | None) -> str | None:
    """``owner/repo`` or None (unset). Raises ``MirrorConfigError`` for
    anything else: URLs, ``.git`` suffixes, extra path segments,
    credentials, whitespace inside."""
    text = (value or "").strip()
    if not text:
        return None
    owner, sep, repo = text.partition("/")
    if (
        not sep
        or not _OWNER.match(owner)
        or not _REPO.match(repo)
        or repo in {".", ".."}
        or repo.lower().endswith(".git")
    ):
        raise MirrorConfigError(
            "SFS_PUBLIC_CATALOG_MIRROR must be a GitHub repository as owner/repo "
            "(for example SessionFS/marketplace)"
        )
    return f"{owner}/{repo}"


def https_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def ssh_remote(repo: str) -> str:
    """The push remote the export job uses with a deploy key."""
    return f"git@github.com:{repo}.git"
