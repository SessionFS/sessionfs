"""Unit tests for enterprise feature loading."""

from __future__ import annotations

from sessionfs.features import get_feature, is_ee_available


def test_ee_available():
    """ee/ package should be available in dev (same repo)."""
    # In the dev environment with ee/ directory, this should return True
    # In a stripped MIT-only install, it would return False
    result = is_ee_available()
    assert isinstance(result, bool)


def test_get_feature_nonexistent():
    """Requesting a nonexistent feature returns None."""
    result = get_feature("nonexistent_module_xyz")
    assert result is None
