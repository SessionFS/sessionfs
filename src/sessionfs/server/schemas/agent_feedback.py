"""v0.17.0 — vocabulary for structured agent-feedback records.

Single source of truth for the enum-as-string values and the fixed
label vocabulary. The DB stores these as plain VARCHARs (cross-DB
compatible, matching the ticket/agent_run pattern); the CHECK
constraints in migration 064 enforce the enum membership, and the
label → feedback_type mapping below is enforced at the route layer
(before any write) so a label can never attach to the wrong type.

Auto-capture (ticket reassignment) writes one canonical record:
    feedback_type=route, verdict=negative, label=wrong_route, source=auto
"""

from __future__ import annotations

FEEDBACK_TYPES: tuple[str, ...] = (
    "route",
    "summary",
    "actionability",
    "duplicate",
    "resolution",
)

VERDICTS: tuple[str, ...] = ("positive", "negative", "neutral")

SOURCES: tuple[str, ...] = ("human", "auto")

# Fixed label vocabulary, keyed by feedback_type. A label supplied for a
# different feedback_type is rejected with 422 before any DB write.
LABELS_BY_TYPE: dict[str, frozenset[str]] = {
    "route": frozenset({"correct_route", "wrong_route"}),
    "summary": frozenset({"summary_good", "summary_bad"}),
    "actionability": frozenset({"actionable", "not_actionable"}),
    "duplicate": frozenset({"duplicate", "new_issue"}),
    "resolution": frozenset(
        {"false_positive", "flaky", "resolved_by_fix", "resolved_by_test_update"}
    ),
}

ALL_LABELS: frozenset[str] = frozenset().union(*LABELS_BY_TYPE.values())


def label_valid_for_type(feedback_type: str, label: str | None) -> bool:
    """True when `label` is None, or belongs to the fixed set for `type`."""
    if label is None:
        return True
    allowed = LABELS_BY_TYPE.get(feedback_type)
    if allowed is None:
        return False
    return label in allowed
