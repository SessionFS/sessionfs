"""Project-root resolution shared by `sfs rules`, `sfs skill` and SkillManager.

Project-scoped state (`.sessionfs/`, adapter skill roots) must live at the
repository top level: written under a subdirectory it would be invisible to
provenance capture and to the tools themselves.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def find_git_toplevel(start: Path | None = None) -> Path | None:
    """`git rev-parse --show-toplevel` for `start` (default: cwd); None outside a repo."""
    base = Path(start) if start is not None else Path.cwd()
    try:
        result = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top) if top else None


def resolve_project_root(start: Path | None = None) -> Path:
    """The git top level when inside a repository, else `start`/cwd itself."""
    base = Path(start) if start is not None else Path.cwd()
    return (find_git_toplevel(base) or base).resolve()
