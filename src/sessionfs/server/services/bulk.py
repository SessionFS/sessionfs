"""Shared rules for all-or-nothing bulk routes.

Every bulk route takes at most ``MAX_BULK_ITEMS`` items, refuses a request
that names the same target twice, runs every item through the single
route's own helper inside one transaction, and when any item is refused
writes nothing and answers 422 ``bulk_item_failed`` with the per-item
result list.
"""

from __future__ import annotations

from typing import Any, Iterable

from fastapi import HTTPException
from pydantic import BaseModel

MAX_BULK_ITEMS = 100

NOT_APPLIED = "Not applied because another item failed."


def check_bulk_size(count: int) -> None:
    """422 too_many_items above the cap."""
    if count > MAX_BULK_ITEMS:
        raise HTTPException(
            422,
            {
                "code": "too_many_items",
                "message": f"At most {MAX_BULK_ITEMS} items per request.",
                "max_items": MAX_BULK_ITEMS,
                "count": count,
            },
        )


def check_bulk_duplicates(keys: Iterable[str], what: str) -> None:
    """422 duplicate_items when a key appears more than once."""
    seen: set[str] = set()
    dupes: list[str] = []
    for key in keys:
        if key in seen and key not in dupes:
            dupes.append(key)
        seen.add(key)
    if dupes:
        raise HTTPException(
            422,
            {
                "code": "duplicate_items",
                "message": f"Each {what} may appear only once per request.",
                "duplicates": dupes[:20],
            },
        )


def bulk_refusal(results: list[Any]) -> HTTPException:
    """The all-or-nothing refusal: nothing was written; every item listed.

    Items that would have succeeded are marked ``ok: true`` with a note that
    they were not applied, so a caller can fix the failed ones and resend."""
    out: list[dict] = []
    for r in results:
        row = r.model_dump() if isinstance(r, BaseModel) else dict(r)
        if row.get("ok") and not row.get("message"):
            row["message"] = NOT_APPLIED
        out.append(row)
    return HTTPException(
        422,
        {
            "code": "bulk_item_failed",
            "message": "One or more items failed; nothing was changed.",
            "failed": sum(1 for row in out if not row.get("ok")),
            "results": out,
        },
    )
