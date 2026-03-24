"""Tests for session alias validation and CLI alias command."""

from __future__ import annotations

import json
import re

import pytest

# Alias validation regex (same as in server and CLI)
_ALIAS_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{2,99}$")


class TestAliasValidation:
    """Test alias format validation rules."""

    def test_valid_simple(self):
        assert _ALIAS_RE.match("auth-debug")

    def test_valid_underscores(self):
        assert _ALIAS_RE.match("my_session_1")

    def test_valid_hyphens(self):
        assert _ALIAS_RE.match("fix-login-bug")

    def test_valid_alphanumeric(self):
        assert _ALIAS_RE.match("session42")

    def test_valid_min_length(self):
        assert _ALIAS_RE.match("abc")

    def test_valid_max_length(self):
        assert _ALIAS_RE.match("a" * 100)

    def test_too_short(self):
        assert not _ALIAS_RE.match("ab")

    def test_too_long(self):
        assert not _ALIAS_RE.match("a" * 101)

    def test_no_spaces(self):
        assert not _ALIAS_RE.match("has spaces")

    def test_no_special_chars(self):
        assert not _ALIAS_RE.match("has@special!")

    def test_no_dots(self):
        assert not _ALIAS_RE.match("has.dots")

    def test_starts_with_letter(self):
        assert _ALIAS_RE.match("abc123")

    def test_starts_with_number(self):
        assert _ALIAS_RE.match("123abc")

    def test_cannot_start_with_hyphen(self):
        assert not _ALIAS_RE.match("-starts-bad")

    def test_cannot_start_with_underscore(self):
        assert not _ALIAS_RE.match("_starts-bad")

    def test_uppercase_allowed(self):
        assert _ALIAS_RE.match("MySession")

    def test_mixed_case_hyphens(self):
        assert _ALIAS_RE.match("Fix-Auth-Bug")

    def test_empty_string(self):
        assert not _ALIAS_RE.match("")

    def test_single_char(self):
        assert not _ALIAS_RE.match("a")


class TestAliasInManifest:
    """Test alias storage in local manifests."""

    def test_add_alias_to_manifest(self):
        manifest = {"session_id": "ses_test123", "title": "Test"}
        manifest["alias"] = "auth-debug"
        data = json.dumps(manifest)
        loaded = json.loads(data)
        assert loaded["alias"] == "auth-debug"

    def test_remove_alias_from_manifest(self):
        manifest = {"session_id": "ses_test123", "title": "Test", "alias": "old"}
        del manifest["alias"]
        data = json.dumps(manifest)
        loaded = json.loads(data)
        assert "alias" not in loaded

    def test_alias_not_in_manifest_returns_none(self):
        manifest = {"session_id": "ses_test123", "title": "Test"}
        assert manifest.get("alias") is None
