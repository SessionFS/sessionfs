"""Safe hosted ingestion for portable Agent Skill and Agent Plugin packages."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sessionfs.security.secrets import scan_dlp as scan_dlp_with_policy
from sessionfs.server.dlp import scan_dlp
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

MAX_UPLOAD_BYTES = 128 * 1024 * 1024
MAX_PACKAGE_FILES = 5_000
MAX_ARCHIVE_MEMBERS = 10_000
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
# Hard ceiling on DECOMPRESSED bytes pulled from an upload. Reached before any
# member cap can run when a GNU longname/PAX header claims a huge size.
MAX_DECOMPRESSED_BYTES = MAX_PACKAGE_BYTES + MAX_ARCHIVE_MEMBERS * 1024
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


class HostedSkillPackageError(ValueError):
    """Safe package validation failure suitable for a 422 response."""


class PackageValidationError(HostedSkillPackageError):
    """Raised when the decompressed byte budget is exhausted.

    ``consumed_bytes`` records how much decompressed data was read before the
    reader refused to continue (always bounded by the budget plus one chunk).
    """

    def __init__(self, message: str, *, consumed_bytes: int) -> None:
        super().__init__(message)
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
                    "tar.gz without long-name or PAX records"
                )
            try:
                size = self._size(header[124:136])
            except ValueError as exc:
                raise HostedSkillPackageError("Package must be a valid tar.gz archive") from exc
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


def _safe_member_name(raw: str) -> PurePosixPath:
    if not raw or "\\" in raw or "\x00" in raw:
        raise HostedSkillPackageError("Archive contains an unsafe path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or len(path.parts) > 16:
        raise HostedSkillPackageError("Archive contains an unsafe path")
    for part in path.parts:
        stem = part.split(".", 1)[0].casefold()
        if (
            part in {"", "."}
            or part.endswith((" ", "."))
            or stem in _WINDOWS_RESERVED
            or any(char in '<>:"|?*' or ord(char) < 32 or ord(char) == 127 for char in part)
        ):
            raise HostedSkillPackageError("Archive contains a non-portable path")
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
        raise HostedSkillPackageError(f"Compressed package exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit")
    files = 0
    total = 0
    collisions = CasefoldCollisionTracker()
    reader = _BudgetedReader(gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb"), MAX_DECOMPRESSED_BYTES)
    guarded = _HeaderGuardReader(reader)
    try:
        archive = tarfile.open(fileobj=guarded, mode="r|")
    except PackageValidationError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise HostedSkillPackageError("Package must be a valid tar.gz archive") from exc
    try:
        with archive:
            members = 0
            while True:
                try:
                    member = archive.next()
                except PackageValidationError:
                    raise
                except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
                    raise HostedSkillPackageError("Package must be a valid tar.gz archive") from exc
                if member is None:
                    break
                members += 1
                if members > MAX_ARCHIVE_MEMBERS:
                    raise HostedSkillPackageError("Archive contains too many members")
                if member.type in _EXTENDED_HEADER_TYPES:
                    if member.size > MAX_EXTENDED_HEADER_BYTES:
                        raise HostedSkillPackageError("Archive extended header is too large")
                    raise HostedSkillPackageError("Archive contains an unsupported header member")
                if member.size > MAX_MEMBER_BYTES:
                    raise HostedSkillPackageError(f"Package member exceeds the {MAX_MEMBER_BYTES // (1024 * 1024)} MiB limit")
                path = _safe_member_name(member.name)
                normalized = path.as_posix().rstrip("/")
                try:
                    # The tracker keys on NFC + casefold but compares the raw
                    # spelling for directory identity, so it must see the raw
                    # name: composed and decomposed spellings of one directory
                    # are two directories on Linux and one on macOS.
                    collisions.add(normalized, is_dir=member.isdir())
                except ValueError as exc:
                    raise HostedSkillPackageError("Archive contains duplicate paths") from exc
                if member.isdir():
                    (root / path).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise HostedSkillPackageError("Archive may contain only regular files")
                files += 1
                total += member.size
                if files > MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                    raise HostedSkillPackageError("Expanded package exceeds its safety limits")
                source = archive.extractfile(member)
                if source is None:
                    raise HostedSkillPackageError("Package member could not be read")
                payload = source.read(MAX_MEMBER_BYTES + 1)
                if len(payload) != member.size or len(payload) > MAX_MEMBER_BYTES:
                    raise HostedSkillPackageError("Package member size is invalid")
                destination = root.joinpath(*path.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
    except PackageValidationError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise HostedSkillPackageError("Package must be a valid tar.gz archive") from exc
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
                raise HostedSkillPackageError(f"Instruction file is not UTF-8: {relative}") from exc
            total += len(raw)
            if total > MAX_INSTRUCTION_BYTES:
                raise HostedSkillPackageError("Instruction bundle exceeds the 256 KiB limit")
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
                    raise HostedSkillPackageError(f"Package path exceeds the ustar limit: {relative}")
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
    """Scan EVERY text-like member of the exact tree that will be served.

    Coverage is decided by content, not by filename suffix: only members that
    are binary by magic bytes or NUL presence are skipped. Text members above
    the per-member scan cap are rejected rather than partially scanned so the
    served archive is always exactly what was scanned. Org publishes also run
    the organization's own DLP policy (custom patterns / allowlist).
    """
    findings: list[Any] = []
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
        if len(raw) > MAX_DLP_TEXT_BYTES:
            raise HostedSkillPackageError(
                f"Text package member exceeds the DLP scan limit: {path.relative_to(package_root)}"
            )
        content = raw.decode("utf-8", errors="replace")
        scanned += 1
        if dlp_policy:
            # The hard gate (secrets + phi) runs through the policy-aware scanner
            # so the org's allowlist and custom patterns both apply to it.
            findings.extend(
                scan_dlp_with_policy(
                    content,
                    categories=sorted({"secrets", "phi"} | set(categories)),
                    custom_patterns=custom_patterns,
                    allowlist=allowlist,
                )
            )
        else:
            findings.extend(scan_dlp(content, ["secrets", "phi"]))
    if findings:
        categories_found = sorted({str(finding.category) for finding in findings})
        raise HostedSkillPackageError(
            "Package content failed DLP scanning "
            f"({len(findings)} finding(s): {', '.join(categories_found)})"
        )
    return scanned


def validate_hosted_package(
    data: bytes,
    *,
    source_locator: str,
    source_commit: str | None = None,
    source_subdir: str = "",
    dlp_policy: dict | None = None,
) -> HostedSkillPackage:
    """Validate, normalize, scan, and deterministically repack one upload."""
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
            raise HostedSkillPackageError(str(exc)) from exc
        # R21 P1-1 (c): anything the MCP translation had to strip is a hard
        # 422 for publishers - literals must be removed, never shipped blanked.
        stripped = [
            reason for component, reason, transform in record.degradations
            if component == "portable-mcp" and transform == "names-only"
        ]
        # R24 P1-1: an INDEPENDENT tree-wide walk of the served tree, not a
        # byproduct of whichever normalizer branch ran.
        residual = [
            f"{path.relative_to(record.package_root).as_posix()}: {', '.join('.'.join(str(p) for p in leaf) for leaf in leaves[:6])}"
            for path, leaves in scan_mcp_tree(record.package_root)
        ]
        if stripped or residual:
            raise HostedSkillPackageError(
                "Package MCP configuration carries credential-like literals; remove them before "
                "publishing (" + "; ".join([*stripped, *residual]) + ")"
            )
        bundle = _instruction_bundle(
            record.package_root, [skill.relative_dir for skill in record.skills]
        )
        scanned_members = _scan_package_dlp(record.package_root, dlp_policy)
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
            },
            compatibility=compatibility,
            license_metadata=license_metadata,
            instruction_bundle=bundle,
            canonical_archive=_canonical_archive(record.package_root),
        )
