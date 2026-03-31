"""Organization role definitions and permission checks."""

from __future__ import annotations

from enum import Enum


class OrgRole(str, Enum):
    ADMIN = "admin"
    MEMBER = "member"
    # Future: VIEWER = "viewer"
    # Future: LICENSE_MANAGER = "license_manager"


# Higher number = more permissions
ROLE_LEVEL = {
    OrgRole.MEMBER: 10,
    OrgRole.ADMIN: 50,
}


def has_minimum_role(user_role: str, required_role: str) -> bool:
    """Check if user's role meets or exceeds the required role."""
    try:
        user_level = ROLE_LEVEL[OrgRole(user_role)]
    except (ValueError, KeyError):
        return False
    try:
        required_level = ROLE_LEVEL[OrgRole(required_role)]
    except (ValueError, KeyError):
        return False
    return user_level >= required_level
