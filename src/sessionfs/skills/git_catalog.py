"""Deterministic, server-generated git repositories served over "dumb HTTP".

Native tool catalogs (marketplace v2 §7.2) are git repositories. SessionFS
never runs a git server process: the repository is built here, in pure
Python, as one undeltified pack plus the small files the dumb-HTTP protocol
reads (``info/refs``, ``HEAD``, ``objects/info/packs``). Every file is
immutable, so it can be stored in blob storage and served as-is.

Determinism: the commit SHA depends only on the file tree, the fixed
author/committer identity, the timestamp the caller passes (the newest
``published_at`` of the catalog's inputs) and the message. Equal inputs give
an equal SHA (golden-tested). Pack data is zlib-compressed at a fixed level;
object ids never depend on compression.

Hostile-content posture (§7.7 "git client exploits via crafted repo
content"): every path is validated here, whatever produced it. Only regular
files (mode 100644) and directories are emitted: no symlinks, no
executables, no submodules (gitlinks), no ``.git`` / ``.gitmodules`` /
``.gitattributes`` component in any spelling a case-insensitive or
NTFS/HFS-normalizing checkout would treat as one.
"""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
import zlib
from dataclasses import dataclass, field

CATALOG_AUTHOR_NAME = "SessionFS Catalog"
CATALOG_AUTHOR_EMAIL = "catalog@sessionfs.dev"
DEFAULT_BRANCH = "main"
MAX_PATH_BYTES = 1024
MAX_FILES = 20_000
_ZLIB_LEVEL = 6

# Components a checkout would treat as git metadata. Compared after NFC +
# casefold, with trailing dots/spaces stripped (NTFS) and ignorable code
# points removed (HFS+ ignores some zero-width characters).
_FORBIDDEN_COMPONENTS = frozenset({".git", ".gitmodules", ".gitattributes"})
_NTFS_SHORT_NAME = re.compile(r"^git~\d+$")
_HFS_IGNORABLE = dict.fromkeys(
    [0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
     0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F, 0xFEFF]
)


class GitCatalogError(ValueError):
    """A tree that must never be written into a served repository."""


def _normalized_component(component: str) -> str:
    value = unicodedata.normalize("NFC", component).translate(_HFS_IGNORABLE).casefold()
    return value.rstrip(". ")


def validate_repo_path(path: str) -> str:
    """Return ``path`` unchanged when it is safe as a served repo path."""
    if not isinstance(path, str) or not path:
        raise GitCatalogError("empty repository path")
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        raise GitCatalogError(f"repository path too long: {path[:80]}")
    if path.startswith("/") or path.endswith("/") or "\\" in path or "//" in path:
        raise GitCatalogError(f"invalid repository path: {path}")
    for component in path.split("/"):
        if component in {"", ".", ".."}:
            raise GitCatalogError(f"invalid repository path: {path}")
        if any(ord(char) < 32 or ord(char) == 127 for char in component):
            raise GitCatalogError(f"control character in repository path: {path!r}")
        normalized = _normalized_component(component)
        if normalized in _FORBIDDEN_COMPONENTS or _NTFS_SHORT_NAME.match(normalized):
            raise GitCatalogError(f"git metadata path is not allowed: {path}")
    return path


def _object(kind: str, payload: bytes) -> tuple[str, bytes]:
    raw = kind.encode("ascii") + b" " + str(len(payload)).encode("ascii") + b"\0" + payload
    return hashlib.sha1(raw).hexdigest(), raw  # noqa: S324 - git object ids are SHA-1


@dataclass
class GitRepository:
    """One generated repository: its commit and every served file."""

    commit_sha: str
    tree_sha: str
    # object id -> raw (uncompressed) object bytes, header included.
    raw_objects: dict[str, bytes] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)

    def pack(self) -> tuple[str, bytes, bytes]:
        """(pack name, .pack bytes, .idx bytes) holding every object, undeltified.

        A dumb-HTTP client that finds no loose object reads
        ``objects/info/packs`` and fetches the pack, so a clone is a handful
        of requests however many objects the catalog has."""
        return build_pack(self.raw_objects)

    def served_files(self) -> dict[str, bytes]:
        """Path (relative to the ``.git`` URL) -> bytes, dumb-HTTP layout."""
        name, pack, index = self.pack()
        return {
            "HEAD": f"ref: refs/heads/{DEFAULT_BRANCH}\n".encode("ascii"),
            "info/refs": self.info_refs(),
            "objects/info/packs": packs_listing(name),
            f"objects/pack/{name}.pack": pack,
            f"objects/pack/{name}.idx": index,
        }

    def info_refs(self) -> bytes:
        return info_refs(self.refs)


def info_refs(refs: dict[str, str]) -> bytes:
    return "".join(f"{sha}\t{ref}\n" for ref, sha in sorted(refs.items())).encode("ascii")


def packs_listing(pack_name: str) -> bytes:
    return f"P {pack_name}.pack\n\n".encode("ascii")


_PACK_TYPES = {b"commit": 1, b"tree": 2, b"blob": 3, b"tag": 4}


def _pack_entry_header(kind: int, size: int) -> bytes:
    first = (kind << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def build_pack(raw_objects: dict[str, bytes]) -> tuple[str, bytes, bytes]:
    """Version-2 pack + version-2 index of undeltified objects."""
    ids = sorted(raw_objects)
    body = bytearray(b"PACK" + struct.pack(">II", 2, len(ids)))
    offsets: dict[str, int] = {}
    crcs: dict[str, int] = {}
    for object_id in ids:
        header, _, payload = raw_objects[object_id].partition(b"\0")
        kind = _PACK_TYPES[header.split(b" ", 1)[0]]
        entry = _pack_entry_header(kind, len(payload)) + zlib.compress(payload, _ZLIB_LEVEL)
        offsets[object_id] = len(body)
        crcs[object_id] = zlib.crc32(entry) & 0xFFFFFFFF
        body += entry
    pack_checksum = hashlib.sha1(bytes(body)).digest()  # noqa: S324 - pack trailer is SHA-1
    body += pack_checksum
    fanout = [0] * 256
    for object_id in ids:
        fanout[int(object_id[:2], 16)] += 1
    index = bytearray(b"\xfftOc" + struct.pack(">I", 2))
    running = 0
    for count in fanout:
        running += count
        index += struct.pack(">I", running)
    for object_id in ids:
        index += bytes.fromhex(object_id)
    for object_id in ids:
        index += struct.pack(">I", crcs[object_id])
    for object_id in ids:
        if offsets[object_id] >= 0x80000000:
            raise GitCatalogError("catalog pack exceeds 2 GiB")
        index += struct.pack(">I", offsets[object_id])
    index += pack_checksum
    index += hashlib.sha1(bytes(index)).digest()  # noqa: S324 - index trailer is SHA-1
    return f"pack-{pack_checksum.hex()}", bytes(body), bytes(index)


def check_tree(files: dict[str, bytes]) -> None:
    """Raise ``GitCatalogError`` unless ``files`` can be written as one git
    tree: every path valid and no path both a file and a directory. Callers
    preflight one plugin at a time so a single bad package is excluded
    instead of failing the whole catalog."""
    _tree_entries(files)


def _tree_entries(files: dict[str, bytes]) -> dict:
    root: dict = {}
    for path in sorted(files):
        validate_repo_path(path)
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise GitCatalogError(f"path is both a file and a directory: {path}")
            node = child
        if parts[-1] in node:
            raise GitCatalogError(f"path is both a file and a directory: {path}")
        node[parts[-1]] = files[path]
    return root


def _write_tree(node: dict, objects: dict[str, bytes]) -> str:
    entries: list[tuple[bytes, bytes, str]] = []
    for name, value in node.items():
        if isinstance(value, dict):
            sha = _write_tree(value, objects)
            # git sorts a directory as if its name ended with "/".
            entries.append((name.encode("utf-8") + b"/", b"40000 " + name.encode("utf-8"), sha))
        else:
            sha, raw = _object("blob", value)
            objects[sha] = raw
            entries.append((name.encode("utf-8"), b"100644 " + name.encode("utf-8"), sha))
    entries.sort(key=lambda item: item[0])
    payload = b"".join(head + b"\0" + bytes.fromhex(sha) for _, head, sha in entries)
    sha, raw = _object("tree", payload)
    objects[sha] = raw
    return sha


def build_repository(
    files: dict[str, bytes],
    *,
    timestamp: int,
    message: str,
    tags: dict[str, None] | None = None,
) -> GitRepository:
    """Build a single-commit repository holding exactly ``files``.

    ``tags`` names lightweight tags (``refs/tags/<name>``) pointing at the
    commit, for tools that install a ``--ref``.
    """
    if not files:
        raise GitCatalogError("a catalog repository needs at least one file")
    if len(files) > MAX_FILES:
        raise GitCatalogError("catalog repository has too many files")
    if timestamp < 0:
        raise GitCatalogError("negative commit timestamp")
    objects: dict[str, bytes] = {}
    tree_sha = _write_tree(_tree_entries(files), objects)
    identity = f"{CATALOG_AUTHOR_NAME} <{CATALOG_AUTHOR_EMAIL}> {int(timestamp)} +0000"
    clean_message = message.replace("\r", "").strip() + "\n"
    commit_payload = (
        f"tree {tree_sha}\nauthor {identity}\ncommitter {identity}\n\n{clean_message}"
    ).encode("utf-8")
    commit_sha, raw = _object("commit", commit_payload)
    objects[commit_sha] = raw
    refs = {f"refs/heads/{DEFAULT_BRANCH}": commit_sha}
    for tag in sorted(tags or {}):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}", tag) or ".." in tag or tag.endswith(".lock"):
            raise GitCatalogError(f"invalid tag name: {tag}")
        refs[f"refs/tags/{tag}"] = commit_sha
    return GitRepository(commit_sha=commit_sha, tree_sha=tree_sha, raw_objects=objects, refs=refs)
