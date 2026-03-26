"""Tests for EKS round 2 fixes: session ID format, rate limiter config, S3 prefix."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from sessionfs.session_id import SESSION_ID_RE, validate_session_id


# --- Session ID validation: accept 8-40 hex chars ---


class TestSessionIdShortFormat:
    """Session IDs with 8 hex chars (short form) must be accepted."""

    def test_8_char_hex_id(self):
        assert validate_session_id("ses_ae7652a4") is True

    def test_16_char_hex_id(self):
        assert validate_session_id("ses_346b4d7288214b0f") is True

    def test_40_char_hex_id(self):
        assert validate_session_id("ses_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2") is True

    def test_reject_7_char_id(self):
        assert validate_session_id("ses_ae7652a") is False

    def test_reject_41_char_id(self):
        assert validate_session_id("ses_" + "a" * 41) is False

    def test_reject_uppercase(self):
        assert validate_session_id("ses_AE7652A4") is False

    def test_reject_special_chars(self):
        assert validate_session_id("ses_ae76!2a4") is False

    def test_regex_pattern(self):
        assert SESSION_ID_RE.pattern == r"^ses_[a-z0-9]{8,40}$"


# --- Rate limiter respects configuration ---


class TestRateLimiterConfig:
    """Rate limiter must respect SFS_RATE_LIMIT_PER_MINUTE."""

    def test_disabled_with_zero(self):
        """SFS_RATE_LIMIT_PER_MINUTE=0 should disable rate limiting."""
        from sessionfs.server.auth import dependencies

        # Reset global state
        dependencies._rate_limiter = None
        dependencies._rate_limit_disabled = None

        with patch.dict(os.environ, {"SFS_RATE_LIMIT_PER_MINUTE": "0"}):
            dependencies.get_rate_limiter()
            assert dependencies._rate_limit_disabled is True

        # Cleanup
        dependencies._rate_limiter = None
        dependencies._rate_limit_disabled = None

    def test_custom_limit(self):
        """SFS_RATE_LIMIT_PER_MINUTE=10000 should create limiter with 10000."""
        from sessionfs.server.auth import dependencies

        dependencies._rate_limiter = None
        dependencies._rate_limit_disabled = None

        with patch.dict(os.environ, {"SFS_RATE_LIMIT_PER_MINUTE": "10000"}):
            limiter = dependencies.get_rate_limiter()
            assert limiter.max_requests == 10000
            assert dependencies._rate_limit_disabled is False

        dependencies._rate_limiter = None
        dependencies._rate_limit_disabled = None

    def test_default_limit(self):
        """Default rate limit should be 120/min."""
        from sessionfs.server.auth import dependencies

        dependencies._rate_limiter = None
        dependencies._rate_limit_disabled = None

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SFS_RATE_LIMIT_PER_MINUTE", None)
            limiter = dependencies.get_rate_limiter()
            assert limiter.max_requests == 120

        dependencies._rate_limiter = None
        dependencies._rate_limit_disabled = None


# --- S3 bucket name with path prefix ---


class TestS3BucketPrefix:
    """S3BlobStore must handle bucket names containing path prefixes."""

    def test_bucket_with_slash(self):
        """Bucket name 'my-bucket/path' should split into bucket + prefix."""
        with patch("boto3.client"):
            from sessionfs.server.storage.s3 import S3BlobStore

            store = S3BlobStore(bucket="my-bucket/some/path", region="us-east-1")
            assert store.bucket == "my-bucket"
            assert store.prefix == "some/path/"

    def test_bucket_without_slash(self):
        """Plain bucket name should have empty prefix."""
        with patch("boto3.client"):
            from sessionfs.server.storage.s3 import S3BlobStore

            store = S3BlobStore(bucket="my-bucket", region="us-east-1")
            assert store.bucket == "my-bucket"
            assert store.prefix == ""

    def test_explicit_prefix(self):
        """Explicit prefix parameter should work."""
        with patch("boto3.client"):
            from sessionfs.server.storage.s3 import S3BlobStore

            store = S3BlobStore(bucket="my-bucket", region="us-east-1", prefix="sessions/")
            assert store.bucket == "my-bucket"
            assert store.prefix == "sessions/"

    def test_bucket_slash_plus_explicit_prefix(self):
        """Bucket with slash and explicit prefix should combine."""
        with patch("boto3.client"):
            from sessionfs.server.storage.s3 import S3BlobStore

            store = S3BlobStore(bucket="my-bucket/env", region="us-east-1", prefix="data/")
            assert store.bucket == "my-bucket"
            assert store.prefix == "env/data/"

    def test_key_prefixing(self):
        """Keys should be prefixed correctly."""
        with patch("boto3.client"):
            from sessionfs.server.storage.s3 import S3BlobStore

            store = S3BlobStore(bucket="my-bucket", region="us-east-1", prefix="prod/")
            assert store._key("ses_abc123/manifest.json") == "prod/ses_abc123/manifest.json"

    def test_key_no_prefix(self):
        """Without prefix, keys should pass through unchanged."""
        with patch("boto3.client"):
            from sessionfs.server.storage.s3 import S3BlobStore

            store = S3BlobStore(bucket="my-bucket", region="us-east-1")
            assert store._key("ses_abc123/manifest.json") == "ses_abc123/manifest.json"


# --- Server route session ID validation ---


class TestRouteSessionIdValidation:
    """Route-level session ID validation must accept short IDs."""

    def test_short_id_accepted(self):
        pytest.importorskip("fastapi", reason="Server tests require: pip install -e '.[dev]'")
        from sessionfs.server.routes.sessions import _validate_session_id

        assert _validate_session_id("ses_ae7652a4") == "ses_ae7652a4"

    def test_long_id_accepted(self):
        pytest.importorskip("fastapi", reason="Server tests require: pip install -e '.[dev]'")
        from sessionfs.server.routes.sessions import _validate_session_id

        assert _validate_session_id("ses_346b4d7288214b0f") == "ses_346b4d7288214b0f"

    def test_40_char_id_accepted(self):
        pytest.importorskip("fastapi", reason="Server tests require: pip install -e '.[dev]'")
        from sessionfs.server.routes.sessions import _validate_session_id

        long_id = "ses_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        assert _validate_session_id(long_id) == long_id
