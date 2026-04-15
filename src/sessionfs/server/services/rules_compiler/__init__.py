"""Rules compilers — deterministic text formatters for the 5 core AI tools.

Each compiler takes canonical rules + optional knowledge claims + optional
project context and emits a single file of a tool-specific format.

The managed marker at the top of each compiled file is what lets SessionFS
detect its own files on subsequent compiles. Unmanaged files with the same
name are never overwritten unless --force is passed.
"""

from __future__ import annotations

from sessionfs.server.services.rules_compiler.base import (
    CompileContext,
    CompileResult,
    KnowledgeClaim,
    RuleCompiler,
    aggregate_hash,
    compute_output_hash,
    estimate_tokens,
    is_managed_content,
    parse_managed_marker,
)
from sessionfs.server.services.rules_compiler.claude_code import ClaudeCodeCompiler
from sessionfs.server.services.rules_compiler.codex import CodexCompiler
from sessionfs.server.services.rules_compiler.copilot import CopilotCompiler
from sessionfs.server.services.rules_compiler.cursor import CursorCompiler
from sessionfs.server.services.rules_compiler.gemini import GeminiCompiler

# Registry: tool slug -> compiler instance.
# These are the 5 supported v0.9.9 tools.
COMPILERS: dict[str, RuleCompiler] = {
    "claude-code": ClaudeCodeCompiler(),
    "codex": CodexCompiler(),
    "cursor": CursorCompiler(),
    "copilot": CopilotCompiler(),
    "gemini": GeminiCompiler(),
}


SUPPORTED_TOOLS: tuple[str, ...] = tuple(COMPILERS.keys())


def get_compiler(tool: str) -> RuleCompiler:
    """Return the compiler for a tool slug. Raises KeyError if unknown."""
    return COMPILERS[tool]


__all__ = [
    "COMPILERS",
    "SUPPORTED_TOOLS",
    "CompileContext",
    "CompileResult",
    "KnowledgeClaim",
    "RuleCompiler",
    "aggregate_hash",
    "compute_output_hash",
    "estimate_tokens",
    "get_compiler",
    "is_managed_content",
    "parse_managed_marker",
]
