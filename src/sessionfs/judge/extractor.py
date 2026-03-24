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
    """Classify a claim into a category based on its text.

    More specific categories are checked first to avoid false positives
    from broader patterns (e.g. code_change before file_operation).
    """
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


def _find_nearby_tool_results(messages: list[dict], msg_index: int, window: int = 5) -> list[int]:
    """Find tool_result message indices near a given message."""
    refs: list[int] = []
    start = max(0, msg_index - window)
    end = min(len(messages), msg_index + window + 1)
    for i in range(start, end):
        if i == msg_index:
            continue
        m = messages[i]
        role = m.get("role", "")
        if role == "tool":
            refs.append(i)
            continue
        content = m.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    refs.append(i)
                    break
    return refs


def _is_verifiable_sentence(sentence: str) -> bool:
    """Check if a sentence contains a verifiable claim."""
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
    # Also match direct statements about creating/modifying things
    if re.search(
        r"(?:I(?:'ve| have)?\s+)?\b(?:created|modified|deleted|updated|wrote|added|removed|fixed|implemented|installed|configured)\b",
        sentence,
        re.IGNORECASE,
    ):
        return True
    # Match speculative claims that reference concrete outcomes
    if _SPECULATIVE_WORDS.search(sentence) and re.search(
        r"\b(?:work|fail|break|error|issue|problem|bug|crash|compatible|support)\b",
        sentence,
        re.IGNORECASE,
    ):
        return True
    return False


def extract_claims(messages: list[dict]) -> list[Claim]:
    """Extract verifiable claims from assistant messages.

    Scans assistant messages for verifiable statements, matches them to
    nearby tool_result messages by proximity, categorises them, and rates
    confidence based on the language used.
    """
    claims: list[Claim] = []

    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue

        texts = _extract_text_blocks(msg)
        for text in texts:
            # Split into sentences (rough heuristic)
            sentences = re.split(r"(?<=[.!])\s+", text)
            for sentence in sentences:
                sentence = sentence.strip()
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

    return claims
