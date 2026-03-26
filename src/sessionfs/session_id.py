"""Session ID generation and validation.

All .sfs session IDs use the format: ses_ + 8-40 hex characters.
This module provides the canonical way to generate and validate them.
"""

from __future__ import annotations

import re
import uuid as uuid_mod

SESSION_ID_RE = re.compile(r"^ses_[a-z0-9]{8,40}$")


def generate_session_id() -> str:
    """Generate a new random ses_ prefixed session ID."""
    return f"ses_{uuid_mod.uuid4().hex[:16]}"


def session_id_from_native(native_id: str) -> str:
    """Derive a deterministic ses_ prefixed ID from a native tool UUID.

    Uses the first 16 hex chars of the UUID (stripping dashes) so that
    repeated captures of the same native session produce the same ses_ ID.
    """
    hex_chars = native_id.replace("-", "")[:16]
    return f"ses_{hex_chars}"


def validate_session_id(session_id: str) -> bool:
    """Check whether a session ID matches the ses_ prefix format."""
    return bool(SESSION_ID_RE.match(session_id))
