"""Deterministic per-tool Agent Skill emission plans."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sessionfs.skills.models import (
    SUPPORTED_TOOLS,
    Degradation,
    EmitPlan,
    Emission,
    Omission,
    PackageRecord,
    SkillError,
    SkillMetadata,
)
from sessionfs.skills.package import (
    KNOWN_PLUGIN_ROOT_PLACEHOLDERS,
    MAX_PATH_DEPTH,
    is_executable_reference,
    is_text_member,
    substitute_plugin_root,
)
from sessionfs.skills.path_safety import is_link_or_reparse

PROJECT_SKILL_ROOTS = {
    "claude-code": ".claude/skills",
    "codex": ".codex/skills",
    "gemini": ".gemini/skills",
    "cursor": ".agents/skills",
    "copilot": ".github/skills",
    "amp": ".agents/skills",
    "cline": ".cline/skills",
    "roo-code": ".roo/skills",
    "kilo-code": ".kilo/skills",
}

USER_SKILL_ROOTS = {
    "claude-code": ".claude/skills",
    "codex": ".codex/skills",
    "gemini": ".gemini/skills",
    "cursor": ".cursor/skills",
    "copilot": ".copilot/skills",
    "amp": ".config/amp/skills",
    "cline": ".cline/skills",
    "roo-code": ".roo/skills",
    "kilo-code": ".kilo/skills",
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _plan_hash(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(raw)


# A plugin-root reference is the placeholder plus a package path. That path
# grammar is WIDE (`_validate_portable_relative_path`): spaces, Unicode, `$`,
# quotes and parentheses are all legal in a package member name, so a
# character class can never say where a reference ends. A DELIVERED asset is
# therefore located by longest-match against the layout this target actually
# receives; the character scan below only bounds a reference that resolves to
# nothing, and only so the disclosure marker can name it.
_PLACEHOLDER_PATTERNS = {
    placeholder: re.compile(re.escape(placeholder))
    for placeholder in KNOWN_PLUGIN_ROOT_PLACEHOLDERS
}
# Never legal inside a validated package path, plus the punctuation
# instruction text uses to CLOSE a reference. (`?`, `:` and `>` are already in
# the illegal set, so they always close a reference.)
_ILLEGAL_PATH_CHARS = frozenset('<>:"|?*')
_REFERENCE_CLOSERS = frozenset("`'\")]},;")
# LEGAL path characters that also end a sentence. After a matched reference a
# run of them is punctuation, never part of the name: a delivered name that
# really contains one (`done!.md`, `alert!`) is consumed first by longest-match.
_SENTENCE_TRAILERS = ".!"
_MAX_REFERENCE_BYTES = 512
# Substituted in place of a reference whose target is NOT part of what this
# target tool actually receives. Deliberately not a path: an unresolvable
# reference must never become a runnable location (and never the cache root).
_UNAVAILABLE = "<sessionfs-unavailable{suffix}>"


def _is_reference_delimiter(char: str) -> bool:
    return (
        char.isspace()
        or char in _ILLEGAL_PATH_CHARS
        or char in _REFERENCE_CLOSERS
        or ord(char) < 32
        or ord(char) == 127
    )


@dataclass
class _PathNode:
    """One path segment of a layout, for longest-prefix reference matching."""

    children: dict[str, _PathNode] = field(default_factory=dict)
    terminal: bool = False
    value: tuple[Path, bool] | None = None


def _build_path_trie(entries: dict[str, tuple[Path, bool] | None]) -> _PathNode:
    root = _PathNode()
    for key in sorted(entries):
        node = root
        for segment in (part for part in key.split("/") if part):
            node = node.children.setdefault(segment, _PathNode())
        node.terminal = True
        node.value = entries[key]
    return root


def _match_longest(node: _PathNode, rest: str) -> tuple[int, _PathNode] | None:
    """Longest prefix of `rest` that names an entry of this layout.

    `rest` is the reference text after a separator, with `\\` already folded to
    `/` (a validated package path can never contain a backslash), so the
    consumed length is also the length consumed in the ORIGINAL text.
    """
    best: tuple[int, _PathNode] | None = None
    for name, child in node.children.items():
        if not rest.startswith(name):
            continue
        consumed = len(name)
        if child.terminal and (best is None or consumed > best[0]):
            best = (consumed, child)
        remainder = rest[consumed:]
        if remainder[:1] == "/":
            deeper = _match_longest(child, remainder[1:])
            if deeper is not None and (best is None or consumed + 1 + deeper[0] > best[0]):
                best = (consumed + 1 + deeper[0], deeper[1])
    return best


def _reference_terminates(remainder: str, *, directory: bool) -> bool:
    """True when the matched path really ENDS here.

    - A remainder starting with a separator means the author named something
      DEEPER than anything this target received (`.../helper/..`,
      `.../helper/missing.md`), so the prefix is never substituted — a path
      that was never emitted must not be written into the target. The one
      exception is a separator run after a DIRECTORY that is followed by the
      end of the text or a delimiter (`see ${ROOT}/skills/a/ now`): the run is
      left outside the token, so it survives byte-exactly. A separator run
      followed by `.` or `!` is a dot segment (or a name this target never
      received) and stays rejected.
    - A run of sentence trailers (`.`/`!`) may follow a match when the run is
      itself followed by the end of the text or a delimiter
      ("see ${ROOT}/skills/a/README.md." / "...README.md!").
    """
    if not remainder:
        return True
    if remainder[0] == "/":
        if not directory:
            return False
        after = remainder.lstrip("/")
        return not after or _is_reference_delimiter(after[0])
    stripped = remainder.lstrip(_SENTENCE_TRAILERS)
    if stripped != remainder:
        return not stripped or _is_reference_delimiter(stripped[0])
    return _is_reference_delimiter(remainder[0])


def _fallback_span(rest: str) -> int:
    """Extent of a reference that resolves to nothing, for disclosure only.

    Bounded by the characters a package path may not contain plus the usual
    closing punctuation, the package depth ceiling and a byte cap. A trailing
    run of sentence trailers (`.`/`!`) is dropped when the last segment still
    has content, so `/..` and `/.` are reported whole.
    """
    index = 0
    depth = 0
    limit = min(len(rest), _MAX_REFERENCE_BYTES)
    while index < limit:
        char = rest[index]
        if _is_reference_delimiter(char):
            break
        if char == "/":
            depth += 1
            if depth >= MAX_PATH_DEPTH:
                break
        index += 1
    span = rest[:index]
    segments = span.split("/")
    if segments and segments[-1].strip(_SENTENCE_TRAILERS):
        span = span.rstrip(_SENTENCE_TRAILERS)
    return len(span)


def _emitted_index(
    source_root: Path,
    destination_base: Path,
    files: list[Path],
) -> dict[str, tuple[Path, bool]]:
    """Skill-relative key -> (emitted destination, exposes-executable).

    The key "" is the skill directory itself. A directory inherits the
    executable flag of any file emitted beneath it.
    """
    index: dict[str, tuple[Path, bool]] = {}
    for source in files:
        relative = source.relative_to(source_root)
        key = relative.as_posix()
        executable = is_executable_reference(key)
        index[key] = (destination_base / relative, executable)
        parent = relative.parent
        while True:
            directory_key = "" if parent == Path(".") else parent.as_posix()
            destination = destination_base if not directory_key else destination_base / parent
            prior = index.get(directory_key)
            index[directory_key] = (destination, executable or bool(prior and prior[1]))
            if not directory_key:
                break
            parent = parent.parent
    return index


# Where import relocates a vendor plugin's NON-skill root assets
# (`_normalize_source` / `_normalize_gemini_extension`). The subtree mirrors
# the ORIGINAL plugin root: `<vendor>/<origin>/scripts/run.py` was written by
# the author as `${CLAUDE_PLUGIN_ROOT}/scripts/run.py`.
_VENDOR_SUBTREE = ("io.sessionfs.portability", "vendor")


def _package_layout(package_root: Path, origin_format: str | None = None) -> _PathNode:
    """Every path PRESENT in the cached package (delivered or not), keyed by
    the names the AUTHOR wrote.

    Used only to measure how far an unresolved reference extends, so a marker
    names a plugin-root script or a vendor-relocated asset exactly (spaces, a
    legal trailing `!` and all). Relocated vendor members are indexed under
    their original plugin-root-relative path as well as their relocated one;
    the relocated cache path is kept as metadata only. Nothing read from this
    layout is ever substituted into emitted text: the caller uses the match
    LENGTH, never the value.

    `origin_format` limits the aliasing to that origin's vendor subtree; when
    it is None every origin present is aliased (deterministically, first in
    sorted order wins).
    """
    entries: dict[str, tuple[Path, bool] | None] = {}
    aliases: dict[str, tuple[Path, bool]] = {}
    vendor_prefix = "/".join(_VENDOR_SUBTREE) + "/"
    for source in sorted(package_root.rglob("*")):
        if is_link_or_reparse(source):
            continue
        try:
            relative = source.relative_to(package_root).as_posix()
        except ValueError:  # pragma: no cover - rglob is always below the root
            continue
        entries[relative] = None
        if not relative.startswith(vendor_prefix):
            continue
        origin, _, original = relative[len(vendor_prefix) :].partition("/")
        if not original or (origin_format is not None and origin != origin_format):
            continue
        aliases.setdefault(original, (source, False))
    for original, metadata in aliases.items():
        # A real top-level entry keeps precedence over an alias of the same name.
        entries.setdefault(original, metadata)
    return _build_path_trie(entries)


def _plugin_root_substitutions(
    text: str,
    *,
    tool: str,
    package_root: Path,
    emitted: _PathNode,
    present: Callable[[], _PathNode],
) -> tuple[list[tuple[str, str]], list[str], bool]:
    """Origin-aware projection of plugin-root references for `tool`.

    A reference is substituted with the path the target tool ACTUALLY receives
    for that asset; a reference to anything this target does not receive (a
    plugin-root script, a vendor-relocated asset, an omitted executable
    extension, a `..` escape, a deeper path than was delivered) is replaced
    with a non-runnable marker and disclosed. The cache root is never emitted,
    so an unconsented executable can never be handed to a target as a runnable
    absolute path.

    Returns (substitution pairs, unresolved reference labels, exposes-executable).
    """
    pairs: dict[str, str] = {}
    unresolved: list[str] = []
    exposes_executable = False
    for placeholder, pattern in _PLACEHOLDER_PATTERNS.items():
        if placeholder not in text:
            continue
        if substitute_plugin_root(placeholder, package_root=package_root, tool=tool) == placeholder:
            continue  # the target expands this placeholder natively (Claude Code)
        for match in pattern.finditer(text):
            tail = text[match.end() :]
            # A validated package path never contains a backslash, so folding
            # separators is length-preserving and keeps the token byte-exact.
            rest = tail.replace("\\", "/")
            if rest[:1] != "/":
                node = emitted if emitted.terminal else None
                token = placeholder
                if node is not None and node.value is not None:
                    if token not in pairs:
                        pairs[token] = str(node.value[0])
                        exposes_executable = exposes_executable or node.value[1]
                    continue
                if token not in pairs:
                    pairs[token] = _UNAVAILABLE.format(suffix="")
                    unresolved.append("(plugin root)")
                continue
            rest = rest[1:]
            matched = _match_longest(emitted, rest)
            if matched is not None and _reference_terminates(
                rest[matched[0] :], directory=bool(matched[1].children)
            ):
                token = placeholder + tail[: 1 + matched[0]]
                if token in pairs:
                    continue
                value = matched[1].value
                assert value is not None  # terminal nodes of `emitted` carry one
                pairs[token] = str(value[0])
                exposes_executable = exposes_executable or value[1]
                continue
            # Not delivered here: measure the reference against what the
            # package actually contains, else fall back to the character scan.
            span = _fallback_span(rest)
            in_package = _match_longest(present(), rest)
            if in_package is not None and in_package[0] > span:
                span = in_package[0]
            token = placeholder + tail[: 1 + span]
            if token in pairs:
                continue
            label = rest[:span] or "(plugin root)"
            pairs[token] = _UNAVAILABLE.format(suffix=f":{label}" if rest[:span] else "")
            unresolved.append(label)
    # Longest reference first: `${ROOT}/a/b` must be replaced before `${ROOT}/a`.
    ordered = sorted(pairs.items(), key=lambda item: (-len(item[0]), item[0]))
    return ordered, unresolved, exposes_executable


def plan_package(
    package: PackageRecord,
    *,
    tool: str,
    scope: str,
    project_root: Path,
    home_dir: Path,
) -> EmitPlan:
    """Plan native Agent Skill copies and disclose non-installed components."""
    if tool not in SUPPORTED_TOOLS:
        raise SkillError(f"Unsupported tool: {tool}")
    if scope not in {"project", "user"}:
        raise SkillError("Skill scope must be 'project' or 'user'")
    base = project_root if scope == "project" else home_dir
    relative_root = PROJECT_SKILL_ROOTS[tool] if scope == "project" else USER_SKILL_ROOTS[tool]
    destination_root = base / relative_root
    emissions: list[Emission] = []
    placeholder_notes: list[Degradation] = []
    # Pass 1: the layout this target actually receives. Plugin-root references
    # are resolved against THIS layout (never against the cache root), so a
    # substituted path always names a file the target really has.
    planned: list[tuple[SkillMetadata, Path, Path, list[Path]]] = []
    index: dict[str, tuple[Path, bool]] = {}
    for skill in package.skills:
        source_root = package.package_root / skill.relative_dir
        destination_base = destination_root / skill.name
        files = [
            source
            for source in sorted(source_root.rglob("*"))
            if not is_link_or_reparse(source) and source.is_file()
        ]
        planned.append((skill, source_root, destination_base, files))
        local = _emitted_index(source_root, destination_base, files)
        base_key = skill.relative_dir.strip("/")
        for key, value in local.items():
            index.setdefault(f"{base_key}/{key}" if key else base_key, value)
            if package.origin_format == "agent-skill":
                # A bare `SKILL.md` import: the ORIGIN plugin root was the skill
                # directory itself, so its references are skill-relative.
                index.setdefault(key, value)
    emitted = _build_path_trie(dict(index))
    cached_present: list[_PathNode] = []

    def _present_layout() -> _PathNode:
        # Built at most once per plan, and only when a reference resolved to
        # nothing: it bounds the disclosure marker, never a path.
        if not cached_present:
            cached_present.append(
                _package_layout(package.package_root, package.origin_format)
            )
        return cached_present[0]

    exposes_executable = False
    for skill, source_root, destination_base, files in planned:
        for source in files:
            relative = source.relative_to(source_root)
            destination = destination_base / relative
            raw = source.read_bytes()
            substitutions: tuple[tuple[str, str], ...] = ()
            # `${CLAUDE_PLUGIN_ROOT}` in TEXT members: Claude Code keeps the
            # placeholder; other tools get the EMITTED location of the
            # referenced asset, or a disclosed non-runnable marker when this
            # target receives no such asset. Binary bytes are never touched.
            if is_text_member(raw):
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = ""
                pairs, unresolved, executable = _plugin_root_substitutions(
                    text,
                    tool=tool,
                    package_root=package.package_root,
                    emitted=emitted,
                    present=_present_layout,
                )
                exposes_executable = exposes_executable or executable
                member = f"{skill.relative_dir}/{relative.as_posix()}"
                if pairs:
                    substitutions = tuple(pairs)
                    resolved_pairs = [
                        item for item in pairs if not item[1].startswith("<sessionfs-unavailable")
                    ]
                    if resolved_pairs:
                        placeholder_notes.append(
                            Degradation(
                                component="placeholder-substituted",
                                reason=f"{member}: "
                                + ", ".join(f"{ph} -> {rep}" for ph, rep in resolved_pairs),
                                transform="substituted",
                            )
                        )
                if unresolved:
                    placeholder_notes.append(
                        Degradation(
                            component="placeholder-unresolved",
                            reason=f"{member}: not installed for {tool}: "
                            + ", ".join(sorted(dict.fromkeys(unresolved))),
                            transform="omitted",
                        )
                    )
            emission = Emission(
                source=str(source),
                destination=str(destination),
                output_hash="",
                substitutions=substitutions,
            )
            emissions.append(
                Emission(
                    source=emission.source,
                    destination=emission.destination,
                    output_hash=_sha256(emission.transform(raw)),
                    substitutions=substitutions,
                )
            )
    omissions: list[Omission] = []
    if package.has_mcp:
        omissions.append(
            Omission(
                component="portable-mcp",
                reason="MCP registration requires the separate enable-mcp consent flow",
            )
        )
    if package.has_executable_extensions:
        omissions.append(
            Omission(
                component="executable-extensions",
                reason="Hooks, agents, commands, and vendor executables require verified adapters",
            )
        )
    requirements: list[str] = []
    if any(skill.has_scripts for skill in package.skills) or exposes_executable:
        # A substituted reference that hands the target a concrete path to an
        # executable asset carries the same consent as the asset itself.
        requirements.append("skill-scripts")
    if any(skill.has_hooks for skill in package.skills):
        # Frontmatter hooks / allowed-tools are executed or pre-approved by the
        # target tool on invocation: a permission grant, never covered by --yes.
        requirements.append("skill-hooks")
    if any(skill.has_hook_extensions for skill in package.skills):
        requirements.append("executable-extensions")
    if package.dlp_findings:
        # Client-side DLP findings are a consent requirement, not a block:
        # `--allow-dlp-findings` (never `--yes` alone) accepts them.
        requirements.append("dlp-findings")
    degradations = [
        Degradation(component=item[0], reason=item[1], transform=item[2])
        for item in package.degradations
    ]
    degradations.extend(placeholder_notes)
    for finding in package.dlp_findings:
        # Persisted into state once installed (i.e. once consent was given):
        # `sfs skill show` discloses exactly what was accepted.
        degradations.append(
            Degradation(
                component="dlp",
                reason=f"{finding.get('path')}:{finding.get('line')} {finding.get('category')}/{finding.get('pattern')}",
                transform="allowed-by-user",
            )
        )
    payload = {
        "package_id": package.package_id,
        "content_hash": package.content_hash,
        "tool": tool,
        "scope": scope,
        "emissions": [item.as_dict() for item in emissions],
        "omissions": [item.as_dict() for item in omissions],
        "degradations": [item.as_dict() for item in degradations],
        "consent_requirements": requirements,
    }
    return EmitPlan(
        package_id=package.package_id,
        content_hash=package.content_hash,
        tool=tool,
        scope=scope,
        emissions=tuple(emissions),
        degradations=tuple(degradations),
        omissions=tuple(omissions),
        consent_requirements=tuple(requirements),
        plan_hash=_plan_hash(payload),
    )
