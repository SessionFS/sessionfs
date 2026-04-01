"""License key generation for self-hosted deployments."""

from __future__ import annotations

import secrets

_PREFIXES = {
    "trial": "sfs_trial_",
    "paid": "sfs_helm_",
    "internal": "sfs_int_",
}


def generate_license_key(license_type: str = "paid") -> str:
    """Generate a prefixed license key with 24 hex characters of entropy."""
    prefix = _PREFIXES.get(license_type, "sfs_helm_")
    return f"{prefix}{secrets.token_hex(12)}"
