"""Unit tests for .release/sanitize_main.py.

`.release/` is stripped from the public main branch, so this test
file imports the module lazily and skips cleanly when the helper
isn't on disk (e.g. when the test suite runs on main itself, which
should never happen but defends against the case).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_HELPER_PATH = Path(__file__).resolve().parents[2] / ".release" / "sanitize_main.py"


def _load_helper():
    if not _HELPER_PATH.exists():
        pytest.skip(".release/sanitize_main.py not present (likely on main)")
    spec = importlib.util.spec_from_file_location("sanitize_main", _HELPER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def helper():
    return _load_helper()


def test_parse_private_paths_strips_comments_and_blanks(helper, tmp_path):
    manifest = tmp_path / "private-files.txt"
    manifest.write_text(
        """# header comment

# Section
.agents/

# trailing comment on entry
docs/positioning.md
docs/pricing.md  # inline comment

# blank line above
brand/

# another empty section follows
"""
    )
    paths = helper.parse_private_paths(manifest)
    assert paths == [
        ".agents/",
        "docs/positioning.md",
        "docs/pricing.md",
        "brand/",
    ]


def test_parse_private_paths_missing_file_raises(helper, tmp_path):
    with pytest.raises(FileNotFoundError):
        helper.parse_private_paths(tmp_path / "does-not-exist.txt")


def test_find_leaks_directory_pattern_matches_by_prefix(helper):
    tracked = {
        ".agents/atlas-backend.md",
        ".agents/scribe-docs.md",
        "src/main.py",
        "docs/positioning.md",
    }
    private = [".agents/", "docs/positioning.md"]
    leaks = helper.find_leaks(tracked, private)

    assert sorted(leaks.keys()) == [".agents/", "docs/positioning.md"]
    assert leaks[".agents/"] == [
        ".agents/atlas-backend.md",
        ".agents/scribe-docs.md",
    ]
    assert leaks["docs/positioning.md"] == ["docs/positioning.md"]


def test_find_leaks_file_pattern_does_not_match_prefix(helper):
    """`docs/positioning.md` should NOT match `docs/positioning.md.bak`.
    Defensive: file entries must be exact matches, not prefix matches —
    otherwise removing `CLAUDE.md` would also catch `CLAUDE.md.old`."""
    tracked = {
        "docs/positioning.md.bak",
        "CLAUDE.md.archive",
    }
    private = ["docs/positioning.md", "CLAUDE.md"]
    leaks = helper.find_leaks(tracked, private)
    assert leaks == {}


def test_find_leaks_returns_empty_when_clean(helper):
    """Public-only tracked set produces no leaks regardless of how many
    private patterns are configured."""
    tracked = {
        "src/sessionfs/__init__.py",
        "README.md",
        "pyproject.toml",
    }
    private = [".agents/", "src/spikes/", "DOGFOOD.md", "brand/"]
    leaks = helper.find_leaks(tracked, private)
    assert leaks == {}


def test_find_leaks_handles_mixed_dir_and_file_patterns(helper):
    """A typical private-files.txt mixes directory entries (with `/`)
    and file entries (without). Both must work in one call."""
    tracked = {
        ".agents/atlas-backend.md",
        "DOGFOOD.md",
        "src/main.py",
        ".release/sanitize_main.py",
        "CLAUDE.md",
    }
    private = [".agents/", "DOGFOOD.md", ".release/", "CLAUDE.md"]
    leaks = helper.find_leaks(tracked, private)
    assert sorted(leaks.keys()) == sorted(private)


def test_parse_private_paths_ignores_whitespace_only_lines(helper, tmp_path):
    """Lines that are nothing but whitespace must be filtered out
    before they become 'leak patterns' that match every tracked file."""
    manifest = tmp_path / "private-files.txt"
    manifest.write_text(
        "# header\n"
        ".agents/\n"
        "   \n"
        "\t\t\n"
        "DOGFOOD.md\n"
    )
    paths = helper.parse_private_paths(manifest)
    assert paths == [".agents/", "DOGFOOD.md"]


def test_inline_comments_in_path_lines_stripped(helper, tmp_path):
    """Entries like `docs/pricing.md  # marketing` must yield just the
    path, not the path + comment."""
    manifest = tmp_path / "private-files.txt"
    manifest.write_text("docs/pricing.md  # marketing\n.agents/  # internal personas\n")
    paths = helper.parse_private_paths(manifest)
    assert paths == ["docs/pricing.md", ".agents/"]
