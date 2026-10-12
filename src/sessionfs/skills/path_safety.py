"""Cross-platform path predicates for untrusted package operations."""

from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path


def is_link_or_reparse(path: Path) -> bool:
    """Return True for symlinks and Windows reparse points, failing closed."""
    try:
        if path.is_symlink():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(reparse_flag and attributes & reparse_flag)
    except FileNotFoundError:
        return False
    except OSError:
        return True


class CasefoldCollisionTracker:
    """Reject paths that collide on a case-insensitive filesystem.

    Covers file/file (``Notes.md`` vs ``notes.md``), dir/dir (``Docs/a`` vs
    ``docs/b``) and file/dir (``a`` vs ``a/x``) collisions so an import hashes
    identically on every host. Paths are POSIX-relative.

    Keys are Unicode-normalized (NFC) before casefolding: composed ``é`` and
    decomposed ``e + U+0301`` are one name on macOS (APFS/HFS+) but two on
    Linux, so treating them as distinct would make package contents
    host-dependent.
    """

    @staticmethod
    def key(value: str) -> str:
        return unicodedata.normalize("NFC", value).casefold()

    def __init__(self) -> None:
        self._dirs: dict[str, str] = {}
        self._files: dict[str, str] = {}

    def add(self, relative: str, *, is_dir: bool = False) -> None:
        parts = [part for part in relative.split("/") if part]
        prefix_parts: list[str] = []
        for index, part in enumerate(parts):
            prefix_parts.append(part)
            original = "/".join(prefix_parts)
            key = self.key(original)
            last = index == len(parts) - 1
            if last and not is_dir:
                if key in self._dirs:
                    raise ValueError(f"path collides case-insensitively (or after Unicode normalization) with a directory: {relative}")
                previous = self._files.get(key)
                if previous is not None:
                    raise ValueError(f"path collides case-insensitively (or after Unicode normalization): {relative}")
                self._files[key] = original
                continue
            if key in self._files:
                raise ValueError(f"path collides case-insensitively (or after Unicode normalization) with a file: {relative}")
            previous = self._dirs.get(key)
            if previous is None:
                self._dirs[key] = original
            elif previous != original:
                raise ValueError(f"directory collides case-insensitively (or after Unicode normalization): {relative}")
