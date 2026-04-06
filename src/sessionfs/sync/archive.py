"""Pack and unpack .sfs session directories to/from tar.gz archives."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path


def pack_session(session_dir: Path) -> bytes:
    """Pack an .sfs session directory into a tar.gz archive.

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
                tar.add(str(file_path), arcname=arcname)
    return buf.getvalue()


def validate_tar_archive(archive_data: bytes) -> None:
    """M7: Validate a tar.gz archive for safety before extraction.

    Rejects:
    - Path traversal (.. components)
    - Absolute paths
    - Symlinks and hardlinks
    - Excessively large members (>50 MB per file)

    Raises:
        ValueError: If the archive contains unsafe entries.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if ".." in member.name:
                    raise ValueError(f"Path traversal in tar member: {member.name}")
                if member.name.startswith("/"):
                    raise ValueError(f"Absolute path in tar member: {member.name}")
                if member.issym() or member.islnk():
                    raise ValueError(f"Symlink in tar archive: {member.name}")
                if member.size > 50 * 1024 * 1024:
                    raise ValueError(
                        f"Member too large: {member.name} ({member.size} bytes)"
                    )
    except tarfile.TarError as e:
        raise ValueError(f"Invalid tar.gz archive: {e}") from e


def unpack_session(archive_data: bytes, target_dir: Path) -> None:
    """Unpack a tar.gz archive into an .sfs session directory.

    Args:
        archive_data: Bytes of the tar.gz archive.
        target_dir: Directory to extract into (will be created).

    Raises:
        ValueError: If the archive contains unsafe entries.
    """
    # M7: Full validation before extraction
    validate_tar_archive(archive_data)

    # Clear existing contents so stale files from a previous version
    # don't survive when the remote archive no longer includes them.
    if target_dir.exists():
        import shutil
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO(archive_data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        tar.extractall(target_dir, filter="data")
