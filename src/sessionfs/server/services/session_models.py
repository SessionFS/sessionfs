"""Model-id bucketing and filters for captured sessions.

``Session.model_provider`` / ``Session.model_id`` come from the client
manifest, so they are untrusted free text.  This module is the one place that
decides which stored values carry no real model (the "unknown" bucket) and how
a model filter turns into SQL, so the sessions list route, the
``/sessions/models`` menu and the session helper can never disagree.

Two filter flavours:

* :func:`exact_model_filter` — the dashboard's ``?model=`` filter: exact,
  case-sensitive ids picked from the models menu.
* :func:`model_family_filter` / :func:`provider_family_filter` — the session
  helper's filters: case-insensitive substring matches so ``deepseek`` finds
  ``deepseek-v4-pro`` and ``deepseek-flash``.  User text is LIKE-escaped, so
  ``%`` or ``_`` match literally.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, false, func, or_

from sessionfs.server.db.models import Session

# The filter sentinel for the "unknown model" bucket.
UNKNOWN_MODEL = "unknown"
# model_id is client-supplied (manifest), so these stored values carry no real
# model and fold into the unknown bucket alongside NULL: an empty id, a
# literal "unknown" (which would otherwise collide with the sentinel) and
# Claude Code's ``<synthetic>`` placeholder for locally generated turns.
UNKNOWN_MODEL_IDS: tuple[str, ...] = ("", UNKNOWN_MODEL, "<synthetic>")
# Provider values that carry no real provider.
UNKNOWN_PROVIDER_IDS: tuple[str, ...] = ("", UNKNOWN_MODEL)


def escape_like(query: str) -> str:
    """Escape LIKE wildcards so a query of ``%`` or ``_`` matches literally.

    Pair with ``escape="\\\\"`` on ``like``/``ilike``: the DB then treats a
    backslash as the escape character, and every ``\\``, ``%`` and ``_`` in
    the user's text is backslash-escaped here.
    """
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def model_is_unknown() -> ColumnElement[bool]:
    """SQL predicate: the row belongs to the unknown-model bucket."""
    return or_(Session.model_id.is_(None), Session.model_id.in_(UNKNOWN_MODEL_IDS))


def provider_is_unknown() -> ColumnElement[bool]:
    """SQL predicate: the row carries no real provider."""
    return or_(
        Session.model_provider.is_(None),
        # Same normalisation as :func:`provider_bucket`: trim, then lower.
        func.lower(func.trim(Session.model_provider)).in_(UNKNOWN_PROVIDER_IDS),
    )


def model_bucket(model_id: str | None) -> str | None:
    """The bucket a stored model id belongs to (None = the unknown bucket)."""
    if model_id is None or model_id in UNKNOWN_MODEL_IDS:
        return None
    return model_id


def provider_bucket(provider: str | None) -> str | None:
    """The bucket a stored provider belongs to (None = no real provider)."""
    if provider is None or provider.strip().lower() in UNKNOWN_PROVIDER_IDS:
        return None
    return provider.strip()


def exact_model_filter(values: list[str]) -> ColumnElement[bool]:
    """Exact, repeatable model filter (OR across values).

    Known ids become one ``IN`` clause; the ``unknown`` sentinel (or any value
    that is itself an unknown-bucket id) adds a single unknown-bucket clause.
    """
    known = [v for v in values if v not in UNKNOWN_MODEL_IDS]
    wants_unknown = len(known) != len(values)
    clauses: list[ColumnElement[bool]] = []
    if known:
        clauses.append(Session.model_id.in_(known))
    if wants_unknown:
        clauses.append(model_is_unknown())
    return clauses[0] if len(clauses) == 1 else or_(*clauses)


def _wants_unknown(value: str, unknown_ids: tuple[str, ...]) -> bool:
    return value.lower() in unknown_ids or value.lower() == UNKNOWN_MODEL


def model_family_filter(values: list[str]) -> ColumnElement[bool]:
    """Case-insensitive family filter on ``model_id`` (OR across values).

    ``deepseek`` matches every id containing it (``deepseek-v4-pro``,
    ``deepseek-flash``, ``openrouter/deepseek/…``).  ``unknown`` (or any
    unknown-bucket id) selects the unknown bucket.  Unknown-bucket rows never
    match a family value: a stored literal ``"unknown"`` is not a model.
    Empty values are ignored; an all-empty list matches nothing.
    """
    clauses: list[ColumnElement[bool]] = []
    unknown_added = False
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        if _wants_unknown(value, UNKNOWN_MODEL_IDS):
            if not unknown_added:
                clauses.append(model_is_unknown())
                unknown_added = True
            continue
        clauses.append(
            Session.model_id.ilike(f"%{escape_like(value)}%", escape="\\")
            & ~model_is_unknown()
        )
    if not clauses:
        return false()
    return clauses[0] if len(clauses) == 1 else or_(*clauses)


def provider_family_filter(values: list[str]) -> ColumnElement[bool]:
    """Case-insensitive substring filter on ``model_provider`` (OR across values).

    ``unknown`` selects sessions with no recorded provider.
    """
    clauses: list[ColumnElement[bool]] = []
    unknown_added = False
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        if _wants_unknown(value, UNKNOWN_PROVIDER_IDS):
            if not unknown_added:
                clauses.append(provider_is_unknown())
                unknown_added = True
            continue
        clauses.append(
            Session.model_provider.ilike(f"%{escape_like(value)}%", escape="\\")
            & ~provider_is_unknown()
        )
    if not clauses:
        return false()
    return clauses[0] if len(clauses) == 1 else or_(*clauses)
