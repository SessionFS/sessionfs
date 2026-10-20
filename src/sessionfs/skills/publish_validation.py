"""Hosted-marketplace publish validation, shared by the API server and the CLI.

This module is THE publish gate for Agent Skill / Agent Plugin packages. The
server publish routes (``server/routes/marketplace.py``, via the
``server/services/skill_packages.py`` re-export) and the local
``sfs skill validate`` / ``sfs skill publish`` commands both call
:func:`validate_hosted_package` on the exact same archive bytes, so a package
the CLI accepts is accepted by the server and vice versa.

It must stay importable without the server extras (FastAPI, SQLAlchemy): only
``sessionfs.skills`` and ``sessionfs.security`` are imported here.

Checks that need server state stay server-side and are NOT run here: plan /
feature gates, storage quotas, version caps, publish concurrency, and an
organization's own DLP policy (custom patterns and allowlist).

Every failure is a :class:`HostedSkillPackageError`. Its ``str()`` is the
exact message the server returns in the 422 body; ``reason`` is a stable
machine-readable sub-code, and ``path`` / ``line`` / ``hint`` / ``findings``
carry local-only detail for the CLI (never echoed by the server).
"""

from __future__ import annotations

import errno
import gzip
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sessionfs.security.secrets import scan_dlp as scan_dlp_with_policy
from sessionfs.security.secrets import scan_dlp_builtin
from sessionfs.skills.disclosure import compute_manifest
from sessionfs.skills.instruction_scan import ScanResult, scan_tree
from sessionfs.skills.models import SUPPORTED_TOOLS, SkillError, SourceIdentity
from sessionfs.skills.path_safety import CasefoldCollisionTracker
from sessionfs.skills.package import (
    AGENT_PLUGIN_SCHEMA,
    BINARY_MAGIC,
    fits_ustar,
    import_package,
    looks_binary,
    scan_mcp_tree,
)

MAX_PACKAGE_FILES = 5_000
MAX_ARCHIVE_MEMBERS = 10_000
# Hosted (server-side) expanded-package ceiling. Deliberately below the 128 MiB
# client import cap (skills/package.py MAX_TOTAL_BYTES): every hosted publish is
# decompressed, DLP-scanned and repacked on an API worker, so the server budget
# is sized to the largest official-marketplace plugin (salesforce-development,
# ~38 MiB unpacked) plus headroom, not to what a local import can tolerate.
MAX_PACKAGE_BYTES = 48 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
# Hard ceiling on DECOMPRESSED bytes pulled from an upload. Reached before any
# member cap can run when a GNU longname/PAX header claims a huge size.
MAX_DECOMPRESSED_BYTES = MAX_PACKAGE_BYTES + MAX_ARCHIVE_MEMBERS * 1024
# Compressed upload ceiling. A legitimate gzip stream is never meaningfully
# larger than the tar stream it inflates to, so the decompressed budget bounds
# it too (gzip's worst-case expansion on incompressible data is ~0.03%).
MAX_UPLOAD_BYTES = MAX_DECOMPRESSED_BYTES
MAX_EXTENDED_HEADER_BYTES = 4096
MAX_DLP_TEXT_BYTES = 16 * 1024 * 1024
MAX_INSTRUCTION_BYTES = 256 * 1024
_INSTRUCTION_SUFFIXES = {".md", ".txt"}
_EXECUTABLE_PARTS = {"bin", "commands", "executables", "hooks", "scripts"}
_EXTENDED_HEADER_TYPES = {
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
    tarfile.XHDTYPE,
    tarfile.XGLTYPE,
}
_BINARY_MAGIC = BINARY_MAGIC
_WINDOWS_RESERVED = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}

# Publish form identifiers (validated by the route before the body is read).
NAMESPACE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,98}[a-z0-9])?$")
COLLECTION_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,98}[a-z0-9])?$")

# Error codes the server puts in the 422 envelope's ``error.code``.
CODE_INVALID_PACKAGE = "invalid_skill_package"
CODE_LICENSE_REQUIRED = "license_required"
# Instruction-pattern scan: critical findings refuse publish (marketplace v2 §5).
REASON_SCAN_CRITICAL = "instruction_scan_critical"

LICENSE_REQUIRED_MESSAGE = (
    "Packages published to an organization or public listing must declare "
    "a license in plugin.json (for example \"MIT\" or \"UNLICENSED\")."
)

# reason -> how to fix it. Every reason this module can produce has an entry.
REASON_HINTS: dict[str, str] = {
    "package_too_large": (
        f"Keep the compressed upload under {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB and the "
        f"expanded package under {MAX_PACKAGE_BYTES // (1024 * 1024)} MiB and "
        f"{MAX_PACKAGE_FILES} files; move large assets out of the package."
    ),
    "member_too_large": (
        f"Every file must be at most {MAX_MEMBER_BYTES // (1024 * 1024)} MiB; split or remove the file."
    ),
    "too_many_members": f"An archive may hold at most {MAX_ARCHIVE_MEMBERS} entries; remove files.",
    "invalid_archive": (
        "Upload a gzip-compressed tar (.tar.gz). Let `sfs skill publish <dir>` build it for you."
    ),
    "extended_header": (
        "Hosted packages must be plain ustar/GNU tar without PAX or long-name records: keep each "
        "path within 100 bytes for the file name and 155 bytes for its directory, and pack with "
        "`sfs skill publish <dir>` (or `tar --format=ustar`)."
    ),
    "unsafe_path": "Use relative paths inside the package only: no absolute paths, '..', or backslashes.",
    "non_portable_path": (
        "Rename the path: no Windows-reserved names (con, nul, aux, com1...), no trailing dot or "
        "space, and none of < > : \" | ? * or control characters."
    ),
    "path_collision": (
        "Two paths differ only by letter case or Unicode normalization; rename one so they "
        "stay distinct on macOS and Windows."
    ),
    "non_regular_file": "Packages may contain only regular files and directories; replace links with real files.",
    "symlink": "Symlinks are not allowed in packages; replace each link with the file it points to.",
    "path_too_long": (
        "Shorten the path: hosted archives are ustar, so a file name may be at most 100 bytes "
        "and its directory at most 155 bytes."
    ),
    "missing_manifest": (
        "Add a plugin.json with \"$schema\": \"" + AGENT_PLUGIN_SCHEMA + "\", \"name\", \"version\" "
        "and \"description\", plus a skills/<name>/SKILL.md for each skill."
    ),
    "plugin_manifest": (
        "Fix plugin.json: it must be a JSON object targeting \"" + AGENT_PLUGIN_SCHEMA + "\" with a "
        "lowercase \"name\", a non-empty \"description\" and a short printable \"version\"."
    ),
    "skill_missing": "Every directory under skills/ needs a SKILL.md with YAML frontmatter.",
    "skill_frontmatter": (
        "SKILL.md must start with a '---' line, then YAML with `name` (matching the directory) "
        "and `description`, then a '---' line at column 0. Use plain keys and values: no "
        "anchors, aliases, tags, duplicate keys, or NEL/U+2028/U+2029 characters."
    ),
    "skill_name": (
        "Use 1-64 lowercase letters, digits and '-' for skill names, with no consecutive '-'; "
        "the name must match the skill's directory."
    ),
    "reserved_name_case": "Spell reserved files exactly (plugin.json, SKILL.md, mcp.json); case matters on Linux.",
    "mcp_config": (
        "Fix the MCP declaration: mcpServers must be an object, URLs must be plain http(s) "
        "without embedded credentials, and secrets must come from the environment at runtime."
    ),
    "mcp_credentials": (
        "Remove credential-like literals from the MCP configuration; reference environment "
        "variables (for example ${API_TOKEN}) that users set at install time."
    ),
    "instruction_not_utf8": "Save instruction files (.md, .txt) as UTF-8.",
    "instruction_bundle_too_large": (
        f"Instruction text (.md/.txt inside skills) must total at most {MAX_INSTRUCTION_BYTES // 1024} "
        "KiB; move reference material into fewer or shorter files."
    ),
    "dlp_scan_limit": (
        f"Text files must be at most {MAX_DLP_TEXT_BYTES // (1024 * 1024)} MiB to be scanned; split the file."
    ),
    "dlp_findings": (
        "Remove the secret or personal data from the reported lines (rotate any real "
        "credential), then validate again. Example values must not look like real keys."
    ),
    "instruction_scan_critical": (
        "Remove the flagged instruction or script pattern (see each finding's rule and line): "
        "no piping downloads into a shell, decoding and executing payloads, reading credential "
        "stores, sending data to capture services, disabling permission prompts, or hidden "
        "characters and comments. Ship install steps as reviewable files instead."
    ),
    "license_required": (
        "Add a \"license\" field to plugin.json (for example \"MIT\", \"Apache-2.0\", or "
        "\"UNLICENSED\" for internal-only work)."
    ),
    "file_changed": (
        "A file or directory changed (or was replaced by a link) while it was being packed; "
        "stop whatever is modifying the package and run again."
    ),
    "unreadable_file": "Make every file in the package readable by you, then run again.",
    "symlinked_source": (
        "Pass the real directory or archive path (for example the output of `realpath`), not a "
        "symbolic link to it."
    ),
    "invalid_package": "Fix the reported problem and validate again.",
}


class HostedSkillPackageError(ValueError):
    """Safe package validation failure suitable for a 422 response.

    ``str(exc)`` is the exact server message. The keyword detail is optional
    and local-only: the server envelope adds ``reason`` alone, never the path,
    line or DLP findings of an upload.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "invalid_package",
        path: str | None = None,
        line: int | None = None,
        findings: list[dict[str, Any]] | None = None,
        code: str = CODE_INVALID_PACKAGE,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.path = path
        self.line = line
        self.findings = findings or []
        self.code = code

    @property
    def hint(self) -> str:
        return REASON_HINTS.get(self.reason, REASON_HINTS["invalid_package"])


class PackageValidationError(HostedSkillPackageError):
    """Raised when the decompressed byte budget is exhausted.

    ``consumed_bytes`` records how much decompressed data was read before the
    reader refused to continue (always bounded by the budget plus one chunk).
    """

    def __init__(self, message: str, *, consumed_bytes: int) -> None:
        super().__init__(message, reason="package_too_large")
        self.consumed_bytes = consumed_bytes


class _HeaderGuardReader(io.RawIOBase):
    """Pass-through reader that parses raw 512-byte tar header blocks BEFORE
    tarfile does and rejects extended-header members outright.

    tarfile consumes GNU longname/longlink ('L'/'K') and PAX ('x'/'g') records
    inside ``next()`` with whatever size the header declares, so a size check
    on yielded members never sees them. Hosted packages must therefore be plain
    ustar/GNU archives without extended headers (paths fit ustar name+prefix).
    """

    _REJECTED_TYPEFLAGS = {b"L", b"K", b"x", b"g"}

    def __init__(self, stream: Any) -> None:
        super().__init__()
        self._stream = stream
        self._header = bytearray()
        self._skip = 0  # data + padding bytes remaining for the current member
        self._done = False

    def readable(self) -> bool:
        return True

    @staticmethod
    def _size(field: bytes) -> int:
        if field and field[0] & 0x80:  # GNU base-256 size encoding
            return int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
        digits = field.split(b"\0", 1)[0].strip(b" \0")
        return int(digits or b"0", 8)

    def _inspect(self, chunk: bytes) -> None:
        offset = 0
        while offset < len(chunk) and not self._done:
            if self._skip:
                step = min(self._skip, len(chunk) - offset)
                self._skip -= step
                offset += step
                continue
            need = 512 - len(self._header)
            self._header += chunk[offset : offset + need]
            offset += need
            if len(self._header) < 512:
                return
            header = bytes(self._header)
            self._header.clear()
            if header == b"\0" * 512:
                self._done = True
                return
            typeflag = header[156:157]
            if typeflag in self._REJECTED_TYPEFLAGS:
                raise HostedSkillPackageError(
                    "Archive uses PAX/GNU extended headers; publish a plain ustar/GNU "
                    "tar.gz without long-name or PAX records",
                    reason="extended_header",
                )
            try:
                size = self._size(header[124:136])
            except ValueError as exc:
                raise HostedSkillPackageError(
                    "Package must be a valid tar.gz archive", reason="invalid_archive"
                ) from exc
            if typeflag in {b"1", b"2", b"3", b"4", b"5", b"6"}:
                size = 0  # link/char/block/dir/fifo entries carry no data blocks
            self._skip = -(-size // 512) * 512

    def readinto(self, buffer) -> int:  # type: ignore[override]
        view = memoryview(buffer)
        chunk = self._stream.read(len(view))
        if not chunk:
            return 0
        self._inspect(chunk)
        view[: len(chunk)] = chunk
        return len(chunk)


class _BudgetedReader(io.RawIOBase):
    """Byte-budgeted wrapper over a decompressing stream.

    tarfile reads GNU longname / PAX extended headers eagerly with the size the
    header declares; without a budget a 600 KiB upload expands to hundreds of
    MiB in memory. Every read passes through here and the cumulative count is
    enforced before the bytes are handed to tarfile.
    """

    def __init__(self, stream: Any, budget: int) -> None:
        super().__init__()
        self._stream = stream
        self._budget = budget
        self.consumed = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:  # type: ignore[override]
        view = memoryview(buffer)
        remaining = self._budget - self.consumed
        if remaining < 0:
            raise PackageValidationError(
                "Expanded package exceeds its decompressed byte budget",
                consumed_bytes=self.consumed,
            )
        # Never read past budget+1: the single extra byte detects overflow, so
        # `consumed <= budget + 1` holds at all times.
        chunk = self._stream.read(min(len(view), remaining + 1))
        if not chunk:
            return 0
        self.consumed += len(chunk)
        if self.consumed > self._budget:
            raise PackageValidationError(
                "Expanded package exceeds its decompressed byte budget",
                consumed_bytes=self.consumed,
            )
        view[: len(chunk)] = chunk
        return len(chunk)


@dataclass(frozen=True)
class HostedSkillPackage:
    name: str
    version: str | None
    description: str
    content_hash: str
    source_commit: str | None
    agent_plugin_schema: str
    manifest: dict
    risk_summary: dict
    compatibility: dict
    license_metadata: dict
    instruction_bundle: list[dict[str, str]]
    canonical_archive: bytes
    # Local-only (validate_hosted_package(defer_dlp_findings=True)): built-in
    # DLP findings an organization's policy may still allow. Never set on the
    # server path, which always blocks on findings.
    deferred_dlp: HostedSkillPackageError | None = field(default=None, compare=False, repr=False)
    # Full instruction-pattern scan result (risk_summary carries the bounded
    # summary). None only for packages built without the scanner.
    instruction_scan: ScanResult | None = field(default=None, compare=False)
    # Byte-derived capability manifest (skills/disclosure.py, marketplace v2
    # §3.2), computed from the same normalized tree on client and server.
    capability_manifest: dict = field(default_factory=dict, compare=False)


def _invalid_archive() -> HostedSkillPackageError:
    return HostedSkillPackageError("Package must be a valid tar.gz archive", reason="invalid_archive")


def _safe_member_name(raw: str) -> PurePosixPath:
    if not raw or "\\" in raw or "\x00" in raw:
        raise HostedSkillPackageError("Archive contains an unsafe path", reason="unsafe_path", path=raw)
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or len(path.parts) > 16:
        raise HostedSkillPackageError("Archive contains an unsafe path", reason="unsafe_path", path=raw)
    for part in path.parts:
        stem = part.split(".", 1)[0].casefold()
        if (
            part in {"", "."}
            or part.endswith((" ", "."))
            or stem in _WINDOWS_RESERVED
            or any(char in '<>:"|?*' or ord(char) < 32 or ord(char) == 127 for char in part)
        ):
            raise HostedSkillPackageError(
                "Archive contains a non-portable path", reason="non_portable_path", path=raw
            )
    return path


def _extract_archive(data: bytes, root: Path) -> int:
    """Stream-extract one upload into ``root``; return decompressed bytes read.

    The archive is opened in sequential stream mode over a byte-budgeted
    decompressor, so there is no seek-back and the budget is the load-bearing
    guard against header-declared size bombs. Extended-header members are
    capped independently, and every regular member is size-checked BEFORE its
    payload is read.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise HostedSkillPackageError(
            f"Compressed package exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit",
            reason="package_too_large",
        )
    files = 0
    total = 0
    collisions = CasefoldCollisionTracker()
    reader = _BudgetedReader(gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb"), MAX_DECOMPRESSED_BYTES)
    guarded = _HeaderGuardReader(reader)
    try:
        archive = tarfile.open(fileobj=guarded, mode="r|")
    except HostedSkillPackageError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise _invalid_archive() from exc
    try:
        with archive:
            members = 0
            while True:
                try:
                    member = archive.next()
                except HostedSkillPackageError:
                    raise
                except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
                    raise _invalid_archive() from exc
                if member is None:
                    break
                members += 1
                if members > MAX_ARCHIVE_MEMBERS:
                    raise HostedSkillPackageError(
                        "Archive contains too many members", reason="too_many_members"
                    )
                if member.type in _EXTENDED_HEADER_TYPES:
                    if member.size > MAX_EXTENDED_HEADER_BYTES:
                        raise HostedSkillPackageError(
                            "Archive extended header is too large", reason="extended_header"
                        )
                    raise HostedSkillPackageError(
                        "Archive contains an unsupported header member", reason="extended_header"
                    )
                if member.size > MAX_MEMBER_BYTES:
                    raise HostedSkillPackageError(
                        f"Package member exceeds the {MAX_MEMBER_BYTES // (1024 * 1024)} MiB limit",
                        reason="member_too_large",
                        path=member.name,
                    )
                path = _safe_member_name(member.name)
                normalized = path.as_posix().rstrip("/")
                try:
                    # The tracker keys on NFC + casefold but compares the raw
                    # spelling for directory identity, so it must see the raw
                    # name: composed and decomposed spellings of one directory
                    # are two directories on Linux and one on macOS.
                    collisions.add(normalized, is_dir=member.isdir())
                except ValueError as exc:
                    raise HostedSkillPackageError(
                        "Archive contains duplicate paths", reason="path_collision", path=normalized
                    ) from exc
                if member.isdir():
                    (root / path).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise HostedSkillPackageError(
                        "Archive may contain only regular files",
                        reason="non_regular_file",
                        path=normalized,
                    )
                files += 1
                total += member.size
                if files > MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                    raise HostedSkillPackageError(
                        "Expanded package exceeds its safety limits", reason="package_too_large"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise HostedSkillPackageError(
                        "Package member could not be read", reason="invalid_archive", path=normalized
                    )
                payload = source.read(MAX_MEMBER_BYTES + 1)
                if len(payload) != member.size or len(payload) > MAX_MEMBER_BYTES:
                    raise HostedSkillPackageError(
                        "Package member size is invalid", reason="invalid_archive", path=normalized
                    )
                destination = root.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
    except HostedSkillPackageError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise _invalid_archive() from exc
    return reader.consumed


def _looks_binary(raw: bytes) -> bool:
    return looks_binary(raw)


def _is_text_member(raw: bytes) -> bool:
    """THE predicate shared by the instruction bundle and the DLP scanner.

    Anything this returns True for may be served as text and is therefore
    always scanned as text; anything it rejects is never served (a `.md` that
    starts with `MZ` or contains a NUL is binary, whatever its suffix says).
    """
    return not _looks_binary(raw)


def _instruction_bundle(package_root: Path, skill_dirs: list[str]) -> list[dict[str, str]]:
    """Instruction text from the VALIDATED skill roots only.

    `skills/decoy/prompt.md` (no SKILL.md) is never served, and a skill that is
    legitimately named `hooks` or `scripts` IS: the executable-directory
    exclusion applies to paths *inside* each skill root, not to the root name.
    """
    bundle: list[dict[str, str]] = []
    total = 0
    for relative_dir in sorted(skill_dirs):
        skill_root = package_root / relative_dir
        if not skill_root.is_dir():
            continue
        for path in sorted(skill_root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _INSTRUCTION_SUFFIXES:
                continue
            inside = path.relative_to(skill_root)
            if any(part.casefold() in _EXECUTABLE_PARTS for part in inside.parts[:-1]):
                continue
            relative = path.relative_to(package_root)
            raw = path.read_bytes()
            if not _is_text_member(raw):
                continue  # binary by the shared predicate: never served as text
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HostedSkillPackageError(
                    f"Instruction file is not UTF-8: {relative}",
                    reason="instruction_not_utf8",
                    path=relative.as_posix(),
                ) from exc
            total += len(raw)
            if total > MAX_INSTRUCTION_BYTES:
                raise HostedSkillPackageError(
                    "Instruction bundle exceeds the 256 KiB limit",
                    reason="instruction_bundle_too_large",
                    path=relative.as_posix(),
                )
            bundle.append({"path": relative.as_posix(), "content": content})
    return bundle


def _canonical_archive(package_root: Path) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        # ustar only: no GNU longname / PAX records, so a downloaded canonical
        # archive can always be re-uploaded through the header guard. Every
        # path was already checked with fits_ustar() at import.
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for path in sorted(package_root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(package_root).as_posix()
                if not fits_ustar(relative):
                    raise HostedSkillPackageError(
                        f"Package path exceeds the ustar limit: {relative}",
                        reason="path_too_long",
                        path=relative,
                    )
                payload = path.read_bytes()
                info = tarfile.TarInfo(relative)
                info.size = len(payload)
                info.mode = 0o644
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _scan_package_dlp(package_root: Path, dlp_policy: dict | None = None) -> int:
    """Scan the served tree and raise on any DLP finding (the server gate)."""
    scanned, blocked = _collect_package_dlp(package_root, dlp_policy)
    if blocked is not None:
        raise blocked
    return scanned


def _collect_package_dlp(
    package_root: Path, dlp_policy: dict | None = None
) -> tuple[int, HostedSkillPackageError | None]:
    """Scan EVERY text-like member of the exact tree that will be served.

    Coverage is decided by content, not by filename suffix: only members that
    are binary by magic bytes or NUL presence are skipped. Text members above
    the per-member scan cap are rejected rather than partially scanned so the
    served archive is always exactly what was scanned. Org publishes also run
    the organization's own DLP policy (custom patterns / allowlist).
    """
    findings: list[Any] = []
    located: list[dict[str, Any]] = []
    custom_patterns = None
    allowlist = None
    categories = ["secrets", "phi"]
    if dlp_policy:
        custom_patterns = dlp_policy.get("custom_patterns") or None
        allowlist = dlp_policy.get("allowlist") or None
        policy_categories = dlp_policy.get("categories")
        if isinstance(policy_categories, list) and policy_categories:
            categories = sorted(set(categories) | set(policy_categories))
    scanned = 0
    for path in sorted(package_root.rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        # Rule: anything that could ever be served as text is scanned as text.
        # Instruction-suffix members are scanned UNCONDITIONALLY (a `.md` that
        # starts with `MZ` is never served — same predicate as the bundle — but
        # a secret inside it still blocks publish); other members are scanned
        # when the shared predicate classifies them as text.
        if not _is_text_member(raw) and path.suffix.lower() not in _INSTRUCTION_SUFFIXES:
            continue
        relative = path.relative_to(package_root).as_posix()
        if len(raw) > MAX_DLP_TEXT_BYTES:
            raise HostedSkillPackageError(
                f"Text package member exceeds the DLP scan limit: {path.relative_to(package_root)}",
                reason="dlp_scan_limit",
                path=relative,
            )
        content = raw.decode("utf-8", errors="replace")
        scanned += 1
        if dlp_policy:
            # The hard gate (secrets + phi) runs through the policy-aware scanner
            # so the org's allowlist and custom patterns both apply to it.
            member_findings = scan_dlp_with_policy(
                content,
                categories=sorted({"secrets", "phi"} | set(categories)),
                custom_patterns=custom_patterns,
                allowlist=allowlist,
            )
        else:
            member_findings = scan_dlp_builtin(content, ["secrets", "phi"])
        findings.extend(member_findings)
        # Location only (never the matched text): local CLI detail.
        located.extend(
            {
                "path": relative,
                "line": finding.line_number,
                "pattern": finding.pattern_name,
                "category": str(finding.category),
                "severity": finding.severity,
            }
            for finding in member_findings
        )
    if findings:
        categories_found = sorted({str(finding.category) for finding in findings})
        first = located[0]
        return scanned, HostedSkillPackageError(
            "Package content failed DLP scanning "
            f"({len(findings)} finding(s): {', '.join(categories_found)})",
            reason="dlp_findings",
            path=first["path"],
            line=first["line"],
            findings=located,
        )
    return scanned, None


# --- SkillError (import/normalize) classification ----------------------------

_SKILL_MD_PATH_RE = re.compile(r"^(?P<path>\S+/SKILL\.md)\b")
_YAML_LINE_RE = re.compile(r"\bline (?P<line>\d+), column \d+")
_TRAILING_PATH_RE = re.compile(r": (?P<path>[^\s:]+)$")


def _classify_skill_error(message: str) -> tuple[str, str | None, int | None]:
    """Map an import-time SkillError message to (reason, path, line).

    Classification only adds detail; the message the server returns is the
    SkillError text itself, unchanged.
    """
    path: str | None = None
    line: int | None = None
    skill_md = _SKILL_MD_PATH_RE.match(message)
    if skill_md:
        path = skill_md.group("path")
    lowered = message.casefold()
    if skill_md and "is missing" in lowered:
        return "skill_missing", path, None
    if skill_md:
        if "invalid yaml frontmatter" in lowered:
            marks = _YAML_LINE_RE.findall(message)
            if marks:
                # The last mark is the problem location. YAML counts from the
                # first frontmatter line; the file has the opening '---' above.
                line = int(marks[-1]) + 1
        elif "must begin with yaml frontmatter" in lowered:
            line = 1
        if "name '" in message and "must match its directory" in lowered:
            return "skill_name", path, None
        return "skill_frontmatter", path, line
    if "symlink" in lowered:
        reason = "symlink"
    elif "ustar limit" in lowered:
        reason = "path_too_long"
    elif "must be spelled exactly" in lowered:
        reason = "reserved_name_case"
    elif "source must contain plugin.json" in lowered:
        reason = "missing_manifest"
    elif "not portable" in lowered:
        reason = "non_portable_path"
    elif "unsafe" in lowered and "path" in lowered:
        reason = "unsafe_path"
    elif "mcp" in lowered:
        reason = "mcp_config"
    elif "skill name" in lowered:
        reason = "skill_name"
    elif "plugin" in lowered:
        reason = "plugin_manifest"
        path = "plugin.json"
    elif "dlp scan limit" in lowered:
        reason = "dlp_scan_limit"
    elif "limit" in lowered and ("exceeds" in lowered or "too long" in lowered):
        reason = "package_too_large"
    else:
        reason = "invalid_package"
    if path is None:
        trailing = _TRAILING_PATH_RE.search(message)
        if trailing and ("/" in trailing.group("path") or "." in trailing.group("path")):
            path = trailing.group("path")
    return reason, path, line


def validate_hosted_package(
    data: bytes,
    *,
    source_locator: str,
    source_commit: str | None = None,
    source_subdir: str = "",
    dlp_policy: dict | None = None,
    defer_dlp_findings: bool = False,
) -> HostedSkillPackage:
    """Validate, normalize, scan, and deterministically repack one upload.

    ``defer_dlp_findings`` is for the local CLI only: DLP *findings* (which an
    organization's DLP allowlist can legitimately permit) are returned on
    ``deferred_dlp`` instead of raised, so the server - which knows the org's
    policy - decides. Every other check, including the DLP scan-size limit,
    still raises. The server never passes it.
    """
    with tempfile.TemporaryDirectory(prefix="sfs-hosted-skill-") as temp:
        root = Path(temp)
        source_root = root / "source"
        source_root.mkdir()
        _extract_archive(data, source_root)
        try:
            # The hosted gate runs its own policy-aware DLP scan below.
            record = import_package(
                source_root,
                root / "store",
                SourceIdentity(
                    source_type="hosted-upload",
                    locator=source_locator,
                    commit=source_commit,
                    subdir=source_subdir,
                ),
                dlp_scan=False,
            )
        except SkillError as exc:
            reason, path, line = _classify_skill_error(str(exc))
            raise HostedSkillPackageError(str(exc), reason=reason, path=path, line=line) from exc
        # R21 P1-1 (c): anything the MCP translation had to strip is a hard
        # 422 for publishers - literals must be removed, never shipped blanked.
        stripped = [
            reason for component, reason, transform in record.degradations
            if component == "portable-mcp" and transform == "names-only"
        ]
        # R24 P1-1: an INDEPENDENT tree-wide walk of the served tree, not a
        # byproduct of whichever normalizer branch ran.
        residual_hits = list(scan_mcp_tree(record.package_root))
        residual = [
            f"{path.relative_to(record.package_root).as_posix()}: {', '.join('.'.join(str(p) for p in leaf) for leaf in leaves[:6])}"
            for path, leaves in residual_hits
        ]
        if stripped or residual:
            raise HostedSkillPackageError(
                "Package MCP configuration carries credential-like literals; remove them before "
                "publishing (" + "; ".join([*stripped, *residual]) + ")",
                reason="mcp_credentials",
                path=(
                    residual_hits[0][0].relative_to(record.package_root).as_posix()
                    if residual_hits
                    else None
                ),
            )
        bundle = _instruction_bundle(
            record.package_root, [skill.relative_dir for skill in record.skills]
        )
        scanned_members, dlp_blocked = _collect_package_dlp(record.package_root, dlp_policy)
        if dlp_blocked is not None and not defer_dlp_findings:
            raise dlp_blocked
        # Runs after DLP, so a secret is always reported as a DLP finding.
        instruction_scan = scan_tree(record.package_root)
        check_instruction_scan(instruction_scan)
        capability_manifest = compute_manifest(record.package_root, scan=instruction_scan)
        manifest = json.loads((record.package_root / "plugin.json").read_text("utf-8"))
        omissions = []
        if record.has_mcp:
            omissions.append("mcp")
        if record.has_executable_extensions or record.has_skill_hook_extensions:
            omissions.append("executable_extensions")
        if any(skill.has_scripts for skill in record.skills):
            omissions.append("scripts_require_consent")
        if record.has_hooks:
            omissions.append("hooks_require_consent")
        compatibility = {
            tool: {
                "native_skills": True,
                "instruction_files": len(bundle),
                "omitted_components": omissions,
            }
            for tool in SUPPORTED_TOOLS
        }
        declared_license = manifest.get("license")
        license_metadata = (
            {"declared": True, "value": declared_license.strip()}
            if isinstance(declared_license, str)
            and declared_license.strip()
            and len(declared_license) <= 200
            else {"declared": False}
        )
        return HostedSkillPackage(
            name=record.name,
            version=record.version,
            description=record.description,
            content_hash=record.content_hash,
            source_commit=source_commit,
            agent_plugin_schema=str(manifest.get("$schema", AGENT_PLUGIN_SCHEMA)),
            manifest=manifest,
            risk_summary={
                "scripts": any(skill.has_scripts for skill in record.skills),
                "mcp": record.has_mcp,
                "executable_extensions": (
                    record.has_executable_extensions or record.has_skill_hook_extensions
                ),
                # SKILL.md frontmatter `hooks:` / `allowed-tools:` are executed or
                # pre-approved by the target tool on invocation (A1 mirror).
                "hooks": record.has_hooks,
                "allowed_tools": list(record.allowed_tools),
                "hook_events": sorted(
                    {event for skill in record.skills for event in skill.hook_events}
                ),
                "disclosed_metadata": {
                    skill.name: dict(skill.disclosed_metadata)
                    for skill in record.skills
                    if skill.disclosed_metadata
                },
                "instruction_only_load": True,
                # Publish-time DLP dossier: the hard gate passed (findings would
                # have blocked), so consumers see what was scanned.
                "dlp": {"status": "passed", "scanned_text_members": scanned_members, "findings": []},
                # Instruction-pattern scan (no critical findings, or publish
                # was refused above). Value-free: rule, file, line, severity.
                "instruction_scan": instruction_scan.summary(),
            },
            compatibility=compatibility,
            license_metadata=license_metadata,
            instruction_bundle=bundle,
            canonical_archive=_canonical_archive(record.package_root),
            deferred_dlp=dlp_blocked,
            instruction_scan=instruction_scan,
            capability_manifest=capability_manifest,
        )


def check_instruction_scan(result: ScanResult) -> None:
    """Refuse a package with any critical instruction-scan finding.

    The message names rule ids only (never paths or matched text), so the
    server can return it as-is; locations stay in ``findings`` for the CLI.
    """
    critical = result.at_least("critical")
    if not critical:
        return
    rules = sorted({finding.rule_id for finding in critical})
    shown = ", ".join(rules[:5]) + (f" and {len(rules) - 5} more" if len(rules) > 5 else "")
    first = critical[0]
    raise HostedSkillPackageError(
        f"Package failed instruction scanning ({len(critical)} critical finding(s): {shown})",
        reason=REASON_SCAN_CRITICAL,
        path=first.path,
        line=first.line,
        findings=[finding.to_problem_dict() for finding in result.at_least("medium")],
    )


def scan_canonical_archive(data: bytes) -> ScanResult:
    """Re-scan a stored canonical archive (rescans). The archive goes through
    the same guarded extraction as an upload, then the same scanner the publish
    gate ran on the normalized tree."""
    with tempfile.TemporaryDirectory(prefix="sfs-skill-rescan-") as temp:
        root = Path(temp)
        _extract_archive(data, root)
        return scan_tree(root)


# --- Publish policy (needs the target, not server state) ---------------------


def license_required_for(owner_type: str, visibility: str | None = None) -> bool:
    """Org and public listings redistribute a package to other people, so the
    package must state its license (§14 #5). Decided by the listing's
    VISIBILITY: a personal publisher's public listing needs one too. Personal
    (user-visibility) uploads need not."""
    return owner_type == "org" or visibility in ("org", "public")


def check_publish_license(
    hosted: HostedSkillPackage, *, owner_type: str, visibility: str | None = None
) -> None:
    """Raise the server's ``license_required`` error when the target needs a
    declared license and the package has none. Any declared value is accepted,
    including "UNLICENSED" / "proprietary" for internal work."""
    if license_required_for(owner_type, visibility) and not hosted.license_metadata.get("declared"):
        raise HostedSkillPackageError(
            LICENSE_REQUIRED_MESSAGE,
            reason="license_required",
            path="plugin.json",
            code=CODE_LICENSE_REQUIRED,
        )


# --- Deterministic packing of a local directory ------------------------------

# Never uploaded from a working directory: VCS metadata and OS litter.
PACK_EXCLUDED_DIRS = frozenset({".git", ".hg", ".svn"})
PACK_EXCLUDED_FILES = frozenset({".DS_Store", "Thumbs.db"})


def pack_directory(source: Path) -> bytes:
    """Pack a package directory into the deterministic ustar tar.gz the CLI
    uploads (and validates locally).

    Entries are sorted, timestamps / owners zeroed, files 0644 and directories
    0755, gzip mtime 0: the same tree always yields the same bytes. Symlinks
    and other special files are packed as what they are (never followed), so
    the shared validator rejects them exactly as the server would. A path too
    long for ustar raises the server's extended-header error instead of
    silently switching to PAX.

    The walk is descriptor-based so nothing outside the package can be read:
    every directory is opened relative to its verified parent descriptor with
    O_DIRECTORY | O_NOFOLLOW, every file with O_NOFOLLOW, and each opened
    object must still be the inode and type the listing saw (fstat). A file
    or directory swapped for a symlink or another object mid-walk is rejected
    with ``file_changed``, never re-opened by path.
    """
    fd, opened = _open_source(source)
    try:
        if not stat.S_ISDIR(opened.st_mode):
            raise HostedSkillPackageError(f"Not a directory: {source}", reason="invalid_archive")
        return _pack_directory_fd(fd)
    finally:
        os.close(fd)


def _pack_directory_fd(root_fd: int) -> bytes:
    if not _FD_WALK_SUPPORTED:
        raise HostedSkillPackageError(
            "Packing a directory needs a POSIX system; build a ustar .tar.gz and pass the archive",
            reason="invalid_archive",
        )
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            _pack_dir_fd(archive, root_fd, "", _PackCounters())
    return output.getvalue()


# Top-level source open: never follow a final symlink, never block on a FIFO.
_SOURCE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _open_source(source: Path) -> tuple[int, os.stat_result]:
    """Open the path the user named - exactly that path, not its resolved
    target - and return (fd, fstat). A final-component symlink is refused:
    following it could publish a directory or file other than the one shown.
    Parent directories may be links (e.g. /tmp -> /private/tmp on macOS)."""
    try:
        if os.path.islink(source):
            raise OSError(errno.ELOOP, "symbolic link")
        fd = os.open(source, _SOURCE_FLAGS)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise HostedSkillPackageError(
                f"{source} is a symbolic link; pass the real directory or archive path instead",
                reason="symlinked_source",
                path=str(source),
            ) from exc
        if exc.errno == errno.ENOENT:
            raise HostedSkillPackageError(
                f"No such package directory or archive: {source}", reason="invalid_archive"
            ) from exc
        raise HostedSkillPackageError(
            f"Cannot open {source}: {exc.strerror or exc}", reason="unreadable_file", path=str(source)
        ) from exc
    try:
        return fd, os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise _os_error(str(source), exc) from exc


def read_publish_source(source: Path) -> tuple[bytes, bool]:
    """Return (upload bytes, packed_from_directory) for a directory or a
    ``.tar.gz``, reading only from one descriptor opened on ``source`` itself.

    A directory is packed from that descriptor. An archive must be a regular
    file within MAX_UPLOAD_BYTES, and at most MAX_UPLOAD_BYTES + 1 bytes are
    ever read, so a swap to a huge file or a device cannot exhaust memory.
    """
    fd, opened = _open_source(source)
    try:
        if stat.S_ISDIR(opened.st_mode):
            return _pack_directory_fd(fd), True
        if not stat.S_ISREG(opened.st_mode):
            raise HostedSkillPackageError(
                f"{source} is not a regular file or directory",
                reason="invalid_archive",
                path=str(source),
            )
        too_large = HostedSkillPackageError(
            f"Compressed package exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit",
            reason="package_too_large",
            path=source.name,
        )
        if opened.st_size > MAX_UPLOAD_BYTES:
            raise too_large
        chunks: list[bytes] = []
        received = 0
        try:
            while received <= MAX_UPLOAD_BYTES:
                chunk = os.read(fd, min(MAX_UPLOAD_BYTES + 1 - received, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
        except OSError as exc:
            raise _os_error(str(source), exc) from exc
        if received > MAX_UPLOAD_BYTES:
            raise too_large  # grew after the size check
        return b"".join(chunks), False
    finally:
        os.close(fd)


_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_FD_WALK_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.scandir in os.supports_fd
    and os.stat in os.supports_dir_fd
    and os.open in os.supports_dir_fd
    and os.readlink in os.supports_dir_fd
)
# errno values meaning "the entry is no longer what the listing saw".
_CHANGED_ERRNOS = {errno.ELOOP, errno.ENOTDIR, errno.ENOENT, errno.EINVAL, errno.EISDIR}


class _PackCounters:
    def __init__(self) -> None:
        self.members = 0
        self.files = 0
        self.total = 0


def _changed(relative: str) -> HostedSkillPackageError:
    return HostedSkillPackageError(
        f"Package file changed while it was being packed: {relative}",
        reason="file_changed",
        path=relative,
    )


def _os_error(relative: str, exc: OSError) -> HostedSkillPackageError:
    if exc.errno in _CHANGED_ERRNOS:
        return _changed(relative)
    return HostedSkillPackageError(
        f"Cannot read package file {relative}: {exc.strerror or exc}",
        reason="unreadable_file",
        path=relative,
    )


def _same_object(expected: os.stat_result, opened: os.stat_result) -> bool:
    return (
        expected.st_dev == opened.st_dev
        and expected.st_ino == opened.st_ino
        and stat.S_IFMT(expected.st_mode) == stat.S_IFMT(opened.st_mode)
    )


def _read_verified_file(dir_fd: int, name: str, expected: os.stat_result, relative: str) -> bytes:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise _os_error(relative, exc) from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or not _same_object(expected, opened):
            raise _changed(relative)
        if opened.st_size > MAX_MEMBER_BYTES:
            raise HostedSkillPackageError(
                f"Package member exceeds the {MAX_MEMBER_BYTES // (1024 * 1024)} MiB limit",
                reason="member_too_large",
                path=relative,
            )
        chunks: list[bytes] = []
        remaining = MAX_MEMBER_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    except OSError as exc:
        raise _os_error(relative, exc) from exc
    finally:
        os.close(fd)
    if len(payload) != opened.st_size:
        raise _changed(relative)  # grew or shrank while being read
    return payload


def _add_member(archive: tarfile.TarFile, info: tarfile.TarInfo, payload: bytes | None, relative: str) -> None:
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    try:
        archive.addfile(info, io.BytesIO(payload) if payload is not None else None)
    except ValueError as exc:
        # tarfile refuses names/links that do not fit ustar.
        raise HostedSkillPackageError(
            "Archive uses PAX/GNU extended headers; publish a plain ustar/GNU "
            "tar.gz without long-name or PAX records",
            reason="extended_header",
            path=relative,
        ) from exc


def _pack_dir_fd(archive: tarfile.TarFile, dir_fd: int, prefix: str, counters: _PackCounters) -> None:
    try:
        with os.scandir(dir_fd) as listing:
            names = sorted(entry.name for entry in listing)
    except OSError as exc:
        raise _os_error(prefix.rstrip("/") or ".", exc) from exc
    for name in names:
        relative = f"{prefix}{name}"
        try:
            seen = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise _os_error(relative, exc) from exc
        mode = seen.st_mode
        if name in PACK_EXCLUDED_FILES and not stat.S_ISDIR(mode):
            continue
        if name in PACK_EXCLUDED_DIRS and stat.S_ISDIR(mode):
            continue
        counters.members += 1
        if counters.members > MAX_ARCHIVE_MEMBERS:
            raise HostedSkillPackageError("Archive contains too many members", reason="too_many_members")
        info = tarfile.TarInfo(relative)
        if stat.S_ISLNK(mode):
            try:
                target = os.readlink(name, dir_fd=dir_fd)
            except OSError as exc:
                raise _os_error(relative, exc) from exc
            info.type = tarfile.SYMTYPE
            info.linkname = target
            info.mode = 0o777
            _add_member(archive, info, None, relative)
        elif stat.S_ISDIR(mode):
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            try:
                child_fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                raise _os_error(relative, exc) from exc
            try:
                if not _same_object(seen, os.fstat(child_fd)):
                    raise _changed(relative)
                _add_member(archive, info, None, relative)
                _pack_dir_fd(archive, child_fd, relative + "/", counters)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(mode):
            payload = _read_verified_file(dir_fd, name, seen, relative)
            counters.files += 1
            counters.total += len(payload)
            if counters.files > MAX_PACKAGE_FILES or counters.total > MAX_PACKAGE_BYTES:
                raise HostedSkillPackageError(
                    "Expanded package exceeds its safety limits", reason="package_too_large"
                )
            info.size = len(payload)
            info.mode = 0o644
            _add_member(archive, info, payload, relative)
        else:
            # FIFOs, sockets, devices: never opened (a FIFO open would block);
            # packed as a FIFO entry so validation refuses it like the server.
            info.type = tarfile.FIFOTYPE
            info.mode = 0o644
            _add_member(archive, info, None, relative)
