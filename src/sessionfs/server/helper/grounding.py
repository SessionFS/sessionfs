"""Sentence-level evidence for session-helper answers (claim-level grounding).

The helper answers with a small text protocol instead of provider JSON mode
(the 14 providers support structured output unevenly):

* every delivered tool result carries a server-assigned evidence id, shown to
  the model as a first line ``[evidence e12]``;
* the model writes the ids it relied on after each factual sentence, as
  ``[e12]`` or ``[e12, e14]``;
* the server checks every id against the tool results delivered in this
  conversation, removes the ids from the prose (an id whose result names one
  citable thing — a ticket, a wiki page, a session, a docs section — becomes
  that thing's normal citation marker), and keeps a per-sentence evidence map
  for the dashboard and API callers;
* when the answer used the protocol, a sentence that looks factual (numbers,
  dates, ids, names, status words) and carries neither a valid id nor a valid
  citation marker gets a visible "(not verified)" flag.

An answer with no valid id at all (a model that ignored the protocol) falls
back to the older checks: markers are still validated and an answer citing
nothing gets the "(No sources cited …)" note.  The answer is never dropped.

Offsets in the map are UTF-16 code units so the dashboard can apply them to
the JavaScript string directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# ``[e12]`` / ``[e12, e14]`` / ``[E12]`` — the evidence tag the model writes
# (a copied ``[evidence e12]`` header is accepted too).
EVIDENCE_TAG_RE = re.compile(
    r"\[\s*(?:evidence\s+)?[eE]\d{1,6}(?:\s*,\s*(?:evidence\s+)?[eE]\d{1,6})*\s*\]"
)
_EVIDENCE_ID_RE = re.compile(r"(?<![A-Za-z])[eE](\d{1,6})")
# A tag plus the horizontal whitespace before it (removed together so
# "done [e12]." becomes "done.").
# The lookbehind anchors a match at the START of a whitespace run, so a long
# run of spaces is scanned once rather than once per position (quadratic).
_TAG_WITH_SPACE_RE = re.compile(r"(?<![ \t])[ \t]*" + EVIDENCE_TAG_RE.pattern)
# Any citation marker (already validated upstream by the time it gets here).
_MARKER_RE = re.compile(r"\[(?:session|ticket|wiki|doc|meeting|chat):[^\]\n]{1,500}\]")
# Something absorbed after a sentence terminator: a tag or a marker.
_TRAILER_RE = re.compile(EVIDENCE_TAG_RE.pattern + "|" + _MARKER_RE.pattern)

# The first line the server puts on a delivered tool result.
EVIDENCE_HEADER_RE = re.compile(r"\A\[evidence (e\d{1,6})\]\n")

NOT_VERIFIED = "(not verified)"

# A pending sentence longer than this is released at its last space so a
# model that never ends a sentence can't hold the whole answer back.
_MAX_PENDING = 4000
_CLOSERS = "\"')]”’*_`"
# Characters _segment_end acts on, and a horizontal-whitespace run.
_SCAN_RE = re.compile(r"[\n\[.!?]")
_HWS_RE = re.compile(r"[ \t]*")
# _could_be_trailer only needs this much of the rest (it refuses > 520).
_TRAILER_PEEK = 521


def evidence_header(evidence_id: str) -> str:
    return f"[evidence {evidence_id}]\n"


def format_evidence_id(n: int) -> str:
    return f"e{n}"


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def tag_ids(tag: str) -> list[str]:
    """``[e12, E14]`` → ``["e12", "e14"]``."""
    return [f"e{int(m.group(1))}" for m in _EVIDENCE_ID_RE.finditer(tag)]


# ---------------------------------------------------------------------------
# Sentence segmentation (shared by the stream and the final pass)
# ---------------------------------------------------------------------------


def _could_be_trailer(rest: str) -> bool:
    """``rest`` starts with ``[`` that may still become a tag or marker."""
    if not rest.startswith("["):
        return False
    close = rest.find("]")
    if close != -1:
        return False
    if "\n" in rest:
        return False
    return len(rest) <= 520


def _segment_end(buf: str, start: int, final: bool) -> int | None:
    """End (exclusive) of the sentence starting at ``start``, or ``None``.

    A sentence ends at a newline (kept in the segment), or at ``.``/``!``/``?``
    (plus closing quotes/brackets) followed by whitespace; evidence tags and
    citation markers written right after the terminator belong to that
    sentence, as does the whitespace before the next sentence.  ``None`` means
    more text is needed to decide (never returned when ``final``).
    """
    n = len(buf)
    i = start
    while i < n:
        # Jump to the next character that can matter (C-speed search rather
        # than a Python step per character: the stream rescans its pending
        # buffer on every delta).
        hit = _SCAN_RE.search(buf, i)
        if hit is None:
            break
        i = hit.start()
        ch = buf[i]
        if ch == "\n":
            return i + 1
        if ch == "[":
            # A tag or marker inside the sentence is skipped whole so a ``.``
            # inside a wiki slug is never a terminator.
            m = _TRAILER_RE.match(buf, i)
            if m is not None:
                i = m.end()
                continue
            if not final and _could_be_trailer(buf[i : i + _TRAILER_PEEK]):
                return None
        if ch in ".!?":
            j = i + 1
            while j < n and buf[j] in _CLOSERS:
                j += 1
            if j >= n:
                return n if (final or n - start > _MAX_PENDING) else None
            if buf[j] in " \t":
                k = j
                while True:
                    ws = _HWS_RE.match(buf, k)  # ``[ \t]*`` always matches
                    k = ws.end() if ws is not None else k
                    if k >= n:
                        # The pending cap applies here too: a terminator
                        # followed by an endless whitespace run must not
                        # hold (and rescan) an unbounded buffer.
                        return n if (final or n - start > _MAX_PENDING) else None
                    m = _TRAILER_RE.match(buf, k)
                    if m is not None:
                        k = m.end()
                        continue
                    if not final and _could_be_trailer(buf[k : k + _TRAILER_PEEK]):
                        return None
                    return k
            if buf[j] == "\n":
                return j + 1
        i += 1
    if final:
        return n if n > start else None
    if n - start > _MAX_PENDING:
        cut = buf.rfind(" ", start, n)
        return cut + 1 if cut > start else n
    return None


def split_segments(buf: str, final: bool) -> tuple[list[str], str]:
    """Split ``buf`` into complete sentence segments and the undecided rest."""
    segments: list[str] = []
    pos = 0
    while pos < len(buf):
        end = _segment_end(buf, pos, final)
        if end is None:
            break
        segments.append(buf[pos:end])
        pos = end
    return segments, buf[pos:]


# ---------------------------------------------------------------------------
# Evidence rows
# ---------------------------------------------------------------------------


@dataclass
class EvidenceRow:
    """One delivered tool result the model may name as evidence."""

    id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    at: str | None = None
    # The citation marker for the one thing this result is about (a ticket,
    # a wiki page, a session, a docs section), or None for a list/search.
    marker: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "at": self.at,
            "marker": self.marker,
        }


def primary_marker(tool: str, args: dict[str, Any], refs: dict[str, Any] | None) -> str | None:
    """The citation marker for a single-subject tool result, else ``None``.

    Only for results about exactly one citable thing, and only when that thing
    is in the result's own structured refs (so it was resolved and readable).
    """
    refs = refs or {}
    if tool == "get_ticket":
        ticket_id = args.get("ticket_id")
        if isinstance(ticket_id, str) and ticket_id in (refs.get("ticket_ids") or []):
            return f"[ticket:{ticket_id}]"
        return None
    if tool == "get_wiki_page":
        wiki = refs.get("wiki_refs") or []
        if len(wiki) == 1 and isinstance(wiki[0], (list, tuple)) and len(wiki[0]) == 2:
            return f"[wiki:{wiki[0][0]}/{wiki[0][1]}]"
        return None
    if tool in ("get_session_summary", "get_session_excerpts"):
        sessions = refs.get("session_ids") or []
        if len(sessions) == 1 and isinstance(sessions[0], str):
            return f"[session:{sessions[0]}]"
        return None
    if tool == "get_sessionfs_doc":
        docs = refs.get("doc_refs") or []
        if len(docs) == 1 and isinstance(docs[0], str):
            return f"[doc:{docs[0]}]"
        return None
    return None


# ---------------------------------------------------------------------------
# Converting one segment
# ---------------------------------------------------------------------------

MarkerCheck = Callable[[str], Awaitable[bool]]


async def _convert_segment(
    segment: str,
    evidence: dict[str, EvidenceRow],
    marker_ok: MarkerCheck | None,
) -> tuple[str, list[str]]:
    """Remove the evidence tags from ``segment``.

    Returns ``(text, valid_ids)``.  A valid id whose result has a single
    citable subject is replaced by that subject's marker (once per segment,
    and only when ``marker_ok`` accepts it); every other tag is removed with
    the whitespace before it.  Unknown ids are dropped silently.
    """
    valid: list[str] = []
    present = set(_MARKER_RE.findall(segment))
    out: list[str] = []
    pos = 0
    for match in _TAG_WITH_SPACE_RE.finditer(segment):
        out.append(segment[pos : match.start()])
        pos = match.end()
        markers: list[str] = []
        for eid in tag_ids(match.group(0)):
            row = evidence.get(eid)
            if row is None:
                continue
            if eid not in valid:
                valid.append(eid)
            marker = row.marker
            if marker and marker not in present:
                if marker_ok is None or await marker_ok(marker):
                    present.add(marker)
                    markers.append(marker)
        if markers:
            # Keep the space the model put before the tag, if any.
            lead = " " if match.group(0)[:1] in " \t" else ""
            out.append(lead + "".join(markers))
    out.append(segment[pos:])
    return "".join(out), valid


# ---------------------------------------------------------------------------
# Looks-factual heuristic
# ---------------------------------------------------------------------------

_STATUS_RE = re.compile(
    r"\b(?:done|open|opened|closed|resolved|unresolved|fail(?:ed|ing|s|ure|ures)?|"
    r"pass(?:ed|ing|es)?|blocked|running|in[ _]progress|dry[- ]?runs?|merged|"
    r"cancel(?:l)?ed|approved|rejected|pending|completed?|shipped|released|"
    r"deployed|broken|fixed|assigned|unassigned|active|inactive|expired|"
    r"enabled|disabled|stale|overdue|explained|unexplained)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|"
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|yesterday|"
    r"today|tomorrow|last week|last month|ago)\b",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"\b[a-z]{1,8}_[A-Za-z0-9]{6,}\b")
_NAME_RE = re.compile(r"(?<=[a-z,;:] )[A-Z][a-z]+(?: [A-Z][a-z]+)+")
_META_PREFIXES = (
    "i ",
    "i'",
    "i’",
    "let me",
    "here is",
    "here are",
    "here's",
    "here’s",
    "sure",
    "want me",
    "would you",
    "do you",
    "if you want",
    "if you'd",
    "in short",
    "to summarise",
    "to summarize",
)
_BLOCK_PREFIX_RE = re.compile(r"^[ \t]*(?:>[ \t]*)*(?:[-*+][ \t]+|\d{1,3}[.)][ \t]+)?")


def looks_factual(sentence: str) -> bool:
    """Heuristic: does this sentence state a checkable fact?

    Numbers, dates, ids, two-word names and status words count; questions,
    headings, meta sentences ("Here is what I found", "Let me check") and
    very short fragments don't.
    """
    text = _MARKER_RE.sub("", sentence)
    text = _BLOCK_PREFIX_RE.sub("", text).strip().strip("*_").strip()
    if not text or text.startswith(("#", "|", "```")):
        return False
    if text.endswith("?"):
        return False
    lower = text.lower()
    if lower.startswith(_META_PREFIXES):
        return False
    words = text.split()
    has_digit = any(ch.isdigit() for ch in text)
    if len(words) < 3 and not has_digit and not _ID_RE.search(text):
        return False
    return bool(
        has_digit
        or _ID_RE.search(text)
        or _DATE_RE.search(text)
        or _STATUS_RE.search(text)
        or _NAME_RE.search(text)
    )


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class GroundingStream:
    """Sentence-buffered pass that strips evidence tags from streamed text.

    Each sentence is held until it is complete (its trailing tags have
    arrived), then released with its tags turned into citation markers or
    removed.  "(not verified)" flags are not streamed: whether the answer used
    the protocol is only known at the end, so the flagged text arrives with
    the final ``grounding`` event.
    """

    def __init__(self, evidence: dict[str, EvidenceRow], marker_ok: MarkerCheck | None) -> None:
        self._evidence = evidence
        self._marker_ok = marker_ok
        self._buf = ""

    async def feed(self, text: str) -> str:
        self._buf += text
        segments, self._buf = split_segments(self._buf, final=False)
        out = [(await _convert_segment(s, self._evidence, self._marker_ok))[0] for s in segments]
        return "".join(out)

    async def flush(self) -> str:
        segments, rest = split_segments(self._buf, final=True)
        self._buf = ""
        out = [(await _convert_segment(s, self._evidence, self._marker_ok))[0] for s in segments]
        return "".join(out) + rest


# ---------------------------------------------------------------------------
# Final pass
# ---------------------------------------------------------------------------


@dataclass
class GroundedAnswer:
    text: str
    # "strict": the answer used evidence ids and sentences are checked;
    # "fallback": no valid id — older marker checks only, nothing flagged.
    mode: str
    sentences: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    unverified: int = 0

    def payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "sentences": self.sentences,
            "evidence": self.evidence,
            "unverified_count": self.unverified,
        }


def _flag_position(body: str) -> int:
    """Where "(not verified)" goes: before the final terminator, else the end."""
    end = len(body)
    j = end
    while j > 0 and body[j - 1] in _CLOSERS:
        j -= 1
    if j > 0 and body[j - 1] in ".!?":
        return j - 1
    return end


async def ground_answer(
    text: str,
    evidence: dict[str, EvidenceRow],
    *,
    marker_ok: MarkerCheck | None = None,
    flag: bool = True,
) -> GroundedAnswer:
    """Convert evidence tags, build the per-sentence map and flag gaps.

    ``text`` is the answer with citation markers already validated.  Flags
    are added only when ``flag`` is true and the answer named at least one
    valid evidence id.
    """
    segments, rest = split_segments(text, final=True)
    if rest:
        segments.append(rest)
    converted: list[tuple[str, list[str]]] = [
        await _convert_segment(s, evidence, marker_ok) for s in segments
    ]
    any_valid = any(ids for _, ids in converted)
    mode = "strict" if any_valid else "fallback"
    do_flag = flag and any_valid

    out: list[str] = []
    offset = 0  # UTF-16 units of ``out`` so far
    sentences: list[dict[str, Any]] = []
    used: list[str] = []
    unverified = 0
    in_code = False
    for seg, ids in converted:
        stripped_line = seg.strip()
        if stripped_line.startswith("```"):
            in_code = not in_code
            out.append(seg)
            offset += utf16_len(seg)
            continue
        lead = len(seg) - len(seg.lstrip())
        prefix_match = _BLOCK_PREFIX_RE.match(seg[lead:])
        lead += prefix_match.end() if prefix_match else 0
        body_end = len(seg.rstrip())
        body = seg[lead:body_end]
        markers = _MARKER_RE.findall(body)
        status: str | None = None
        if ids or markers:
            status = "supported"
        elif do_flag and not in_code and body and looks_factual(body):
            status = "unverified"
        start = offset + utf16_len(seg[:lead])
        if status == "unverified":
            pos = _flag_position(body)
            spacer = "" if pos == 0 or body[pos - 1] in " \t" else " "
            flagged = body[:pos] + spacer + NOT_VERIFIED + body[pos:]
            flag_start = start + utf16_len(body[:pos])
            flag_end = flag_start + utf16_len(spacer + NOT_VERIFIED)
            new_seg = seg[:lead] + flagged + seg[body_end:]
            sentences.append(
                {
                    "start": start,
                    "end": start + utf16_len(flagged),
                    "status": "unverified",
                    "evidence": [],
                    "flag": [flag_start, flag_end],
                }
            )
            unverified += 1
            out.append(new_seg)
            offset += utf16_len(new_seg)
            continue
        if status == "supported":
            sentences.append(
                {
                    "start": start,
                    "end": start + utf16_len(body),
                    "status": "supported",
                    "evidence": ids,
                }
            )
            for eid in ids:
                if eid not in used:
                    used.append(eid)
        out.append(seg)
        offset += utf16_len(seg)
    return GroundedAnswer(
        text="".join(out),
        mode=mode,
        sentences=sentences,
        evidence=[evidence[eid].to_json() for eid in used if eid in evidence],
        unverified=unverified,
    )


def strip_evidence_tags(text: str) -> str:
    """Remove every evidence tag (used when grounding is off)."""
    return _TAG_WITH_SPACE_RE.sub("", text)
