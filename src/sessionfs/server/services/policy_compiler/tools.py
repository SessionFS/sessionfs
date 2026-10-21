"""Per-tool compilers (marketplace v2 §8.3). See base.py for the contract.

Sources (all checked 2026-10-20):

- Claude Code: https://code.claude.com/docs/en/managed-settings,
  https://code.claude.com/docs/en/settings-reference
- GitHub Copilot managed settings:
  https://docs.github.com/en/copilot/reference/enterprise-administrators/enterprise-managed-settings,
  https://docs.github.com/en/copilot/how-tos/administer-copilot/manage-for-enterprise/use-managed-settings/deploy-managed-settings
- VS Code policies: https://code.visualstudio.com/docs/enterprise/policies,
  https://code.visualstudio.com/docs/enterprise/manage-ai-settings
- Gemini CLI: https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md,
  https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/enterprise.md
- Codex: https://developers.openai.com/codex/enterprise/managed-configuration,
  https://developers.openai.com/codex/config-reference
- Cursor: https://cursor.com/docs/enterprise/model-and-integration-management,
  https://cursor.com/docs/plugins
"""

from __future__ import annotations

from typing import Any

from sessionfs.server.services.policy_compiler.base import (
    POLICY_FIELDS,
    Artifact,
    CatalogSource,
    CompiledPolicy,
    Warning,
    classify,
    dump_json,
    dump_toml,
    marketplaces_restricted,
    source_list,
)
from sessionfs.server.services.policy_compiler.managed_settings import compile_managed_settings
from sessionfs.skills.policy import SkillsPolicy

_CLAUDE_SOURCES = source_list(
    (
        "managed-settings.json paths, strictKnownMarketplaces, extraKnownMarketplaces, "
        "allowManagedHooksOnly, allowedMcpServers, deniedMcpServers",
        "https://code.claude.com/docs/en/managed-settings",
    ),
    ("settings keys reference", "https://code.claude.com/docs/en/settings-reference"),
)
_COPILOT_SOURCES = source_list(
    (
        "managed-settings.json keys",
        "https://docs.github.com/en/copilot/reference/enterprise-administrators/enterprise-managed-settings",
    ),
    (
        "managed-settings.json paths",
        "https://docs.github.com/en/copilot/how-tos/administer-copilot/manage-for-enterprise/"
        "use-managed-settings/deploy-managed-settings",
    ),
)
_VSCODE_SOURCES = source_list(
    ("policy names, /etc/vscode/policy.json", "https://code.visualstudio.com/docs/enterprise/policies"),
    ("chat plugin / hook policies", "https://code.visualstudio.com/docs/enterprise/manage-ai-settings"),
)
_GEMINI_SOURCES = source_list(
    (
        "system settings paths, security.blockGitExtensions",
        "https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md",
    ),
    (
        "enterprise system settings precedence",
        "https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/enterprise.md",
    ),
)
_CODEX_SOURCES = source_list(
    (
        "requirements.toml paths and keys",
        "https://developers.openai.com/codex/enterprise/managed-configuration",
    ),
    ("config reference", "https://developers.openai.com/codex/config-reference"),
)
_CURSOR_SOURCES = source_list(
    (
        "MCP allowlist (team dashboard)",
        "https://cursor.com/docs/enterprise/model-and-integration-management",
    ),
    ("team marketplaces", "https://cursor.com/docs/plugins"),
)


def compile_claude_code(policy: SkillsPolicy, catalogs: list[CatalogSource]) -> CompiledPolicy:
    return compile_managed_settings(
        policy,
        catalogs,
        tool="claude-code",
        paths={
            "macos": "/Library/Application Support/ClaudeCode/managed-settings.json",
            "linux": "/etc/claude-code/managed-settings.json",
            "windows": "C:\\Program Files\\ClaudeCode\\managed-settings.json",
        },
        skills_dir_source=True,
        hooks_key=True,
        sources=_CLAUDE_SOURCES,
    )


def compile_copilot(policy: SkillsPolicy, catalogs: list[CatalogSource]) -> CompiledPolicy:
    result = compile_managed_settings(
        policy,
        catalogs,
        tool="copilot",
        paths={
            "macos": "/Library/Application Support/GitHubCopilot/managed-settings.json",
            "linux": "/etc/github-copilot/managed-settings.json",
            "windows": "%ProgramFiles%\\GitHubCopilot\\managed-settings.json",
        },
        skills_dir_source=False,
        hooks_key=False,
        sources=_COPILOT_SOURCES,
    )
    result.notes.append(
        "GitHub can also serve this file from copilot/managed-settings.json in the "
        "enterprise's .github-private repository; enterprise-managed settings override "
        "VS Code device policies for the same control."
    )
    return result


def compile_vscode(policy: SkillsPolicy, catalogs: list[CatalogSource]) -> CompiledPolicy:
    result = CompiledPolicy(tool="vscode", enforcement="managed", sources=_VSCODE_SOURCES)
    values: dict[str, Any] = {}
    enforced: set[str] = set()
    if policy.mode == "disabled":
        # Turns agent plugins off; skills copied into a workspace are not
        # covered, so "mode" stays a SessionFS check.
        values["ChatPluginsEnabled"] = False
        enforced.add("block_unmanaged_marketplaces")
    if catalogs and policy.mode != "disabled":
        values["ChatExtraMarketplaces"] = {catalog.name: catalog.git_url for catalog in catalogs}
    if not policy.allow_hooks:
        values["ChatAllowManagedHooksOnly"] = True
        enforced.add("allow_hooks")
        result.warnings.append(
            Warning(
                code="vscode.managed_hooks_only",
                tool="vscode",
                message="ChatAllowManagedHooksOnly also stops users' own agent hooks (VS Code 1.132+).",
            )
        )
    classify(result, enforced)
    result.notes.append(
        "Not emitted (value shapes not confirmed in VS Code docs): ChatStrictMarketplaces, "
        "ChatMCP (chat.mcp.access), ChatAllowedMcpServers / ChatDeniedMcpServers, "
        "McpGalleryServiceUrl. Use the GitHub Copilot managed settings for MCP and "
        "marketplace limits."
    )
    result.artifacts.append(
        Artifact(
            kind="file",
            format="json",
            content=dump_json(values),
            paths={"linux": "/etc/vscode/policy.json"},
            description=(
                "VS Code device policy (Linux file; deploy the same policy names through ADMX "
                "on Windows or a configuration profile on macOS)"
            ),
        )
    )
    return result


def compile_gemini(policy: SkillsPolicy, catalogs: list[CatalogSource]) -> CompiledPolicy:
    result = CompiledPolicy(tool="gemini", enforcement="managed", sources=_GEMINI_SOURCES)
    settings: dict[str, Any] = {}
    enforced: set[str] = set()
    if marketplaces_restricted(policy):
        settings["security"] = {"blockGitExtensions": True}
        enforced.add("block_unmanaged_marketplaces")
    classify(result, enforced)
    result.notes.append(
        "Gemini CLI MCP allow/exclude lists are keyed by server name; the policy's MCP "
        "limits are checked by SessionFS. admin.* keys are set from the Google Management "
        "Console, not the system settings file, and are not emitted."
    )
    result.artifacts.append(
        Artifact(
            kind="file",
            format="json",
            content=dump_json(settings),
            paths={
                "macos": "/Library/Application Support/GeminiCli/settings.json",
                "linux": "/etc/gemini-cli/settings.json",
                "windows": "C:\\ProgramData\\gemini-cli\\settings.json",
            },
            description="System settings (override user and workspace settings)",
        )
    )
    return result


def compile_codex(policy: SkillsPolicy, catalogs: list[CatalogSource]) -> CompiledPolicy:
    result = CompiledPolicy(tool="codex", enforcement="managed", sources=_CODEX_SOURCES)
    top: dict[str, Any] = {}
    tables: list[tuple[str, dict[str, Any]]] = []
    enforced: set[str] = set()
    if not policy.allow_hooks:
        top["allow_managed_hooks_only"] = True
        enforced.add("allow_hooks")
        result.warnings.append(
            Warning(
                code="codex.managed_hooks_only",
                tool="codex",
                message="allow_managed_hooks_only also stops users' own Codex hooks.",
            )
        )
    if policy.mode == "disabled":
        tables.append(("features", {"plugins": False}))
        enforced.add("block_unmanaged_marketplaces")
    elif marketplaces_restricted(policy):
        tables.append(("marketplaces", {"restrict_to_allowed_sources": True}))
        for catalog in catalogs:
            tables.append(
                (
                    f"marketplaces.allowed_sources.{catalog.name}",
                    {"source": "git", "url": catalog.git_url},
                )
            )
        enforced.add("block_unmanaged_marketplaces")
        if not catalogs:
            result.warnings.append(
                Warning(
                    code="codex.marketplace_lockdown",
                    tool="codex",
                    message="Every plugin marketplace, including OpenAI-curated ones, is blocked.",
                )
            )
    if not policy.allow_local_mcp and not policy.allow_remote_mcp:
        tables.append(("mcp_servers", {}))
        enforced.update({"allow_local_mcp", "allow_remote_mcp", "allowed_remote_mcp_origins"})
        result.warnings.append(
            Warning(
                code="codex.mcp_lockdown",
                tool="codex",
                message="An empty mcp_servers requirement disables every MCP server, including SessionFS's.",
            )
        )
    classify(result, enforced)
    result.artifacts.append(
        Artifact(
            kind="file",
            format="toml",
            content=dump_toml(tables, top),
            paths={
                "macos": "/etc/codex/requirements.toml",
                "linux": "/etc/codex/requirements.toml",
                "windows": "%ProgramData%\\OpenAI\\Codex\\requirements.toml",
            },
            description=(
                "Admin-enforced requirements (macOS MDM: domain com.openai.codex, key "
                "requirements_toml_base64)"
            ),
        )
    )
    return result


def compile_cursor(policy: SkillsPolicy, catalogs: list[CatalogSource]) -> CompiledPolicy:
    """Cursor has no local managed file we rely on: a dashboard checklist."""
    result = CompiledPolicy(tool="cursor", enforcement="checklist", sources=_CURSOR_SOURCES)
    steps = [
        "# Cursor team settings checklist",
        "",
        "Cursor applies these from the team admin dashboard (Teams / Enterprise). SessionFS "
        "cannot set them, so it lists every field as checked by SessionFS only.",
        "",
    ]
    header = len(steps)
    if catalogs and policy.mode != "disabled":
        for catalog in catalogs:
            steps.append(
                f"- [ ] Dashboard > Plugins & MCPs: import the team marketplace `{catalog.git_url}`."
            )
    if policy.mode == "disabled":
        steps.append("- [ ] Dashboard > Plugins & MCPs: set plugin installation to Default Off.")
    elif marketplaces_restricted(policy):
        steps.append(
            "- [ ] Dashboard > Plugins & MCPs: keep only SessionFS catalogs as team marketplaces."
        )
    if not policy.allow_local_mcp or not policy.allow_remote_mcp or policy.allowed_remote_mcp_origins:
        allowed = ", ".join(f"`{origin}/*`" for origin in policy.allowed_remote_mcp_origins) or "any"
        steps.append(
            "- [ ] Dashboard > MCP Configuration (Enterprise): allowlist only approved servers "
            f"(local servers: {'allowed' if policy.allow_local_mcp else 'none'}; remote servers: "
            f"{('origins ' + allowed) if policy.allow_remote_mcp else 'none'})."
        )
    if len(steps) == header:
        steps.append("- [ ] Nothing to configure for this policy.")
    classify(result, set())
    result.artifacts.append(
        Artifact(
            kind="checklist",
            format="markdown",
            content="\n".join(steps) + "\n",
            description="Admin dashboard checklist",
        )
    )
    return result


def compile_report_only(tool: str) -> CompiledPolicy:
    result = CompiledPolicy(tool=tool, enforcement="report_only")
    classify(result, set())
    result.notes.append(
        f"{tool} has no managed settings SessionFS can rely on; SessionFS enforces the policy "
        "on its own installs and reports compliance during reconcile."
    )
    return result


def compile_unsupported(tool: str, enforcement: str = "unsupported") -> CompiledPolicy:
    result = CompiledPolicy(tool=tool, enforcement=enforcement)
    result.unsupported.extend(POLICY_FIELDS)
    result.notes.append(
        "Policy output is turned off for this tool."
        if enforcement == "off"
        else f"{tool} is not supported until its formats are verified."
    )
    return result
