"""Crash-recoverable local application of deterministic skill plans."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sessionfs.skills.models import EmitPlan, PackageRecord, SkillError
from sessionfs.skills.path_safety import is_link_or_reparse


# ONE ceiling shared by the reader, the writer and instruction provenance:
# a state file the writer produces must always be readable back.
MAX_STATE_BYTES = 32 * 1024 * 1024

# Managed outputs above this size are treated as dirty without hashing so a
# sparse or huge file can never exhaust memory or stall a remove/sync.
MAX_HASH_BYTES = 256 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024

# Windows `msvcrt.locking(LK_LOCK)` gives up after ~10 one-second attempts; a
# sync waiting behind a long install would then fail spuriously. The lock is
# retried non-blocking every 250ms for this budget instead and fails closed
# after it (POSIX flock blocks; this is the bounded equivalent).
LOCK_WAIT_SECONDS = 120.0
_LOCK_RETRY_SECONDS = 0.25
_lock_sleep = time.sleep
_lock_clock = time.monotonic


def read_state_bytes(path: Path, *, limit: int = MAX_STATE_BYTES) -> bytes | None:
    """Bounded, no-follow read of a state/lock/journal file.

    Opens with O_NOFOLLOW (explicit link check where unavailable), refuses
    anything that is not a regular file (a symlink to /dev/zero would hang an
    unbounded read), and never pulls more than `limit` bytes.
    """
    import stat as _stat

    if is_link_or_reparse(path):
        raise SkillError(f"Skill state file cannot be a symlink: {path}")
    # O_NONBLOCK: opening a FIFO/device read-only would otherwise block until a
    # writer appears; the S_ISREG check below rejects it right after the open.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SkillError(f"Cannot read skill state {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise SkillError(f"Skill state file is not a regular file: {path}")
        if info.st_size > limit:
            raise SkillError(f"Skill state file exceeds the {limit}-byte limit: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > limit:
        raise SkillError(f"Skill state file exceeds the {limit}-byte limit: {path}")
    return raw


def read_json(path: Path, default: Any) -> Any:
    raw = read_state_bytes(path)
    if raw is None:
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillError(f"Cannot read skill state {path}: {exc}") from exc


def atomic_json(path: Path, value: Any) -> None:
    if is_link_or_reparse(path.parent):
        raise SkillError(f"Skill state directory cannot be a symlink: {path.parent}")
    # Serialize BEFORE touching the filesystem: a state file larger than the
    # reader accepts would brick every later list/sync/doctor/recovery, so the
    # mutation is refused and the previous file is left untouched.
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise SkillError(
            f"Refusing to write skill state larger than the {MAX_STATE_BYTES}-byte limit "
            f"({len(encoded)} bytes): {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_reparse(path) or (path.exists() and not path.is_file()):
        raise SkillError(f"Skill state file cannot be a symlink or special file: {path}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if is_link_or_reparse(path):
            raise SkillError(f"Skill state file cannot be a symlink: {path}")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    # The rename itself must be durable: a journal that names backups which
    # never reached the disk is worse than no journal.
    _fsync_directory(path.parent)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    """fsync a directory entry after a rename (no-op where the platform cannot
    open directories, e.g. Windows - the rename is still atomic there)."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def copy_backup(source: Path, destination: Path) -> None:
    """Copy a file that is about to be overwritten or unlinked and make the copy
    durable BEFORE the (fsynced) journal that references it is written: on a
    power loss the journal must never point at a backup that does not exist."""
    shutil.copyfile(source, destination)
    _fsync_file(destination)


def _lock_with_msvcrt(fd: int, msvcrt: Any) -> None:
    """Bounded retry around Windows byte-range locking (see LOCK_WAIT_SECONDS)."""
    deadline = _lock_clock() + LOCK_WAIT_SECONDS
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if _lock_clock() >= deadline:
                raise SkillError(
                    f"Timed out after {LOCK_WAIT_SECONDS:.0f}s waiting for the skill lock; "
                    "another sfs skill operation is still running"
                ) from exc
            _lock_sleep(_LOCK_RETRY_SECONDS)


@contextlib.contextmanager
def interprocess_lock(path: Path) -> Iterator[None]:
    """Hold one scope lock on POSIX and Windows.

    The lock file is opened without following symlinks (O_NOFOLLOW where the
    platform has it, an explicit link check otherwise), its parent directory
    must not be a symlink, and no bytes are ever written to it: the lock is
    taken on the descriptor only. A pre-planted symlink at the lock path can
    therefore never make us touch an arbitrary user-writable file.
    """
    parent = path.parent
    if is_link_or_reparse(parent):
        raise SkillError(f"Lock directory cannot be a symlink: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_reparse(path):
        raise SkillError(f"Lock file cannot be a symlink: {path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SkillError(f"Cannot open lock file {path}: {exc}") from exc
    try:
        if is_link_or_reparse(path) or not os.path.isfile(path):
            raise SkillError(f"Lock file cannot be a symlink: {path}")
        try:
            import fcntl
        except ImportError:  # pragma: no cover - exercised on Windows CI
            import msvcrt

            _lock_with_msvcrt(fd, msvcrt)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _emission_hash(source: Path, emission: Any) -> str | None:
    """Hash of the bytes that will actually be written: the source bytes with
    the emission's text substitutions applied (none for plain copies)."""
    if not getattr(emission, "substitutions", ()):
        return _sha256_file(source)
    import hashlib

    try:
        if is_link_or_reparse(source) or not source.is_file():
            return None
        return "sha256:" + hashlib.sha256(emission.transform(source.read_bytes())).hexdigest()
    except (OSError, UnicodeDecodeError):
        return None


def _oversize_marker(size: int) -> str:
    """Disclosed stand-in for the hash of a file too large to hash.

    Never equal to a recorded ``sha256:`` output hash, so the output is
    treated as dirty (kept, never overwritten or unlinked) and the reason is
    visible wherever the actual hash is reported.
    """
    return f"unhashed:file exceeds the {MAX_HASH_BYTES}-byte hashing limit ({size} bytes)"


def _sha256_file(path: Path) -> str | None:
    import hashlib

    try:
        if is_link_or_reparse(path) or not path.is_file():
            return None
        size = path.stat().st_size
        if size > MAX_HASH_BYTES:
            return _oversize_marker(size)
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return None


def _restore_backup(destination: Path, backup: Path | None) -> None:
    if backup and (is_link_or_reparse(backup) or is_link_or_reparse(destination)):
        raise SkillError(f"Refusing to restore through a symlink: {destination}")
    if backup is not None:
        # A recorded backup means the destination EXISTED before the
        # transaction. If the copy is gone, unlinking would destroy a
        # pre-existing file: stop and leave everything (journal included)
        # for manual recovery instead.
        if not backup.is_file():
            raise SkillError(
                f"Transaction backup is missing for {destination} (expected at {backup}); "
                "the file was left untouched - manual recovery required: restore it by "
                "hand, then delete skills.journal.json and the .skills-txn-* directory"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(destination.name + f".restore-{os.getpid()}")
        shutil.copyfile(backup, tmp)
        os.replace(tmp, destination)
        return
    try:
        destination.unlink()
    except FileNotFoundError:
        pass


class SkillStateStore:
    def __init__(
        self,
        state_root: Path,
        *,
        scope_root: Path | None = None,
        allowed_output_roots: tuple[Path, ...] | None = None,
    ) -> None:
        self.root = state_root
        self.scope_root = scope_root or state_root.parent
        self.allowed_output_roots = allowed_output_roots or (state_root.parent,)
        self.state_path = state_root / "skills.state.json"
        self.lock_path = state_root / "skills.lock.json"
        self.journal_path = state_root / "skills.journal.json"
        self.mutex_path = state_root / "skills.lock"

    @staticmethod
    def _lexical(path: Path) -> Path:
        return Path(os.path.abspath(os.fspath(path)))

    @staticmethod
    def _assert_no_symlink_components(root: Path, path: Path) -> None:
        if is_link_or_reparse(root):
            raise SkillError(f"Managed root is a symlink: {root}")
        current = root
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise SkillError(f"Managed path escapes its declared root: {path}") from exc
        for part in relative.parts:
            current /= part
            if is_link_or_reparse(current):
                raise SkillError(f"Managed path traverses a symlink: {current}")
            if current.exists() and current != path and not current.is_dir():
                raise SkillError(f"Managed path parent is not a directory: {current}")

    def _validate_state_root(self) -> None:
        root = self._lexical(self.root)
        scope = self._lexical(self.scope_root)
        self._assert_no_symlink_components(scope, root)

    def _validate_output(self, path: Path) -> Path:
        destination = self._lexical(path)
        scope = self._lexical(self.scope_root)
        for raw_root in self.allowed_output_roots:
            root = self._lexical(raw_root)
            try:
                destination.relative_to(root)
            except ValueError:
                continue
            self._assert_no_symlink_components(scope, root)
            self._assert_no_symlink_components(root, destination)
            return destination
        raise SkillError(f"Managed output is outside adapter-owned roots: {path}")

    def _output_root(self, path: Path) -> Path:
        destination = self._lexical(path)
        for raw_root in self.allowed_output_roots:
            root = self._lexical(raw_root)
            try:
                destination.relative_to(root)
            except ValueError:
                continue
            return root
        raise SkillError(f"Managed output is outside adapter-owned roots: {path}")

    @staticmethod
    def _owner_by_path(
        packages: dict[str, Any], *, exclude: set[str] | None = None
    ) -> dict[str, tuple[str, str, str]]:
        """Map every recorded output path to (owner package id, output hash, owner name)."""
        owner_by_path: dict[str, tuple[str, str, str]] = {}
        for owner_id, row in packages.items():
            if not isinstance(row, dict) or (exclude and owner_id in exclude):
                continue
            owner_name = str(row.get("name") or "")
            for output in row.get("outputs") or []:
                if isinstance(output, dict) and isinstance(output.get("path"), str):
                    owner_by_path[output["path"]] = (
                        owner_id,
                        str(output.get("output_hash", "")),
                        owner_name,
                    )
        return owner_by_path

    @staticmethod
    def _revision(existing: dict[str, Any] | None) -> int:
        """Monotonic per-row revision (legacy rows without one are 0)."""
        if not isinstance(existing, dict):
            return 0
        value = existing.get("revision", 0)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    @staticmethod
    def _previous_package_ids(
        existing: dict[str, Any] | None, previous_id: str | None, package_id: str
    ) -> list[str]:
        """Ordered, de-duplicated ids this row has been known under (newest last),
        never including the row's current id."""
        ids: list[str] = []
        if isinstance(existing, dict):
            for item in existing.get("previous_package_ids") or []:
                if isinstance(item, str) and item and item not in ids:
                    ids.append(item)
        if previous_id and previous_id not in ids:
            ids.append(previous_id)
        return [item for item in ids if item != package_id]

    @staticmethod
    def _exceeds_hash_limit(path: Path) -> bool:
        try:
            return (
                not is_link_or_reparse(path)
                and path.is_file()
                and path.stat().st_size > MAX_HASH_BYTES
            )
        except OSError:
            return False

    @staticmethod
    def _assert_not_owned_by_other(
        owner: tuple[str, str, str] | None, package_id: str, destination: Path
    ) -> None:
        if owner is not None and owner[0] != package_id:
            label = f"{owner[0]} ({owner[2]!r})" if owner[2] else owner[0]
            raise SkillError(
                f"Output is owned by package {label}: {destination}; "
                "remove that package first or choose different targets"
            )

    def _prune_empty_parents(self, removed: list[Path]) -> list[str]:
        """Remove now-empty directories above each removed file, stopping at
        (never including) the adapter root. Only empty, non-symlink directories
        are removed, so a directory holding a user's dirty file survives."""
        pruned: list[str] = []
        for path in removed:
            root = self._output_root(path)
            current = path.parent
            while current != root and root in current.parents:
                try:
                    if is_link_or_reparse(current) or not current.is_dir():
                        break
                    if any(current.iterdir()):
                        break
                    current.rmdir()
                except OSError:
                    break
                pruned.append(str(current))
                current = current.parent
        return pruned

    def _validate_transaction_path(self, path: Path, tx_dir: Path) -> Path:
        candidate = self._lexical(path)
        transaction_root = self._lexical(tx_dir)
        try:
            candidate.relative_to(transaction_root)
        except ValueError as exc:
            raise SkillError(f"Recovery path escapes its transaction directory: {path}") from exc
        self._assert_no_symlink_components(transaction_root, candidate)
        return candidate

    def state(self) -> dict[str, Any]:
        self._validate_state_root()
        value = read_json(self.state_path, {"version": 1, "packages": {}})
        if not isinstance(value, dict) or not isinstance(value.get("packages"), dict):
            raise SkillError("skills.state.json must contain a packages object")
        return value

    def desired(self) -> dict[str, Any]:
        self._validate_state_root()
        value = read_json(self.lock_path, {"version": 1, "packages": {}})
        if not isinstance(value, dict) or not isinstance(value.get("packages"), dict):
            raise SkillError("skills.lock.json must contain a packages object")
        return value

    def _recover_locked(self) -> None:
        self._validate_state_root()
        if not self.journal_path.exists():
            return
        journal = read_json(self.journal_path, {})
        if not isinstance(journal, dict):
            raise SkillError("Invalid skill transaction journal")
        tx_dir_raw = journal.get("tx_dir")
        if not isinstance(tx_dir_raw, str):
            raise SkillError("Skill transaction journal has no recovery directory")
        tx_dir = self._lexical(Path(tx_dir_raw))
        expected_parent = self._lexical(self.root)
        if tx_dir.parent != expected_parent or not tx_dir.name.startswith(".skills-txn-"):
            raise SkillError("Skill transaction journal has an unsafe recovery directory")
        if journal.get("phase") != "committed":
            created: list[Path] = []
            for action in reversed(journal.get("actions") or []):
                if not isinstance(action, dict) or not isinstance(action.get("destination"), str):
                    continue
                backup_raw = action.get("backup")
                destination = self._validate_output(Path(action["destination"]))
                backup = (
                    self._validate_transaction_path(Path(backup_raw), tx_dir)
                    if isinstance(backup_raw, str)
                    else None
                )
                _restore_backup(destination, backup)
                if backup is None:
                    created.append(destination)
            # Files that did not exist before the transaction are gone again;
            # drop the directory skeletons the aborted writes created.
            self._prune_empty_parents(created)
            for key, path in (("state_backup", self.state_path), ("lock_backup", self.lock_path)):
                backup_raw = journal.get(key)
                existed = bool(journal.get(key + "_existed"))
                if existed and isinstance(backup_raw, str):
                    backup = self._validate_transaction_path(Path(backup_raw), tx_dir)
                    _restore_backup(path, backup)
                elif not existed:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
        try:
            self.journal_path.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(tx_dir, ignore_errors=True)

    def recover(self) -> None:
        self._validate_state_root()
        with interprocess_lock(self.mutex_path):
            self._recover_locked()

    def validate_root(self) -> None:
        """Validate the state path before an operation lock file is created."""
        self._validate_state_root()

    def apply(
        self,
        package: PackageRecord,
        plans: list[EmitPlan],
        *,
        consented_capabilities: set[str] | None = None,
        expected_current_hash: str | None = None,
        allow_existing_change: bool = False,
        replace_package_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._validate_state_root()
        with interprocess_lock(self.mutex_path):
            self._recover_locked()
            state = self.state()
            desired = self.desired()
            packages = state["packages"]
            # Content-bound local packages change id when their bytes change;
            # the previous row is migrated (outputs it owns become ours).
            previous_id = (
                replace_package_id
                if replace_package_id and replace_package_id != package.package_id
                else None
            )
            if previous_id is not None and previous_id in packages:
                if isinstance(packages.get(package.package_id), dict):
                    # Two distinct local packages converging on identical bytes
                    # would collapse into one row and orphan the other's outputs.
                    other = packages[package.package_id]
                    raise SkillError(
                        f"Cannot migrate {previous_id} to {package.package_id}: that id is already "
                        f"installed as {other.get('name')!r}; remove one of them first"
                    )
                packages[package.package_id] = packages.pop(previous_id)
                desired["packages"].pop(previous_id, None)
            existing = packages.get(package.package_id)
            previous_ids = self._previous_package_ids(existing, previous_id, package.package_id)
            if isinstance(existing, dict):
                # Prepared operations are fenced by the row REVISION as well as
                # the content hash: a target-changing compile with the same
                # hash that landed after planning must not be overwritten.
                if expected_revision is not None and self._revision(existing) != expected_revision:
                    raise SkillError(
                        f"Installed state changed since planning (revision "
                        f"{self._revision(existing)} != {expected_revision}); re-run the command: "
                        f"{package.package_id}"
                    )
                current_hash = existing.get("content_hash")
                if expected_current_hash is not None and current_hash != expected_current_hash:
                    raise SkillError(
                        f"Installed package changed concurrently: {package.package_id}"
                    )
                if (
                    expected_current_hash is None
                    and not allow_existing_change
                    and current_hash != package.content_hash
                ):
                    raise SkillError(
                        f"Package is already installed at another version; use update: "
                        f"{package.package_id}"
                    )
            elif expected_current_hash is not None:
                raise SkillError(f"Installed package disappeared concurrently: {package.package_id}")
            prior_outputs = existing.get("outputs", []) if isinstance(existing, dict) else []
            owner_by_path = self._owner_by_path(packages)
            emissions = [item for plan in plans for item in plan.emissions]
            new_paths = {item.destination for item in emissions}
            stale = [
                item for item in prior_outputs
                if isinstance(item, dict) and item.get("path") not in new_paths
            ]
            for emission in emissions:
                destination = self._validate_output(Path(emission.destination))
                source = self._lexical(Path(emission.source))
                package_root = self._lexical(package.package_root)
                try:
                    source.relative_to(package_root)
                except ValueError as exc:
                    raise SkillError(f"Planned source escapes package content: {source}") from exc
                self._assert_no_symlink_components(package_root, source)
                if _emission_hash(source, emission) != emission.output_hash:
                    raise SkillError(f"Planned source integrity mismatch: {source}")
                owner = owner_by_path.get(emission.destination)
                # Ownership lives in STATE, not on disk: a managed output that
                # was deleted by hand still belongs to its package, otherwise a
                # second package could claim the path and two rows would own
                # one file.
                self._assert_not_owned_by_other(owner, package.package_id, destination)
                if destination.exists():
                    if is_link_or_reparse(destination) or not destination.is_file():
                        raise SkillError(f"Refusing non-file output collision: {destination}")
                    if owner is None:
                        raise SkillError(f"Refusing unmanaged output collision: {destination}")
                    if _sha256_file(destination) != owner[1]:
                        raise SkillError(f"Refusing to overwrite modified managed output: {destination}")
            self.root.mkdir(parents=True, exist_ok=True)
            tx_dir = self.root / f".skills-txn-{uuid.uuid4().hex}"
            backup_dir = tx_dir / "backups"
            backup_dir.mkdir(parents=True)
            actions: list[dict[str, Any]] = []
            touched = [Path(item.destination) for item in emissions]
            for item in stale:
                if isinstance(item.get("path"), str):
                    stale_path = self._validate_output(Path(item["path"]))
                    # An oversize stale output is dirty by definition (never
                    # unlinked), so it needs no backup; copying it would stall.
                    if not self._exceeds_hash_limit(stale_path):
                        touched.append(stale_path)
            unique_touched = list(dict.fromkeys(str(item) for item in touched))
            for index, raw in enumerate(unique_touched):
                destination = self._validate_output(Path(raw))
                backup: Path | None = None
                if destination.is_file() and not is_link_or_reparse(destination):
                    backup = backup_dir / str(index)
                    copy_backup(destination, backup)
                actions.append(
                    {"destination": str(destination), "backup": str(backup) if backup else None}
                )
            journal: dict[str, Any] = {
                "version": 1,
                "phase": "applying",
                "tx_dir": str(tx_dir),
                "actions": actions,
                "state_backup_existed": self.state_path.exists(),
                "lock_backup_existed": self.lock_path.exists(),
            }
            if self.state_path.exists():
                state_backup = tx_dir / "skills.state.json"
                copy_backup(self.state_path, state_backup)
                journal["state_backup"] = str(state_backup)
            if self.lock_path.exists():
                lock_backup = tx_dir / "skills.lock.json"
                copy_backup(self.lock_path, lock_backup)
                journal["lock_backup"] = str(lock_backup)
            atomic_json(self.journal_path, journal)
            try:
                dirty_stale: list[str] = []
                for item in stale:
                    path_raw = item.get("path")
                    expected = item.get("output_hash")
                    if not isinstance(path_raw, str):
                        continue
                    path = self._validate_output(Path(path_raw))
                    if not path.exists():
                        continue
                    if _sha256_file(path) == expected:
                        path.unlink()
                        self._prune_empty_parents([path])
                    else:
                        dirty_stale.append(path_raw)
                outputs: list[dict[str, Any]] = []
                recorded_outputs: dict[str, dict[str, Any]] = {}
                for plan in plans:
                    for emission in plan.emissions:
                        source = self._lexical(Path(emission.source))
                        destination = self._validate_output(Path(emission.destination))
                        # Tools sharing one native root (cursor + amp both use
                        # .agents/skills) plan the same output; write and record
                        # it once so removal never unlinks the same path twice.
                        previous = recorded_outputs.get(str(destination))
                        if previous is not None:
                            if previous["output_hash"] != emission.output_hash:
                                raise SkillError(
                                    f"Conflicting planned content for shared output: {destination}"
                                )
                            previous["tools"].append(plan.tool)
                            continue
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        fd, tmp_name = tempfile.mkstemp(
                            prefix=f".{destination.name}.", dir=destination.parent
                        )
                        try:
                            with os.fdopen(fd, "wb") as handle:
                                handle.write(emission.transform(source.read_bytes()))
                                handle.flush()
                                os.fsync(handle.fileno())
                            os.chmod(tmp_name, 0o600)
                            os.replace(tmp_name, destination)
                        except Exception:
                            try:
                                os.unlink(tmp_name)
                            except OSError:
                                pass
                            raise
                        record = {
                            "path": emission.destination,
                            "output_hash": emission.output_hash,
                            "tool": plan.tool,
                            "tools": [plan.tool],
                        }
                        recorded_outputs[str(destination)] = record
                        outputs.append(record)
                package_row = package.as_dict(include_path=False)
                capabilities = sorted(consented_capabilities or set())
                prior_receipts = (
                    existing.get("consent_receipts", [])
                    if isinstance(existing, dict)
                    and existing.get("content_hash") == package.content_hash
                    else []
                )
                receipts = [item for item in prior_receipts if isinstance(item, dict)]
                covered = {
                    (item.get("capability"), item.get("tool"), item.get("scope"))
                    for item in receipts
                }
                for capability in capabilities:
                    for plan in plans:
                        key = (capability, plan.tool, plan.scope)
                        if key in covered:
                            continue
                        receipts.append(
                            {
                                "id": "scr_" + uuid.uuid4().hex,
                                "content_hash": package.content_hash,
                                "capability": capability,
                                "tool": plan.tool,
                                "scope": plan.scope,
                                "actor": "local-user",
                                "created_at": datetime.now(timezone.utc).isoformat(),
                            }
                        )
                package_row.update(
                    {
                        "scope": plans[0].scope if plans else "project",
                        "targets": [item.tool for item in plans],
                        "plan_hashes": {item.tool: item.plan_hash for item in plans},
                        "degradations": [
                            value.as_dict() for plan in plans for value in plan.degradations
                        ],
                        "omissions": [value.as_dict() for plan in plans for value in plan.omissions],
                        "outputs": outputs,
                        "dirty_stale_outputs": dirty_stale,
                        "consent_receipts": receipts,
                        "consent_receipt_ids": [item["id"] for item in receipts],
                        # Ids this row was migrated from: cached versions imported
                        # under them are still this package's (rollback/doctor).
                        "previous_package_ids": previous_ids,
                        "revision": self._revision(existing) + 1,
                    }
                )
                packages[package.package_id] = package_row
                desired["packages"][package.package_id] = {
                    "package_id": package.package_id,
                    "name": package.name,
                    "content_hash": package.content_hash,
                    "source": package.source.as_dict(),
                    "scope": package_row["scope"],
                    "targets": package_row["targets"],
                    "required_capabilities": sorted(
                        {
                            requirement
                            for plan in plans
                            for requirement in plan.consent_requirements
                        }
                    ),
                }
                atomic_json(self.state_path, state)
                atomic_json(self.lock_path, desired)
                journal["phase"] = "committed"
                atomic_json(self.journal_path, journal)
            except Exception:
                self._recover_locked()
                raise
            self._recover_locked()
            return package_row

    def remove(
        self,
        package_id: str,
        *,
        expected_current_hash: str | None = None,
    ) -> dict[str, list[str]]:
        self._validate_state_root()
        with interprocess_lock(self.mutex_path):
            self._recover_locked()
            state = self.state()
            desired = self.desired()
            row = state["packages"].get(package_id)
            if not isinstance(row, dict):
                raise SkillError(f"Installed package not found: {package_id}")
            if (
                expected_current_hash is not None
                and row.get("content_hash") != expected_current_hash
            ):
                raise SkillError(f"Installed package changed concurrently: {package_id}")
            removable: list[Path] = []
            dirty: list[str] = []
            for output in self._unique_outputs(row):
                path = self._validate_output(Path(output["path"]))
                if not path.exists():
                    continue
                if _sha256_file(path) == output.get("output_hash"):
                    removable.append(path)
                else:
                    dirty.append(str(path))
            self.root.mkdir(parents=True, exist_ok=True)
            tx_dir = self.root / f".skills-txn-{uuid.uuid4().hex}"
            backup_dir = tx_dir / "backups"
            backup_dir.mkdir(parents=True)
            actions: list[dict[str, Any]] = []
            for index, path in enumerate(removable):
                backup = backup_dir / str(index)
                copy_backup(path, backup)
                actions.append({"destination": str(path), "backup": str(backup)})
            journal: dict[str, Any] = {
                "version": 1,
                "phase": "removing",
                "tx_dir": str(tx_dir),
                "actions": actions,
                "state_backup_existed": self.state_path.exists(),
                "lock_backup_existed": self.lock_path.exists(),
            }
            if self.state_path.exists():
                state_backup = tx_dir / "skills.state.json"
                copy_backup(self.state_path, state_backup)
                journal["state_backup"] = str(state_backup)
            if self.lock_path.exists():
                lock_backup = tx_dir / "skills.lock.json"
                copy_backup(self.lock_path, lock_backup)
                journal["lock_backup"] = str(lock_backup)
            atomic_json(self.journal_path, journal)
            try:
                for path in removable:
                    path.unlink()
                self._prune_empty_parents(removable)
                del state["packages"][package_id]
                desired["packages"].pop(package_id, None)
                atomic_json(self.state_path, state)
                atomic_json(self.lock_path, desired)
                journal["phase"] = "committed"
                atomic_json(self.journal_path, journal)
            except Exception:
                self._recover_locked()
                raise
            self._recover_locked()
            return {"removed": [str(path) for path in removable], "dirty": dirty}

    def _unique_outputs(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Managed outputs deduplicated by resolved path (legacy state may repeat)."""
        unique: dict[str, dict[str, Any]] = {}
        for output in row.get("outputs") or []:
            if not isinstance(output, dict) or not isinstance(output.get("path"), str):
                continue
            key = str(self._lexical(Path(output["path"])))
            unique.setdefault(key, output)
        return list(unique.values())

    def _write_output(self, source: Path, destination: Path, *, emission: Any = None) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                raw = source.read_bytes()
                handle.write(emission.transform(raw) if emission is not None else raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, destination)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _package_rows(
        self,
        package: PackageRecord,
        plans: list[EmitPlan],
        outputs: list[dict[str, Any]],
        dirty_stale: list[str],
        existing: dict[str, Any] | None,
        consented_capabilities: set[str] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        package_row = package.as_dict(include_path=False)
        capabilities = sorted(consented_capabilities or set())
        prior_receipts = (
            existing.get("consent_receipts", [])
            if isinstance(existing, dict) and existing.get("content_hash") == package.content_hash
            else []
        )
        receipts = [item for item in prior_receipts if isinstance(item, dict)]
        covered = {(item.get("capability"), item.get("tool"), item.get("scope")) for item in receipts}
        for capability in capabilities:
            for plan in plans:
                key = (capability, plan.tool, plan.scope)
                if key in covered:
                    continue
                receipts.append(
                    {
                        "id": "scr_" + uuid.uuid4().hex,
                        "content_hash": package.content_hash,
                        "capability": capability,
                        "tool": plan.tool,
                        "scope": plan.scope,
                        "actor": "local-user",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
        package_row.update(
            {
                "scope": plans[0].scope if plans else "project",
                "targets": [item.tool for item in plans],
                "plan_hashes": {item.tool: item.plan_hash for item in plans},
                "degradations": [value.as_dict() for plan in plans for value in plan.degradations],
                "omissions": [value.as_dict() for plan in plans for value in plan.omissions],
                "outputs": outputs,
                "dirty_stale_outputs": dirty_stale,
                "consent_receipts": receipts,
                "consent_receipt_ids": [item["id"] for item in receipts],
                "previous_package_ids": self._previous_package_ids(
                    existing, None, package.package_id
                ),
                "revision": self._revision(existing) + 1,
            }
        )
        lock_row = {
            "package_id": package.package_id,
            "name": package.name,
            "content_hash": package.content_hash,
            "source": package.source.as_dict(),
            "scope": package_row["scope"],
            "targets": package_row["targets"],
            "required_capabilities": sorted(
                {requirement for plan in plans for requirement in plan.consent_requirements}
            ),
        }
        return package_row, lock_row

    def reconcile(
        self,
        installs: list[tuple[PackageRecord, list[EmitPlan], set[str]]],
        removals: list[str],
    ) -> list[dict[str, Any]]:
        """Apply many removals and installs as ONE journaled transaction.

        Phase 1 (no writes): every planned output across the whole desired set
        is preflighted — against each other (two packages may not plan the
        same path), against unmanaged files, and against dirty managed files.
        Phase 2 performs every unlink and write under a single journal entry
        so a failure or crash anywhere rolls the entire reconcile back.
        """
        self._validate_state_root()
        with interprocess_lock(self.mutex_path):
            self._recover_locked()
            state = self.state()
            desired = self.desired()
            packages = state["packages"]
            removal_set = set(removals)
            for package_id in removals:
                if not isinstance(packages.get(package_id), dict):
                    raise SkillError(f"Installed package not found: {package_id}")
            # ---- phase 1: preflight ----
            # Ownership is computed against the POST-removal set: a package
            # that stops planning an output (dropped target) releases it in
            # phase 2 BEFORE any write, so another package may adopt the path
            # in the same reconcile.
            install_rows: list[tuple[PackageRecord, list[EmitPlan], set[str], dict[str, Any] | None, list[Any], list[dict[str, Any]]]] = []
            dropped_by_owner: dict[str, set[str]] = {}
            for package, plans, consented in installs:
                existing_raw = packages.get(package.package_id)
                existing = existing_raw if isinstance(existing_raw, dict) else None
                prior_outputs = existing.get("outputs", []) if existing else []
                emissions = [item for plan in plans for item in plan.emissions]
                new_paths = {item.destination for item in emissions}
                stale = [
                    item for item in prior_outputs
                    if isinstance(item, dict) and item.get("path") not in new_paths
                ]
                dropped_by_owner[package.package_id] = {
                    item["path"] for item in stale if isinstance(item.get("path"), str)
                }
                install_rows.append((package, plans, consented, existing, emissions, stale))
            owner_by_path = self._owner_by_path(packages, exclude=removal_set)
            for owner_id, dropped in dropped_by_owner.items():
                for raw in dropped:
                    owner = owner_by_path.get(raw)
                    if owner is not None and owner[0] == owner_id:
                        del owner_by_path[raw]
            removable: list[Path] = []
            removal_results: dict[str, dict[str, list[str]]] = {}
            freed: set[str] = set()
            kept_dirty: set[str] = set()
            for package_id in removals:
                row = packages[package_id]
                result: dict[str, list[str]] = {"removed": [], "dirty": []}
                for output in self._unique_outputs(row):
                    path = self._validate_output(Path(output["path"]))
                    if not path.exists():
                        continue
                    if _sha256_file(path) == output.get("output_hash"):
                        removable.append(path)
                        result["removed"].append(str(path))
                        freed.add(str(path))
                    else:
                        result["dirty"].append(str(path))
                        kept_dirty.add(str(path))
                removal_results[package_id] = result
            # Stale outputs of updated packages are released the same way:
            # unmodified ones are unlinked before any write, modified ones are
            # kept (and may not be overwritten by anyone).
            for _package, _plans, _consented, _existing, _emissions, stale in install_rows:
                for item in stale:
                    if not isinstance(item.get("path"), str):
                        continue
                    path = self._validate_output(Path(item["path"]))
                    if not path.exists():
                        continue
                    if _sha256_file(path) == item.get("output_hash"):
                        freed.add(str(path))
                    else:
                        kept_dirty.add(str(path))
            planned_by: dict[str, str] = {}
            prepared: list[dict[str, Any]] = []
            for package, plans, consented, existing, emissions, stale in install_rows:
                package_root = self._lexical(package.package_root)
                for emission in emissions:
                    destination = self._validate_output(Path(emission.destination))
                    key = str(destination)
                    other = planned_by.get(key)
                    if other is not None and other != package.package_id:
                        raise SkillError(
                            f"Output is planned by both {other} and {package.package_id}: {destination}"
                        )
                    planned_by[key] = package.package_id
                    source = self._lexical(Path(emission.source))
                    try:
                        source.relative_to(package_root)
                    except ValueError as exc:
                        raise SkillError(f"Planned source escapes package content: {source}") from exc
                    self._assert_no_symlink_components(package_root, source)
                    if _emission_hash(source, emission) != emission.output_hash:
                        raise SkillError(f"Planned source integrity mismatch: {source}")
                    if key in kept_dirty:
                        raise SkillError(f"Refusing to overwrite modified managed output: {destination}")
                    if key in freed:
                        continue  # unlinked by a removal before this write
                    owner = owner_by_path.get(emission.destination)
                    self._assert_not_owned_by_other(owner, package.package_id, destination)
                    if destination.exists():
                        if is_link_or_reparse(destination) or not destination.is_file():
                            raise SkillError(f"Refusing non-file output collision: {destination}")
                        if owner is None:
                            raise SkillError(f"Refusing unmanaged output collision: {destination}")
                        if _sha256_file(destination) != owner[1]:
                            raise SkillError(
                                f"Refusing to overwrite modified managed output: {destination}"
                            )
                prepared.append(
                    {
                        "package": package,
                        "plans": plans,
                        "consented": consented,
                        "existing": existing,
                        "emissions": emissions,
                        "stale": stale,
                    }
                )
            # ---- phase 2: one journal for everything ----
            self.root.mkdir(parents=True, exist_ok=True)
            tx_dir = self.root / f".skills-txn-{uuid.uuid4().hex}"
            backup_dir = tx_dir / "backups"
            backup_dir.mkdir(parents=True)
            touched: list[Path] = list(removable)
            for item in prepared:
                touched.extend(Path(emission.destination) for emission in item["emissions"])
                for stale_item in item["stale"]:
                    if isinstance(stale_item.get("path"), str):
                        stale_path = self._validate_output(Path(stale_item["path"]))
                        if not self._exceeds_hash_limit(stale_path):
                            touched.append(stale_path)
            actions: list[dict[str, Any]] = []
            for index, raw in enumerate(dict.fromkeys(str(item) for item in touched)):
                destination = self._validate_output(Path(raw))
                backup: Path | None = None
                if destination.is_file() and not is_link_or_reparse(destination):
                    backup = backup_dir / str(index)
                    copy_backup(destination, backup)
                actions.append({"destination": str(destination), "backup": str(backup) if backup else None})
            journal: dict[str, Any] = {
                "version": 1,
                "phase": "reconciling",
                "tx_dir": str(tx_dir),
                "actions": actions,
                "state_backup_existed": self.state_path.exists(),
                "lock_backup_existed": self.lock_path.exists(),
            }
            if self.state_path.exists():
                state_backup = tx_dir / "skills.state.json"
                copy_backup(self.state_path, state_backup)
                journal["state_backup"] = str(state_backup)
            if self.lock_path.exists():
                lock_backup = tx_dir / "skills.lock.json"
                copy_backup(self.lock_path, lock_backup)
                journal["lock_backup"] = str(lock_backup)
            atomic_json(self.journal_path, journal)
            results: list[dict[str, Any]] = []
            try:
                for package_id in removals:
                    row = packages[package_id]
                    for raw in removal_results[package_id]["removed"]:
                        Path(raw).unlink()
                    self._prune_empty_parents([Path(raw) for raw in removal_results[package_id]["removed"]])
                    del packages[package_id]
                    desired["packages"].pop(package_id, None)
                    results.append(
                        {
                            "action": "removed",
                            "package_id": package_id,
                            "name": row.get("name"),
                            **removal_results[package_id],
                        }
                    )
                # Every stale unlink precedes every write, whatever the order
                # of the desired set: a path one package drops and another
                # adopts is released first.
                for item in prepared:
                    dirty_stale: list[str] = []
                    for stale_item in item["stale"]:
                        path_raw = stale_item.get("path")
                        if not isinstance(path_raw, str):
                            continue
                        path = self._validate_output(Path(path_raw))
                        if not path.exists():
                            continue
                        if _sha256_file(path) == stale_item.get("output_hash"):
                            path.unlink()
                            self._prune_empty_parents([path])
                        else:
                            dirty_stale.append(path_raw)
                    item["dirty_stale"] = dirty_stale
                for item in prepared:
                    package = item["package"]
                    plans = item["plans"]
                    dirty_stale = item["dirty_stale"]
                    outputs: list[dict[str, Any]] = []
                    recorded: dict[str, dict[str, Any]] = {}
                    for plan in plans:
                        for emission in plan.emissions:
                            destination = self._validate_output(Path(emission.destination))
                            previous = recorded.get(str(destination))
                            if previous is not None:
                                previous["tools"].append(plan.tool)
                                continue
                            if emission.substitutions:
                                self._write_output(self._lexical(Path(emission.source)), destination, emission=emission)
                            else:
                                self._write_output(self._lexical(Path(emission.source)), destination)
                            record = {
                                "path": emission.destination,
                                "output_hash": emission.output_hash,
                                "tool": plan.tool,
                                "tools": [plan.tool],
                            }
                            recorded[str(destination)] = record
                            outputs.append(record)
                    package_row, lock_row = self._package_rows(
                        package, plans, outputs, dirty_stale, item["existing"], item["consented"]
                    )
                    packages[package.package_id] = package_row
                    desired["packages"][package.package_id] = lock_row
                    results.append(package_row)
                atomic_json(self.state_path, state)
                atomic_json(self.lock_path, desired)
                journal["phase"] = "committed"
                atomic_json(self.journal_path, journal)
            except Exception:
                self._recover_locked()
                raise
            self._recover_locked()
            return results

    def drift(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for package_id, row in self.state()["packages"].items():
            if not isinstance(row, dict):
                continue
            for output in self._unique_outputs(row):
                path = self._validate_output(Path(output["path"]))
                actual = _sha256_file(path)
                expected = str(output.get("output_hash", ""))
                if actual != expected:
                    result.append(
                        {
                            "package_id": package_id,
                            "path": output["path"],
                            "expected": expected,
                            "actual": actual or "missing",
                        }
                    )
        return result
