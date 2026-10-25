"""M10: Secret detection and DLP scanner.

Scans session content for potential secrets (API keys, passwords, private keys,
connection strings) and PHI (protected health information). Used by daemon
(warn on capture), CLI (gate export/sync), server (metadata annotation),
and DLP pipeline (block/redact before sync).

IMPORTANT: Never log or display the actual secret value. Only log the pattern
name and a masked context snippet.
"""

from __future__ import annotations

import bisect
import itertools
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("sessionfs.secrets")

# ---------------------------------------------------------------------------
# Regex patterns for common secrets
# ---------------------------------------------------------------------------

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    # -- SessionFS's OWN keys (found via the v0.15 handoff-preview DLP tests:
    # the scrubber matched AWS keys but not our own sk_sfs_ format — a user's
    # own API key sailed through every DLP gate) --
    "sessionfs_api_key": re.compile(
        r"(?P<secret>sk_sfs_[0-9a-f]{32})"
    ),
    # -- Cloud Provider Keys --
    "aws_access_key_id": re.compile(
        r"(?:^|[^A-Z0-9])(?P<secret>AKIA[0-9A-Z]{16})(?:[^A-Z0-9]|$)"
    ),
    "aws_secret_access_key": re.compile(
        r"(?:aws_secret_access_key|secret_access_key|AWS_SECRET)\s*[=:]\s*['\"]?"
        r"(?P<secret>[A-Za-z0-9/+=]{40})['\"]?"
    ),
    "gcp_service_account": re.compile(
        r'"type"\s*:\s*"service_account"'
    ),
    "azure_storage_key": re.compile(
        r"(?:AccountKey|azure_storage_key)\s*[=:]\s*['\"]?"
        r"(?P<secret>[A-Za-z0-9+/=]{86,88})['\"]?"
    ),
    # -- API Keys --
    "openai_api_key": re.compile(
        r"(?P<secret>sk-(?:proj-)?[A-Za-z0-9_-]{20,})"
    ),
    "anthropic_api_key": re.compile(
        r"(?P<secret>sk-ant-[A-Za-z0-9_-]{20,})"
    ),
    "github_token": re.compile(
        r"(?P<secret>(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,})"
    ),
    "github_fine_grained": re.compile(
        r"(?P<secret>github_pat_[A-Za-z0-9_]{22,})"
    ),
    "stripe_secret_key": re.compile(
        r"(?P<secret>(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,})"
    ),
    "slack_token": re.compile(
        r"(?P<secret>xox[bpors]-[A-Za-z0-9-]{10,})"
    ),
    "slack_webhook": re.compile(
        r"(?P<secret>https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+)"
    ),
    "sendgrid_api_key": re.compile(
        r"(?P<secret>SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43})"
    ),
    "twilio_api_key": re.compile(
        r"(?P<secret>SK[0-9a-fA-F]{32})"
    ),
    # -- Private Keys --
    "private_key_pem": re.compile(
        r"(?P<secret>-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----)"
    ),
    # -- Connection Strings --
    "database_url": re.compile(
        r"(?P<secret>(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://"
        r"[^:]+:[^@]+@[^\s'\"]+)",
        re.IGNORECASE,
    ),
    # -- Generic Patterns --
    "generic_password_assignment": re.compile(
        r"(?:password|passwd|pwd|secret|token|api_key|apikey|auth_token|access_token)"
        r"\s*[=:]\s*['\"](?P<secret>[^'\"]{8,})['\"]",
        re.IGNORECASE,
    ),
    "bearer_token": re.compile(
        r"(?:Authorization|Bearer)\s*[=:]\s*['\"]?Bearer\s+(?P<secret>[A-Za-z0-9._~+/=-]{20,})",
        re.IGNORECASE,
    ),
    "base64_high_entropy": re.compile(
        r"(?:key|secret|token|password|credential)\s*[=:]\s*['\"]?"
        r"(?P<secret>[A-Za-z0-9+/]{40,}={0,2})['\"]?",
        re.IGNORECASE,
    ),
    # -- JWT --
    "jwt_token": re.compile(
        r"(?P<secret>eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
    ),
}

# Allowlist: patterns that should NOT be flagged as secrets
ALLOWLIST: list[re.Pattern[str]] = [
    # NOTE deliberately REMOVED (v0.15): sk_sfs_ was blanket-allowlisted as
    # "our own API keys", which meant a user's REAL SessionFS key passed every
    # DLP gate (session push scans, handoff previews, public share pages).
    # Docs placeholders are still covered by the dummy-value filters below.
    re.compile(r"password.*changeme", re.IGNORECASE),
    re.compile(r"password.*example", re.IGNORECASE),
    re.compile(r"password.*placeholder", re.IGNORECASE),
    re.compile(r"YOUR_.*_HERE"),
    re.compile(r"<YOUR_.*>"),
    re.compile(r"xxx+", re.IGNORECASE),
]

SEVERITY_MAP: dict[str, str] = {
    "aws_access_key_id": "critical",
    "aws_secret_access_key": "critical",
    "gcp_service_account": "critical",
    "private_key_pem": "critical",
    "openai_api_key": "high",
    "anthropic_api_key": "high",
    "github_token": "high",
    "stripe_secret_key": "high",
    "database_url": "high",
    "generic_password_assignment": "medium",
    "bearer_token": "medium",
    "jwt_token": "medium",
    "slack_webhook": "medium",
}

# ---------------------------------------------------------------------------
# PHI (Protected Health Information) patterns — toggle independently of secrets
# ---------------------------------------------------------------------------

PHI_PATTERNS: dict[str, tuple[re.Pattern[str], str]] = {
    # (pattern_name -> (compiled regex, severity))
    "ssn": (
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "critical",
    ),
    "mrn": (
        re.compile(r"\b(?:MRN|mrn)[:\s#]*\d{4,12}\b"),
        "critical",
    ),
    "dob": (
        re.compile(
            r"\b(?:DOB|dob|date\s*of\s*birth)[:\s]*"
            r"(?:0?[1-9]|1[0-2])[/\-](?:0?[1-9]|[12]\d|3[01])[/\-](?:\d{2}|\d{4})\b"
        ),
        "high",
    ),
    "npi_dea_license": (
        re.compile(r"\b(?:DEA|NPI|license)\s*(?:#|number|:)\s*[\w-]{6,15}\b"),
        "high",
    ),
    "health_plan_id": (
        re.compile(
            r"\b(?:health\s*plan|member)\s*(?:id|#|number)[:\s]*[\w-]{6,20}\b",
            re.IGNORECASE,
        ),
        "high",
    ),
    "patient_name": (
        re.compile(r"\b(?:patient|pt)\s*(?:name|:)\s*[A-Z][a-z]+\s+[A-Z][a-z]+\b", re.IGNORECASE),
        "high",
    ),
    "patient_phone": (
        re.compile(
            r"\b(?:patient|emergency)\s*(?:phone|contact|tel)[:\s]*[\d\s\-\(\)]{10,}\b"
        ),
        "medium",
    ),
    "fax_number": (
        re.compile(r"\b(?:fax)[:\s]*[\d\s\-\(\)]{10,}\b"),
        "medium",
    ),
    "vin": (
        re.compile(r"\bVIN[:\s]*[A-HJ-NPR-Z0-9]{17}\b"),
        "medium",
    ),
    "device_id": (
        re.compile(r"\b(?:device|serial)\s*(?:id|#|number)[:\s]*[\w-]{6,20}\b"),
        "medium",
    ),
    "biometric_id": (
        re.compile(r"\b(?:biometric|fingerprint|retina)\s*(?:id|#|scan)[:\s]*[\w-]+\b"),
        "high",
    ),
    "phi_url": (
        re.compile(r"https?://[^\s]*\b(?:patient|mrn|ssn|dob)\b[^\s]*"),
        "critical",
    ),
    "medical_email": (
        re.compile(
            r"\b(?:patient|medical)\s*(?:email|e-mail)[:\s]*[\w.+-]+@[\w-]+\.[\w.-]+\b"
        ),
        "medium",
    ),
    "medical_age_90_plus": (
        re.compile(r"\b(?:age|aged?)\s*[:\s]*(?:[89]\d|[1-9]\d{2,})\b"),
        "low",
    ),
}


# ---------------------------------------------------------------------------
# Finding dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DLPFinding:
    """A detected DLP violation (secret or PHI)."""

    pattern_name: str       # e.g. "AWS_ACCESS_KEY", "SSN"
    category: str           # "secret" or "phi"
    severity: str           # "critical", "high", "medium", "low"
    line_number: int
    match_text: str         # The matched text (for redaction)
    context: str            # ~50 chars surrounding match
    message_index: int = 0


@dataclass
class SecretFinding:
    """A detected potential secret."""
    pattern_name: str
    line_number: int
    context: str  # Masked context (secret value replaced)
    severity: str


# ---------------------------------------------------------------------------
# Scanning functions
# ---------------------------------------------------------------------------

def _is_allowlisted(text: str) -> bool:
    """Check if the matched text is a known false positive."""
    return any(pattern.search(text) for pattern in ALLOWLIST)


def _mask_secret(text: str, match: re.Match[str]) -> str:
    """Replace the secret value with a masked version."""
    try:
        secret = match.group("secret")
    except IndexError:
        secret = match.group(0)
    if len(secret) <= 8:
        masked = "****"
    else:
        masked = secret[:4] + "****" + secret[-4:]
    return text[:match.start()] + masked + text[match.end():]


def scan_text(text: str, line_number: int = 0) -> list[SecretFinding]:
    """Scan a text string for potential secrets."""
    findings: list[SecretFinding] = []

    for pattern_name, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            matched_text = match.group(0)

            if _is_allowlisted(matched_text):
                continue

            # Build masked context (40 chars before/after)
            start = max(0, match.start() - 40)
            end = min(len(text), match.end() + 40)
            context_raw = text[start:end]
            context_match = pattern.search(context_raw)
            if context_match:
                masked_context = _mask_secret(context_raw, context_match)
            else:
                masked_context = context_raw

            findings.append(SecretFinding(
                pattern_name=pattern_name,
                line_number=line_number,
                context=masked_context,
                severity=SEVERITY_MAP.get(pattern_name, "medium"),
            ))

    return findings


def scan_messages_jsonl(messages_path: Path) -> list[SecretFinding]:
    """Scan a messages.jsonl file for potential secrets."""
    findings: list[SecretFinding] = []
    if not messages_path.is_file():
        return findings

    with open(messages_path, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            findings.extend(scan_text(line, line_number=line_number))

    return findings


def scan_session_dir(session_dir: Path) -> list[SecretFinding]:
    """Scan an entire .sfs session directory for secrets."""
    findings: list[SecretFinding] = []

    messages = session_dir / "messages.jsonl"
    if messages.is_file():
        findings.extend(scan_messages_jsonl(messages))

    workspace = session_dir / "workspace.json"
    if workspace.is_file():
        findings.extend(scan_text(workspace.read_text(), line_number=0))

    return findings


def summarize_findings(findings: list[SecretFinding]) -> dict[str, int]:
    """Summarize findings by pattern name."""
    summary: dict[str, int] = {}
    for f in findings:
        summary[f.pattern_name] = summary.get(f.pattern_name, 0) + 1
    return summary


# ---------------------------------------------------------------------------
# DLP scanner — unified secrets + PHI scanning
# ---------------------------------------------------------------------------

def _is_dlp_allowlisted(text: str, extra_allowlist: list[str] | None = None) -> bool:
    """Check if text matches the built-in allowlist or a caller-supplied list."""
    if _is_allowlisted(text):
        return True
    if extra_allowlist:
        for entry in extra_allowlist:
            if entry in text:
                return True
    return False


def _build_context(text: str, match: re.Match[str], width: int = 25) -> str:
    """Return ~50 chars of context around a match, masking the matched value."""
    start = max(0, match.start() - width)
    end = min(len(text), match.end() + width)
    snippet = text[start:end]
    # Mask the matched portion within the snippet
    m_start = match.start() - start
    m_end = match.end() - start
    matched = snippet[m_start:m_end]
    if len(matched) <= 8:
        masked = "****"
    else:
        masked = matched[:4] + "****" + matched[-4:]
    return snippet[:m_start] + masked + snippet[m_end:]


# ---------------------------------------------------------------------------
# Scan planning (performance only: never changes what a scan finds)
# ---------------------------------------------------------------------------
#
# Trying every pattern at every position of every line made large DLP scans
# slow (GH #71): most built-in patterns start with a case-insensitive
# alternation or a ``\b``, which the regex engine can't skip ahead for. Two
# exact shortcuts:
#
# 1. NEEDLES: a pattern can only match a line containing one of its needles
#    (a literal every match includes), so other lines skip that pattern.
# 2. ANCHORS: every match starts at a fixed offset from an occurrence of one
#    of the pattern's anchors (its leading literal alternatives), so the
#    pattern is tried at those positions only, in order and resuming after
#    each match exactly as finditer does.
#
# The unchanged pattern still decides every match, so findings, their order
# and their line numbers are identical to a full scan.
#
# Folded literals are lowercase and looked up in the lowercased text (for
# IGNORECASE patterns). Under IGNORECASE a few non-ASCII characters also
# match ASCII letters (``_CASE_TRAPS``: dotted capital I, dotless i, long s,
# Kelvin sign) and U+0130 grows when lowercased, so a text containing any of
# them gets no folded shortcuts. tests/unit/test_dlp_scan_speed.py checks
# these facts against the regex engine and compares results with a full
# scan.


@dataclass(frozen=True)
class _Rule:
    needles: tuple[str, ...]
    needles_folded: bool = False
    # (literal, offset of the match start from the literal); None = no anchors
    anchors: tuple[tuple[str, int], ...] | None = None
    anchors_folded: bool = False


def _starts_with(*words: str) -> tuple[tuple[str, int], ...]:
    return tuple((word, 0) for word in words)


_SCAN_RULES: dict[str, _Rule] = {
    # -- secrets --
    "sessionfs_api_key": _Rule(("sk_sfs_",)),
    "aws_access_key_id": _Rule(("AKIA",)),
    "aws_secret_access_key": _Rule(("secret_access_key", "AWS_SECRET")),
    "gcp_service_account": _Rule(('"service_account"',)),
    "azure_storage_key": _Rule(("AccountKey", "azure_storage_key")),
    "openai_api_key": _Rule(("sk-",)),
    "anthropic_api_key": _Rule(("sk-ant-",)),
    "github_token": _Rule(("ghp_", "gho_", "ghu_", "ghs_", "ghr_")),
    "github_fine_grained": _Rule(("github_pat_",)),
    "stripe_secret_key": _Rule(("_live_", "_test_")),
    "slack_token": _Rule(("xox",)),
    "slack_webhook": _Rule(("https://hooks.slack.com/services/T",)),
    "sendgrid_api_key": _Rule(("SG.",)),
    "twilio_api_key": _Rule(("SK",)),
    "private_key_pem": _Rule(("-----BEGIN ",)),
    # IGNORECASE; "://" has no cased characters.
    "database_url": _Rule(
        ("://",),
        anchors=_starts_with("postgres", "mysql", "mongodb", "redis", "amqp"),
        anchors_folded=True,
    ),
    "generic_password_assignment": _Rule(
        ("password", "passwd", "pwd", "secret", "token", "api_key", "apikey"),
        needles_folded=True,
        anchors=_starts_with(
            "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
            "auth_token", "access_token",
        ),
        anchors_folded=True,
    ),
    "bearer_token": _Rule(
        ("bearer",),
        needles_folded=True,
        anchors=_starts_with("authorization", "bearer"),
        anchors_folded=True,
    ),
    "base64_high_entropy": _Rule(
        ("key", "secret", "token", "password", "credential"),
        needles_folded=True,
        anchors=_starts_with("key", "secret", "token", "password", "credential"),
        anchors_folded=True,
    ),
    "jwt_token": _Rule(("eyJ",)),
    # -- PHI --
    # \d{3}-\d{2}-\d{4}: the first "-" is always 3 characters into a match.
    "ssn": _Rule(("-",), anchors=(("-", -3),)),
    "mrn": _Rule(("MRN", "mrn"), anchors=_starts_with("MRN", "mrn")),
    "dob": _Rule(("DOB", "dob", "date"), anchors=_starts_with("DOB", "dob", "date")),
    "npi_dea_license": _Rule(
        ("DEA", "NPI", "license"), anchors=_starts_with("DEA", "NPI", "license")
    ),
    "health_plan_id": _Rule(
        ("health", "member"),
        needles_folded=True,
        anchors=_starts_with("health", "member"),
        anchors_folded=True,
    ),
    "patient_name": _Rule(
        ("patient", "pt"),
        needles_folded=True,
        anchors=_starts_with("patient", "pt"),
        anchors_folded=True,
    ),
    "patient_phone": _Rule(
        ("patient", "emergency"), anchors=_starts_with("patient", "emergency")
    ),
    "fax_number": _Rule(("fax",), anchors=_starts_with("fax")),
    "vin": _Rule(("VIN",), anchors=_starts_with("VIN")),
    "device_id": _Rule(("device", "serial"), anchors=_starts_with("device", "serial")),
    "biometric_id": _Rule(
        ("biometric", "fingerprint", "retina"),
        anchors=_starts_with("biometric", "fingerprint", "retina"),
    ),
    "phi_url": _Rule(("http",), anchors=_starts_with("http")),
    "medical_email": _Rule(
        ("patient", "medical"), anchors=_starts_with("patient", "medical")
    ),
    "medical_age_90_plus": _Rule(("age",), anchors=_starts_with("age")),
}

_CASE_TRAPS = ("\u0130", "\u0131", "\u017f", "\u212a")


def line_starts(text: str) -> list[int]:
    """Offset in ``text`` where each line of ``text.splitlines()`` starts."""
    starts = list(itertools.accumulate(map(len, text.splitlines(True)), initial=0))
    starts.pop()
    return starts


def _needle_lines(haystack: str, needles: tuple[str, ...], starts: list[int]) -> set[int]:
    """Indexes (0-based, splitlines order) of lines containing a needle.

    Needles contain no line-break characters, so an occurrence never spans
    two lines.
    """
    found: set[int] = set()
    n_starts = len(starts)
    for needle in needles:
        pos = haystack.find(needle)
        while pos != -1:
            line = bisect.bisect_right(starts, pos) - 1
            found.add(line)
            # One hit selects the line; continue on the next one.
            nxt = starts[line + 1] if line + 1 < n_starts else len(haystack)
            pos = haystack.find(needle, max(nxt, pos + 1))
    return found


def _anchor_positions(
    haystack: str, anchors: tuple[tuple[str, int], ...], starts: list[int]
) -> dict[int, list[int]]:
    """Per line index, the in-line positions where a match could start.

    Anchors contain no line-break characters, so an occurrence (and a match
    starting at its offset) stays within one line.
    """
    found: dict[int, list[int]] = {}
    for literal, offset in anchors:
        at = haystack.find(literal)
        while at != -1:
            line = bisect.bisect_right(starts, at) - 1
            rel = at - starts[line] + offset
            if rel >= 0:
                found.setdefault(line, []).append(rel)
            at = haystack.find(literal, at + 1)
    return found


# (name, pattern, candidate lines or None for every line, per-line anchor
# positions or None to try every position)
_PlanEntry = tuple[str, re.Pattern[str], "set[int] | None", "dict[int, list[int]] | None"]


class _ScanPlan:
    """Which patterns to try on which lines (and where) for one text."""

    def __init__(self, text: str, lines: list[str]) -> None:
        self._text = text
        self._lines = lines
        self._starts = line_starts(text)
        self._usable = len(self._starts) == len(lines)
        self._lowered: str | None = None
        if self._usable and not any(trap in text for trap in _CASE_TRAPS):
            lowered = text.lower()
            if len(lowered) == len(text):
                self._lowered = lowered
        # Union of candidate lines of every planned pattern; None = all lines.
        self.line_ids: set[int] | None = set()

    def _haystack(self, folded: bool) -> str | None:
        return self._lowered if folded else self._text

    def plan(self, patterns: Iterable[tuple[str, re.Pattern[str]]]) -> list[_PlanEntry]:
        """Entries for the patterns that can match somewhere, in order."""
        entries: list[_PlanEntry] = []
        for name, pattern in patterns:
            rule = _SCAN_RULES.get(name) if self._usable else None
            candidates: set[int] | None = None
            positions: dict[int, list[int]] | None = None
            if rule is not None:
                anchor_text = self._haystack(rule.anchors_folded)
                needle_text = self._haystack(rule.needles_folded)
                if rule.anchors is not None and anchor_text is not None:
                    positions = _anchor_positions(anchor_text, rule.anchors, self._starts)
                    candidates = set(positions)
                elif needle_text is not None:
                    candidates = _needle_lines(needle_text, rule.needles, self._starts)
                if candidates is not None and not candidates:
                    continue
            if candidates is None:
                self.line_ids = None
            elif self.line_ids is not None:
                self.line_ids |= candidates
            entries.append((name, pattern, candidates, positions))
        return entries

    def order(self, every_line: bool) -> Iterable[int]:
        """Line indexes to visit, ascending."""
        if every_line or self.line_ids is None:
            return range(len(self._lines))
        return sorted(self.line_ids)

    def matches(self, index: int, entry: _PlanEntry) -> Iterator[re.Match[str]]:
        """Exactly ``pattern.finditer(line)`` for line ``index`` (0-based)."""
        _name, pattern, _candidates, positions = entry
        line = self._lines[index]
        if positions is None:
            yield from pattern.finditer(line)
            return
        resume = 0
        for at in sorted(set(positions.get(index, ()))):
            if at < resume:
                continue
            match = pattern.match(line, at)
            if match is not None:
                yield match
                # Anchored matches are never empty, so finditer resumes at
                # the match end.
                resume = match.end()


def _phi_items() -> Iterator[tuple[str, re.Pattern[str]]]:
    for name, (pattern, _severity) in PHI_PATTERNS.items():
        yield name, pattern


def scan_dlp(
    text: str,
    categories: list[str] | None = None,
    custom_patterns: list[dict] | None = None,
    allowlist: list[str] | None = None,
) -> list[DLPFinding]:
    """Scan *text* for secrets and/or PHI, returning unified DLPFinding list.

    Parameters
    ----------
    text:
        Multi-line text to scan (e.g. full messages.jsonl content).
    categories:
        Which pattern sets to enable. Accepts ``["secrets"]``,
        ``["phi"]``, or ``["secrets", "phi"]``.  Defaults to both.
    custom_patterns:
        Optional extra patterns from org config. Each dict must contain
        ``name`` (str), ``regex`` (str), ``category`` (str), and
        ``severity`` (str).
    allowlist:
        Extra literal strings to skip (on top of built-in ALLOWLIST).
    """
    if categories is None:
        categories = ["secrets", "phi"]

    findings: list[DLPFinding] = []
    lines = text.splitlines()

    # Pre-compile custom patterns once.  A stored pattern with a bad regex is
    # skipped (logged), never fatal: an org policy saved before stricter
    # validation must not take down every subsequent scan.  (Never log the
    # regex itself — it may be a near-secret literal.)
    compiled_custom: list[tuple[str, re.Pattern[str], str, str]] = []
    if custom_patterns:
        malformed = 0
        for cp in custom_patterns:
            # A stored policy can contain non-dict entries or a non-string
            # regex (re.compile would raise TypeError on those). Skip them and
            # log once, never take down the whole scan.
            if not isinstance(cp, dict):
                malformed += 1
                continue
            name = cp.get("name")
            regex = cp.get("regex")
            if not isinstance(regex, str) or not isinstance(name, str) or not name:
                malformed += 1
                continue
            category = cp.get("category") or "secret"
            severity = cp.get("severity") or "high"
            try:
                compiled_custom.append((name, re.compile(regex), category, severity))
            except re.error as exc:
                logger.warning(
                    "Skipping invalid custom DLP pattern %r: %s", name, exc
                )
        if malformed:
            logger.warning(
                "Skipping %d malformed custom DLP pattern(s) (non-dict entry, missing name or non-string regex)",
                malformed,
            )

    plan = _ScanPlan(text, lines)
    secret_entries = plan.plan(SECRET_PATTERNS.items()) if "secrets" in categories else []
    phi_entries = plan.plan(_phi_items()) if "phi" in categories else []

    # Custom patterns see every line.
    for line_idx0 in plan.order(every_line=bool(compiled_custom)):
        line = lines[line_idx0]
        line_idx = line_idx0 + 1
        # --- Secret patterns ---
        for entry in secret_entries:
            candidates = entry[2]
            if candidates is not None and line_idx0 not in candidates:
                continue
            pattern_name = entry[0]
            for match in plan.matches(line_idx0, entry):
                matched_text = match.group(0)
                if _is_dlp_allowlisted(matched_text, allowlist):
                    continue
                findings.append(DLPFinding(
                    pattern_name=pattern_name,
                    category="secret",
                    severity=SEVERITY_MAP.get(pattern_name, "medium"),
                    line_number=line_idx,
                    match_text=matched_text,
                    context=_build_context(line, match),
                ))

        # --- PHI patterns ---
        for entry in phi_entries:
            candidates = entry[2]
            if candidates is not None and line_idx0 not in candidates:
                continue
            pattern_name = entry[0]
            severity = PHI_PATTERNS[pattern_name][1]
            for match in plan.matches(line_idx0, entry):
                matched_text = match.group(0)
                if _is_dlp_allowlisted(matched_text, allowlist):
                    continue
                findings.append(DLPFinding(
                    pattern_name=pattern_name,
                    category="phi",
                    severity=severity,
                    line_number=line_idx,
                    match_text=matched_text,
                    context=_build_context(line, match),
                ))

        # --- Custom patterns ---
        for cp_name, cp_regex, cp_category, cp_severity in compiled_custom:
            for match in cp_regex.finditer(line):
                matched_text = match.group(0)
                if _is_dlp_allowlisted(matched_text, allowlist):
                    continue
                findings.append(DLPFinding(
                    pattern_name=cp_name,
                    category=cp_category,
                    severity=cp_severity,
                    line_number=line_idx,
                    match_text=matched_text,
                    context=_build_context(line, match),
                ))

    return findings


def scan_dlp_builtin(
    text: str,
    categories: list[str] | None = None,
) -> list[DLPFinding]:
    """Scan *text* with the built-in secret/PHI rules only (no org policy).

    This is the server's historical default scanner (``server.dlp.scan_dlp``),
    kept here so client code - the local skill-publish validator - runs the
    exact same rules without importing the server package. Unlike
    :func:`scan_dlp`, the built-in allowlist applies to secret matches only and
    the context is the raw 25-character window around the match.
    """
    if categories is None:
        categories = ["secrets"]

    findings: list[DLPFinding] = []
    lines = text.splitlines()

    plan = _ScanPlan(text, lines)
    secret_entries = plan.plan(SECRET_PATTERNS.items()) if "secrets" in categories else []
    phi_entries = plan.plan(_phi_items()) if "phi" in categories else []

    for line_idx0 in plan.order(every_line=False):
        line = lines[line_idx0]
        line_idx = line_idx0 + 1
        for entry in secret_entries:
            candidates = entry[2]
            if candidates is not None and line_idx0 not in candidates:
                continue
            pattern_name = entry[0]
            for match in plan.matches(line_idx0, entry):
                matched_text = match.group(0)
                if _is_allowlisted(matched_text):
                    continue
                start = max(0, match.start() - 25)
                end = min(len(line), match.end() + 25)
                findings.append(DLPFinding(
                    pattern_name=pattern_name,
                    category="secret",
                    severity=SEVERITY_MAP.get(pattern_name, "medium"),
                    line_number=line_idx,
                    match_text=matched_text,
                    context=line[start:end],
                ))

        for entry in phi_entries:
            candidates = entry[2]
            if candidates is not None and line_idx0 not in candidates:
                continue
            pattern_name = entry[0]
            severity = PHI_PATTERNS[pattern_name][1]
            for match in plan.matches(line_idx0, entry):
                matched_text = match.group(0)
                start = max(0, match.start() - 25)
                end = min(len(line), match.end() + 25)
                findings.append(DLPFinding(
                    pattern_name=pattern_name,
                    category="phi",
                    severity=severity,
                    line_number=line_idx,
                    match_text=matched_text,
                    context=line[start:end],
                ))

    return findings
