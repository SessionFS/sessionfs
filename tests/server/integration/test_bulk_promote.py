"""v0.10.12 — unit tests for promote_eligible_notes service.

Exercises each skip reason and the dry_run safety invariant against
an in-memory async session. The endpoint integration tests in
test_knowledge.py exercise the wire format.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import KnowledgeEntry, Project, User
from sessionfs.server.services.bulk_promote import (
    SKIP_REASONS,
    promote_eligible_notes,
)


def _content(words: int = 10) -> str:
    """Return a content string with N distinct words, long enough to
    clear the 50-char gate by default."""
    return " ".join(f"word{w}{uuid.uuid4().hex[:4]}" for w in range(words))


async def _make_user(db: AsyncSession, name: str = "alice") -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="pro",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_project(db: AsyncSession, owner: User) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"bulk-promote-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/p-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _add_note(
    db: AsyncSession,
    *,
    project: Project,
    user: User,
    content: str,
    confidence: float = 0.85,
    entry_type: str = "decision",
    dismissed: bool = False,
    superseded_by: int | None = None,
    claim_class: str = "note",
) -> KnowledgeEntry:
    entry = KnowledgeEntry(
        project_id=project.id,
        user_id=user.id,
        session_id="ses_test",
        entry_type=entry_type,
        content=content,
        confidence=confidence,
        dismissed=dismissed,
        superseded_by=superseded_by,
        claim_class=claim_class,
        created_at=datetime.now(timezone.utc),
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


@pytest.mark.asyncio
async def test_promotes_eligible_note(db_session: AsyncSession):
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    entry = await _add_note(
        db_session, project=project, user=user, content=_content(15)
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=False
    )
    assert result.promoted == 1
    assert result.skipped == 0
    assert entry.id in result.promoted_ids

    await db_session.refresh(entry)
    assert entry.claim_class == "claim"
    assert entry.promoted_at is not None
    assert entry.promoted_by == user.id


@pytest.mark.asyncio
async def test_dry_run_default_makes_no_writes(db_session: AsyncSession):
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    entry = await _add_note(
        db_session, project=project, user=user, content=_content(15)
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id
    )
    assert result.dry_run is True
    assert result.promoted == 1
    assert entry.id in result.promoted_ids

    # The entry should NOT have been mutated.
    await db_session.refresh(entry)
    assert entry.claim_class == "note"
    assert entry.promoted_at is None


@pytest.mark.asyncio
async def test_skips_too_short(db_session: AsyncSession):
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _add_note(
        db_session, project=project, user=user, content="x" * 10,  # < 50
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=False
    )
    assert result.promoted == 0
    assert result.skipped == 1
    assert result.reasons["too_short"] == 1


@pytest.mark.asyncio
async def test_skips_dismissed(db_session: AsyncSession):
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _add_note(
        db_session, project=project, user=user,
        content=_content(15), dismissed=True,
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=False
    )
    assert result.promoted == 0
    assert result.reasons["dismissed"] == 1


@pytest.mark.asyncio
async def test_skips_superseded(db_session: AsyncSession):
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    # Need a superseding entry to exist
    seed = await _add_note(
        db_session, project=project, user=user, content=_content(15),
    )
    await _add_note(
        db_session, project=project, user=user,
        content=_content(15), superseded_by=seed.id,
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=False
    )
    # `seed` is eligible, the superseded one is not.
    assert result.promoted == 1
    assert result.reasons["superseded"] == 1


@pytest.mark.asyncio
async def test_skips_low_confidence_when_no_override(db_session: AsyncSession):
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _add_note(
        db_session, project=project, user=user,
        content=_content(15), confidence=0.5,
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id,
        min_confidence=0.85, dry_run=False,
    )
    assert result.promoted == 0
    assert result.reasons["low_confidence"] == 1


@pytest.mark.asyncio
async def test_set_confidence_overrides_low_confidence(db_session: AsyncSession):
    """When the caller passes set_confidence, the existing low confidence
    is ignored AND the entry's confidence is bumped to the new value."""
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    entry = await _add_note(
        db_session, project=project, user=user,
        content=_content(15), confidence=0.5,
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id,
        set_confidence=0.9, dry_run=False,
    )
    assert result.promoted == 1
    await db_session.refresh(entry)
    assert entry.claim_class == "claim"
    assert entry.confidence == 0.9


@pytest.mark.asyncio
async def test_entry_type_filter(db_session: AsyncSession):
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _add_note(
        db_session, project=project, user=user,
        content=_content(15), entry_type="decision",
    )
    await _add_note(
        db_session, project=project, user=user,
        content=_content(15), entry_type="pattern",
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id,
        entry_type="decision", dry_run=False,
    )
    assert result.promoted == 1
    assert result.reasons["wrong_type"] == 1


@pytest.mark.asyncio
async def test_skips_duplicate_of_existing_claim(db_session: AsyncSession):
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    # Pre-existing active claim with specific content
    await _add_note(
        db_session, project=project, user=user,
        content="the migration uses sqlalchemy core for raw sql performance",
        claim_class="claim",
    )
    # Near-duplicate note (same words, slight reordering)
    await _add_note(
        db_session, project=project, user=user,
        content="migration uses sqlalchemy core for raw sql performance the",
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=False
    )
    assert result.promoted == 0
    assert result.reasons["duplicate"] == 1


@pytest.mark.asyncio
async def test_per_reason_breakdown_aggregates_across_entries(
    db_session: AsyncSession,
):
    """One bulk run touching multiple skip reasons should produce a
    structured per-reason breakdown — that's the whole point of the
    feature for users debugging why their KB is stuck."""
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)

    # 2 eligible
    for _ in range(2):
        await _add_note(
            db_session, project=project, user=user, content=_content(15),
        )
    # 3 too-short
    for _ in range(3):
        await _add_note(
            db_session, project=project, user=user, content="x" * 10,
        )
    # 1 dismissed
    await _add_note(
        db_session, project=project, user=user,
        content=_content(15), dismissed=True,
    )
    # 1 low-confidence
    await _add_note(
        db_session, project=project, user=user,
        content=_content(15), confidence=0.3,
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=False
    )
    assert result.promoted == 2
    assert result.skipped == 5
    assert result.reasons["too_short"] == 3
    assert result.reasons["dismissed"] == 1
    assert result.reasons["low_confidence"] == 1


@pytest.mark.asyncio
async def test_skip_reason_keys_are_stable(db_session: AsyncSession):
    """The SKIP_REASONS tuple is the source of truth — every reason
    must be present (even with count 0) so callers can rely on the
    shape for rendering tables."""
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=True
    )
    for r in SKIP_REASONS:
        assert r in result.reasons, f"missing skip reason key: {r}"


@pytest.mark.asyncio
async def test_in_run_promoted_competes_for_duplicate(
    db_session: AsyncSession,
):
    """If two near-duplicate notes are both eligible in one bulk run,
    we should promote the first and skip the second as duplicate —
    not promote both and end up with two near-identical claims."""
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    base = "the migration uses sqlalchemy core for raw sql performance reasons"
    await _add_note(db_session, project=project, user=user, content=base)
    await _add_note(
        db_session, project=project, user=user, content=base + " more"
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=False
    )
    assert result.promoted == 1
    assert result.reasons["duplicate"] == 1


@pytest.mark.asyncio
async def test_dry_run_predicts_confirm_for_in_run_duplicates(
    db_session: AsyncSession,
):
    """Codex R1 MEDIUM on tk_03263e280f4b4732: dry-run MUST faithfully
    predict the confirmed run, including in-run duplicate suppression.
    Two near-duplicate eligible notes: BOTH dry-run AND confirm must
    return promoted=1, duplicate=1. The earlier implementation only
    appended to the comparison set on the not-dry-run path, so dry-run
    reported promoted=2 while confirm correctly reported 1 — breaking
    the inspect-then-mutate safety contract."""
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    base = "the migration uses sqlalchemy core for raw sql performance reasons"
    note1 = await _add_note(
        db_session, project=project, user=user, content=base
    )
    note2 = await _add_note(
        db_session, project=project, user=user, content=base + " more"
    )

    dry = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=True
    )
    assert dry.promoted == 1
    assert dry.reasons["duplicate"] == 1
    # First eligible entry wins; second is the duplicate.
    assert dry.promoted_ids == [note1.id]

    # Confirm path must report the same shape — that's the contract.
    await db_session.refresh(note2)
    confirmed = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=False
    )
    assert confirmed.promoted == dry.promoted
    assert confirmed.reasons["duplicate"] == dry.reasons["duplicate"]
    assert confirmed.promoted_ids == dry.promoted_ids


@pytest.mark.asyncio
async def test_to_dict_shape_for_json(db_session: AsyncSession):
    import json

    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _add_note(
        db_session, project=project, user=user, content=_content(15)
    )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=True
    )
    payload = json.dumps(result.to_dict())
    parsed = json.loads(payload)
    assert parsed["promoted"] == 1
    assert parsed["dry_run"] is True
    assert "reasons" in parsed
    assert isinstance(parsed["promoted_ids"], list)


@pytest.mark.asyncio
async def test_scales_to_hundreds(db_session: AsyncSession):
    """The whole point of this feature is repairing 270+ stuck notes.
    Verify the service handles that order of magnitude without N+1
    queries blowing up. Functional check, not benchmark."""
    user = await _make_user(db_session)
    project = await _make_project(db_session, user)
    for _ in range(300):
        await _add_note(
            db_session, project=project, user=user, content=_content(15),
        )

    result = await promote_eligible_notes(
        db_session, project.id, user_id=user.id, dry_run=False
    )
    # Most should promote; some may be filtered as duplicates because
    # word_overlap with 10 random uuid words can occasionally exceed
    # 0.85. Allow a generous floor.
    assert result.promoted >= 100
    assert result.promoted + result.skipped == 300

    # All promoted entries are actually claims now.
    rows = (
        await db_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.project_id == project.id,
                KnowledgeEntry.claim_class == "claim",
            )
        )
    ).scalars().all()
    assert len(rows) == result.promoted
