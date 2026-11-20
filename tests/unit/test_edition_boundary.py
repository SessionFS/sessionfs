"""Edition boundary: Community code never imports the Enterprise package.

The only place Community code may reference ``sessionfs_ee`` is
``src/sessionfs/features.py`` (the dynamic seam that loads features when the
Enterprise package is installed). Any other ``import sessionfs_ee`` or
``from sessionfs_ee`` would make the Community package hard-depend on ``ee/``
and break a Community-only install.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# The single permitted seam.
SEAM = SRC / "sessionfs" / "features.py"
# Research spikes are private (see .release/private-files.txt) and are not
# part of the Community package, so they sit outside this boundary.
PRIVATE_SPIKES = SRC / "spikes"


def _community_sources() -> list[Path]:
    return [
        p
        for p in sorted(SRC.rglob("*.py"))
        if p.is_file() and p != SEAM and PRIVATE_SPIKES not in p.parents
    ]


_SOURCES = _community_sources()


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_community_code_never_imports_sessionfs_ee(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "import sessionfs_ee" not in text, (
        f"{path.relative_to(ROOT)} imports the Enterprise package; only "
        "src/sessionfs/features.py may reference sessionfs_ee"
    )
    assert "from sessionfs_ee" not in text, (
        f"{path.relative_to(ROOT)} imports the Enterprise package; only "
        "src/sessionfs/features.py may reference sessionfs_ee"
    )
