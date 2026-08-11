"""Tests for the install/install.sh curl installer."""

import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INSTALL_SH = REPO_ROOT / "install" / "install.sh"
SITE_INSTALL_SH = REPO_ROOT / "site" / "public" / "install.sh"


def _has_shellcheck():
    """Return True if shellcheck is on PATH and executable."""
    try:
        subprocess.run(
            ["shellcheck", "--version"],
            capture_output=True,
            timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


class TestInstallShSyntax:
    """Verify install.sh passes basic shell syntax checks."""

    def test_syntax_check_with_sh_dash_n(self):
        """install.sh must pass `sh -n` (POSIX syntax check)."""
        result = subprocess.run(
            ["sh", "-n", str(INSTALL_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"sh -n failed on install.sh:\n{result.stderr}"
        )

    def test_shellcheck_if_available(self):
        """install.sh must pass shellcheck at warning+ severity."""
        if not _has_shellcheck():
            pytest.skip("shellcheck not on PATH")
        result = subprocess.run(
            ["shellcheck", "-S", "warning", str(INSTALL_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"shellcheck failed on install.sh:\n{result.stdout}\n{result.stderr}"
        )


class TestInstallShSiteSync:
    """Verify install/install.sh and site/public/install.sh are identical."""

    def test_files_are_byte_identical(self):
        """The synced copy in site/public must match the source of truth."""
        if not SITE_INSTALL_SH.exists():
            pytest.skip("site/public/install.sh not created yet")
        source = INSTALL_SH.read_bytes()
        site_copy = SITE_INSTALL_SH.read_bytes()
        assert source == site_copy, (
            "install/install.sh and site/public/install.sh differ. "
            "Run: cp install/install.sh site/public/install.sh"
        )
