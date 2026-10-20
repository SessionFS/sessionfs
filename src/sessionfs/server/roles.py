"""Organization role definitions and permission checks."""

from __future__ import annotations

from enum import Enum


class OrgRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


# Higher number = more permissions
ROLE_LEVEL = {
    OrgRole.MEMBER: 10,
    OrgRole.ADMIN: 50,
    OrgRole.OWNER: 100,
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


def roles_at_least(required_role: str) -> tuple[str, ...]:
    """Every role string whose level meets or exceeds ``required_role``.

    The SQL counterpart of :func:`has_minimum_role`, for query filters
    such as ``OrgMember.role.in_(roles_at_least("admin"))``. Ordered from
    the highest level down.
    """
    required_level = ROLE_LEVEL[OrgRole(required_role)]
    return tuple(
        role.value
        for role, level in sorted(ROLE_LEVEL.items(), key=lambda kv: -kv[1])
        if level >= required_level
    )


# Roles that carry org-admin standing. Owners rank above admins (v0.11.0),
# so every "is this user an org admin?" check must accept both.
ADMIN_ROLES: tuple[str, ...] = roles_at_least(OrgRole.ADMIN.value)
