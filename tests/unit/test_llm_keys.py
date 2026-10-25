"""server.llm_keys — the single Fernet helper for stored LLM keys."""

from __future__ import annotations

import base64
import hashlib
import os

import pytest
from cryptography.fernet import Fernet, InvalidToken

from sessionfs.server.llm_keys import (
    EncryptionUnavailableError,
    decrypt_llm_key,
    encrypt_llm_key,
)


def _legacy_fernet(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def test_round_trip():
    key = "sk-test-abcdef-1234"
    assert decrypt_llm_key(encrypt_llm_key(key)) == key


def test_round_trip_with_custom_secret(monkeypatch):
    monkeypatch.setenv("SFS_ENVIRONMENT", "production")
    monkeypatch.setenv("SFS_VERIFICATION_SECRET", "a-unique-deployment-secret")
    assert decrypt_llm_key(encrypt_llm_key("sk-custom")) == "sk-custom"


def test_legacy_ciphertext_still_decrypts():
    """Backward compat: a token produced by the old inline sha256 derivation
    still decrypts with the new helper — existing UserJudgeSettings /
    GitLabSettings ciphertexts stay readable."""
    secret = os.environ.get("SFS_VERIFICATION_SECRET", "dev-secret")
    token = _legacy_fernet(secret).encrypt(b"secret-key").decode()
    assert decrypt_llm_key(token) == "secret-key"


def test_new_ciphertext_is_not_legacy_sha256():
    """Encryption now derives a dedicated HKDF key, so the legacy sha256 key
    can no longer read new tokens (the old wire format is Fernet, but the key
    is different)."""
    secret = os.environ.get("SFS_VERIFICATION_SECRET", "dev-secret")
    token = encrypt_llm_key("secret-key").encode()
    with pytest.raises(InvalidToken):
        _legacy_fernet(secret).decrypt(token)


@pytest.mark.parametrize("secret", ["dev-secret", "change-me"])
def test_refuses_known_default_secret_outside_development(monkeypatch, secret):
    monkeypatch.setenv("SFS_ENVIRONMENT", "production")
    monkeypatch.setenv("SFS_VERIFICATION_SECRET", secret)
    with pytest.raises(EncryptionUnavailableError):
        encrypt_llm_key("sk-abc")
    with pytest.raises(EncryptionUnavailableError):
        decrypt_llm_key("garbage-token")


def test_refuses_missing_secret_outside_development(monkeypatch):
    monkeypatch.setenv("SFS_ENVIRONMENT", "production")
    monkeypatch.delenv("SFS_VERIFICATION_SECRET", raising=False)
    with pytest.raises(EncryptionUnavailableError):
        encrypt_llm_key("sk-abc")
