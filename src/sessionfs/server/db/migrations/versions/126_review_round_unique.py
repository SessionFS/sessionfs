"""One recorded run per review round.

Revision ID: 126
Revises: 125

Adds a partial unique index on ``agent_runs`` over (ticket_id, role,
review_round, coalesce(workflow_step_id, '')) WHERE review_round IS NOT NULL
— the key record_review_round's idempotency check reads. PostgreSQL already
serialises concurrent rounds with a row lock; the index is the backstop on
SQLite (no row locks) and against any future writer that skips the lock.

Only ``record_review_round`` sets ``review_round``. If an existing database
already holds two runs for one round (only possible from a race on SQLite),
the index is not created: the colliding rows are printed for review and
nothing is deleted, because agent runs are audit records.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "126"
down_revision = "125"
branch_labels = None
depends_on = None

_INDEX = "uq_agent_run_review_round"


def upgrade() -> None:
    conn = op.get_bind()
    dupes = conn.execute(
        sa.text(
            "SELECT ticket_id, role, review_round, coalesce(workflow_step_id, ''), "
            "count(*) FROM agent_runs WHERE review_round IS NOT NULL "
            "GROUP BY ticket_id, role, review_round, coalesce(workflow_step_id, '') "
            "HAVING count(*) > 1"
        )
    ).all()
    if dupes:
        print(
            f"126: {len(dupes)} review round(s) recorded more than once; "
            f"{_INDEX} NOT created. Review these and re-run the index by hand:"
        )
        for ticket_id, role, review_round, step, count in dupes:
            print(f"  ticket={ticket_id} role={role} round={review_round} step={step!r} runs={count}")
        return
    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX} ON agent_runs "
        "(ticket_id, role, review_round, coalesce(workflow_step_id, '')) "
        "WHERE review_round IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
