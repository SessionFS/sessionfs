"""Enterprise Edition feature loading."""

from __future__ import annotations

import importlib
from types import ModuleType


def is_ee_available() -> bool:
    """Check if Enterprise Edition code is available."""
    try:
        import sessionfs_ee  # noqa: F401
        return True
    except ImportError:
        return False


def get_feature(feature_name: str) -> ModuleType | None:
    """Load a feature from ee/ if available, otherwise return None."""
    if not is_ee_available():
        return None
    try:
        return importlib.import_module(f"sessionfs_ee.{feature_name}")
    except ImportError:
        return None
