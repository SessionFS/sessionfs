"""Org skills policy: schema, canonical form and evaluation (marketplace v2 §8.2).

Pure module shared by the server (enforcement on install / assign / load and
on the device reconcile listings) and the CLI (showing a policy and
explaining a refusal). No I/O, no server imports.

The policy is the v0.16 §7.4 block extended to ``policy_version`` 2. Every
revision is validated here, stored canonically and never edited in place.

Evaluation is deliberately monotonic: making any field of a policy more
restrictive can only add violations, never remove one (property-tested).
Mode order, least to most restrictive: open < allowlist < library_only <
disabled. In ``allowlist`` mode the org's own library packages are allowed
alongside the allowlist entries (publishing to the org library already
requires an org owner or admin).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from sessionfs.skills.models import SUPPORTED_TOOLS

POLICY_VERSION = 2
MODES = ("open", "allowlist", "library_only", "disabled")
MODE_RANK = {mode: rank for rank, mode in enumerate(MODES)}
# The scanner's severity scale (skills/instruction_scan.SEVERITY_RANK).
SEVERITIES = ("none", "low", "medium", "high", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

MAX_ALLOWLIST = 500
MAX_ORIGINS = 200
MAX_TRUSTED_PUBLISHERS = 100
MAX_POLICY_BYTES = 64 * 1024

_PUBLISHER_ID_RE = re.compile(r"^pub_[A-Za-z0-9_-]{1,60}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9._~:/@+-]{3,500}$")

# Extra policy targets beyond the nine capture tools: VS Code itself and
# Antigravity (D5, install-only, unverified formats).
POLICY_TOOLS = tuple(SUPPORTED_TOOLS) + ("vscode", "antigravity")
ToolEnforcement = Literal["managed", "report_only", "off"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AllowlistEntry(_Strict):
    """Exactly one of ``publisher`` (a verified publisher id) or ``source``
    (a git source, optionally pinned to a commit).

    A ``source`` entry matches ONLY verified provenance: the NUMERIC GitHub
    repository id and commit of a trusted-publishing run, taken from the
    GitHub OIDC token the server verified. ``source`` names the repository
    for people; ``repository_id`` is what matches (a name can be renamed,
    deleted and re-registered by someone else; the id is never reused). The
    server resolves ``repository_id`` from GitHub when a policy is written
    and refuses an entry it cannot resolve; an entry without one matches
    nothing. A package's declared ``source_locator`` / ``source_commit`` are
    free text any publisher sets, so they never match (fail closed)."""

    publisher: str | None = None
    require_attestation: bool = False
    source: str | None = None
    sha: str | None = None
    repository_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_kind(self) -> "AllowlistEntry":
        if (self.publisher is None) == (self.source is None):
            raise ValueError("an allowlist entry names exactly one of publisher or source")
        if self.publisher is not None:
            if not _PUBLISHER_ID_RE.fullmatch(self.publisher):
                raise ValueError("publisher must be a publisher id (pub_...)")
            if self.sha is not None:
                raise ValueError("sha applies to source entries only")
            if self.repository_id is not None:
                raise ValueError("repository_id applies to source entries only")
        if self.source is not None:
            if self.require_attestation:
                raise ValueError("require_attestation applies to publisher entries only")
            if not _SOURCE_RE.fullmatch(self.source) or normalize_source(self.source) == "":
                raise ValueError("source must be a git source such as github.com/acme/skills")
            sha = (self.sha or "any").strip().lower()
            if sha != "any" and not _SHA_RE.fullmatch(sha):
                raise ValueError("sha must be 'any' or a full 40-character hex commit")
            self.sha = sha
        return self


class ToolSettings(_Strict):
    # managed: compile the tool's native settings (default);
    # report_only: SessionFS only reports on this tool during reconcile;
    # off: no output and no reporting for this tool.
    enforcement: ToolEnforcement = "managed"


class SkillsPolicy(_Strict):
    policy_version: int = POLICY_VERSION
    mode: Literal["open", "allowlist", "library_only", "disabled"] = "open"
    allowlist: list[AllowlistEntry] = Field(default_factory=list, max_length=MAX_ALLOWLIST)
    allowed_publishers_verified_only: bool = True
    allow_hooks: bool = False
    allow_local_mcp: bool = False
    allow_remote_mcp: bool = True
    # Empty = any origin (when remote MCP is allowed at all).
    allowed_remote_mcp_origins: list[str] = Field(default_factory=list, max_length=MAX_ORIGINS)
    allow_skill_scripts: bool = False
    allow_hidden_unicode: bool = False
    require_signature: bool = False
    require_dlp_clean_import: bool = True
    # Highest instruction-scan severity a version may carry.
    max_scan_severity: Literal["none", "low", "medium", "high", "critical"] = "medium"
    auto_update: Literal["none", "non_widening"] = "none"
    trust_code_updates_publishers: list[str] = Field(
        default_factory=list, max_length=MAX_TRUSTED_PUBLISHERS
    )
    block_unmanaged_marketplaces: bool = False
    tools: dict[str, ToolSettings] = Field(default_factory=dict)

    @field_validator("policy_version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value not in (1, POLICY_VERSION):
            raise ValueError(f"policy_version must be 1 or {POLICY_VERSION}")
        return POLICY_VERSION  # v0.16 blocks are read as version 2

    @field_validator("allowed_remote_mcp_origins")
    @classmethod
    def _origins(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            origin = canonical_origin(value)
            if origin is None:
                raise ValueError(f"not an https origin: {value[:80]!r}")
            if origin not in cleaned:
                cleaned.append(origin)
        return sorted(cleaned)

    @field_validator("trust_code_updates_publishers")
    @classmethod
    def _publishers(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _PUBLISHER_ID_RE.fullmatch(value):
                raise ValueError("trust_code_updates_publishers holds publisher ids (pub_...)")
        return sorted(set(values))

    @field_validator("tools")
    @classmethod
    def _tools(cls, values: dict[str, ToolSettings]) -> dict[str, ToolSettings]:
        unknown = sorted(set(values) - set(POLICY_TOOLS))
        if unknown:
            raise ValueError(f"unknown tools: {', '.join(unknown)}")
        return dict(sorted(values.items()))

    def tool_enforcement(self, tool: str) -> str:
        settings = self.tools.get(tool)
        return settings.enforcement if settings else "managed"


class PolicyDocument(_Strict):
    skills: SkillsPolicy


class PolicyError(ValueError):
    """A policy document that does not validate (message is user-facing)."""


def canonical_origin(value: str) -> str | None:
    """``https://host[:port]`` lower-cased, or None when not an https origin."""
    if not isinstance(value, str) or len(value) > 300:
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if parts.path not in ("", "/"):
        return None
    host = parts.hostname.lower()
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


def github_repository(source: str) -> tuple[str, str] | None:
    """(owner, repo) for a ``github.com/owner/repo`` source, else None."""
    host, _, path = normalize_source(source).partition("/")
    parts = path.split("/")
    if host != "github.com" or len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def normalize_source(locator: str) -> str:
    """``host/path`` lower-cased without scheme, ``git@``, ``.git`` or
    trailing slashes, so ``https://github.com/Acme/skills.git``,
    ``git@github.com:acme/skills`` and ``github.com/acme/skills`` match."""
    value = (locator or "").strip()
    if "://" in value:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        path = parts.path
    else:
        match = re.match(r"^(?:[A-Za-z0-9._-]+@)?([A-Za-z0-9.-]+):(?!//)(.+)$", value)
        if match:
            host, path = match.group(1).lower(), match.group(2)
        else:
            host, _, path = value.partition("/")
            host = host.lower()
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/").lower()
    if not host or not path:
        return ""
    return f"{host}/{path}"


def parse_policy(document: Any) -> PolicyDocument:
    """Validate a policy document (``{"skills": {...}}``) or raise PolicyError."""
    if not isinstance(document, dict):
        raise PolicyError("the policy must be a JSON object with a 'skills' block")
    try:
        return PolicyDocument.model_validate(document)
    except ValidationError as exc:
        problems = []
        for error in exc.errors()[:10]:
            where = ".".join(str(part) for part in error.get("loc", ()))
            problems.append(f"{where or 'policy'}: {error.get('msg')}")
        raise PolicyError("; ".join(problems)) from None


def canonical_json(document: PolicyDocument) -> str:
    return json.dumps(document.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def policy_hash(document: PolicyDocument) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(document).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class PolicySubject:
    """What the policy is evaluated against: one immutable package version."""

    package_org_id: str | None
    org_id: str  # the org whose policy applies
    publisher_id: str | None = None
    publisher_verified: bool = False
    # VERIFIED provenance only (trusted publishing: the numeric repository
    # id and run commit from the server-verified GitHub OIDC token). Never
    # the package's self-declared source_locator / source_commit.
    verified_repository_id: int | None = None
    verified_commit: str | None = None
    # The capability manifest (skills/disclosure.compute_manifest), or None
    # when it is not available; capability ceilings then fail closed.
    manifest: dict[str, Any] | None = None
    attested: bool = False
    dlp_status: str = "passed"
    scan_max_severity: str = "none"


@dataclass
class Violation:
    code: str
    field: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message}


@dataclass
class Verdict:
    violations: list[Violation] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "violations": [v.as_dict() for v in self.violations]}


def _matches_allowlist(policy: SkillsPolicy, subject: PolicySubject) -> bool:
    repository_id = subject.verified_repository_id
    commit = (subject.verified_commit or "").lower()
    for entry in policy.allowlist:
        if entry.publisher is not None:
            if subject.publisher_id != entry.publisher:
                continue
            if policy.allowed_publishers_verified_only and not subject.publisher_verified:
                continue
            if entry.require_attestation and not subject.attested:
                continue
            return True
        elif (
            entry.source is not None
            and entry.repository_id is not None
            and repository_id is not None
            and entry.repository_id == repository_id
        ):
            # Exact match only: an abbreviated pin could be met by a commit
            # ground out to share its prefix.
            if entry.sha in (None, "any") or (commit and commit == entry.sha):
                return True
    return False


def _count(manifest: dict[str, Any], key: str) -> int:
    total = manifest.get(f"{key}_total")
    if isinstance(total, int):
        return total
    value = manifest.get(key)
    return len(value) if isinstance(value, list) else 0


def evaluate(policy: SkillsPolicy, subject: PolicySubject) -> Verdict:
    """Every way ``subject`` breaks ``policy`` (empty = allowed)."""
    verdict = Verdict()
    add = verdict.violations.append
    own_library = subject.package_org_id is not None and subject.package_org_id == subject.org_id

    if policy.mode == "disabled":
        add(Violation("skills_disabled", "mode", "Skills are disabled by your organization's policy."))
    elif policy.mode == "library_only" and not own_library:
        add(Violation(
            "not_in_org_library", "mode",
            "Only packages in your organization's own library are allowed.",
        ))
    elif policy.mode == "allowlist" and not own_library and not _matches_allowlist(policy, subject):
        add(Violation(
            "not_allowlisted", "allowlist",
            "This package's publisher or source is not on your organization's allowlist.",
        ))

    if policy.require_dlp_clean_import and subject.dlp_status != "passed":
        add(Violation("dlp_not_clean", "require_dlp_clean_import", "The package did not pass DLP."))
    if policy.require_signature and not subject.attested:
        add(Violation(
            "signature_required", "require_signature",
            "Your organization requires a signed (attested) release.",
        ))
    severity = subject.scan_max_severity if subject.scan_max_severity in SEVERITY_RANK else "critical"
    if SEVERITY_RANK[severity] > SEVERITY_RANK[policy.max_scan_severity]:
        add(Violation(
            "scan_severity", "max_scan_severity",
            f"The instruction scan found {severity}-severity patterns; your organization allows "
            f"up to {policy.max_scan_severity}.",
        ))

    restrictive_ceiling = not (
        policy.allow_hooks
        and policy.allow_local_mcp
        and policy.allow_remote_mcp
        and not policy.allowed_remote_mcp_origins
        and policy.allow_skill_scripts
        and policy.allow_hidden_unicode
    )
    manifest = subject.manifest
    if manifest is None or not isinstance(manifest, dict) or manifest.get("incomplete"):
        if restrictive_ceiling:
            add(Violation(
                "manifest_unavailable", "capabilities",
                "The package's capability manifest is not available, so the policy's capability "
                "limits cannot be checked.",
            ))
        return verdict

    hook_extensions = [
        item for item in manifest.get("executable_extensions") or []
        if isinstance(item, dict) and item.get("kind") in {"skill-hooks", "hooks"}
    ]
    other_extensions = [
        item for item in manifest.get("executable_extensions") or []
        if isinstance(item, dict) and item.get("kind") not in {"skill-hooks", "hooks"}
    ]
    if not policy.allow_hooks and (_count(manifest, "hooks") or hook_extensions):
        add(Violation("hooks_not_allowed", "allow_hooks", "The package installs hooks."))
    if not policy.allow_skill_scripts and (_count(manifest, "scripts") or other_extensions):
        add(Violation(
            "scripts_not_allowed", "allow_skill_scripts",
            "The package ships scripts or executable extensions.",
        ))
    remote = [item for item in manifest.get("mcp_remote") or [] if isinstance(item, dict)]
    local_count = _count(manifest, "mcp_local")
    if manifest.get("mcp_declared") and not local_count and not _count(manifest, "mcp_remote"):
        local_count = 1  # declared but not resolvable: treated as local (fail closed)
    if not policy.allow_local_mcp and local_count:
        add(Violation("local_mcp_not_allowed", "allow_local_mcp", "The package runs a local MCP server."))
    if remote or _count(manifest, "mcp_remote"):
        if not policy.allow_remote_mcp:
            add(Violation(
                "remote_mcp_not_allowed", "allow_remote_mcp", "The package connects to a remote MCP server."
            ))
        # Checked independently of allow_remote_mcp so tightening one field
        # never hides the other's violation (monotonic evaluation).
        if policy.allowed_remote_mcp_origins:
            allowed = set(policy.allowed_remote_mcp_origins)
            outside = sorted(
                {
                    str(item.get("origin"))
                    for item in remote
                    if canonical_origin(str(item.get("origin") or "")) not in allowed
                }
            )
            truncated = _count(manifest, "mcp_remote") > len(remote)
            if outside or truncated:
                add(Violation(
                    "remote_mcp_origin_not_allowed", "allowed_remote_mcp_origins",
                    "The package connects to MCP origins outside the allowed list"
                    + (f": {', '.join(outside[:5])}" if outside else "") + ".",
                ))
    if not policy.allow_hidden_unicode:
        counts = manifest.get("unicode_counts") or {}
        hidden = sum(v for v in counts.values() if isinstance(v, int)) if isinstance(counts, dict) else 0
        if hidden or manifest.get("unicode_truncated"):
            add(Violation(
                "hidden_unicode_not_allowed", "allow_hidden_unicode",
                "The package contains hidden or bidirectional Unicode characters.",
            ))
    return verdict


def refusal_message(org_name: str, verdict: Verdict) -> str:
    reasons = "; ".join(v.message for v in verdict.violations[:5])
    return (
        f"Blocked by the {org_name} skills policy: {reasons} "
        f"Ask an owner or admin of {org_name} to change the policy or approve another version."
    )


# ---------------------------------------------------------------------------
# Relaxation (used while an org is below the tier that includes the policy)
# ---------------------------------------------------------------------------

_TOOL_ENFORCEMENT_RANK = {"off": 0, "report_only": 1, "managed": 2}
# field -> the restrictive value of a boolean field.
_RESTRICTIVE_BOOLS = {
    "allowed_publishers_verified_only": True,
    "allow_hooks": False,
    "allow_local_mcp": False,
    "allow_remote_mcp": False,
    "allow_skill_scripts": False,
    "allow_hidden_unicode": False,
    "require_signature": True,
    "require_dlp_clean_import": True,
    "block_unmanaged_marketplaces": True,
}


def _entry_key(entry: AllowlistEntry) -> str:
    return json.dumps(entry.model_dump(mode="json"), sort_keys=True)


def tightened_fields(old: SkillsPolicy, new: SkillsPolicy) -> list[str]:
    """Fields on which ``new`` restricts more than ``old`` (empty = ``new``
    only relaxes or keeps ``old``). An org below the tier that includes the
    policy keeps its last revision enforced and may only publish revisions
    for which this is empty, down to a policy that restricts nothing."""
    tighter: list[str] = []
    if MODE_RANK[new.mode] > MODE_RANK[old.mode]:
        tighter.append("mode")
    if new.mode == "allowlist" and old.mode == "allowlist":
        if not {_entry_key(e) for e in old.allowlist} <= {_entry_key(e) for e in new.allowlist}:
            tighter.append("allowlist")
    for name, restrictive in _RESTRICTIVE_BOOLS.items():
        if getattr(new, name) == restrictive and getattr(old, name) != restrictive:
            tighter.append(name)
    old_origins, new_origins = set(old.allowed_remote_mcp_origins), set(new.allowed_remote_mcp_origins)
    if new_origins and (not old_origins or not old_origins <= new_origins):
        tighter.append("allowed_remote_mcp_origins")
    if SEVERITY_RANK[new.max_scan_severity] < SEVERITY_RANK[old.max_scan_severity]:
        tighter.append("max_scan_severity")
    if new.auto_update == "none" and old.auto_update != "none":
        tighter.append("auto_update")
    if not set(old.trust_code_updates_publishers) <= set(new.trust_code_updates_publishers):
        tighter.append("trust_code_updates_publishers")
    for tool in POLICY_TOOLS:
        if _TOOL_ENFORCEMENT_RANK[new.tool_enforcement(tool)] > _TOOL_ENFORCEMENT_RANK[old.tool_enforcement(tool)]:
            tighter.append("tools")
            break
    return tighter


# A policy that restricts nothing: what disabling the policy publishes.
UNRESTRICTED_POLICY: dict[str, Any] = {
    "skills": {
        "mode": "open",
        "allowed_publishers_verified_only": False,
        "allow_hooks": True,
        "allow_local_mcp": True,
        "allow_remote_mcp": True,
        "allowed_remote_mcp_origins": [],
        "allow_skill_scripts": True,
        "allow_hidden_unicode": True,
        "require_signature": False,
        "require_dlp_clean_import": False,
        "max_scan_severity": "critical",
        "block_unmanaged_marketplaces": False,
    }
}


def unrestricted_from(old: SkillsPolicy | None) -> PolicyDocument:
    """The "disabled" revision: restricts nothing, and keeps ``old``'s
    update settings (auto_update, trusted code-update publishers) and
    per-tool output settings so it is never a tightening of ``old``."""
    raw = json.loads(json.dumps(UNRESTRICTED_POLICY))
    if old is not None:
        raw["skills"]["auto_update"] = old.auto_update
        raw["skills"]["trust_code_updates_publishers"] = list(old.trust_code_updates_publishers)
        raw["skills"]["tools"] = {name: t.model_dump(mode="json") for name, t in old.tools.items()}
    return parse_policy(raw)
