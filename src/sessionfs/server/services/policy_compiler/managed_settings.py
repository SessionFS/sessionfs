"""Claude Code and GitHub Copilot ``managed-settings.json``.

Both tools read a root-owned ``managed-settings.json`` with the same keys
for marketplaces and MCP servers (confirmed 2026-10-20):

- ``strictKnownMarketplaces``: array of source objects; unset = any
  marketplace; ``[]`` = complete lockdown (including the vendor's own).
  Claude Code: any allowlist also stops ``skills-dir`` plugins unless a
  ``{"source": "skills-dir"}`` entry is present.
- ``extraKnownMarketplaces``: ``{name: {"source": {...}}}``.
- ``allowedMcpServers`` / ``deniedMcpServers``: entries with exactly one of
  ``serverName`` / ``serverCommand`` / ``serverUrl`` (wildcards allowed in
  ``serverUrl``; ``serverCommand`` matches the exact command array); deny
  wins; ``[]`` allowlist blocks every user-added server, and an allowlist
  of ``serverUrl`` entries only blocks every stdio server.
- Claude Code only: ``allowManagedHooksOnly`` (managed-only): only managed
  hooks and hooks of plugins force-enabled in managed ``enabledPlugins`` run.

Keys deliberately NOT emitted: ``blockedMarketplaces`` (we have no list of
marketplaces to block - restriction is expressed as an allowlist),
``enabledPlugins`` (needs the phase 3a catalog's plugin@marketplace names),
``permissions.deny`` for capability ceilings (no rule maps a package
capability without also blocking the user's own tool use).
"""

from __future__ import annotations

from typing import Any

from sessionfs.server.services.policy_compiler.base import (
    Artifact,
    CatalogSource,
    CompiledPolicy,
    Warning,
    classify,
    dump_json,
    marketplaces_restricted,
)
from sessionfs.skills.policy import SkillsPolicy


def _mcp_rules(policy: SkillsPolicy) -> tuple[dict[str, Any], set[str], list[str]]:
    """(settings, enforced fields, warning codes) for MCP ceilings."""
    settings: dict[str, Any] = {}
    enforced: set[str] = set()
    warnings: list[str] = []
    local, remote = policy.allow_local_mcp, policy.allow_remote_mcp
    if not local and not remote:
        settings["allowedMcpServers"] = []
        enforced.update({"allow_local_mcp", "allow_remote_mcp", "allowed_remote_mcp_origins"})
        warnings.append("mcp_lockdown")
    elif local and not remote:
        settings["deniedMcpServers"] = [{"serverUrl": "http://*"}, {"serverUrl": "https://*"}]
        enforced.update({"allow_remote_mcp", "allowed_remote_mcp_origins"})
    elif not local and remote and policy.allowed_remote_mcp_origins:
        # Only remote servers on the allowed origins; with only serverUrl
        # entries in the allowlist, a stdio server matches nothing - which
        # includes SessionFS's own MCP server (`sfs mcp serve`, stdio).
        settings["allowedMcpServers"] = [
            {"serverUrl": f"{origin}/*"} for origin in policy.allowed_remote_mcp_origins
        ]
        enforced.update({"allowed_remote_mcp_origins", "allow_remote_mcp", "allow_local_mcp"})
        # No SessionFS allow entry is added (checked 2026-10-20,
        # https://code.claude.com/docs/en/managed-mcp "Match servers by URL,
        # command, or name"): ``serverCommand`` matches the exact command
        # and arguments, and `sfs mcp install` registers each machine's
        # absolute path to `sfs`, so no single entry matches every device;
        # a ``serverName`` entry would let ANY stdio server the user labels
        # "sessionfs" through (the docs say it is not a security control),
        # which would defeat allow_local_mcp=false. The admin acknowledges
        # the side effect instead (and can add a per-fleet serverCommand
        # entry through MDM).
        warnings.append("sessionfs_mcp_blocked")
    # local allowed + remote restricted to origins, or local forbidden with
    # any remote origin: not expressible without naming every server, so
    # SessionFS reports on it during reconcile.
    return settings, enforced, warnings


def compile_managed_settings(
    policy: SkillsPolicy,
    catalogs: list[CatalogSource],
    *,
    tool: str,
    paths: dict[str, str],
    skills_dir_source: bool,
    hooks_key: bool,
    sources: list[dict[str, str]],
) -> CompiledPolicy:
    result = CompiledPolicy(tool=tool, enforcement="managed", sources=sources)
    settings: dict[str, Any] = {}
    enforced: set[str] = set()
    warning_codes: list[str] = []

    if policy.mode == "disabled":
        settings["strictKnownMarketplaces"] = []
        # Plugins from any marketplace are blocked; skills placed directly in
        # the skills directory are not, so "mode" stays a SessionFS check.
        enforced.add("block_unmanaged_marketplaces")
        warning_codes.append("marketplace_lockdown")
    elif marketplaces_restricted(policy):
        allowed: list[dict[str, Any]] = [
            {"source": "git", "url": catalog.git_url} for catalog in catalogs
        ]
        if skills_dir_source:
            # SessionFS delivers skills into the tool's skills directory.
            allowed.append({"source": "skills-dir"})
        settings["strictKnownMarketplaces"] = allowed
        enforced.add("block_unmanaged_marketplaces")
        if not catalogs:
            warning_codes.append("marketplace_lockdown")
    if catalogs and policy.mode != "disabled":
        settings["extraKnownMarketplaces"] = {
            catalog.name: {"source": {"source": "git", "url": catalog.git_url}}
            for catalog in catalogs
        }

    if hooks_key and not policy.allow_hooks:
        settings["allowManagedHooksOnly"] = True
        enforced.add("allow_hooks")
        warning_codes.append("managed_hooks_only")

    mcp, mcp_enforced, mcp_warnings = _mcp_rules(policy)
    settings.update(mcp)
    enforced |= mcp_enforced
    warning_codes += mcp_warnings

    messages = {
        "marketplace_lockdown": (
            "The tool's own marketplaces, including the vendor's official one, are blocked; "
            "only SessionFS-delivered skills remain."
        ),
        "managed_hooks_only": (
            "allowManagedHooksOnly also stops users' own hooks (and in Claude Code /goal and "
            "command-source plugins)."
        ),
        "mcp_lockdown": (
            "An empty MCP allowlist blocks every MCP server users add, including the "
            "SessionFS MCP server."
        ),
        "sessionfs_mcp_blocked": (
            "An MCP allowlist of remote origins only blocks every local (stdio) MCP server, "
            "including the SessionFS MCP server (sfs mcp serve)."
        ),
    }
    for code in warning_codes:
        result.warnings.append(Warning(code=f"{tool}.{code}", tool=tool, message=messages[code]))
    classify(result, enforced)
    if not catalogs:
        result.notes.append(
            "No SessionFS native catalog is published for this org yet; marketplace "
            "registration (extraKnownMarketplaces) is omitted."
        )
    result.artifacts.append(
        Artifact(
            kind="file",
            format="json",
            content=dump_json(settings),
            paths=paths,
            description="Managed settings (root-owned; overrides user and project settings)",
        )
    )
    return result
