"""Sanitize archive-derived values before binding them to PostgreSQL.

PostgreSQL ``text`` rejects the NUL byte (``\\x00``) — asyncpg raises
``CharacterNotInRepertoireError``, and the daemon treats that as a transient
failure and retries the push forever. These helpers strip NULs from values
*derived* from a pushed archive (titles, previews, search text, git metadata,
manifest provenance) so the push is ACCEPTED while the derived DB columns stay
NUL-free. The archive blob itself is never modified.

Explicit user-supplied fields (title / tags query params) keep their own
``422``/``400`` validation and are NOT routed through here — only archive-
derived values are sanitized.
"""

from __future__ import annotations

from typing import Any


def strip_nul(value: Any) -> tuple[Any, int]:
    """Return ``(value_with_nuls_stripped, count_of_nuls_removed)``.

    Non-string values and NUL-free strings are returned unchanged with a
    count of 0, so callers can apply this unconditionally at every binding
    point without branching.
    """
    if not isinstance(value, str):
        return value, 0
    count = value.count("\x00")
    if count == 0:
        return value, 0
    return value.replace("\x00", ""), count


def sanitize_surrogates(value: str) -> str:
    """Replace lone surrogates (U+D800–U+DFFF) with U+FFFD.

    JSON can carry lone surrogates (e.g. ``"a\\ud800b"``) that Python's
    ``json`` decoder leaves as a surrogate code point in the ``str``. Such a
    string cannot be encoded to UTF-8, so any downstream ``encode("utf-8")``
    raises ``UnicodeEncodeError`` and 500s the endpoint. The UTF-16 round-trip
    maps each lone surrogate to U+FFFD while leaving every other character
    intact. The fast path keeps the common (already-encodable) case cheap.
    """
    if not value:
        return value
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def sanitize_archive_string(value: Any) -> tuple[Any, int]:
    """Strip NULs *and* replace lone surrogates; returns ``(value, nul_count)``.

    The NUL count is the only thing callers report (surrogates are replaced
    in place and never counted), so the ``(value, count)`` contract matches
    ``strip_nul`` exactly.
    """
    value, count = strip_nul(value)
    if isinstance(value, str):
        value = sanitize_surrogates(value)
    return value, count


# String keys in the manifest-metadata dict (see
# ``_extract_manifest_metadata``) that are bound to PostgreSQL String/Text
# columns. Integer counters and JSON-encoded strings (``tags``,
# ``instruction_artifacts``) are intentionally omitted: ``json.dumps`` escapes
# ``\\x00`` as the literal six characters ``\\u0000``, so no raw NUL ever
# reaches those columns.
_META_STRING_KEYS: tuple[str, ...] = (
    "title",
    "source_tool",
    "source_tool_version",
    "original_session_id",
    "parent_session_id",
    "model_provider",
    "model_id",
    "persona_name",
    "ticket_id",
    "retrieval_audit_id",
    "rules_source",
    "rules_version",
    "rules_hash",
)


def sanitize_meta(meta: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Strip NULs and lone surrogates from every String-bound manifest key.

    Mutates and returns ``meta`` along with the total number of NULs removed.
    """
    count = 0
    for key in _META_STRING_KEYS:
        if key in meta:
            value, n = sanitize_archive_string(meta[key])
            if value is not meta[key]:
                meta[key] = value
            if n:
                count += n
    return meta, count
