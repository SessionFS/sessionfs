"""Org skills policy compiler registry (marketplace v2 §8.3).

``compile(policy, catalogs, tool) -> CompiledPolicy`` is a pure,
deterministic registry mirroring ``rules_compiler``. See base.py for the
per-field enforceable / report_only / unsupported contract.
"""

from __future__ import annotations

from typing import Callable

from sessionfs.server.services.policy_compiler.base import (
    COMPILER_VERSION,
    POLICY_FIELDS,
    Artifact,
    CatalogSource,
    CompiledPolicy,
    Warning,
)
from sessionfs.server.services.policy_compiler.tools import (
    compile_claude_code,
    compile_codex,
    compile_copilot,
    compile_cursor,
    compile_gemini,
    compile_report_only,
    compile_unsupported,
    compile_vscode,
)
from sessionfs.skills.policy import POLICY_TOOLS, SkillsPolicy

COMPILERS: dict[str, Callable[[SkillsPolicy, list[CatalogSource]], CompiledPolicy]] = {
    "claude-code": compile_claude_code,
    "copilot": compile_copilot,
    "vscode": compile_vscode,
    "gemini": compile_gemini,
    "codex": compile_codex,
    "cursor": compile_cursor,
}
REPORT_ONLY_TOOLS = ("amp", "cline", "roo-code", "kilo-code")
UNSUPPORTED_TOOLS = ("antigravity",)
TOOLS: tuple[str, ...] = POLICY_TOOLS


def compile_policy(policy: SkillsPolicy, catalogs: list[CatalogSource], tool: str) -> CompiledPolicy:
    if tool not in TOOLS:
        raise KeyError(tool)
    enforcement = policy.tool_enforcement(tool)
    if enforcement == "off":
        return compile_unsupported(tool, "off")
    if tool in UNSUPPORTED_TOOLS:
        return compile_unsupported(tool)
    if tool in REPORT_ONLY_TOOLS or enforcement == "report_only":
        return compile_report_only(tool)
    return COMPILERS[tool](policy, list(catalogs))


def compile_all(policy: SkillsPolicy, catalogs: list[CatalogSource]) -> dict[str, CompiledPolicy]:
    return {tool: compile_policy(policy, catalogs, tool) for tool in TOOLS}


__all__ = [
    "COMPILERS",
    "COMPILER_VERSION",
    "POLICY_FIELDS",
    "TOOLS",
    "Artifact",
    "CatalogSource",
    "CompiledPolicy",
    "Warning",
    "compile_all",
    "compile_policy",
]
