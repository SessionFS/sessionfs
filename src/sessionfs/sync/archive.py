"""Pack and unpack .sfs session directories to/from tar.gz archives."""

from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

# Safety ceiling for archives received from the network. The server already
# enforces a tier-aware per-member cap on upload, so callers with a resolved
# tier limit should pass `member_limit_bytes` explicitly. This constant is
# the fallback when the caller doesn't know — chosen to match the server
# abuse ceiling (`_validate_tar_gz`, 100 MB) so anything that passed upload
# validation also passes unpack validation.
DEFAULT_MEMBER_LIMIT_BYTES = int(
    os.environ.get("SFS_MAX_SYNC_MEMBER_BYTES_PAID", str(100 * 1024 * 1024))
)


def pack_session(session_dir: Path) -> bytes:
    """Pack an .sfs session directory into a tar.gz archive.

    Snapshots file contents into memory first to handle active sessions
    where the daemon may be writing concurrently.

    Args:
        session_dir: Path to the .sfs directory (e.g. ~/.sessionfs/sessions/{id}.sfs/)

    Returns:
        Bytes of the tar.gz archive.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for file_path in sorted(session_dir.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(session_dir).as_posix()
                # Snapshot file content to avoid "unexpected end of data"
                # when the daemon writes to the file during packing
                file_data = file_path.read_bytes()
                info = tarfile.TarInfo(name=arcname)
                info.size = len(file_data)
                info.mtime = int(file_path.stat().st_mtime)
                tar.addfile(info, io.BytesIO(file_data))
    return buf.getvalue()


def validate_tar_archive(
    archive_data: bytes,
    *,
    member_limit_bytes: int | None = None,
) -> None:
    """M7: Validate a tar.gz archive for safety before extraction.

    Rejects:
    - Path traversal (.. components)
    - Absolute paths
    - Symlinks and hardlinks
    - Members larger than `member_limit_bytes` (defaults to
      `DEFAULT_MEMBER_LIMIT_BYTES`, the same ceiling the server enforces
      at upload time — see `_validate_tar_gz` in routes/sessions.py).

    Callers that know the user's effective tier should pass a resolved
    `member_limit_bytes` (e.g. `SFS_MAX_SYNC_MEMBER_BYTES_FREE` for free
    tier, `SFS_MAX_SYNC_MEMBER_BYTES_PAID` for paid) so client-side
    rejection stays in sync with the tier-aware upload cap. When None,
    falls back to the paid-tier env override (or 100 MB) — the upload path
    has already enforced the appropriate per-tier limit, so this is a
    safety ceiling against malformed network responses, not the primary
    quota.

    Raises:
        ValueError: If the archive contains unsafe entries.
    """
    limit = (
        member_limit_bytes
        if member_limit_bytes is not None
        else DEFAULT_MEMBER_LIMIT_BYTES
    )
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if ".." in member.name:
                    raise ValueError(f"Path traversal in tar member: {member.name}")
                if member.name.startswith("/"):
                    raise ValueError(f"Absolute path in tar member: {member.name}")
                if member.issym() or member.islnk():
                    raise ValueError(f"Symlink in tar archive: {member.name}")
                if member.size > limit:
                    raise ValueError(
                        f"Member too large: {member.name} ({member.size} bytes)"
                    )
    except tarfile.TarError as e:
        raise ValueError(f"Invalid tar.gz archive: {e}") from e


def unpack_session(
    archive_data: bytes,
    target_dir: Path,
    *,
    member_limit_bytes: int | None = None,
) -> None:
    """Unpack a tar.gz archive into an .sfs session directory.

    Args:
        archive_data: Bytes of the tar.gz archive.
        target_dir: Directory to extract into (will be created).
        member_limit_bytes: Optional per-member size cap forwarded to
            `validate_tar_archive`. Pass a tier-resolved limit when the
            caller knows the user's effective tier; otherwise the default
            safety ceiling applies.

    Raises:
        ValueError: If the archive contains unsafe entries.
    """
    # M7: Full validation before extraction
    validate_tar_archive(archive_data, member_limit_bytes=member_limit_bytes)

    # Clear existing contents so stale files from a previous version
    # don't survive when the remote archive no longer includes them.
    if target_dir.exists():
        import shutil
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO(archive_data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        tar.extractall(target_dir, filter="data")
