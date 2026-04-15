"""Claude Code rules compiler -> CLAUDE.md."""

from __future__ import annotations

from sessionfs.server.services.rules_compiler.base import (
    CompileContext,
    CompileResult,
    _BaseCompiler,
)


class ClaudeCodeCompiler(_BaseCompiler):
    def __init__(self) -> None:
        super().__init__(
            tool="claude-code",
            filename="CLAUDE.md",
            token_ceiling=6000,
            comment_style="html",
        )

    def compile(self, ctx: CompileContext) -> CompileResult:
        return super().compile(ctx)
