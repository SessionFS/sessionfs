"""PostgreSQL: the external-contact route tests against a real database
(GitHub #125). Runs only with SFS_TEST_PG_URL.

Proves the insert order (contact flushed before its aliases, which have no
relationship()), the FOR UPDATE locks in merge/delete/patch, the partial
unique email index freed by a merge, and the erasure path on PostgreSQL.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from sessionfs.server.db.models import Base
from tests.server.integration.test_contacts_api import (  # noqa: F401 — collected here
    test_crud_same_404_and_email_masking,
    test_dedupe_and_dlp,
    test_link_on_verified_email_moves_open_tickets,
    test_merge_delete_export,
    test_merge_email_conflict,
    test_resolution_order_and_override,
    test_suggestions_apply_and_dismiss,
    test_ticket_assignment_to_contact,
)
from tests.server.integration.test_contacts_review_fixes import (  # noqa: F401 — collected here
    test_assignee_contact_revalidated_in_transaction,
    test_contact_email_dlp_block_mode,
    test_contact_email_dlp_redact_mode_refuses,
    test_erase_contact_guarded_delete,
    test_linked_or_merged_contact_not_assignable,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set"
)


@pytest.fixture
async def db_engine():
    engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
