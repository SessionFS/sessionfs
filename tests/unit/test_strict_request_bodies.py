"""New write routes must use a strict request body (tk_3855ef8b11f94ee2).

Routes that existed before the API-contract work keep accepting unknown
top-level fields (and report them in ``X-SessionFS-Ignored-Fields``), so
older clients don't break. They are listed in ``api_lenient_routes.txt``.
Any write route NOT in that list must forbid unknown fields: derive its body
from :class:`sessionfs.server.api_contract.StrictRequestModel` (or set
``model_config = ConfigDict(extra="forbid")``).

Regenerate the list only to REMOVE routes that became strict:
``PYTHONPATH=src python tests/unit/test_strict_request_bodies.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

BASELINE = Path(__file__).with_name("api_lenient_routes.txt")


def _lenient_routes() -> set[str]:
    from sessionfs.server.api_contract import lenient_body_keys
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.route_introspection import iter_api_routes

    app = create_app(ServerConfig(dashboard_dir="/nonexistent-sessionfs-dashboard"))
    out: set[str] = set()
    for entry in iter_api_routes(app):
        if lenient_body_keys(entry.route) is not None:
            for method in sorted(set(entry.methods) & {"POST", "PUT", "PATCH"}):
                out.add(f"{method} {entry.path}")
    return out


def _baseline() -> set[str]:
    return {
        line.strip()
        for line in BASELINE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def test_new_write_routes_use_strict_bodies():
    new = sorted(_lenient_routes() - _baseline())
    assert not new, (
        "These write routes silently drop unknown request fields. New routes must "
        "use sessionfs.server.api_contract.StrictRequestModel (extra='forbid') for "
        "their body:\n  " + "\n  ".join(new)
    )


def test_baseline_has_no_stale_entries():
    stale = sorted(_baseline() - _lenient_routes())
    assert not stale, (
        "These routes are strict now (or gone); remove them from "
        f"{BASELINE.name}:\n  " + "\n  ".join(stale)
    )


if __name__ == "__main__":  # pragma: no cover - maintenance helper
    header = "# Write routes that predate strict request bodies (tk_3855ef8b11f94ee2).\n"
    header += "# Only remove lines. New routes must use StrictRequestModel.\n"
    BASELINE.write_text(header + "\n".join(sorted(_lenient_routes())) + "\n")
