"""Safe source acquisition for local paths and remote Git repositories."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import shutil
# Git runs with a fixed argv, shell disabled, and an absolute executable path.
import subprocess  # nosec B404
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from sessionfs.skills.models import SkillError, SourceIdentity
from sessionfs.skills.package import USTAR_NAME_LIMIT, USTAR_PREFIX_LIMIT, fits_ustar
from sessionfs.skills.path_safety import CasefoldCollisionTracker, is_link_or_reparse

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Blobs above this size are never fetched: the partial clone filter keeps them
# out of the fetch, and the tree walk rejects them before any lazy fetch.
PER_FILE_CAP_BYTES = 16 * 1024 * 1024
MAX_GIT_FILES = 5_000
MAX_GIT_TOTAL_BYTES = 128 * 1024 * 1024
# One NUL-terminated `ls-tree -lrz` record (mode, type, oid, size, TAB, path),
# NOT counting the selected subdir prefix, which is added to the limit per
# import. Package paths must fit ustar (~256 bytes) and are checked per entry
# before being retained; a longer record is refused whether or not its NUL has
# arrived yet, so acceptance never depends on pipe read boundaries.
MAX_GIT_TREE_RECORD_BYTES = 4 * 1024
_GIT_TIMEOUT_SECONDS = 60
_LOCAL_SOURCE_TYPES = {"local", "local-project"}


@dataclass(frozen=True)
class MaterializedSource:
    path: Path
    identity: SourceIdentity


def normalize_git_url(value: str) -> str:
    """Return a stable, credential-free HTTPS/SSH Git locator."""
    if (
        value.startswith("-")
        or value.startswith("ext::")
        or value.startswith("file:")
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        raise SkillError("Unsafe Git source URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SkillError("Invalid Git source URL") from exc
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
        raise SkillError("Remote Git sources must use an explicit https:// or ssh:// URL")
    if parsed.password or parsed.query or parsed.fragment:
        raise SkillError("Git source URLs cannot contain credentials, query parameters, or fragments")
    # A bare username is the SSH transport identity (ssh://git@host/...), never
    # a secret; HTTPS userinfo is a credential channel and stays rejected.
    if parsed.username and parsed.scheme != "ssh":
        raise SkillError("Git source URLs cannot contain credentials, query parameters, or fragments")
    username = parsed.username or ""
    if username and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username):
        raise SkillError("Git SSH usernames must be simple identifiers")
    host = parsed.hostname.lower()
    if not host.isascii():
        raise SkillError("Git source hostnames must be ASCII")
    if ":" in host:
        host = f"[{host}]"
    port_suffix = f":{port}" if port else ""
    if username:
        host = f"{username}@{host}"
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path or path == "/":
        raise SkillError("Git source URL requires a repository path")
    return urlunsplit((parsed.scheme.lower(), host + port_suffix, path, "", ""))


def _safe_subdir(value: str) -> str:
    clean = value.strip()
    if (
        clean.startswith(("/", "\\"))
        or "\\" in clean
        or any(
            char in '<>:"|?*' or ord(char) < 32 or ord(char) == 127
            for char in clean
        )
    ):
        raise SkillError(f"Unsafe source subdirectory: {value}")
    clean = clean.rstrip("/")
    if not clean:
        return ""
    path = PurePosixPath(clean)
    if path.is_absolute() or ".." in path.parts or len(path.parts) > 16:
        raise SkillError(f"Unsafe source subdirectory: {value}")
    return path.as_posix()


MIN_GIT_VERSION = (2, 44)  # GIT_NO_LAZY_FETCH support
_GIT_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)")
_MISSING_OBJECT_MARKERS = (
    "could not read object",
    "unable to read",
    "not our ref",
    "missing object",
    "bad object",
    "could not fetch",
    "promisor",
    "lazy",
    "does not exist",
    "not a valid object",
)


# Hard cap on bytes a fetch may put on disk: a server that ignores the
# partial-clone filter sends the full pack, so the CLIENT must bound it.
MAX_FETCH_BYTES = 256 * 1024 * 1024
_FETCH_WATCHDOG_INTERVAL = 0.25
_FILTER_UNSUPPORTED_MARKERS = (
    "filtering not recognized by server",
    "does not support filter",
    "filter not supported",
)


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


# POSIX: run git in its own session/process group so a kill reaches every
# descendant (git-remote-https, ssh, ...). A surviving descendant that inherited
# stdout would otherwise keep the pipe open and block the reader indefinitely.
_NEW_PROCESS_GROUP = hasattr(os, "killpg")


def _kill_process_tree(process: subprocess.Popen) -> None:
    """Kill git and all of its descendants (POSIX process group), falling back
    to killing just the child where process groups do not exist (Windows)."""
    killpg = getattr(os, "killpg", None)
    if killpg is not None and _NEW_PROCESS_GROUP:
        try:
            killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    with contextlib.suppress(OSError):
        process.kill()


def _run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    watch_dir: Path,
    disk_cap: int,
    env: dict[str, str] | None,
    timeout: float,
) -> tuple[int, bytes, bytes]:
    """Run a subprocess while a watchdog polls `watch_dir`; kill it and raise
    once the directory exceeds `disk_cap` bytes. Bounds disk regardless of how
    the remote server behaves."""
    import threading

    process = subprocess.Popen(  # nosec B603
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=_NEW_PROCESS_GROUP,
    )
    tripped = threading.Event()
    stop = threading.Event()

    def _watch() -> None:
        while not stop.wait(_FETCH_WATCHDOG_INTERVAL):
            if _dir_size(watch_dir) > disk_cap:
                tripped.set()
                _kill_process_tree(process)
                return

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_tree(process)
        process.communicate()
        raise SkillError("Git source operation timed out") from exc
    finally:
        stop.set()
        watcher.join(timeout=2)
    if tripped.is_set():
        raise SkillError(
            f"Git fetch exceeded the {MAX_FETCH_BYTES}-byte download limit and was aborted"
        )
    return process.returncode, stdout, stderr


def _run_streaming(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    on_record: Callable[[bytes], None],
    timeout: float,
    max_record_bytes: int = MAX_GIT_TREE_RECORD_BYTES,
) -> tuple[int, bytes]:
    """Run a subprocess and hand each NUL-terminated stdout record to
    `on_record` as it arrives, never buffering the whole output. If
    `on_record` raises (a limit was exceeded) the process is killed at once
    and the exception propagates. stderr goes to a temp file so a chatty
    process can never deadlock on a full pipe."""
    import threading

    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(  # nosec B603
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            env=env,
            start_new_session=_NEW_PROCESS_GROUP,
        )
        timed_out = threading.Event()

        def _expire() -> None:
            timed_out.set()
            _kill_process_tree(process)

        timer = threading.Timer(timeout, _expire)
        timer.daemon = True
        timer.start()
        stdout = process.stdout
        assert stdout is not None  # noqa: S101 - stdout=PIPE above
        try:
            pending = b""
            while True:
                chunk = stdout.read1(65536)
                if not chunk:
                    break
                pending += chunk
                *records, pending = pending.split(b"\0")
                for record in records:
                    if len(record) > max_record_bytes:
                        raise SkillError("Git tree contains an invalid entry")
                    on_record(record)
                if len(pending) > max_record_bytes:
                    raise SkillError("Git tree contains an invalid entry")
            if pending:
                on_record(pending)
            returncode = process.wait()
        except BaseException:
            _kill_process_tree(process)
            process.wait()
            raise
        finally:
            timer.cancel()
            stdout.close()
        if timed_out.is_set():
            raise SkillError("Git source operation timed out")
        stderr_file.seek(0)
        return returncode, stderr_file.read(64 * 1024)


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    input_data: bytes | None = None,
    env: dict[str, str] | None = None,
    watch_dir: Path | None = None,
    disk_cap: int | None = None,
    stderr_sink: list[str] | None = None,
    on_record: Callable[[bytes], None] | None = None,
    max_record_bytes: int = MAX_GIT_TREE_RECORD_BYTES,
) -> bytes:
    """Run git hardened (no hooks, fixed argv, no shell, 60 s timeout).

    With `on_record`, stdout is streamed record by record (NUL-separated) into
    the callback instead of being returned, so a hostile tree listing is
    refused - and git killed - as soon as a limit trips; returns b""."""
    git_path = shutil.which("git")
    if git_path is None:
        raise SkillError("git is required to import remote skills")
    run_env = None
    if env:
        run_env = {**os.environ, **env}
    argv = [git_path, "-c", "core.hooksPath=/dev/null", *args]
    try:
        if on_record is not None:
            returncode, stderr = _run_streaming(
                argv,
                cwd=cwd,
                env=run_env,
                on_record=on_record,
                timeout=_GIT_TIMEOUT_SECONDS,
                max_record_bytes=max_record_bytes,
            )
            stdout = b""
        elif watch_dir is not None and disk_cap is not None:
            returncode, stdout, stderr = _run_bounded(
                argv,
                cwd=cwd,
                watch_dir=watch_dir,
                disk_cap=disk_cap,
                env=run_env,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        else:
            # Every dynamic value remains one argv element; no shell is involved.
            result = subprocess.run(  # nosec B603
                argv,
                cwd=cwd,
                input=input_data,
                capture_output=True,
                check=False,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=run_env,
            )
            returncode, stdout, stderr = result.returncode, result.stdout, result.stderr
    except FileNotFoundError as exc:
        raise SkillError("git is required to import remote skills") from exc
    except subprocess.TimeoutExpired as exc:
        raise SkillError("Git source operation timed out") from exc
    error_text = stderr.decode("utf-8", errors="replace").strip()
    if stderr_sink is not None:
        stderr_sink.append(error_text)
    if returncode != 0:
        raise SkillError(f"Git source operation failed: {error_text[:500]}")
    return stdout


def _pack_bytes(repo: Path) -> int:
    pack_dir = repo / "objects" / "pack"
    if not pack_dir.is_dir():
        return 0
    return sum(item.stat().st_size for item in pack_dir.iterdir() if item.is_file())


def _require_git_version(cwd: Path) -> None:
    """Refuse to import remote sources on git < 2.44: without GIT_NO_LAZY_FETCH
    a hostile repo can force `ls-tree -l` to download every filtered blob."""
    raw = _run_git(["--version"], cwd=cwd).decode("utf-8", errors="replace")
    match = _GIT_VERSION_RE.search(raw)
    if match is None:
        raise SkillError("Could not determine the git version")
    version = (int(match.group(1)), int(match.group(2)))
    if version < MIN_GIT_VERSION:
        raise SkillError(
            f"git >= {MIN_GIT_VERSION[0]}.{MIN_GIT_VERSION[1]} is required to import remote "
            f"skills safely (found {version[0]}.{version[1]})"
        )


@contextlib.contextmanager
def materialize_source(
    source: str | Path,
    *,
    pin: str | None = None,
    subdir: str = "",
    source_type: str | None = None,
    registry_scope: str | None = None,
    registry_id: str | None = None,
    kind: str | None = None,
    paths: tuple[str, ...] | None = None,
    registry_alias: str | None = None,
    listing_name: str | None = None,
):
    """Yield a source directory without executing repository-controlled code.

    ``kind`` is "local" or "git". When omitted it is inferred from
    ``source_type``: anything other than local/local-project is a remote Git
    locator and must already be in normalized form, so a lockfile or registry
    row can never point a "marketplace"/"git" source at a local directory.
    ``paths`` restricts a Git materialization to the given tree pathspecs.
    """
    if kind is None:
        if source_type is None:
            kind = "auto"
        elif source_type in _LOCAL_SOURCE_TYPES:
            kind = "local"
        else:
            kind = "git"
    if kind not in {"auto", "local", "git"}:
        raise SkillError("Source kind must be 'local' or 'git'")
    if kind == "git" and source_type is not None and source_type not in _LOCAL_SOURCE_TYPES:
        try:
            normalized = normalize_git_url(str(source))
        except SkillError as exc:
            raise SkillError(
                f"{source_type} sources must use a normalized Git locator, not a local path"
            ) from exc
        if normalized != str(source):
            raise SkillError(
                f"{source_type} sources must use a normalized Git locator, not a local path"
            )
    path = Path(source).expanduser() if not isinstance(source, Path) else source.expanduser()
    clean_subdir = _safe_subdir(subdir)
    if kind == "local" and not path.exists():
        raise SkillError(f"Source directory does not exist: {path}")
    if kind != "git" and path.exists():
        root = path.resolve()
        unresolved = root / clean_subdir
        current = root
        for part in PurePosixPath(clean_subdir).parts:
            current /= part
            if is_link_or_reparse(current):
                raise SkillError(f"Source subdirectory traverses a symlink: {clean_subdir}")
        selected = unresolved.resolve()
        try:
            selected.relative_to(root)
        except ValueError as exc:
            raise SkillError("Source subdirectory escapes the local source root") from exc
        if not selected.is_dir():
            raise SkillError(f"Source directory does not exist: {selected}")
        identity = SourceIdentity(
            source_type=source_type or "local",
            locator=str(root),
            subdir=clean_subdir,
            registry_scope=registry_scope,
            registry_id=registry_id,
            registry_alias=registry_alias,
            listing_name=listing_name,
        )
        yield MaterializedSource(selected, identity)
        return

    locator = normalize_git_url(str(source))
    ref = pin or "HEAD"
    with tempfile.TemporaryDirectory(prefix="sfs-skill-git-") as tmp:
        repo = Path(tmp) / "repo.git"
        output = Path(tmp) / "tree"
        repo.mkdir()
        _require_git_version(repo)
        _run_git(["init", "--bare", "--quiet"], cwd=repo)
        fetch_stderr: list[str] = []
        _run_git(
            [
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "protocol.file.allow=never",
                "fetch",
                "--no-tags",
                "--depth=1",
                f"--filter=blob:limit={PER_FILE_CAP_BYTES}",
                "--",
                locator,
                ref,
            ],
            cwd=repo,
            watch_dir=repo,
            disk_cap=MAX_FETCH_BYTES,
            stderr_sink=fetch_stderr,
        )
        stderr_text = "\n".join(fetch_stderr).lower()
        if any(marker in stderr_text for marker in _FILTER_UNSUPPORTED_MARKERS):
            raise SkillError(
                "The Git server does not support partial-clone filters; remote skill "
                "sources must be hosted on a server that honors --filter"
            )
        if _pack_bytes(repo) > MAX_FETCH_BYTES:
            raise SkillError(
                f"Git fetch exceeded the {MAX_FETCH_BYTES}-byte download limit"
            )
        commit = _run_git(["rev-parse", "FETCH_HEAD^{commit}"], cwd=repo).decode().strip()
        if not _SHA_RE.fullmatch(commit):
            raise SkillError("Git did not resolve the source to a full commit SHA")
        if pin and _SHA_RE.fullmatch(pin) and commit != pin:
            # A sha pin is exact: the server must have served that very commit.
            raise SkillError(
                f"Git source resolved to {commit} but the pinned commit is {pin}"
            )
        tree_args = ["ls-tree", "-lrz", "-r", "--full-tree", commit]
        pathspecs: list[str] = []
        if paths:
            for item in paths:
                clean_item = _safe_subdir(item)
                if not clean_item:
                    raise SkillError("Git tree path filters must be relative paths")
                pathspecs.append(
                    f"{clean_subdir}/{clean_item}" if clean_subdir else clean_item
                )
        elif clean_subdir:
            pathspecs.append(clean_subdir)
        if pathspecs:
            tree_args.extend(["--", *pathspecs])
        output.mkdir()
        count = 0
        total = 0
        records = 0
        collisions = CasefoldCollisionTracker()
        # Pass 1 (no fetch): parse and validate EVERY entry - mode, path
        # safety, case collisions, per-file cap, aggregate limits - so a
        # rejected tree never causes a single blob to be cat-file'd. Entries are
        # validated AS GIT STREAMS THEM: a tree bomb (millions of entries, or
        # absurdly deep paths) is refused and git killed at the first entry
        # past a limit, never buffered in full.
        planned: list[tuple[PurePosixPath, str, int, str]] = []

        def _plan_entry(entry: bytes) -> None:
            nonlocal count, total, records
            if not entry:
                return
            records += 1
            if records > MAX_GIT_FILES:
                raise SkillError("Git package exceeds import limits")
            try:
                header, raw_path = entry.split(b"\t", 1)
                mode, kind, object_id, raw_size = header.decode("ascii").split()
                size = int(raw_size)
                repository_path = raw_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise SkillError("Git tree contains an invalid entry") from exc
            if kind != "blob" or mode not in {"100644", "100755"} or size < 0:
                raise SkillError(f"Git source contains unsupported entry: {repository_path}")
            relative = repository_path
            if clean_subdir:
                prefix = clean_subdir + "/"
                if not relative.startswith(prefix):
                    return
                relative = relative[len(prefix):]
            pure = PurePosixPath(relative)
            if (
                not relative
                or pure.is_absolute()
                or ".." in pure.parts
                or "\\" in relative
                or "\x00" in relative
                or any(
                    char in '<>:"|?*' or ord(char) < 32 or ord(char) == 127
                    for char in relative
                )
                or len(pure.parts) > 16
            ):
                raise SkillError(f"Unsafe Git tree path: {repository_path}")
            # Refuse over-long paths HERE, before they are retained: import would
            # reject them later anyway, but 5,000 multi-KiB paths (plus their
            # collision-tracker prefixes) are enough to exhaust memory first.
            if not fits_ustar(relative):
                raise SkillError(
                    f"Package path exceeds the ustar limit (name <= {USTAR_NAME_LIMIT} bytes with a "
                    f"prefix <= {USTAR_PREFIX_LIMIT} bytes): {relative[:300]}"
                )
            try:
                # A.md vs a.md (or Docs/ vs docs/, a vs a/x): silently overwrites on
                # case-insensitive filesystems and makes the hash host-dependent.
                collisions.add(pure.as_posix())
            except ValueError as exc:
                raise SkillError(f"Git tree {exc}") from exc
            if size > PER_FILE_CAP_BYTES:
                # Rejected BEFORE cat-file: the blob was filtered out of the
                # fetch and must never be lazily fetched from the remote.
                raise SkillError(
                    f"Git source file exceeds the {PER_FILE_CAP_BYTES}-byte per-file limit: "
                    f"{repository_path}"
                )
            count += 1
            total += size
            if count > MAX_GIT_FILES or total > MAX_GIT_TOTAL_BYTES:
                raise SkillError("Git package exceeds import limits")
            planned.append((pure, object_id, size, repository_path))

        tree_rejections: list[SkillError] = []

        def _on_tree_record(entry: bytes) -> None:
            try:
                _plan_entry(entry)
            except SkillError as exc:
                # Remember OUR rejection so it is never re-labelled below as a
                # git missing-object failure (a path may contain marker words).
                tree_rejections.append(exc)
                raise

        try:
            # `ls-tree -l` prints sizes and would lazily fetch every blob the
            # blob:limit filter left out; with lazy fetching disabled git fails
            # instead, which can only mean a blob above the per-file cap.
            _run_git(
                tree_args,
                cwd=repo,
                env={"GIT_NO_LAZY_FETCH": "1"},
                on_record=_on_tree_record,
                # The fixed record budget applies to the part after the
                # selected subdir, so a deep but legitimate subdir still fits.
                max_record_bytes=MAX_GIT_TREE_RECORD_BYTES + len(clean_subdir.encode("utf-8")) + 1,
            )
        except SkillError as exc:
            if exc in tree_rejections:
                raise
            message = str(exc).lower()
            if any(marker in message for marker in _MISSING_OBJECT_MARKERS):
                raise SkillError(
                    f"Git source contains a file above the {PER_FILE_CAP_BYTES}-byte per-file "
                    "limit (its blob was withheld by the partial clone filter)"
                ) from exc
            raise
        # Pass 2: fetch only after the whole tree passed validation.
        for pure, object_id, size, repository_path in planned:
            data = _run_git(["cat-file", "blob", object_id], cwd=repo)
            if len(data) != size:
                raise SkillError(f"Git blob size changed while reading: {repository_path}")
            destination = output / pure.as_posix()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        if not count:
            raise SkillError("Git source did not contain files at the selected subdirectory")
        identity = SourceIdentity(
            source_type=source_type or "git",
            locator=locator,
            commit=commit,
            subdir=clean_subdir,
            registry_scope=registry_scope,
            registry_id=registry_id,
            registry_alias=registry_alias,
            listing_name=listing_name,
        )
        yield MaterializedSource(output, identity)
