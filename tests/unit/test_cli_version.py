"""`sfs --version` / `sfs -V` / `sfs version` (GitHub #85)."""

from __future__ import annotations

import platform
import re
import sys

from typer.testing import CliRunner

from sessionfs import __version__
from sessionfs.cli.main import app

runner = CliRunner()


def _expected() -> str:
    py = ".".join(str(part) for part in sys.version_info[:3])
    return f"sessionfs {__version__} (python {py}, {sys.platform} {platform.machine()})"


def test_version_flag_prints_version_and_exits_zero():
    for flag in ("--version", "-V"):
        result = runner.invoke(app, [flag])
        assert result.exit_code == 0, result.output
        assert result.output.strip() == _expected()


def test_version_flag_is_eager_and_skips_the_subcommand():
    result = runner.invoke(app, ["--version", "list"])
    assert result.exit_code == 0
    assert result.output.strip() == _expected()


def test_version_subcommand():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == _expected()


def test_bare_sfs_still_shows_help():
    result = runner.invoke(app, [])
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "Usage" in plain
    assert "--version" in plain
