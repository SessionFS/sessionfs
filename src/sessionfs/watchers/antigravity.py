"""Google Antigravity session watcher.

One watcher covers every Antigravity app under ``~/.gemini``: the desktop app
(``antigravity/``), the agy CLI (``antigravity-cli/``) and the IDE build
(``antigravity-ide/``). It watches each root's ``brain/`` directory and the
root itself (for ``conversation_summaries.db`` and its WAL) with watchdog
(FSEvents / inotify — never polling), debounces bursts of step writes, and
re-captures only the conversations whose fingerprint (transcripts plus
``steps/<n>/output.txt``) changed. Subagent conversations are captured
inside their parent session.

Conversations that exist only as encrypted ``.pb`` (older builds) or
protobuf ``.db`` files have no plaintext transcript: they are skipped and
counted in the watcher status — never half-captured.

Capture-only — no write-back support.
"""

from __future__ import annotations

import enum
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from sessionfs.converters.antigravity_io import UnsafePath
from sessionfs.converters.antigravity_to_sfs import (
    ROOT_NAMES,
    SUMMARIES_DB,
    TOOL_NAME,
    SummaryIndex,
    antigravity_roots,
    conversation_fingerprint,
    conversation_id_from_path,
    descendant_ids,
    discover_antigravity_sessions,
    discover_unreadable_conversations,
    locate_conversation_dir,
    relevant_summary,
    session_fingerprint,
    sfs_session_id_for,
    summary_digest,
    top_level_conversation,
)
from sessionfs.daemon.config import AntigravityWatcherConfig
from sessionfs.daemon.status import WatcherStatus
from sessionfs.store import deleted as _deleted
from sessionfs.store.local import LocalStore
from sessionfs.watchers.base import CaptureHealthTracker, NativeSessionRef

logger = logging.getLogger("sfsd.watcher.antigravity")

MERGED_REASON_PREFIX = "merged_into_parent:"
RETRY_BASE_S = 5.0  # first retry of a deferred or failed capture
RETRY_MAX_S = 600.0  # backoff ceiling; retries continue until a capture succeeds
_RETRY_MAX_EXPONENT = 16  # 5 s * 2**16 is far past the ceiling; avoids huge ints
# Consecutive summaries reads that were not current before capture health is
# degraded (one read per event pass while a DB stays unreadable).
SUMMARY_FAILURE_THRESHOLD = 10
# A conversation whose transcript is unreadable or wholly malformed must keep
# the SAME fingerprint (mtime/size) this many reads before each read counts as
# a capture failure — a single busy/unreadable read is not a failure yet.
UNREADABLE_STABLE_REQUIRED = 2
# A linked subagent that stays pending (torn final line, folder never created,
# wholly malformed transcript) with the SAME fingerprint for
# UNREADABLE_STABLE_REQUIRED reads spanning at least this long is no longer
# waited on: the parent is installed with whatever the child has readable (or
# without it), and the child grows in on a later capture through the normal
# path. Without this a stuck child would block its parent forever.
PENDING_CHILD_SETTLE_S = 60.0


def _dict(v: Any) -> dict:
    """A manifest field that is not an object degrades to empty on read.

    Mirrors the write-path guard in sessionfs/store/index.py so a corrupt or
    hand-edited manifest.json cannot raise on a truthy non-dict value."""
    return v if isinstance(v, dict) else {}


def _list(v: Any) -> list:
    """A manifest field that is not a list degrades to empty on read.

    ``sub_agents`` is list-typed but read straight off a bare ``json.loads``
    manifest, so a truthy non-iterable value (``5``, ``true``, ``3.14``) would
    raise ``TypeError`` when iterated. Guard the container before the loop so
    the element-level ``isinstance(ref, dict)`` check can still run."""
    return v if isinstance(v, list) else []


def _count(v: Any) -> int:
    """A numeric manifest field that is not an int degrades to 0 on read.

    ``message_count`` is int-typed but read straight off a bare ``json.loads``
    manifest, so a truthy non-numeric value (``"n/a"``, a non-empty dict or
    list) would raise ``ValueError``/``TypeError`` when cast with ``int()``.
    Only a real int (never bool) survives; everything else degrades to 0,
    matching the ``_dict``/``_list`` tolerance pattern."""
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _stored_count(v: Any) -> int | None:
    """A stored subagent's ``message_count`` as a real int, or None when corrupt.

    Unlike ``_count`` — where degrading a corrupt value to 0 is the safe
    sentinel for a bad stats total (it forces a repair recapture) — a stored
    subagent's count decides whether its sidechain is carried or dropped.
    Degrading it to 0 would make a child with N stored messages look like it
    had 0, so the shrink verdict would neither carry nor protect it. None
    marks the count unknown so the caller carries the child forward instead.
    """
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _is_watched_file(path: str) -> bool:
    norm = path.replace("\\", "/")
    if "/annotations/" in norm and norm.endswith(".pbtxt"):
        return True  # the title fallback of a conversation
    if "/brain/" not in norm:
        return False
    return norm.endswith(".jsonl") or norm.endswith("/output.txt")


class _AntigravityEventHandler(FileSystemEventHandler):
    """Queues transcript / step-output writes, flags summaries-DB changes, and
    flags a rescan when a root or brain/ appears."""

    def __init__(
        self,
        queue: list[str],
        lock: threading.Lock,
        rescan: threading.Event,
        summaries_changed: threading.Event | None = None,
        skipped_dirty: threading.Event | None = None,
    ):
        self._queue = queue
        self._lock = lock
        self._rescan = rescan
        self._summaries_changed = summaries_changed
        self._skipped_dirty = skipped_dirty

    def _handle(self, path: str, is_directory: bool, created: bool = False) -> None:
        name = Path(path).name
        if is_directory:
            # Only a NEW root or brain/ needs the observers re-planned; the
            # brain/ directory itself is modified on every new conversation.
            if created and (name in ("brain", "conversations", "annotations") or name in ROOT_NAMES):
                self._rescan.set()
            return
        if name in (SUMMARIES_DB, SUMMARIES_DB + "-wal") and Path(path).parent.name in ROOT_NAMES:
            if self._summaries_changed is not None:
                # Under the same lock the consumer uses to test-and-clear the
                # flag, so a summaries event can never be swallowed mid-pass.
                with self._lock:
                    self._summaries_changed.set()
            return
        if Path(path).parent.name == "conversations" and name.endswith((".pb", ".db")):
            if self._skipped_dirty is not None:
                self._skipped_dirty.set()
            return
        if _is_watched_file(path):
            with self._lock:
                if path not in self._queue:
                    self._queue.append(path)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle(str(event.src_path), event.is_directory)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(str(event.src_path), event.is_directory, created=True)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle_gone(str(event.src_path), event.is_directory)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Atomic temp-file + rename writes land as a move onto the target;
        # the source of a move is gone from where it was.
        self._handle_gone(str(event.src_path), event.is_directory)
        dest = getattr(event, "dest_path", "") or ""
        if dest:
            self._handle(str(dest), event.is_directory, created=True)

    def _handle_gone(self, path: str, is_directory: bool) -> None:
        """A path removed or moved away: the skipped (.pb/.db) counts change,
        and a conversation that lost a transcript, step output or annotation
        is recaptured like on a modification. Directories matter too: a
        ``steps/<n>`` or transcript-chunk directory can leave the tree with
        only a directory event, and a removed watched directory kills its
        observer, so the watches are re-planned."""
        p = Path(path)
        if is_directory:
            name = p.name
            if name in ("brain", "conversations", "annotations") or name in ROOT_NAMES:
                self._rescan.set()
                if name == "conversations" and self._skipped_dirty is not None:
                    self._skipped_dirty.set()
            elif conversation_id_from_path(path) is not None:
                # A directory inside brain/<cid> (or brain/<cid> itself):
                # the stored capture is stale until it is recaptured.
                with self._lock:
                    if path not in self._queue:
                        self._queue.append(path)
            return
        if p.parent.name in ROOT_NAMES and p.name in (SUMMARIES_DB, SUMMARIES_DB + "-wal"):
            # The summaries DB (or its WAL) was deleted or moved away: cached
            # titles and parent links may no longer exist, so reload them
            # exactly like on a create or modification.
            if self._summaries_changed is not None:
                with self._lock:
                    self._summaries_changed.set()
            return
        if p.parent.name == "conversations" and p.name.endswith((".pb", ".db")):
            if self._skipped_dirty is not None:
                self._skipped_dirty.set()
        elif _is_watched_file(path):
            with self._lock:
                if path not in self._queue:
                    self._queue.append(path)


class CaptureOutcome(enum.Enum):
    """What a capture attempt did. Only STORED means the session on disk now
    reflects the conversation; DEFERRED / FAILED are retried later."""

    STORED = "stored"  # a new capture was installed
    UNCHANGED = "unchanged"  # parsed fine; the compression guard kept the stored capture
    DEFERRED = "deferred"  # not safe yet (no steps, pending subagent, it is a subagent of another)
    KEPT_UNREADABLE = "kept_unreadable"  # kept the stored capture: a carried child could not be read
    EXCLUDED = "excluded"  # the user deleted it
    FAILED = "failed"  # parse/convert/install error


class AntigravityWatcher:
    """Watches Antigravity storage roots and captures conversations to .sfs."""

    def __init__(
        self,
        config: AntigravityWatcherConfig,
        store: LocalStore,
        scan_interval: float = 5.0,
    ) -> None:
        self._config = config
        self._store = store
        self._scan_interval = scan_interval
        self._gemini_dir = Path(config.gemini_dir)

        self._summaries = SummaryIndex()
        self._tracked: dict[str, NativeSessionRef] = {}
        self._digests: dict[str, str] = {}  # native id -> stored summary digest
        self._capture_health = CaptureHealthTracker(tool_name=TOOL_NAME)
        self._last_scan_at: str | None = None
        self._last_event_time = 0.0
        self._skipped: dict[str, int] = {}
        self._skipped_logged: set[str] = set()

        self._observer: Any = None
        self._watch_paths: list[str] = []
        self._event_queue: list[str] = []
        self._event_lock = threading.Lock()
        self._rescan = threading.Event()
        self._summaries_changed = threading.Event()
        self._last_summaries: dict[tuple[str, str], dict[str, Any]] = {}
        # Sessions whose last capture was DEFERRED or FAILED: native id ->
        # (attempts, monotonic time of the next attempt). Kept until a
        # capture succeeds; a real source change retries it immediately.
        self._retry: dict[str, tuple[int, float]] = {}
        self._excluded_seen: dict[str, tuple[float, int]] = {}
        # Sessions stored by this pass, and captures in progress (recursion guard).
        self._stored_this_pass: set[str] = set()
        self._capturing: set[str] = set()
        # Conversations that parsed to zero messages because the transcript is
        # unreadable or malformed: native id -> (mtime, size, stable reads).
        self._unreadable: dict[str, tuple[float, int, int]] = {}
        # Parent native id -> pending child ids that were flagged unreadable,
        # so their per-session health latch is released when the parent stores
        # (a child deleted at the source must not leave the watcher degraded).
        self._pending_unreadable: dict[str, set[str]] = {}
        # Pending child id -> (its fingerprint, or None when it has no
        # transcript; consecutive reads at that fingerprint; monotonic time
        # first seen at it). Decides when a stuck child stops blocking its
        # parent (see PENDING_CHILD_SETTLE_S).
        self._pending_seen: dict[str, tuple[tuple[float, int] | None, int, float]] = {}
        self.last_defer_reason = ""
        # Set when conversations/ changes (.pb/.db appear or go): the skipped
        # counts are refreshed on the next event pass.
        self._skipped_dirty = threading.Event()
        # Set when a full scan had to wait for the summaries DBs; the next
        # event pass runs the scan again.
        self._scan_pending = False
        self._summary_failures = 0

    # -- summaries -----------------------------------------------------------

    def _load_summaries(
        self, *, force: bool,
    ) -> tuple[dict[tuple[str, str], dict[str, Any]], bool]:
        """Load the summaries. The flag is True only when every DB is current
        (read now, or unchanged since its last good read); otherwise nothing
        may be captured from these rows, another forced refresh is queued,
        and a DB that stays unreadable degrades capture health."""
        rows, status = self._summaries.load_with_status(self._gemini_dir, force=force)
        if SummaryIndex.is_current(status):
            if self._summary_failures >= SUMMARY_FAILURE_THRESHOLD:
                logger.info("Antigravity summaries are readable again")
                self._capture_health.set_healthy()
            self._summary_failures = 0
            return rows, True
        self._summary_failures += 1
        self._summaries_changed.set()
        bad = sorted(str(db) for db, s in status.items() if s not in (SummaryIndex.FRESH, SummaryIndex.CACHED))
        logger.info("Antigravity summaries could not be read (%s); will retry", ", ".join(bad))
        if self._summary_failures >= SUMMARY_FAILURE_THRESHOLD:
            self._capture_health.set_degraded(
                f"Antigravity conversation summaries unreadable for {self._summary_failures} "
                f"attempts in a row: {', '.join(bad)}"
            )
        return rows, False

    # -- retry bookkeeping ----------------------------------------------------

    def _capture(
        self,
        native_id: str,
        conv_dir: Path,
        mtime: float,
        size: int,
        summaries: dict[tuple[str, str], dict[str, Any]],
        *,
        ignore_compression: bool = False,
    ) -> CaptureOutcome:
        """Every capture goes through here: DEFERRED / FAILED / KEPT_UNREADABLE
        outcomes are retried with backoff until a capture succeeds."""
        self._capturing.add(native_id)
        try:
            outcome = self._capture_session(
                native_id, conv_dir, mtime, size, summaries,
                ignore_compression=ignore_compression,
            )
        finally:
            self._capturing.discard(native_id)
        if outcome is CaptureOutcome.STORED:
            self._stored_this_pass.add(native_id)
        if outcome in (CaptureOutcome.DEFERRED, CaptureOutcome.FAILED, CaptureOutcome.KEPT_UNREADABLE):
            attempts = self._retry.get(native_id, (0, 0.0))[0] + 1
            delay = min(RETRY_BASE_S * (2 ** min(attempts - 1, _RETRY_MAX_EXPONENT)), RETRY_MAX_S)
            self._retry[native_id] = (attempts, time.monotonic() + delay)
        else:
            self._retry.pop(native_id, None)
        return outcome

    def capture_now(self, native_id: str, *, ignore_compression: bool = True) -> CaptureOutcome:
        """Capture one conversation through the full ownership-aware pipeline
        (used by ``sfs recapture``). Owners of detached subagents are captured
        first when needed; nothing is written if the capture is not safe."""
        self._stored_this_pass.clear()
        summaries, status = self._summaries.load_with_status(self._gemini_dir, force=True)
        if not SummaryIndex.is_current(status):
            # A summaries DB could not be read (e.g. mid-checkpoint) and no
            # copy matching it exists: ownership and titles are unknown.
            self.last_defer_reason = "Antigravity's conversation summaries are busy; retry in a moment"
            return CaptureOutcome.DEFERRED
        app = self._app_of(native_id, summaries)
        resolved = top_level_conversation(native_id, app, summaries, self._gemini_dir) if app else None
        if resolved is None:
            from sessionfs.converters.antigravity_to_sfs import locate_conversation_dir

            for root in antigravity_roots(self._gemini_dir):
                found = locate_conversation_dir(self._gemini_dir, root.name, native_id)
                if found is not None:
                    resolved = (native_id, found)
                    break
        if resolved is None:
            self.last_defer_reason = "its transcript was not found"
            return CaptureOutcome.FAILED
        if resolved[0] != native_id:
            self.last_defer_reason = f"it is part of session {resolved[0]} (a subagent); capture that one"
            return CaptureOutcome.DEFERRED
        # Capture the new owners of any detached subagents first, so this
        # session can prove them stored elsewhere and shrink in the same pass
        # (the same stored-first order the scan path uses).
        for owner in self._detached_owners(native_id, summaries):
            self._owner_stored_now(owner, summaries)
        mtime, size = session_fingerprint(resolved[1], summaries)
        outcome = self._capture(
            native_id, resolved[1], mtime, size, summaries,
            ignore_compression=ignore_compression,
        )
        if outcome is CaptureOutcome.STORED:
            # Retire standalone captures of subagents this session now holds.
            self._reconcile_subagents(summaries)
        return outcome

    def _owner_stored_now(
        self, owner: str, summaries: dict[tuple[str, str], dict[str, Any]],
    ) -> bool:
        """True only once the owner's CURRENT capture has been stored in
        this pass — capturing it now if needed. A stored manifest from an
        earlier capture is never taken as proof of current ownership."""
        if owner in self._stored_this_pass:
            return True
        if owner in self._capturing:
            return False
        app = self._app_of(owner, summaries)
        resolved = top_level_conversation(owner, app, summaries, self._gemini_dir) if app else None
        if resolved is None:
            # The owner's summary row is gone (e.g. a detached child whose
            # row disappeared): find its transcript across every root, the
            # same fallback capture_now() and _retry_targets() use.
            from sessionfs.converters.antigravity_to_sfs import locate_conversation_dir

            for root in antigravity_roots(self._gemini_dir):
                found = locate_conversation_dir(self._gemini_dir, root.name, owner)
                if found is not None:
                    resolved = (owner, found)
                    break
        if resolved is None or resolved[0] != owner:
            return False
        mtime, size = session_fingerprint(resolved[1], summaries)
        return self._capture(owner, resolved[1], mtime, size, summaries) is CaptureOutcome.STORED

    def _detached_owners(
        self, native_id: str, summaries: dict[tuple[str, str], dict[str, Any]],
    ) -> list[str]:
        """Top-level sessions that now own a subagent this capture stored but
        no longer links. ``capture_now`` captures them first so the parent can
        prove them stored and drop the child in the same pass."""
        try:
            sfs_id = sfs_session_id_for(native_id)
        except UnsafePath:
            return []
        manifest = self._stored_manifest(sfs_id)
        if manifest is None:
            return []
        linked = descendant_ids(summaries, native_id, self._gemini_dir)
        owners: list[str] = []
        for ref in _list(manifest.get("sub_agents")):
            child = ref.get("agent_id") if isinstance(ref, dict) else None
            if not isinstance(child, str) or child in linked:
                continue
            app = self._app_of(child, summaries)
            resolved = top_level_conversation(child, app, summaries, self._gemini_dir) if app else None
            owner = resolved[0] if resolved else child
            if owner != native_id and owner not in owners:
                owners.append(owner)
        return owners

    def _due_retries(self, now: float) -> list[str]:
        return [cid for cid, (_n, due) in self._retry.items() if due <= now]

    # -- scanning -----------------------------------------------------------

    def full_scan(self) -> None:
        roots = antigravity_roots(self._gemini_dir)
        if not roots:
            self._capture_health.set_degraded(
                f"Antigravity storage not found under {self._gemini_dir} "
                f"(looked for {', '.join(ROOT_NAMES)})"
            )
            return

        self._stored_this_pass.clear()
        try:
            summaries, trusted = self._load_summaries(force=self._scan_pending)
            if not trusted:
                # Without every summaries DB, subagents would be captured as
                # top-level sessions and titles lost: scan again later.
                logger.info("Antigravity summaries are busy; deferring the scan")
                self._scan_pending = True
                return
            self._scan_pending = False
            self._last_summaries = summaries
            sessions = discover_antigravity_sessions(self._gemini_dir, summaries)
            # Conversations without a stored capture first: a child that
            # became top-level is then stored before its old parent shrinks.
            sessions.sort(key=lambda s: self._is_stored(s["session_id"]))
            captured = 0
            for s_info in sessions:
                native_id = s_info["session_id"]
                conv_dir = Path(s_info["path"])
                mtime, size = s_info["mtime"], s_info["size_bytes"]
                # Metadata changed while the daemon was not running counts too.
                if self._unchanged(native_id, mtime, size) and self._digest_current(native_id, summaries):
                    continue
                if self._capture(native_id, conv_dir, mtime, size, summaries) is CaptureOutcome.STORED:
                    captured += 1

            self._reconcile_subagents(summaries)
            self._sweep_pending(summaries)
            self._refresh_skipped()
            self._capture_health.set_healthy()
            self._last_scan_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "Antigravity scan: %d found, %d captured, %d skipped",
                len(sessions), captured, sum(self._skipped.values()),
            )
        except Exception as e:
            logger.error("Antigravity full scan failed: %s", e, exc_info=True)
            self._capture_health.set_degraded(str(e))

    def _still_excluded(self, native_id: str) -> bool:
        """Whether the in-memory 'deleted, skip it' note still holds: the
        tombstone is re-checked every time, and the note is dropped once the
        user restored the session."""
        if native_id not in self._excluded_seen:
            return False
        try:
            excluded = _deleted.is_excluded(sfs_session_id_for(native_id), base_dir=self._store.store_dir)
        except UnsafePath:
            excluded = False
        if not excluded:
            self._excluded_seen.pop(native_id, None)
        return excluded

    def _unchanged(self, native_id: str, mtime: float, size: int) -> bool:
        if self._excluded_seen.get(native_id) == (mtime, size) and self._still_excluded(native_id):
            return True
        existing = self._store.get_tracked_session(native_id)
        if existing and existing.last_mtime >= mtime and existing.last_size == size:
            self._tracked[native_id] = existing
            return True
        return False

    def _is_stored(self, native_id: str) -> bool:
        try:
            return self._stored_manifest(sfs_session_id_for(native_id)) is not None
        except UnsafePath:
            return False

    def _stored_manifest(self, sfs_id: str) -> dict[str, Any] | None:
        try:
            manifest = self._store.get_session_manifest(sfs_id)
        except (OSError, ValueError):
            return None
        return manifest if isinstance(manifest, dict) else None

    def _digest_current(
        self, native_id: str, summaries: dict[tuple[str, str], dict[str, Any]],
    ) -> bool:
        """True when the stored capture was made from the current metadata.
        A missing manifest is stale (unless the user deleted the session)."""
        try:
            sfs_id = sfs_session_id_for(native_id)
        except UnsafePath:
            return True
        manifest = self._stored_manifest(sfs_id)
        if manifest is None:
            self._digests.pop(native_id, None)
            self._still_excluded(native_id)  # drops a stale note after a restore
            # A pruned session keeps its tracked fingerprint with no sfs id:
            # it was removed on purpose, so treat it as current (and skip the
            # recapture) unless the source changed — which _unchanged already
            # gates on mtime/size before this runs.
            existing = self._store.get_tracked_session(native_id)
            if existing is not None and existing.sfs_session_id is None:
                return True
            return _deleted.is_excluded(sfs_id, base_dir=self._store.store_dir)
        stored = self._digests.get(native_id)
        if stored is None:
            meta = _dict(_dict(manifest.get("metadata")).get("antigravity"))
            stored = meta.get("summary_digest")
            if not isinstance(stored, str):
                return False  # captured before digests existed: refresh once
            self._digests[native_id] = stored
        return stored == summary_digest(summaries, native_id, self._gemini_dir)

    def _refresh_skipped(self) -> None:
        unreadable = discover_unreadable_conversations(self._gemini_dir)
        counts: dict[str, int] = {}
        for cid, reason in unreadable.items():
            counts[reason] = counts.get(reason, 0) + 1
            if cid not in self._skipped_logged:
                self._skipped_logged.add(cid)
                logger.info(
                    "Skipping Antigravity conversation %s: %s",
                    cid[:8],
                    "encrypted at rest (.pb), not readable offline"
                    if reason == "encrypted"
                    else "no plaintext transcript (protobuf .db only)",
                )
        self._skipped = counts

    # -- capture -------------------------------------------------------------

    def _remember(self, native_id: str, conv_dir: Path, sfs_id: str, mtime: float, size: int,
                  project_path: str | None = None, captured: bool = False) -> None:
        ref = NativeSessionRef(
            tool=TOOL_NAME,
            native_session_id=native_id,
            native_path=str(conv_dir),
            sfs_session_id=sfs_id,
            last_mtime=mtime,
            last_size=size,
            last_captured_at=datetime.now(timezone.utc).isoformat() if captured else None,
            project_path=project_path,
        )
        self._store.upsert_tracked_session(ref)
        self._tracked[native_id] = ref

    def _note_excluded(self, native_id: str, conv_dir: Path, sfs_id: str, mtime: float, size: int) -> None:
        if self._store.get_session_metadata(sfs_id) is not None:
            self._remember(native_id, conv_dir, sfs_id, mtime, size)
        else:
            # No session row to point a tracking row at (foreign key):
            # remember the fingerprint in memory instead.
            self._excluded_seen[native_id] = (mtime, size)

    @staticmethod
    def _unreadable_or_invalid(parse_errors: list[str]) -> bool:
        """A transcript that parsed to zero messages is still being written if
        its only errors are trailing partial lines; a read that failed
        outright, or a line that is malformed or oversized, is a real failure
        worth reporting."""
        for err in parse_errors:
            if not err.endswith(": incomplete final line"):
                return True
        return False

    def _note_unreadable(self, native_id: str, mtime: float, size: int) -> None:
        """Track a zero-message transcript that is unreadable or malformed.

        Once the SAME fingerprint has been seen enough times in a row (the
        transcript is not being actively written), each read is recorded as a
        capture failure so a permanently-broken conversation degrades health
        instead of being deferred forever with no signal. Retries continue
        either way — a real source change resets the streak and can recover.
        """
        prev = self._unreadable.get(native_id)
        if prev is not None and prev[0] == mtime and prev[1] == size:
            stable = prev[2] + 1
        else:
            stable = 1
        self._unreadable[native_id] = (mtime, size, stable)
        if stable >= UNREADABLE_STABLE_REQUIRED:
            # A per-session latch: a single permanently-broken conversation is
            # enough to degrade, and a success on another session does not
            # clear it (unlike a format break, which needs many failures).
            self._capture_health.record_unreadable_session(
                f"Antigravity conversation {native_id[:8]} is unreadable or malformed; retrying",
                session_key=native_id,
            )

    def _note_pending_child(
        self,
        child_id: str,
        summaries: dict[tuple[str, str], dict[str, Any]],
        parent_id: str | None = None,
    ) -> None:
        """Track a pending subagent's own fingerprint for health reporting.

        A first-time child that is still being written changes mtime/size on
        every read, so it never reaches the stability threshold and stays a
        silent (correct) deferral. A child whose transcript is present but
        permanently malformed has a stable fingerprint, so it eventually
        degrades capture health — the same signal the parent's own zero-
        message transcript uses."""
        app = self._app_of(child_id, summaries)
        child_dir = locate_conversation_dir(self._gemini_dir, app, child_id) if app is not None else None
        if child_dir is None:
            # No transcript on disk (or no summary row): genuinely in flight.
            # If this child was previously latched as permanently unreadable,
            # its transcript is now gone, so the latch must not keep the
            # watcher degraded forever — a child deleted at the source has no
            # stored parent to release it.
            self._release_pending_child(child_id, parent_id)
            return
        if parent_id is not None:
            self._pending_unreadable.setdefault(parent_id, set()).add(child_id)
        c_mtime, c_size = conversation_fingerprint(child_dir)
        self._note_unreadable(child_id, c_mtime, c_size)

    def _pending_child_settled(
        self, child_id: str, summaries: dict[tuple[str, str], dict[str, Any]],
    ) -> bool:
        """True once a pending child has stopped changing long enough that
        waiting on it would block its parent forever.

        The child's own fingerprint (or its absence, for a linked child whose
        folder was never created or was deleted) must be the same for
        UNREADABLE_STABLE_REQUIRED reads in a row spanning at least
        PENDING_CHILD_SETTLE_S. A child that is actively being written changes
        fingerprint and keeps resetting the streak, so it is still waited on."""
        app = self._app_of(child_id, summaries)
        child_dir = locate_conversation_dir(self._gemini_dir, app, child_id) if app is not None else None
        fp = conversation_fingerprint(child_dir) if child_dir is not None else None
        now = time.monotonic()
        prev = self._pending_seen.get(child_id)
        if prev is not None and prev[0] == fp:
            stable, since = prev[1] + 1, prev[2]
        else:
            stable, since = 1, now
        self._pending_seen[child_id] = (fp, stable, since)
        return stable >= UNREADABLE_STABLE_REQUIRED and now - since >= PENDING_CHILD_SETTLE_S

    def _resolve_top(
        self, native_id: str, summaries: dict[tuple[str, str], dict[str, Any]],
    ) -> tuple[str, Path] | None:
        """The top-level conversation *native_id* is captured in, or None when
        it is gone (no summary row resolves it and no transcript is on disk)."""
        app = self._app_of(native_id, summaries)
        resolved = top_level_conversation(native_id, app, summaries, self._gemini_dir) if app else None
        if resolved is None:
            for root in antigravity_roots(self._gemini_dir):
                found = locate_conversation_dir(self._gemini_dir, root.name, native_id)
                if found is not None:
                    return native_id, found
        return resolved

    def _is_excluded_native(self, native_id: str) -> bool:
        try:
            return _deleted.is_excluded(sfs_session_id_for(native_id), base_dir=self._store.store_dir)
        except UnsafePath:
            return False

    def _release_parent_pending(
        self, parent_id: str, summaries: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> None:
        """Release every pending-child latch and stability streak a parent
        owns — the parent is gone, excluded, or no longer top-level, so no
        later capture of it will ever release them."""
        children = set(self._pending_unreadable.pop(parent_id, set()))
        if summaries is not None:
            children |= descendant_ids(summaries, parent_id, self._gemini_dir) & set(self._pending_seen)
        for child in children:
            self._unreadable.pop(child, None)
            self._pending_seen.pop(child, None)
            self._capture_health.clear_unreadable_session(child)

    def _sweep_pending(self, summaries: dict[tuple[str, str], dict[str, Any]]) -> None:
        """Drop pending-child state whose owner can no longer release it.

        A parent installed without waiting for a stuck child (or excluded by
        the user) leaves the retry queue, so neither a recapture nor the
        gone-retry path will ever release that child's latch. Each pass:

        * a parent that no longer resolves to its own top-level conversation
          (deleted, folded into another) or that the user excluded releases
          all its pending children;
        * a child whose ``_pending_seen`` streak no longer belongs to a live,
          non-excluded parent (row gone, no longer a subagent, owner gone or
          excluded) is forgotten, so the map cannot grow without bound.
        """
        for parent in list(self._pending_unreadable):
            resolved = self._resolve_top(parent, summaries)
            if resolved is None or resolved[0] != parent or self._is_excluded_native(parent):
                self._release_parent_pending(parent)
                if resolved is None:
                    self._unreadable.pop(parent, None)
                    self._capture_health.clear_unreadable_session(parent)
                continue
            linked = descendant_ids(summaries, parent, self._gemini_dir)
            for child in list(self._pending_unreadable.get(parent, set())):
                if child not in linked:
                    self._unreadable.pop(child, None)
                    self._pending_seen.pop(child, None)
                    self._release_pending_child_latch(child, parent)
        for child in list(self._pending_seen):
            app = self._app_of(child, summaries)
            owner = (
                top_level_conversation(child, app, summaries, self._gemini_dir)
                if app is not None else None
            )
            if owner is None or owner[0] == child or self._is_excluded_native(owner[0]):
                self._pending_seen.pop(child, None)

    def _release_pending_child_latch(self, child_id: str, parent_id: str) -> None:
        self._capture_health.clear_unreadable_session(child_id)
        pending = self._pending_unreadable.get(parent_id)
        if pending is not None:
            pending.discard(child_id)
            if not pending:
                self._pending_unreadable.pop(parent_id, None)

    def _release_pending_child(self, child_id: str, parent_id: str | None) -> None:
        """Release a pending child's unreadable latch and tracking once its
        transcript is gone (deleted at the source, or never readable)."""
        if self._capture_health.has_unreadable_session(child_id):
            self._unreadable.pop(child_id, None)
            self._capture_health.clear_unreadable_session(child_id)
        if parent_id is not None:
            pending = self._pending_unreadable.get(parent_id)
            if pending is not None:
                pending.discard(child_id)
                if not pending:
                    self._pending_unreadable.pop(parent_id, None)

    def _capture_session(
        self,
        native_id: str,
        conv_dir: Path,
        mtime: float,
        size: int,
        summaries: dict[tuple[str, str], dict[str, Any]],
        *,
        ignore_compression: bool = False,
    ) -> CaptureOutcome:
        """Capture one top-level conversation and say what happened.

        ``ignore_compression`` (``sfs recapture``) lets a shorter capture
        replace the stored one; every ownership/topology safety still applies.
        The reason for a DEFERRED outcome is left in ``last_defer_reason``.
        """
        self.last_defer_reason = ""
        logger.info("Capturing Antigravity conversation %s (%d bytes)", native_id[:8], size)
        try:
            from sessionfs.converters.antigravity_to_sfs import (
                convert_antigravity_to_sfs,
                parse_antigravity_conversation,
            )

            sfs_id = sfs_session_id_for(native_id)  # validated, ses_ + hex only
            base = self._store.store_dir
            resolved = top_level_conversation(
                native_id, conv_dir.parent.parent.name, summaries, self._gemini_dir,
            )
            if resolved is None or resolved[0] != native_id:
                owner = resolved[0] if resolved else "?"
                self.last_defer_reason = f"it is part of session {owner} (a subagent); capture that one"
                return CaptureOutcome.DEFERRED  # it is a subagent now: folded elsewhere
            # Captured as a top-level session, so a merge record from when it
            # was a subagent no longer applies (lifted only if still a merge).
            # Checked before parsing: a conversation the user deleted must not
            # wait on, or latch the health of, its pending subagents.
            _deleted.remove_exclusion_if_reason(sfs_id, MERGED_REASON_PREFIX, base_dir=base)
            if _deleted.is_excluded(sfs_id, base_dir=base):
                self._note_excluded(native_id, conv_dir, sfs_id, mtime, size)
                self._release_parent_pending(native_id, summaries)
                return CaptureOutcome.EXCLUDED
            session = parse_antigravity_conversation(conv_dir, summaries)
            if session.message_count < 1:
                self.last_defer_reason = "it has no readable steps yet"
                if self._unreadable_or_invalid(session.parse_errors):
                    self._note_unreadable(native_id, mtime, size)
                else:
                    self._unreadable.pop(native_id, None)  # still writing
                    self._capture_health.clear_unreadable_session(native_id)
                return CaptureOutcome.DEFERRED  # retried with backoff
            self._unreadable.pop(native_id, None)  # readable now
            self._capture_health.clear_unreadable_session(native_id)
            pending_now = set(session.pending_subagents)
            # Children that parsed cleanly this time are no longer "unreadable
            # pending". One that parsed only partly (a torn final line) is
            # still pending: its latch and stability streak must survive, or
            # a stuck child would reset its own health signal on every read.
            for ref in session.sub_agents or []:
                if (
                    isinstance(ref, dict) and isinstance(ref.get("agent_id"), str)
                    and ref["agent_id"] not in pending_now
                ):
                    self._unreadable.pop(ref["agent_id"], None)
                    self._pending_seen.pop(ref["agent_id"], None)
                    self._capture_health.clear_unreadable_session(ref["agent_id"])
            stored = self._stored_manifest(sfs_id)
            if session.pending_subagents:
                # A pending child with no stored sidechain is genuinely in
                # flight: keep deferring. One that already has a stored
                # sidechain falls through to _shrink_verdict, which carries
                # that sidechain forward byte-for-byte so the parent keeps
                # capturing its own thread instead of deferring forever.
                stored_sidechains = self._stored_sidechain_ids(stored)
                still_pending = [
                    c for c in session.pending_subagents
                    if c not in stored_sidechains
                ]
                # A pending child whose transcript is present but
                # permanently malformed must eventually degrade capture
                # health (stable fingerprint) instead of deferring forever
                # with the daemon reporting healthy.
                for child in still_pending:
                    self._note_pending_child(child, summaries, parent_id=native_id)
                # A pending child already latched (or tracked) for this parent
                # but now carried by the stored capture keeps its health signal
                # current: released once its transcript is gone, kept while it
                # stays broken.
                tracked = self._pending_unreadable.get(native_id, set())
                for child in session.pending_subagents:
                    if child not in still_pending and (
                        child in tracked or self._capture_health.has_unreadable_session(child)
                    ):
                        self._note_pending_child(child, summaries, parent_id=native_id)
                # A child that has stopped changing for long enough no longer
                # blocks the parent: it is installed with whatever the child
                # has readable (or without it) and grows in on a later capture.
                settled = [c for c in still_pending if self._pending_child_settled(c, summaries)]
                if settled:
                    logger.warning(
                        "Antigravity conversation %s: subagent(s) %s stayed unreadable or "
                        "incomplete; capturing the parent without waiting for them",
                        native_id[:8], ", ".join(c[:8] for c in settled),
                    )
                still_pending = [c for c in still_pending if c not in settled]
                if still_pending:
                    self.last_defer_reason = (
                        "subagent(s) " + ", ".join(c[:8] for c in still_pending)
                        + " are still being written or not readable yet"
                    )
                    logger.info(
                        "Antigravity conversation %s: linked subagent(s) %s not readable yet; retrying",
                        native_id[:8], ", ".join(c[:8] for c in still_pending),
                    )
                    return CaptureOutcome.DEFERRED

            if _deleted.is_excluded(sfs_id, base_dir=base):
                self._note_excluded(native_id, conv_dir, sfs_id, mtime, size)
                self._release_parent_pending(native_id, summaries)
                return CaptureOutcome.EXCLUDED

            if stored is not None:
                verdict = self._shrink_verdict(native_id, session, stored, summaries)
                if verdict is CaptureOutcome.KEPT_UNREADABLE:
                    # A needed carry-forward could not read the stored child's
                    # messages: keep the stored capture rather than install one
                    # that would lose them. The health failure was recorded in
                    # _shrink_verdict. Do NOT mark this fingerprint current —
                    # the source has moved on and its newer messages still need
                    # recapturing once the stored messages read again; _capture
                    # retries this outcome with backoff like a DEFERRED.
                    return verdict
                if verdict is CaptureOutcome.UNCHANGED and not ignore_compression:
                    # Compression guard: the stored capture has more messages.
                    self._capture_health.record_success()
                    self._remember(native_id, conv_dir, sfs_id, mtime, size)
                    return verdict

            # Same store write path as every other watcher: allocate (which
            # records the prior sync state), convert in place, annotate, then
            # upsert (which carries the etag and marks the session dirty).
            # Re-check the tombstone right before writing.
            if _deleted.is_excluded(sfs_id, base_dir=base):
                self._note_excluded(native_id, conv_dir, sfs_id, mtime, size)
                self._release_parent_pending(native_id, summaries)
                return CaptureOutcome.EXCLUDED
            session_dir = self._store.allocate_session_dir(sfs_id)
            convert_antigravity_to_sfs(session, session_dir, session_id=sfs_id)
            from sessionfs.watchers.provenance import (
                annotate_manifest_with_provenance,
                workspace_root_from_session,
            )
            annotate_manifest_with_provenance(
                session_dir, TOOL_NAME,
                workspace_root_from_session(session_dir) or session.workspace_root,
            )
            from sessionfs.watchers.active_ticket_annot import (
                annotate_manifest_with_active_ticket,
            )
            annotate_manifest_with_active_ticket(session_dir)
            manifest = json.loads((session_dir / "manifest.json").read_text())
            self._store.upsert_session_metadata(sfs_id, manifest, str(session_dir))
            self._remember(
                native_id, conv_dir, sfs_id, mtime, size,
                project_path=session.workspace_root, captured=True,
            )
            digest = session.metadata.get("summary_digest")
            if isinstance(digest, str):
                self._digests[native_id] = digest
            # A stored parent resolved every pending child: release their
            # unreadable health latches (a child deleted at the source must
            # not leave the watcher degraded forever).
            # A child still pending (installed without waiting for it) keeps
            # its latch so health stays degraded while it is stuck; it is
            # released when it parses cleanly or its transcript goes away.
            kept = set()
            for child in self._pending_unreadable.pop(native_id, set()):
                if child in pending_now:
                    kept.add(child)
                else:
                    self._capture_health.clear_unreadable_session(child)
            if kept:
                self._pending_unreadable[native_id] = kept
            self._capture_health.record_success()
            return CaptureOutcome.STORED

        except UnsafePath as e:
            logger.warning("Refusing Antigravity conversation %r: %s", native_id[:40], e)
            return CaptureOutcome.FAILED
        except Exception as e:
            logger.error(
                "Failed to capture Antigravity conversation %s: %s",
                native_id[:8], e, exc_info=True,
            )
            self._capture_health.record_failure(f"Capture failed: {e}", session_key=native_id)
            return CaptureOutcome.FAILED

    def _shrink_verdict(
        self,
        native_id: str,
        session: Any,
        stored: dict[str, Any],
        summaries: dict[tuple[str, str], dict[str, Any]],
    ) -> CaptureOutcome | None:
        """Decide whether a recapture may replace the stored one.

        Detachment is read from the CURRENT summary parent links, never from
        what the parse happened to produce:

        * a subagent that is still linked but came back with fewer messages
          than before (unreadable, empty or compressed), and a subagent that
          is no longer linked but is not proven held elsewhere, are CARRIED
          forward: their stored sidechain is kept in the new capture
          unchanged;
        * a subagent that is no longer linked is dropped only once the
          session that now owns it holds it, in its stored manifest, with at
          least as many messages as this capture stored for it (or the user
          deleted that session);
        * the parent's own thread, and the conversation as a whole (counting
          carried children as present), must not have shrunk (compression
          guard) → UNCHANGED.

        ``session`` is mutated in place when a child is carried, so the
        converted capture holds the carried sidechains byte-for-byte as the
        stored one did. Returns None when the new capture may be installed;
        KEPT_UNREADABLE when a needed carry could not read the stored
        messages (the stored capture must be kept, never a lossy install).
        """
        old_refs: dict[str, dict[str, Any]] = {}
        old_subs: dict[str, int | None] = {}
        for ref in _list(stored.get("sub_agents")):
            if isinstance(ref, dict) and isinstance(ref.get("agent_id"), str):
                old_refs[ref["agent_id"]] = ref
                old_subs[ref["agent_id"]] = _stored_count(ref.get("message_count"))
        new_subs = {
            ref["agent_id"]: _count(ref.get("message_count"))
            for ref in session.sub_agents or []
            if isinstance(ref, dict) and isinstance(ref.get("agent_id"), str)
        }
        linked = descendant_ids(summaries, native_id, self._gemini_dir)
        # The parent's own thread is compared on its own (see below); capture
        # it before any carried sidechains are folded back in.
        new_main = session.message_count - sum(new_subs.values())

        carried: dict[str, dict[str, Any]] = {}
        dropped_count = 0
        for child, count in old_subs.items():
            if count is None:
                # The stored count is corrupt, so the child's real size is
                # unknown: it can neither be proven held elsewhere nor safely
                # dropped. Carry its stored sidechain forward (the actual
                # count is resolved from the stored messages).
                logger.info(
                    "Antigravity subagent %s of %s has a corrupt stored message_count; "
                    "carrying its stored sidechain forward",
                    child[:8], native_id[:8],
                )
                carried[child] = old_refs[child]
            elif child in linked:
                if new_subs.get(child, 0) < count:
                    logger.info(
                        "Antigravity subagent %s of %s came back shorter; carrying its stored sidechain forward",
                        child[:8], native_id[:8],
                    )
                    carried[child] = old_refs[child]
            elif self._held_elsewhere(child, summaries, count):
                dropped_count += count
            else:
                logger.info(
                    "Antigravity subagent %s left %s but is not stored elsewhere yet; "
                    "carrying its stored sidechain forward",
                    child[:8], native_id[:8],
                )
                carried[child] = old_refs[child]

        carried_counts: dict[str, int] = {}
        if carried:
            carried_counts = self._carry_stored_sidechains(native_id, session, carried)
            if carried_counts is None:
                self._capture_health.record_failure(
                    f"Antigravity conversation {native_id[:8]} kept its stored capture: "
                    "a carried subagent's stored messages could not be read",
                    session_key=native_id,
                )
                return CaptureOutcome.KEPT_UNREADABLE

        # A carried child's actual stored count supersedes a corrupt manifest
        # count, so the compression guard and floor use real numbers.
        resolved_old_subs = {
            child: carried_counts.get(child, count)
            for child, count in old_subs.items()
        }
        stats_total = _stored_count(_dict(stored.get("stats")).get("message_count"))
        if stats_total is None:
            # A missing or non-int total is unknown, never 0 (round 25):
            # degrading it to 0 would silently turn off the compression guard
            # and the retire floor and let a shorter recapture truncate the
            # stored messages. Derive the true total and main-thread count
            # from the stored messages; refuse to shrink when they cannot be
            # read.
            stored_messages = self._stored_messages(native_id)
            if stored_messages is None:
                return CaptureOutcome.UNCHANGED
            old_count = len(stored_messages)
            old_main = sum(
                1 for m in stored_messages if m.get("is_sidechain") is not True
            )
        else:
            old_count = stats_total
            old_main = old_count - sum(
                count for count in resolved_old_subs.values() if count is not None
            )
        if new_main < old_main:
            # Compression guard: a growing or carried subagent must not hide
            # parent messages that went missing.
            return CaptureOutcome.UNCHANGED
        floor = old_count - dropped_count
        if session.message_count < floor:
            return CaptureOutcome.UNCHANGED
        return None

    def _stored_messages(self, native_id: str) -> list[dict[str, Any]] | None:
        """Read a stored capture's messages.jsonl; None when it cannot be
        read or a line cannot be parsed."""
        try:
            sfs_id = sfs_session_id_for(native_id)
        except UnsafePath:
            return None
        session_dir = self._store.get_session_dir(sfs_id)
        if session_dir is None:
            return None
        path = session_dir / "messages.jsonl"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        messages: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                return None
            if not isinstance(msg, dict):
                return None
            messages.append(msg)
        return messages

    def _carry_stored_sidechains(
        self,
        native_id: str,
        session: Any,
        carried: dict[str, dict[str, Any]],
    ) -> dict[str, int] | None:
        """Fold each carried child's STORED sidechain into ``session`` in
        place of its new (partial or missing) version, byte-for-byte.

        The carried children's messages are appended after the parent's own
        thread and any freshly parsed sidechains, mirroring the converter's
        sidechain ordering; their ``sub_agents`` refs keep the stored model
        and are re-pointed at the actual stored message count. Returns
        ``{child: actual stored message count}`` for the carried children, or
        None when a stored child's messages cannot be read or — when the
        stored count is a real int — do not match it."""
        messages = self._stored_messages(native_id)
        if messages is None:
            return None
        by_child: dict[str, list[dict[str, Any]]] = {child: [] for child in carried}
        for msg in messages:
            agent = msg.get("agent_id")
            if isinstance(agent, str) and msg.get("is_sidechain") is True and agent in by_child:
                by_child[agent].append(msg)
        counts: dict[str, int] = {}
        for child, ref in carried.items():
            expected = _stored_count(ref.get("message_count"))
            if expected is not None and len(by_child[child]) != expected:
                return None
            counts[child] = len(by_child[child])
        kept = [
            m for m in session.messages
            if not (m.get("is_sidechain") and m.get("agent_id") in by_child)
        ]
        for child in carried:
            kept.extend(by_child[child])
        session.messages = kept
        session.message_count = len(kept)
        fresh_refs = [
            ref for ref in (session.sub_agents or [])
            if isinstance(ref, dict) and isinstance(ref.get("agent_id"), str)
            and ref.get("agent_id") not in carried
        ]
        for child, ref in carried.items():
            carried_ref = dict(ref)
            carried_ref["message_count"] = counts[child]
            fresh_refs.append(carried_ref)
        session.sub_agents = fresh_refs
        self._recompute_aggregate_stats(session)
        return counts

    @staticmethod
    def _recompute_aggregate_stats(session: Any) -> None:
        """Re-derive the token/tool aggregates from the final message list.

        A carry folds stored sidechain messages in after the fresh parse, so
        the token and tool totals that the fresh parse produced (which reflect
        the now-empty or partial child) no longer match ``session.messages``.
        Recompute them from the messages so the written manifest's stats and
        tools match the folded messages. ``turn_count`` is intentionally left
        alone: the converter derives it from the parent's own thread only, and
        a carried child never changes that thread."""
        input_tokens = 0
        output_tokens = 0
        tool_use_count = 0
        tools_used: set[str] = set()
        for msg in session.messages:
            usage = msg.get("usage")
            if isinstance(usage, dict):
                input_tokens += _count(usage.get("input_tokens"))
                output_tokens += _count(usage.get("output_tokens"))
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_use_count += 1
                        name = block.get("name")
                        if isinstance(name, str):
                            tools_used.add(name)
        session.total_input_tokens = input_tokens
        session.total_output_tokens = output_tokens
        session.tool_use_count = tool_use_count
        session.tools_used = tools_used

    @staticmethod
    def _stored_sidechain_ids(manifest: dict[str, Any] | None) -> set[str]:
        """The subagents a stored capture already holds as sidechains.

        A pending (0-message) linked child that already has a stored sidechain
        is carried forward by ``_shrink_verdict`` rather than deferring the
        parent forever."""
        if manifest is None:
            return set()
        return {
            ref["agent_id"]
            for ref in _list(manifest.get("sub_agents"))
            if isinstance(ref, dict) and isinstance(ref.get("agent_id"), str)
        }

    @staticmethod
    def _thread_counts(manifest: dict[str, Any]) -> tuple[int | None, dict[str, int | None]]:
        """(main-thread message count, {subagent id: message count}) held by
        a stored capture. A missing or non-int count is None (unknown), never
        0: callers must derive the true count from the stored messages or
        refuse to act."""
        subs: dict[str, int | None] = {}
        for ref in _list(manifest.get("sub_agents")):
            if isinstance(ref, dict) and isinstance(ref.get("agent_id"), str):
                subs[ref["agent_id"]] = _stored_count(ref.get("message_count"))
        total = _stored_count(_dict(manifest.get("stats")).get("message_count"))
        if total is None:
            return None, subs
        known = [c for c in subs.values() if c is not None]
        if len(known) != len(subs):
            # A subagent count is unknown, so the main thread cannot be
            # derived from the total alone.
            return None, subs
        return total - sum(known), subs

    def _held_elsewhere(
        self,
        child_id: str,
        summaries: dict[tuple[str, str], dict[str, Any]],
        min_messages: int = 0,
    ) -> bool:
        """Whether a detached subagent is proven held elsewhere and may be
        dropped from its old parent.

        Proof comes only from the owner's STORED capture: the manifest must
        claim the child AND the owner's stored messages.jsonl must actually
        contain at least ``min_messages`` messages for it. A manifest written
        just before a crash that never wrote its messages file is not proof.
        The owner is never recaptured here, so an owner whose capture is
        still in flight or compression-guarded releases the child only once
        an earlier stored capture already holds it. A hard local or
        everywhere user deletion of the owner (no transient reason) also
        releases the child. Anything less keeps the child carried forward, so
        a deleted-at-source child (no row, no transcript) is never dropped on
        its own: only a real owner's proof or tombstone releases it."""
        app = self._app_of(child_id, summaries)
        resolved = top_level_conversation(child_id, app, summaries, self._gemini_dir) if app else None
        owner = resolved[0] if resolved else child_id
        try:
            owner_sfs = sfs_session_id_for(owner)
        except UnsafePath:
            return False
        entry = _deleted.get_entry(owner_sfs, base_dir=self._store.store_dir)
        if entry is not None and not str(entry.get("reason") or "").startswith(MERGED_REASON_PREFIX):
            # Only a hard exclusion that removed the LOCAL copy releases the
            # child: a cloud-only deletion keeps the local copy, and a
            # transient auto-exclusion (too_large) is not a user deletion.
            if entry.get("scope") in ("local", "everywhere") and entry.get("reason") not in _deleted.TRANSIENT_REASONS:
                return True  # the user deleted the session that now owns it
        manifest = self._stored_manifest(owner_sfs)
        if manifest is None:
            return False
        if owner == child_id:
            # The child is now its own top-level session: its own thread is
            # the main thread of the owner's capture.
            if _dict(manifest.get("source")).get("original_session_id") != child_id:
                return False
            main = self._stored_main_count(owner)
            return main is not None and main >= min_messages
        claimed = any(
            isinstance(s, dict) and s.get("agent_id") == child_id
            and _count(s.get("message_count")) >= min_messages
            for s in _list(manifest.get("sub_agents"))
        )
        if not claimed:
            return False
        sidechain = self._stored_sidechain_messages(owner, child_id)
        return sidechain is not None and len(sidechain) >= min_messages

    def _stored_main_count(self, owner_id: str) -> int | None:
        """The main-thread message count of a stored capture, or None when its
        messages.jsonl cannot be read."""
        messages = self._stored_messages(owner_id)
        if messages is None:
            return None
        return sum(1 for m in messages if m.get("is_sidechain") is not True)

    def _stored_sidechain_messages(
        self, owner_id: str, child_id: str,
    ) -> list[dict[str, Any]] | None:
        """The stored messages an owner holds for ``child_id`` as a sidechain,
        or None when its messages.jsonl cannot be read."""
        messages = self._stored_messages(owner_id)
        if messages is None:
            return None
        return [
            m for m in messages
            if m.get("is_sidechain") is True and m.get("agent_id") == child_id
        ]

    # -- subagents captured before their parent row existed ------------------

    def _tracked_antigravity(self) -> list[tuple[str, str | None]]:
        try:
            rows = self._store.index.conn.execute(
                "SELECT native_session_id, sfs_session_id FROM tracked_sessions WHERE tool = ?",
                (TOOL_NAME,),
            ).fetchall()
        except Exception:
            return []
        return [(str(r[0]), r[1]) for r in rows]

    def _reconcile_subagents(self, summaries: dict[tuple[str, str], dict[str, Any]]) -> None:
        """Fold standalone captures of conversations that turned out to be
        subagents (their parent row committed later) into the parent.

        The parent is recaptured first; only when that capture was STORED in
        this pass and contains the child's sidechain is the standalone capture
        retired, with a reason recorded in deleted.json (``sfs trash``).
        """
        for native_id, child_sfs in self._tracked_antigravity():
            if not child_sfs:
                continue
            app = self._app_of(native_id, summaries)
            if app is None:
                continue
            resolved = top_level_conversation(native_id, app, summaries, self._gemini_dir)
            if resolved is None or resolved[0] == native_id:
                continue
            parent_id, _parent_dir = resolved
            if not self._owner_stored_now(parent_id, summaries):
                continue  # retried on a later pass
            try:
                parent_sfs = sfs_session_id_for(parent_id)
            except UnsafePath:
                continue
            if not self._parent_holds_child(parent_sfs, native_id):
                continue
            child_manifest = self._stored_manifest(child_sfs)
            if child_manifest is None:
                # The standalone capture cannot be read to compare it: only
                # retire when there is nothing left on disk to lose.
                child_dir = self._store.get_session_dir(child_sfs)
                if child_dir is not None and child_dir.exists():
                    continue
            else:
                parent_manifest = self._stored_manifest(parent_sfs)
                if parent_manifest is None or not self._owner_covers(
                    child_manifest, parent_manifest, native_id
                ):
                    # The new capture holds less of the child (a compressed or
                    # partial sidechain) than the standalone capture: keep the
                    # standalone one; retried on a later pass.
                    continue
            self._retire_standalone(native_id, child_sfs, parent_sfs)

    def _owner_covers(
        self, standalone: dict[str, Any], owner: dict[str, Any], child_id: str
    ) -> bool:
        """Whether the owner's capture retains at least as much of the child
        as its standalone capture does: the child's own thread, and every
        sidechain the standalone held, each with at least as many messages.

        A missing or non-int count in the standalone's manifest is unknown,
        never 0 (round 25): the child's own thread is re-derived from its
        stored messages, and any count that cannot be determined refuses the
        retire (the standalone capture is kept rather than dropped)."""
        main, subs = self._thread_counts(standalone)
        if main is None:
            main = self._stored_main_count(child_id)
            if main is None:
                return False
        _owner_main, owner_subs = self._thread_counts(owner)
        owner_child = owner_subs.get(child_id)
        if owner_child is None or owner_child < main:
            return False
        for g, count in subs.items():
            if count is None:
                return False  # standalone's sub-subagent count is unknown
            owner_g = owner_subs.get(g)
            if owner_g is None or owner_g < count:
                return False
        return True

    def _apply_summary_changes(
        self,
        old: dict[tuple[str, str], dict[str, Any]],
        new: dict[tuple[str, str], dict[str, Any]],
        handled: set[str],
    ) -> None:
        """Recapture every top-level session whose metadata or topology
        changed: titles, workspaces, status, and parent links added or
        removed. Both the old and the new top-level owner of a changed
        conversation are refreshed (if top-level now). Sessions without a
        stored capture go first, so a child that became top-level is stored
        before its old parent is allowed to shrink — and _shrink_verdict
        enforces that even if this order is not enough."""
        changed = [
            key for key in set(old) | set(new)
            if relevant_summary(old.get(key)) != relevant_summary(new.get(key))
        ]
        if not changed:
            return
        targets: dict[str, Path] = {}
        for app, cid in changed:
            for rows in (new, old):
                resolved = top_level_conversation(cid, app, rows, self._gemini_dir)
                if resolved is None:
                    continue
                top_id, conv_dir = resolved
                now = top_level_conversation(top_id, conv_dir.parent.parent.name, new, self._gemini_dir)
                if now is None or now[0] != top_id:
                    continue  # an old owner that is itself a subagent now: reconcile folds it
                targets.setdefault(top_id, conv_dir)
        self._capture_targets(targets, new, handled)

    def _capture_targets(
        self,
        targets: dict[str, Path],
        summaries: dict[tuple[str, str], dict[str, Any]],
        handled: set[str],
        *,
        force: bool = False,
    ) -> None:
        """Capture the given top-level sessions, unstored ones first."""
        for top_id in sorted(targets, key=self._is_stored):
            conv_dir = targets[top_id]
            handled.add(top_id)
            mtime, size = session_fingerprint(conv_dir, summaries)
            if not force and self._unchanged(top_id, mtime, size) and self._digest_current(top_id, summaries):
                self._retry.pop(top_id, None)
                continue
            self._capture(top_id, conv_dir, mtime, size, summaries)

    def _retry_targets(self, ids: list[str], summaries: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Path]:
        targets: dict[str, Path] = {}
        for native_id in ids:
            app = self._app_of(native_id, summaries)
            resolved = top_level_conversation(native_id, app, summaries, self._gemini_dir) if app else None
            if resolved is None:
                # No summary row: find it on disk directly.
                for root in antigravity_roots(self._gemini_dir):
                    from sessionfs.converters.antigravity_to_sfs import locate_conversation_dir

                    found = locate_conversation_dir(self._gemini_dir, root.name, native_id)
                    if found is not None:
                        resolved = (native_id, found)
                        break
            if resolved is None:
                self._retry.pop(native_id, None)  # the conversation is gone
                self._unreadable.pop(native_id, None)
                self._capture_health.clear_unreadable_session(native_id)
                # Release the unreadable latches of any pending children this
                # session linked before it disappeared (a deleted parent must
                # not leave its children degrading the watcher forever).
                self._release_parent_pending(native_id)
                continue
            if resolved[0] != native_id:
                self._retry.pop(native_id, None)  # folded into another session now
                resolved_top = resolved[0]
                targets.setdefault(resolved_top, resolved[1])
                continue
            targets[native_id] = resolved[1]
        return targets

    @staticmethod
    def _app_of(cid: str, summaries: dict[tuple[str, str], dict[str, Any]]) -> str | None:
        for app, other in summaries:
            if other == cid:
                return app
        return None

    def _parent_holds_child(self, parent_sfs: str, child_id: str) -> bool:
        manifest = self._stored_manifest(parent_sfs)
        if manifest is None:
            return False
        subs = _list(manifest.get("sub_agents"))
        return any(isinstance(s, dict) and s.get("agent_id") == child_id for s in subs)

    def _retire_standalone(self, child_id: str, child_sfs: str, parent_sfs: str) -> None:
        import shutil

        from sessionfs.store.index import delete_session_rows

        base = self._store.store_dir
        if child_sfs == parent_sfs:
            return
        try:
            # Record the merge. A hard user deletion or an earlier merge
            # record is kept; a stale cloud-only or transient (too_large)
            # entry — neither of which removed the local copy — is replaced,
            # since the standalone copy is gone now.
            _deleted.mark_merged(
                child_sfs, f"{MERGED_REASON_PREFIX}{parent_sfs}", base_dir=base,
            )
        except Exception as exc:
            logger.warning("Could not record merge of %s: %s", child_sfs, exc)
            return  # never remove a capture without the note
        session_dir = self._store.get_session_dir(child_sfs)
        if session_dir is not None and session_dir.exists():
            try:
                shutil.rmtree(session_dir)
            except OSError as exc:
                # Keep the index and tracking rows so the next pass retries;
                # the exclusion keeps it from being recaptured meanwhile.
                logger.warning(
                    "Could not remove the merged capture %s (%s); will retry", child_sfs, exc,
                )
                return
        try:
            conn = self._store.index.conn
            conn.execute("DELETE FROM tracked_sessions WHERE native_session_id = ?", (child_id,))
            delete_session_rows(conn, child_sfs)
        except Exception as exc:
            logger.warning("Could not clean index for %s: %s", child_sfs, exc)
        self._tracked.pop(child_id, None)
        self._digests.pop(child_id, None)
        logger.info(
            "Antigravity conversation %s is a subagent of %s: its standalone capture "
            "%s was merged into %s (recorded in deleted.json; see `sfs trash`)",
            child_id[:8], parent_sfs, child_sfs, parent_sfs,
        )

    # -- filesystem events ---------------------------------------------------

    def start_watching(self) -> None:
        handler = _AntigravityEventHandler(
            self._event_queue, self._event_lock, self._rescan, self._summaries_changed,
            self._skipped_dirty,
        )
        observer = Observer()
        paths: list[str] = []
        for root in antigravity_roots(self._gemini_dir):
            # The root itself, non-recursively: conversation_summaries.db/-wal
            # and the moment brain/ is created.
            observer.schedule(handler, str(root), recursive=False)
            paths.append(str(root))
            brain = root / "brain"
            if brain.is_dir() and not brain.is_symlink():
                observer.schedule(handler, str(brain), recursive=True)
                paths.append(str(brain))
            for sub in ("conversations", "annotations"):
                folder = root / sub
                if folder.is_dir() and not folder.is_symlink():
                    # Non-recursive: .pb/.db files update the skipped counts;
                    # .pbtxt annotations carry the title fallback.
                    observer.schedule(handler, str(folder), recursive=False)
                    paths.append(str(folder))
        present = {p.name for p in antigravity_roots(self._gemini_dir)}
        if self._gemini_dir.is_dir() and any(n not in present for n in ROOT_NAMES):
            # A root that is created later (e.g. agy installed after the
            # daemon started): non-recursive, so ~/.gemini/tmp is not watched.
            observer.schedule(handler, str(self._gemini_dir), recursive=False)
            paths.append(str(self._gemini_dir))
        if not paths:
            return
        observer.daemon = True
        observer.start()
        self._observer = observer
        self._watch_paths = paths
        logger.info("Watching %s for Antigravity session changes", ", ".join(paths))

    def stop_watching(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

    def process_events(self) -> None:
        now = time.monotonic()
        if now - self._last_event_time < self._scan_interval:
            return

        if self._rescan.is_set():
            self._rescan.clear()
            self._summaries_changed.clear()
            self._last_event_time = now
            with self._event_lock:
                self._event_queue.clear()
            self.stop_watching()
            self.start_watching()
            self.full_scan()
            return

        if self._scan_pending:
            self._summaries_changed.clear()
            self._last_event_time = now
            self.full_scan()
            return

        with self._event_lock:
            # Consume the event queue and the summaries flag in one locked
            # step; the handler sets the flag under the same lock, so a
            # summaries event can never land between the check and the clear.
            summaries_changed = self._summaries_changed.is_set()
            if summaries_changed:
                self._summaries_changed.clear()
            paths = list(dict.fromkeys(self._event_queue))
            self._event_queue.clear()
        due = self._due_retries(now)
        skipped_dirty = self._skipped_dirty.is_set()
        if not paths and not summaries_changed and not due and not skipped_dirty:
            return
        self._last_event_time = now
        self._stored_this_pass.clear()

        # A forced refresh re-reads the DBs; a DB that fell back to cached
        # rows re-queues another refresh (see _load_summaries).
        previous = self._last_summaries
        summaries, trusted = self._load_summaries(force=summaries_changed)
        if not trusted:
            # Keep the events for the next pass (the summaries are re-read).
            with self._event_lock:
                self._event_queue[:0] = [p for p in paths if p not in self._event_queue]
            return

        handled: set[str] = set()
        if summaries_changed:
            # Topology and metadata first, so ordinary transcript events below
            # see the sessions already re-homed.
            self._apply_summary_changes(previous, summaries, handled)

        changed: dict[str, str] = {}
        for path_str in paths:
            located = conversation_id_from_path(path_str)
            if located is not None:
                root, cid = located
                changed.setdefault(cid, root.name)

        targets: dict[str, Path] = {}
        for cid, app in changed.items():
            resolved = top_level_conversation(cid, app, summaries, self._gemini_dir)
            if resolved is not None and resolved[0] not in handled:
                targets.setdefault(resolved[0], resolved[1])
        self._capture_targets(targets, summaries, handled)

        # Captures that were deferred or failed earlier and are due again.
        retry = {k: v for k, v in self._retry_targets(due, summaries).items() if k not in handled}
        self._capture_targets(retry, summaries, handled, force=True)

        if summaries_changed or self._stored_this_pass:
            # A stored owner may now hold a subagent that still has its own
            # standalone capture.
            self._reconcile_subagents(summaries)
        self._sweep_pending(summaries)
        # Keep the skipped (encrypted / protobuf-only) counts current.
        self._skipped_dirty.clear()
        self._refresh_skipped()
        with self._event_lock:
            if self._summaries_changed.is_set():
                # A summaries event landed mid-pass: the rows loaded above may
                # already include the change, so keep the pre-pass rows for
                # the next pass to diff against — the flag stays set for it.
                self._last_summaries = previous
            else:
                self._last_summaries = summaries
    def get_status(self) -> WatcherStatus:
        return WatcherStatus(
            name=TOOL_NAME,
            enabled=True,
            health=self._capture_health.health.value,
            sessions_tracked=len(self._tracked),
            last_scan_at=self._last_scan_at,
            last_error=self._capture_health.last_error,
            degraded_since=self._capture_health.degraded_since,
            watch_paths=list(self._watch_paths)
            or [str(self._gemini_dir / name / "brain") for name in ROOT_NAMES],
            sessions_skipped=dict(self._skipped),
        )
