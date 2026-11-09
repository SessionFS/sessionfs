"""Ordering for merge check posts (GitHub check runs and GitLab statuses).

Posts used to be last-writer-wins: an evaluation that read "ready" before a
newer CHANGES-REQUESTED verdict could send its success after the newer
evaluation's action_required. The protocol, per (project, provider, repo,
head sha) row in ``merge_check_posts``:

1. ``take_eval_seq`` bumps ``eval_seq`` right before readiness is computed.
   The UPDATE row-locks until the evaluating transaction commits (which is
   always before any outbound call), so evaluations of one commit are
   serialised and a higher sequence always read newer state.
2. Just before sending, ``PostGuard.claim`` advances ``posted_seq`` with a
   guarded UPDATE (``posted_seq <= seq``) in its own short transaction and
   stores what it is about to send. A post whose sequence is below the
   stored one is skipped; a retry of the same evaluation is allowed.
3. After a successful send, ``PostGuard.newest`` re-reads the row: when a
   newer evaluation claimed while this request was in flight (its request
   may have landed first), the newest stored state is re-sent, so the last
   request to land always carries the newest claimed state.

No DB lock is held across an HTTP call: the claim and the re-read each
commit before the caller sends anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import MergeCheckPost

logger = logging.getLogger("sessionfs.merge_checks")

# How many times a poster re-sends the newest state after finding its own
# request was overtaken. Each round needs a newer evaluation to claim while
# the previous send was in flight, so this is rarely more than one.
REPAIR_ROUNDS = 3


def _key(project_id: str, provider: str, repo: str, head_sha: str) -> tuple:
    return (
        MergeCheckPost.project_id == project_id,
        MergeCheckPost.provider == provider,
        MergeCheckPost.repo == repo,
        MergeCheckPost.head_sha == head_sha,
    )


async def take_eval_seq(
    db: AsyncSession, *, project_id: str, provider: str, repo: str, head_sha: str
) -> int:
    """Take the next evaluation sequence for one commit, inside the caller's
    transaction (the row stays locked until the caller commits, which it does
    before any outbound call). Raises on a DB error."""
    where = _key(project_id, provider, repo, head_sha)
    for _ in range(3):
        result = await db.execute(
            update(MergeCheckPost)
            .where(*where)
            .values(
                eval_seq=MergeCheckPost.eval_seq + 1,
                updated_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            return int(
                (await db.execute(select(MergeCheckPost.eval_seq).where(*where))).scalar_one()
            )
        try:
            async with db.begin_nested():
                await db.execute(
                    insert(MergeCheckPost).values(
                        project_id=project_id,
                        provider=provider,
                        repo=repo,
                        head_sha=head_sha,
                        eval_seq=1,
                        posted_seq=0,
                    )
                )
            return 1
        except IntegrityError:
            # A concurrent evaluation created the row; bump it instead.
            continue
    raise RuntimeError("could not take a merge check evaluation sequence")


@dataclass
class PostGuard:
    """Compare-and-skip guard for one evaluation's post."""

    db: AsyncSession
    project_id: str
    provider: str
    repo: str
    head_sha: str
    seq: int

    async def claim(self, state: str, summary: str) -> bool:
        """Advance ``posted_seq`` to this evaluation's sequence unless a newer
        one already holds it. Commits. False means skip the post (a newer
        evaluation claimed it, or the row could not be updated)."""
        try:
            result = await self.db.execute(
                update(MergeCheckPost)
                .where(
                    *_key(self.project_id, self.provider, self.repo, self.head_sha),
                    MergeCheckPost.posted_seq <= self.seq,
                )
                .values(
                    posted_seq=self.seq,
                    posted_state=state,
                    posted_summary=summary,
                    updated_at=datetime.now(timezone.utc),
                )
                .execution_options(synchronize_session=False)
            )
            await self.db.commit()
        except Exception:
            logger.warning(
                "merge check: could not claim post for %s @ %s",
                self.repo,
                self.head_sha,
                exc_info=True,
            )
            await _safe_rollback(self.db)
            return False
        return result.rowcount == 1

    async def newest(self) -> Optional[tuple[int, str, str]]:
        """The newest claimed post ``(seq, state, summary)``, or None. Ends
        the read transaction before returning."""
        try:
            row = (
                await self.db.execute(
                    select(
                        MergeCheckPost.posted_seq,
                        MergeCheckPost.posted_state,
                        MergeCheckPost.posted_summary,
                    ).where(*_key(self.project_id, self.provider, self.repo, self.head_sha))
                )
            ).first()
            await self.db.commit()
        except Exception:
            logger.warning(
                "merge check: could not re-read post state for %s @ %s",
                self.repo,
                self.head_sha,
                exc_info=True,
            )
            await _safe_rollback(self.db)
            return None
        if row is None or row[1] is None:
            return None
        return int(row[0]), str(row[1]), str(row[2] or "")


async def _safe_rollback(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:
        pass
