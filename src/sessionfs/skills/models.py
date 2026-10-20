"""Typed records shared by the local skills portability subsystem."""

from __future__ import annotations

import re

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SUPPORTED_TOOLS = (
    "claude-code",
    "codex",
    "gemini",
    "cursor",
    "copilot",
    "amp",
    "cline",
    "roo-code",
    "kilo-code",
)


class SkillError(ValueError):
    """A safe, user-facing package/import/install error."""


class PackageChangedConcurrently(SkillError):
    """The installed row changed between the decision and the locked mutation."""


MAX_DEVICE_ID_LENGTH = 100


def validate_device_id(value: Any) -> str:
    """Single device-id rule shared by the server API and the MCP client.

    A device id is a non-empty, printable (no control characters) string of at
    most MAX_DEVICE_ID_LENGTH characters; Unicode and spaces are allowed.
    """
    if not isinstance(value, str):
        raise ValueError("device_id must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_DEVICE_ID_LENGTH:
        raise ValueError(f"device_id must be 1-{MAX_DEVICE_ID_LENGTH} printable characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise ValueError("device_id must not contain control characters")
    return cleaned


_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")


def clean_untrusted_text(value: Any, *, limit: int, keep_newlines: bool = True) -> str:
    """Strip terminal escape sequences and C0/C1 control characters from text
    that came from a package or marketplace and bound its length.

    Registry descriptions and package metadata reach the terminal (Rich), the
    dashboard and audit logs; a `\x1b[` sequence or a stray C1 byte must never
    survive ingestion. Rich markup like ``[link=...]`` is NOT touched here — it
    is rendered literally at the display layer instead.
    """
    text = str(value if value is not None else "")
    text = _OSC_RE.sub("", _CSI_RE.sub("", text))
    text = _CONTROL_RE.sub("", text)
    if not keep_newlines:
        text = text.replace("\r", " ").replace("\n", " ")
    return text[:limit]


@dataclass(frozen=True)
class SourceIdentity:
    source_type: str
    locator: str
    commit: str | None = None
    subdir: str = ""
    registry_scope: str | None = None
    registry_id: str | None = None
    # Marketplace catalog identity: the listing selected at install time. The
    # listing name may differ from the package's plugin.json name, and update()
    # must re-resolve the SAME catalog entry.
    registry_alias: str | None = None
    listing_name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    relative_dir: str
    has_scripts: bool = False
    # SKILL.md frontmatter `hooks:` (any non-empty value) or tool pre-approval
    # (`allowed-tools:` / `disallowed-tools:`) is a permission grant executed by
    # the target tool when the skill is invoked; it requires `skill-hooks` consent.
    has_hooks: bool = False
    hook_events: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    # `hooks.json` / `hooks/` inside the skill directory are executable
    # extensions and require `executable-extensions` consent.
    has_hook_extensions: bool = False
    # Capability-bearing frontmatter keys that are disclosed but need no consent
    # (context, agent, model, effort, shell), as (key, value) pairs.
    disclosed_metadata: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["hook_events"] = list(self.hook_events)
        result["allowed_tools"] = list(self.allowed_tools)
        result["disclosed_metadata"] = dict(self.disclosed_metadata)
        return result


@dataclass(frozen=True)
class PackageRecord:
    package_id: str
    name: str
    version: str | None
    description: str
    content_hash: str
    source: SourceIdentity
    package_root: Path
    skills: tuple[SkillMetadata, ...]
    has_mcp: bool = False
    has_executable_extensions: bool = False
    origin_format: str = "agent-plugin"
    # Translation losses recorded at import (e.g. stripped MCP env values);
    # the untranslated original always survives in the vendor subtree.
    degradations: tuple[tuple[str, str, str], ...] = ()
    # Redacted client-side DLP findings (path, line, category, pattern,
    # severity) - a CONSENT requirement (`dlp-findings`), never a block:
    # import is the consumer's risk decision; the hosted publish gate stays hard.
    dlp_findings: tuple[dict[str, Any], ...] = ()
    # Safety provenance of a HOSTED install (listing_id, visibility,
    # owner_org_id) as delivered for THIS install; written onto the installed
    # state row atomically with it. None for non-hosted / cached loads.
    provenance: dict[str, Any] | None = None
    # Token of the pull operation installing this package: written onto the
    # installed row in the same write, so an interrupted pull can be
    # finished or rolled back. None outside a pull install.
    pull_op: str | None = None

    @property
    def has_dlp_findings(self) -> bool:
        return bool(self.dlp_findings)

    @property
    def has_hooks(self) -> bool:
        return any(item.has_hooks for item in self.skills)

    @property
    def has_skill_hook_extensions(self) -> bool:
        return any(item.has_hook_extensions for item in self.skills)

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(tool for item in self.skills for tool in item.allowed_tools)
        )

    @property
    def risky(self) -> bool:
        return (
            self.has_mcp
            or self.has_executable_extensions
            or self.has_hooks
            or self.has_skill_hook_extensions
            or any(item.has_scripts for item in self.skills)
        )

    def as_dict(self, *, include_path: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "package_id": self.package_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "content_hash": self.content_hash,
            "source": self.source.as_dict(),
            "skills": [item.as_dict() for item in self.skills],
            "has_mcp": self.has_mcp,
            "has_executable_extensions": self.has_executable_extensions,
            "has_hooks": self.has_hooks,
            "has_skill_hook_extensions": self.has_skill_hook_extensions,
            "allowed_tools": list(self.allowed_tools),
            "risky": self.risky,
            "origin_format": self.origin_format,
            "degradations": [
                {"component": item[0], "reason": item[1], "transform": item[2]}
                for item in self.degradations
            ],
            "dlp_findings": [dict(item) for item in self.dlp_findings],
        }
        if self.provenance is not None:
            result["provenance"] = dict(self.provenance)
        if self.pull_op is not None:
            result["pull_op"] = self.pull_op
        if include_path:
            result["package_root"] = str(self.package_root)
        return result


@dataclass(frozen=True)
class Emission:
    source: str
    destination: str
    output_hash: str
    # Emit-time text substitutions (placeholder -> replacement) applied to the
    # source bytes; `output_hash` is the hash of the SUBSTITUTED bytes.
    substitutions: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"source": self.source, "destination": self.destination, "output_hash": self.output_hash}
        if self.substitutions:
            result["substitutions"] = [list(item) for item in self.substitutions]
        return result

    def transform(self, raw: bytes) -> bytes:
        if not self.substitutions:
            return raw
        text = raw.decode("utf-8")
        for placeholder, replacement in self.substitutions:
            text = text.replace(placeholder, replacement)
        return text.encode("utf-8")


@dataclass(frozen=True)
class Degradation:
    component: str
    reason: str
    transform: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Omission:
    component: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class EmitPlan:
    package_id: str
    content_hash: str
    tool: str
    scope: str
    emissions: tuple[Emission, ...] = ()
    degradations: tuple[Degradation, ...] = ()
    omissions: tuple[Omission, ...] = ()
    consent_requirements: tuple[str, ...] = ()
    plan_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "content_hash": self.content_hash,
            "tool": self.tool,
            "scope": self.scope,
            "emissions": [item.as_dict() for item in self.emissions],
            "degradations": [item.as_dict() for item in self.degradations],
            "omissions": [item.as_dict() for item in self.omissions],
            "consent_requirements": list(self.consent_requirements),
            "plan_hash": self.plan_hash,
        }


@dataclass
class MarketplaceRegistry:
    registry_id: str
    alias: str
    scope: str
    source: str
    commit: str
    packages: list[dict[str, Any]] = field(default_factory=list)
    # Entries whose source could not be interpreted: recorded (name + reason),
    # excluded from install, never abort registration of the other packages.
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    # Top-level `renames` (old name -> new name) honored on lookup.
    renames: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
