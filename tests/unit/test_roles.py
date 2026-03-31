"""Unit tests for RBAC role definitions."""

from __future__ import annotations

import pytest

from sessionfs.server.roles import OrgRole, has_minimum_role


class TestHasMinimumRole:
    def test_admin_meets_admin(self):
        assert has_minimum_role("admin", "admin") is True

    def test_admin_meets_member(self):
        assert has_minimum_role("admin", "member") is True

    def test_member_meets_member(self):
        assert has_minimum_role("member", "member") is True

    def test_member_does_not_meet_admin(self):
        assert has_minimum_role("member", "admin") is False

    def test_invalid_user_role(self):
        assert has_minimum_role("unknown", "member") is False

    def test_invalid_required_role(self):
        assert has_minimum_role("admin", "unknown") is False


class TestOrgRoleEnum:
    def test_admin_value(self):
        assert OrgRole.ADMIN == "admin"

    def test_member_value(self):
        assert OrgRole.MEMBER == "member"

    def test_from_string(self):
        assert OrgRole("admin") == OrgRole.ADMIN
