"""Per-ticket trusted review comments for round counts (tk_86db8de1c3bf4bf0 #10).

The ship report and the model scoreboard count review rounds over many
tickets. They used one shared LIMIT 5000 (ship report) or no limit at all
(scoreboard), so one long thread could crowd every other ticket's rounds
out. The shared loader keeps trusted verdicts only, capped per ticket.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Project, Ticket, TicketComment, User
from sessionfs.server.services import releases
from sessionfs.server.services.review_state import (
    compute_review_state,
    load_trusted_review_comments_by_ticket,
)


def _verdict(n: int, clean: bool) -> str:
    label = "VERIFIED-CLEAN" if clean else "CHANGES REQUESTED"
    findings = "Findings: none." if clean else "Findings:\n • HIGH — thing harm: crash"
    return f"Reviewer R{n} review on tk_x: {label}\n\n{findings}"


async def _seed(db: AsyncSession) -> tuple[Project, Ticket, Ticket]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"rc-{uuid.uuid4().hex[:6]}@example.com",
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="rc",
        git_remote_normalized=f"acme/rc-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=user.id,
    )
    db.add(project)
    await db.flush()
    busy = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id, title="busy",
        created_by_user_id=user.id, status="done",
    )
    quiet = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id, title="quiet",
        created_by_user_id=user.id, status="done",
    )
    db.add_all([busy, quiet])
    await db.flush()
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    # The busy ticket: lots of untrusted chatter, then three trusted rounds.
    for i in range(30):
        db.add(TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}", ticket_id=busy.id, author_user_id=user.id,
            content=f"note {i}", verdict_trusted=False, created_at=base + timedelta(minutes=i),
        ))
    for n in (1, 2, 3):
        db.add(TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}", ticket_id=busy.id, author_user_id=user.id,
            author_persona="reviewer", content=_verdict(n, n == 3), verdict_trusted=True,
            created_at=base + timedelta(hours=n),
        ))
    # The quiet ticket's rounds come after all of the busy ticket's comments.
    for n in (1, 2):
        db.add(TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}", ticket_id=quiet.id, author_user_id=user.id,
            author_persona="reviewer", content=_verdict(n, n == 2), verdict_trusted=True,
            created_at=base + timedelta(days=1, hours=n),
        ))
    await db.commit()
    return project, busy, quiet


@pytest.mark.asyncio
async def test_loader_keeps_trusted_only_and_caps_per_ticket(db_session: AsyncSession):
    _, busy, quiet = await _seed(db_session)
    got = await load_trusted_review_comments_by_ticket(db_session, [busy.id, quiet.id])
    assert len(got[busy.id]) == 3 and len(got[quiet.id]) == 2
    assert all(c["verdict_trusted"] for c in got[busy.id])
    assert len(compute_review_state(got[busy.id]).rounds) == 3

    capped = await load_trusted_review_comments_by_ticket(
        db_session, [busy.id, quiet.id], cap=2
    )
    # The cap is per ticket and keeps the oldest rounds.
    assert len(capped[busy.id]) == 2 and len(capped[quiet.id]) == 2
    assert "R1 review" in capped[busy.id][0]["content"]
    assert await load_trusted_review_comments_by_ticket(db_session, []) == {}


@pytest.mark.asyncio
async def test_ship_report_counts_every_tickets_rounds(
    db_session: AsyncSession, monkeypatch
):
    """The ship report counts each ticket's trusted rounds through the
    per-ticket loader, regardless of untrusted chatter on other tickets."""
    from sessionfs.server.db.models import ReleaseTicket

    project, busy, quiet = await _seed(db_session)
    rel = await releases.record_release(
        db_session, project_id=project.id, version="1.0.0", tag_sha="a" * 40,
        released_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
    )
    for t in (busy, quiet):
        db_session.add(ReleaseTicket(release_id=rel["id"], ticket_id=t.id))
    await db_session.commit()
    report = await releases.ship_report(
        db_session, project_id=project.id, show_dollars=False, version="1.0.0"
    )
    assert report["review_rounds"] == 5
