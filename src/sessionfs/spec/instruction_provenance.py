"""Best-effort discovery of instruction artifacts that shaped a session.

Used at capture time to populate:
- rules_version        — if embedded in a SessionFS-managed file
- rules_hash           — sha256 of the active rules file (or "mixed" compound)
- rules_source         — sessionfs | manual | mixed | none
- instruction_artifacts[] — list of artifact descriptors

All discovery is non-fatal. Any exception or missing file is swallowed —
we never break session capture because of provenance.

Artifact shape:
    {
      "artifact_type": "rules_file" | "agent" | "skill" | "settings",
      "path": "CLAUDE.md",     # project-relative; omitted for global
      "scope": "project" | "global",
      "source": "sessionfs" | "manual" | "tool",
      "hash": "sha256:...",
      "version": 3,            # only when managed + parseable
    }
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
PROJECT_AUX_DIRS: list[tuple[str, str]] = [
    (".agents", "agent"),
    (".claude/agents", "agent"),
    (".claude/skills", "skill"),
    (".claude/commands", "skill"),
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
    instruction_artifacts: list[InstructionArtifact] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rules_source": self.rules_source,
            "rules_version": self.rules_version,
            "rules_hash": self.rules_hash,
            "instruction_artifacts": [asdict(a) for a in self.instruction_artifacts],
        }


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _safe_read(path: Path, max_bytes: int = 1024 * 1024) -> bytes | None:
    """Read up to max_bytes; return None on any failure or oversize."""
    try:
        if not path.is_file() or path.is_symlink():
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
        root = Path(project_root) if project_root else None
    except Exception:
        root = None

    prov = Provenance()

    if root is None or not root.is_dir():
        return prov

    has_managed = False
    has_manual = False
    active_hash: str | None = None
    active_version: int | None = None

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
                    has_manual = True
            elif p.is_dir():
                # Hash each file inside (bounded — up to 30 files).
                count = 0
                for child in sorted(p.iterdir()):
                    if count >= 30:
                        break
                    if child.is_file() and not child.is_symlink():
                        data = _safe_read(child)
                        if data is None:
                            continue
                        prov.instruction_artifacts.append(
                            InstructionArtifact(
                                artifact_type=kind,
                                hash=_sha256(data),
                                source="manual",
                                scope="project",
                                path=str(Path(rel) / child.name),
                            )
                        )
                        has_manual = True
                        count += 1
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

    # Derive rules_source
    if has_managed and has_manual:
        prov.rules_source = "mixed"
    elif has_managed:
        prov.rules_source = "sessionfs"
    elif has_manual:
        prov.rules_source = "manual"
    else:
        prov.rules_source = "none"

    prov.rules_hash = active_hash
    prov.rules_version = active_version

    return prov
