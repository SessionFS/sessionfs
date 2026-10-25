"""DLP redaction for helper tool output before it reaches the model.

Every transcript excerpt, summary, and KB text leaving for the provider goes
through :func:`redact` with the org's DLP policy (personal scope: the default
policy).  BLOCK-mode findings are redacted, not sent.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.security.secrets import scan_dlp as scan_dlp_full
from sessionfs.server.dlp import DEFAULT_DLP_POLICY, get_org_dlp_policy, redact_text
from sessionfs.server.db.models import Organization


class RedactionError(Exception):
    """DLP redaction failed (e.g. an org custom regex is invalid).

    Raised instead of letting the underlying error escape ``execute``; the
    caller fails closed and returns an ``is_error`` result with no data text.
    """


# The neutral marker label for a custom pattern that does NOT belong to the
# scope org: the pattern still redacts, but its org-private name is not leaked
# into another org's turn (N2).
_NEUTRAL_CUSTOM_LABEL = "custom"


def redact(text: str, policy: dict | None) -> str:
    """Redact DLP findings in ``text`` under ``policy`` (None = default).

    Uses the full scanner (categories + custom patterns + allowlist) so org
    policies are honoured, then replaces matches with ``[REDACTED:...]``
    markers.  Returns ``text`` unchanged when nothing matches.  Raises
    :class:`RedactionError` when the scan/redaction itself fails.
    """
    if not text:
        return text

    policy = policy or DEFAULT_DLP_POLICY
    # Secrets are ALWAYS scanned: an org policy's categories only ADD phi or
    # custom patterns, they can never turn off secret redaction.
    categories = policy.get("categories")
    if not isinstance(categories, list) or not categories:
        categories = []
    categories = ["secrets", *[c for c in categories if c != "secrets"]]
    custom_patterns = policy.get("custom_patterns")
    if not isinstance(custom_patterns, list):
        custom_patterns = None
    allowlist = policy.get("allowlist")
    if not isinstance(allowlist, list):
        allowlist = None

    try:
        findings = scan_dlp_full(
            text,
            categories=categories,
            custom_patterns=custom_patterns,
            allowlist=allowlist,
        )
    except Exception as exc:
        raise RedactionError(f"DLP scan failed: {exc}") from exc
    findings = [f for f in findings if getattr(f, "match_text", None)]
    if not findings:
        return text
    try:
        return redact_text(text, findings, custom_patterns)
    except Exception as exc:
        raise RedactionError(f"DLP redaction failed: {exc}") from exc


def union_dlp_policies(
    policies: list[dict | None],
    scope_pattern_names: set[str] | None = None,
) -> dict | None:
    """Combine several DLP policies into the strictest single policy.

    The result is the union of every policy's ``categories`` and
    ``custom_patterns`` (more findings to redact) and the *intersection* of
    their ``allowlist`` entries (fewer substrings whitelisted).  A policy with
    no ``allowlist`` contributes the empty set, so any org without an allowlist
    disables allowlisting for the turn — the safe direction.  Returns ``None``
    when no policy contributes anything, so callers fall back to the default
    (secrets-only) policy.

    Custom-pattern names are org-private: a pattern whose name is NOT in
    ``scope_pattern_names`` (the scope org's own patterns) is relabelled
    ``custom`` before it enters the union, so one org can't read another org's
    pattern names through a turn's redaction markers.  When
    ``scope_pattern_names`` is empty (no scope org), every custom pattern is
    relabelled.
    """
    categories: list[str] = []
    custom_patterns: list[dict] = []
    allowlist_sets: list[set[str]] = []
    any_policy = False
    scope_pattern_names = scope_pattern_names or set()
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        any_policy = True

        cats = policy.get("categories")
        if isinstance(cats, list):
            for category in cats:
                if isinstance(category, str) and category and category not in categories:
                    categories.append(category)

        patterns = policy.get("custom_patterns")
        if isinstance(patterns, list):
            for pattern in patterns:
                if not isinstance(pattern, dict):
                    continue
                if pattern.get("name") not in scope_pattern_names:
                    pattern = {**pattern, "name": _NEUTRAL_CUSTOM_LABEL}
                if pattern not in custom_patterns:
                    custom_patterns.append(pattern)

        allowlist = policy.get("allowlist")
        if isinstance(allowlist, list):
            allowlist_sets.append({a for a in allowlist if isinstance(a, str)})
        else:
            allowlist_sets.append(set())

    if not any_policy:
        return None

    result: dict = {"categories": categories}
    if custom_patterns:
        result["custom_patterns"] = custom_patterns
    if allowlist_sets:
        allowlist = set.intersection(*allowlist_sets)
        result["allowlist"] = sorted(allowlist)
    return result


async def org_dlp_policy(db: AsyncSession, org_id: str | None) -> dict | None:
    """Resolve the DLP policy for ``org_id`` (None → no org → None)."""
    if not org_id:
        return None
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        return None
    return get_org_dlp_policy(org)
