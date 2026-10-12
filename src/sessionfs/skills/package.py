"""Standards-preserving Agent Skill and Agent Plugin import."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from dataclasses import replace as _replace

from sessionfs.skills.models import (
    PackageRecord,
    SkillError,
    SkillMetadata,
    SourceIdentity,
    clean_untrusted_text,
)
from sessionfs.skills.path_safety import CasefoldCollisionTracker, is_link_or_reparse

AGENT_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
AGENT_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
MAX_FILES = 5_000
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024  # text-suffix members; bundled JS in official plugins exceeds 1 MiB
MAX_FRONTMATTER_BYTES = 64 * 1024
MAX_PATH_DEPTH = 16
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_PLUGIN_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}
_EXECUTABLE_DIRS = {"hooks", "commands", "agents", "scripts", "bin", "executables"}
_TEXT_SUFFIXES = {
    ".bash",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
    ".zsh",
}
# Consent policy is INVERTED: inside a skill directory any file that is not
# on this inert allowlist (or is a shebang file, or lives under scripts/)
# requires `skill-scripts` consent. Suffix denylists missed .rb/.bat/.exe/...
_INERT_SUFFIXES = {
    ".csv",
    ".css",
    ".gif",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".svg",
    ".toml",
    ".txt",
    ".xml",
    ".xsd",
    ".yaml",
    ".yml",
}
# Exact document names only (README / LICENSE / NOTICE, optionally .md/.txt):
# a prefix match let README.exe and license.py skip script consent.
_INERT_DOCUMENT_STEMS = {"license", "readme", "notice"}
_SKILL_HOOK_EXTENSION_NAMES = {"hooks.json", "hooks"}
# SKILL.md frontmatter keys the target tool acts on when the skill is invoked.
_FRONTMATTER_HOOK_KEYS = ("hooks",)
_FRONTMATTER_TOOL_GRANT_KEYS = ("allowed-tools", "disallowed-tools")
_FRONTMATTER_DISCLOSED_KEYS = ("context", "agent", "model", "effort", "shell")
_WINDOWS_RESERVED = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


MAX_FRONTMATTER_DEPTH = 8


# Characters PyYAML (YAML 1.1) treats as line breaks but YAML 1.2 parsers used
# by target tools treat as ordinary content. No single parse can model both,
# so they are refused inside SKILL.md frontmatter (fail closed); the body may
# still contain them. (VT/FF/FS/GS/RS are already rejected by PyYAML itself.)
_YAML_VERSION_AMBIGUOUS_BREAKS = frozenset("\x85\u2028\u2029")


class _UniqueSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys, anchors/aliases, and deep nesting.

    Aliases let a few KiB of frontmatter expand to gigabytes when serialized
    (and cyclic aliases raise from json.dumps); frontmatter never needs them.
    """

    _depth = 0

    def compose_node(self, parent, index):  # type: ignore[override]
        if self.check_event(yaml.events.AliasEvent):
            event = self.peek_event()
            raise yaml.constructor.ConstructorError(
                None, None, "YAML aliases are not allowed in frontmatter", event.start_mark
            )
        event = self.peek_event()
        if getattr(event, "anchor", None):
            raise yaml.constructor.ConstructorError(
                None, None, "YAML anchors are not allowed in frontmatter", event.start_mark
            )
        # Explicit tags (`!!binary`, `!!str`, `!custom`, ...) are refused: target
        # tools resolve unsupported tags differently (often as plain strings), so
        # a tagged `allowed-tools` key could be a grant there but not here.
        implicit = getattr(event, "implicit", True)
        explicit = not any(implicit) if isinstance(implicit, tuple) else not implicit
        if getattr(event, "tag", None) is not None and explicit:
            raise yaml.constructor.ConstructorError(
                None, None, "YAML tags are not allowed in frontmatter", event.start_mark
            )
        self._depth += 1
        try:
            if self._depth > MAX_FRONTMATTER_DEPTH:
                raise yaml.constructor.ConstructorError(
                    None, None, f"frontmatter nesting exceeds {MAX_FRONTMATTER_DEPTH} levels", event.start_mark
                )
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def _construct_unique_mapping(
    loader: _UniqueSafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            # Every frontmatter key is a string; a key PyYAML resolves to
            # another type (bool `on`, int, bytes) could be a string key with
            # capability meaning in a target tool's parser.
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found a non-string key {key!r}",
                key_node.start_mark,
            )
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_name(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise SkillError(
            f"{label} must be 1-64 lowercase characters using letters, digits, '-' or '.'"
        )
    if "--" in value or ".." in value:
        raise SkillError(f"{label} cannot contain consecutive '-' or '.' characters")
    return value


def _validate_skill_name(value: Any) -> str:
    if not isinstance(value, str) or not _SKILL_NAME_RE.fullmatch(value) or "--" in value:
        raise SkillError(
            "skill name must be 1-64 lowercase characters using letters, digits, or '-'"
        )
    return value


def _validate_text(value: Any, *, label: str, max_length: int) -> str:
    """Package metadata text: escape sequences / C0 / C1 controls are stripped at
    ingestion (they would otherwise reach terminals and logs), length bounded."""
    if not isinstance(value, str) or not value.strip():
        raise SkillError(f"{label} requires a non-empty string")
    if len(value) > max_length:
        raise SkillError(f"{label} is too long (limit {max_length} characters)")
    cleaned = clean_untrusted_text(value, limit=max_length).strip()
    if not cleaned:
        raise SkillError(f"{label} requires a non-empty string")
    return cleaned


USTAR_NAME_LIMIT = 100
USTAR_PREFIX_LIMIT = 155


def fits_ustar(relative: str) -> bool:
    """True when a POSIX path fits the ustar name(100)/prefix(155) split.

    Canonical archives are written in ustar format (no GNU/PAX extended
    headers, which the hosted validator rejects), so every package path must
    be representable without them — mirrors tarfile's `_posix_split_name`.
    """
    encoded = relative.encode("utf-8")
    if len(encoded) <= USTAR_NAME_LIMIT:
        return True
    if len(encoded) > USTAR_NAME_LIMIT + USTAR_PREFIX_LIMIT + 1:
        return False
    components = encoded.split(b"/")
    for index in range(1, len(components)):
        prefix = b"/".join(components[:index])
        name = b"/".join(components[index:])
        if len(prefix) <= USTAR_PREFIX_LIMIT and len(name) <= USTAR_NAME_LIMIT:
            return True
    return False


def _validate_portable_relative_path(relative: str) -> PurePosixPath:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or len(path.parts) > MAX_PATH_DEPTH:
        raise SkillError(f"Unsafe package path: {relative}")
    if not fits_ustar(relative):
        raise SkillError(
            f"Package path exceeds the ustar limit (name <= {USTAR_NAME_LIMIT} bytes with a "
            f"prefix <= {USTAR_PREFIX_LIMIT} bytes): {relative}"
        )
    for part in path.parts:
        stem = part.split(".", 1)[0].casefold()
        if (
            not part
            or part.endswith((" ", "."))
            or stem in _WINDOWS_RESERVED
            or any(char in '<>:"|?*\\' or ord(char) < 32 or ord(char) == 127 for char in part)
        ):
            raise SkillError(f"Package path is not portable across supported systems: {relative}")
    return path


MAX_DLP_TEXT_BYTES = 16 * 1024 * 1024  # == per-file cap: every text member is scanned, never skipped
INSTRUCTION_SUFFIXES = {".md", ".txt"}
BINARY_MAGIC = (
    b"\x89PNG",
    b"GIF8",
    b"\xff\xd8\xff",
    b"%PDF",
    b"\x1f\x8b",
    b"PK\x03\x04",
    b"\x7fELF",
    b"MZ",
    b"\x00asm",
    b"\xca\xfe\xba\xbe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
)


def looks_binary(raw: bytes) -> bool:
    """A NUL anywhere makes a member binary; magic signatures are only
    meaningful at the start of the member."""
    if b"\x00" in raw:
        return True
    head = raw[:8192]
    return any(head.startswith(magic) for magic in BINARY_MAGIC)


def is_text_member(raw: bytes) -> bool:
    """THE predicate shared by the client import DLP scan, the hosted
    instruction bundle and the hosted DLP scanner: what may ever be served as
    text is always scanned as text."""
    return not looks_binary(raw)


def scan_package_for_dlp(package_root: Path) -> list[dict[str, Any]]:
    """Client-side twin of the hosted DLP gate: every text member (and every
    instruction-suffix member, whatever its bytes) is scanned with the standard
    secrets + PHI patterns. Findings are returned REDACTED (path, line,
    category, pattern) - the matched text never leaves the scanner."""
    from sessionfs.security.secrets import scan_dlp

    findings: list[dict[str, Any]] = []
    for path, relative, size in _safe_files(package_root):
        raw = path.read_bytes()
        suffix = PurePosixPath(relative).suffix.lower()
        if not is_text_member(raw) and suffix not in INSTRUCTION_SUFFIXES:
            continue
        if len(raw) > MAX_DLP_TEXT_BYTES:
            raise SkillError(f"Text package member exceeds the DLP scan limit: {relative}")
        content = raw.decode("utf-8", errors="replace")
        for finding in scan_dlp(content, ["secrets", "phi"]):
            findings.append(
                {
                    "path": relative,
                    "line": int(getattr(finding, "line_number", 0) or 0),
                    "category": str(getattr(finding, "category", "")),
                    "pattern": str(getattr(finding, "pattern_name", "")),
                    "severity": str(getattr(finding, "severity", "")),
                }
            )
    findings.sort(key=lambda item: (item["path"], item["line"], item["pattern"]))
    return findings


def _exact_entry(directory: Path, name: str) -> Path | None:
    """Return `directory/name` only if the directory holds an entry spelled
    EXACTLY that way.

    A case-insensitive filesystem (APFS, NTFS) resolves `plugin.json` to a
    file named `Plugin.json`, Linux does not - so a package would validate
    on one host and fail on another. A casefold/Unicode-equivalent entry
    with a different spelling is a hard error, never silently accepted.
    """
    try:
        entries = os.listdir(directory)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise SkillError(f"Cannot read package directory {directory.name}: {exc}") from exc
    if name in entries:
        return directory / name
    wanted = unicodedata.normalize("NFC", name).casefold()
    for entry in entries:
        if unicodedata.normalize("NFC", entry).casefold() == wanted:
            raise SkillError(
                f"Reserved file {name!r} must be spelled exactly; found {entry!r} "
                "(case-insensitive filesystems would accept it, Linux would not)"
            )
    return None


def _exact_relative_file(root: Path, relative: str, *, label: str) -> Path:
    """Resolve a manifest-referenced relative file (e.g. `./servers.json`)
    with exact-spelling probes on every segment; must be a regular file."""
    clean = relative.strip().replace("\\", "/")
    clean = clean[2:] if clean.startswith("./") else clean
    pure = PurePosixPath(clean)
    if not clean or pure.is_absolute() or ".." in pure.parts or len(pure.parts) > 16:
        raise SkillError(f"{label}: unsafe path {relative!r}")
    current: Path | None = root
    for part in pure.parts:
        current = _exact_entry(current, part) if current is not None else None
        if current is None:
            raise SkillError(f"{label}: referenced file does not exist: {relative}")
        if is_link_or_reparse(current):
            raise SkillError(f"Package symlink is not allowed: {relative}")
    assert current is not None
    if not current.is_file():
        raise SkillError(f"{label}: referenced path is not a file: {relative}")
    return current


def _exact_file(directory: Path, name: str) -> Path | None:
    """`_exact_entry` narrowed to regular (non-link) files."""
    path = _exact_entry(directory, name)
    if path is None:
        return None
    if is_link_or_reparse(path):
        raise SkillError(f"Package symlink is not allowed: {name}")
    return path if path.is_file() else None


def _assert_store_path(root: Path, target: Path) -> None:
    root = Path(os.path.abspath(root))
    target = Path(os.path.abspath(target))
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise SkillError(f"Package store path escapes its root: {target}") from exc
    if is_link_or_reparse(root):
        raise SkillError(f"Package store root cannot be a symlink: {root}")
    current = root
    for part in relative.parts:
        current /= part
        if is_link_or_reparse(current):
            raise SkillError(f"Package store path traverses a symlink: {current}")
        if current.exists() and current != target and not current.is_dir():
            raise SkillError(f"Package store parent is not a directory: {current}")


def _read_bounded(path: Path, *, label: str, limit: int = MAX_TEXT_BYTES) -> bytes:
    """Read a metadata file without ever pulling more than `limit` bytes.

    Stat first (a sparse/huge file is rejected before any read) and then read
    at most limit+1 bytes so a file that grows under us is still bounded.
    """
    if is_link_or_reparse(path):
        raise SkillError(f"{label} cannot be a symlink")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SkillError(f"Cannot read {label}: {exc}") from exc
    if size > limit:
        raise SkillError(f"{label} exceeds the {limit}-byte limit")
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        raise SkillError(f"Cannot read {label}: {exc}") from exc
    if len(raw) > limit:
        raise SkillError(f"{label} exceeds the {limit}-byte limit")
    return raw


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    raw = _read_bounded(path, label=label)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SkillError(f"{label} must contain a JSON object")
    return value


def validate_plugin_manifest(path: Path) -> dict[str, Any]:
    """Validate the portable Agent Plugins v1 manifest subset we consume."""
    value = _load_json_object(path, label="plugin.json")
    if value.get("$schema") != AGENT_PLUGIN_SCHEMA:
        raise SkillError(f"plugin.json must target {AGENT_PLUGIN_SCHEMA}")
    _validate_name(value.get("name"), label="plugin name")
    # Agent Plugins v1 requires clients to report and ignore unknown fields,
    # not reject an otherwise valid package. Preserve the original bytes.
    _ = sorted(set(value) - _PLUGIN_FIELDS)
    extensions = value.get("extensions")
    if extensions is not None and not isinstance(extensions, dict):
        raise SkillError("plugin.json extensions must be an object")
    description = value.get("description")
    if description is not None:
        # Store the SANITIZED value: this manifest feeds package records and
        # marketplace listings (the original bytes survive in the copied tree).
        value["description"] = _validate_text(
            description, label="plugin description", max_length=4096
        )
    version = value.get("version")
    if version is not None and (
        not isinstance(version, str)
        or not version.strip()
        or len(version) > 100
        or any(ord(char) < 33 or ord(char) == 127 for char in version)
    ):
        raise SkillError("plugin version must be a short, printable string")
    return value


def validate_skill_dir(path: Path, *, relative_dir: str) -> SkillMetadata:
    """Validate one Agent Skills directory without changing its bytes."""
    skill_file = _exact_file(path, "SKILL.md")
    if skill_file is None:
        raise SkillError(f"{relative_dir}/SKILL.md is missing")
    raw = _read_bounded(skill_file, label=f"{relative_dir}/SKILL.md")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError(f"{relative_dir}/SKILL.md must be UTF-8") from exc
    # SessionFS must never close the frontmatter EARLIER than a target tool,
    # or `hooks:` / `allowed-tools:` after the early "closer" would bypass the
    # consent gate and the hosted risk summary. So split on LF only (not
    # `str.splitlines()`, which also breaks on VT/FF/FS/GS/RS/NEL/LS/PS) and
    # accept only an exact column-0 `---` closer. A tool that is more lenient
    # closes earlier and therefore sees less, which is the safe direction.
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].rstrip(" \t") != "---":
        raise SkillError(f"{relative_dir}/SKILL.md must begin with YAML frontmatter")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line == "---")
    except StopIteration as exc:
        raise SkillError(f"{relative_dir}/SKILL.md has unterminated YAML frontmatter") from exc
    frontmatter = "\n".join(lines[1:end])
    if len(frontmatter.encode("utf-8")) > MAX_FRONTMATTER_BYTES:
        raise SkillError(
            f"{relative_dir}/SKILL.md frontmatter exceeds {MAX_FRONTMATTER_BYTES} bytes"
        )
    if any(char in _YAML_VERSION_AMBIGUOUS_BREAKS for char in frontmatter):
        raise SkillError(
            f"{relative_dir}/SKILL.md frontmatter contains a NEL, U+2028 or U+2029 character; "
            "YAML 1.1 and 1.2 parse these differently, so remove them from the frontmatter"
        )
    try:
        # The custom loader subclasses SafeLoader to reject duplicate keys,
        # anchors/aliases, and nesting deeper than MAX_FRONTMATTER_DEPTH.
        metadata = yaml.load(frontmatter, Loader=_UniqueSafeLoader)  # nosec B506
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise SkillError(f"{relative_dir}/SKILL.md has invalid YAML frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise SkillError(f"{relative_dir}/SKILL.md frontmatter must be an object")
    name = _validate_skill_name(metadata.get("name"))
    expected_dir = PurePosixPath(relative_dir).name
    if expected_dir != "pending" and expected_dir != name:
        raise SkillError(
            f"{relative_dir}/SKILL.md name '{name}' must match its directory '{expected_dir}'"
        )
    description = _validate_text(
        metadata.get("description"),
        label=f"{relative_dir}/SKILL.md description",
        max_length=1024,
    )
    hook_events, allowed_tools, disclosed = _frontmatter_capabilities(metadata)
    has_hooks = bool(hook_events) or bool(allowed_tools)
    has_scripts = False
    has_hook_extensions = False
    for candidate, _, _ in _safe_files(path):
        # Case-folded: on case-insensitive filesystems HOOKS.JSON / Hooks/ /
        # SCRIPTS/run.PY resolve to the lowercase names the target tool acts on.
        parts = tuple(part.casefold() for part in candidate.relative_to(path).parts)
        if parts[0] in _SKILL_HOOK_EXTENSION_NAMES:
            has_hook_extensions = True
        if has_scripts:
            continue
        try:
            with candidate.open("rb") as handle:
                has_shebang = handle.read(2) == b"#!"
        except OSError as exc:
            raise SkillError(f"Cannot inspect skill asset {candidate}: {exc}") from exc
        if "scripts" in parts or has_shebang or not _is_inert_asset(candidate.name):
            has_scripts = True
    return SkillMetadata(
        name=name,
        description=description,
        relative_dir=relative_dir,
        has_scripts=has_scripts,
        has_hooks=has_hooks,
        hook_events=hook_events,
        allowed_tools=allowed_tools,
        has_hook_extensions=has_hook_extensions,
        disclosed_metadata=disclosed,
    )


def is_inert_asset(filename: str) -> bool:
    """True when a skill asset cannot plausibly be executed by an agent."""
    lowered = filename.casefold()
    if lowered == "skill.md":
        return True
    pure = PurePosixPath(lowered)
    suffix = pure.suffix
    if pure.stem in _INERT_DOCUMENT_STEMS and suffix in {"", ".md", ".txt"}:
        return True
    return bool(suffix) and suffix in _INERT_SUFFIXES


# Internal alias kept for the existing call sites.
_is_inert_asset = is_inert_asset


def is_executable_reference(relative: str) -> bool:
    """True when a package-relative path names material an agent could RUN.

    Shared by the emit-time plugin-root projection (adapters) so that handing a
    target tool a concrete path to an executable asset always carries the same
    consent requirement the asset itself does.
    """
    pure = PurePosixPath(relative)
    if any(part.casefold() in _EXECUTABLE_DIRS for part in pure.parts):
        return True
    return not is_inert_asset(pure.name)


def _stringify_frontmatter(value: Any, *, limit: int = 200) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    except (ValueError, TypeError, RecursionError) as exc:
        raise SkillError("SKILL.md frontmatter value cannot be serialized") from exc
    text = "".join(char for char in text if ord(char) >= 32 and ord(char) != 127)
    return text[:limit]


def _frontmatter_capabilities(
    metadata: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Detect capability-bearing SKILL.md frontmatter keys from parsed YAML.

    Returns (hook_events, allowed_tools, disclosed_metadata). Hooks and tool
    pre-approval are consent-gated; the remaining keys are disclosed only.
    """
    hook_events: list[str] = []
    for key in _FRONTMATTER_HOOK_KEYS:
        value = metadata.get(key)
        if value is None or value == "" or value == {} or value == []:
            continue
        if isinstance(value, dict):
            hook_events.extend(_stringify_frontmatter(item, limit=64) for item in value)
        elif isinstance(value, list):
            hook_events.append(f"{key}[{len(value)}]")
        else:
            hook_events.append(_stringify_frontmatter(value, limit=64))
    allowed_tools: list[str] = []
    for key in _FRONTMATTER_TOOL_GRANT_KEYS:
        value = metadata.get(key)
        if value is None or value == "" or value == []:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            allowed_tools.append(f"{key}:{_stringify_frontmatter(item, limit=64)}")
    disclosed: list[tuple[str, str]] = []
    for key in _FRONTMATTER_DISCLOSED_KEYS:
        if key in metadata and metadata[key] is not None:
            disclosed.append((key, _stringify_frontmatter(metadata[key])))
    return (
        tuple(dict.fromkeys(hook_events)),
        tuple(dict.fromkeys(allowed_tools)),
        tuple(disclosed),
    )


# Local materialization skips VCS metadata and OS droppings — exactly what a
# remote git import never contains. Deliberately minimal and documented: no
# node_modules or other heuristics; anything else in the tree is part of the
# package and is classified for consent like every other file.
_VCS_METADATA_DIRS = {".git", ".hg", ".svn", ".jj"}
# Shared with instruction provenance's auxiliary walker (same pruning set).
VCS_METADATA_DIRS = frozenset(_VCS_METADATA_DIRS)
_OS_DROPPINGS = {".ds_store", "thumbs.db"}


def _safe_files(root: Path) -> list[tuple[Path, str, int]]:
    files: list[tuple[Path, str, int]] = []
    total = 0
    collisions = CasefoldCollisionTracker()
    # os.walk(followlinks=False) still descends a symlinked TOP-LEVEL root.
    if is_link_or_reparse(root):
        raise SkillError(f"Package directory cannot be a symlink: {root.name}")
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            base = Path(dirpath)
            dirnames[:] = [name for name in dirnames if name.casefold() not in _VCS_METADATA_DIRS]
            for name in list(dirnames):
                candidate = base / name
                if is_link_or_reparse(candidate):
                    raise SkillError(f"Package symlink is not allowed: {candidate.relative_to(root)}")
            dirnames.sort()
            for name in sorted(filenames):
                if name.casefold() in _OS_DROPPINGS:
                    continue
                candidate = base / name
                if is_link_or_reparse(candidate):
                    raise SkillError(
                        f"Package symlink is not allowed: {candidate.relative_to(root)}"
                    )
                if not candidate.is_file():
                    raise SkillError(
                        f"Package entry must be a regular file: {candidate.relative_to(root)}"
                    )
                relative = candidate.relative_to(root).as_posix()
                _validate_portable_relative_path(relative)
                try:
                    # Same rule as the Git and hosted loaders: Notes.md + notes.md
                    # (or Docs/ + docs/, a + a/x) is not reproducible everywhere.
                    collisions.add(relative)
                except ValueError as exc:
                    raise SkillError(f"Package {exc}") from exc
                size = candidate.stat().st_size
                if size > MAX_FILE_BYTES:
                    raise SkillError(
                        f"File exceeds the {MAX_FILE_BYTES}-byte per-file limit: "
                        f"{candidate.relative_to(root)}"
                    )
                if size > MAX_TEXT_BYTES and candidate.suffix.lower() in _TEXT_SUFFIXES:
                    raise SkillError(
                        f"Text file exceeds the {MAX_TEXT_BYTES}-byte limit: "
                        f"{candidate.relative_to(root)}"
                    )
                total += size
                files.append((candidate, relative, size))
                if len(files) > MAX_FILES:
                    raise SkillError(f"Package exceeds the {MAX_FILES}-file limit")
                if total > MAX_TOTAL_BYTES:
                    raise SkillError(f"Package exceeds the {MAX_TOTAL_BYTES}-byte limit")
    except OSError as exc:
        raise SkillError(f"Cannot inspect package: {exc}") from exc
    return files


def _copy_bounded_file(source: Path, destination: Path, *, label: str) -> None:
    """Copy one top-level vendor file after a stat-based size check (sparse-safe)."""
    if is_link_or_reparse(source):
        raise SkillError(f"Package symlink is not allowed: {label}")
    _validate_portable_relative_path(label)
    size = source.stat().st_size
    if size > MAX_TOTAL_BYTES:
        raise SkillError(f"Package exceeds the {MAX_TOTAL_BYTES}-byte limit: {label}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    with source.open("rb") as reader, destination.open("wb") as writer:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAX_TOTAL_BYTES:
                raise SkillError(f"Package exceeds the {MAX_TOTAL_BYTES}-byte limit: {label}")
            writer.write(chunk)


def _copy_safe_tree(
    source: Path, destination: Path, *, skip: set[str] | None = None, skip_prefix: str = ""
) -> None:
    """Copy a validated tree; `skip` holds package-relative POSIX paths that
    are written separately (sanitized MCP documents), `skip_prefix` is the
    package-relative location of `source`."""
    if is_link_or_reparse(source):
        raise SkillError(f"Package symlink is not allowed: {source.name}")
    for path, relative, _ in _safe_files(source):
        if skip and (f"{skip_prefix}/{relative}" if skip_prefix else relative) in skip:
            continue
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)


def _portable_manifest_from_vendor(value: dict[str, Any], fallback_name: str) -> dict[str, Any]:
    raw_name = value.get("name") or fallback_name
    normalized = re.sub(r"[^a-z0-9.-]+", "-", str(raw_name).strip().lower()).strip("-.")
    name = _validate_name(normalized, label="plugin name")
    result: dict[str, Any] = {"$schema": AGENT_PLUGIN_SCHEMA, "name": name}
    for field in ("version", "description", "homepage", "repository", "license", "keywords"):
        if field in value:
            result[field] = value[field]
    author = value.get("author")
    if isinstance(author, dict):
        result["author"] = {
            key: item for key, item in author.items()
            if key in {"name", "email", "url"} and isinstance(item, str)
        }
    elif isinstance(author, str):
        result["author"] = {"name": author}
    return result


_MCP_NATIVE_TRANSPORTS = {"stdio", "http", "sse", "streamable-http"}


def translate_native_mcp(
    value: dict[str, Any], *, origin: str
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    portable, degradations, _stripped = translate_native_mcp_detailed(value, origin=origin)
    return portable, degradations


LeafPath = tuple[Any, ...]  # keys / list indices from the document root


def walk_string_leaves(value: Any, path: LeafPath = ()) -> list[tuple[LeafPath, str]]:
    """Every string leaf of a JSON document with its path (depth-first)."""
    leaves: list[tuple[LeafPath, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            leaves.extend(walk_string_leaves(item, (*path, key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            leaves.extend(walk_string_leaves(item, (*path, index)))
    elif isinstance(value, str):
        leaves.append((path, value))
    return leaves


def _leaf_key_path(path: LeafPath) -> str:
    return ".".join(str(part) for part in path)


def _leaf_is_secret_like(path: LeafPath, value: str) -> bool:
    """Score one leaf: the KEY path (its last string key names the value) plus
    the value itself; `url`-like leaves are judged by `_validate_mcp_url`
    elsewhere, so only credential-shaped material is flagged here."""
    if not value:
        return False
    string_keys = [str(part) for part in path if isinstance(part, str)]
    key_name = string_keys[-1] if string_keys else ""
    # `args` elements: option consumption + per-element rules.
    if len(path) >= 2 and path[-2] == "args" and isinstance(path[-1], int):
        return _arg_is_secret_like(value)
    if key_name.lower() in {"url", "httpurl"}:
        # URL-keyed leaves ANYWHERE (extra.url, env.URL, the non-selected form
        # of a Gemini dual url/httpUrl server) are scored as URLs: userinfo or
        # a secret-like query parameter is a credential; a non-URL value falls
        # back to the ordinary value rules.
        return _url_is_secret_like(value)
    if key_name.lower() in {"command", "type", "transport", "cwd"}:
        return False
    return _env_value_is_secret_like(key_name, value)


def _url_is_secret_like(value: str) -> bool:
    from urllib.parse import urlsplit

    if is_secret_reference(value) or contains_plugin_root_placeholder(value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _env_value_is_secret_like("url", value)
    if not parsed.scheme or not parsed.netloc:
        return _env_value_is_secret_like("url", value)
    try:
        _validate_mcp_url(value, label="url")
    except SkillError:
        return True
    return False


def secret_leaves(document: Any) -> list[LeafPath]:
    """Paths of every secret-like string leaf anywhere in a native MCP
    document (servers that will be dropped, keys that are not portable,
    headers on stdio servers, env on remote servers, nested objects, ... -
    everything ships into the hashed payload unless it is blanked)."""
    flagged: list[LeafPath] = []
    for path, value in walk_string_leaves(document):
        if _leaf_is_secret_like(path, value):
            flagged.append(path)
    # `args` lists: option consumption (`--password value`) needs the whole list.
    def visit(node: Any, path: LeafPath) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key == "args" and isinstance(item, list):
                    # Index-preserving: non-string positions are blanked for the
                    # option-consumption pass and walked on their own.
                    strings = [x if isinstance(x, str) else "" for x in item]
                    for index in _secret_arg_indices(strings):
                        flagged.append((*path, "args", index))
                    for index, element in enumerate(item):
                        if not isinstance(element, str):
                            visit(element, (*path, "args", index))
                else:
                    visit(item, (*path, key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, (*path, index))

    visit(document, ())
    return sorted(set(flagged), key=lambda item: tuple(str(part) for part in item))


# Casefolded: the risk engine and the credential walker must agree on what an
# MCP declaration is (`.MCP.json` counts for both, and is rejected as a
# case-variant spelling of a reserved file).
_MCP_DECLARATION_BASENAMES = {"mcp.json", ".mcp.json"}
_MCP_MANIFEST_BASENAMES = {"plugin.json", "gemini-extension.json"}
_MCP_TREE_BASENAMES = _MCP_DECLARATION_BASENAMES | _MCP_MANIFEST_BASENAMES


def _assert_exact_reserved_spelling(path: Path) -> None:
    """A reserved MCP/manifest file must be spelled exactly (`.MCP.json` would be
    an MCP declaration on macOS and an inert file on Linux)."""
    folded = path.name.casefold()
    if folded in _MCP_TREE_BASENAMES and path.name not in _MCP_TREE_BASENAMES:
        expected = next(name for name in _MCP_TREE_BASENAMES if name == folded)
        raise SkillError(
            f"Reserved file {expected!r} must be spelled exactly; found {path.name!r} "
            "(case-insensitive filesystems would accept it, Linux would not)"
        )


def _load_json_document_bytes(path: Path, *, label: str) -> Any:
    """Same bytes loader as `_load_json_object` (BOM / UTF-16 auto-detect via
    json.loads(bytes)); a declaration that does not parse is a HARD error -
    the risk engine counts it as MCP, so the walker must never skip it."""
    raw = _read_bounded(path, label=label)
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillError(f"{label} is not valid JSON (MCP declarations must parse to be scanned): {exc}") from exc


def _mcp_document_for_scan(path: Path, *, referenced: bool = False) -> tuple[Any, LeafPath] | None:
    """The JSON document to score for one tree file: MCP declarations and
    manifest-REFERENCED files are scored WHOLE (a referenced file named
    `plugin.json` is a bare server map, not a manifest); manifests only under
    their `mcpServers` key (other manifest text is not credential-shaped).
    A list-valued manifest `mcpServers` is a hard error everywhere."""
    label = path.name
    document = _load_json_document_bytes(path, label=label)
    if not referenced and path.name.casefold() in _MCP_MANIFEST_BASENAMES:
        servers = document.get("mcpServers") if isinstance(document, dict) else None
        if isinstance(servers, dict):
            return {"mcpServers": servers}, ()
        if servers in (None, "", {}) or isinstance(servers, str):
            return None
        raise SkillError(
            f"{label} mcpServers must be an object of servers or a relative file path, not {type(servers).__name__}"
        )
    return document, ()


def _manifest_referenced_mcp_files(package_root: Path) -> set[Path]:
    """Files named by a manifest `"mcpServers": "./relative.json"` anywhere in the tree."""
    referenced: set[Path] = set()
    for manifest_path in package_root.rglob("*"):
        if manifest_path.name.casefold() not in _MCP_MANIFEST_BASENAMES or not manifest_path.is_file():
            continue
        _assert_exact_reserved_spelling(manifest_path)
        document = _load_json_document_bytes(manifest_path, label=manifest_path.name)
        target = document.get("mcpServers") if isinstance(document, dict) else None
        if not isinstance(target, (str, dict)) and target not in (None, ""):
            raise SkillError(
                f"{manifest_path.name} mcpServers must be an object of servers or a relative file path, "
                f"not {type(target).__name__}"
            )
        if isinstance(target, str) and target.strip():
            clean = target.strip().replace("\\", "/")
            clean = clean[2:] if clean.startswith("./") else clean
            # A vendor manifest (`.claude-plugin/plugin.json`) references files
            # relative to its PLUGIN root (the normalizer resolves it that way);
            # any other manifest relative to its own directory.
            bases = [manifest_path.parent]
            if manifest_path.parent.name in {".claude-plugin", ".codex-plugin", ".cursor-plugin"}:
                bases.insert(0, manifest_path.parent.parent)
            for base in bases:
                candidate = base / clean
                try:
                    resolved = Path(os.path.abspath(candidate))
                    resolved.relative_to(Path(os.path.abspath(package_root)))
                except ValueError:
                    continue
                if resolved.is_file() and not is_link_or_reparse(resolved):
                    referenced.add(resolved)
                    break
    return referenced


def scan_mcp_tree(package_root: Path) -> list[tuple[Path, list[LeafPath]]]:
    """INDEPENDENT credential gate over the whole normalized tree.

    Every JSON file named mcp.json / .mcp.json / plugin.json /
    gemini-extension.json at ANY depth (vendor subtree included) plus every
    manifest-referenced `mcpServers` file is walked with `secret_leaves()`,
    whatever branch of the normalizer produced the tree. Returns
    (file, flagged leaf paths) for every file with a hit.
    """
    files: set[Path] = set()
    for path in package_root.rglob("*"):
        if is_link_or_reparse(path):
            if path.name.casefold() in _MCP_TREE_BASENAMES:
                raise SkillError(f"Package symlink is not allowed: {path.name}")
            continue
        if path.is_file() and path.name.casefold() in _MCP_TREE_BASENAMES:
            _assert_exact_reserved_spelling(path)
            files.add(Path(os.path.abspath(path)))
    referenced = _manifest_referenced_mcp_files(package_root)
    hits: list[tuple[Path, list[LeafPath]]] = []
    for path in sorted(files | referenced):
        scored = _mcp_document_for_scan(path, referenced=path in referenced)
        if scored is None:
            continue
        document, _prefix = scored
        flagged = secret_leaves(document)
        if flagged:
            hits.append((path, flagged))
    return hits


def sanitize_mcp_tree(package_root: Path) -> list[tuple[str, str, str]]:
    """Blank every flagged leaf in place (structure preserved) and disclose it
    as a `names-only` degradation per file. Manifests are rewritten with only
    their `mcpServers` subtree touched."""
    notes: list[tuple[str, str, str]] = []
    referenced = _manifest_referenced_mcp_files(package_root)
    for path, flagged in scan_mcp_tree(package_root):
        document = _load_json_document_bytes(path, label=path.name)
        if path not in referenced and path.name.casefold() in _MCP_MANIFEST_BASENAMES:
            sanitized = dict(document)
            sanitized["mcpServers"] = sanitize_native_mcp({"mcpServers": document["mcpServers"]}, flagged)["mcpServers"]
        else:
            sanitized = sanitize_native_mcp(document, flagged)
        path.write_bytes(json.dumps(sanitized, indent=2).encode("utf-8") + b"\n")
        relative = path.relative_to(Path(os.path.abspath(package_root))).as_posix()
        notes.append(
            (
                "portable-mcp",
                f"{relative}: secret-like values stripped at " + ", ".join(_leaf_key_path(item) for item in flagged[:12])
                + (" ..." if len(flagged) > 12 else ""),
                "names-only",
            )
        )
    return notes


def sanitize_native_mcp(value: Any, stripped: list[LeafPath]) -> Any:
    """Return a deep copy of a native MCP document with every stripped leaf
    blanked in place (structure, key order, references and placeholders
    preserved) so the vendor copy carries exactly the `names-only` promise the
    disclosure makes - wherever in the document the leaf lives."""
    import copy

    document = copy.deepcopy(value)
    for path in stripped:
        node = document
        for part in path[:-1]:
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node):
                node = node[part]
            else:
                node = None
                break
        if node is None:
            continue
        last = path[-1]
        if isinstance(node, dict) and last in node and isinstance(node[last], str):
            node[last] = ""
        elif isinstance(node, list) and isinstance(last, int) and 0 <= last < len(node) and isinstance(node[last], str):
            node[last] = ""
    return document


def translate_native_mcp_detailed(
    value: dict[str, Any], *, origin: str
) -> tuple[dict[str, Any], list[tuple[str, str, str]], list[LeafPath]]:
    """Translate a vendor `mcpServers` map to the Agent Plugins mcp.json shape.

    stdio servers keep command/args and the env NAMES only (values are
    stripped: they are frequently secrets and must be supplied locally);
    remote servers keep the URL with a declared transport. Anything that does
    not translate is dropped and recorded as a Degradation. The untouched
    original always survives in the vendor subtree.
    """
    servers_raw = value.get("mcpServers")
    wrapped = True
    if not isinstance(servers_raw, dict):
        # Claude Code also accepts a BARE server map (`.mcp.json` whose
        # top-level values are server objects with `command` / `url` /
        # `httpUrl`), e.g. the official laravel-boost and gitlab plugins.
        if value and all(
            isinstance(server, dict)
            and any(isinstance(server.get(key), str) for key in ("command", "url", "httpUrl"))
            for server in value.values()
        ) and "$schema" not in value:
            servers_raw = value
            wrapped = False
        else:
            raise SkillError(f"{origin} MCP configuration must contain an mcpServers object")
    degradations: list[tuple[str, str, str]] = []
    # EVERY secret-like string leaf anywhere in the document is stripped from
    # the vendor copy - translated, dropped or unknown alike (R23 P1-1).
    stripped_values: list[LeafPath] = secret_leaves(value)
    stripped_set = set(stripped_values)

    def leaf_path(*parts: Any) -> LeafPath:
        return (("mcpServers",) if wrapped else ()) + tuple(parts)

    # Disclosures are grouped per server and field so a reader sees WHAT was
    # blanked (names / positions), never the values.
    prefix_len = 1 if wrapped else 0
    grouped: dict[tuple[str, str], list[str]] = {}
    for path in stripped_values:
        rest = path[prefix_len:]
        server_name = str(rest[0]) if rest else "?"
        field = str(rest[1]) if len(rest) >= 2 else ""
        if field in {"env", "headers"} and len(rest) == 3:
            grouped.setdefault((server_name, field), []).append(str(rest[2]))
        elif field == "args" and len(rest) == 3:
            grouped.setdefault((server_name, "args"), []).append(str(rest[2]))
        else:
            grouped.setdefault((server_name, "other"), []).append(".".join(str(part) for part in rest[1:]))
    # Non-string env values are blanked too (not portable): disclose them with the env names.
    for server_name, raw_server in servers_raw.items():
        if isinstance(raw_server, dict) and isinstance(raw_server.get("env"), dict):
            for key, item in raw_server["env"].items():
                if isinstance(key, str) and not isinstance(item, str) and item not in (None, ""):
                    grouped.setdefault((str(server_name), "env"), []).append(key)
    for (server_name, field), names in sorted(grouped.items()):
        names = sorted(set(names), key=lambda item: (len(item), item)) if field == "args" else sorted(set(names))
        if field == "env":
            reason = f"{server_name}: secret-like env values stripped ({', '.join(names)})"
        elif field == "headers":
            reason = f"{server_name}: secret-like header values stripped ({', '.join(names)})"
        elif field == "args":
            reason = f"{server_name}: secret-like argument value(s) stripped at position(s) {', '.join(names)}"
        else:
            reason = f"{server_name}: secret-like values stripped at {', '.join(names)}"
        degradations.append(("portable-mcp", reason, "names-only"))

    servers: dict[str, Any] = {}
    for name, raw in servers_raw.items():
        if not isinstance(name, str) or not name or not isinstance(raw, dict):
            degradations.append(("portable-mcp", f"server entry {name!r} is not an object", "dropped"))
            continue
        # Gemini declares streamable-HTTP as `httpUrl` (and SSE as `url`).
        http_url = raw.get("httpUrl")
        url = raw.get("url")
        command = raw.get("command")
        declared = str(raw.get("type") or raw.get("transport") or "").lower()
        if isinstance(http_url, str) and http_url:
            url, declared = http_url, "streamable-http"
        if isinstance(url, str) and url:
            _validate_mcp_url(url, label=f"{origin} mcp server {name!r}")
            transport = "sse" if declared == "sse" else "streamable-http"
            if declared not in {"", "http", "sse", "streamable-http"}:
                degradations.append(
                    ("portable-mcp", f"{name}: unknown transport {declared!r}", "assumed streamable-http")
                )
            entry: dict[str, Any] = {"type": transport, "url": url}
            headers = raw.get("headers")
            if isinstance(headers, dict) and headers:
                portable_headers: dict[str, str] = {}
                for key in sorted(k for k in headers if isinstance(k, str)):
                    item = headers[key]
                    if isinstance(item, str) and leaf_path(name, "headers", key) not in stripped_set:
                        portable_headers[key] = item  # e.g. Accept, or `Bearer ${TOKEN}`
                    else:
                        portable_headers[key] = ""
                entry["headers"] = portable_headers
        elif isinstance(command, str) and command:
            entry = {"type": "stdio", "command": command}
            args = raw.get("args")
            if isinstance(args, list) and all(isinstance(item, str) for item in args):
                # Translation never raises for argv: secret-like elements are
                # blanked (disclosed above); specs, paths, references pass through.
                entry["args"] = [
                    "" if leaf_path(name, "args", index) in stripped_set else item
                    for index, item in enumerate(args)
                ]
            elif args is not None:
                degradations.append(("portable-mcp", f"{name}: args must be a string list", "dropped"))
            env = raw.get("env")
            if isinstance(env, dict) and env:
                portable_env: dict[str, str] = {}
                for key in sorted(k for k in env if isinstance(k, str)):
                    item = env[key]
                    if isinstance(item, str) and leaf_path(name, "env", key) not in stripped_set:
                        portable_env[key] = item  # safe literals and `${REF}` survive verbatim
                    else:
                        portable_env[key] = ""  # secret-like or non-string: name only
                entry["env"] = portable_env
        else:
            degradations.append(("portable-mcp", f"{name}: neither command nor url", "dropped"))
            continue
        for key in raw:
            if key not in {"type", "transport", "url", "httpUrl", "command", "args", "env", "headers"}:
                degradations.append(("portable-mcp", f"{name}: {key} is not portable", "dropped"))
        servers[name] = entry
    return {"$schema": AGENT_MCP_SCHEMA, "mcpServers": servers}, degradations, stripped_values


_SECRET_ENV_NAME_RE = re.compile(r"token|secret|key|password|passwd|credential|auth|cookie", re.IGNORECASE)


def _shannon_entropy(value: str) -> float:
    import math
    from collections import Counter

    counts = Counter(value)
    length = len(value)
    return -sum(count / length * math.log2(count / length) for count in counts.values())


# Plugin-root placeholders the vendor tools expand at runtime. They must
# survive validation and translation untouched; adapters that lack native
# support substitute their own package root at emit time.
KNOWN_PLUGIN_ROOT_PLACEHOLDERS = ("${CLAUDE_PLUGIN_ROOT}",)
_PLACEHOLDER_NATIVE_TOOLS = {"${CLAUDE_PLUGIN_ROOT}": {"claude-code"}}


def contains_plugin_root_placeholder(value: str) -> bool:
    return any(placeholder in value for placeholder in KNOWN_PLUGIN_ROOT_PLACEHOLDERS)


def substitute_plugin_root(value: str, *, package_root: Path, tool: str) -> str:
    """Resolve plugin-root placeholders for `tool`: preserved verbatim when the
    tool expands them natively (Claude Code), otherwise replaced with the
    installed package root."""
    result = value
    for placeholder in KNOWN_PLUGIN_ROOT_PLACEHOLDERS:
        if placeholder in result and tool not in _PLACEHOLDER_NATIVE_TOOLS.get(placeholder, set()):
            result = result.replace(placeholder, str(package_root))
    return result


# Opaque-token alphabet: base64 / base64url / hex material (`/`, `+`, `=`
# INCLUDED - an AWS secret key contains `/`). Non-secrets are excluded by
# SHAPE instead: paths, URLs, package specs, versions and slugs.
_OPAQUE_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=_-]{20,}$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_NPM_SCOPED_SPEC_RE = re.compile(r"^@[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(?:@[A-Za-z0-9.^~<>=*+-]+)?$")
_NAME_AT_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]+@[0-9][A-Za-z0-9.+-]*$")
_PIP_SPEC_RE = re.compile(r"^[A-Za-z0-9._-]+(?:\[[A-Za-z0-9,._-]+\])?(?:==|>=|<=|~=|!=|>|<)[0-9][A-Za-z0-9.*+-]*$")
_MIXED_RUN_RE = re.compile(r"[A-Za-z0-9+/=]{20,}")
# `user:password` basic-auth shape (the password part is credential-like).
# (user part >= 2 chars and no backslash in the password so `C:\Users\...`
# Windows drive paths never match)
_BASIC_AUTH_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}:(?P<password>[^\s:@\\]{8,})$")


def _has_mixed_case_and_digit(value: str) -> bool:
    return (
        any(char.islower() for char in value)
        and any(char.isupper() for char in value)
        and any(char.isdigit() for char in value)
    )


def _authority_has_credentials(value: str) -> bool:
    """`scheme://user:pass@host...` in any scheme: any `@` in the authority (or a
    malformed host:port produced by a `/` inside the password) is userinfo."""
    from urllib.parse import urlsplit

    if is_secret_reference(value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return True
    try:
        parsed.port
    except ValueError:
        return True
    return False


def _is_non_secret_shape(value: str) -> bool:
    """Paths, URLs, package specs, versions, CLI flags, plugin-root
    placeholders and content digests (git SHAs, `sha256:<hex>`) - never
    opaque tokens."""
    if value.startswith(("/", "./", "../", "~", "\\", "-")) or contains_plugin_root_placeholder(value):
        return True
    if _HEX_DIGEST_RE.fullmatch(value) or _PREFIXED_DIGEST_RE.fullmatch(value):
        return True
    if _SCHEME_RE.match(value):
        # A URL / DSN of ANY scheme (https, postgresql, mssql, ldap, ...) is a
        # non-secret shape only when its authority carries no credentials.
        return not _authority_has_credentials(value)
    if _NPM_SCOPED_SPEC_RE.fullmatch(value) or _NAME_AT_VERSION_RE.fullmatch(value) or _PIP_SPEC_RE.fullmatch(value):
        return True
    if "." in value:
        # A version / dotted path / file name unless it embeds a >= 20 char
        # mixed-case+digit run (`prefix.<opaque token>` style).
        return not any(_has_mixed_case_and_digit(run) for run in _MIXED_RUN_RE.findall(value))
    if "/" in value:
        # Relative path: word-like segments (`lib/node_modules/pkg1`,
        # `Users/alice/Projects/app2`) - lowercase/digit words or Capitalized
        # words; base64 material has case changes INSIDE a segment.
        segments = value.split("/")
        if all(
            len(seg) <= 24 and (re.fullmatch(r"[a-z0-9._-]*", seg) or re.fullmatch(r"[A-Z][a-z0-9._-]*", seg))
            for seg in segments
        ):
            return True
    return False


def _looks_opaque_token(value: str) -> bool:
    if not _OPAQUE_TOKEN_RE.fullmatch(value):
        return False
    if _is_non_secret_shape(value):
        return False
    if not (any(char.isalpha() for char in value) and any(char.isdigit() for char in value)):
        return False
    # Slugs (`server-github-2025-04-08`, `my_project_v2_build`) are short
    # word-like segments joined by separators; opaque material is not.
    segments = re.split(r"[-_]", value)
    if len(segments) >= 3 and max(len(segment) for segment in segments) < 16:
        return False
    return _shannon_entropy(value) >= 3.8


def _looks_basic_auth(value: str) -> bool:
    match = _BASIC_AUTH_RE.fullmatch(value)
    if match is None or _SCHEME_RE.match(value) or _PREFIXED_DIGEST_RE.fullmatch(value):
        return False
    password = match.group("password")
    return _has_mixed_case_and_digit(password) or _shannon_entropy(password) >= 3.5


_SECRET_REFERENCE_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}$|^\$[A-Za-z_][A-Za-z0-9_]*$")
_REFERENCE_TOKEN_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}|\$[A-Za-z_][A-Za-z0-9_]*")
# A literal prefix is a short word ending in a separator (`Bearer `, `token=`,
# `Basic `); bare material glued to a reference is NOT a prefix.
_LITERAL_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}[ =:]$")


def is_secret_reference(value: str) -> bool:
    """`${GITHUB_TOKEN}`, `${TOKEN:-default}`, `$TOKEN`, or a short literal
    prefix + reference (`Bearer ${TOKEN}`, `token=${TOKEN}`) - the documented
    way for publishers to ship credentials as RUNTIME references (design §7.5).
    A reference followed by secret-looking literal material is NOT a reference."""
    text = value.strip()
    if not text or "$" not in text:
        return False
    if _SECRET_REFERENCE_RE.fullmatch(text):
        return True
    references = list(_REFERENCE_TOKEN_RE.finditer(text))
    if not references:
        return False
    # Everything outside the references must be a short, plain literal prefix
    # (`Bearer `, `token=`) and nothing may FOLLOW the last reference.
    last = references[-1]
    if last.end() != len(text):
        return False
    prefix = text[: references[0].start()]
    if prefix and not _LITERAL_PREFIX_RE.fullmatch(prefix):
        return False
    between = [text[a.end():b.start()] for a, b in zip(references, references[1:])]
    return all(not gap or _LITERAL_PREFIX_RE.fullmatch(gap) for gap in between)


def _env_value_is_secret_like(key: str, value: str) -> bool:
    """Decide whether a native MCP env / header value must be stripped from the
    portable package. Three signals, any of which strips (and is disclosed as
    a `names-only` degradation):

    1. the variable NAME looks like a credential (token/secret/key/...),
    2. the DLP secret scanner recognizes the VALUE (AWS keys, GitHub tokens, ...),
    3. the VALUE has an opaque-token shape (>= 20 chars of base64/base64url/
       hex material with letters and digits and high entropy) or a
       `user:password` basic-auth shape. Paths, URLs, package specs,
       versions, slugs and plain literals are excluded by shape.
    """
    from sessionfs.security.secrets import scan_text

    if not value:
        return False
    if contains_plugin_root_placeholder(value):
        return False  # `${CLAUDE_PLUGIN_ROOT}/scripts/x` is a path template
    if is_secret_reference(value):
        return False  # a runtime reference is exactly what publishers should ship
    if _REFERENCE_TOKEN_RE.search(value):
        # A reference glued to other material: a path template (`${HOME}/data`)
        # is fine, anything else (`${TOKEN}abc123XYZ...`) is secret-looking.
        remainder = _REFERENCE_TOKEN_RE.sub("", value).strip()
        return not (not remainder or _is_non_secret_shape(remainder))
    if _SECRET_ENV_NAME_RE.search(key):
        return True
    if _SCHEME_RE.match(value) and _authority_has_credentials(value):
        return True  # `mssql://sa:<opaque>@db/app` under DATABASE_DSN, `https://u:p@h` in args
    if scan_text(value):
        return True
    return _looks_opaque_token(value) or _looks_basic_auth(value)


def _validate_mcp_url(url: str, *, label: str) -> None:
    """Portable MCP URLs never carry credentials: userinfo and secret-like
    query parameters are rejected (they would be cached, hashed and served)."""
    from urllib.parse import parse_qsl, urlsplit

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise SkillError(f"{label}: invalid MCP url") from exc
    # Any `@` in the authority is userinfo. (urlsplit reports username/password
    # as None when the password part contains `/` - the authority then parses
    # as host:port - so the raw netloc is what gets checked.)
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise SkillError(f"{label}: MCP url must not embed credentials (userinfo)")
    try:
        parsed.port  # `user:pa/ss@host` parses as host "user", port "pa" - malformed authority
    except ValueError as exc:
        raise SkillError(f"{label}: MCP url authority is not host[:port] (credentials must not be embedded)") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SkillError(f"{label}: MCP url must be an http(s) URL")
    # Per-user tokens travel in the PATH (Zapier: `/api/mcp/s/<token>/mcp` - the
    # docs say to treat the URL as a password) and in the FRAGMENT. Every path
    # segment and the fragment are scored with the opaque-token / DLP rules.
    # UUID segments (`8-4-4-4-12` hex) stay allowed by design: they are
    # resource identifiers, not credentials, and are excluded from the
    # opaque-token shape by their dashes.
    from sessionfs.security.secrets import scan_text

    for segment in parsed.path.split("/"):
        if segment and not is_secret_reference(segment) and (_looks_opaque_token(segment) or scan_text(segment)):
            raise SkillError(f"{label}: MCP url must not embed credentials (path segment looks like a token)")
    if parsed.fragment:
        fragment_values = [parsed.fragment, *[part.split("=", 1)[-1] for part in parsed.fragment.split("&")]]
        if any(
            not is_secret_reference(item) and (_looks_opaque_token(item) or scan_text(item))
            for item in fragment_values
            if item
        ):
            raise SkillError(f"{label}: MCP url must not embed credentials (fragment looks like a token)")
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if is_secret_reference(value):
            continue  # `?api_key=${SMITHERY_API_KEY}` is the documented runtime-reference form
        if _env_value_is_secret_like(key, value) or _SECRET_ENV_NAME_RE.search(key):
            raise SkillError(f"{label}: MCP url query parameter {key!r} looks like a credential")


_SECRET_OPTION_RE = re.compile(
    r"^--?(?:token|secret|key|api[-_]?key|password|passwd|pass|credential|auth|bearer)$",
    re.IGNORECASE,
)
# Bare `-p` is ambiguous (port / password): its value is only stripped when
# the value ITSELF looks secret-like.
_AMBIGUOUS_OPTION_RE = re.compile(r"^-p$")
_HEX_DIGEST_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)
_PREFIXED_DIGEST_RE = re.compile(r"^(?:sha256|sha512|sha1|md5):[0-9a-f]{32,128}$", re.IGNORECASE)


def _arg_is_secret_like(arg: str) -> bool:
    """`--token=sk_...` style values, DLP-recognized secrets and opaque
    token-shaped blobs (per element). Package specs, paths, URLs and versions
    never match (see `_looks_opaque_token`). For `--option=value` the VALUE
    is what gets scored, so `--allowed-dirs=/Users/alice/Projects/app2` is a
    path, not a token."""
    if contains_plugin_root_placeholder(arg) or is_secret_reference(arg):
        return False  # `${CLAUDE_PLUGIN_ROOT}/scripts/x` path template / `${TOKEN}` reference
    key, sep, value = arg.partition("=")
    if sep and key.startswith("-"):
        if is_secret_reference(value) or contains_plugin_root_placeholder(value):
            return False
        if _SECRET_ENV_NAME_RE.search(key.lstrip("-")) and value:
            return True
        return _env_value_is_secret_like("", value)
    return _env_value_is_secret_like("", arg)


def _secret_arg_indices(args: list[str]) -> list[int]:
    """Indices of argv elements that carry a credential.

    A secret-named option (`--password`, `--api-key`, `-p`, ...) CONSUMES the
    next element, so `["--password", "hunter2"]` flags the value even though
    it looks like nothing on its own; every element is also checked alone
    (`=`-form, DLP patterns, opaque-token shape).
    """
    flagged: list[int] = []
    for index, arg in enumerate(args):
        option = arg.partition("=")[0]
        has_value = "=" not in arg and index + 1 < len(args) and bool(args[index + 1])
        if (
            _SECRET_OPTION_RE.fullmatch(option)
            and has_value
            and not _is_non_secret_shape(args[index + 1])  # `--key ${CLAUDE_PLUGIN_ROOT}/certs/key.pem`
            and not is_secret_reference(args[index + 1])  # `--token ${TOKEN}`
        ):
            flagged.append(index + 1)
        elif (
            _AMBIGUOUS_OPTION_RE.fullmatch(option)
            and has_value
            and not args[index + 1].isdigit()
            and not _is_non_secret_shape(args[index + 1])
            and _env_value_is_secret_like("password", args[index + 1])
        ):
            flagged.append(index + 1)
        if _arg_is_secret_like(arg):
            flagged.append(index)
    return sorted(set(flagged))


def _validate_mcp_args(args: list[str], *, label: str) -> None:
    """Portable input: reject credentials passed as argv."""
    flagged = _secret_arg_indices(args)
    if flagged:
        first = args[flagged[0]]
        option = args[flagged[0] - 1] if flagged[0] > 0 and _SECRET_OPTION_RE.fullmatch(args[flagged[0] - 1].partition("=")[0]) else None
        if option is not None:
            raise SkillError(
                f"{label}: MCP argument {option!r} passes a credential; "
                "pass secrets via the environment at runtime"
            )
        raise SkillError(
            f"{label}: MCP argument looks like a credential; pass secrets via the environment at runtime"
            + ("" if len(first) > 64 else "")
        )


def _write_translated_mcp(
    native: dict[str, Any], package_root: Path, *, origin: str
) -> list[tuple[str, str, str]]:
    return _write_translated_mcp_detailed(native, package_root, origin=origin)[0]


def _write_translated_mcp_detailed(
    native: dict[str, Any], package_root: Path, *, origin: str
) -> tuple[list[tuple[str, str, str]], list[LeafPath]]:
    if native.get("$schema") == AGENT_MCP_SCHEMA:
        (package_root / "mcp.json").write_bytes(_canonical_json(native) + b"\n")
        return [], []
    portable, degradations, stripped = translate_native_mcp_detailed(native, origin=origin)
    (package_root / "mcp.json").write_bytes(_canonical_json(portable) + b"\n")
    return degradations, stripped


def _normalize_source(
    source_root: Path, package_root: Path
) -> tuple[dict[str, Any], str, list[tuple[str, str, str]]]:
    # Every reserved-name probe checks the EXACT directory-entry spelling so
    # validation is host-independent (P2-3).
    portable = _exact_file(source_root, "plugin.json")
    vendor_candidates: list[tuple[Path, str]] = []
    for vendor_dir_name, origin_name in (
        (".claude-plugin", "claude-plugin"),
        (".codex-plugin", "codex-plugin"),
        (".cursor-plugin", "cursor-plugin"),
    ):
        vendor_dir = _exact_entry(source_root, vendor_dir_name)
        if vendor_dir is None:
            continue
        vendor_manifest = _exact_file(vendor_dir, "plugin.json") if vendor_dir.is_dir() else None
        if vendor_manifest is not None:
            vendor_candidates.append((vendor_manifest, origin_name))
    if portable is not None:
        manifest = validate_plugin_manifest(portable)
        _copy_safe_tree(source_root, package_root)
        return manifest, "agent-plugin", []

    gemini_manifest = _exact_file(source_root, "gemini-extension.json")
    if gemini_manifest is not None and not vendor_candidates:
        return _normalize_gemini_extension(source_root, package_root, gemini_manifest)

    for vendor_path, origin in vendor_candidates:
        vendor = _load_json_object(vendor_path, label=str(vendor_path.relative_to(source_root)))
        manifest = _portable_manifest_from_vendor(vendor, source_root.name)
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "plugin.json").write_bytes(_canonical_json(manifest) + b"\n")
        skills_dir = source_root / "skills"
        if is_link_or_reparse(skills_dir):
            raise SkillError("Package symlink is not allowed: skills")
        if is_link_or_reparse(vendor_path.parent) or is_link_or_reparse(vendor_path):
            raise SkillError(f"Package symlink is not allowed: {vendor_path.parent.name}")
        if skills_dir.is_dir():
            _copy_safe_tree(skills_dir, package_root / "skills")
        degradations: list[tuple[str, str, str]] = []
        vendor_root = package_root / "io.sessionfs.portability" / "vendor" / origin
        # R22 P1-1: EVERY native MCP document the plugin declares is translated
        # and sanitized - the manifest's inline `mcpServers`, a manifest-
        # referenced file (`"mcpServers": "./servers.json"`), `mcp.json` AND
        # `.mcp.json`. Server names are merged into one portable mcp.json
        # (a collision is an error) and each vendor copy is written through
        # sanitize_native_mcp with its OWN stripped list.
        documents: list[tuple[str, dict[str, Any], Path | None]] = []  # (label, document, source file)
        seen_sources: set[Path] = set()
        # EVERY vendor manifest candidate (.claude-plugin, .codex-plugin,
        # .cursor-plugin) contributes its MCP documents; the first one is the
        # metadata source (R24 P1-1).
        secondary_manifest_stripped: dict[str, list[LeafPath]] = {}
        for candidate_path, _candidate_origin in vendor_candidates:
            candidate_vendor = vendor if candidate_path == vendor_path else _load_json_object(
                candidate_path, label=str(candidate_path.relative_to(source_root))
            )
            manifest_servers = candidate_vendor.get("mcpServers")
            candidate_label = f"{candidate_path.parent.name}/plugin.json"
            if isinstance(manifest_servers, dict) and manifest_servers:
                documents.append((f"{candidate_label}#mcpServers", {"mcpServers": manifest_servers}, None if candidate_path == vendor_path else candidate_path))
            elif isinstance(manifest_servers, str) and manifest_servers.strip():
                referenced = _exact_relative_file(source_root, manifest_servers, label=f"{candidate_label} mcpServers")
                referenced_relative = referenced.relative_to(source_root).as_posix()
                if PurePosixPath(referenced_relative).parts[0] == "skills":
                    # The skills/ tree is copied verbatim into the payload; an MCP
                    # document there could never be sanitized - refuse it.
                    raise SkillError(
                        f"{candidate_label} mcpServers must not reference a file inside skills/: {referenced_relative}"
                    )
                if referenced.resolve() not in seen_sources:
                    documents.append((referenced_relative, _load_json_object(referenced, label=manifest_servers), referenced))
                    seen_sources.add(referenced.resolve())
            elif manifest_servers not in (None, "", {}):
                # A list or other type is never silently ignored (it would ship untranslated).
                raise SkillError(
                    f"{candidate_label} mcpServers must be an object of servers or a relative file path, "
                    f"not {type(manifest_servers).__name__}"
                )
        for mcp_name in ("mcp.json", ".mcp.json"):
            mcp_source = _exact_file(source_root, mcp_name)
            if mcp_source is not None and mcp_source.resolve() not in seen_sources:
                # (a manifest reference to `./.mcp.json` is the same document:
                # deduplicated by resolved path, never a self-collision)
                seen_sources.add(mcp_source.resolve())
                documents.append((mcp_name, _load_json_object(mcp_source, label=mcp_name), mcp_source))
        merged_servers: dict[str, Any] = {}
        declared_in: dict[str, str] = {}
        sanitized_vendor: dict[str, bytes] = {}  # vendor-relative path -> sanitized bytes
        manifest_stripped: list[LeafPath] = []
        for label, document, source_file in documents:
            portable, doc_degradations, stripped = translate_native_mcp_detailed(document, origin=origin)
            degradations.extend(doc_degradations)
            for server_name, server in portable["mcpServers"].items():
                if server_name in merged_servers:
                    raise SkillError(
                        f"MCP server {server_name!r} is declared more than once "
                        f"({declared_in[server_name]} and {label}); remove the duplicate"
                    )
                merged_servers[server_name] = server
                declared_in[server_name] = label
            if source_file is None:
                manifest_stripped = stripped
            elif source_file.name == "plugin.json" and label.endswith("#mcpServers"):
                # A secondary vendor manifest with inline servers: its vendor
                # copy is rewritten with only the mcpServers subtree blanked.
                if stripped:
                    secondary_manifest_stripped[source_file.relative_to(source_root).as_posix()] = stripped
            elif stripped:
                sanitized_vendor[source_file.relative_to(source_root).as_posix()] = (
                    json.dumps(sanitize_native_mcp(document, stripped), indent=2).encode("utf-8") + b"\n"
                )
        if documents:
            (package_root / "mcp.json").write_bytes(
                _canonical_json({"$schema": AGENT_MCP_SCHEMA, "mcpServers": merged_servers}) + b"\n"
            )
        _copy_safe_tree(
            vendor_path.parent,
            vendor_root / vendor_path.parent.name,
        )
        if manifest_stripped:
            # The inline document was scored as {"mcpServers": ...}; the same
            # paths apply to the manifest itself.
            (vendor_root / vendor_path.parent.name / "plugin.json").write_bytes(
                json.dumps(sanitize_native_mcp(vendor, manifest_stripped), indent=2).encode("utf-8") + b"\n"
            )
        handled_files = {
            label for label, _doc, source_file in documents
            if source_file is not None and not label.endswith("#mcpServers")
        }
        for relative_manifest, stripped_paths in secondary_manifest_stripped.items():
            raw_manifest = _load_json_object(source_root / relative_manifest, label=relative_manifest)
            target = vendor_root / relative_manifest
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(json.dumps(sanitize_native_mcp(raw_manifest, stripped_paths), indent=2).encode("utf-8") + b"\n")
            handled_files.add(relative_manifest)
        for label, _document, source_file in documents:
            if source_file is None or label.endswith("#mcpServers"):
                continue
            target = vendor_root / label
            target.parent.mkdir(parents=True, exist_ok=True)
            if label in sanitized_vendor:
                target.write_bytes(sanitized_vendor[label])
            else:
                _copy_bounded_file(source_file, target, label=label)
        for child in source_root.iterdir():
            if child.name == "skills" or child == vendor_path.parent or child.name in handled_files:
                continue
            if is_link_or_reparse(child):
                raise SkillError(f"Package symlink is not allowed: {child.name}")
            if child.is_dir():
                _copy_safe_tree(child, vendor_root / child.name, skip=handled_files, skip_prefix=child.name)
            elif child.is_file():
                _copy_bounded_file(child, vendor_root / child.name, label=child.name)
        return manifest, origin, degradations

    if _exact_file(source_root, "SKILL.md") is not None:
        skill = validate_skill_dir(source_root, relative_dir="skills/pending")
        manifest = {
            "$schema": AGENT_PLUGIN_SCHEMA,
            "name": skill.name,
            "description": skill.description,
        }
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "plugin.json").write_bytes(_canonical_json(manifest) + b"\n")
        _copy_safe_tree(source_root, package_root / "skills" / skill.name)
        return manifest, "agent-skill", []

    raise SkillError(
        "Source must contain plugin.json, a supported vendor plugin manifest "
        "(.claude-plugin, .codex-plugin, .cursor-plugin, gemini-extension.json), or SKILL.md"
    )


_GEMINI_PORTABLE_FIELDS = ("version", "description", "homepage", "repository", "license", "keywords")


def _normalize_gemini_extension(
    source_root: Path, package_root: Path, manifest_path: Path
) -> tuple[dict[str, Any], str, list[tuple[str, str, str]]]:
    """Gemini CLI extension: `gemini-extension.json` + `skills/` + inline mcpServers.

    Portable metadata becomes plugin.json, skills copy as-is, the manifest's
    mcpServers map translates to portable mcp.json, and everything Gemini-
    specific (contextFileName, excludeTools, settings, commands, hooks, ...)
    is preserved untouched under the vendor subtree.
    """
    origin = "gemini-extension"
    if is_link_or_reparse(manifest_path):
        raise SkillError("Package symlink is not allowed: gemini-extension.json")
    vendor = _load_json_object(manifest_path, label="gemini-extension.json")
    manifest = _portable_manifest_from_vendor(vendor, source_root.name)
    package_root.mkdir(parents=True, exist_ok=True)
    (package_root / "plugin.json").write_bytes(_canonical_json(manifest) + b"\n")
    skills_dir = source_root / "skills"
    if is_link_or_reparse(skills_dir):
        raise SkillError("Package symlink is not allowed: skills")
    if skills_dir.is_dir():
        _copy_safe_tree(skills_dir, package_root / "skills")
    degradations: list[tuple[str, str, str]] = []
    stripped_mcp: list[LeafPath] = []
    if isinstance(vendor.get("mcpServers"), dict) and vendor["mcpServers"]:
        degradations, stripped_mcp = _write_translated_mcp_detailed(
            {"mcpServers": vendor["mcpServers"]}, package_root, origin=origin
        )
    elif vendor.get("mcpServers") not in (None, "", {}):
        raise SkillError("gemini-extension.json mcpServers must be an object of servers")
    for key in ("contextFileName", "excludeTools", "settings", "hooks", "commands"):
        if key in vendor:
            degradations.append(
                ("gemini-extension", f"{key} is Gemini-specific", "preserved in vendor subtree only")
            )
    vendor_root = package_root / "io.sessionfs.portability" / "vendor" / origin
    vendor_root.mkdir(parents=True, exist_ok=True)
    if stripped_mcp:
        (vendor_root / "gemini-extension.json").write_bytes(
            json.dumps(sanitize_native_mcp(vendor, stripped_mcp), indent=2).encode("utf-8") + b"\n"
        )
    else:
        shutil.copyfile(manifest_path, vendor_root / "gemini-extension.json")
    for child in source_root.iterdir():
        if child.name in {"skills", "gemini-extension.json"}:
            continue
        if is_link_or_reparse(child):
            raise SkillError(f"Package symlink is not allowed: {child.name}")
        if child.is_dir():
            _copy_safe_tree(child, vendor_root / child.name)
        elif child.is_file():
            _copy_bounded_file(child, vendor_root / child.name, label=child.name)
    return manifest, origin, degradations


def _validate_mcp(path: Path) -> bool:
    """Shared portable mcp.json validator (local import, translation output and
    hosted upload all pass through here): no credential may reach the cache."""
    if not path.is_file():
        return False
    value = _load_json_object(path, label="mcp.json")
    if value.get("$schema") != AGENT_MCP_SCHEMA or not isinstance(value.get("mcpServers"), dict):
        raise SkillError(f"mcp.json must target {AGENT_MCP_SCHEMA} and contain mcpServers")
    for name, server in value["mcpServers"].items():
        if not isinstance(server, dict):
            raise SkillError(f"mcp.json server {name!r} must be an object")
        label = f"mcp.json server {name!r}"
        url = server.get("url")
        if isinstance(url, str) and url:
            _validate_mcp_url(url, label=label)
        args = server.get("args")
        if isinstance(args, list):
            _validate_mcp_args([item for item in args if isinstance(item, str)], label=label)
    # Every string leaf at any depth (portable input is rejected, never stored).
    flagged = secret_leaves(value)
    if flagged:
        raise SkillError(
            "mcp.json carries a credential at "
            + ", ".join(_leaf_key_path(path) for path in flagged[:12])
            + "; portable packages ship names or ${REFERENCES} only (values are supplied at runtime)"
        )
    return True


_HOOK_FILE_NAMES = {"hooks.json"}
_MCP_FILE_NAMES = {"mcp.json", ".mcp.json"}


def _declared_root_risk(package_root: Path) -> tuple[bool, bool]:
    """Hooks / MCP declared at the package or vendor root rather than inside a
    skill directory: a manifest `hooks` (e.g. `"./hooks.json"`) or
    `mcpServers` key, or a root-level `hooks.json` / `hooks/` / `mcp.json`
    file. Returns (declares_mcp, declares_hooks)."""
    declares_mcp = False
    declares_hooks = False
    roots: list[Path] = [package_root]
    vendor_base = package_root / "io.sessionfs.portability" / "vendor"
    if vendor_base.is_dir():
        roots.extend(sorted(child for child in vendor_base.iterdir() if child.is_dir()))
    for root in roots:
        manifests = [root / "plugin.json", root / "gemini-extension.json"] + [
            root / vendor_dir / "plugin.json"
            for vendor_dir in (".claude-plugin", ".codex-plugin", ".cursor-plugin")
        ]
        for manifest_path in manifests:
            if not manifest_path.is_file() or is_link_or_reparse(manifest_path):
                continue
            try:
                manifest = _load_json_object(manifest_path, label=manifest_path.name)
            except SkillError:
                continue
            if manifest.get("hooks") not in (None, "", [], {}):
                declares_hooks = True
            if manifest.get("mcpServers") not in (None, "", [], {}):
                declares_mcp = True
        for child in root.iterdir() if root.is_dir() else []:
            name = child.name.casefold()
            if child.is_file() and name in _MCP_FILE_NAMES and child.name not in _MCP_FILE_NAMES:
                _assert_exact_reserved_spelling(child)
            if name in _HOOK_FILE_NAMES and child.is_file():
                declares_hooks = True
            elif name in _EXECUTABLE_DIRS and child.is_dir():
                declares_hooks = True
            elif name in _MCP_FILE_NAMES and child.is_file():
                declares_mcp = True
    return declares_mcp, declares_hooks


def _compute_risk(package_root: Path) -> tuple[bool, bool]:
    """Byte-derived risk flags (design §7.2): never trusted from an envelope."""
    has_mcp = _validate_mcp(package_root / "mcp.json")
    has_executable_extensions = any(
        part.casefold() in _EXECUTABLE_DIRS
        for _, relative, _ in _safe_files(package_root)
        if PurePosixPath(relative).parts[0].casefold() not in {"skills"}
        for part in PurePosixPath(relative).parts
    )
    declared_mcp, declared_hooks = _declared_root_risk(package_root)
    return has_mcp or declared_mcp, has_executable_extensions or declared_hooks


def _aggregate_integrity(package_root: Path) -> tuple[str, dict[str, str]]:
    records: dict[str, str] = {}
    aggregate = hashlib.sha256()
    for path, relative, _ in _safe_files(package_root):
        digest = _sha256(path.read_bytes())
        records[relative] = digest
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
    return "sha256:" + aggregate.hexdigest(), records


def _package_id(source: SourceIdentity, name: str, content_hash: str | None = None) -> str:
    """Stable package identity.

    Remote and project-relative sources are identified by their locator; a
    plain LOCAL directory carries no declared identity (its absolute path is
    machine-specific), so it is content-bound: the same bytes under the same
    declared name yield the same id on every machine.
    """
    if source.source_type == "local":
        identity: dict[str, Any] = {"name": name, "content_hash": content_hash, "local": True}
    else:
        identity = {
            "locator": source.locator,
            "subdir": source.subdir,
            "name": name,
            "registry_scope": source.registry_scope,
            "registry_id": source.registry_id,
        }
    return "skp_" + hashlib.sha256(_canonical_json(identity)).hexdigest()[:24]


def local_package_id(name: str, content_hash: str) -> str:
    """Public helper: content-bound id for a local package (rollback lookups)."""
    return _package_id(SourceIdentity(source_type="local", locator=""), name, content_hash)


ENVELOPE_VERSION = 2


def _build_envelope(
    *,
    package_id: str,
    manifest: dict[str, Any],
    content_hash: str,
    source: SourceIdentity,
    origin_format: str,
    degradations: list[tuple[str, str, str]] | list[list[str]],
    skills: list[SkillMetadata],
    has_mcp: bool,
    has_executable_extensions: bool,
    dlp_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "sfsk_version": 1,
        "envelope_version": ENVELOPE_VERSION,
        "dlp_findings": [dict(item) for item in (dlp_findings or [])],
        "package_id": package_id,
        "name": manifest["name"],
        "version": manifest.get("version"),
        "description": manifest.get("description", ""),
        "content_hash": content_hash,
        "source": source.as_dict(),
        "origin_format": origin_format,
        "degradations": [list(item) for item in degradations],
        "risk": {
            "scripts": any(item.has_scripts for item in skills),
            "mcp": has_mcp,
            "executable_extensions": has_executable_extensions,
            "hooks": any(item.has_hooks for item in skills),
            "skill_hook_extensions": any(item.has_hook_extensions for item in skills),
            "allowed_tools": sorted({tool for item in skills for tool in item.allowed_tools}),
        },
    }


def _validated_skills(package_root: Path) -> list[SkillMetadata]:
    return _validated_skills_with_notes(package_root)[0]


def _validated_skills_with_notes(
    package_root: Path,
) -> tuple[list[SkillMetadata], list[tuple[str, str, str]]]:
    """Validate every skill directory.

    A skill dir whose ONLY manifest is a casefold-equivalent spelling
    (`skill.md`) is OMITTED with a disclosed degradation instead of failing
    the whole package (official `newrelic` ships one next to three proper
    SKILL.md); a dir holding BOTH spellings is still a collision error, and
    reserved ROOT files keep their exact-spelling hard error.
    """
    skills: list[SkillMetadata] = []
    notes: list[tuple[str, str, str]] = []
    skills_root = package_root / "skills"
    if skills_root.exists() and not skills_root.is_dir():
        raise SkillError("skills must be a directory")
    if skills_root.is_dir():
        for child in sorted(skills_root.iterdir(), key=lambda item: item.name):
            if is_link_or_reparse(child) or not child.is_dir():
                continue
            try:
                manifest = _exact_file(child, "SKILL.md")
            except SkillError as exc:
                if "must be spelled exactly" not in str(exc):
                    raise
                found = re.search(r"found '([^']*)'", str(exc))
                spelling = found.group(1) if found else "skill.md"
                notes.append(
                    (
                        "skills",
                        f"skills/{child.name}: {spelling} is not spelled SKILL.md; skipped "
                        "(would not load on case-sensitive filesystems)",
                        "skipped",
                    )
                )
                continue
            if manifest is not None:
                skills.append(validate_skill_dir(child, relative_dir=f"skills/{child.name}"))
    return skills, notes


def refresh_envelope(version_dir: Path) -> bool:
    """Rewrite a stale-but-valid envelope from its freshly validated payload.

    Returns True when the envelope was rewritten. Identity (package id, content
    hash, integrity manifest) must already verify; only non-identity metadata
    (envelope_version, risk shape, degradations, name/description) is refreshed.
    The stored source (first-import provenance) and origin_format are kept.
    """
    sfsk = _load_json_object(version_dir / "sfsk.json", label="sfsk.json")
    package_root = version_dir / "package"
    content_hash, file_hashes = _aggregate_integrity(package_root)
    if content_hash != sfsk.get("content_hash"):
        raise SkillError(f"Package integrity mismatch for {sfsk.get('package_id', version_dir.name)}")
    source_raw = sfsk.get("source")
    if not isinstance(source_raw, dict):
        raise SkillError("sfsk.json source must be an object")
    try:
        source = SourceIdentity(**source_raw)
    except TypeError as exc:
        raise SkillError("sfsk.json source has invalid fields") from exc
    manifest = validate_plugin_manifest(package_root / "plugin.json")
    skills = _validated_skills(package_root)
    has_mcp, has_executable_extensions = _compute_risk(package_root)
    raw_degradations = sfsk.get("degradations")
    degradations = (
        [item for item in raw_degradations if isinstance(item, list) and len(item) == 3]
        if isinstance(raw_degradations, list)
        else []
    )
    dlp_findings = scan_package_for_dlp(package_root)
    fresh = _build_envelope(
        package_id=str(sfsk.get("package_id")),
        manifest=manifest,
        content_hash=content_hash,
        source=source,
        origin_format=str(sfsk.get("origin_format", "agent-plugin")),
        degradations=degradations,
        skills=skills,
        has_mcp=has_mcp,
        has_executable_extensions=has_executable_extensions,
        dlp_findings=dlp_findings,
    )
    if fresh == sfsk:
        return False
    tmp = version_dir / f".sfsk.json.{os.getpid()}.tmp"
    tmp.write_bytes(_canonical_json(fresh) + b"\n")
    os.replace(tmp, version_dir / "sfsk.json")
    tmp_integrity = version_dir / f".integrity.json.{os.getpid()}.tmp"
    tmp_integrity.write_bytes(
        _canonical_json({"content_hash": content_hash, "files": file_hashes}) + b"\n"
    )
    os.replace(tmp_integrity, version_dir / "integrity.json")
    return True


def envelope_is_stale(version_dir: Path) -> bool:
    """True when the envelope predates ENVELOPE_VERSION or its metadata drifted."""
    sfsk = _load_json_object(version_dir / "sfsk.json", label="sfsk.json")
    if sfsk.get("envelope_version") != ENVELOPE_VERSION:
        return True
    package_root = version_dir / "package"
    source_raw = sfsk.get("source")
    if not isinstance(source_raw, dict):
        return True
    try:
        source = SourceIdentity(**source_raw)
    except TypeError:
        return True
    manifest = validate_plugin_manifest(package_root / "plugin.json")
    skills = _validated_skills(package_root)
    has_mcp, has_executable_extensions = _compute_risk(package_root)
    raw_degradations = sfsk.get("degradations")
    degradations = (
        [item for item in raw_degradations if isinstance(item, list) and len(item) == 3]
        if isinstance(raw_degradations, list)
        else []
    )
    dlp_findings = scan_package_for_dlp(package_root)
    fresh = _build_envelope(
        package_id=str(sfsk.get("package_id")),
        manifest=manifest,
        content_hash=str(sfsk.get("content_hash")),
        source=source,
        origin_format=str(sfsk.get("origin_format", "agent-plugin")),
        degradations=degradations,
        skills=skills,
        has_mcp=has_mcp,
        has_executable_extensions=has_executable_extensions,
        dlp_findings=dlp_findings,
    )
    return fresh != sfsk


def evict_version(version_dir: Path) -> None:
    """Remove an invalid cached version so it is re-materialized on demand."""
    if len(version_dir.parents) < 3:
        raise SkillError(f"Package version directory is invalid: {version_dir}")
    _assert_store_path(version_dir.parents[2], version_dir)
    if is_link_or_reparse(version_dir):
        raise SkillError(f"Package version directory is invalid: {version_dir}")
    shutil.rmtree(version_dir, ignore_errors=True)


def import_package(
    source_root: Path,
    store_root: Path,
    source: SourceIdentity,
    *,
    allow_dlp_findings: bool = False,
    dlp_scan: bool = True,
) -> PackageRecord:
    """Normalize, validate, hash, and persist one immutable package version.

    Content-addressed cache: an existing version with a verified payload is
    reused; a stale envelope (older ENVELOPE_VERSION / drifted metadata) is
    transparently rewritten from the freshly validated payload; a version
    whose integrity does not verify is evicted and re-materialized.

    Every text member is DLP-scanned (same predicate + patterns as the hosted
    gate). Findings never block the import: they are recorded redacted on the
    record/envelope and surface as the `dlp-findings` consent requirement at
    install (`--allow-dlp-findings`, honored for every source type; `--yes`
    alone never satisfies it). `allow_dlp_findings` is accepted for API
    compatibility. `dlp_scan=False` is for callers that run their own
    policy-aware scan (the hosted gate).
    """
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise SkillError(f"Source directory does not exist: {source_root}")
    store_root = Path(os.path.abspath(store_root))
    store_root.mkdir(parents=True, exist_ok=True)
    _assert_store_path(store_root, store_root)
    with tempfile.TemporaryDirectory(prefix="sfs-skill-import-") as tmp:
        package_root = Path(tmp) / "package"
        manifest, origin_format, degradations = _normalize_source(source_root, package_root)
        manifest = validate_plugin_manifest(package_root / "plugin.json")
        # Client-side DLP: findings are RECORDED (redacted) and become the
        # `dlp-findings` consent requirement at install time - import is the
        # consumer's risk decision. The hosted publish gate stays a hard block.
        # A PORTABLE mcp.json at the package root is rejected outright when it
        # carries credentials (never stored, never blanked) ...
        _validate_mcp(package_root / "mcp.json")
        # ... then, independent of which branch normalized the tree, every
        # other MCP document anywhere in it (vendor subtree included) is
        # credential-walked and blanked, and the disclosure feeds the hosted
        # 422 (R24 P1-1).
        tree_notes = sanitize_mcp_tree(package_root)
        seen_reasons = {reason for _, reason, _ in degradations}
        degradations = [*degradations, *[note for note in tree_notes if note[1] not in seen_reasons]]
        dlp_findings = scan_package_for_dlp(package_root) if dlp_scan else []
        skills, skill_notes = _validated_skills_with_notes(package_root)
        degradations = [*degradations, *skill_notes]
        has_mcp, has_executable_extensions = _compute_risk(package_root)
        content_hash, file_hashes = _aggregate_integrity(package_root)
        package_id = _package_id(source, manifest["name"], content_hash)
        version_dir = store_root / "packages" / package_id / content_hash.removeprefix("sha256:")
        _assert_store_path(store_root, version_dir)
        final_package_root = version_dir / "package"
        if final_package_root.exists():
            try:
                cached = load_package_record(version_dir)
                identity_ok = (
                    cached.package_id == package_id and cached.content_hash == content_hash
                )
            except SkillError:
                identity_ok = False
                cached = None
            if cached is None or not identity_ok:
                # Corrupt / mismatched cache entry: evict and re-materialize.
                evict_version(version_dir)
            elif envelope_is_stale(version_dir):
                refresh_envelope(version_dir)
        if not final_package_root.exists():
            version_dir.parent.mkdir(parents=True, exist_ok=True)
            staging = version_dir.with_name(
                version_dir.name + f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
            )
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            shutil.copytree(package_root, staging / "package")
            sfsk = _build_envelope(
                package_id=package_id,
                manifest=manifest,
                content_hash=content_hash,
                source=source,
                origin_format=origin_format,
                degradations=degradations,
                skills=skills,
                has_mcp=has_mcp,
                has_executable_extensions=has_executable_extensions,
                dlp_findings=dlp_findings,
            )
            (staging / "sfsk.json").write_bytes(_canonical_json(sfsk) + b"\n")
            (staging / "integrity.json").write_bytes(
                _canonical_json({"content_hash": content_hash, "files": file_hashes}) + b"\n"
            )
            try:
                os.replace(staging, version_dir)
            except OSError:
                if not version_dir.exists():
                    raise
                shutil.rmtree(staging, ignore_errors=True)
        stored = load_package_record(version_dir)

        def _identity(value: SourceIdentity) -> SourceIdentity:
            # The commit is provenance, not identity (identical bytes at a later
            # commit reuse the cached version); a plain local directory's
            # absolute path is machine-specific provenance as well; the
            # catalog alias and listing name are DISPLAY provenance (a
            # rename-only marketplace commit with identical bytes must reuse
            # the cached version, not fail as a metadata mismatch).
            stripped = _replace(value, commit=None, registry_alias=None, listing_name=None)
            if value.source_type == "local":
                return _replace(stripped, locator="")
            return stripped

        if (
            stored.package_id != package_id
            or stored.content_hash != content_hash
            or _identity(stored.source) != _identity(source)
        ):
            raise SkillError(f"Stored package metadata mismatch for {package_id}")
        # Content-addressed reuse: the immutable envelope keeps the provenance
        # it was first imported with; the returned record (and hence the lock)
        # carries the source that was actually resolved now.
        if stored.source != source:
            return _replace(stored, source=source)
        return stored


def load_package_record(
    version_dir: Path, *, source: SourceIdentity | None = None
) -> PackageRecord:
    """Load and revalidate an immutable package from the local content store.

    `source` is the AUTHORITATIVE provenance from the installed state/lock row
    (e.g. the later commit a byte-identical version was resolved at); the
    envelope's own source is only the fallback when no row is supplied.
    """
    if len(version_dir.parents) < 3:
        raise SkillError(f"Package version directory is invalid: {version_dir}")
    _assert_store_path(version_dir.parents[2], version_dir)
    if is_link_or_reparse(version_dir) or not version_dir.is_dir():
        raise SkillError(f"Package version directory is invalid: {version_dir}")
    sfsk = _load_json_object(version_dir / "sfsk.json", label="sfsk.json")
    package_root = version_dir / "package"
    if is_link_or_reparse(package_root) or not package_root.is_dir():
        raise SkillError(f"Package payload directory is invalid: {package_root}")
    content_hash, file_hashes = _aggregate_integrity(package_root)
    if content_hash != sfsk.get("content_hash"):
        raise SkillError(f"Package integrity mismatch for {sfsk.get('package_id', version_dir.name)}")
    integrity = _load_json_object(version_dir / "integrity.json", label="integrity.json")
    if integrity.get("content_hash") != content_hash or integrity.get("files") != file_hashes:
        raise SkillError(f"Package integrity manifest mismatch for {version_dir.parent.name}")
    expected_digest = content_hash.removeprefix("sha256:")
    if version_dir.name != expected_digest or sfsk.get("package_id") != version_dir.parent.name:
        raise SkillError("Package store path does not match its immutable identity")
    source_raw = sfsk.get("source")
    if not isinstance(source_raw, dict):
        raise SkillError("sfsk.json source must be an object")
    try:
        envelope_source = SourceIdentity(**source_raw)
    except TypeError as exc:
        raise SkillError("sfsk.json source has invalid fields") from exc
    if source is None:
        source = envelope_source
    elif source.source_type != envelope_source.source_type or (
        source.source_type != "local" and source.locator != envelope_source.locator
    ):
        raise SkillError("Installed package provenance does not match its cached envelope")
    manifest = validate_plugin_manifest(package_root / "plugin.json")
    skills = _validated_skills(package_root)
    # sfsk.json's risk section is informational only: it is outside the content
    # hash, so recompute every flag from the validated payload bytes.
    has_mcp, has_executable_extensions = _compute_risk(package_root)
    degradations_raw = sfsk.get("degradations")
    degradations: list[tuple[str, str, str]] = []
    if isinstance(degradations_raw, list):
        for item in degradations_raw:
            if isinstance(item, list) and len(item) == 3 and all(isinstance(part, str) for part in item):
                degradations.append((item[0], item[1], item[2]))
    return PackageRecord(
        package_id=str(sfsk["package_id"]),
        name=str(manifest["name"]),
        version=manifest.get("version"),
        description=str(manifest.get("description", "")),
        content_hash=content_hash,
        source=source,
        package_root=package_root,
        skills=tuple(skills),
        has_mcp=has_mcp,
        has_executable_extensions=has_executable_extensions,
        origin_format=str(sfsk.get("origin_format", "agent-plugin")),
        degradations=tuple(degradations),
        # Byte-derived like the risk flags: never trusted from the envelope.
        dlp_findings=tuple(scan_package_for_dlp(package_root)),
    )
