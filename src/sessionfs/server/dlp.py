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


def redact_and_repack(
    tar_data: bytes,
    findings: list[DLPFinding],
    *,
    member_limit_bytes: int | None = None,
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

    Raises `DlpMemberTooLargeError` (not ValueError) so the caller can
    distinguish a tier-cap rejection from a malformed-archive failure
    and return the structured 413 envelope.
    """
    if not findings:
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

    # Phase 2: redact ALL .json/.jsonl files in the archive
    for key in list(members_data.keys()):
        if key.endswith(".json") or key.endswith(".jsonl"):
            original_text = members_data[key].decode("utf-8", errors="replace")
            redacted_text = redact_text(original_text, findings)
            redacted_bytes = redacted_text.encode("utf-8")
            # Redaction can expand the payload (e.g. replacing a short token
            # with a longer "[REDACTED:...]" marker). Re-validate the final
            # member size after replacement so a member that started under the
            # cap cannot slip past the tier limit once repacked.
            if len(redacted_bytes) > limit:
                raise DlpMemberTooLargeError(
                    member_name=key,
                    member_size=len(redacted_bytes),
                    limit_bytes=limit,
                )
            members_data[key] = redacted_bytes

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
