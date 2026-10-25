"""Fernet encryption for stored secrets.

Single source of truth for the Fernet key derivation used by the judge-settings
routes, the org-wide helper/LLM keys, the summary / audit routes and the
GitLab/Jira integrations.

Key derivation (v0.23.0 Shield LOW): the encryption key is now derived from
``SFS_VERIFICATION_SECRET`` with HKDF-SHA256 under the domain-separation label
``sessionfs-llm-keys-v1`` instead of a bare ``sha256`` of the secret.  Decrypt
still tries the HKDF key first and then falls back to the legacy sha256-derived
key, so existing ``UserJudgeSettings`` / ``GitLabSettings`` / ``OrgLlmSettings``
ciphertexts stay readable.  Encryption always writes the new key, and the wire
format is unchanged: a valid Fernet token.

Outside development/test, an unset or known-default secret
(``dev-secret``, ``change-me``, …) raises ``EncryptionUnavailableError`` rather
than silently encrypting real secrets under a public placeholder key; routes
surface that as a 503.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Domain-separation label for the HKDF-derived encryption key.  Distinct from
# the legacy sha256 derivation so a rotated secret can never let an old key
# decrypt a new token (or vice versa) by accident.
_HKDF_INFO = b"sessionfs-llm-keys-v1"

# Known placeholder secrets that must never protect real data outside a
# development/test environment.  Mirrors routes/auth.py's default set, plus
# the historical ``llm_keys`` default.
_KNOWN_DEFAULT_SECRETS = frozenset(
    {"dev-secret", "dev-verification-secret", "change-me"}
)

_DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "dev", "test", "testing", "local"})

# Fallback when ``SFS_VERIFICATION_SECRET`` is unset in a dev/test environment.
# This preserves the historical behaviour (the old default was ``dev-secret``).
_DEV_SECRET = "dev-secret"


class EncryptionUnavailableError(Exception):
    """The verification secret is missing or a known default in production.

    Raised by encrypt/decrypt when storing or reading a secret would be
    meaningless (or worse, would use a public placeholder key).  The global
    exception handler surfaces it as a 503 so a misconfigured deployment fails
    loudly instead of writing ciphertext nobody can trust.
    """


def _is_development_environment() -> bool:
    """True when the server is running in a dev/test setting.

    Mirrors ``github_oidc.resolve_trusted_publishing``: an explicit
    ``SFS_ENVIRONMENT`` in the development set, or (unset) a SQLite database.
    """
    environment = os.environ.get("SFS_ENVIRONMENT", "").strip().lower()
    if environment:
        return environment in _DEVELOPMENT_ENVIRONMENTS
    database_url = os.environ.get(
        "SFS_DATABASE_URL", "sqlite+aiosqlite:///./sessionfs.db"
    )
    return database_url.startswith("sqlite")


def _resolve_secret() -> bytes:
    """Return the verification secret bytes, refusing insecure defaults.

    An empty or known-default secret is allowed only in development/test; in
    production it raises ``EncryptionUnavailableError``.
    """
    secret = os.environ.get("SFS_VERIFICATION_SECRET", "")
    if not secret or secret in _KNOWN_DEFAULT_SECRETS:
        if _is_development_environment():
            return (secret or _DEV_SECRET).encode()
        raise EncryptionUnavailableError(
            "SFS_VERIFICATION_SECRET is unset or a known default; set a unique "
            "secret before storing or reading encrypted secrets."
        )
    return secret.encode()


def _hkdf_key(secret: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(secret)


def _new_fernet(secret: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_hkdf_key(secret)))


def _legacy_fernet(secret: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


def encrypt_secret(secret: str) -> str:
    """Encrypt a secret (LLM key, GitLab token, Jira token, …) for storage.

    Returns the Fernet token text.  Raises ``EncryptionUnavailableError`` when
    the verification secret is unset or a known default outside dev/test.
    """
    return _new_fernet(_resolve_secret()).encrypt(secret.encode()).decode()


def decrypt_secret(encrypted: str) -> str:
    """Decrypt a stored secret. Returns the plaintext.

    Tries the current HKDF-derived key first, then falls back to the legacy
    sha256-derived key so pre-Shield ciphertexts stay readable.  Raises
    ``cryptography.fernet.InvalidToken`` (or ``EncryptionUnavailableError``
    for an insecure secret outside dev/test) on bad input — callers that must
    tolerate a secret-rotation or corrupt row wrap this in a try/except.
    """
    secret = _resolve_secret()
    token = encrypted.encode()
    try:
        return _new_fernet(secret).decrypt(token).decode()
    except InvalidToken:
        return _legacy_fernet(secret).decrypt(token).decode()


# Back-compat aliases kept for the LLM-key callers.
encrypt_llm_key = encrypt_secret
decrypt_llm_key = decrypt_secret
