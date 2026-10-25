"""Fernet encryption for stored secrets.

Single source of truth for the Fernet key derivation that was previously
copied into the judge-settings routes (``routes/settings.py``), the
summary / audit routes and the GitLab integration (``routes/settings.py``
and ``routes/webhooks.py``). The key is derived from
``SFS_VERIFICATION_SECRET`` the same way as before — sha256 of the secret,
urlsafe-base64-encoded — so existing ``UserJudgeSettings`` and
``GitLabSettings`` ciphertexts stay decryptable.  This module must not change
the wire format; callers switch to it with no behaviour change.

The org-wide session-helper key (migration 084, ``OrgLlmSettings``) uses the
same derivation, keeping every stored secret readable through one path.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    secret = os.environ.get("SFS_VERIFICATION_SECRET", "dev-secret")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(secret: str) -> str:
    """Encrypt a secret (LLM key, GitLab token, …) for storage.

    Returns the Fernet token text.
    """
    return _get_fernet().encrypt(secret.encode()).decode()


def decrypt_secret(encrypted: str) -> str:
    """Decrypt a stored secret. Returns the plaintext.

    Raises ``cryptography.fernet.InvalidToken`` (or other ``Exception``
    subclasses) on bad input — callers that must tolerate a secret-rotation
    or corrupt row wrap this in a try/except.
    """
    return _get_fernet().decrypt(encrypted.encode()).decode()


# Back-compat aliases kept for the LLM-key callers.
encrypt_llm_key = encrypt_secret
decrypt_llm_key = decrypt_secret
