"""Server-side DLP (Data Loss Prevention) module.

Provides text redaction, tar.gz repack with redacted content, org policy
extraction, and policy validation. Works with DLPFinding objects from the
security.secrets scanner.
"""

from __future__ import annotations

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
    SEVERITY_MAP,
    ALLOWLIST,
)

logger = logging.getLogger("sessionfs.api")

VALID_MODES = {"warn", "redact", "block"}
VALID_CATEGORIES = {"secrets", "phi"}

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
    """
    if categories is None:
        categories = ["secrets"]

    findings: list[DLPFinding] = []
    lines = text.splitlines()

    for line_idx, line in enumerate(lines, start=1):
        if "secrets" in categories:
            for pattern_name, pattern in SECRET_PATTERNS.items():
                for match in pattern.finditer(line):
                    matched_text = match.group(0)
                    if _is_allowlisted(matched_text):
                        continue
                    # Build masked context
                    start = max(0, match.start() - 25)
                    end = min(len(line), match.end() + 25)
                    context = line[start:end]
                    findings.append(DLPFinding(
                        pattern_name=pattern_name,
                        category="secret",
                        severity=SEVERITY_MAP.get(pattern_name, "medium"),
                        line_number=line_idx,
                        match_text=matched_text,
                        context=context,
                    ))

        if "phi" in categories:
            for pattern_name, (pattern, severity) in PHI_PATTERNS.items():
                for match in pattern.finditer(line):
                    matched_text = match.group(0)
                    start = max(0, match.start() - 25)
                    end = min(len(line), match.end() + 25)
                    context = line[start:end]
                    findings.append(DLPFinding(
                        pattern_name=pattern_name,
                        category="phi",
                        severity=severity,
                        line_number=line_idx,
                        match_text=matched_text,
                        context=context,
                    ))

    return findings


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def redact_text(text: str, findings: list[DLPFinding]) -> str:
    """Replace each finding's match_text with ``[REDACTED:{pattern_name}]``.

    Processes findings in reverse order of position so that earlier
    replacements don't shift the indices of later ones.
    """
    if not findings:
        return text

    # Build (start, end, replacement) tuples from the original text
    replacements: list[tuple[int, int, str]] = []
    for finding in findings:
        replacement = f"[REDACTED:{finding.pattern_name}]"
        # Find all occurrences of match_text and replace them
        pattern = re.escape(finding.match_text)
        for match in re.finditer(pattern, text):
            replacements.append((match.start(), match.end(), replacement))

    # Deduplicate overlapping ranges — keep the longest match
    replacements.sort(key=lambda r: (r[0], -(r[1] - r[0])))
    merged: list[tuple[int, int, str]] = []
    for start, end, repl in replacements:
        if merged and start < merged[-1][1]:
            # Overlapping — skip the shorter one
            continue
        merged.append((start, end, repl))

    # Apply in reverse order to preserve positions
    result = text
    for start, end, repl in reversed(merged):
        result = result[:start] + repl + result[end:]

    return result


def redact_and_repack(tar_data: bytes, findings: list[DLPFinding]) -> bytes:
    """Extract tar.gz, redact findings in messages.jsonl, repack.

    Uses the same safe extraction validation as sync/archive.py:
    rejects path traversal, absolute paths, symlinks, and oversized members.
    """
    if not findings:
        return tar_data

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
                if member.size > 50 * 1024 * 1024:
                    raise ValueError(
                        f"Member too large: {member.name} ({member.size} bytes)"
                    )
                f = tar.extractfile(member)
                if f is not None:
                    members_data[member.name] = f.read()
    except tarfile.TarError as e:
        raise ValueError(f"Invalid tar.gz archive: {e}") from e

    # Phase 2: redact ALL .json/.jsonl files in the archive
    for key in list(members_data.keys()):
        if key.endswith(".json") or key.endswith(".jsonl"):
            original_text = members_data[key].decode("utf-8", errors="replace")
            redacted_text = redact_text(original_text, findings)
            members_data[key] = redacted_text.encode("utf-8")

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
