"""Server-side DLP (Data Loss Prevention) module.

Provides text redaction, tar.gz repack with redacted content, org policy
extraction, and policy validation. Works with DLPFinding objects from the
security.secrets scanner.
"""

from __future__ import annotations

import dataclasses
import functools
import io
import json
import logging
import re
import tarfile

from sessionfs.server.db.models import Organization
from sessionfs.security.secrets import (
    DLPFinding,
    PHI_PATTERNS,
    SECRET_PATTERNS,
    ALLOWLIST,
    scan_dlp as scan_dlp_full,
    scan_dlp_builtin,
)

logger = logging.getLogger("sessionfs.api")

VALID_MODES = {"warn", "redact", "block"}
VALID_CATEGORIES = {"secrets", "phi"}

# Default member-size cap when the caller doesn't pass one. Matches the
# free-tier limit at routes/sessions.py:SFS_MAX_SYNC_MEMBER_BYTES_FREE so
# unauthenticated / non-tier paths behave conservatively. Production
# sync_push always passes a tier-resolved limit — the constant only
# kicks in for callers that don't have a User context.
DEFAULT_DLP_MEMBER_LIMIT_BYTES = 10 * 1024 * 1024


class DlpMemberTooLargeError(Exception):
    """Raised by redact_and_repack when a tar member exceeds the
    caller-supplied size limit. Carries enough detail for the route to
    return the same structured 413 envelope that _check_member_sizes
    uses, so DLP-mode failures look identical to non-DLP failures.
    """

    def __init__(self, member_name: str, member_size: int, limit_bytes: int):
        self.member_name = member_name
        self.member_size = member_size
        self.limit_bytes = limit_bytes
        super().__init__(
            f"Member too large: {member_name} ({member_size} bytes, "
            f"limit {limit_bytes} bytes)"
        )

DEFAULT_DLP_POLICY: dict = {
    "enabled": False,
    "mode": "warn",
    "categories": ["secrets"],
}


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _is_allowlisted(text: str) -> bool:
    """Check if the matched text is a known false positive."""
    return any(pattern.search(text) for pattern in ALLOWLIST)


def scan_dlp(
    text: str,
    categories: list[str] | None = None,
) -> list[DLPFinding]:
    """Scan text for secrets and/or PHI, returning DLPFinding objects.

    Args:
        text: The text to scan (may be multiline).
        categories: Which categories to scan. Defaults to ["secrets"].

    Returns:
        List of DLPFinding objects with match details.

    The rules live in ``security.secrets.scan_dlp_builtin`` so client-side
    validators (``sfs skill validate``) run exactly the same scan.
    """
    return scan_dlp_builtin(text, categories)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def _apply_redactions(text: str, replacements: list[tuple[int, int, str]]) -> str:
    """Apply (start, end, replacement) spans, merging overlaps into their union.

    Shared by the raw-text and value-level redaction paths. Overlapping spans
    (e.g. a short custom match inside a longer password assignment) become one
    span covering every character any of them covered, labelled with the
    longest match; dropping the later span instead could leave part of a
    secret in place.
    """
    if not replacements:
        return text

    replacements.sort(key=lambda r: (r[0], -(r[1] - r[0])))
    merged: list[tuple[int, int, str]] = []
    longest: list[int] = []
    for start, end, repl in replacements:
        if merged and start < merged[-1][1]:
            m_start, m_end, m_repl = merged[-1]
            label = repl if end - start > longest[-1] else m_repl
            merged[-1] = (m_start, max(m_end, end), label)
            longest[-1] = max(longest[-1], end - start)
            continue
        merged.append((start, end, repl))
        longest.append(end - start)

    # Apply in reverse order to preserve positions
    result = text
    for start, end, repl in reversed(merged):
        result = result[:start] + repl + result[end:]

    return result


_BUILTIN_LABELS = frozenset(SECRET_PATTERNS) | frozenset(PHI_PATTERNS) | {
    "residual",
    "custom_pattern",
}

# Custom pattern names only become marker labels when they are plain
# identifiers that no detector flags, so a policy can't put a secret into a
# marker (which later scans trust and skip).
_SAFE_LABEL = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


def _policy_key(custom_patterns: list[dict] | None) -> tuple:
    key = []
    for cp in custom_patterns or []:
        if not isinstance(cp, dict):
            continue
        name, regex = cp.get("name"), cp.get("regex")
        if isinstance(name, str) and isinstance(regex, str):
            key.append(
                (name, regex, str(cp.get("category", "secret")), str(cp.get("severity", "high")))
            )
    return tuple(key)


@functools.lru_cache(maxsize=2048)
def _label_is_safe(name: str, policy_key: tuple) -> bool:
    if name in _BUILTIN_LABELS:
        return True
    if not _SAFE_LABEL.fullmatch(name):
        return False
    custom = [
        {"name": n, "regex": r, "category": c, "severity": sev} for n, r, c, sev in policy_key
    ]
    try:
        return not scan_dlp_full(name, categories=["secrets", "phi"], custom_patterns=custom or None)
    except Exception:
        return False


def _marker_for(pattern_name: str, custom_patterns: list[dict] | None = None) -> str:
    """The redaction marker for a finding: its pattern name when that is safe
    to show, otherwise the generic ``custom_pattern`` label."""
    label = (
        pattern_name
        if _label_is_safe(pattern_name, _policy_key(custom_patterns))
        else "custom_pattern"
    )
    return f"[REDACTED:{label}]"


def redact_text(
    text: str, findings: list[DLPFinding], custom_patterns: list[dict] | None = None
) -> str:
    """Replace each finding's match_text with ``[REDACTED:{pattern_name}]``.

    Operates on RAW text, so a finding whose match_text ends on a JSON escape
    sequence (e.g. a backslash escaping a closing quote) can leave the
    surrounding JSON unbalanced. JSON members should instead go through
    ``redact_and_repack``'s value-level path, which re-serializes from
    decoded objects; this function remains the fallback for non-JSON text
    (and for lines that fail to parse as JSON).
    """
    if not findings:
        return text

    # Build (start, end, replacement) tuples from the original text
    replacements: list[tuple[int, int, str]] = []
    for finding in findings:
        replacement = _marker_for(finding.pattern_name, custom_patterns)
        # Find all occurrences of match_text and replace them
        pattern = re.escape(finding.match_text)
        for match in re.finditer(pattern, text):
            replacements.append((match.start(), match.end(), replacement))

    return _apply_redactions(text, replacements)


def _redact_decoded_value(
    text: str,
    *,
    categories: list[str],
    custom_patterns: list[dict] | None,
    allowlist: list[str] | None,
    known: list[DLPFinding],
) -> str:
    """Redact one DECODED JSON string value.

    Re-runs the full DLP scanner on the decoded string (not the escaped
    serialization), so patterns whose anchors sit inside JSON-escaped quotes
    (``generic_password_assignment`` — any secret quoted within a JSON string
    value) are found here where the raw-text scan cannot see them. Uses the
    same categories / custom patterns / allowlist as the raw-text scan, so the
    result is at least as strict as the raw pass and never resurrects a JSON
    escape sequence.

    ``known`` carries every secret the raw-text scan already found (plus its
    JSON-decoded spelling), so anything the old raw pass redacted is redacted
    here too even if the decoded re-scan would not match it on its own.
    """
    # Existing genuine markers are ignored so a pattern can't treat one as a
    # secret (password = "[REDACTED:…]"); spans are mapped back to ``text``.
    spans = _policy_spans(
        text, categories=categories, custom_patterns=custom_patterns, allowlist=allowlist
    )
    stripped, offsets = _strip_markers(text, custom_patterns)
    for finding in known:
        if not finding.match_text:
            continue
        for m in re.finditer(re.escape(finding.match_text), stripped):
            start, end = _to_original(offsets, m.start(), m.end())
            spans.append((start, end, _marker_for(finding.pattern_name, custom_patterns)))
    if not spans:
        return text
    return _apply_redactions(text, spans)


def _known_secret_spellings(findings: list[DLPFinding]) -> list[DLPFinding]:
    """Raw-scan findings plus the decoded spelling of each matched secret.

    A raw finding's ``match_text`` comes from the escaped serialization, so it
    may contain JSON escapes (``\\"``, ``\\n``) or end on the backslash of an
    escaped quote. Decoded string values contain the unescaped form instead.
    """
    out: list[DLPFinding] = []
    seen: set[str] = set()
    for f in findings:
        trimmed = f.match_text.rstrip("\\")
        spellings = [f.match_text, trimmed]
        try:
            spellings.append(json.loads('"' + trimmed + '"'))
        except (json.JSONDecodeError, ValueError):
            pass
        # The escaped form too: a finding from decoded text must also be found
        # in a malformed line that is redacted as raw text.
        spellings += [json.dumps(t)[1:-1] for t in list(spellings) if isinstance(t, str)]
        for text in spellings:
            if isinstance(text, str) and text and text not in seen:
                seen.add(text)
                out.append(dataclasses.replace(f, match_text=text))
    return out


# JSON nested deeper than this cannot be inspected safely (recursion), so DLP
# refuses it rather than letting it pass unscanned. Real sessions are shallow.
MAX_JSON_DEPTH = 200


class DlpUninspectableError(ValueError):
    """A JSON member DLP could not fully inspect (e.g. pathologically deep)."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    # json.loads keeps only the LAST value of a repeated key, so an earlier
    # value would never be scanned (or stored redacted). Refuse such objects.
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise DlpUninspectableError("JSON object repeats a key")
        out[key] = value
    return out


class _JsonNumber:
    """A JSON number kept as its exact source text.

    Parsing numbers into float/int and writing them back would round long
    decimals and turn 1e9999 into Infinity (not valid JSON), corrupting
    values redaction never meant to touch. Redaction writes this text back
    verbatim.
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _JsonNumber) and other.text == self.text

    def __hash__(self) -> int:
        return hash(self.text)

    def __repr__(self) -> str:
        return self.text


def _is_number(value: object) -> bool:
    return isinstance(value, _JsonNumber) or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    )


def _number_text(value: object) -> str:
    return value.text if isinstance(value, _JsonNumber) else json.dumps(value)


def _loads_for_dlp(text: str) -> object:
    """Parse JSON for DLP. Syntax errors propagate as JSONDecodeError (the
    caller treats the text as not-JSON); anything else that stops a full
    parse (too deep, repeated keys, an integer past Python's digit limit)
    raises DlpUninspectableError so the content is refused, not passed."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_JsonNumber,
            parse_int=_JsonNumber,
            parse_constant=_JsonNumber,
        )
    except json.JSONDecodeError:
        raise
    except DlpUninspectableError:
        raise
    except (RecursionError, ValueError, OverflowError) as exc:
        raise DlpUninspectableError(f"JSON could not be fully parsed: {exc}") from exc


# Characters that would end or split the synthetic quoted value are replaced
# so a value like abc"defghijk is still seen as one run of value characters by
# assignment patterns: quotes, backslash, and every character str.splitlines()
# treats as a line boundary (scanners work line by line).
_PAIR_NEUTRAL = str.maketrans(
    {
        c: "_"
        for c in "\"'`\\"
        + "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
    }
)


_JSON_ESCAPE = re.compile(r'\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))')
_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _unescape_with_map(line: str) -> tuple[str, list[int]]:
    """Undo JSON string escapes, returning the text and an offset map.

    ``offsets[i]`` is the index in ``line`` where unescaped character ``i``
    came from; ``offsets[len(text)]`` is ``len(line)``. Lets a match found in
    the unescaped text be redacted at the exact original span, whatever escape
    spelling (``\\"``, ``\\u0022``, ``\\/``) was used.
    """
    chars: list[str] = []
    offsets: list[int] = []
    pos = 0
    for match in _JSON_ESCAPE.finditer(line):
        for i in range(pos, match.start()):
            chars.append(line[i])
            offsets.append(i)
        if match.group(1):
            chars.append(chr(int(match.group(1), 16)))
        else:
            chars.append(_SIMPLE_ESCAPES[match.group(2)])
        offsets.append(match.start())
        pos = match.end()
    for i in range(pos, len(line)):
        chars.append(line[i])
        offsets.append(i)
    offsets.append(len(line))
    return "".join(chars), offsets


def _best_effort_unescape(line: str) -> str:
    """Undo JSON string escapes in a line that is not valid JSON.

    A malformed line can't be decoded, but its quoted secrets still sit behind
    escaped quotes (``password = \\"…\\"``) that raw patterns can't see.
    """
    return _unescape_with_map(line)[0]


# A redaction marker. Markers are never secrets, but some patterns would match them
# (e.g. password = "[REDACTED:…]"), so re-scans mask genuine ones first.
@functools.lru_cache(maxsize=64)
def _marker_re_for(custom_names: tuple[str, ...]) -> re.Pattern[str]:
    labels = set(_BUILTIN_LABELS) | set(custom_names)
    alternatives = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
    # No "#n" collision suffix here: it is only ever appended to JSON KEYS,
    # and accepting it would let "[REDACTED:x]#12345678" hide the digits. A
    # leftover "#2" is harmless to scan.
    return re.compile(rf"\[REDACTED:(?:{alternatives})\]")


def custom_marker_labels(custom_patterns: list[dict] | None) -> list[str]:
    """The custom pattern names usable as marker labels: plain identifiers
    that no detector flags (see ``_marker_for``)."""
    key = _policy_key(custom_patterns)
    return sorted(
        {
            str(cp["name"])
            for cp in (custom_patterns or [])
            if isinstance(cp, dict)
            and isinstance(cp.get("name"), str)
            and _label_is_safe(cp["name"], key)
        }
    )


def _marker_re(
    custom_patterns: list[dict] | None, trusted_labels: tuple[str, ...] = ()
) -> re.Pattern[str]:
    """A redaction marker this code could have written for the policy.

    Only labels redaction actually uses (built-in pattern names, the policy's
    custom pattern names, "residual") count. Pushed text merely shaped like a
    marker (``[REDACTED:password = "…"]``) is NOT trusted and gets scanned.
    """
    labels = set(custom_marker_labels(custom_patterns))
    # Labels vetted earlier (recorded with a session) still get the built-in
    # check: never trust a label that is itself secret-shaped.
    labels |= {label for label in trusted_labels if _label_is_safe(label, ())}
    return _marker_re_for(tuple(sorted(labels)))


def _strip_markers(
    text: str, custom_patterns: list[dict] | None, trusted_labels: tuple[str, ...] = ()
) -> tuple[str, list[int] | None]:
    """``text`` with genuine markers removed, plus a map back to ``text``.

    ``offsets[i]`` is the index in ``text`` of kept character ``i`` (None when
    nothing was removed). Markers are removed rather than replaced: any
    stand-in character could close or split the surrounding value and hide a
    secret written right next to a marker.
    """
    if "[REDACTED:" not in text:
        return text, None
    pieces: list[str] = []
    offsets: list[int] = []
    pos = 0
    for m in _marker_re(custom_patterns, trusted_labels).finditer(text):
        pieces.append(text[pos:m.start()])
        offsets.extend(range(pos, m.start()))
        pos = m.end()
    if pos == 0:
        return text, None
    pieces.append(text[pos:])
    offsets.extend(range(pos, len(text)))
    return "".join(pieces), offsets


def _to_original(offsets: list[int] | None, start: int, end: int) -> tuple[int, int]:
    """Map a non-empty ``[start, end)`` span in stripped text back to ``text``."""
    if offsets is None:
        return start, end
    return offsets[start], offsets[end - 1] + 1


def dedupe_findings(findings: list[DLPFinding]) -> list[DLPFinding]:
    """One finding per (pattern, matched text).

    Archive scans read each value several ways (raw JSON, decoded string,
    key = "value" pair), so one secret can match up to three times; report
    and count it once.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[DLPFinding] = []
    for finding in findings:
        key = (finding.pattern_name, finding.match_text)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def mask_redaction_markers(
    text: str,
    custom_patterns: list[dict] | None = None,
    trusted_labels: tuple[str, ...] = (),
) -> str:
    """``text`` with genuine redaction markers removed, for archive-level scans.

    Only labels redaction writes for this policy count as markers, so an
    already-redacted session is not flagged again while marker-shaped text
    with any other label is still scanned. ``custom_patterns`` must be the
    full pattern definitions (names alone can't be vetted against the
    policy); ``trusted_labels`` are labels already vetted earlier.
    """
    return _strip_markers(text, custom_patterns, trusted_labels)[0]


def _policy_spans(
    text: str,
    *,
    categories: list[str],
    custom_patterns: list[dict] | None,
    allowlist: list[str] | None,
) -> list[tuple[int, int, str]]:
    """``(start, end, marker)`` for every policy match in ``text``, ignoring
    existing redaction markers. Spans are in ``text`` coordinates."""
    stripped, offsets = _strip_markers(text, custom_patterns)
    spans: list[tuple[int, int, str]] = []
    for finding in scan_dlp_full(
        stripped, categories=categories, custom_patterns=custom_patterns, allowlist=allowlist
    ):
        if not finding.match_text:
            continue
        for m in re.finditer(re.escape(finding.match_text), stripped):
            start, end = _to_original(offsets, m.start(), m.end())
            spans.append((start, end, _marker_for(finding.pattern_name, custom_patterns)))
    return spans


# Passes allowed for redaction to settle before a member is refused.
_SETTLE_PASSES = 3


def _malformed_spans(
    line: str,
    *,
    known: list[DLPFinding],
    categories: list[str],
    custom_patterns: list[dict] | None,
    allowlist: list[str] | None,
    rescan_raw: bool = True,
) -> list[tuple[int, int, str]]:
    """Every span to redact in a non-JSON line, all in ORIGINAL coordinates:
    known spellings, policy matches on the raw text, and policy matches on the
    unescaped text mapped back through the offset map.

    ``rescan_raw=False`` skips the raw policy scan for text the caller has
    already scanned (its matches arrive as ``known``)."""
    policy = {"categories": categories, "custom_patterns": custom_patterns, "allowlist": allowlist}
    spans: list[tuple[int, int, str]] = []
    for finding in known:
        if not finding.match_text:
            continue
        for m in re.finditer(re.escape(finding.match_text), line):
            spans.append((m.start(), m.end(), _marker_for(finding.pattern_name, custom_patterns)))
    if rescan_raw:
        spans += _policy_spans(line, **policy)
    if "\\" not in line:
        # No escapes: the unescaped text is the line itself, already scanned.
        return spans
    unescaped, offsets = _unescape_with_map(line)
    for start, end, marker in _policy_spans(unescaped, **policy):
        spans.append((offsets[start], offsets[end], marker))
    stripped, strip_offsets = _strip_markers(unescaped, custom_patterns)
    for finding in known:
        if not finding.match_text:
            continue
        for m in re.finditer(re.escape(finding.match_text), stripped):
            u_start, u_end = _to_original(strip_offsets, m.start(), m.end())
            spans.append(
                (offsets[u_start], offsets[u_end], _marker_for(finding.pattern_name, custom_patterns))
            )
    return spans


def _redact_malformed_line(
    line: str,
    *,
    known: list[DLPFinding],
    categories: list[str],
    custom_patterns: list[dict] | None,
    allowlist: list[str] | None,
) -> str:
    """Redact a line that is not valid JSON (it can't be made worse).

    All matches are found against the line as it stands (raw, and unescaped
    then mapped back), applied together, and the result re-scanned until
    clean. Raises ValueError if it does not settle.
    """
    scan = {
        "known": known,
        "categories": categories,
        "custom_patterns": custom_patterns,
        "allowlist": allowlist,
    }
    # The original line was already scanned raw by the caller (its matches
    # are in ``known``); only text changed by redaction is re-scanned raw.
    spans = _malformed_spans(line, **scan, rescan_raw=False)
    if not spans:
        return line
    current = _apply_redactions(line, spans)
    for _ in range(_SETTLE_PASSES):
        spans = _malformed_spans(current, **scan)
        if not spans:
            return current
        current = _apply_redactions(current, spans)
    if _malformed_spans(current, **scan):
        raise ValueError("DLP could not fully redact an unreadable line")
    return current


def _pair_text(key: str, value_text: str) -> tuple[str, int]:
    """A synthetic ``key = "value"`` line and the offset where the value starts.

    Assignment-style patterns (``password = "…"``, ``AccountKey=…``) need the
    field name next to the value; in a JSON object the two are separate
    tokens, so neither the leaf alone nor the serialized JSON matches. Both
    sides are neutralized first so no character in them can end the quoted
    value early. (The value itself is also scanned on its own, unaltered.)
    """
    prefix = f'{key.translate(_PAIR_NEUTRAL)} = "'
    return f'{prefix}{value_text.translate(_PAIR_NEUTRAL)}"', len(prefix)


def _redact_json_value(
    obj: object,
    *,
    categories: list[str],
    custom_patterns: list[dict] | None,
    allowlist: list[str] | None,
    known: list[DLPFinding],
    depth: int = 0,
) -> tuple[object, bool]:
    """Recursively redact a decoded JSON object.

    String values, dict keys and numbers are all scanned: a secret can sit in
    a key (e.g. an env map keyed by a token) or be all digits. Each scalar in
    an object is also scanned together with its key, and a match reaching into
    the value replaces the whole value. A redacted number becomes the marker
    string. Returns ``(redacted_obj, changed)``.
    """
    if depth > MAX_JSON_DEPTH:
        raise DlpUninspectableError("JSON is nested too deeply to scan")
    scan = {
        "categories": categories,
        "custom_patterns": custom_patterns,
        "allowlist": allowlist,
        "known": known,
    }
    if isinstance(obj, str):
        if _marker_re(custom_patterns).fullmatch(obj):
            return obj, False
        redacted = _redact_decoded_value(obj, **scan)
        return redacted, redacted != obj
    if _is_number(obj):
        as_text = _number_text(obj)
        redacted = _redact_decoded_value(as_text, **scan)
        if redacted != as_text:
            return redacted, True
        return obj, False
    if isinstance(obj, list):
        changed = False
        out: list[object] = []
        for item in obj:
            new_item, c = _redact_json_value(item, **scan, depth=depth + 1)
            out.append(new_item)
            changed = changed or c
        return out, changed
    if isinstance(obj, dict):
        changed = False
        out_dict: dict = {}
        for key, value in obj.items():
            new_key = _redact_decoded_value(key, **scan) if isinstance(key, str) else key
            changed = changed or new_key != key
            new_value, c = _redact_json_value(value, **scan, depth=depth + 1)
            changed = changed or c
            if isinstance(key, str) and _is_scalar(value) and new_value == value:
                pair_hit = _pair_finding(key, value, categories, custom_patterns, allowlist)
                if pair_hit is not None:
                    new_value = f"[REDACTED:{pair_hit}]"
                    changed = True
            # Keys can collide after redaction (two secrets, or a secret and an
            # existing marker key); never let one entry overwrite another.
            final_key, n = new_key, 2
            while final_key in out_dict:
                final_key = f"{new_key}#{n}"
                n += 1
            if final_key != new_key:
                changed = True
            out_dict[final_key] = new_value
        return out_dict, changed
    return obj, False


def _is_scalar(value: object) -> bool:
    return isinstance(value, str) or _is_number(value)


def _pair_finding(
    key: str,
    value: object,
    categories: list[str],
    custom_patterns: list[dict] | None,
    allowlist: list[str] | None,
) -> str | None:
    """Pattern name of a finding that needs ``key`` and reaches into ``value``."""
    if isinstance(value, str) and _marker_re(custom_patterns).fullmatch(value):
        return None
    value_text = value if isinstance(value, str) else _number_text(value)
    pair, value_start = _pair_text(key, value_text)
    for start, end, marker in _policy_spans(
        pair, categories=categories, custom_patterns=custom_patterns, allowlist=allowlist
    ):
        if end > value_start:
            return marker[len("[REDACTED:"):-1]
    return None


def decoded_json_text(text: str, *, strict: bool = False) -> str:
    """Every key, string and number inside a JSON or JSONL document, decoded.

    The raw serialized text hides quoted content behind escape sequences
    (``password = \"…\"``), so DLP scans run over this decoded form as
    well as the raw text. Each scalar inside an object is also emitted as a
    ``key = "value"`` line so assignment-style patterns see the field name.
    Lines that are not valid JSON contribute nothing here (the raw scan still
    covers them).

    Raises ``DlpUninspectableError`` when a document is nested too deeply to
    inspect or repeats a key, so callers can refuse it instead of treating it
    as clean. With ``strict=True`` an unparseable JSONL line is uninspectable
    too (used where a lenient reader would otherwise drop it silently).
    """
    parts: list[str] = []

    def walk(obj: object, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise DlpUninspectableError("JSON is nested too deeply to scan")
        if isinstance(obj, str):
            parts.append(obj)
        elif _is_number(obj):
            parts.append(_number_text(obj))
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)
        elif isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(key, str):
                    parts.append(key)
                    if _is_scalar(value):
                        value_text = value if isinstance(value, str) else _number_text(value)
                        parts.append(_pair_text(key, value_text)[0])
                walk(value, depth + 1)

    try:
        document = _loads_for_dlp(text)
    except json.JSONDecodeError:
        document = None
    else:
        walk(document, 0)
        return "\n".join(parts)
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            walk(_loads_for_dlp(line), 0)
        except json.JSONDecodeError as exc:
            if strict:
                raise DlpUninspectableError("unreadable JSON line") from exc
            # Still scan what it says, with string escapes undone.
            parts.append(_best_effort_unescape(line))
    return "\n".join(parts)


def _residual_spans(text: str, findings: list[DLPFinding]) -> list[tuple[int, int]]:
    """Spans in ``text`` where an original raw finding is still present."""
    spans: list[tuple[int, int]] = []
    for finding in findings:
        if not finding.match_text:
            continue
        for m in re.finditer(re.escape(finding.match_text), text):
            spans.append((m.start(), m.end()))
    return spans


Path = tuple


def _serialize(obj: object, indent: int | None) -> str:
    """The canonical serialization redaction writes back (JSONL compact,
    .json documents with indent=2): the same layout as json.dumps, with
    numbers written as their exact source text."""
    return _serialize_with_spans(obj, indent)[0]


def _serialize_with_spans(
    obj: object, indent: int | None
) -> tuple[str, list[tuple[int, int, Path]]]:
    """The redaction serializer: json.dumps layout plus the span of every key
    and scalar.

    Each span carries the path to its node: ``("key", k)`` for an object key,
    ``("val", k)`` / ``("idx", i)`` steps for values. Lets a match in the text
    be traced to the exact occurrence it covers, never to another node that
    happens to serialize the same way.
    """
    parts: list[str] = []
    spans: list[tuple[int, int, Path]] = []
    pos = 0
    item_sep = ","
    key_sep = ": " if indent is not None else ":"

    def emit(chunk: str) -> None:
        nonlocal pos
        parts.append(chunk)
        pos += len(chunk)

    def newline(level: int) -> None:
        if indent is not None:
            emit("\n" + " " * (indent * level))

    def walk(node: object, path: Path, level: int) -> None:
        if level > MAX_JSON_DEPTH:
            raise DlpUninspectableError("JSON is nested too deeply to scan")
        if isinstance(node, dict):
            if not node:
                emit("{}")
                return
            emit("{")
            for i, (key, value) in enumerate(node.items()):
                if i:
                    emit(item_sep)
                newline(level + 1)
                start = pos
                emit(json.dumps(key, ensure_ascii=False))
                spans.append((start, pos, path + (("key", key),)))
                emit(key_sep)
                walk(value, path + (("val", key),), level + 1)
            newline(level)
            emit("}")
        elif isinstance(node, list):
            if not node:
                emit("[]")
                return
            emit("[")
            for i, item in enumerate(node):
                if i:
                    emit(item_sep)
                newline(level + 1)
                walk(item, path + (("idx", i),), level + 1)
            newline(level)
            emit("]")
        else:
            start = pos
            emit(_number_text(node) if _is_number(node) else json.dumps(node, ensure_ascii=False))
            if _is_scalar(node):
                spans.append((start, pos, path))

    walk(obj, (), 0)
    return "".join(parts), spans


def _redact_paths(obj: object, targets: set, path: Path = ()) -> object:
    """Replace exactly the keys and scalars at ``targets`` with a marker."""
    if isinstance(obj, dict):
        out: dict = {}
        for key, value in obj.items():
            new_key = "[REDACTED:residual]" if path + (("key", key),) in targets else key
            new_value = _redact_paths(value, targets, path + (("val", key),))
            final_key, n = new_key, 2
            while final_key in out:
                final_key = f"{new_key}#{n}"
                n += 1
            out[final_key] = new_value
        return out
    if isinstance(obj, list):
        return [_redact_paths(item, targets, path + (("idx", i),)) for i, item in enumerate(obj)]
    if _is_scalar(obj) and path in targets:
        return "[REDACTED:residual]"
    return obj


def _redact_overlapping(
    obj: object, indent: int | None, match_spans: list[tuple[int, int]]
) -> tuple[object, str]:
    """Redact the nodes whose own tokens overlap ``match_spans``.

    ``match_spans`` are offsets in the canonical serialization of ``obj``.
    Returns the new object and its canonical serialization.
    """
    _text, node_spans = _serialize_with_spans(obj, indent)
    targets = {
        node_path
        for ns, ne, node_path in node_spans
        for ms, me in match_spans
        if ns < me and ms < ne
    }
    if targets:
        obj = _redact_paths(obj, targets)
    return obj, _serialize(obj, indent)


def _sweep_residual(
    obj: object,
    text: str,
    findings: list[DLPFinding],
    indent: int | None,
    *,
    categories: list[str],
    custom_patterns: list[dict] | None,
    allowlist: list[str] | None,
) -> tuple[object, str, bool]:
    """Make sure no original raw finding survives in ``text``.

    Value-level scanning can miss a pattern written against serialized JSON
    (e.g. a custom ``"employee_code"\\s*:\\s*"…"``). If an original finding is
    still present, the object is re-serialized canonically, matches are
    located again in that text (original spellings and the policy), and the
    exact keys/values they cover are redacted. Returns ``(obj, text, changed)``;
    the settle pass that follows re-checks the result.
    """
    if not _residual_spans(text, findings):
        return obj, text, False
    canonical = _serialize(obj, indent)
    matches = _residual_spans(canonical, findings) + [
        (s, e)
        for s, e, _m in _policy_spans(
            canonical, categories=categories, custom_patterns=custom_patterns, allowlist=allowlist
        )
    ]
    obj, canonical = _redact_overlapping(obj, indent, matches)
    return obj, canonical, True


def _settle_json(
    obj: object,
    text: str,
    indent: int | None,
    *,
    categories: list[str],
    custom_patterns: list[dict] | None,
    allowlist: list[str] | None,
    member_name: str,
) -> tuple[object, str, bool]:
    """Re-scan the redacted text with the policy until nothing matches.

    Redacting one value re-serializes the JSON, which can change whitespace or
    escaping so a pattern now matches text the earlier passes never saw. Each
    pass scans the canonical serialization and its decoded keys/strings,
    redacts exactly the nodes involved, and repeats. Raises ValueError if it
    does not settle, so a policy match is never stored while the scan reports
    it redacted.
    """
    policy = {"categories": categories, "custom_patterns": custom_patterns, "allowlist": allowlist}
    changed = False
    text = _serialize(obj, indent)
    for _ in range(_SETTLE_PASSES + 1):
        raw_spans = _policy_spans(text, **policy)
        decoded_findings = scan_dlp_full(
            mask_redaction_markers(decoded_json_text(text), custom_patterns), **policy
        )
        if not raw_spans and not decoded_findings:
            return obj, text, changed
        if decoded_findings:
            obj, _ = _redact_json_value(
                obj, **policy, known=_known_secret_spellings(decoded_findings)
            )
            text = _serialize(obj, indent)
            raw_spans = _policy_spans(text, **policy)
        if raw_spans:
            obj, text = _redact_overlapping(obj, indent, [(s, e) for s, e, _m in raw_spans])
        changed = True
    raise ValueError(
        f"DLP could not redact every finding in member {member_name} without "
        "breaking its JSON structure"
    )


def _check_repacked_size(data: bytes, member_name: str, limit: int) -> None:
    """Raise DlpMemberTooLargeError if a repacked member exceeds the cap.

    Redaction can expand a payload (a short token becomes a longer
    ``[REDACTED:...]`` marker), so the final member must be re-validated
    against the tier limit after replacement.
    """
    if len(data) > limit:
        raise DlpMemberTooLargeError(
            member_name=member_name,
            member_size=len(data),
            limit_bytes=limit,
        )


def _redact_jsonl_member(
    data: bytes,
    *,
    findings: list[DLPFinding],
    categories: list[str],
    custom_patterns: list[dict] | None,
    allowlist: list[str] | None,
    limit: int,
    member_name: str,
) -> bytes:
    """Value-level redaction for a ``.jsonl`` member (one JSON object/line).

    Unchanged lines are returned byte-identical (they are not re-serialized),
    so repacking only touches lines that actually contain a secret.
    """
    known = _known_secret_spellings(findings)
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    out: list[str] = []
    changed = False

    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        try:
            obj = _loads_for_dlp(line)
        except json.JSONDecodeError:
            # Not valid JSON — redact this line as text (never drop it, never
            # make it worse), including secrets behind any escape spelling.
            redacted = _redact_malformed_line(
                line,
                known=known,
                categories=categories,
                custom_patterns=custom_patterns,
                allowlist=allowlist,
            )
            out.append(redacted)
            changed = changed or redacted != line
            continue

        redacted_obj, line_changed = _redact_json_value(
            obj,
            categories=categories,
            custom_patterns=custom_patterns,
            allowlist=allowlist,
            known=known,
        )

        current = _serialize(redacted_obj, None) if line_changed else line
        redacted_obj, current, swept = _sweep_residual(
            redacted_obj,
            current,
            findings,
            None,
            categories=categories,
            custom_patterns=custom_patterns,
            allowlist=allowlist,
        )
        if not line_changed and not swept:
            # Byte-identical to the scanned original: the residual check above
            # already confirmed none of its original findings remain.
            out.append(line)
            continue
        # Re-serialized: whitespace/escaping changed, so re-scan until clean.
        redacted_obj, current, _settled = _settle_json(
            redacted_obj,
            current,
            None,
            categories=categories,
            custom_patterns=custom_patterns,
            allowlist=allowlist,
            member_name=member_name,
        )

        new_line = current
        # HARD INVARIANT: a line that was valid JSON stays valid JSON.
        try:
            _loads_for_dlp(new_line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Redaction produced invalid JSONL for member {member_name}: {exc}"
            ) from exc
        out.append(new_line)
        changed = True

    if not changed:
        return data

    result = ("\n".join(out)).encode("utf-8")
    _check_repacked_size(result, member_name, limit)
    return result


def _redact_json_doc_member(
    data: bytes,
    *,
    findings: list[DLPFinding],
    categories: list[str],
    custom_patterns: list[dict] | None,
    allowlist: list[str] | None,
    limit: int,
    member_name: str,
) -> bytes:
    """Value-level redaction for a ``.json`` member (a whole document)."""
    known = _known_secret_spellings(findings)
    text = data.decode("utf-8", errors="replace")
    try:
        obj = _loads_for_dlp(text)
    except json.JSONDecodeError:
        redacted = _redact_malformed_line(
            text,
            known=known,
            categories=categories,
            custom_patterns=custom_patterns,
            allowlist=allowlist,
        )
        if redacted == text:
            return data
        result = redacted.encode("utf-8")
        _check_repacked_size(result, member_name, limit)
        return result

    redacted_obj, changed = _redact_json_value(
        obj,
        categories=categories,
        custom_patterns=custom_patterns,
        allowlist=allowlist,
        known=known,
    )

    current = _serialize(redacted_obj, 2) if changed else text
    redacted_obj, current, swept = _sweep_residual(
        redacted_obj,
        current,
        findings,
        2,
        categories=categories,
        custom_patterns=custom_patterns,
        allowlist=allowlist,
    )
    if not changed and not swept:
        return data
    # Re-serialized: whitespace/escaping changed, so re-scan until clean.
    redacted_obj, current, _settled = _settle_json(
        redacted_obj,
        current,
        2,
        categories=categories,
        custom_patterns=custom_patterns,
        allowlist=allowlist,
        member_name=member_name,
    )

    new_text = current
    # HARD INVARIANT: a document that was valid JSON stays valid JSON.
    try:
        _loads_for_dlp(new_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Redaction produced invalid JSON for member {member_name}: {exc}"
        ) from exc
    result = new_text.encode("utf-8")
    _check_repacked_size(result, member_name, limit)
    return result


def redact_and_repack(
    tar_data: bytes,
    findings: list[DLPFinding],
    *,
    member_limit_bytes: int | None = None,
    categories: list[str] | None = None,
    custom_patterns: list[dict] | None = None,
    allowlist: list[str] | None = None,
) -> bytes:
    """Extract tar.gz, redact findings in messages.jsonl, repack.

    Uses the same safe extraction validation as sync/archive.py:
    rejects path traversal, absolute paths, symlinks, and oversized
    members.

    `member_limit_bytes` is the per-file cap. Callers in the sync path
    pass the tier-resolved limit from `_member_size_limit_for_tier()`
    so DLP enforcement stays in sync with the upload-time check at
    `_check_member_sizes` — before v0.9.9.8 this was a hardcoded 50 MB
    that silently nullified any `SFS_MAX_SYNC_MEMBER_BYTES_PAID` override
    above 50 MB for orgs with DLP=REDACT mode enabled.

    `categories` / `custom_patterns` / `allowlist` mirror the raw-text
    `scan_dlp` arguments and are threaded through to the value-level
    re-scan so decoded strings are scanned with the same policy. When
    omitted, they are derived from `findings` (or default to ``["secrets"]``).

    Raises `DlpMemberTooLargeError` (not ValueError) so the caller can
    distinguish a tier-cap rejection from a malformed-archive failure
    and return the structured 413 envelope.
    """
    explicit_scan = (
        categories is not None or custom_patterns is not None or allowlist is not None
    )

    if categories is None:
        categories = []
        if any(f.category == "secret" for f in findings):
            categories.append("secrets")
        if any(f.category == "phi" for f in findings):
            categories.append("phi")
        if not categories:
            categories = ["secrets"]

    # Nothing to redact: no raw findings AND the caller did not ask for an
    # explicit re-scan. A secret hidden behind JSON-escaped quotes is only
    # visible to the value-level re-scan, which a caller opts into by passing
    # categories/custom_patterns/allowlist, so with none of those set and no
    # findings the archive is returned unchanged (backward-compatible no-op).
    if not findings and not explicit_scan:
        return tar_data

    limit = (
        member_limit_bytes
        if member_limit_bytes is not None
        else DEFAULT_DLP_MEMBER_LIMIT_BYTES
    )

    # Phase 1: validate and extract all members
    members_data: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:gz") as tar:
            for member in tar.getmembers():
                # Safe extraction checks (mirrors sync/archive.py)
                if ".." in member.name:
                    raise ValueError(f"Path traversal in tar member: {member.name}")
                if member.name.startswith("/"):
                    raise ValueError(f"Absolute path in tar member: {member.name}")
                if member.issym() or member.islnk():
                    raise ValueError(f"Symlink in tar archive: {member.name}")
                if member.size > limit:
                    raise DlpMemberTooLargeError(
                        member_name=member.name,
                        member_size=member.size,
                        limit_bytes=limit,
                    )
                f = tar.extractfile(member)
                if f is not None:
                    members_data[member.name] = f.read()
    except tarfile.TarError as e:
        raise ValueError(f"Invalid tar.gz archive: {e}") from e

    # Phase 2: redact ALL .json/.jsonl files in the archive at the VALUE
    # level. Decoded strings are re-scanned with the full DLP scanner and the
    # object re-serialized, so a JSON escape sequence can never be left
    # unbalanced (the #65 bug), and secrets hidden behind escaped quotes are
    # caught too.
    for key in list(members_data.keys()):
        if key.endswith(".jsonl"):
            members_data[key] = _redact_jsonl_member(
                members_data[key],
                findings=findings,
                categories=categories,
                custom_patterns=custom_patterns,
                allowlist=allowlist,
                limit=limit,
                member_name=key,
            )
        elif key.endswith(".json"):
            members_data[key] = _redact_json_doc_member(
                members_data[key],
                findings=findings,
                categories=categories,
                custom_patterns=custom_patterns,
                allowlist=allowlist,
                limit=limit,
                member_name=key,
            )

    # Phase 3: repack
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in sorted(members_data.items()):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Org policy helpers
# ---------------------------------------------------------------------------

def get_org_dlp_policy(org: Organization) -> dict | None:
    """Extract DLP policy from org.settings JSON.

    Returns None if DLP is not enabled or no policy is configured.
    """
    try:
        settings = json.loads(org.settings) if isinstance(org.settings, str) else org.settings
    except (json.JSONDecodeError, TypeError):
        return None

    policy = settings.get("dlp")
    if not policy or not isinstance(policy, dict):
        return None

    if not policy.get("enabled", False):
        return None

    return policy


def validate_dlp_policy(policy: dict) -> dict:
    """Validate and normalize a DLP policy dict.

    Valid modes: "warn", "redact", "block".
    Valid categories: "secrets", "phi".

    Returns the normalized policy dict.
    Raises ValueError on invalid input.
    """
    if not isinstance(policy, dict):
        raise ValueError("Policy must be a dict")

    # Mode
    mode = policy.get("mode", "warn")
    if mode not in VALID_MODES:
        raise ValueError(
            f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(VALID_MODES))}"
        )

    # Categories
    categories = policy.get("categories", ["secrets"])
    if not isinstance(categories, list) or not categories:
        raise ValueError("Categories must be a non-empty list")
    for cat in categories:
        if cat not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid category '{cat}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}"
            )

    # Enabled flag
    enabled = policy.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("'enabled' must be a boolean")

    result: dict = {
        "enabled": enabled,
        "mode": mode,
        "categories": sorted(set(categories)),
    }

    # Preserve custom_patterns and allowlist if provided
    custom_patterns = policy.get("custom_patterns")
    if custom_patterns is not None:
        if not isinstance(custom_patterns, list):
            raise ValueError("custom_patterns must be a list")
        result["custom_patterns"] = custom_patterns

    allowlist = policy.get("allowlist")
    if allowlist is not None:
        if not isinstance(allowlist, list):
            raise ValueError("allowlist must be a list")
        result["allowlist"] = allowlist

    return result
