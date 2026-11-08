"""Static guard: no server function adds an FK parent and a child that would
be inserted first in the same flush (tk_59579f33997047c8).

db/models.py declares no relationship(), so SQLAlchemy's unit of work orders
the INSERTs of different models by mapper sort key (module + class name), not
by foreign key. A child whose class name sorts before its parent's goes
first, and PostgreSQL rejects it (SQLite never enforces the FK, so the test
suite stays green). The safe forms are an ``await db.flush()`` (or any awaited
query, which autoflushes) between adding the parent and adding the child.

This walks every function under src/sessionfs/server and fails when one adds
both rows of such a pair with no awaited DB round trip between them. It is a
same-function check; tests/server/integration/test_flush_fk_order_pg.py runs
the riskiest guarded routes on PostgreSQL.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap

from sessionfs.server.db import models
from sessionfs.server.db.models import Base

SERVER = pathlib.Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server"

# Awaited calls that reach the database (a flush, or a statement that
# autoflushes pending rows first).
_ROUND_TRIPS = {
    "flush", "commit", "execute", "scalar", "scalars", "refresh", "rollback",
    "begin_nested", "run_sync", "stream", "stream_scalars",
}

# Foreign keys created only by migrations (PostgreSQL), not on the models.
_MIGRATION_ONLY_FKS = {("Ticket", "User")}


def _unsafe_pairs() -> dict[str, set[str]]:
    """parent class -> child classes the unit of work would insert first."""
    by_table = {m.local_table.name: m.class_ for m in Base.registry.mappers}

    def key(cls: type) -> str:
        return f"{cls.__module__}.{cls.__name__}"

    pairs: dict[str, set[str]] = {}
    for mapper in Base.registry.mappers:
        child = mapper.class_
        for fk in mapper.local_table.foreign_keys:
            parent = by_table.get(fk.column.table.name)
            if parent is None or parent is child:
                continue
            if key(child) < key(parent):
                pairs.setdefault(parent.__name__, set()).add(child.__name__)
    for child, parent in _MIGRATION_ONLY_FKS:
        pairs.setdefault(parent, set()).add(child)
    return pairs


def _model_names() -> dict[str, str]:
    """Every name code can build a model by (class names and module-level
    aliases such as ``MeetingTranscript``) -> the mapped class name."""
    mapped = {m.class_ for m in Base.registry.mappers}
    names = {cls.__name__: cls.__name__ for cls in mapped}
    for name, value in list(vars(models).items()):
        if isinstance(value, type) and value in mapped:
            names[name] = value.__name__
    return names


def _segments(fn: ast.AST, classes: dict[str, str]) -> list[list[tuple[int, str]]]:
    """The models a function adds, split at each awaited DB round trip. A
    ``var = Model(...)`` counts where ``var`` is added, not where it is built."""
    var_cls: dict[str, str] = {}
    assigned: set[int] = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in classes
        ):
            var_cls[node.targets[0].id] = classes[node.value.func.id]
            assigned.add(id(node.value))

    events: list[tuple[int, int, str, str]] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name) and func.id in classes:
                    if id(child) not in assigned:
                        events.append((child.lineno, child.col_offset, "add", classes[func.id]))
                elif isinstance(func, ast.Attribute) and func.attr in ("add", "add_all"):
                    for arg in child.args:
                        items = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
                        for item in items:
                            if isinstance(item, ast.Name) and item.id in var_cls:
                                events.append(
                                    (child.lineno, child.col_offset, "add", var_cls[item.id])
                                )
            if (
                isinstance(child, ast.Await)
                and isinstance(child.value, ast.Call)
                and isinstance(child.value.func, ast.Attribute)
                and child.value.func.attr in _ROUND_TRIPS
            ):
                events.append((child.lineno, child.col_offset, "trip", ""))
            visit(child)

    visit(fn)
    events.sort()
    segments: list[list[tuple[int, str]]] = [[]]
    for line, _col, kind, name in events:
        if kind == "trip":
            segments.append([])
        else:
            segments[-1].append((line, name))
    return segments


def _violations(
    tree: ast.AST, label: str, pairs: dict[str, set[str]], classes: dict[str, str]
) -> list[str]:
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for segment in _segments(fn, classes):
            names = {name for _line, name in segment}
            for parent in sorted(names & set(pairs)):
                for child in sorted(names & pairs[parent]):
                    line = min(ln for ln, name in segment if name == parent)
                    out.append(f"{label}:{line} {fn.name}: {parent} + {child} in one flush")
    return out


def test_no_parent_and_earlier_sorting_child_in_one_flush() -> None:
    pairs = _unsafe_pairs()
    classes = _model_names()
    found: list[str] = []
    for path in sorted(SERVER.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        rel = path.relative_to(SERVER.parents[1])
        found += _violations(ast.parse(path.read_text()), str(rel), pairs, classes)
    assert found == [], (
        "Add `await db.flush()` after the parent row (no relationship() orders "
        "these INSERTs on PostgreSQL):\n" + "\n".join(found)
    )


def test_the_guard_catches_the_meeting_extraction_bug() -> None:
    pairs = _unsafe_pairs()
    classes = _model_names()
    assert "MeetingActionItem" in pairs["MeetingParticipant"]
    bad = textwrap.dedent(
        """
        async def extract(db):
            p = MeetingParticipant(id="mp")
            db.add(p)
            db.add(MeetingActionItem(owner_participant_id=p.id))
            await db.commit()
        """
    )
    good = bad.replace("db.add(p)\n", "db.add(p)\n    await db.flush()\n")
    assert _violations(ast.parse(bad), "bad", pairs, classes)
    assert _violations(ast.parse(good), "good", pairs, classes) == []
    # A module-level alias builds the same model (chat upload's parent row).
    aliased = textwrap.dedent(
        """
        async def upload(db):
            m = MeetingTranscript(id="chat_x")
            db.add(m)
            db.add_all([ConversationMessageMeta(source_id=m.id, line_no=1)])
        """
    )
    assert _violations(ast.parse(aliased), "aliased", pairs, classes)
