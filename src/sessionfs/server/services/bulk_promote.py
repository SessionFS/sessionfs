"""v0.10.12 — bulk promote eligible KB notes to claims.

Background: the v0.10.10 confidence clamp bug (since fixed) left many
production projects with hundreds of stuck note-class entries that
never reached the 0.8 promotion gate. The per-entry repair path
(`PUT /entries/{id}/confidence` then `PUT /entries/{id}/promote`)
isn't viable at 270+ entries. This service does it in one operation.

Eligibility, in this order:
  1. Skip if NOT class=note (already_claim or other).
  2. Skip if dismissed.
  3. Skip if superseded.
  4. Skip if entry_type doesn't match the optional filter.
  5. Skip if content length < min_length.
  6. If set_confidence is None and entry.confidence < min_confidence:
     skip (low_confidence). This is the "caller doesn't want to assert
     confidence, just promote already-confident notes" path.
     If set_confidence is provided, override before the duplicate check.
  7. Skip if near-duplicate (word_overlap > 0.85) against any active claim.
  8. Otherwise: promote — set claim_class='claim', promoted_at,
     promoted_by. If set_confidence was used, also update confidence.

dry_run=True is the default. The service does ALL filtering work but
makes zero writes — useful for `--dry-run` to see what WOULD happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import KnowledgeEntry
from sessionfs.server.services.knowledge import word_overlap


# Mirror the single-entry promote gates so behaviour stays consistent.
_PROMOTE_GATE_CONFIDENCE = 0.8
_PROMOTE_GATE_LENGTH = 50
_NEAR_DUPLICATE_THRESHOLD = 0.85
# Cap recent-claims fetched for duplicate comparison. Same number the
# single-entry promote endpoint uses (limit 50).
_RECENT_CLAIMS_LIMIT = 50


SKIP_REASONS = (
    "already_claim",
    "dismissed",
    "superseded",
    "wrong_type",
    "too_short",
    "low_confidence",
    "duplicate",
)


@dataclass
class BulkPromoteResult:
    """Structured result from a bulk-promote operation.

    `promoted` and `skipped` are counts; `reasons` is a per-reason
    breakdown so the caller can show "12 too short, 8 duplicates,
    3 dismissed" rather than just "23 skipped".

    `dry_run` echoes the input so callers can pipe the result without
    losing the safety state. `promoted_ids` is a list of entry IDs
    that flipped to claim (or would have, in dry-run mode).
    """

    promoted: int = 0
    skipped: int = 0
    reasons: dict[str, int] = field(default_factory=lambda: {r: 0 for r in SKIP_REASONS})
    promoted_ids: list[int] = field(default_factory=list)
    dry_run: bool = True

    def _bump_skip(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons[reason] = 0
        self.reasons[reason] += 1
        self.skipped += 1

    def to_dict(self) -> dict:
        return {
            "promoted": self.promoted,
            "skipped": self.skipped,
            "reasons": dict(self.reasons),
            "promoted_ids": list(self.promoted_ids),
            "dry_run": self.dry_run,
        }


async def promote_eligible_notes(
    db: AsyncSession,
    project_id: str,
    *,
    user_id: str,
    min_length: int = _PROMOTE_GATE_LENGTH,
    min_confidence: float = _PROMOTE_GATE_CONFIDENCE,
    set_confidence: Optional[float] = None,
    entry_type: Optional[str] = None,
    dry_run: bool = True,
) -> BulkPromoteResult:
    """Promote eligible note entries to claim in one operation.

    Args:
      db: AsyncSession (caller manages transaction; we commit at the end).
      project_id: project to scan.
      user_id: stamped onto `promoted_by` on the rows we flip.
      min_length: skip entries with content shorter than this. Matches the
        single-entry promote gate (50 chars) by default.
      min_confidence: ONLY used when set_confidence is None — entries
        below this stay note-class. Matches the 0.8 single-entry gate.
      set_confidence: when provided, override each candidate's confidence
        BEFORE the near-duplicate check. Used by the CLI/MCP
        `--confidence` flag — the caller is asserting "these notes are
        claim-worthy at this confidence."
      entry_type: optional filter — only consider this entry_type.
      dry_run: when True (default), compute the eligibility decision but
        make zero writes. The returned result still shows what WOULD
        happen (promoted_ids includes the entries that would have
        flipped). Default is True so the surface is safe-by-default.

    Returns: BulkPromoteResult with promoted count, skipped count,
    per-reason breakdown, the list of promoted ids, and an echo of
    dry_run.
    """
    result = BulkPromoteResult(dry_run=dry_run)

    # Fetch every note in the project. We do NOT pre-filter on length
    # or confidence in SQL because we want the per-reason breakdown in
    # the response — knowing "8 too short, 12 low_confidence" is more
    # actionable than "20 ineligible." For 270 entries this is trivial
    # in-memory work.
    notes = (
        await db.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.project_id == project_id,
                KnowledgeEntry.claim_class == "note",
            )
        )
    ).scalars().all()

    # Pre-fetch active claims ONCE for near-duplicate comparison instead
    # of re-querying inside the loop. The single-entry promote endpoint
    # caps at 50 recent claims; we keep parity.
    existing_claims = (
        await db.execute(
            select(KnowledgeEntry.content)
            .where(
                KnowledgeEntry.project_id == project_id,
                KnowledgeEntry.claim_class == "claim",
                KnowledgeEntry.dismissed == False,  # noqa: E712
            )
            .order_by(KnowledgeEntry.created_at.desc())
            .limit(_RECENT_CLAIMS_LIMIT)
        )
    ).all()
    existing_claim_texts = [c[0] for c in existing_claims if c[0]]

    now = datetime.now(timezone.utc)

    for entry in notes:
        # 1. Defensive: claim_class filter already excludes claims, but
        #    a row with claim_class=None or another value should skip.
        if getattr(entry, "claim_class", "note") != "note":
            result._bump_skip("already_claim")
            continue
        # 2. Dismissed
        if entry.dismissed:
            result._bump_skip("dismissed")
            continue
        # 3. Superseded
        if entry.superseded_by is not None:
            result._bump_skip("superseded")
            continue
        # 4. Entry-type filter
        if entry_type is not None and entry.entry_type != entry_type:
            result._bump_skip("wrong_type")
            continue
        # 5. Length gate
        if len(entry.content or "") < min_length:
            result._bump_skip("too_short")
            continue
        # 6. Confidence: either override via set_confidence, or check
        #    the existing value against min_confidence.
        if set_confidence is None:
            if (entry.confidence or 0.0) < min_confidence:
                result._bump_skip("low_confidence")
                continue
        # 7. Near-duplicate
        is_dup = False
        for existing in existing_claim_texts:
            if word_overlap(entry.content, existing) > _NEAR_DUPLICATE_THRESHOLD:
                is_dup = True
                break
        if is_dup:
            result._bump_skip("duplicate")
            continue

        # All gates passed — promote (or pretend to in dry-run).
        result.promoted += 1
        result.promoted_ids.append(entry.id)
        # Codex R1 MEDIUM (tk_03263e280f4b4732): the eligible entry
        # joins the comparison set regardless of dry_run so subsequent
        # candidates in this pass see the same duplicate-suppression
        # behaviour. Otherwise a dry-run with two near-duplicate notes
        # would report both as promoted while the confirmed run would
        # skip one — breaking the "inspect-then-mutate" safety contract.
        existing_claim_texts.append(entry.content)
        if not dry_run:
            if set_confidence is not None and (
                entry.confidence is None or entry.confidence < set_confidence
            ):
                entry.confidence = set_confidence
            entry.claim_class = "claim"
            entry.promoted_at = now
            entry.promoted_by = user_id

    if not dry_run and result.promoted > 0:
        await db.commit()

    return result
