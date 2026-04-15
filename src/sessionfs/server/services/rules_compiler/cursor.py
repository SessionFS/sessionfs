"""Cursor rules compiler -> .cursorrules.

Cursor reads .cursorrules as plain text — we use `#` comments for the
managed marker since HTML comments would be rendered literally.
"""

from __future__ import annotations

from sessionfs.server.services.rules_compiler.base import (
    CompileContext,
    CompileResult,
    _BaseCompiler,
)


class CursorCompiler(_BaseCompiler):
    def __init__(self) -> None:
        super().__init__(
            tool="cursor",
            filename=".cursorrules",
            # Cursor treats this file as a single prompt — keep it tight.
            token_ceiling=2000,
            comment_style="hash",
        )

    def compile(self, ctx: CompileContext) -> CompileResult:
        return super().compile(ctx)
