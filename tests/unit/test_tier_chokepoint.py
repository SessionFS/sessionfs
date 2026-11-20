"""Static guard: a plan must never be built straight from a stored ``.tier``.

The Community keyless chokepoint lives in ``tier_gate.py``: every plan
resolution must pass through ``apply_edition_gating`` /
``resolve_org_plan_tier`` (or the batched bulk variant) so ``SFS_EDITION``
clamps the resolved plan. Constructing ``Tier(<row>.tier)`` (or
``Tier(<entitlement>.tier)``) anywhere else bypasses that floor — a keyless
Community org with leftover Team/Enterprise data would keep Team/Enterprise
features.

This test fails when a new ``Tier(<something>.tier)`` construction appears
outside the allowlist below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "sessionfs"

# A ``Tier(...)`` whose argument is a ``.tier`` attribute read off a stored
# row (``ent.tier``, ``org.tier``, ``user.tier``, ``license.tier``, ...).
_TIER_ATTRIBUTE_RE = re.compile(r"\bTier\(\s*[A-Za-z_][A-Za-z0-9_]*\.tier\s*\)")

# Legitimate exceptions — the only files allowed to read a stored ``.tier``
# column into a Tier. Each is the chokepoint, never a bypass.
_ALLOWED: dict[str, str] = {
    # The chokepoint itself: every ``Tier(ent.tier / org.tier / user.tier)``
    # here is immediately passed through apply_edition_gating (or the bulk
    # _apply_community_floor) before it is used.
    "src/sessionfs/server/tier_gate.py": "the edition-gating chokepoint",
    # The entitlement-resolution seam named by the review; it maps stored
    # entitlement rows and is the single read path the chokepoint consumes.
    "src/sessionfs/server/services/entitlements.py": "the entitlement resolution seam",
}


def _python_sources() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py")) if p.is_file()]


_SOURCES = _python_sources()


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_tier_never_built_from_a_stored_tier_column_outside_chokepoint(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()
    if rel in _ALLOWED:
        return
    for match in _TIER_ATTRIBUTE_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        pytest.fail(
            f"{rel}:{line} builds Tier({match.group(0)}) from a stored .tier "
            "column; route it through resolve_org_plan_tier / "
            "apply_edition_gating instead of bypassing the Community chokepoint"
        )
