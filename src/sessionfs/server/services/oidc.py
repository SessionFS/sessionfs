"""OIDC SSO foundation helpers — atomic primitives with no route coupling.

tk_c2cdbe7114804403 (SSO-P1).  These are PURE DATA helpers used by the
P2/P3 routes.  They import from db/models only; they do NOT import from
routes, auth, or tier_gate.  Routes are the callers, never the callees.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import OidcLoginAttempt


async def consume_login_attempt(
    db: AsyncSession, *, state: str
) -> OidcLoginAttempt | None:
    """Atomically consume a pending OIDC login attempt by its `state`.

    Rowcount-1 guard — the CSRF/replay defense.  Two concurrent callbacks
    with the same `state` cannot both consume the row: exactly one UPDATE
    qualifies (status='pending', not expired), and the rowcount-1 check
    rejects the loser.

    Returns the consumed row on success, or None if the state is missing,
    already consumed, or expired.

    This mirrors the ActivationAttempt consume pattern in
    routes/activation.py:620-634.
    """
    now = datetime.now(timezone.utc)

    # 1. Atomic consume — rowcount-1 guard.
    result = await db.execute(
        update(OidcLoginAttempt)
        .where(
            OidcLoginAttempt.state == state,
            OidcLoginAttempt.status == "pending",
            OidcLoginAttempt.expires_at > now,
        )
        .values(status="consumed", consumed_at=now)
    )
    if result.rowcount != 1:
        return None

    # 2. Re-read the consumed row (could be done via RETURNING on PG,
    #    but SELECT is cross-DB-safe).  The state column is unique so
    #    this is a single-row fetch.
    row = await db.execute(
        select(OidcLoginAttempt).where(OidcLoginAttempt.state == state)
    )
    return row.scalar_one_or_none()
