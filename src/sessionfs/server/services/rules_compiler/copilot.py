"""GitHub Copilot rules compiler -> .github/copilot-instructions.md."""

from __future__ import annotations

from sessionfs.server.services.rules_compiler.base import (
    CompileContext,
    CompileResult,
    _BaseCompiler,
)


class CopilotCompiler(_BaseCompiler):
    def __init__(self) -> None:
        super().__init__(
            tool="copilot",
            filename=".github/copilot-instructions.md",
            token_ceiling=2500,
            comment_style="html",
        )

    def compile(self, ctx: CompileContext) -> CompileResult:
        return super().compile(ctx)
