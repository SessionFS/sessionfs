"""M2: Session ID validation."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="Server tests require: pip install -e '.[dev]'")

from fastapi import HTTPException

from sessionfs.server.routes.sessions import _validate_session_id


class TestSessionIdValidation:

    def test_valid_session_id_8_chars(self):
        assert _validate_session_id("ses_ae7652a4") == "ses_ae7652a4"

    def test_valid_session_id_16_chars(self):
        assert _validate_session_id("ses_346b4d7288214b0f") == "ses_346b4d7288214b0f"

    def test_valid_session_id_40_chars(self):
        long_id = "ses_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
        assert _validate_session_id(long_id) == long_id

    def test_missing_prefix(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id("abc123def456ab")
        assert exc_info.value.status_code == 400

    def test_path_traversal(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id("../etc/passwd")
        assert exc_info.value.status_code == 400

    def test_sql_injection(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id("ses_'; DROP TABLE sessions;--")
        assert exc_info.value.status_code == 400

    def test_empty_string(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id("")
        assert exc_info.value.status_code == 400

    def test_too_short(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id("ses_abc")
        assert exc_info.value.status_code == 400

    def test_too_long(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id("ses_" + "a" * 41)
        assert exc_info.value.status_code == 400

    def test_null_bytes(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id("ses_abc\x00def456ab")
        assert exc_info.value.status_code == 400

    def test_slash_in_id(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id("ses_abc/def456ab")
        assert exc_info.value.status_code == 400

    def test_special_chars(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id("ses_abc!@#$%^&*()")
        assert exc_info.value.status_code == 400

    def test_uppercase_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            _validate_session_id("ses_AE7652A4BCDE")
        assert exc_info.value.status_code == 400
