"""Enterprise marker convention: every Enterprise source file is marked.

Every ``.py``/``.ts``/``.tsx`` file under ``ee/`` and ``dashboard/src/ee/``
must carry the literal ``SESSIONFS-ENTERPRISE-ONLY`` in a comment. The
public-artifact checker (``.github/scripts/check_public_artifacts.py``) keys
off that marker, so a missing marker means an Enterprise file could slip into
a Community build without being caught. The whole module skips when neither
tree is present (a Community-only checkout has no ``ee/``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EE_ROOTS = [ROOT / "ee", ROOT / "dashboard" / "src" / "ee"]
MARKER = "SESSIONFS-ENTERPRISE-ONLY"
SUFFIXES = {".py", ".ts", ".tsx"}


def _enterprise_source_files() -> list[Path]:
    files: list[Path] = []
    for root in EE_ROOTS:
        if root.is_dir():
            files.extend(
                sorted(
                    p for p in root.rglob("*") if p.is_file() and p.suffix in SUFFIXES
                )
            )
    return files


_EE_FILES = _enterprise_source_files()

if not _EE_FILES:
    pytest.skip("no ee/ or dashboard/src/ee/ sources present", allow_module_level=True)


@pytest.mark.parametrize("path", _EE_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_enterprise_source_has_marker(path: Path) -> None:
    assert MARKER in path.read_text(encoding="utf-8"), (
        f"{path.relative_to(ROOT)} must contain the {MARKER!r} marker in a comment"
    )
