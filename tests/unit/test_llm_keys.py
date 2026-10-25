"""server.llm_keys — the single Fernet helper for stored LLM keys."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet

from sessionfs.server.llm_keys import decrypt_llm_key, encrypt_llm_key


def test_round_trip():
    key = "sk-test-abcdef-1234"
    assert decrypt_llm_key(encrypt_llm_key(key)) == key


def test_derivation_matches_legacy_judge_settings():
    """The refactor must not change the wire format.

    A token produced by the old inline derivation still decrypts with the
    new helper, and vice versa — existing UserJudgeSettings ciphertexts stay
    readable.
    """
    secret = os.environ.get("SFS_VERIFICATION_SECRET", "dev-secret")
    derived = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    legacy = Fernet(derived)

    token = legacy.encrypt(b"secret-key").decode()
    assert decrypt_llm_key(token) == "secret-key"
    assert legacy.decrypt(encrypt_llm_key("secret-key").encode()) == b"secret-key"
