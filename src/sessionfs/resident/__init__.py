"""SessionFS Resident Runner — client-side, long-running review/implement loops.

R1: reviewer-runner skeleton — proves the loop + settle-path + trust
against a registered service key. No persistent mind yet (that is R2).
"""

from sessionfs.resident.config import ResidentConfig, LLMConfig
from sessionfs.resident.llm_adapter import ReviewLLM, ReviewContext, ReviewResult
from sessionfs.resident.runner import ResidentRunner

__all__ = [
    "ResidentConfig",
    "LLMConfig",
    "ReviewLLM",
    "ReviewContext",
    "ReviewResult",
    "ResidentRunner",
]
