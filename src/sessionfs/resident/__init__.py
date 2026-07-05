"""SessionFS Resident Runner — client-side, long-running review/implement loops.

R1: reviewer-runner skeleton — proves the loop + settle-path + trust
against a registered service key.
R2: living context — hydrate durable + private context before each review,
write durable memory back after, and compact periodically.
R3: implementer — propose-only code writer that addresses implement/
fix_findings directives in a sandboxed worktree.
"""

from sessionfs.resident.config import ResidentConfig, LLMConfig
from sessionfs.resident.context import LivingContext, hydrate_living_context
from sessionfs.resident.llm_adapter import (
    ReviewLLM,
    ReviewContext,
    ReviewResult,
    ImplementLLM,
    ImplementContext,
    ImplementResult,
    FileChange,
)
from sessionfs.resident.memory import (
    write_reasoning,
    writeback_durable_knowledge,
    writeback_wiki_page,
    compact_memory,
    summarize_for_digest,
)
from sessionfs.resident.runner import ResidentRunner
from sessionfs.resident.implementer import run_implement_directive

__all__ = [
    "ResidentConfig",
    "LLMConfig",
    "LivingContext",
    "hydrate_living_context",
    "ReviewLLM",
    "ReviewContext",
    "ReviewResult",
    "ImplementLLM",
    "ImplementContext",
    "ImplementResult",
    "FileChange",
    "ResidentRunner",
    "run_implement_directive",
    "write_reasoning",
    "writeback_durable_knowledge",
    "writeback_wiki_page",
    "compact_memory",
    "summarize_for_digest",
]
