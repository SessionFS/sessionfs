"""M10: Secret detection and DLP scanner.

Scans session content for potential secrets (API keys, passwords, private keys,
connection strings) and PHI (protected health information). Used by daemon
(warn on capture), CLI (gate export/sync), server (metadata annotation),
and DLP pipeline (block/redact before sync).

IMPORTANT: Never log or display the actual secret value. Only log the pattern
name and a masked context snippet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Regex patterns for common secrets
# ---------------------------------------------------------------------------

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
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
    re.compile(r"sk_sfs_"),                              # Our own API keys
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

    # Pre-compile custom patterns once
    compiled_custom: list[tuple[str, re.Pattern[str], str, str]] = []
    if custom_patterns:
        for cp in custom_patterns:
            compiled_custom.append((
                cp["name"],
                re.compile(cp["regex"]),
                cp["category"],
                cp["severity"],
            ))

    for line_idx, line in enumerate(lines, start=1):
        # --- Secret patterns ---
        if "secrets" in categories:
            for pattern_name, pattern in SECRET_PATTERNS.items():
                for match in pattern.finditer(line):
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
        if "phi" in categories:
            for pattern_name, (pattern, severity) in PHI_PATTERNS.items():
                for match in pattern.finditer(line):
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
