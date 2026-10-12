"""Best-effort discovery of instruction artifacts that shaped a session.

Used at capture time to populate:
- rules_version        — if embedded in a SessionFS-managed file
- rules_hash           — sha256 of the active rules file (or "mixed" compound)
- rules_source         — sessionfs | manual | mixed | none   (RULES files only:
                         the migration-028 contract consumed by sessions.py and
                         `sfs resume`; managed skill packages never change it)
- skills_source        — managed | manual | mixed | none    (skill packages and
                         manually installed skill dirs; additive JSON field,
                         no server column)
- instruction_artifacts[] — list of artifact descriptors

All discovery is non-fatal. Any exception or missing file is swallowed —
we never break session capture because of provenance.

Artifact shape:
    {
      "artifact_type": "rules_file" | "agent" | "skill" | "settings",
      "path": "CLAUDE.md",     # project-relative (home-relative for a
                               # user-scope skill root); omitted for the
                               # fixed global rule-file candidates
      "scope": "project" | "global",
      "source": "sessionfs" | "manual" | "tool",
      "hash": "sha256:...",
      "version": 3,            # only when managed + parseable
    }

Managed skill packages are captured separately at package granularity from
skills.state.json. This avoids truncating package provenance at the bounded
auxiliary-file scan used for unmanaged artifacts.
"""

from __future__ import annotations

from typing import Callable

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sessionfs.skills.adapters import PROJECT_SKILL_ROOTS, USER_SKILL_ROOTS
from sessionfs.skills.package import VCS_METADATA_DIRS
from sessionfs.skills.path_safety import is_link_or_reparse
from sessionfs.server.services.rules_compiler.base import (
    is_managed_content,
    parse_managed_marker,
)

# Map tool -> list of project-local rule filenames to inspect.
# Order matters: first-match wins for the "primary" active file detection.
PROJECT_RULES_FILES: dict[str, list[str]] = {
    "claude-code": ["CLAUDE.md"],
    "codex": ["codex.md", "AGENTS.md"],
    "cursor": [".cursorrules"],
    "copilot": [".github/copilot-instructions.md"],
    "gemini": ["GEMINI.md"],
}

# Global artifact candidates (home-directory files). Hash-only, no content.
GLOBAL_CANDIDATES: list[tuple[str, str]] = [
    # (relative-to-home, artifact_type)
    (".claude/CLAUDE.md", "rules_file"),
    (".codex/instructions.md", "rules_file"),
    (".config/github-copilot/intellij/global-copilot-instructions.md", "rules_file"),
    (".gemini/config.json", "settings"),
]

# Project-scoped supplementary artifact dirs (agents + skills + settings).
# Skill directories are derived from the adapter registry so a manually
# installed Gemini / Copilot / Cline / Roo / Kilo skill is scanned too, not
# only Claude Code's. Skill roots come first so a file under `.agents/skills`
# is recorded once, as a skill (the `.agents` agent scan then skips it).
PROJECT_AUX_DIRS: list[tuple[str, str]] = [
    *[(root, "skill") for root in sorted(set(PROJECT_SKILL_ROOTS.values()))],
    (".claude/commands", "skill"),
    (".agents", "agent"),
    (".claude/agents", "agent"),
    (".sessionfs.toml", "settings"),  # a file is fine too
]


@dataclass
class InstructionArtifact:
    artifact_type: str
    hash: str
    source: str
    scope: str
    path: str | None = None
    version: int | None = None


@dataclass
class Provenance:
    rules_source: str = "none"
    rules_version: int | None = None
    rules_hash: str | None = None
    skills_source: str = "none"
    instruction_artifacts: list[InstructionArtifact] = field(default_factory=list)
    managed_packages: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        result = {
            "rules_source": self.rules_source,
            "rules_version": self.rules_version,
            "rules_hash": self.rules_hash,
            "skills_source": self.skills_source,
            "instruction_artifacts": [asdict(a) for a in self.instruction_artifacts],
        }
        if self.managed_packages:
            result["managed_packages"] = self.managed_packages
        return result


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _safe_read(path: Path, max_bytes: int = 1024 * 1024) -> bytes | None:
    """Read up to max_bytes; return None on any failure or oversize."""
    try:
        if not path.is_file() or is_link_or_reparse(path):
            return None
        size = path.stat().st_size
        if size > max_bytes:
            return None
        with path.open("rb") as f:
            return f.read(max_bytes + 1)
    except (OSError, PermissionError):
        return None


def _global_rules_enabled() -> bool:
    """Respect SFS_CAPTURE_GLOBAL_RULES=off."""
    val = os.environ.get("SFS_CAPTURE_GLOBAL_RULES", "").strip().lower()
    return val not in ("off", "0", "false", "no")


def _safe_managed_output(
    raw_path: str,
    *,
    scope: str,
    project_root: Path,
    home_dir: Path,
) -> Path | None:
    """Accept only non-symlinked paths below a declared adapter root.

    State records RESOLVED output paths (SkillManager resolves its roots) while
    a watcher hands over the workspace path verbatim; a symlink component in
    that prefix (`/tmp` -> `/private/tmp`, a symlinked `~/code`) must not make
    every managed package look drifted. Both sides are therefore compared on
    the real base: the base is realpath'd, and a recorded path expressed under
    the LEXICAL base is re-rooted the same way (its own components are still
    walked for symlinks below; the recorded path itself is never realpath'd,
    so a link inside the root can never be followed).
    """
    try:
        path = Path(os.path.abspath(raw_path))
        lexical_base = Path(
            os.path.abspath(project_root if scope == "project" else home_dir)
        )
        base = Path(os.path.realpath(lexical_base))
        if base != lexical_base:
            try:
                path = base / path.relative_to(lexical_base)
            except ValueError:
                pass
        roots = PROJECT_SKILL_ROOTS if scope == "project" else USER_SKILL_ROOTS
        for relative_root in set(roots.values()):
            root = Path(os.path.abspath(base / relative_root))
            try:
                relative = path.relative_to(root)
                root_relative = root.relative_to(base)
            except ValueError:
                continue
            current = base
            for part in (*root_relative.parts, *relative.parts):
                current /= part
                if is_link_or_reparse(current):
                    return None
            return path
    except (OSError, TypeError, ValueError):
        return None
    return None


def _iter_aux_files(
    root: Path,
    limit: int = 30,
    *,
    exclude: Callable[[Path], bool] | None = None,
) -> list[Path]:
    """Recursively collect project-local aux files in deterministic order.

    We intentionally bound discovery so provenance capture stays cheap even in
    repos with many nested skills/agents. Nested directories are included
    because skills/agents are commonly organized by folder. Files rejected by
    `exclude` (managed outputs, paths already recorded) do NOT count against
    the limit, so a root full of managed files cannot hide a manual skill.
    VCS metadata and dot-directories are pruned: a manual skill checked out
    as a git repository must not spend the budget on `.git/objects` and hide
    its siblings.
    """
    files: list[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            # Deterministic directory traversal; skip symlinked subdirs.
            dirnames[:] = sorted(
                name for name in dirnames
                if not name.startswith(".")
                and name.casefold() not in VCS_METADATA_DIRS
                and not is_link_or_reparse(Path(dirpath, name))
            )
            for name in sorted(filenames):
                if len(files) >= limit:
                    return files
                child = Path(dirpath) / name
                try:
                    if is_link_or_reparse(child) or not child.is_file():
                        continue
                except (OSError, PermissionError):
                    continue
                if exclude is not None and exclude(child):
                    continue
                files.append(child)
    except (OSError, PermissionError):
        return files
    return files


def _state_size_limit() -> int:
    """Same ceiling SkillStateStore accepts, so a large valid state is never silently skipped."""
    from sessionfs.skills.state import MAX_STATE_BYTES

    return MAX_STATE_BYTES


def _output_serves_tool(output: dict, tool: str) -> bool:
    """Shared outputs (cursor + amp both emit .agents/skills) carry a `tools` list."""
    tools = output.get("tools")
    if isinstance(tools, list) and tool in tools:
        return True
    return output.get("tool") == tool


# Per state file: a hostile committed skills.state.json must not stall the
# watcher. Outputs are deduplicated by absolute path, and hashing stops once
# either budget trips (the affected packages report `unverified`).
MANAGED_OUTPUT_ENTRY_CAP = 2_000
MANAGED_OUTPUT_HASH_BYTES_CAP = 64 * 1024 * 1024


def _managed_package_provenance(
    project_root: Path,
    home_dir: Path,
    tool: str | None,
) -> list[dict]:
    """Read authoritative managed package state; never raise into capture."""
    state_paths = (
        ("project", project_root / ".sessionfs" / "skills.state.json"),
        ("user", home_dir / ".sessionfs" / "skills" / "skills.state.json"),
    )
    result: list[dict] = []
    for scope, state_path in state_paths:
        try:
            raw_state = _safe_read(state_path, max_bytes=_state_size_limit())
            if raw_state is None:
                continue  # missing, symlinked, or oversized state: never follow/hang
            value = json.loads(raw_state.decode("utf-8"))
            packages = value.get("packages") if isinstance(value, dict) else None
            if not isinstance(packages, dict):
                continue
            hashed_paths: dict[str, bool] = {}  # abspath -> matched expected hash
            entries_seen = 0
            bytes_hashed = 0
            budget_tripped = False
            for package_id, row in sorted(packages.items()):
                if not isinstance(row, dict):
                    continue
                targets = row.get("targets")
                if not isinstance(targets, list) or (tool and tool not in targets):
                    continue
                source: dict = (
                    row["source"] if isinstance(row.get("source"), dict) else {}
                )
                plan_hashes: dict = (
                    row["plan_hashes"]
                    if isinstance(row.get("plan_hashes"), dict)
                    else {}
                )
                outputs = [
                    item for item in (row.get("outputs") or [])
                    if isinstance(item, dict) and (not tool or _output_serves_tool(item, tool))
                ]
                drift = False
                unverified = False
                for output in outputs:
                    path_raw = output.get("path")
                    expected = output.get("output_hash")
                    if not isinstance(path_raw, str) or not isinstance(expected, str):
                        drift = True
                        break
                    safe_path = _safe_managed_output(
                        path_raw,
                        scope=scope,
                        project_root=project_root,
                        home_dir=home_dir,
                    )
                    if safe_path is None:
                        drift = True
                        break
                    entries_seen += 1
                    if budget_tripped or entries_seen > MANAGED_OUTPUT_ENTRY_CAP:
                        # Entry cap counts RAW entries (duplicates included):
                        # a state file padded with thousands of rows is not
                        # trusted, whatever it repeats.
                        budget_tripped = True
                        unverified = True
                        break
                    key = os.path.abspath(safe_path)
                    cached = hashed_paths.get(key)
                    if cached is not None:
                        # Same output listed again: reuse the verdict, never re-hash.
                        if not cached:
                            drift = True
                            break
                        continue
                    try:
                        size = os.stat(key).st_size
                    except OSError:
                        size = 0
                    if bytes_hashed + size > MANAGED_OUTPUT_HASH_BYTES_CAP:
                        budget_tripped = True
                        unverified = True
                        break
                    bytes_hashed += size
                    data = _safe_read(safe_path, max_bytes=20 * 1024 * 1024)
                    matched = data is not None and _sha256(data) == expected
                    hashed_paths[key] = matched
                    if not matched:
                        drift = True
                        break
                record = {
                    "package_id": package_id,
                    "name": row.get("name"),
                    "content_hash": row.get("content_hash"),
                    "source_type": source.get("source_type"),
                    "source_commit": source.get("commit"),
                    "target_tool": tool,
                    "scope": scope,
                    "plan_hash": plan_hashes.get(tool) if tool else None,
                    "degradations": row.get("degradations") or [],
                    "omissions": row.get("omissions") or [],
                    "consent_receipt_ids": row.get("consent_receipt_ids") or [],
                    "integrity_status": (
                        "unverified" if unverified else "drifted" if drift else "verified"
                    ),
                }
                if source.get("source_type") not in {"local", "local-project"}:
                    record["source_url"] = source.get("locator")
                result.append(record)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, AttributeError):
            continue
    return result


def _managed_output_paths(project_root: Path, home_dir: Path) -> set[Path]:
    result: set[Path] = set()
    for scope, state_path in (
        ("project", project_root / ".sessionfs" / "skills.state.json"),
        ("user", home_dir / ".sessionfs" / "skills" / "skills.state.json"),
    ):
        try:
            raw_state = _safe_read(state_path, max_bytes=_state_size_limit())
            if raw_state is None:
                continue  # missing, symlinked, or oversized state: never follow/hang
            value = json.loads(raw_state.decode("utf-8"))
            packages = value.get("packages") if isinstance(value, dict) else None
            if not isinstance(packages, dict):
                continue
            for row in packages.values():
                if not isinstance(row, dict):
                    continue
                for output in row.get("outputs") or []:
                    path_raw = output.get("path") if isinstance(output, dict) else None
                    if not isinstance(path_raw, str):
                        continue
                    path = _safe_managed_output(
                        path_raw,
                        scope=scope,
                        project_root=project_root,
                        home_dir=home_dir,
                    )
                    if path is not None:
                        result.add(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, AttributeError):
            continue
    return result


def _user_skill_roots(tool: str | None) -> list[str]:
    """Home-relative skill roots to scan: the active tool's when it is known,
    otherwise every distinct root (each bounded independently)."""
    if tool and tool in USER_SKILL_ROOTS:
        return [USER_SKILL_ROOTS[tool]]
    return sorted(set(USER_SKILL_ROOTS.values()))


def _inspect_rules_file(path: Path) -> tuple[InstructionArtifact | None, bool, int | None]:
    """Return (artifact, is_managed, version)."""
    data = _safe_read(path)
    if data is None:
        return None, False, None
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    managed = is_managed_content(text)
    version: int | None = None
    if managed:
        parsed = parse_managed_marker(text)
        if parsed:
            version = parsed[0]
    art = InstructionArtifact(
        artifact_type="rules_file",
        hash=_sha256(data),
        source="sessionfs" if managed else "manual",
        scope="project",
        path=None,  # caller sets relative path
        version=version,
    )
    return art, managed, version


def discover(
    project_root: Path | str,
    tool: str | None = None,
    home_dir: Path | str | None = None,
) -> Provenance:
    """Discover instruction artifacts for `project_root` (and `tool` if known).

    All I/O errors are non-fatal — partial results are always acceptable.
    """
    try:
        # Real path: managed output paths in state are resolved, and a
        # symlinked workspace prefix must compare equal to them.
        root = Path(os.path.realpath(project_root)) if project_root else None
    except Exception:
        root = None

    prov = Provenance()

    if root is None or not root.is_dir():
        return prov

    has_managed = False
    has_manual = False
    active_hash: str | None = None
    active_version: int | None = None

    package_home = Path(home_dir) if home_dir else Path.home()
    prov.managed_packages = _managed_package_provenance(root, package_home, tool)
    managed_output_paths = _managed_output_paths(root, package_home)
    # Managed skill packages are reported under `skills_source`; they never
    # flip `rules_source`, which describes the RULES files only.

    # Pass 1: primary rules files for the active tool (if known)
    primary_files: list[str] = []
    if tool and tool in PROJECT_RULES_FILES:
        primary_files = PROJECT_RULES_FILES[tool]

    for rel in primary_files:
        p = root / rel
        art, managed, version = _inspect_rules_file(p)
        if art is None:
            continue
        art.path = rel
        prov.instruction_artifacts.append(art)
        if managed:
            has_managed = True
            # The first managed file wins for the top-level fields.
            if active_hash is None:
                active_hash = art.hash
                active_version = version
        else:
            has_manual = True
            if active_hash is None:
                active_hash = art.hash

    # Pass 2: *all* other tool rule files in the repo (hash-only provenance —
    # the other tools' rule files are still relevant context even if they
    # aren't the active tool).
    for other_tool, files in PROJECT_RULES_FILES.items():
        if other_tool == tool:
            continue
        for rel in files:
            p = root / rel
            art, managed, _ = _inspect_rules_file(p)
            if art is None:
                continue
            art.path = rel
            prov.instruction_artifacts.append(art)
            if managed:
                has_managed = True
            else:
                has_manual = True

    # Pass 3: auxiliary project artifacts (agents, skills, settings)
    recorded_aux_paths: set[str] = set()
    for rel, kind in PROJECT_AUX_DIRS:
        p = root / rel
        try:
            if p.is_file():
                data = _safe_read(p)
                if data is not None:
                    prov.instruction_artifacts.append(
                        InstructionArtifact(
                            artifact_type=kind,
                            hash=_sha256(data),
                            source="manual",
                            scope="project",
                            path=rel,
                        )
                    )
                    if kind != "skill":
                        has_manual = True  # skills only affect skills_source
            elif p.is_dir():
                # Hash files recursively (bounded — up to 30 files per aux root).
                def _already_covered(child: Path, _root: Path = p, _rel: str = rel) -> bool:
                    if Path(os.path.abspath(child)) in managed_output_paths:
                        return True
                    return str(Path(_rel) / child.relative_to(_root).as_posix()) in recorded_aux_paths

                for child in _iter_aux_files(p, limit=30, exclude=_already_covered):
                    child_rel = str(Path(rel) / child.relative_to(p).as_posix())
                    data = _safe_read(child)
                    if data is None:
                        continue
                    recorded_aux_paths.add(child_rel)
                    prov.instruction_artifacts.append(
                        InstructionArtifact(
                            artifact_type=kind,
                            hash=_sha256(data),
                            source="manual",
                            scope="project",
                            path=child_rel,
                        )
                    )
                    if kind != "skill":
                        has_manual = True  # skills only affect skills_source
        except (OSError, PermissionError):
            continue

    # Pass 4: global artifacts (hash-only, opt-out via env)
    if _global_rules_enabled():
        home = Path(home_dir) if home_dir else Path.home()
        for rel, kind in GLOBAL_CANDIDATES:
            p = home / rel
            data = _safe_read(p)
            if data is None:
                continue
            prov.instruction_artifacts.append(
                InstructionArtifact(
                    artifact_type=kind,
                    hash=_sha256(data),
                    source="manual",
                    scope="global",
                    path=None,
                )
            )
            has_manual = True

        # Pass 4b: user-scope skill roots. A session whose ONLY active skill is
        # a manually installed user-scope skill (`~/.codex/skills/foo`) would
        # otherwise report `skills_source="none"` with no artifact. Same budget,
        # pruning and dedupe discipline as the project scan; managed outputs are
        # excluded because they are reported at package granularity instead.
        # Managed user-scope outputs are recorded under the REAL home (see
        # `_safe_managed_output`), so scan the real home too: a symlinked home
        # prefix must not make every managed output look like a manual skill.
        try:
            scan_home = Path(os.path.realpath(home))
        except OSError:
            scan_home = home
        for rel in _user_skill_roots(tool):
            skill_root = scan_home / rel
            try:
                if not skill_root.is_dir():
                    continue

                def _already_managed(child: Path) -> bool:
                    return Path(os.path.abspath(child)) in managed_output_paths

                for child in _iter_aux_files(skill_root, limit=30, exclude=_already_managed):
                    data = _safe_read(child)
                    if data is None:
                        continue
                    prov.instruction_artifacts.append(
                        InstructionArtifact(
                            artifact_type="skill",
                            hash=_sha256(data),
                            source="manual",
                            scope="global",
                            # Home-RELATIVE: identifies the skill without
                            # disclosing the home directory itself. Skills never
                            # flip `rules_source`, only `skills_source`.
                            path=str(Path(rel) / child.relative_to(skill_root).as_posix()),
                        )
                    )
            except (OSError, PermissionError):
                continue

    # Derive rules_source (rules files only)
    if has_managed and has_manual:
        prov.rules_source = "mixed"
    elif has_managed:
        prov.rules_source = "sessionfs"
    elif has_manual:
        prov.rules_source = "manual"
    else:
        prov.rules_source = "none"

    # Derive skills_source (managed packages vs manually installed skills)
    managed_skills = bool(prov.managed_packages)
    manual_skills = any(
        artifact.artifact_type == "skill" and artifact.source == "manual"
        for artifact in prov.instruction_artifacts
    )
    if managed_skills and manual_skills:
        prov.skills_source = "mixed"
    elif managed_skills:
        prov.skills_source = "managed"
    elif manual_skills:
        prov.skills_source = "manual"
    else:
        prov.skills_source = "none"

    prov.rules_hash = active_hash
    prov.rules_version = active_version

    return prov
