"""DNS TXT domain verification shared by SSO and marketplace publishers.

Extracted from routes/sso_admin.py (whose ``_resolve_txt`` now delegates
here) so publisher verification can reuse the exact proof without the
``oidc_sso`` tier gate. The record format is unchanged:
``sessionfs-verification=<token>`` at the domain apex.

Resolution failures (NXDOMAIN, no TXT, timeout) return ``[]`` and therefore
fail closed. Tests replace :func:`resolve_txt` on this module.
"""

from __future__ import annotations

import logging
import re

from sessionfs.server.services.sso_domains import FREE_EMAIL_DENYLIST

logger = logging.getLogger("sessionfs.api")

DOMAIN_VERIFICATION_PREFIX = "sessionfs-verification="

# Lowercase hostname: labels of letters, digits and hyphens, a dot-separated
# TLD of 2+ letters, total <= 253. No trailing dot, no IP literals.
_DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def canonical_domain(value: str) -> str:
    """THE canonical spelling of a domain, used for storage, uniqueness,
    lock keys and every comparison (SSO and publisher verification alike):
    trimmed, lowercased, without a trailing root dot, and a strict hostname.
    Raises ValueError with a user message."""
    domain = (value or "").strip().lower().rstrip(".")
    if "@" in domain:
        raise ValueError("domain must not contain '@'; provide just the domain part")
    if not _DOMAIN_RE.fullmatch(domain):
        raise ValueError("domain must be a valid hostname such as example.com")
    return domain


def normalize_domain(value: str) -> str:
    """canonical_domain() plus the consumer-email denylist (publisher
    verification); raises ValueError with a user message."""
    domain = canonical_domain(value)
    if domain in FREE_EMAIL_DENYLIST:
        raise ValueError(
            f"'{domain}' is a consumer email provider and cannot be verified as an "
            "organizational domain."
        )
    return domain


def txt_record(token: str) -> str:
    return f"{DOMAIN_VERIFICATION_PREFIX}{token}"


def records_contain_token(records: list[str], token: str) -> bool:
    """Exact match of one TXT string (the SSO check used a substring; an
    exact match is stricter and still accepts the documented record)."""
    expected = txt_record(token)
    return any(record.strip() == expected for record in records)


async def resolve_txt(domain: str) -> list[str]:
    """Resolve DNS TXT records for *domain* (async, via dnspython).

    Returns the decoded TXT strings, or [] on any resolution failure."""
    try:
        import dns.asyncresolver
    except ImportError:  # pragma: no cover - dnspython is a declared dep
        logger.warning("dnspython not installed; DNS TXT resolution unavailable for %s", domain)
        return []

    try:
        answers = await dns.asyncresolver.resolve(domain, "TXT")
    except Exception as exc:
        logger.info("DNS TXT resolution failed for %s: %s", domain, exc)
        return []

    records: list[str] = []
    for rdata in answers:
        try:
            records.append("".join(chunk.decode("utf-8", "ignore") for chunk in rdata.strings))
        except Exception:  # pragma: no cover - defensive
            continue
    return records
