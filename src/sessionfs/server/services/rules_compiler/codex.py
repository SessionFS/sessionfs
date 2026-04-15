"""Codex rules compiler -> codex.md."""

from __future__ import annotations

from sessionfs.server.services.rules_compiler.base import (
    CompileContext,
    CompileResult,
    _BaseCompiler,
)


class CodexCompiler(_BaseCompiler):
    def __init__(self) -> None:
        super().__init__(
            tool="codex",
            filename="codex.md",
            token_ceiling=4000,
            comment_style="html",
        )

    def compile(self, ctx: CompileContext) -> CompileResult:
        return super().compile(ctx)
