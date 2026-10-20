"""Every ``AdminAction(target_type=...)`` literal in src/ fits its column.

``admin_actions.target_type`` is VARCHAR(20). SQLite does not enforce the
length, but PostgreSQL rejects a longer value and the whole transaction
(e.g. an admin rescan and its audit row) rolls back.
"""

from __future__ import annotations

import ast
from pathlib import Path

from sessionfs.server.db.models import AdminAction

SRC = Path(__file__).resolve().parents[2] / "src"


def _literals(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _literals(node.body) + _literals(node.orelse)
    return []


def _admin_action_target_types() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if name != "AdminAction":
                continue
            for keyword in node.keywords:
                if keyword.arg == "target_type":
                    for value in _literals(keyword.value):
                        found.append((str(path.relative_to(SRC)), node.lineno, value))
    return found


def test_admin_action_target_type_literals_fit_the_column() -> None:
    limit = AdminAction.__table__.c.target_type.type.length
    found = _admin_action_target_types()
    assert found, "no AdminAction(target_type=...) literals found"
    too_long = [(path, line, value) for path, line, value in found if len(value) > limit]
    assert too_long == [], f"longer than VARCHAR({limit}): {too_long}"
