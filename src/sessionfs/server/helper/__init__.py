"""Session helper tools (piece 3).

The helper is an assistant that answers questions over the sessions and
projects the caller can already read.  This package provides the tool
registry, execution context, retrieval budget, DLP redaction, and retrieval
audit hooks — no HTTP routes.

Wired together by piece 4 (API + SSE), which builds a ``HelperContext`` per
turn and drives ``execute`` from ``sessionfs.judge.agent_stream`` tool calls.
"""

from __future__ import annotations

from sessionfs.server.helper.context import (
    HelperAuditSink,
    HelperContext,
    HelperScope,
    RetrievalBudget,
)
from sessionfs.server.helper.tools import TOOLS, execute, tool_specs

__all__ = [
    "HelperAuditSink",
    "HelperContext",
    "HelperScope",
    "RetrievalBudget",
    "TOOLS",
    "execute",
    "tool_specs",
]
