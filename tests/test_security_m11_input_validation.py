"""M11: Input validation on upload parameters."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="Server tests require: pip install -e '.[dev]'")

from pydantic import ValidationError

from sessionfs.server.schemas.sessions import SessionMetadataUpdate
from sessionfs.server.routes.sessions import _sanitize_string, _validate_tags


class TestInputValidation:

    # --- Pydantic schema validation ---

    def test_title_max_length(self):
        with pytest.raises(ValidationError):
            SessionMetadataUpdate(title="x" * 501)

    def test_title_null_bytes_rejected(self):
        with pytest.raises(ValidationError):
            SessionMetadataUpdate(title="hello\x00world")

    def test_title_html_stripped(self):
        update = SessionMetadataUpdate(title="<script>alert(1)</script>Hello")
        assert "<script>" not in update.title
        assert "Hello" in update.title

    def test_tags_max_count(self):
        with pytest.raises(ValidationError):
            SessionMetadataUpdate(tags=["tag"] * 21)

    def test_tags_max_length_per_tag(self):
        with pytest.raises(ValidationError):
            SessionMetadataUpdate(tags=["x" * 51])

    def test_tags_null_bytes_rejected(self):
        with pytest.raises(ValidationError):
            SessionMetadataUpdate(tags=["hello\x00world"])

    def test_valid_update_accepted(self):
        update = SessionMetadataUpdate(title="My Session", tags=["test", "demo"])
        assert update.title == "My Session"
        assert update.tags == ["test", "demo"]

    # --- Route-level sanitization ---

    def test_sanitize_string_strips_html(self):
        result = _sanitize_string("<b>bold</b> text <script>evil</script>")
        assert "<b>" not in result
        assert "<script>" not in result
        assert "bold" in result
        assert "text" in result

    def test_sanitize_string_null_bytes_rejected(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _sanitize_string("hello\x00world")
        assert exc_info.value.status_code == 400

    def test_sanitize_string_normal_text_passes(self):
        assert _sanitize_string("Hello World 123!") == "Hello World 123!"

    # --- Tags JSON validation ---

    def test_validate_tags_valid(self):
        result = _validate_tags('["test", "demo"]')
        assert result == '["test", "demo"]'

    def test_validate_tags_invalid_json(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_tags("not json")
        assert exc_info.value.status_code == 400

    def test_validate_tags_not_array(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_tags('{"key": "value"}')

    def test_validate_tags_too_many(self):
        from fastapi import HTTPException
        tags = '["t' + '","t'.join(str(i) for i in range(21)) + '"]'
        with pytest.raises(HTTPException):
            _validate_tags(tags)

    def test_validate_tags_tag_too_long(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_tags(f'["{("x" * 51)}"]')

    def test_validate_tags_null_bytes_rejected(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_tags('["hello\\u0000world"]')

    # --- XSS payload tests ---

    def test_xss_in_title_stripped(self):
        update = SessionMetadataUpdate(title='Real title <img src=x onerror="alert(1)"> tail')
        assert "<img" not in update.title
        assert update.title == "Real title  tail"

    def test_xss_only_title_rejected_as_empty(self):
        """tk_cf9f1691091d4e8e: a title that strips to empty is rejected (422)
        rather than silently saved as empty."""
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            SessionMetadataUpdate(title='<img src=x onerror="alert(1)">')
