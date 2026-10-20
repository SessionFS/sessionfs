"""Google Antigravity -> .sfs converter.

Antigravity (the desktop app, the IDE build and the ``agy`` CLI) keeps one
storage root per app under ``~/.gemini``:

    ~/.gemini/antigravity/       desktop app 2.x   (app_data_dir "antigravity")
    ~/.gemini/antigravity-cli/   agy CLI           (app_data_dir "antigravity-cli")
    ~/.gemini/antigravity-ide/   IDE build         (same layout, not yet seen)

Each root holds, per conversation ``<cid>``:

    brain/<cid>/.system_generated/logs/transcript_full.jsonl
        Plaintext JSONL, one object per step: step_index, source, type,
        status, created_at, content, thinking, tool_calls [{name, args}],
        input_tokens, output_tokens, cache_read_tokens. THE capture source.
    brain/<cid>/.system_generated/logs/transcript.jsonl
        Same steps; tool args may be stringified. Fallback only.
    brain/<cid>/.system_generated/logs/chunks/transcript_full/NNNNNNNN.jsonl
        Chunked copies. Long conversations may roll the top-level file over,
        so steps are merged from chunks + top-level by step_index (never
        double-counted).
    brain/<cid>/.system_generated/steps/<n>/output.txt
        Raw tool output for step n (used when longer than the transcript copy).
    annotations/<cid>.pbtxt          title (fallback)
    conversations/<cid>.db|.pb       protobuf / encrypted — never read.

Metadata comes from ``conversation_summaries.db`` (SQLite, table
``conversation_summaries``). One conversation may be listed in more than one
root's DB, so rows are keyed by ``(app_data_dir, conversation_id)`` and the
transcript is located under the root whose name matches ``app_data_dir``.
The DB is opened from a private copy (copy-on-read): SessionFS never writes
to, locks, or holds open a file the app is using.

Antigravity is capture-only — there is no write-back path.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from sessionfs.converters import antigravity_io as _io
from sessionfs.converters.antigravity_io import UnsafePath, is_conversation_id
from sessionfs.spec.version import SFS_CONVERTER_VERSION, SFS_FORMAT_VERSION

logger = logging.getLogger("sessionfs.converters.antigravity_to_sfs")

TOOL_NAME = "antigravity"

# Storage roots under ~/.gemini, in discovery order. The directory name is
# the app's ``app_data_dir`` value in conversation_summaries.
ROOT_NAMES: tuple[str, ...] = ("antigravity", "antigravity-cli", "antigravity-ide")

_APP_INFO: dict[str, tuple[str, str]] = {
    # app_data_dir: (display name, manifest source.interface)
    "antigravity": ("Antigravity", "desktop"),
    "antigravity-cli": ("Antigravity CLI (agy)", "cli"),
    "antigravity-ide": ("Antigravity IDE", "ide"),
}

LOGS_REL = Path(".system_generated") / "logs"
SUMMARIES_DB = "conversation_summaries.db"

# Subagent nesting is followed at most this deep (and cycles are refused).
MAX_SUBAGENT_DEPTH = 5
# Cycle/fan-out guard for subagent walks; a larger tree is marked truncated.
MAX_SUBAGENT_NODES = 2000

_SUMMARY_COLUMNS = (
    "conversation_id",
    "title",
    "preview",
    "step_count",
    "last_modified_time",
    "workspace_uris",
    "status",
    "project_id",
    "agent_name",
    "parent_conversation_id",
    "nesting_depth",
    "battle_id",
    "winning_conversation_id",
    "app_data_dir",
)

# Tool-argument keys that carry a filesystem path, with the file role they
# imply when the tool name does not say more.
_PATH_ARG_KEYS = ("TargetFile", "AbsolutePath", "FilePath", "File", "Path", "DirectoryPath", "SearchPath")
_DIR_ARG_KEYS = ("Cwd", "DirectoryPath", "SearchPath")

_TOOL_FILE_ROLES: dict[str, str] = {
    "view_file": "read",
    "view_file_outline": "read",
    "view_code_item": "read",
    "write_to_file": "created",
    "replace_file_content": "edited",
    "multi_replace_file_content": "edited",
}

_ROLE_PRIORITY = {"deleted": 5, "created": 4, "edited": 3, "written": 2, "read": 1, "referenced": 0}

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)
_WRAPPER_RE = re.compile(r"<([A-Z][A-Z0-9_]*)>.*?</\1>", re.DOTALL)
_SETTINGS_RE = re.compile(r"<USER_SETTINGS_CHANGE>(.*?)</USER_SETTINGS_CHANGE>", re.DOTALL)
_MODEL_CHANGE_RE = re.compile(
    r"changed setting `Model Selection` from .+? to (.+?)\.(?:\s|$)", re.DOTALL
)
# The innermost block only: the preamble itself mentions "<SYSTEM_MESSAGE>".
_SYSTEM_BLOCK_RE = re.compile(
    r"<SYSTEM_MESSAGE>((?:(?!<SYSTEM_MESSAGE>).)*?)</SYSTEM_MESSAGE>", re.DOTALL
)
_MENTION_RE = re.compile(r"@\[([^\]\n]+)\]")
_EXIT_CODE_RE = re.compile(r"exited with code (-?\d+)")
_HEADER_RE = re.compile(r"^(Created At|Completed At):\s*(\S+)\s*$")
_EFFORT_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$")
_ERROR_PREFIX = "Encountered error in step execution"
_PBTXT_TITLE_RE = re.compile(r'title:\s*"((?:[^"\\]|\\.)*)"')


# ---------------------------------------------------------------------------
# Parsed session
# ---------------------------------------------------------------------------


@dataclass
class AntigravityParsedSession:
    """Intermediate representation of one Antigravity conversation."""

    session_id: str  # native conversation id
    app_data_dir: str = "antigravity"
    source_path: str | None = None
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    model_id: str | None = None
    reasoning_effort: str | None = None
    workspace_root: str | None = None
    workspace_source: str | None = None  # "workspace_uris" | "derived"
    metadata: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    file_refs: list[dict[str, str]] = field(default_factory=list)
    sub_agents: list[dict[str, Any]] = field(default_factory=list)
    # Linked subagents whose transcript is not readable right now (retry).
    pending_subagents: list[str] = field(default_factory=list)
    # Linked subagents stored only as .pb/.db (never readable; noted).
    unreadable_subagents: list[str] = field(default_factory=list)
    subagents_truncated: bool = False
    tools_used: set[str] = field(default_factory=set)
    message_count: int = 0
    turn_count: int = 0
    tool_use_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    parse_errors: list[str] = field(default_factory=list)


@dataclass
class _ConversationSteps:
    steps: list[dict[str, Any]]
    parse_errors: list[str]


# ---------------------------------------------------------------------------
# Paths / roots
# ---------------------------------------------------------------------------
#
# Every path below a storage root is built from validated names and opened
# through antigravity_io (O_NOFOLLOW, dir_fd-relative): conversation ids
# must be canonical UUIDs, and no symlink below the root is ever followed.


def default_gemini_dir() -> Path:
    return Path.home() / ".gemini"


def antigravity_roots(gemini_dir: Path | None = None) -> list[Path]:
    """Return the Antigravity storage roots that exist under *gemini_dir*."""
    base = gemini_dir or default_gemini_dir()
    return [base / name for name in ROOT_NAMES if (base / name).is_dir()]


def split_conversation_dir(conv_dir: Path) -> tuple[Path, str]:
    """(storage root, conversation id) for ``<root>/brain/<cid>``.

    Raises UnsafePath unless the id is a canonical UUID and the directory
    sits directly in a known root's ``brain/``.
    """
    conv_dir = Path(conv_dir)
    cid = conv_dir.name
    if not is_conversation_id(cid) or conv_dir.parent.name != "brain":
        raise UnsafePath(f"not an Antigravity conversation directory: {conv_dir}")
    root = conv_dir.parent.parent
    if root.name not in ROOT_NAMES:
        raise UnsafePath(f"not an Antigravity storage root: {root}")
    return root, cid


def conversation_dir(root: Path, cid: str) -> Path:
    """``root/brain/<cid>`` for a validated id (raises UnsafePath otherwise)."""
    if not is_conversation_id(cid):
        raise UnsafePath(f"invalid conversation id: {cid!r}")
    return root / "brain" / cid


def sfs_session_id_for(cid: str) -> str:
    """The .sfs session id for a conversation: ``ses_`` + 16 hex chars.

    Safe by construction — the id is validated as a UUID first, so the
    result can only contain ``[0-9a-f]`` after the prefix.
    """
    if not is_conversation_id(cid):
        raise UnsafePath(f"invalid conversation id: {cid!r}")
    return "ses_" + cid.replace("-", "").lower()[:16]


def _logs_parts(cid: str) -> tuple[str, ...]:
    return ("brain", cid, ".system_generated", "logs")


def transcript_files(conv_dir: Path) -> list[tuple[str, ...]]:
    """Every transcript file that can contribute steps, as parts below the root.

    Chunks come first (in name order), then the top-level file, for the
    ``transcript_full`` family; the stringified ``transcript`` family is only
    used when no ``transcript_full`` file exists at all. Symlinks are ignored.
    """
    try:
        root, cid = split_conversation_dir(conv_dir)
    except UnsafePath:
        return []
    logs = _logs_parts(cid)
    top_entries = dict(_io.list_dir(root, logs))
    for family in ("transcript_full", "transcript"):
        files: list[tuple[str, ...]] = []
        chunk_parts = (*logs, "chunks", family)
        for name, is_dir in _io.list_dir(root, chunk_parts):
            if not is_dir and name.endswith(".jsonl"):
                files.append((*chunk_parts, name))
        if top_entries.get(f"{family}.jsonl") is False:
            files.append((*logs, f"{family}.jsonl"))
        if files:
            return files
    return []


def _root_has_transcripts(root: Path) -> bool:
    """Whether *root* still has any ``brain/<uuid>`` with a plaintext transcript.

    Used to tell an absent ``conversation_summaries.db`` (whose empty read is
    the "no DB" sentinel, not a real empty snapshot) apart from a root that
    genuinely has nothing to capture.
    """
    for name, is_dir in _io.list_dir(root, ("brain",)):
        if is_dir and is_conversation_id(name) and transcript_files(conversation_dir(root, name)):
            return True
    return False


def _step_output_parts(conv_dir: Path) -> list[tuple[str, ...]]:
    """``steps/<n>/output.txt`` files of a conversation, as parts below the root."""
    try:
        root, cid = split_conversation_dir(conv_dir)
    except UnsafePath:
        return []
    steps = ("brain", cid, ".system_generated", "steps")
    out = []
    for name, is_dir in _io.list_dir(root, steps):
        if is_dir and name.isdigit():
            out.append((*steps, name, "output.txt"))
    return out


def conversation_fingerprint(conv_dir: Path) -> tuple[float, int]:
    """(max mtime, total size) over a conversation's transcript files, its
    ``steps/<n>/output.txt`` files (written after their transcript step) and
    its ``annotations/<cid>.pbtxt`` (the title fallback)."""
    try:
        root, cid = split_conversation_dir(conv_dir)
    except UnsafePath:
        return 0.0, 0
    mtime = 0.0
    size = 0
    annotation = ("annotations", f"{cid}.pbtxt")
    for parts in [*transcript_files(conv_dir), *_step_output_parts(conv_dir), annotation]:
        st = _io.stat_file(root, parts)
        if st is None:
            continue
        mtime = max(mtime, st.st_mtime)
        size += st.st_size
    return mtime, size


def conversation_id_from_path(path: Path | str) -> tuple[Path, str] | None:
    """Map a path inside ``<root>/brain/<cid>/...`` or the annotation file
    ``<root>/annotations/<cid>.pbtxt`` to (root, cid)."""
    parts = Path(path).parts
    if (
        len(parts) >= 3 and parts[-2] == "annotations" and parts[-3] in ROOT_NAMES
        and parts[-1].endswith(".pbtxt") and is_conversation_id(parts[-1][: -len(".pbtxt")])
    ):
        return Path(*parts[:-2]), parts[-1][: -len(".pbtxt")]
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == "brain" and i + 1 < len(parts) and i >= 1:
            if parts[i - 1] in ROOT_NAMES and is_conversation_id(parts[i + 1]):
                return Path(*parts[:i]), parts[i + 1]
    return None


# ---------------------------------------------------------------------------
# Metadata: conversation_summaries.db (copy-on-read) + annotations
# ---------------------------------------------------------------------------

_SNAPSHOT_ATTEMPTS = 5


def _db_fingerprint(db_path: Path) -> tuple[Any, ...]:
    def _st(name: str) -> tuple[int, int, int]:
        st = _io.stat_file(db_path.parent, (name,))
        return (st.st_ino, st.st_mtime_ns, st.st_size) if st else (0, 0, -1)

    return (*_st(db_path.name), *_st(db_path.name + "-wal"))


def _snapshot_complete(
    fingerprint: tuple[Any, ...],
    main: tuple[bytes, bool] | None,
    wal: tuple[bytes, bool] | None,
) -> bool:
    """Whether every file present in *fingerprint* (the DB and, when it
    exists, its -wal) was read in full. A -wal that exists but could not be
    read would silently drop its committed rows. (-shm is only an index the
    reader rebuilds; it carries no data.)"""
    db_size, wal_size = fingerprint[2], fingerprint[5]
    if main is None or len(main[0]) != db_size:
        return False
    if wal_size == -1:
        return wal is None
    return wal is not None and len(wal[0]) == wal_size


def read_summaries_db(db_path: Path) -> list[dict[str, Any]] | None:
    """Read ``conversation_summaries`` rows from a private copy of *db_path*.

    The live DB and its WAL are copied (never opened in place, never through
    a symlink) into a temp dir; the copy is retried until the DB+WAL
    fingerprint is the same before and after it and every file in it was
    read in full, so a checkpoint in the middle or an unreadable WAL cannot
    produce a torn or incomplete snapshot.

    Returns ``[]`` when there is no DB or no table yet (legitimately empty)
    and ``None`` when the DB could not be read — callers must not treat a
    failure as "no conversations".
    """
    db_path = Path(db_path)
    if _io.stat_file(db_path.parent, (db_path.name,)) is None:
        return [] if not db_path.exists() and not db_path.is_symlink() else None
    wal_path = db_path.with_name(db_path.name + "-wal")
    if _io.stat_file(db_path.parent, (db_path.name + "-wal",)) is None and (
        wal_path.exists() or wal_path.is_symlink()
    ):
        # A -wal that is present but not a regular file (a planted symlink or a
        # non-regular replacement) must not be treated as absent: its committed
        # rows would be silently dropped and the copy reported complete.
        return None
    tmp_dir = Path(tempfile.mkdtemp(prefix="sfs-agy-"))
    try:
        copy = tmp_dir / "summaries.db"
        for _attempt in range(_SNAPSHOT_ATTEMPTS):
            before = _db_fingerprint(db_path)
            main = _io.read_bytes(db_path.parent, (db_path.name,))
            wal = _io.read_bytes(db_path.parent, (db_path.name + "-wal",))
            after = _db_fingerprint(db_path)
            if before == after and _snapshot_complete(before, main, wal):
                break
        else:
            logger.debug("Summaries DB %s kept changing while copying", db_path)
            return None
        copy.write_bytes(main[0])
        wal_copy = Path(str(copy) + "-wal")
        if wal is not None:
            wal_copy.write_bytes(wal[0])
        conn = sqlite3.connect(str(copy))
        try:
            conn.row_factory = sqlite3.Row
            present = {
                row[1]
                for row in conn.execute("PRAGMA table_info(conversation_summaries)")
            }
            if not present:
                return []  # table not created yet
            cols = [c for c in _SUMMARY_COLUMNS if c in present]
            if "conversation_id" not in cols:
                return None
            query = "SELECT " + ", ".join(cols) + " FROM conversation_summaries"
            return [dict(row) for row in conn.execute(query)]
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        logger.debug("Could not read %s: %s", db_path, exc)
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _clean_summary_row(row: dict[str, Any], root_name: str) -> dict[str, Any] | None:
    """Validated copy of a summaries row; None when its id is not a UUID."""
    cid = str(row.get("conversation_id") or "").strip()
    if not is_conversation_id(cid):
        return None
    app = str(row.get("app_data_dir") or "").strip() or root_name
    if app not in ROOT_NAMES:
        app = root_name
    parent = str(row.get("parent_conversation_id") or "").strip()
    out = dict(row)
    out["conversation_id"] = cid
    out["app_data_dir"] = app
    out["parent_conversation_id"] = parent if is_conversation_id(parent) else ""
    return out


class SummaryIndex:
    """Conversation metadata across every root's summaries DB.

    Rows are keyed by ``(app_data_dir, conversation_id)``. When the same key
    appears in several DBs, the row from the DB owned by that app (the root
    whose name equals app_data_dir) wins; otherwise the most recently
    modified row wins. Successful DB reads are cached by file fingerprint; a
    failed read is never cached (the previous good rows, if any, are used
    until the DB can be read again) and is reported by ``load_with_status``.
    """

    # Per-DB status values reported by load_with_status().
    FRESH = "fresh"  # read now
    CACHED = "cached"  # unchanged since the last good read
    FALLBACK = "fallback"  # read failed and the DB changed since the last good rows
    UNAVAILABLE = "unavailable"  # read failed and there is no earlier good copy

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[tuple[Any, ...], list[dict[str, Any]]]] = {}

    def load(
        self, gemini_dir: Path, *, force: bool = False,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Merged rows (see load_with_status)."""
        return self.load_with_status(gemini_dir, force=force)[0]

    def load_with_status(
        self, gemini_dir: Path, *, force: bool = False,
    ) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[Path, str]]:
        """Merged rows plus each DB's status (FRESH / CACHED / FALLBACK /
        UNAVAILABLE).

        ``force`` re-reads every DB even when its fingerprint is unchanged,
        but still falls back to the cached rows when the fresh read fails —
        a forced refresh never loses the last good data. When the read fails
        but the DB is byte-for-byte where the cached rows were read (same
        fingerprint), the cache is current and is reported CACHED. FALLBACK
        (the DB changed since those rows) and UNAVAILABLE (no earlier rows at
        all) mean ownership and titles may be wrong: callers must not capture
        from them and should retry.
        """
        merged: dict[tuple[str, str], tuple[bool, str, dict[str, Any]]] = {}
        status: dict[Path, str] = {}
        for root in antigravity_roots(gemini_dir):
            db = root / SUMMARIES_DB
            fp = _db_fingerprint(db)
            cached = self._cache.get(db)
            if cached and cached[0] == fp and not force:
                rows = cached[1]
                status[db] = self.CACHED
            else:
                fresh = read_summaries_db(db)
                if fresh is None:
                    rows = cached[1] if cached else []
                    if cached and cached[0] == fp:
                        status[db] = self.CACHED  # the DB has not changed since
                    else:
                        status[db] = self.FALLBACK if cached else self.UNAVAILABLE
                elif not fresh and _io.stat_file(db.parent, (db.name,)) is None:
                    # The DB file is gone, so read_summaries_db's empty list
                    # is the "no DB" sentinel, not a real empty snapshot.
                    # Caching it would drop every title/parent link and flatten
                    # subagents into standalone top-level sessions. Keep the
                    # last good rows (FALLBACK) when we have them; otherwise
                    # report UNAVAILABLE while the root still has capturable
                    # conversations, so the watcher defers until the DB is
                    # readable again. A root with no DB and no transcripts is
                    # legitimately FRESH-empty.
                    if cached and cached[1]:
                        rows = cached[1]
                        status[db] = self.FALLBACK
                    elif _root_has_transcripts(root):
                        rows = []
                        status[db] = self.UNAVAILABLE
                    else:
                        # No DB and no transcripts yet: legitimately empty, but
                        # do not cache it. The absent-DB fingerprint is a
                        # constant, so a cached empty snapshot would keep being
                        # served as CACHED after transcripts appear but before
                        # the DB is recreated — flattening subagents into
                        # standalone top-level sessions. Recompute instead:
                        # it's cheap and only runs while the DB file is missing.
                        rows = fresh
                        status[db] = self.FRESH
                else:
                    rows = fresh
                    self._cache[db] = (fp, fresh)
                    status[db] = self.FRESH
            for raw in rows:
                row = _clean_summary_row(raw, root.name)
                if row is None:
                    continue
                cid, app = row["conversation_id"], row["app_data_dir"]
                owned = app == root.name
                modified = str(row.get("last_modified_time") or "")
                key = (app, cid)
                prev = merged.get(key)
                if prev is None or (owned, modified) > (prev[0], prev[1]):
                    merged[key] = (owned, modified, row)
        return {k: v[2] for k, v in merged.items()}, status

    @classmethod
    def is_current(cls, status: dict[Path, str]) -> bool:
        """Whether every summaries DB was read now or is unchanged since its
        last good read — the only state a capture may be based on."""
        return all(s in (cls.FRESH, cls.CACHED) for s in status.values())


# Summary fields whose change alters a captured session (title, workspace,
# status, topology). step_count / last_modified_time change on every step
# and are deliberately left out.
RELEVANT_SUMMARY_FIELDS = (
    "title", "workspace_uris", "status", "project_id", "agent_name",
    "parent_conversation_id", "nesting_depth", "battle_id",
    "winning_conversation_id", "app_data_dir",
)


def relevant_summary(row: dict[str, Any] | None) -> tuple[Any, ...] | None:
    return None if row is None else tuple(row.get(f) for f in RELEVANT_SUMMARY_FIELDS)


@dataclass
class _Ownership:
    """Which session every known conversation is captured in."""

    owner: dict[str, str]
    depth: dict[str, int]  # depth below its owner (0 for an owner)
    members: dict[str, list[str]]  # owner -> folded conversations, breadth-first
    dirs: dict[str, Path | None]
    rows: dict[str, dict[str, Any]]
    overflowed: set[str]  # owners whose node limit pushed children out


_OWNERSHIP_CACHE: list[tuple[Any, str, _Ownership]] = []


def ownership(
    summaries: dict[tuple[str, str], dict[str, Any]], gemini_dir: Path,
) -> _Ownership:
    """The one deterministic ownership map used by discovery, parsing,
    fingerprints and digests.

    * A parent link counts only if the parent is known and has a plaintext
      transcript; otherwise the conversation is a root.
    * A cycle of parent links is broken at its smallest id, which becomes a
      root (every member of the cycle is still captured).
    * Starting from every root, children are folded into the root's session
      breadth-first while they are at most MAX_SUBAGENT_DEPTH levels below it
      and the session holds fewer than MAX_SUBAGENT_NODES subagents; a child
      past either limit becomes the root of its own session. Nothing is
      ever dropped.

    Memoised for the last summaries object (each load returns a new one).
    """
    from collections import deque

    for cached_rows, cached_dir, cached in _OWNERSHIP_CACHE:
        if cached_rows is summaries and cached_dir == str(gemini_dir):
            return cached
    rows: dict[str, dict[str, Any]] = {}
    for key in sorted(summaries):
        rows.setdefault(key[1], summaries[key])
    dirs = {
        cid: locate_conversation_dir(gemini_dir, str(row.get("app_data_dir") or ""), cid)
        for cid, row in rows.items()
    }
    parent_of: dict[str, str | None] = {}
    for cid, row in rows.items():
        parent = str(row.get("parent_conversation_id") or "").strip()
        ok = parent and parent != cid and parent in rows and dirs.get(parent) is not None
        parent_of[cid] = parent if ok else None
    # Break cycles deterministically.
    done: set[str] = set()
    for start in sorted(rows):
        path: list[str] = []
        on_path: set[str] = set()
        node: str | None = start
        while node is not None and node not in done:
            if node in on_path:
                cycle = path[path.index(node):]
                parent_of[min(cycle)] = None
                break
            on_path.add(node)
            path.append(node)
            node = parent_of[node]
        done.update(path)
    children: dict[str, list[str]] = {}
    for cid, parent in parent_of.items():
        if parent is not None:
            children.setdefault(parent, []).append(cid)
    for kids in children.values():
        kids.sort(key=lambda c: (str(rows[c].get("last_modified_time") or ""), c))

    owner: dict[str, str] = {}
    depth: dict[str, int] = {}
    members: dict[str, list[str]] = {}
    overflowed: set[str] = set()
    for root in sorted(c for c in rows if parent_of[c] is None):
        owner[root], depth[root] = root, 0
        queue = deque([root])
        while queue:
            node = queue.popleft()
            for child in children.get(node, []):
                own, d = owner[node], depth[node] + 1
                if d > MAX_SUBAGENT_DEPTH or len(members.get(own, [])) >= MAX_SUBAGENT_NODES:
                    if d <= MAX_SUBAGENT_DEPTH:
                        overflowed.add(own)
                    owner[child], depth[child] = child, 0
                else:
                    owner[child], depth[child] = own, d
                    members.setdefault(own, []).append(child)
                queue.append(child)
    result = _Ownership(owner, depth, members, dirs, rows, overflowed)
    _OWNERSHIP_CACHE[:] = [(summaries, str(gemini_dir), result)]
    return result


def owned_descendants(
    summaries: dict[tuple[str, str], dict[str, Any]],
    cid: str,
    gemini_dir: Path,
) -> tuple[list[tuple[dict[str, Any], int, Path | None]], bool]:
    """The subagents folded into ``cid``'s session: ``(row, depth, dir)``
    breadth-first, plus whether the node limit pushed some out (they are
    sessions of their own). Entries whose ``dir`` is None are linked but not
    readable right now."""
    own = ownership(summaries, gemini_dir)
    entries = [(own.rows[m], own.depth[m], own.dirs.get(m)) for m in own.members.get(cid, [])]
    return entries, cid in own.overflowed


def owner_of(
    cid: str,
    app: str,
    summaries: dict[tuple[str, str], dict[str, Any]],
    gemini_dir: Path,
) -> str:
    """The conversation whose session ``cid`` is captured in."""
    return ownership(summaries, gemini_dir).owner.get(cid, cid)


def descendant_ids(
    summaries: dict[tuple[str, str], dict[str, Any]],
    cid: str,
    gemini_dir: Path | None = None,
) -> set[str]:
    """Conversations currently linked into ``cid``'s session (readable or
    not), under the same ownership rule as ``owned_descendants``."""
    entries, _ = owned_descendants(summaries, cid, gemini_dir or default_gemini_dir())
    return {str(row.get("conversation_id")) for row, _depth, _dir in entries}


def summary_digest(
    summaries: dict[tuple[str, str], dict[str, Any]],
    cid: str,
    gemini_dir: Path | None = None,
) -> str:
    """Digest of the relevant summary fields of a session: its own row and
    its linked subagents' rows. Stored in the manifest so a metadata change
    made while the daemon was not running is noticed at the next start."""
    import hashlib

    ids = sorted({cid} | descendant_ids(summaries, cid, gemini_dir))
    payload = []
    for other in ids:
        rows = [relevant_summary(r) for (_app, c), r in sorted(summaries.items()) if c == other]
        payload.append([other, rows])
    return hashlib.sha256(json.dumps(payload, default=str).encode()).hexdigest()


def conversation_traces(gemini_dir: Path, cid: str) -> str | None:
    """What exists on disk for a conversation with no readable transcript:
    "brain" (its folder exists — the transcript may appear or be readable
    again), "protobuf" (only .pb / .db files — never readable), or None."""
    if not is_conversation_id(cid):
        return None
    found = None
    for root in antigravity_roots(gemini_dir):
        if dict(_io.list_dir(root, ("brain",))).get(cid) is True:
            return "brain"
        names = {name for name, _ in _io.list_dir(root, ("conversations",))}
        if f"{cid}.pb" in names or f"{cid}.db" in names:
            found = "protobuf"
    return found


def _annotation_title(root: Path, cid: str) -> str | None:
    if not is_conversation_id(cid):
        return None
    got = _io.read_bytes(root, ("annotations", f"{cid}.pbtxt"), max_bytes=64 * 1024)
    if got is None:
        return None
    text = got[0].decode("utf-8", errors="replace")
    m = _PBTXT_TITLE_RE.search(text)
    if not m:
        return None
    raw = m.group(1)
    try:
        return json.loads(f'"{raw}"') or None
    except ValueError:
        return raw or None


def _file_uri_to_path(uri: str) -> str | None:
    if not isinstance(uri, str) or not uri:
        return None
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = unquote(parsed.path)
        return path or None
    if uri.startswith("/"):
        return uri
    return None


def _workspace_uris(row: dict[str, Any] | None) -> list[str]:
    if not row:
        return []
    raw = row.get("workspace_uris")
    if not raw or not isinstance(raw, str):
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        value = [raw]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    paths = [_file_uri_to_path(v) for v in value if isinstance(v, str)]
    return [p for p in paths if p]


# ---------------------------------------------------------------------------
# Step loading (chunks + top-level, deduped by step_index)
# ---------------------------------------------------------------------------

# Process-wide incremental reader: chunk files that have not changed are
# never re-read, and a growing top-level transcript is read from where the
# last capture stopped.
_JSONL = _io.JsonlCache()


def load_steps(conv_dir: Path, cache: _io.JsonlCache | None = None) -> _ConversationSteps:
    """Load every step of a conversation exactly once, in step order.

    Steps are keyed by ``step_index``: a step present both in a chunk and in
    the top-level file is counted once (the later file wins — it is the most
    recent write). A trailing partial line (the app mid-append) is skipped
    and picked up on the next capture.
    """
    reader = cache or _JSONL
    by_index: dict[int, dict[str, Any]] = {}
    unindexed: list[tuple[int, str, dict[str, Any]]] = []
    seen_unindexed: set[str] = set()
    errors: list[str] = []
    # Integer ordering keys only: float(step_index) overflows on a huge index
    # (json.loads accepts arbitrarily large ints), which would fail the whole
    # conversation on every capture.
    last_index = -1

    try:
        root, _cid = split_conversation_dir(conv_dir)
    except UnsafePath as exc:
        return _ConversationSteps(steps=[], parse_errors=[str(exc)])

    for parts in transcript_files(conv_dir):
        got = reader.read(root, parts)
        if got is None:
            errors.append(f"{parts[-1]}: unreadable")
            continue
        records, file_errors = got
        errors.extend(file_errors)
        for step in records:
            if not isinstance(step, dict):
                errors.append(f"{parts[-1]}: non-object record skipped")
                continue
            idx = step.get("step_index")
            if isinstance(idx, int) and not isinstance(idx, bool):
                by_index[idx] = step
                last_index = idx
            else:
                ident = json.dumps(step, sort_keys=True, default=str)
                if ident in seen_unindexed:
                    continue
                seen_unindexed.add(ident)
                unindexed.append((last_index, ident, step))

    # An unindexed step sorts right after the indexed step it followed and
    # before the next index: (index, 0) < (index, n + 1) < (index + 1, 0).
    ordered: list[tuple[int, int, dict[str, Any]]] = [
        (i, 0, s) for i, s in by_index.items()
    ]
    ordered.extend((pos, n + 1, s) for n, (pos, _, s) in enumerate(unindexed))
    ordered.sort(key=lambda t: (t[0], t[1]))
    return _ConversationSteps(steps=[s for _, _, s in ordered], parse_errors=errors)


# ---------------------------------------------------------------------------
# Step content helpers
# ---------------------------------------------------------------------------


def _model_from_settings(content: str) -> str | None:
    model = None
    for block in _SETTINGS_RE.findall(content):
        m = _MODEL_CHANGE_RE.search(block)
        if m:
            candidate = m.group(1).strip().strip("`'\"")
            if candidate and candidate.lower() != "none":
                model = candidate
    return model


def _user_text(content: str) -> str:
    requests = [r.strip() for r in _USER_REQUEST_RE.findall(content)]
    requests = [r for r in requests if r]
    if requests:
        return "\n\n".join(requests)
    if "<USER_REQUEST>" in content:
        return ""
    # No request wrapper: drop the known wrapper blocks, keep the rest.
    return _WRAPPER_RE.sub("", content).strip()


# The manifest schema allows low / medium / high. Other labels the app shows
# are mapped onto that scale; the original label is kept in metadata.
_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "minimal": "low"}


def _raw_effort(display: str | None) -> str | None:
    """'Gemini 3.8 Flash (Minimal)' -> 'minimal' (the label in parentheses)."""
    if not display:
        return None
    m = _EFFORT_RE.match(display)
    return m.group(2).strip().lower() if m else None


def _split_model(display: str | None) -> tuple[str | None, str | None]:
    """'Gemini 3.8 Flash (High)' -> ('Gemini 3.8 Flash', 'high').

    Only labels that map onto the schema's effort scale are split off;
    anything else stays part of the model name.
    """
    if not display:
        return None, None
    m = _EFFORT_RE.match(display)
    if m:
        effort = _EFFORT_MAP.get(m.group(2).strip().lower())
        if effort:
            return m.group(1).strip() or display, effort
    return display, None


def _provider_for(model: str | None) -> str | None:
    if not model:
        return None
    lower = model.lower()
    if "claude" in lower:
        return "anthropic"
    if "gpt" in lower or lower.startswith(("o1", "o3", "o4")):
        return "openai"
    if "gemini" in lower or "gemma" in lower:
        return "google"
    return None


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_ts(value: Any) -> str | None:
    dt = _parse_ts(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        # A timestamp at the edge of the datetime range (year 1 / 9999 with
        # an offset) overflows when shifted to UTC: unknown, not a crash.
        dt = dt.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None
    return dt.isoformat().replace("+00:00", "Z")


def _split_result(content: str) -> tuple[str, int | None]:
    """Strip the 'Created At / Completed At' header; return (body, duration_ms)."""
    lines = content.split("\n")
    created = completed = None
    i = 0
    while i < len(lines):
        m = _HEADER_RE.match(lines[i].strip())
        if not m:
            break
        if m.group(1) == "Created At":
            created = _parse_ts(m.group(2))
        else:
            completed = _parse_ts(m.group(2))
        i += 1
    body = "\n".join(lines[i:]).strip("\n")
    duration = None
    if created and completed:
        try:
            duration = max(0, int((completed - created).total_seconds() * 1000))
        except (TypeError, ValueError):
            duration = None
    return body, duration


def _tool_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return {"raw": raw}
        return value if isinstance(value, dict) else {"value": value}
    if raw is None:
        return {}
    return {"value": raw}


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        # json.loads accepts NaN / Infinity / 1e999; int() of those raises,
        # which would fail the whole conversation on every capture.
        return max(0, int(value)) if math.isfinite(value) else 0
    return 0


# Tool output kept per result. Larger outputs are cut with an explicit marker
# so a single huge command output cannot balloon a session.
MAX_TOOL_OUTPUT_BYTES = 1024 * 1024
TRUNCATION_MARKER = "\n[... output truncated by SessionFS at 1 MiB ...]"


def _cap_text(text: str) -> str:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= MAX_TOOL_OUTPUT_BYTES:
        return text
    return raw[:MAX_TOOL_OUTPUT_BYTES].decode("utf-8", errors="ignore") + TRUNCATION_MARKER


def _step_output_file(conv_dir: Path, step_index: Any) -> str | None:
    """``steps/<n>/output.txt`` (symlink-safe, capped at 1 MiB with a marker)."""
    if not isinstance(step_index, int) or isinstance(step_index, bool) or step_index < 0:
        return None
    try:
        root, cid = split_conversation_dir(conv_dir)
    except UnsafePath:
        return None
    got = _io.read_bytes(
        root,
        ("brain", cid, ".system_generated", "steps", str(step_index), "output.txt"),
        max_bytes=MAX_TOOL_OUTPUT_BYTES,
    )
    if got is None:
        return None
    data, truncated = got
    text = data.decode("utf-8", errors="ignore" if truncated else "replace")
    return text + TRUNCATION_MARKER if truncated else text


# ---------------------------------------------------------------------------
# Result -> call pairing
# ---------------------------------------------------------------------------

# Explicit links a step may carry in some build (none seen in real samples:
# tool_calls hold only {name, args} and GENERIC steps carry no call id).
_RESULT_LINK_KEYS = ("tool_call_id", "tool_use_id", "call_id")
_BACKGROUND_TASK_RE = re.compile(r"background task with task id:\s*(\S+)")
_SENDER_RE = re.compile(r"\bsender=(\S+)")
_CREATED_FILE_RE = re.compile(r"Created file (file://\S+|/\S+)")
_FILE_PATH_RE = re.compile(r"File Path: `(file://[^`]+|/[^`]+)`")
_TASK_DESC_RE = re.compile(r"Task Description:\s*(.+)")
_ANSWER_RE = re.compile(r"^A\d+:", re.MULTILINE)
_COMMAND_TOOLS = {"run_command", "send_command_input"}
_WRITE_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content"}
_VIEW_TOOLS = {"view_file", "view_file_outline", "view_code_item"}


@dataclass
class _PendingCall:
    use_id: str
    name: str
    args: dict[str, Any]
    batch: int


def _norm_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = _file_uri_to_path(value) or value
    return os.path.normpath(path) if path.startswith("/") else None


def _same_file(a: str | None, b: str | None) -> int:
    """2 = same path, 1 = same file name (e.g. /var vs /private/var), -1 = different."""
    if not a or not b:
        return -1
    if a == b:
        return 2
    return 1 if os.path.basename(a) == os.path.basename(b) else -1


def _result_affinity(call: _PendingCall, text: str) -> int:
    """How well a GENERIC result's text fits a pending call.

    2 = the result names this call's own file or command; 1 = the result has
    this kind of tool's shape; 0 = no signal; -1 = the result clearly belongs
    to a different call (another file, another tool kind).
    """
    name = call.name
    created = _CREATED_FILE_RE.search(text)
    viewed = _FILE_PATH_RE.search(text)
    task_desc = _TASK_DESC_RE.search(text)
    if text.lstrip().startswith(_ERROR_PREFIX):
        return 1 if (f"tool {name}" in text or f" {name}:" in text) else 0
    if created:
        if name not in _WRITE_TOOLS:
            return -1
        return _same_file(
            _norm_path(created.group(1).rstrip(".,")), _norm_path(call.args.get("TargetFile"))
        )
    if viewed:
        if name not in _VIEW_TOOLS:
            return -1
        own = _norm_path(call.args.get("AbsolutePath")) or _norm_path(call.args.get("TargetFile"))
        return _same_file(_norm_path(viewed.group(1)), own)
    if task_desc or "exited with code" in text or "background task" in text:
        if name not in _COMMAND_TOOLS:
            return -1
        command = call.args.get("CommandLine")
        if task_desc and isinstance(command, str) and command.strip():
            desc, cmd = task_desc.group(1).strip(), command.strip()
            # The description may be a shortened copy of the command line.
            if desc == cmd or cmd.startswith(desc) or desc.startswith(cmd):
                return 2
        return 1
    if _ANSWER_RE.search(text):
        return 1 if name == "ask_question" else -1
    return 0


def _match_pending_call(
    pending: list[_PendingCall], step: dict[str, Any], *texts: str,
) -> _PendingCall | None:
    """Pick (and remove) the pending call a GENERIC step answers, or None.

    1. An explicit link on the step, when a build provides one.
    2. Otherwise the call whose file / command / tool kind the result text
       matches best. When every in-flight call is ruled out by the text, no
       call is consumed and the result is kept unpaired.
    3. Ties go to the oldest call of the most recent planner batch (so a call
       that never got a result cannot shift every later pairing), then to
       the oldest call overall.
    """
    if not pending:
        return None
    for link_key in _RESULT_LINK_KEYS:
        link = step.get(link_key)
        if isinstance(link, str) and link:
            for i, call in enumerate(pending):
                if call.use_id == link:
                    return pending.pop(i)
    text = "\n".join(t for t in texts if t)
    latest = max(c.batch for c in pending)
    best_i = 0
    best_key: tuple[int, int, int] | None = None
    for i, call in enumerate(pending):
        rank = (_result_affinity(call, text), 1 if call.batch == latest else 0, -i)
        if best_key is None or rank > best_key:
            best_key, best_i = rank, i
    if best_key is None or best_key[0] < 0:
        return None
    return pending.pop(best_i)


# ---------------------------------------------------------------------------
# Steps -> .sfs messages
# ---------------------------------------------------------------------------


@dataclass
class _StepConversion:
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_uses: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    model_display: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    turn_count: int = 0
    tool_use_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tools_used: set[str] = field(default_factory=set)


def _convert_steps(
    steps: list[dict[str, Any]],
    *,
    cid: str,
    conv_dir: Path,
    fallback_ts: str,
    agent_id: str | None = None,
) -> _StepConversion:
    out = _StepConversion()
    prefix = f"ag_{cid.replace('-', '')[:12]}"
    if agent_id:
        prefix += "_sub"
    pending: list[_PendingCall] = []  # calls awaiting a GENERIC result
    batch = 0  # increments per PLANNER_RESPONSE that makes calls
    background_tasks: dict[str, str] = {}  # background task id -> tool_use_id
    current_model: str | None = None
    prev_role: str | None = None
    used_ids: set[str] = set()

    def _msg_id(base: str) -> str:
        candidate = base
        n = 1
        while candidate in used_ids:
            n += 1
            candidate = f"{base}_{n}"
        used_ids.add(candidate)
        return candidate

    for pos, step in enumerate(steps):
        idx = step.get("step_index")
        tag = f"{idx:06d}" if isinstance(idx, int) and not isinstance(idx, bool) else f"x{pos:05d}"
        ts = _normalize_ts(step.get("created_at")) or fallback_ts
        if out.first_ts is None:
            out.first_ts = ts
        out.last_ts = ts
        stype = str(step.get("type") or "").upper()
        source = str(step.get("source") or "").upper()
        status = str(step.get("status") or "").upper()
        raw_content = step.get("content")
        content: str = raw_content if isinstance(raw_content, str) else ""
        meta: dict[str, Any] = {"step_index": idx, "step_type": stype or None}
        if status:
            meta["status"] = status

        base: dict[str, Any] = {}
        if agent_id:
            base["is_sidechain"] = True
            base["agent_id"] = agent_id

        if stype == "USER_INPUT" or (not stype and source.startswith("USER")):
            model = _model_from_settings(content)
            if model:
                current_model = model
                out.model_display = model
            text = _user_text(content)
            mentions = [m.strip() for m in _MENTION_RE.findall(text)]
            out.mentions.extend(mentions)
            if text:
                if prev_role != "user":
                    out.turn_count += 1
                user_meta = dict(meta)
                if mentions:
                    user_meta["mentions"] = mentions
                if model:
                    user_meta["model_selected"] = model
                out.messages.append({
                    "msg_id": _msg_id(f"{prefix}_{tag}"),
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                    "timestamp": ts,
                    **base,
                    "metadata": user_meta,
                })
                prev_role = "user"
            elif model:
                out.messages.append({
                    "msg_id": _msg_id(f"{prefix}_{tag}"),
                    "role": "system",
                    "content": [{"type": "text", "text": f"[Settings] Model selection: {model}"}],
                    "timestamp": ts,
                    "is_meta": True,
                    **base,
                    "metadata": meta,
                })
                prev_role = "system"
            continue

        if stype == "PLANNER_RESPONSE":
            blocks: list[dict[str, Any]] = []
            thinking = step.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                blocks.append({"type": "thinking", "text": thinking})
            if content.strip():
                blocks.append({"type": "text", "text": content})
            calls = step.get("tool_calls") if isinstance(step.get("tool_calls"), list) else []
            if calls:
                batch += 1
            for n, call in enumerate(calls):
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or "unknown")
                raw_id = call.get("id")
                use_id = (
                    str(raw_id) if isinstance(raw_id, str) and raw_id
                    else f"{prefix}_{tag}_t{n}"
                )
                args = _tool_args(call.get("args"))
                blocks.append({"type": "tool_use", "tool_use_id": use_id, "name": name, "input": args})
                pending.append(_PendingCall(use_id, name, args, batch))
                out.tool_uses.append((name, args))
                out.tools_used.add(name)
                out.tool_use_count += 1
            if not blocks:
                continue
            msg: dict[str, Any] = {
                "msg_id": _msg_id(f"{prefix}_{tag}"),
                "role": "assistant",
                "content": blocks,
                "timestamp": ts,
            }
            model_id, _ = _split_model(current_model)
            if model_id:
                msg["model"] = model_id
                provider = _provider_for(model_id)
                if provider:
                    msg["provider"] = provider
            if any(b["type"] == "tool_use" for b in blocks):
                msg["stop_reason"] = "tool_use"
            inp = _int(step.get("input_tokens"))
            outp = _int(step.get("output_tokens"))
            cache = _int(step.get("cache_read_tokens"))
            if inp or outp or cache:
                msg["usage"] = {
                    "input_tokens": inp,
                    "output_tokens": outp,
                    "cache_read_tokens": cache,
                }
                out.input_tokens += inp
                out.output_tokens += outp
            msg.update(base)
            msg["metadata"] = meta
            out.messages.append(msg)
            prev_role = "assistant"
            continue

        if stype == "GENERIC":
            body, duration = _split_result(content)
            raw_output = _step_output_file(conv_dir, idx)
            if raw_output is not None and len(raw_output.strip()) > len(body.strip()):
                body = raw_output.strip("\n")
            error_text = step.get("error") if isinstance(step.get("error"), str) else ""
            if not body and error_text:
                body = error_text
            body = _cap_text(body)
            exit_code = None
            # Only the status line before "Output:" — never the command's own output.
            m = _EXIT_CODE_RE.search(body.split("\nOutput:", 1)[0])
            if m:
                try:
                    exit_code = int(m.group(1))
                except ValueError:
                    exit_code = None
            is_error = (
                status in ("ERROR", "FAILED", "CANCELED", "CANCELLED")
                or body.lstrip().startswith(_ERROR_PREFIX)
                or (exit_code is not None and exit_code != 0)
            )
            matched = _match_pending_call(pending, step, content, body)
            if matched is not None:
                use_id = matched.use_id
                task = _BACKGROUND_TASK_RE.search(content)
                if task:
                    background_tasks[task.group(1)] = use_id
                    meta["background_task_id"] = task.group(1)
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": use_id,
                    "content": body,
                    "is_error": bool(is_error),
                }
                if exit_code is not None:
                    block["exit_code"] = exit_code
                if duration is not None:
                    block["duration_ms"] = duration
                out.messages.append({
                    "msg_id": _msg_id(f"{prefix}_{tag}"),
                    "role": "tool",
                    "content": [block],
                    "timestamp": ts,
                    **base,
                    "metadata": meta,
                })
                prev_role = "tool"
            elif body:
                # A result with no call to attach to (none in flight, or every
                # in-flight call ruled out): keep it, as unpaired meta.
                meta["unpaired_tool_result"] = True
                if exit_code is not None:
                    meta["exit_code"] = exit_code
                label = "[Tool error]" if is_error else "[Tool output]"
                out.messages.append({
                    "msg_id": _msg_id(f"{prefix}_{tag}"),
                    "role": "system",
                    "content": [{"type": "text", "text": f"{label} {body}"}],
                    "timestamp": ts,
                    "is_meta": True,
                    **base,
                    "metadata": meta,
                })
                prev_role = "system"
            continue

        # SYSTEM_MESSAGE and anything new: keep the text as a meta message.
        text = content
        inner = _SYSTEM_BLOCK_RE.findall(content)
        if inner:
            text = "\n\n".join(t.strip() for t in inner if t.strip())
        text = _cap_text(text.strip())
        if not text:
            continue
        role = "assistant" if (source == "MODEL" and stype not in ("SYSTEM_MESSAGE",)) else "system"
        entry: dict[str, Any] = {
            "msg_id": _msg_id(f"{prefix}_{tag}"),
            "role": role,
            "content": [{"type": "text", "text": text}],
            "timestamp": ts,
            **base,
            "metadata": meta,
        }
        if role == "system":
            entry["is_meta"] = True
        # A background task's completion arrives later as a system message
        # from sender=<task id>: point it back at the call that started it.
        # Exact token lookup: "task-1" must never match "task-10".
        for sender in _SENDER_RE.findall(content):
            task_use_id = background_tasks.get(sender)
            if task_use_id is not None:
                meta["background_task_id"] = sender
                meta["tool_use_id"] = task_use_id
                break
        out.messages.append(entry)
        prev_role = role

    return out


# ---------------------------------------------------------------------------
# Workspace derivation
# ---------------------------------------------------------------------------


def _find_git_root(path: Path, stop: Path) -> Path | None:
    current = path
    while True:
        if current == stop or current == current.parent:
            return None
        try:
            if (current / ".git").exists():
                return current
        except OSError:
            return None
        current = current.parent


def _existing_anchor(path: Path) -> Path | None:
    current = path
    while not current.exists():
        if current == current.parent:
            return None
        current = current.parent
    return current


# A workspace root is where relative .sfs paths are anchored and where
# provenance scans for rules files, so it must be a real project directory.
_SYSTEM_TREES = (
    "/etc", "/private/etc", "/bin", "/sbin", "/usr", "/System", "/Library",
    "/dev", "/proc", "/sys", "/boot", "/root", "/private/var/root", "/var/root",
)
_SYSTEM_EXACT = frozenset({
    "/", "/var", "/private", "/private/var", "/tmp", "/private/tmp", "/opt",
    "/Applications", "/Users", "/home", "/var/home", "/Volumes", "/mnt", "/media",
    "/srv", "/lib", "/lib64", "/nix", "/snap",
})
_HOMES_PARENTS = ("/Users", "/home", "/var/home")


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def _owned_git_root(path: Path) -> Path | None:
    """The git repository containing *path*, if the current user owns it."""
    root = _find_git_root(path, Path(path.anchor))
    if root is None:
        return None
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return root
    try:
        if os.stat(root).st_uid != getuid() or os.lstat(root / ".git").st_uid != getuid():
            return None
    except OSError:
        return None
    return root


def validate_workspace_root(raw: str | None, *, home: Path | None = None) -> str | None:
    """The one check every workspace root goes through (recorded or derived).

    Rejects ``/``, system directories, the home directory and its ancestors,
    and other users' home directories. A directory inside the current user's
    home is accepted; one outside it only when it is inside a git repository
    the current user owns. Returns the normalized path or None.
    """
    if not isinstance(raw, str) or not raw or "\x00" in raw or not os.path.isabs(raw):
        return None
    norm = os.path.normpath(raw)
    real = os.path.realpath(norm)
    home_real = os.path.realpath(str(home or Path.home()))
    for candidate in {norm, real}:
        if candidate in _SYSTEM_EXACT or any(_under(candidate, t) for t in _SYSTEM_TREES):
            return None
        if _under(home_real, candidate):
            return None  # the home directory itself or one of its ancestors
        for base in _HOMES_PARENTS:
            if candidate.startswith(base + "/"):
                owner_home = os.path.join(base, candidate[len(base) + 1:].split("/", 1)[0])
                if not _under(home_real, owner_home) and not _under(owner_home, home_real):
                    return None  # someone else's home
    if _under(real, home_real):
        return norm
    if _owned_git_root(Path(real)) is not None:
        return norm
    return None

def _candidate_paths(
    tool_uses: list[tuple[str, dict[str, Any]]], mentions: list[str],
) -> list[tuple[str, bool]]:
    """(absolute path, is_dir) pairs referenced by tool args and @-mentions."""
    out: list[tuple[str, bool]] = []
    for _name, args in tool_uses:
        for key in _PATH_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str):
                p = _file_uri_to_path(value) or value
                if p.startswith("/"):
                    out.append((p, key in _DIR_ARG_KEYS))
        cwd = args.get("Cwd")
        if isinstance(cwd, str) and cwd.startswith("/"):
            out.append((cwd, True))
    for mention in mentions:
        p = _file_uri_to_path(mention) or mention
        if p.startswith("/") and len(Path(p).parts) >= 3:
            out.append((p, Path(p).is_dir()))
    return out


def derive_workspace_root(
    tool_uses: list[tuple[str, dict[str, Any]]],
    mentions: list[str],
    *,
    exclude: list[Path],
    home: Path | None = None,
) -> str | None:
    """Pick a workspace root from the paths a conversation touched.

    Prefers the most common git root among those paths; otherwise the common
    directory of all of them. Paths inside the Antigravity storage roots
    (scratch dirs, brain artifacts) are ignored, and the result is never the
    home directory or the filesystem root.
    """
    home = home or Path.home()
    dirs: list[Path] = []
    for raw, is_dir in _candidate_paths(tool_uses, mentions):
        p = Path(os.path.normpath(raw))
        if any(p == ex or ex in p.parents for ex in exclude):
            continue
        dirs.append(p if is_dir else p.parent)
    if not dirs:
        return None

    git_roots: Counter[str] = Counter()
    order: dict[str, int] = {}
    for d in dirs:
        anchor = _existing_anchor(d)
        if anchor is None:
            continue
        root = _find_git_root(anchor, Path(anchor.anchor))
        valid = validate_workspace_root(str(root), home=home) if root is not None else None
        if valid is not None:
            git_roots[valid] += 1
            order.setdefault(valid, len(order))
    if git_roots:
        best = max(git_roots.items(), key=lambda kv: (kv[1], -order[kv[0]]))
        return best[0]

    try:
        common = os.path.commonpath([str(d) for d in dirs])
    except ValueError:
        return None
    return validate_workspace_root(common, home=home)


def _relative_file_refs(
    tool_uses: list[tuple[str, dict[str, Any]]], mentions: list[str], root: str,
) -> list[dict[str, str]]:
    roles: dict[str, str] = {}
    root_path = Path(root)

    def _add(raw: str, role: str) -> None:
        p = Path(os.path.normpath(_file_uri_to_path(raw) or raw))
        if not p.is_absolute():
            return
        try:
            rel = p.relative_to(root_path)
        except ValueError:
            return
        rel_str = rel.as_posix()
        if rel_str in ("", "."):
            return
        current = roles.get(rel_str, "referenced")
        if rel_str not in roles or _ROLE_PRIORITY[role] > _ROLE_PRIORITY[current]:
            roles[rel_str] = role

    for name, args in tool_uses:
        for key in ("TargetFile", "AbsolutePath", "FilePath", "File"):
            value = args.get(key)
            if not isinstance(value, str):
                continue
            role = _TOOL_FILE_ROLES.get(name, "referenced")
            if name == "write_to_file" and args.get("Overwrite") is True:
                role = "written"
            _add(value, role)
    for mention in mentions:
        candidate = _file_uri_to_path(mention) or mention
        if candidate.startswith("/") and not Path(candidate).is_dir():
            _add(candidate, "referenced")
    return [{"path": p, "role": r} for p, r in sorted(roles.items())]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _children_of(
    summaries: dict[tuple[str, str], dict[str, Any]], cid: str,
) -> list[dict[str, Any]]:
    kids = [
        row for row in summaries.values()
        if str(row.get("parent_conversation_id") or "").strip() == cid
        and str(row.get("conversation_id") or "") != cid
    ]
    kids.sort(key=lambda r: (str(r.get("last_modified_time") or ""), str(r.get("conversation_id"))))
    return kids


def locate_conversation_dir(gemini_dir: Path, app_data_dir: str, cid: str) -> Path | None:
    """Find ``brain/<cid>`` under the root owned by *app_data_dir* first.

    The id must be a canonical UUID; a ``brain/<cid>`` that is a symlink is
    never used (transcript_files opens every component without following).
    """
    if not is_conversation_id(cid):
        return None
    names = [app_data_dir] + [n for n in ROOT_NAMES if n != app_data_dir]
    for name in names:
        if name not in ROOT_NAMES:
            continue
        candidate = conversation_dir(gemini_dir / name, cid)
        if transcript_files(candidate):
            return candidate
    return None


def _summary_for(
    summaries: dict[tuple[str, str], dict[str, Any]], app: str, cid: str,
) -> dict[str, Any] | None:
    row = summaries.get((app, cid))
    if row is not None:
        return row
    for (_app, other_cid), candidate in summaries.items():
        if other_cid == cid:
            return candidate
    return None


def parse_antigravity_conversation(
    conv_dir: Path,
    summaries: dict[tuple[str, str], dict[str, Any]] | None = None,
    *,
    include_subagents: bool = True,
) -> AntigravityParsedSession:
    """Parse ``<root>/brain/<cid>`` into an intermediate session.

    *summaries* is the merged conversation_summaries index (see
    :class:`SummaryIndex`); it is loaded on demand when omitted so the
    function also works stand-alone (recapture, ``sfs init``).
    """
    conv_dir = Path(conv_dir)
    root, cid = split_conversation_dir(conv_dir)  # raises UnsafePath
    gemini_dir = root.parent
    app = root.name
    if summaries is None:
        summaries = SummaryIndex().load(gemini_dir)
    row = _summary_for(summaries, app, cid)

    loaded = load_steps(conv_dir)
    session = AntigravityParsedSession(
        session_id=cid,
        app_data_dir=app,
        source_path=str(conv_dir),
        parse_errors=list(loaded.parse_errors),
    )
    fallback_ts = (
        _normalize_ts(str(row.get("last_modified_time") or "").replace(" ", "T")) if row else None
    ) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    main = _convert_steps(loaded.steps, cid=cid, conv_dir=conv_dir, fallback_ts=fallback_ts)
    messages = list(main.messages)
    tool_uses = list(main.tool_uses)
    mentions = list(main.mentions)
    session.turn_count = main.turn_count
    session.tool_use_count = main.tool_use_count
    session.total_input_tokens = main.input_tokens
    session.total_output_tokens = main.output_tokens
    session.tools_used = set(main.tools_used)
    session.created_at = main.first_ts
    session.updated_at = main.last_ts
    model_display = main.model_display

    # Subagents: folded into the parent as sidechain messages (the same
    # shape the Claude Code converter uses for its subagent transcripts).
    if include_subagents:
        entries, truncated = owned_descendants(summaries, cid, gemini_dir)
        session.subagents_truncated = truncated
        for child, _depth, child_dir in entries:
            child_id = str(child.get("conversation_id") or "")
            if child_dir is None:
                # Linked but not readable right now. Only a child stored solely
                # as .pb/.db (never readable) is noted and not waited on; any
                # other linked child — folder not created yet, transcript not
                # written yet — is pending, so this capture is not complete.
                if conversation_traces(gemini_dir, child_id) == "protobuf":
                    session.unreadable_subagents.append(child_id)
                else:
                    session.pending_subagents.append(child_id)
                continue
            child_steps = load_steps(child_dir)
            session.parse_errors.extend(f"{child_id[:8]}: {e}" for e in child_steps.parse_errors)
            agent_id = child_id
            sub = _convert_steps(
                child_steps.steps, cid=child_id, conv_dir=child_dir,
                fallback_ts=fallback_ts, agent_id=agent_id,
            )
            transient = any(
                e.endswith(": unreadable") or e.endswith(": incomplete final line")
                for e in child_steps.parse_errors
            )
            if transient or not sub.messages:
                # Unreadable, mid-append, or nothing usable yet: the child is
                # still being written, so this capture is not complete.
                session.pending_subagents.append(child_id)
            if not sub.messages:
                continue
            messages.extend(sub.messages)
            tool_uses.extend(sub.tool_uses)
            mentions.extend(sub.mentions)
            session.tool_use_count += sub.tool_use_count
            session.total_input_tokens += sub.input_tokens
            session.total_output_tokens += sub.output_tokens
            session.tools_used |= sub.tools_used
            sub_model, _ = _split_model(sub.model_display or model_display)
            sub_ref: dict[str, Any] = {"agent_id": agent_id, "message_count": len(sub.messages)}
            if sub_model:
                sub_ref["model"] = sub_model
            session.sub_agents.append(sub_ref)
            if sub.last_ts and (session.updated_at is None or sub.last_ts > session.updated_at):
                session.updated_at = sub.last_ts

    session.messages = messages
    session.message_count = len(messages)
    session.model_id, session.reasoning_effort = _split_model(model_display)

    # Title: summaries DB -> annotation -> derived later from messages.
    title = None
    if row:
        title = str(row.get("title") or "").strip() or None
    session.title = title or _annotation_title(root, cid)

    # Workspace: workspace_uris when the app recorded one, else derived.
    uris = _workspace_uris(row)
    recorded = next((v for v in (validate_workspace_root(u) for u in uris) if v), None)
    rejected_uris = [u for u in uris if validate_workspace_root(u) is None]
    if recorded:
        session.workspace_root = recorded
        session.workspace_source = "workspace_uris"
    else:
        derived = derive_workspace_root(
            tool_uses, mentions,
            exclude=[gemini_dir / name for name in ROOT_NAMES],
        )
        if derived:
            session.workspace_root = derived
            session.workspace_source = "derived"
    if session.workspace_root:
        session.file_refs = _relative_file_refs(tool_uses, mentions, session.workspace_root)

    display, interface = _APP_INFO.get(app, (app, None))
    meta: dict[str, Any] = {"app_data_dir": app, "app": display}
    if interface:
        meta["interface"] = interface
    if row:
        for key in (
            "project_id", "agent_name", "parent_conversation_id", "nesting_depth",
            "battle_id", "winning_conversation_id", "step_count", "status",
        ):
            value = row.get(key)
            if value not in (None, ""):
                meta[key] = value
        if len(uris) > 1:
            meta["workspace_uris"] = uris
    if rejected_uris:
        meta["rejected_workspace_uris"] = rejected_uris
    if session.workspace_source:
        meta["workspace_source"] = session.workspace_source
    if model_display:
        meta["model_display_name"] = model_display
        raw_effort = _raw_effort(model_display)
        if raw_effort and raw_effort != session.reasoning_effort:
            meta["reasoning_effort_label"] = raw_effort
    if session.parse_errors:
        meta["parse_errors"] = len(session.parse_errors)
    if session.unreadable_subagents:
        meta["unreadable_subagents"] = list(session.unreadable_subagents)
    if session.subagents_truncated:
        meta["subagents_truncated"] = True
        logger.warning(
            "Antigravity conversation %s has more than %d subagents; the rest were not captured",
            cid[:8], MAX_SUBAGENT_NODES,
        )
    meta["summary_digest"] = summary_digest(summaries, cid, gemini_dir)
    session.metadata = meta
    return session


def convert_antigravity_to_sfs(
    session: AntigravityParsedSession,
    session_dir: Path,
    session_id: str | None = None,
) -> Path:
    """Write a parsed Antigravity conversation as a .sfs session directory."""
    from sessionfs.converters.skill_detector import detect_skills, skills_to_tools_json
    from sessionfs.session_id import validate_session_id
    from sessionfs.utils.title_utils import extract_smart_title, is_usable_title, sanitize_secrets

    sid = session_id or sfs_session_id_for(session.session_id)
    if not validate_session_id(sid):
        raise UnsafePath(f"invalid session id: {sid!r}")
    session_dir.mkdir(parents=True, exist_ok=True)

    messages = session.messages
    created_at = session.created_at or datetime.now(timezone.utc).isoformat()
    updated_at = session.updated_at or created_at

    duration_ms = None
    start, end = _parse_ts(created_at), _parse_ts(updated_at)
    if start and end:
        duration_ms = max(0, int((end - start).total_seconds() * 1000))

    # Antigravity generates short titles itself ("Create File Agy.txt In
    # Pwd"); keep them whole (sentence splitting would cut at ".txt") and
    # only fall back to message-derived titles when there is none.
    title: str | None
    if session.title and is_usable_title(session.title) and len(session.title.strip()) <= 80:
        title = sanitize_secrets(session.title.strip())
    else:
        title = extract_smart_title(
            messages=messages or None,
            raw_title=session.title,
            message_count=session.message_count,
        )
    if title and title.startswith("Untitled session"):
        title = None

    _display, interface = _APP_INFO.get(session.app_data_dir, (session.app_data_dir, None))
    manifest: dict[str, Any] = {
        "sfs_version": SFS_FORMAT_VERSION,
        "session_id": sid,
        "title": title,
        "tags": [],
        "created_at": created_at,
        "updated_at": updated_at,
        "source": {
            "tool": TOOL_NAME,
            "tool_version": None,
            "sfs_converter_version": SFS_CONVERTER_VERSION,
            "original_session_id": session.session_id,
            "original_path": session.source_path,
            "interface": interface,
        },
        "stats": {
            "message_count": session.message_count,
            "turn_count": session.turn_count,
            "tool_use_count": session.tool_use_count,
            "total_input_tokens": session.total_input_tokens,
            "total_output_tokens": session.total_output_tokens,
            "duration_ms": duration_ms,
        },
        "metadata": {"antigravity": session.metadata},
    }
    if session.model_id:
        model: dict[str, Any] = {
            "provider": _provider_for(session.model_id) or "google",
            "model_id": session.model_id,
        }
        if session.reasoning_effort:
            model["reasoning_effort"] = session.reasoning_effort
        manifest["model"] = model
    if session.sub_agents:
        manifest["sub_agents"] = session.sub_agents

    (session_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    with open(session_dir / "messages.jsonl", "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, separators=(",", ":")) + "\n")

    if session.workspace_root:
        workspace: dict[str, Any] = {"root_path": session.workspace_root}
        from sessionfs.spec.convert_cc import capture_git_context

        git = capture_git_context(session.workspace_root)
        workspace["git"] = git if git else None
        if session.file_refs:
            workspace["files"] = session.file_refs
        if session.workspace_source:
            workspace["metadata"] = {"source": session.workspace_source}
        (session_dir / "workspace.json").write_text(json.dumps(workspace, indent=2))
    else:
        # A recapture without a workspace must not keep the previous one.
        (session_dir / "workspace.json").unlink(missing_ok=True)

    tools: dict[str, Any] = {}
    if session.tools_used:
        tools["tools_used"] = sorted(session.tools_used)
    detected = detect_skills(messages, TOOL_NAME)
    if detected:
        tools["custom_tools"] = skills_to_tools_json(detected)
    if tools:
        (session_dir / "tools.json").write_text(json.dumps(tools, indent=2))
    else:
        (session_dir / "tools.json").unlink(missing_ok=True)

    return session_dir


def discover_antigravity_sessions(
    gemini_dir: Path | None = None,
    summaries: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Discover capturable Antigravity conversations across every root.

    Returns one dict per top-level conversation (subagent conversations are
    folded into their parent, whose fingerprint covers them) with keys
    session_id, path, tool, app_data_dir, mtime, size_bytes, title,
    project_path. Conversations with no plaintext transcript are not
    returned; see :func:`discover_unreadable_conversations`.
    """
    base = gemini_dir or default_gemini_dir()
    if summaries is None:
        summaries = SummaryIndex().load(base)

    candidates: dict[str, list[Path]] = {}
    for root in antigravity_roots(base):
        # Only real directories named by a canonical UUID; symlinks are skipped.
        for name, is_dir in _io.list_dir(root, ("brain",)):
            if not is_dir or not is_conversation_id(name):
                continue
            conv_dir = conversation_dir(root, name)
            if transcript_files(conv_dir):
                candidates.setdefault(name, []).append(conv_dir)

    owners: dict[str, set[str]] = {}
    for app, other in summaries:
        owners.setdefault(other, set()).add(app)

    convs: dict[str, dict[str, Any]] = {}
    for cid, dirs in candidates.items():
        # Same conversation under two roots: keep the copy in the root that
        # owns it according to the summaries (app_data_dir), else the first.
        owned = [d for d in dirs if d.parent.parent.name in owners.get(cid, set())]
        conv_dir = (owned or dirs)[0]
        mtime, size = conversation_fingerprint(conv_dir)
        convs[cid] = {
            "session_id": cid,
            "path": str(conv_dir),
            "tool": TOOL_NAME,
            "app_data_dir": conv_dir.parent.parent.name,
            "mtime": mtime,
            "size_bytes": size,
        }

    # Fold subagents into the session that owns them (one rule: owner_of).
    results: dict[str, dict[str, Any]] = {}
    folded: dict[str, list[dict[str, Any]]] = {}
    for cid, info in convs.items():
        top = owner_of(cid, info["app_data_dir"], summaries, base)
        if top == cid:
            results[cid] = dict(info)
        else:
            folded.setdefault(top, []).append(info)
    for top, kids in folded.items():
        entry = results.get(top)
        if entry is None:
            continue
        for kid in kids:
            entry["mtime"] = max(entry["mtime"], kid["mtime"])
            entry["size_bytes"] += kid["size_bytes"]

    out: list[dict[str, Any]] = []
    for cid, info in results.items():
        row = _summary_for(summaries, info["app_data_dir"], cid)
        uris = _workspace_uris(row)
        info["title"] = (str((row or {}).get("title") or "").strip() or None)
        info["project_path"] = next(
            (v for v in (validate_workspace_root(u) for u in uris) if v), ""
        )
        out.append(info)
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


def top_level_conversation(
    cid: str,
    app_data_dir: str,
    summaries: dict[tuple[str, str], dict[str, Any]],
    gemini_dir: Path,
) -> tuple[str, Path] | None:
    """Resolve a (possibly subagent) conversation to the session it is captured in."""
    top = owner_of(cid, app_data_dir, summaries, gemini_dir)
    row = _summary_for(summaries, app_data_dir, top)
    app = str((row or {}).get("app_data_dir") or app_data_dir)
    conv_dir = locate_conversation_dir(gemini_dir, app, top)
    if conv_dir is None:
        return None
    return top, conv_dir


def session_fingerprint(
    conv_dir: Path,
    summaries: dict[tuple[str, str], dict[str, Any]],
) -> tuple[float, int]:
    """Fingerprint of a captured session: its transcript plus its subagents'."""
    conv_dir = Path(conv_dir)
    gemini_dir = conv_dir.parent.parent.parent
    mtime, size = conversation_fingerprint(conv_dir)
    entries, _ = owned_descendants(summaries, conv_dir.name, gemini_dir)
    for _row, _depth, child_dir in entries:
        if child_dir is None:
            continue
        c_mtime, c_size = conversation_fingerprint(child_dir)
        mtime = max(mtime, c_mtime)
        size += c_size
    return mtime, size


def discover_unreadable_conversations(gemini_dir: Path | None = None) -> dict[str, str]:
    """Conversations that exist only in a non-plaintext form, with the reason.

    Older builds keep conversations as AES-GCM encrypted ``.pb`` files and
    newer builds as protobuf ``.db`` files; neither is read. A conversation
    that also has a plaintext transcript is not listed.
    """
    base = gemini_dir or default_gemini_dir()
    readable: set[str] = set()
    for root in antigravity_roots(base):
        for name, is_dir in _io.list_dir(root, ("brain",)):
            if is_dir and is_conversation_id(name) and transcript_files(conversation_dir(root, name)):
                readable.add(name)
    out: dict[str, str] = {}
    for root in antigravity_roots(base):
        for name, is_dir in _io.list_dir(root, ("conversations",)):
            stem, _, suffix = name.rpartition(".")
            if is_dir or not is_conversation_id(stem) or stem in readable or stem in out:
                continue
            if suffix == "pb":
                out[stem] = "encrypted"
            elif suffix == "db":
                out[stem] = "no_plaintext_transcript"
    return out
