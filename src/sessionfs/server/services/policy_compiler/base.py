"""Shared types for the org skills policy compiler (marketplace v2 §8.3).

Every compiler is pure and deterministic: the same policy and catalog
sources always produce the same bytes. Each compiled result lists, per
policy field, whether the TOOL enforces it (``enforceable``), whether only
SessionFS checks it during install / assign / reconcile (``report_only``),
or whether nothing checks it (``unsupported``). Nothing is presented as
enforced unless the tool itself enforces it.

Vendor keys: every native key a compiler emits was confirmed against the
vendor's documentation on 2026-10-20; the sources are recorded on each
compiler (``SOURCES``) and returned with the compiled result. Keys that
could not be confirmed are not emitted (see docs/design/marketplace-v2.md
§8.3 "as built").
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sessionfs.skills.policy import SkillsPolicy

COMPILER_VERSION = "1"
CHECKED_ON = "2026-10-20"

# Every policy field a compiled result accounts for (each lands in exactly
# one of enforceable / report_only / unsupported).
POLICY_FIELDS = (
    "mode",
    "allowlist",
    "allowed_publishers_verified_only",
    "allow_hooks",
    "allow_local_mcp",
    "allow_remote_mcp",
    "allowed_remote_mcp_origins",
    "allow_skill_scripts",
    "allow_hidden_unicode",
    "require_signature",
    "require_dlp_clean_import",
    "max_scan_severity",
    "auto_update",
    "trust_code_updates_publishers",
    "block_unmanaged_marketplaces",
)
# Fields SessionFS itself never checks yet (auto-update rollout and the
# trusted code-update exception are follow-ups).
NOT_CHECKED = ("auto_update", "trust_code_updates_publishers")


@dataclass(frozen=True)
class CatalogSource:
    """A SessionFS-served native catalog (phase 3a) to register / allow."""

    name: str  # e.g. "sessionfs-acme"
    git_url: str  # https git URL of the generated catalog repository


@dataclass
class Artifact:
    kind: str  # "file" | "checklist"
    format: str  # "json" | "toml" | "markdown"
    content: str
    # Where IT deploys it, per OS (system paths; never written by `sfs`
    # without an explicit, separate apply step).
    paths: dict[str, str] = field(default_factory=dict)
    description: str = ""

    @property
    def content_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.content.encode()).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "format": self.format,
            "content": self.content,
            "content_hash": self.content_hash,
            "paths": dict(self.paths),
            "description": self.description,
        }


@dataclass
class Warning:
    """A side effect the admin must acknowledge before publishing."""

    code: str
    tool: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "tool": self.tool, "message": self.message}


@dataclass
class CompiledPolicy:
    tool: str
    enforcement: str  # managed | report_only | off | unsupported
    artifacts: list[Artifact] = field(default_factory=list)
    enforceable: list[str] = field(default_factory=list)
    report_only: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    warnings: list[Warning] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)
    compiler_version: str = COMPILER_VERSION

    @property
    def artifact_hash(self) -> str:
        digest = hashlib.sha256()
        for artifact in self.artifacts:
            digest.update(artifact.content_hash.encode() + b"\n")
        digest.update(json.dumps(
            [self.enforceable, self.report_only, self.unsupported], sort_keys=True
        ).encode())
        return "sha256:" + digest.hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "enforcement": self.enforcement,
            "compiler_version": self.compiler_version,
            "artifact_hash": self.artifact_hash,
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "enforceable": list(self.enforceable),
            "report_only": list(self.report_only),
            "unsupported": list(self.unsupported),
            "warnings": [warning.as_dict() for warning in self.warnings],
            "notes": list(self.notes),
            "sources": list(self.sources),
        }


def classify(result: CompiledPolicy, enforced: set[str]) -> None:
    """Fill the three field lists: tool-enforced fields, fields only
    SessionFS checks, and fields nothing checks."""
    for name in POLICY_FIELDS:
        if name in enforced:
            result.enforceable.append(name)
        elif name in NOT_CHECKED:
            result.unsupported.append(name)
        else:
            result.report_only.append(name)


def dump_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value {type(value)!r}")


def _toml_key(key: str) -> str:
    return key if key.replace("-", "").replace("_", "").isalnum() else json.dumps(key)


def dump_toml(tables: list[tuple[str, dict[str, Any]]], top: dict[str, Any] | None = None) -> str:
    """A tiny deterministic TOML writer: top-level scalars, then tables in
    the given order (an empty dict emits an empty table)."""
    lines: list[str] = []
    for key in sorted(top or {}):
        lines.append(f"{_toml_key(key)} = {_toml_value((top or {})[key])}")
    for name, values in tables:
        if lines:
            lines.append("")
        lines.append(f"[{'.'.join(_toml_key(part) for part in name.split('.'))}]")
        for key in sorted(values):
            lines.append(f"{_toml_key(key)} = {_toml_value(values[key])}")
    return "\n".join(lines) + "\n"


def marketplaces_restricted(policy: SkillsPolicy) -> bool:
    """Whether the tool's own marketplaces should be limited to SessionFS
    catalogs: explicitly, or implied by a non-open mode."""
    return policy.block_unmanaged_marketplaces or policy.mode != "open"


def source_list(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"key": key, "url": url, "checked": CHECKED_ON} for key, url in pairs]
