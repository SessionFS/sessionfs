"""Gemini CLI rules compiler -> GEMINI.md."""

from __future__ import annotations

from sessionfs.server.services.rules_compiler.base import (
    CompileContext,
    CompileResult,
    _BaseCompiler,
)


class GeminiCompiler(_BaseCompiler):
    def __init__(self) -> None:
        super().__init__(
            tool="gemini",
            filename="GEMINI.md",
            token_ceiling=4000,
            comment_style="html",
        )

    def compile(self, ctx: CompileContext) -> CompileResult:
        return super().compile(ctx)
