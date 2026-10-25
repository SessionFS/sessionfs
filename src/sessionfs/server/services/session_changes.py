"""Derive per-file changes (diffs) from a session archive's messages.

The Changes tab renders a session's file edits as unified diffs. This module
reconstructs those diffs from the canonical ``tool_use`` blocks the converters
emit. It never reads the original workspace files — every byte comes from the
already-redacted archive, so redaction parity with the messages view is by
construction.

Only edit / create / delete / patch tools contribute entries; read-only tools
(Read / Grep / Glob / LS and their per-tool equivalents) are deliberately
ignored via an explicit allowlist, so a session that only browsed the tree
shows "no file changes".

See ``docs/session-changes-capture.md`` for the per-tool input contract.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

MAX_FILES = 200
MAX_DIFF_BYTES = 1_000_000  # 1 MB of diff text (UTF-8 bytes) across all files
MAX_DIFF_LINES = 2_000  # per-side line cap before we refuse difflib
MAX_DIFF_SIDE_BYTES = 200_000  # per-side byte cap before we refuse difflib
MAX_DIFF_INPUT_BYTES = 5_000_000  # 5 MB of difflib input (old+new UTF-8 bytes) per session
MAX_DIFFED_EDITS = 1_000  # max number of edits actually diffed per session
MAX_WRITE_CONTENT_LINES = 2_000
_TRUNCATION_NOTE = "\n\n… diff truncated …"
_DIFF_TOO_LARGE_PREFIX = "diff too large"
_DIFF_NOT_AVAILABLE = "diff not available"

# Cline/Roo ``replace_in_file`` SEARCH/REPLACE block markers.
_SEARCH_MARK = "<<<<<<< SEARCH"
_REPLACE_MARK = ">>>>>>> REPLACE"
_SEPARATOR = "======="

# name -> (path keys, old_string keys, new_string keys) for single-edit tools.
_EDIT_SPECS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "Edit": (("file_path", "path"), ("old_string", "old_str"), ("new_string", "new_str")),
    "edit": (("path", "file_path"), ("old_string", "old_str"), ("new_string", "new_str")),
    "edit_file": (("path", "file_path"), ("old_str", "old_string"), ("new_str", "new_string")),
    "replace_file_content": (
        ("TargetFile", "file_path", "path"),
        ("OldString", "old_string"),
        ("NewString", "new_string"),
    ),
}

# name -> list-of-edits keys for multi-edit tools.
_MULTI_EDIT_SPECS: dict[str, tuple[str, ...]] = {
    "MultiEdit": ("edits",),
    "multi_replace_file_content": ("Replacements", "Edits"),
}

# name -> (path keys, content keys) for create tools.
_CREATE_SPECS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Write": (("file_path", "path"), ("content",)),
    "WriteFile": (("path", "file_path"), ("content",)),
    "create": (("file_path", "path"), ("content", "file_text")),
    "create_file": (("file_path", "path"), ("content",)),
    "write_file": (("file_path", "path"), ("content",)),
    "newFileCreated": (("path", "file_path"), ("content",)),
    "write_to_file": (("TargetFile", "file_path", "path"), ("CodeContent", "content")),
}


@dataclass
class _Op:
    """A single file-touching operation, expanded from a tool_use block."""

    path: str
    idx: int
    kind: str  # "edit" | "write" | "patch" | "unverified" | "delete" | "move"
    old: str = ""
    new: str = ""
    content: str = ""
    patch: str = ""


class _Budget:
    """A byte budget shared across all files in one derivation."""

    __slots__ = ("remaining",)

    def __init__(self, total: int) -> None:
        self.remaining = total

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


class _DiffWorkBudget:
    """A session-wide budget capping the CPU spent inside difflib.

    The 1 MB output budget counts rendered diff bytes, so it does not bound
    difflib *input*: thousands of large edits each pass through quadratic
    SequenceMatcher before their output is truncated. This budget bounds both
    the total input bytes and the number of edits that actually run difflib;
    once either is exhausted, remaining edits emit the "diff too large"
    placeholder instead of diffing.
    """

    __slots__ = ("input_bytes_remaining", "edits_remaining")

    def __init__(self, input_bytes: int, edits: int) -> None:
        self.input_bytes_remaining = input_bytes
        self.edits_remaining = edits

    @property
    def exhausted(self) -> bool:
        return self.input_bytes_remaining <= 0 or self.edits_remaining <= 0

    def consume(self, input_bytes: int) -> None:
        self.input_bytes_remaining -= input_bytes
        self.edits_remaining -= 1


def derive_session_changes(
    messages: list[dict[str, Any]], workspace: dict[str, Any] | None
) -> dict[str, Any]:
    """Derive per-file changes from a session's messages and workspace.

    Returns a dict with keys ``files`` (list of file-change dicts in
    first-touched order), ``git_ref`` (``branch@commit_sha`` or ``None``), and
    ``truncated`` (True when the 200-file or 1 MB caps were hit).

    Diffs are computed lazily during rendering (after the file cap and byte
    budget are applied), so an over-long session never spends CPU diffing files
    that will be dropped anyway.
    """
    root_path = _workspace_root(workspace)
    order: list[str] = []
    accum: dict[str, list[_Op]] = {}

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            if not isinstance(name, str):
                continue
            inp = block.get("input", {})
            if not isinstance(inp, dict):
                continue
            for op in _expand_tool_use(name, inp, idx, root_path):
                ops = accum.get(op.path)
                if ops is None:
                    ops = []
                    accum[op.path] = ops
                    order.append(op.path)
                ops.append(op)

    # Deletions have no structured tool call — recover them from workspace roles.
    workspace_roles = {
        _norm(path, root_path): role
        for path, role in _workspace_file_roles(workspace).items()
    }
    for path, role in workspace_roles.items():
        if role == "deleted" and path not in accum:
            accum[path] = []
            order.append(path)

    truncated = len(order) > MAX_FILES
    budget = _Budget(MAX_DIFF_BYTES)
    work_budget = _DiffWorkBudget(MAX_DIFF_INPUT_BYTES, MAX_DIFFED_EDITS)
    files: list[dict[str, Any]] = []

    for path in order[:MAX_FILES]:
        files.append(
            _render_file(
                path, accum.get(path, []), workspace_roles.get(path), budget, work_budget
            )
        )
        if budget.exhausted:
            truncated = True
            break

    if work_budget.exhausted:
        truncated = True

    return {"files": files, "git_ref": _git_ref(workspace), "truncated": truncated}


def _expand_tool_use(
    name: str, inp: dict[str, Any], idx: int, root_path: str | None
) -> list[_Op]:
    """Expand one tool_use block into zero or more file operations."""
    if name == "apply_patch":
        return _expand_apply_patch(inp, idx, root_path)
    if name == "replace_in_file":
        return _expand_replace_in_file(inp, idx, root_path)
    if name == "editedExistingFile":
        return _expand_edited_existing_file(inp, idx, root_path)

    if name in _MULTI_EDIT_SPECS:
        ops = _expand_multi_edit(inp, idx, root_path, _MULTI_EDIT_SPECS[name])
        if ops:
            return ops
        if name in _EDIT_SPECS:
            return _expand_edit(inp, idx, root_path, name)
        return []

    if name in _EDIT_SPECS:
        return _expand_edit(inp, idx, root_path, name)

    if name in _CREATE_SPECS:
        path_keys, content_keys = _CREATE_SPECS[name]
        path = _first_str(inp, *path_keys)
        if not path:
            return []
        content = _first_str(inp, *content_keys) or ""
        return [_Op(_norm(path, root_path), idx, "write", content=content)]

    return []


def _expand_edit(inp: dict[str, Any], idx: int, root_path: str | None, name: str) -> list[_Op]:
    path_keys, old_keys, new_keys = _EDIT_SPECS[name]
    path = _first_str(inp, *path_keys)
    if not path:
        return []
    old = _first_str(inp, *old_keys) or ""
    new = _first_str(inp, *new_keys) or ""
    return [_Op(_norm(path, root_path), idx, "edit", old=old, new=new)]


def _expand_multi_edit(
    inp: dict[str, Any], idx: int, root_path: str | None, list_keys: tuple[str, ...]
) -> list[_Op]:
    path = _first_str(inp, "file_path", "path", "TargetFile")
    if not path:
        return []
    for list_key in list_keys:
        items = inp.get(list_key)
        if not isinstance(items, list):
            continue
        ops: list[_Op] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            old = _first_str(item, "old_string", "old_str", "OldString") or ""
            new = _first_str(item, "new_string", "new_str", "NewString") or ""
            ops.append(_Op(_norm(path, root_path), idx, "edit", old=old, new=new))
        if ops:
            return ops
    return []


def _expand_replace_in_file(
    inp: dict[str, Any], idx: int, root_path: str | None
) -> list[_Op]:
    """Expand Cline/Roo ``replace_in_file`` into per-pair Edit ops.

    The tool's ``diff`` field is a SEARCH/REPLACE block (``<<<<<<< SEARCH`` /
    ``=======`` / ``>>>>>>> REPLACE``). Each pair is diffed like an ``Edit`` so
    the same caps and work budget apply.
    """
    path = _first_str(inp, "path", "file_path", "TargetFile")
    if not path:
        return []
    raw = _first_str(inp, "diff", "content") or ""
    if not raw:
        return []
    return [
        _Op(_norm(path, root_path), idx, "edit", old=old, new=new)
        for old, new in _parse_search_replace(raw)
    ]


def _expand_edited_existing_file(
    inp: dict[str, Any], idx: int, root_path: str | None
) -> list[_Op]:
    """Expand the Cline/Roo ui_messages fallback ``editedExistingFile``.

    Its ``diff`` field is rendered verbatim only when it is a real unified diff;
    otherwise the changes tab shows "diff not available".
    """
    path = _first_str(inp, "path", "file_path")
    if not path:
        return []
    diff = _first_str(inp, "diff") or ""
    if not diff:
        return []
    return [_Op(_norm(path, root_path), idx, "unverified", patch=diff)]


def _parse_search_replace(text: str) -> list[tuple[str, str]]:
    """Split a ``replace_in_file`` diff into (search, replace) pairs.

    The block format is::

        <<<<<<< SEARCH
        <old lines>
        =======
        <new lines>
        >>>>>>> REPLACE

    Several pairs may appear in one diff. Marker lines are matched exactly
    (modulo surrounding whitespace); content lines are preserved verbatim.
    """
    pairs: list[tuple[str, str]] = []
    old: list[str] = []
    new: list[str] = []
    in_block = False
    in_new = False

    def flush() -> None:
        nonlocal old, new, in_block, in_new
        if in_block and (old or new):
            pairs.append(("\n".join(old), "\n".join(new)))
        old = []
        new = []
        in_block = False
        in_new = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _SEARCH_MARK:
            flush()
            in_block = True
            in_new = False
        elif stripped == _SEPARATOR:
            in_new = True
        elif stripped == _REPLACE_MARK:
            flush()
        elif in_block:
            (new if in_new else old).append(line)
    flush()
    return pairs


def _is_unified_diff(text: str) -> bool:
    """True when ``text`` carries both ``--- `` and ``+++ `` diff headers."""
    has_minus = has_plus = False
    for line in text.splitlines():
        if line.startswith("--- "):
            has_minus = True
        elif line.startswith("+++ "):
            has_plus = True
    return has_minus and has_plus


def _expand_apply_patch(inp: dict[str, Any], idx: int, root_path: str | None) -> list[_Op]:
    # Codex captures apply_patch via custom_tool_call, whose raw input is a
    # single ``*** Begin Patch`` string. Legacy function_call apply_patch uses a
    # ``patch`` unified-diff field instead.
    raw = inp.get("input")
    if isinstance(raw, str):
        raw = _sanitize(raw)
        if raw.lstrip().startswith("*** Begin Patch"):
            return _expand_codex_patch(raw, idx, root_path)

    patch = _as_str(inp.get("patch"))
    if patch:
        path = _first_str(inp, "file_path", "path") or _path_from_patch(patch)
        if path:
            return [_Op(_norm(path, root_path), idx, "patch", patch=patch)]
    return []


def _expand_codex_patch(raw: str, idx: int, root_path: str | None) -> list[_Op]:
    """Parse Codex's ``*** Begin Patch`` format into per-file operations.

    Sections: ``*** Add File:`` (created, ``+``-prefixed lines), ``*** Update
    File:`` (edited hunks rendered as a unified diff), ``*** Delete File:``
    (deleted), and ``*** Move File:`` / ``*** Move to:`` (recorded as a
    rename-style diff with operation ``unknown``).
    """
    ops: list[_Op] = []
    cur_path: str | None = None
    cur_kind: str | None = None
    cur_lines: list[str] = []
    move_target: str | None = None

    def flush() -> None:
        nonlocal cur_path, cur_kind, cur_lines, move_target
        if cur_path is None or cur_kind is None:
            cur_path = None
            cur_kind = None
            cur_lines = []
            move_target = None
            return
        path = _norm(cur_path, root_path)
        if cur_kind == "add":
            content = "\n".join(_strip_plus(line) for line in cur_lines)
            ops.append(_Op(path=path, idx=idx, kind="write", content=content))
        elif cur_kind == "delete":
            ops.append(_Op(path=path, idx=idx, kind="delete"))
        elif cur_kind == "update":
            target = _norm(move_target, root_path) if move_target else path
            header = [f"--- a/{path}", f"+++ b/{target}"]
            ops.append(_Op(path=path, idx=idx, kind="patch", patch="\n".join(header + cur_lines)))
        elif cur_kind == "move":
            target = _norm(move_target or cur_path, root_path)
            ops.append(_Op(path=target, idx=idx, kind="move", old=path))
        cur_path = None
        cur_kind = None
        cur_lines = []
        move_target = None

    for line in raw.splitlines():
        # Markers only count when they START at column 0. Codex hunk bodies
        # (context lines like `` *** End Patch``, ``+``, ``-``, ``@@``) must not
        # be mistaken for section markers just because they contain a ``***``.
        if line.startswith("*** "):
            stripped = line.strip()
            if stripped in ("*** Begin Patch", "*** End Patch"):
                if stripped == "*** End Patch":
                    flush()
                continue
            if stripped.startswith("*** Add File:"):
                flush()
                cur_kind = "add"
                cur_path = _after_colon(stripped)
            elif stripped.startswith("*** Update File:"):
                flush()
                cur_kind = "update"
                cur_path = _after_colon(stripped)
            elif stripped.startswith("*** Delete File:"):
                flush()
                cur_kind = "delete"
                cur_path = _after_colon(stripped)
            elif stripped.startswith("*** Move File:"):
                flush()
                cur_kind = "move"
                cur_path = _after_colon(stripped)
            elif stripped.startswith("*** Move to:"):
                move_target = _after_colon(stripped)
            else:
                cur_lines.append(line)
        else:
            cur_lines.append(line)
    flush()
    return [op for op in ops if op.path]


def _render_file(
    path: str,
    ops: list[_Op],
    workspace_role: str | None,
    budget: _Budget,
    work_budget: _DiffWorkBudget,
) -> dict[str, Any]:
    segments: list[str] = []
    added = 0
    removed = 0
    message_indexes: list[int] = []
    created = edited = deleted = False
    first_kind: str | None = None
    file_truncated = False

    for op in ops:
        if budget.exhausted:
            file_truncated = True
            break
        if first_kind is None:
            first_kind = op.kind
        text, a, r, flag, op_truncated = _render_op(op, work_budget)
        if op_truncated:
            file_truncated = True
        nbytes = _utf8_len(text) if text else 0
        if text and nbytes > budget.remaining:
            text = _truncate_utf8(text, budget.remaining) + _TRUNCATION_NOTE
            budget.remaining = 0
            file_truncated = True
        else:
            budget.remaining -= nbytes
        if text:
            segments.append(text)
        added += a
        removed += r
        if flag == "created":
            created = True
        elif flag == "edited":
            edited = True
        elif flag == "deleted":
            deleted = True
        if op.idx not in message_indexes:
            message_indexes.append(op.idx)

    operation = _resolve_operation(first_kind, created, edited, deleted, workspace_role)
    return {
        "path": path,
        "operation": operation,
        "diff": "\n".join(segments),
        "added_lines": added,
        "removed_lines": removed,
        "message_indexes": message_indexes,
        "truncated": file_truncated,
    }


def _render_op(
    op: _Op, work_budget: _DiffWorkBudget
) -> tuple[str, int, int, str, bool]:
    """Render an op to (diff text, added, removed, flag, truncated)."""
    if op.kind == "edit":
        lines, added, removed, truncated = _capped_diff(op.path, op.old, op.new, work_budget)
        return "\n".join(lines), added, removed, "edited", truncated
    if op.kind == "write":
        text, added, truncated = _render_created(op.path, op.content)
        return text, added, 0, "created", truncated
    if op.kind == "patch":
        added, removed = _diff_counts(op.patch.splitlines())
        return op.patch, added, removed, "edited", False
    if op.kind == "unverified":
        if _is_unified_diff(op.patch):
            added, removed = _diff_counts(op.patch.splitlines())
            return op.patch, added, removed, "edited", False
        return _DIFF_NOT_AVAILABLE, 0, 0, "edited", False
    if op.kind == "delete":
        return "", 0, 0, "deleted", False
    if op.kind == "move":
        return f"--- a/{op.old}\n+++ b/{op.path}", 0, 0, "unknown", False
    return "", 0, 0, "unknown", False


def _capped_diff(
    path: str, old: str, new: str, work_budget: _DiffWorkBudget
) -> tuple[list[str], int, int, bool]:
    """Return a unified diff of old → new, refusing difflib above the caps.

    difflib's SequenceMatcher is quadratic; a single multi-thousand-line edit
    would block the event loop. When either side exceeds the line or byte cap, or
    the session-wide work budget (total difflib input bytes / edits) is spent, we
    emit a short placeholder instead of diffing.
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    if len(old_lines) > MAX_DIFF_LINES or len(new_lines) > MAX_DIFF_LINES:
        return [_too_large_note(len(old_lines), len(new_lines))], len(new_lines), len(old_lines), True
    if len(old.encode("utf-8")) > MAX_DIFF_SIDE_BYTES or len(new.encode("utf-8")) > MAX_DIFF_SIDE_BYTES:
        return [_too_large_note(len(old_lines), len(new_lines))], len(new_lines), len(old_lines), True

    input_bytes = len(old.encode("utf-8")) + len(new.encode("utf-8"))
    if work_budget.exhausted or input_bytes > work_budget.input_bytes_remaining:
        # Once an edit no longer fits, the input budget is spent: every
        # remaining edit is a placeholder, whatever its size.
        work_budget.input_bytes_remaining = 0
        return [_too_large_note(len(old_lines), len(new_lines))], len(new_lines), len(old_lines), True
    work_budget.consume(input_bytes)

    lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    added, removed = _diff_counts(lines)
    return lines, added, removed, False


def _too_large_note(old_lines: int, new_lines: int) -> str:
    return f"{_DIFF_TOO_LARGE_PREFIX}: -{old_lines}/+{new_lines} lines"


def _render_created(path: str, content: str) -> tuple[str, int, bool]:
    """Render a create as a proper ``--- /dev/null`` unified diff."""
    lines = content.splitlines()
    truncated = len(lines) > MAX_WRITE_CONTENT_LINES
    if truncated:
        lines = lines[:MAX_WRITE_CONTENT_LINES]
    text = "\n".join(
        ["--- /dev/null", f"+++ b/{path}"] + [f"+{line}" for line in lines]
    )
    if truncated:
        text += _TRUNCATION_NOTE
    return text, len(lines), truncated


def _diff_counts(diff_lines: list[str]) -> tuple[int, int]:
    """Count added/removed lines in a unified diff, ignoring ---/+++ headers."""
    added = 0
    removed = 0
    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _resolve_operation(
    first_kind: str | None,
    created: bool,
    edited: bool,
    deleted: bool,
    workspace_role: str | None,
) -> str:
    if workspace_role == "deleted":
        return "deleted"
    if first_kind == "write":
        # A file whose first touch was a create stays "created" even if a later
        # Edit followed (Write-then-Edit is still a brand-new file).
        return "created"
    if deleted:
        return "deleted"
    if edited:
        return "edited"
    if created:
        return "created"
    if first_kind == "move":
        return "unknown"
    if workspace_role == "created":
        return "created"
    if workspace_role in ("edited", "written"):
        return "edited"
    return "unknown"


def _sanitize(text: str) -> str:
    """Replace lone surrogates (U+D800–U+DFFF) with U+FFFD.

    JSON can carry lone surrogates (e.g. ``"a\\ud800b"``) that Python's
    ``json`` decoder leaves as a surrogate code point in the ``str``. Such a
    string cannot be encoded to UTF-8, so any downstream ``encode("utf-8")``
    raises ``UnicodeEncodeError`` and 500s the endpoint. The UTF-16 round-trip
    maps each lone surrogate to U+FFFD while leaving every other character
    intact. The fast path keeps the common (already-encodable) case cheap.
    """
    if not text:
        return text
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _first_str(inp: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = inp.get(key)
        if isinstance(value, str) and value:
            return _sanitize(value)
    return None


def _as_str(value: Any) -> str:
    if isinstance(value, str):
        return _sanitize(value)
    return _sanitize("" if value is None else str(value))


def _strip_plus(line: str) -> str:
    return line[1:] if line.startswith("+") else line


def _after_colon(line: str) -> str:
    return line.split(":", 1)[1].strip() if ":" in line else ""


def _norm(path: str, root_path: str | None) -> str:
    """Relativize an absolute path under the workspace root, else return it."""
    if not root_path or not path or not path.startswith("/"):
        return path
    root = str(root_path).rstrip("/")
    if not root:
        return path
    prefix = root + "/"
    if path == root:
        return path
    if path.startswith(prefix):
        return path[len(prefix):]
    return path


def _workspace_root(workspace: dict[str, Any] | None) -> str | None:
    if not isinstance(workspace, dict):
        return None
    root = workspace.get("root_path")
    return _sanitize(root) if isinstance(root, str) and root else None


def _path_from_patch(patch: str) -> str | None:
    """Extract the target path from a unified patch's ``+++ b/<path>`` header."""
    for line in patch.splitlines():
        stripped = line.strip()
        if stripped.startswith("+++ b/"):
            return stripped[len("+++ b/"):].split("\t", 1)[0].strip()
    return None


def _workspace_file_roles(workspace: dict[str, Any] | None) -> dict[str, str]:
    """Map workspace-relative path → role from workspace.json's files list."""
    if not isinstance(workspace, dict):
        return {}
    files = workspace.get("files", [])
    roles: dict[str, str] = {}
    if not isinstance(files, list):
        return roles
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        role = entry.get("role")
        if isinstance(path, str) and isinstance(role, str):
            roles[_sanitize(path)] = _sanitize(role)
    return roles


def _git_ref(workspace: dict[str, Any] | None) -> str | None:
    if not isinstance(workspace, dict):
        return None
    git = workspace.get("git")
    if not isinstance(git, dict):
        return None
    branch = git.get("branch")
    sha = git.get("commit_sha")
    if isinstance(branch, str):
        branch = _sanitize(branch)
    if isinstance(sha, str):
        sha = _sanitize(sha)
    if isinstance(branch, str) and branch and isinstance(sha, str) and sha:
        return f"{branch}@{sha}"
    if isinstance(branch, str) and branch:
        return branch
    if isinstance(sha, str) and sha:
        return sha
    return None


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate text to at most max_bytes without splitting a UTF-8 sequence."""
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""
