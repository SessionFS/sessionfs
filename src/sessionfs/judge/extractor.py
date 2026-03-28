"""Extract verifiable claims from assistant messages."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Claim:
    message_index: int
    text: str
    category: str  # file_operation, command_output, test_result, code_change, general
    confidence: str  # certain, likely, speculative
    evidence_refs: list[int] = field(default_factory=list)


# Patterns for categorising claims
_FILE_OP_RE = re.compile(
    r"(?:I(?:'ve| have)?\s+)?(created|modified|deleted|updated|wrote|removed|renamed|moved)\s+"
    r"(?:the\s+)?(?:file\s+)?[`'\"]?([^\s`'\",:]+)",
    re.IGNORECASE,
)
_CMD_RESULT_RE = re.compile(
    r"(?:running|executing|ran)\s+[`'\"]?(.+?)[`'\"]?\s+"
    r"(?:produces?|returns?|gives?|outputs?|shows?|results? in)",
    re.IGNORECASE,
)
_TEST_RESULT_RE = re.compile(
    r"(?:the\s+)?(?:tests?|specs?|suite)\s+(?:passes?|fails?|passed|failed|succeeds?|succeeded)",
    re.IGNORECASE,
)
_CODE_CHANGE_RE = re.compile(
    r"(?:I(?:'ve| have)?\s+)?(?:updated|changed|added|refactored|fixed|replaced|implemented|removed)\s+"
    r"(?:the\s+)?(?:function|method|class|variable|import|module|component|handler|route|endpoint)",
    re.IGNORECASE,
)

# Confidence markers
_CERTAIN_WORDS = re.compile(
    r"\b(?:definitely|certainly|confirmed|verified|successfully|done|completed)\b",
    re.IGNORECASE,
)
_SPECULATIVE_WORDS = re.compile(
    r"\b(?:might|could|may|possibly|perhaps|probably|seems?|appears?|I think|I believe)\b",
    re.IGNORECASE,
)


def _classify_category(text: str) -> str:
    """Classify a claim into a category based on its text."""
    if _TEST_RESULT_RE.search(text):
        return "test_result"
    if _CMD_RESULT_RE.search(text):
        return "command_output"
    if _CODE_CHANGE_RE.search(text):
        return "code_change"
    if _FILE_OP_RE.search(text):
        return "file_operation"
    return "general"


def _classify_confidence(text: str) -> str:
    """Rate confidence by language used in the claim."""
    if _CERTAIN_WORDS.search(text):
        return "certain"
    if _SPECULATIVE_WORDS.search(text):
        return "speculative"
    return "likely"


def _extract_text_blocks(message: dict) -> list[str]:
    """Extract text strings from a message's content."""
    content = message.get("content", [])
    if isinstance(content, str):
        return [content]
    texts: list[str] = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    return texts


def _has_tool_use(message: dict) -> bool:
    """Check if a message contains tool_use blocks."""
    content = message.get("content", [])
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


def _is_tool_result_message(message: dict) -> bool:
    """Check if a message is a tool result (role=tool or contains tool_result blocks)."""
    if message.get("role") == "tool":
        return True
    content = message.get("content", [])
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


def _find_nearby_tool_results(messages: list[dict], msg_index: int, window: int = 5) -> list[int]:
    """Find tool_result message indices near a given message."""
    refs: list[int] = []
    start = max(0, msg_index - window)
    end = min(len(messages), msg_index + window + 1)
    for i in range(start, end):
        if i == msg_index:
            continue
        if _is_tool_result_message(messages[i]):
            refs.append(i)
    return refs


def extract_claims(messages: list[dict]) -> list[Claim]:
    """Extract verifiable claims from assistant messages.

    Two extraction strategies:

    1. **Tool-context claims**: Any assistant text that follows tool results
       is a potential claim — the assistant is interpreting/summarizing tool
       output. These are extracted regardless of regex matches, since they
       can be verified against the tool output.

    2. **Standalone claims**: Assistant text that matches specific patterns
       (file operations, command results, test results, code changes) even
       without nearby tool results.

    This ensures Claude Code sessions (which heavily use tool calls) produce
    claims, while simpler sessions (Gemini CLI with inline text) also work.
    """
    claims: list[Claim] = []

    # Track which messages follow tool results
    recent_tool_result_indices: list[int] = []
    last_was_tool_result = False

    for idx, msg in enumerate(messages):
        # Track tool results
        if _is_tool_result_message(msg):
            recent_tool_result_indices.append(idx)
            last_was_tool_result = True
            # Don't continue — role=assistant msgs can have tool_result AND text
            if msg.get("role") != "assistant":
                continue

        # Skip non-assistant messages
        if msg.get("role") != "assistant":
            if not _has_tool_use(msg):
                last_was_tool_result = False
            continue

        # Extract text blocks from this message (even if it also has tool_use)
        texts = _extract_text_blocks(msg)
        has_text = any(t.strip() and len(t.strip()) >= 10 for t in texts)

        # If message only has tool_use (no text), don't reset tracking
        if not has_text:
            continue

        for text in texts:
            text = text.strip()
            if len(text) < 10:
                continue

            # Strategy 1: Tool-context claims
            # Any substantive assistant text after tool results is a claim
            if last_was_tool_result and recent_tool_result_indices:
                category = _classify_category(text)
                confidence = _classify_confidence(text)
                claims.append(
                    Claim(
                        message_index=idx,
                        text=text,
                        category=category,
                        confidence=confidence,
                        evidence_refs=list(recent_tool_result_indices),
                    )
                )
                recent_tool_result_indices = []
                last_was_tool_result = False
                continue

            # Strategy 2: Standalone claims (regex-matched)
            sentences = re.split(r"(?<=[.!])\s+", text)
            for sentence in sentences:
                sentence = sentence.strip()
                if len(sentence) < 15:
                    continue
                if not _is_verifiable_sentence(sentence):
                    continue

                category = _classify_category(sentence)
                confidence = _classify_confidence(sentence)
                evidence_refs = _find_nearby_tool_results(messages, idx)

                claims.append(
                    Claim(
                        message_index=idx,
                        text=sentence,
                        category=category,
                        confidence=confidence,
                        evidence_refs=evidence_refs,
                    )
                )

        # Reset tool result tracking after processing text
        last_was_tool_result = False
        recent_tool_result_indices = []

    return claims


def _is_verifiable_sentence(sentence: str) -> bool:
    """Check if a sentence contains a verifiable claim (for standalone extraction)."""
    sentence = sentence.strip()
    if len(sentence) < 15:
        return False
    patterns = [
        _FILE_OP_RE,
        _CMD_RESULT_RE,
        _TEST_RESULT_RE,
        _CODE_CHANGE_RE,
    ]
    for pat in patterns:
        if pat.search(sentence):
            return True
    if re.search(
        r"(?:I(?:'ve| have)?\s+)?\b(?:created|modified|deleted|updated|wrote|added|removed|fixed|implemented|installed|configured)\b",
        sentence,
        re.IGNORECASE,
    ):
        return True
    if _SPECULATIVE_WORDS.search(sentence) and re.search(
        r"\b(?:work|fail|break|error|issue|problem|bug|crash|compatible|support)\b",
        sentence,
        re.IGNORECASE,
    ):
        return True
    return False
