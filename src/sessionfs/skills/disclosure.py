"""Install disclosure, capability manifests, update diffs and re-consent.

Marketplace v2 §3 (tk_d8bc543da8344124). One module, used unchanged by the
CLI, the daemon, the MCP preview and the server, so every surface shows the
same card and makes the same re-consent decision from the same bytes.

* ``compute_manifest(root)`` derives a deterministic **capability manifest**
  from a normalized package tree (the content-store ``package`` directory on a
  device, the extracted canonical archive on the server). It is byte-derived
  and never read from ``sfsk.json`` / ``plugin.json`` claims.
* ``capability_digest(manifest)``: two versions with the same digest grant the
  same access.
* ``build_disclosure(manifest, tools=...)`` is the card: per target tool, the
  files that land on disk, what can run, MCP servers, tool pre-approvals,
  network endpoints, shell commands the instructions tell the agent to run,
  scanner findings and hidden Unicode.
* ``classify_update(old, new)`` is the D2 update classifier (CEO decision,
  marketplace v2 §11): ``text`` updates need one confirmation, ``code``
  updates (script / hook / MCP-config / executable-extension bytes) need
  approval again, ``widening`` updates (new capability, hook event, MCP
  server, network endpoint, environment variable, tool pre-approval, or a
  trust downgrade) always stop and ask. An unknown baseline is widening
  (fail closed).
* ``trusted_code_update_allowed`` is the policy hook for the Enterprise
  exception ("an org that trusts the publisher's signed release workflow may
  auto-apply code updates"). It returns False until verified publishers and
  trusted publishing (tk_a287f7d654f64b1b) provide provenance.

Hidden / bidirectional Unicode and control characters are detected by the
instruction scanner (``instruction_scan``), never here: this module only
renders the scanner's findings with the code point made visible.
"""

from __future__ import annotations

import difflib
import hashlib
import ipaddress
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit

import yaml

from sessionfs.skills.instruction_scan import (
    SEVERITY_RANK,
    ScanResult,
    resolve_command,
    scan_tree,
)
from sessionfs.skills.models import SUPPORTED_TOOLS, clean_untrusted_text
from sessionfs.skills.shell_lex import ShellLexer, walk_lists

# Bump when the manifest shape or derivation changes. A stored manifest with a
# different version is an UNKNOWN baseline (updates from it are widening).
MANIFEST_VERSION = 1
# The re-consent rule in force when a receipt was written: 1 = v0.16 §7.3
# (any byte change resets consent); 2 = marketplace v2 D2 (Chrome model).
CONSENT_RULE_VERSION = 2

UPDATE_CLASSES = ("unchanged", "text", "code", "widening")
# Bump when the rendered disclosure card's shape changes (it is in the
# disclosure digest approvals bind to).
CARD_VERSION = 1

# Display / storage bounds (the manifest is persisted on the server and on the
# installed state row; every list is bounded and sorted).
MAX_ENDPOINTS = 200  # display only (the card); the manifest keeps the full set
MAX_SHELL_COMMANDS = 200
MAX_UNICODE_FINDINGS = 200
MAX_WRITES_SHOWN = 200
MAX_LIST_ITEMS = 2_000  # display lists (skill files, assets)
# Security sets (the access a version grants) are stored in full up to this
# many entries; beyond it the stored list is truncated for size, but the set
# digest and total still cover EVERY entry, and any change to a set that does
# not fit is treated as widening (fail closed).
SECURITY_SET_CAP = 5_000
SECURITY_SETS = (
    "scripts", "hooks", "mcp_local", "mcp_remote", "allowed_tools", "network_endpoints",
    "env_names", "executable_extensions", "persistence_targets",
)
TEXT_DIFF_BUDGET = 256 * 1024
_MAX_DIFF_INPUT_LINES = 20_000
_MAX_TEXT_BYTES = 16 * 1024 * 1024

# Requirement names a plan / receipt uses for each consent-gated capability.
REQUIREMENT_FOR_CODE_KIND = {
    "script": "skill-scripts",
    "hook": "skill-hooks",
    "executable_extension": "executable-extensions",
    "mcp": "mcp",
}

_PROJECT_ROOTS: dict[str, str] = {}
_USER_ROOTS: dict[str, str] = {}

# Any URL authority (bracketed IPv6 or anything up to a delimiter). Hosts are
# canonicalized afterwards; nothing is dropped for its shape (single-label,
# internal names and IP literals are endpoints too).
_URL_RE = re.compile(
    r"\b(?:https?|wss?|ftps?|grpcs?)://(\[[0-9A-Fa-f:.%]{2,64}\]|[^\s/?#\"'<>()\[\]{}`|\\^,;*]{1,300})",
    re.IGNORECASE,
)
_SHELL_INFO = {"", "sh", "bash", "shell", "zsh", "console", "terminal", "fish", "ksh", "shell-session"}
_POWERSHELL_INFO = {"powershell", "ps1", "pwsh", "ps"}
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s{0,8}([A-Za-z0-9_+.-]{0,40})")
_INTERPRETER_BY_SUFFIX = {
    ".sh": "sh", ".bash": "bash", ".zsh": "zsh", ".fish": "fish", ".ksh": "ksh",
    ".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node", ".ts": "node",
    ".ps1": "powershell", ".psm1": "powershell", ".bat": "cmd", ".cmd": "cmd",
    ".rb": "ruby", ".pl": "perl", ".php": "php", ".lua": "lua",
}
_VENDOR_PREFIX = "io.sessionfs.portability/vendor/"
_MANIFEST_NAMES = {"plugin.json", "gemini-extension.json"}


def _roots() -> tuple[dict[str, str], dict[str, str]]:
    # Imported lazily: adapters imports package, which is heavy but shared.
    if not _PROJECT_ROOTS:
        from sessionfs.skills.adapters import PROJECT_SKILL_ROOTS, USER_SKILL_ROOTS

        _PROJECT_ROOTS.update(PROJECT_SKILL_ROOTS)
        _USER_ROOTS.update(USER_SKILL_ROOTS)
    return _PROJECT_ROOTS, _USER_ROOTS


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _clean(value: Any, limit: int = 200) -> str:
    return clean_untrusted_text(value, limit=limit, keep_newlines=False)


# ---------------------------------------------------------------------------
# Manifest derivation
# ---------------------------------------------------------------------------


def _frontmatter(text: str) -> dict[str, Any]:
    """SKILL.md frontmatter, split exactly like package.validate_skill_dir
    (LF only, exact column-0 closer) and loaded with the same strict loader.
    The package was already validated; a failure here yields {}."""
    from sessionfs.skills.package import _UniqueSafeLoader

    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].rstrip(" \t") != "---":
        return {}
    end = next((i for i, line in enumerate(lines[1:], start=1) if line == "---"), None)
    if end is None:
        return {}
    try:
        value = yaml.load("\n".join(lines[1:end]), Loader=_UniqueSafeLoader)  # nosec B506
    except (yaml.YAMLError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _hook_entries(skill: str, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per (event, matcher) of a skill's frontmatter ``hooks``. The
    digest covers the whole definition under that event, so any change to a
    hook command is a CODE change even when the event and matcher stay."""
    value = metadata.get("hooks")
    if value in (None, "", [], {}):
        return []
    entries: list[dict[str, Any]] = []
    if not isinstance(value, dict):
        return [{"skill": skill, "event": _clean(str(type(value).__name__), 64), "matcher": "*",
                 "digest": _sha(_canonical(value if isinstance(value, (list, str)) else str(value)))}]
    for event, definition in value.items():
        event_name = _clean(event, 64)
        digest = _sha(_canonical(definition))
        matchers: list[str] = []
        if isinstance(definition, list):
            for item in definition:
                if isinstance(item, dict):
                    matcher = item.get("matcher")
                    matchers.append(_clean(matcher, 100) if isinstance(matcher, str) and matcher else "*")
        if not matchers:
            matchers = ["*"]
        for matcher in sorted(set(matchers)):
            entries.append({"skill": skill, "event": event_name, "matcher": matcher, "digest": digest})
    return entries


def _interpreter(path: Path, relative: str) -> str:
    suffix = PurePosixPath(relative).suffix.casefold()
    try:
        with path.open("rb") as handle:
            head = handle.read(128)
    except OSError:
        head = b""
    if head.startswith(b"#!"):
        line = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").strip()
        words = line.split()
        if words:
            name = words[0].rsplit("/", 1)[-1]
            if name == "env":
                rest = [word for word in words[1:] if not word.startswith("-") and "=" not in word]
                name = rest[0].rsplit("/", 1)[-1] if rest else name
            return _clean(name, 40) or "unknown"
    return _INTERPRETER_BY_SUFFIX.get(suffix, "unknown")


def canonical_host(authority: str) -> str | None:
    """Canonical host of a URL authority: userinfo and port removed,
    lowercased, trailing dot removed, IP literals normalized, IDNA names in
    their ASCII form. Single-label and internal names are kept."""
    host = authority.rsplit("@", 1)[-1].strip()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    host = host.strip().rstrip(".").casefold()
    if not host:
        return None
    try:
        return str(ipaddress.ip_address(host.split("%", 1)[0]))
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return _clean(host, 253) or None


def _hosts_in(text: str) -> set[str]:
    hosts: set[str] = set()
    for match in _URL_RE.finditer(text):
        host = canonical_host(match.group(1))
        if host:
            hosts.add(host)
    return hosts


def _origin(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme.casefold()}://{parts.hostname.casefold()}{port}"


_MCP_DECLARATION_NAMES = {"mcp.json", ".mcp.json", "gemini-extension.json", "plugin.json"}


def _mcp_config_digest(root: Path, files: dict[str, str]) -> str | None:
    """Digest of every MCP declaration in the tree (portable mcp.json, vendor
    .mcp.json / mcp.json, and the mcpServers of plugin / extension
    manifests). Any change is treated as widening: the device never sees MCP
    activation, so a reviewer must."""
    parts: list[tuple[str, Any]] = []
    for relative in sorted(files):
        name = PurePosixPath(relative).name.casefold()
        if name not in _MCP_DECLARATION_NAMES:
            continue
        try:
            raw = (root / relative).read_bytes()
        except OSError:
            parts.append((relative, files[relative]))
            continue
        if name in {"plugin.json", "gemini-extension.json"}:
            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                parts.append((relative, files[relative]))
                continue
            servers = document.get("mcpServers") if isinstance(document, dict) else None
            if servers not in (None, "", [], {}):
                parts.append((relative, servers))
        else:
            parts.append((relative, files[relative]))
    return _sha(_canonical(parts)) if parts else None


def _mcp_entries(root: Path) -> tuple[list[dict], list[dict], list[str]]:
    path = root / "mcp.json"
    if not path.is_file() or path.is_symlink():
        return [], [], []
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return [], [{"name": "(unparsed)", "origin": _sha(raw), "transport": "unknown"}], []
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    local: list[dict] = []
    remote: list[dict] = []
    env_names: set[str] = set()
    if isinstance(servers, dict):
        for name, server in servers.items():
            if not isinstance(server, dict):
                continue
            server_name = _clean(name, 100)
            env = server.get("env")
            names = sorted(_clean(key, 100) for key in env) if isinstance(env, dict) else []
            env_names.update(names)
            url = server.get("url")
            if isinstance(url, str) and url:
                # A remote server's identity is (name, origin, transport): a
                # new identity is new access. An unparseable URL fails closed
                # to an identity that changes with its exact text.
                raw_transport = server.get("transport") or server.get("type") or "http"
                remote.append(
                    {
                        "name": server_name,
                        "origin": _origin(url) or "unparsed:" + _sha(url.encode("utf-8", "surrogatepass")),
                        "transport": _clean(raw_transport, 40) if isinstance(raw_transport, str) else "unknown",
                    }
                )
                continue
            command = server.get("command")
            raw_args = server.get("args")
            args: list[Any] = raw_args if isinstance(raw_args, list) else []
            local.append(
                {
                    "name": server_name,
                    "command": _clean(command, 200) if isinstance(command, str) else "",
                    "args_digest": _sha(_canonical([str(item) for item in args])),
                    "env_names": names,
                }
            )
    return (
        sorted(local, key=lambda item: item["name"]),
        sorted(remote, key=lambda item: (item["name"], item["origin"], item["transport"])),
        sorted(env_names),
    )


def _manifest_hooks(root: Path, relative: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """(executable-extension entry, hook identities) for a manifest's inline
    ``hooks`` value; None when it declares none."""
    try:
        document = json.loads((root / relative).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    value = document.get("hooks") if isinstance(document, dict) else None
    if value in (None, "", [], {}):
        return None
    canonical = _canonical(value)
    label = f"{relative}#hooks"
    entry = {"path": label, "kind": "manifest-hooks", "sha256": _sha(canonical)}
    identities = (
        _hooks_json_entries(label, canonical)
        if isinstance(value, dict)
        else [{"skill": label, "event": "(reference)", "matcher": _clean(value, 200), "digest": _sha(canonical)}]
    )
    return entry, identities


def _hooks_json_entries(relative: str, raw: bytes) -> list[dict[str, Any]]:
    """Canonical (event, matcher) identities of a hooks.json, the same shape
    as frontmatter hooks (``{"hooks": {Event: [{matcher, hooks}]}}`` or the
    event map itself). Malformed or unsupported definitions fail closed: one
    ``(unparsed)`` identity keyed by the file's bytes, so any change to it
    is new access."""
    digest = _sha(raw)
    unparsed = [{"skill": relative, "event": "(unparsed)", "matcher": digest, "digest": digest}]
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return unparsed
    if isinstance(document, dict) and isinstance(document.get("hooks"), dict):
        events = document["hooks"]
    elif isinstance(document, dict) and all(isinstance(value, list) for value in document.values()):
        events = document
    else:
        return unparsed
    entries: list[dict[str, Any]] = []
    for event, definition in events.items():
        if not isinstance(definition, list) or not all(isinstance(item, dict) for item in definition):
            return unparsed
        matchers = sorted(
            {
                _clean(item.get("matcher"), 100)
                if isinstance(item.get("matcher"), str) and item.get("matcher")
                else "*"
                for item in definition
            }
            or {"*"}
        )
        for matcher in matchers:
            entries.append(
                {"skill": relative, "event": _clean(event, 64), "matcher": matcher, "digest": _sha(_canonical(definition))}
            )
    return entries


def _fenced_shell_commands(path: str, text: str) -> list[dict[str, Any]]:
    """Commands in fenced shell blocks of an instruction file: what the
    instructions tell the agent to run. Command names come from the
    instruction scanner's own shell lexer and wrapper resolution."""
    found: list[dict[str, Any]] = []
    fence: str | None = None
    info = ""
    for number, line in enumerate(text.split("\n"), start=1):
        match = _FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)[0] * 3
                info = match.group(2).casefold()
            continue
        if line.lstrip().startswith(fence):
            fence = None
            continue
        if info not in _SHELL_INFO and info not in _POWERSHELL_INFO:
            continue
        body = line.strip()
        if info in {"console", "terminal", "shell-session"}:
            if not body.startswith(("$ ", "# ", "% ", "> ")):
                continue
        if body.startswith(("$ ", "% ")):
            body = body[2:].strip()
        if not body or body.startswith("#"):
            continue
        names = _command_names(body)
        if not names:
            continue
        found.append(
            {
                "path": path,
                "line": number,
                "command": names[0],
                "commands": names[:10],
                "text": _clean(body, 160),
            }
        )
        if len(found) >= MAX_SHELL_COMMANDS:
            break
    return found


def _command_names(statement: str) -> list[str]:
    names: list[str] = []
    try:
        lexer = ShellLexer(statement, comments=True)
        root, _end = lexer.statement(0)
        for command_list, quoted in walk_lists(root):
            if quoted:
                continue
            for pipeline in command_list.pipelines:
                for command in pipeline.commands:
                    words = [word.value for word in command.words if word.value]
                    if not words:
                        continue
                    resolved = resolve_command(words)
                    if resolved is None:
                        continue
                    name = _clean(resolved[0], 60)
                    if name and name not in names:
                        names.append(name)
    except Exception:  # noqa: BLE001 - a lexer failure must never block disclosure
        return names
    return names


def _code_point_at(root: Path, relative: str, line: int, column: int) -> str | None:
    try:
        text = (root / relative).read_bytes()[:_MAX_TEXT_BYTES].decode("utf-8", "replace")
    except OSError:
        return None
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        return None
    row = lines[line - 1]
    if not 1 <= column <= len(row):
        return None
    return f"U+{ord(row[column - 1]):04X}"


def unicode_findings(scan: ScanResult, root: Path | None = None) -> list[dict[str, Any]]:
    """The scanner's hidden-Unicode / control-character findings, with the code
    point made visible (``⟦U+202E⟧``) when the tree is available."""
    result: list[dict[str, Any]] = []
    # Worst first, so a critical finding is never crowded out by the cap.
    ordered = sorted(
        (finding for finding in scan.findings if finding.family == "hidden_unicode"),
        key=lambda item: (-SEVERITY_RANK[item.severity], item.path, item.line, item.column),
    )
    for finding in ordered:
        code_point = _code_point_at(root, finding.path, finding.line, finding.column) if root else None
        result.append(
            {
                "path": finding.path,
                "line": finding.line,
                "column": finding.column,
                "rule_id": finding.rule_id,
                "class": finding.rule_id.split(".", 1)[-1],
                "severity": finding.severity,
                "code_point": code_point,
                "visible": f"⟦{code_point}⟧" if code_point else None,
            }
        )
        if len(result) >= MAX_UNICODE_FINDINGS:
            break
    return result


def _unicode_counts(scan: ScanResult) -> dict[str, int]:
    counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for finding in scan.findings:
        if finding.family == "hidden_unicode":
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


def _per_file_security(root: Path, files: dict[str, str]) -> tuple[set[str], dict[str, int], bool]:
    """Persistence rule ids and hidden-character counts from one bounded scan
    per text file (the package-wide list may have been truncated). Neither
    depends on cross-file severity escalation. Returns (rules, counts,
    truncated) where truncated means some single file overflowed."""
    from sessionfs.skills.instruction_scan import scan_files
    from sessionfs.skills.package import looks_binary

    rules: set[str] = set()
    counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    truncated = False
    for relative in sorted(files):
        try:
            raw = (root / relative).read_bytes()
        except OSError:
            truncated = True
            continue
        if looks_binary(raw):
            continue
        result = scan_files([(relative, raw.decode("utf-8", errors="replace"))])
        truncated = truncated or result.truncated
        rules.update(f.rule_id for f in result.findings if f.family == "persistence")
        for severity, value in _unicode_counts(result).items():
            counts[severity] = counts.get(severity, 0) + value
    return rules, counts, truncated


def _bounded_set(manifest: dict[str, Any], field: str, items: list[Any]) -> None:
    """Store a security set: the full list up to SECURITY_SET_CAP entries,
    plus a total and digests over EVERY entry (the access digest ignores
    per-entry hashes; the content digest includes them)."""
    manifest[field] = items[:SECURITY_SET_CAP]
    manifest[f"{field}_total"] = len(items)
    manifest[f"{field}_set_digest"] = _sha(_canonical(_strip_hashes(items)))
    manifest[f"{field}_content_digest"] = _sha(_canonical(items))


def compute_manifest(root: Path, *, scan: ScanResult | None = None) -> dict[str, Any]:
    """Deterministic capability manifest of a normalized package tree.

    ``root`` is the package directory (``plugin.json``, ``skills/``,
    ``mcp.json``, ``io.sessionfs.portability/vendor``). The scan is run when
    not supplied. The result is JSON-serializable and bounded.
    """
    from sessionfs.skills.package import (
        AGENT_MCP_SCHEMA,
        AGENT_PLUGIN_SCHEMA,
        _aggregate_integrity,
        _compute_risk,
        _validated_skills,
        is_inert_asset,
        looks_binary,
    )

    root = Path(root)
    skills = _validated_skills(root)
    has_mcp, has_package_executables = _compute_risk(root)
    _aggregate, files = _aggregate_integrity(root)
    if scan is None:
        scan = scan_tree(root)

    skill_entries: list[dict[str, Any]] = []
    assets: list[str] = []
    scripts: list[dict[str, Any]] = []
    hooks: list[dict[str, Any]] = []
    executable_extensions: list[dict[str, Any]] = []
    allowed_tools: set[str] = set()
    shell_commands: list[dict[str, Any]] = []
    skill_dirs = {skill.relative_dir.strip("/") + "/": skill for skill in skills}

    for skill in skills:
        prefix = skill.relative_dir.strip("/") + "/"
        members = sorted(path[len(prefix):] for path in files if path.startswith(prefix))
        skill_entries.append(
            {"name": skill.name, "dir": skill.relative_dir, "files": members[:MAX_LIST_ITEMS], "files_total": len(members)}
        )
        allowed_tools.update(skill.allowed_tools)
        try:
            text = (root / prefix / "SKILL.md").read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        hooks.extend(_hook_entries(skill.name, _frontmatter(text)))

    for relative in sorted(files):
        owner = next((skill for prefix, skill in skill_dirs.items() if relative.startswith(prefix)), None)
        digest = files[relative]
        if owner is not None:
            inner = PurePosixPath(relative[len(owner.relative_dir.strip("/")) + 1:])
            parts = tuple(part.casefold() for part in inner.parts)
            if parts and parts[0] in {"hooks.json", "hooks"}:
                executable_extensions.append({"path": relative, "kind": "skill-hooks", "sha256": digest})
                if parts[-1] == "hooks.json":
                    hooks.extend(_hooks_json_entries(relative, (root / relative).read_bytes()))
                continue
            if inner.name == "SKILL.md":
                continue
            path = root / relative
            try:
                with path.open("rb") as handle:
                    shebang = handle.read(2) == b"#!"
            except OSError:
                shebang = False
            if "scripts" in parts or shebang or not is_inert_asset(inner.name):
                scripts.append(
                    {"path": relative, "interpreter": _interpreter(path, relative), "sha256": digest}
                )
            else:
                assets.append(relative)
            continue
        parts = tuple(part.casefold() for part in PurePosixPath(relative).parts)
        if parts and parts[-1] in _MANIFEST_NAMES:
            # Inline hook definitions in a (vendor) plugin manifest: hashed as
            # their own executable extension, their events as hook identities.
            entry = _manifest_hooks(root, relative)
            if entry is not None:
                executable_extensions.append(entry[0])
                hooks.extend(entry[1])
        if relative.startswith(_VENDOR_PREFIX):
            # The untranslated native original: its executable members are
            # recorded with their real path, kind and hash below, like root
            # ones (never an unhashed placeholder).
            parts = tuple(part.casefold() for part in PurePosixPath(relative[len(_VENDOR_PREFIX):]).parts[1:])
        if any(part in {"hooks", "commands", "agents", "scripts", "bin", "executables"} for part in parts[:-1]) or (
            parts and parts[-1] == "hooks.json"
        ):
            kind = next(
                (part for part in parts if part in {"hooks", "commands", "agents", "scripts", "bin", "executables"}),
                "hooks",
            )
            executable_extensions.append({"path": relative, "kind": kind, "sha256": digest})
            if parts and parts[-1] == "hooks.json":
                hooks.extend(_hooks_json_entries(relative, (root / relative).read_bytes()))

    if has_package_executables and not any(item["kind"] != "skill-hooks" for item in executable_extensions):
        # Declared in a manifest or the vendor subtree rather than shipped as a
        # root file: still an executable extension, disclosed as such.
        executable_extensions.append({"path": "(declared)", "kind": "declared", "sha256": ""})

    mcp_local, mcp_remote, env_names = _mcp_entries(root)
    mcp_config_digest = _mcp_config_digest(root, files)
    incomplete: list[str] = []

    hosts: set[str] = set()
    for relative in sorted(files):
        try:
            raw = (root / relative).read_bytes()
        except OSError:
            incomplete.append(f"unreadable:{relative}")
            continue
        if looks_binary(raw):
            continue
        if len(raw) > _MAX_TEXT_BYTES:
            incomplete.append(f"too_large:{relative}")
            continue
        # Every text member, executable or not, invalid bytes replaced (a
        # stray byte must never hide a URL).
        text = raw.decode("utf-8", "replace")
        # The portable format's own JSON-schema identifiers are not endpoints.
        hosts.update(_hosts_in(text.replace(AGENT_PLUGIN_SCHEMA, "").replace(AGENT_MCP_SCHEMA, "")))
        if (
            relative.startswith("skills/")
            and PurePosixPath(relative).suffix.casefold() in {".md", ".markdown", ".mdx", ".txt"}
            and len(shell_commands) < MAX_SHELL_COMMANDS
        ):
            shell_commands.extend(_fenced_shell_commands(relative, text))
    for item in mcp_remote:
        host = urlsplit(item["origin"]).hostname if "://" in item["origin"] else None
        if host:
            hosts.add(canonical_host(host) or host)
    persistence_rules = {f.rule_id for f in scan.findings if f.family == "persistence"}
    unicode_counts = _unicode_counts(scan)
    unicode_truncated = bool(scan.truncated)
    if scan.truncated:
        # The package-wide finding list is bounded. The security parts derived
        # from it (persistence targets, hidden-character counts) are rebuilt
        # from one scan per file, each with its own bound; only a single file
        # that overflows its own scan leaves the manifest incomplete.
        persistence_rules, unicode_counts, unicode_truncated = _per_file_security(root, files)
        if unicode_truncated:
            incomplete.append("scan_truncated")
    persistence = sorted(persistence_rules)
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "instructions": bool(skills),
        "skills": skill_entries,
        "assets": assets[:MAX_LIST_ITEMS],
        "assets_total": len(assets),
        "mcp_declared": bool(has_mcp),
        "mcp_config_digest": mcp_config_digest,
        "incomplete": sorted(incomplete),
        "shell_commands": shell_commands[:MAX_SHELL_COMMANDS],
        "unicode_findings": unicode_findings(scan, root),
        "unicode_counts": unicode_counts,
        "unicode_truncated": unicode_truncated,
        "scan": {
            "ruleset": scan.ruleset_version,
            "max_severity": scan.max_severity,
            "finding_count": len(scan.findings),
            "counts": scan.counts(),
            "truncated": bool(scan.truncated),
        },
        "files": dict(sorted(files.items())),
    }
    _bounded_set(manifest, "scripts", scripts)
    _bounded_set(manifest, "hooks", sorted(hooks, key=lambda item: (item["skill"], item["event"], item["matcher"])))
    _bounded_set(manifest, "mcp_local", mcp_local)
    _bounded_set(manifest, "mcp_remote", mcp_remote)
    _bounded_set(manifest, "allowed_tools", sorted(allowed_tools))
    _bounded_set(manifest, "network_endpoints", sorted(hosts))
    _bounded_set(manifest, "env_names", env_names)
    _bounded_set(manifest, "executable_extensions", executable_extensions)
    _bounded_set(manifest, "persistence_targets", persistence)
    return manifest


# ---------------------------------------------------------------------------
# Digest, capability set, widening
# ---------------------------------------------------------------------------

# Display-only parts: never part of the access a version grants.
_DISPLAY_KEYS = (
    "files", "skills", "assets", "assets_total", "shell_commands", "unicode_findings", "scan",
    "unicode_counts", "unicode_truncated",
)


def access_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """The manifest minus display-only parts (still with per-entry hashes):
    what an installed row stores as the consented baseline."""
    return {key: value for key, value in manifest.items() if key not in _DISPLAY_KEYS}


def _strip_hashes(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_hashes(item) for item in value]
    if isinstance(value, dict):
        return {
            k: _strip_hashes(v)
            for k, v in value.items()
            if k not in {"sha256", "digest"} and not k.endswith("_content_digest")
        }
    return value


def capability_digest(manifest: dict[str, Any]) -> str:
    """sha256 of the canonical access manifest without per-file hashes."""
    return _sha(_canonical(_strip_hashes(access_manifest(manifest))))


def capability_set(manifest: dict[str, Any]) -> list[str]:
    """The capability classes a version uses (for badges and receipts)."""
    classes: list[str] = []
    if manifest.get("instructions"):
        classes.append("instructions")
    if manifest.get("assets") or manifest.get("assets_total"):
        classes.append("assets")
    if manifest.get("scripts"):
        classes.append("scripts")
    if manifest.get("hooks"):
        classes.append("hooks")
    if any(str(item).startswith("allowed-tools:") for item in manifest.get("allowed_tools") or []):
        classes.append("allowed_tools")
    if manifest.get("mcp_local") or manifest.get("mcp_remote") or manifest.get("mcp_declared"):
        classes.append("mcp")
    if manifest.get("executable_extensions"):
        classes.append("executable_extensions")
    if manifest.get("network_endpoints"):
        classes.append("network")
    if manifest.get("env_names"):
        classes.append("env")
    if manifest.get("persistence_targets"):
        classes.append("persistence")
    return classes


# Classes whose first appearance widens access (instructions/assets do not).
_WIDENING_CLASSES = {
    "scripts", "hooks", "allowed_tools", "mcp", "executable_extensions", "network", "env", "persistence",
}


def _keys(manifest: dict[str, Any], field: str, key: Any) -> dict[Any, dict]:
    result: dict[Any, dict] = {}
    for item in manifest.get(field) or []:
        if isinstance(item, dict):
            result[key(item)] = item
    return result


def _hook_key(item: dict) -> tuple[str, str, str]:
    return (str(item.get("skill")), str(item.get("event")), str(item.get("matcher")))


def _remote_key(item: dict) -> tuple[str, str, str]:
    return (str(item.get("name")), str(item.get("origin")), str(item.get("transport") or "http"))


def _complete(manifest: dict[str, Any], field: str) -> bool:
    total = manifest.get(f"{field}_total")
    return total is None or total == len(manifest.get(field) or [])


def widens(old: dict[str, Any], new: dict[str, Any], *, old_provenance: dict | None = None,
           new_provenance: dict | None = None) -> list[dict[str, str]]:
    """Every way ``new`` grants more than ``old`` (marketplace v2 §3.2).
    Reordering, duplicates and narrowing never widen."""
    found: list[dict[str, str]] = []

    def add(kind: str, item: str) -> None:
        entry = {"kind": kind, "item": _clean(item, 300)}
        if entry not in found:
            found.append(entry)

    for capability in sorted(set(capability_set(new)) - set(capability_set(old))):
        if capability in _WIDENING_CLASSES:
            add("new_capability", capability)
    for field in SECURITY_SETS:
        # A set that does not fit in the stored list is compared by digest
        # only: any change to it is widening.
        if not (_complete(old, field) and _complete(new, field)) and (
            old.get(f"{field}_set_digest") != new.get(f"{field}_set_digest")
        ):
            add("security_set_changed_beyond_cap", field)
    if new.get("mcp_config_digest") and new.get("mcp_config_digest") != old.get("mcp_config_digest"):
        add("changed_mcp_config", "MCP configuration")
    old_hooks = _keys(old, "hooks", _hook_key)
    for key in sorted(_keys(new, "hooks", _hook_key)):
        if key not in old_hooks:
            add("new_hook", f"{key[0]}: {key[1]} ({key[2]})")
    old_local = _keys(old, "mcp_local", lambda item: str(item.get("name")))
    for name, item in sorted(_keys(new, "mcp_local", lambda item: str(item.get("name"))).items()):
        before = old_local.get(name)
        if before is None:
            add("new_mcp_server", f"{name}: {item.get('command')}")
        elif before.get("command") != item.get("command") or before.get("args_digest") != item.get("args_digest"):
            add("changed_mcp_command", f"{name}: {before.get('command')} -> {item.get('command')}")
    old_remote = set(_keys(old, "mcp_remote", _remote_key))
    for key in sorted(_keys(new, "mcp_remote", _remote_key)):
        if key not in old_remote:
            add("new_mcp_remote_server", f"{key[0]}: {key[1]} ({key[2]})")
    old_tools = set(old.get("allowed_tools") or [])
    new_tools = set(new.get("allowed_tools") or [])
    for entry in sorted(new_tools - old_tools):
        if entry.startswith("allowed-tools:"):
            add("new_allowed_tool", entry.split(":", 1)[1])
    for entry in sorted(old_tools - new_tools):
        if entry.startswith("disallowed-tools:"):
            add("removed_tool_restriction", entry.split(":", 1)[1])
    for host in sorted(set(new.get("network_endpoints") or []) - set(old.get("network_endpoints") or [])):
        add("new_network_endpoint", host)
    for name in sorted(set(new.get("env_names") or []) - set(old.get("env_names") or [])):
        add("new_env_name", name)
    old_ext = _keys(old, "executable_extensions", lambda item: str(item.get("path")))
    for path in sorted(_keys(new, "executable_extensions", lambda item: str(item.get("path")))):
        if path not in old_ext:
            add("new_executable_extension", path)
    for target in sorted(set(new.get("persistence_targets") or []) - set(old.get("persistence_targets") or [])):
        add("new_persistence_target", target)
    old_trust = old_provenance or {}
    new_trust = new_provenance or {}
    for flag in ("publisher_verified", "attested"):
        if old_trust.get(flag) and not new_trust.get(flag):
            add("trust_downgrade", flag)
    return found


def narrows(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, str]]:
    """Access ``new`` no longer grants (informational)."""
    found = widens(new, old)
    return [{"kind": entry["kind"].replace("new_", "removed_", 1), "item": entry["item"]} for entry in found]


def code_changes(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, str]]:
    """Executable bytes that changed without widening: scripts, hook
    definitions, executable extensions, MCP configuration."""
    found: list[dict[str, str]] = []
    old_scripts = _keys(old, "scripts", lambda item: str(item.get("path")))
    new_scripts = _keys(new, "scripts", lambda item: str(item.get("path")))
    for path in sorted(set(old_scripts) | set(new_scripts)):
        before, after = old_scripts.get(path), new_scripts.get(path)
        if before is None:
            found.append({"kind": "script", "change": "added", "path": path})
        elif after is None:
            found.append({"kind": "script", "change": "removed", "path": path})
        elif before.get("sha256") != after.get("sha256"):
            found.append({"kind": "script", "change": "changed", "path": path})
    old_hooks = _keys(old, "hooks", _hook_key)
    for key, item in sorted(_keys(new, "hooks", _hook_key).items()):
        before = old_hooks.get(key)
        if before is not None and before.get("digest") != item.get("digest"):
            found.append({"kind": "hook", "change": "changed", "path": f"{key[0]}: {key[1]} ({key[2]})"})
    old_ext = _keys(old, "executable_extensions", lambda item: str(item.get("path")))
    new_ext = _keys(new, "executable_extensions", lambda item: str(item.get("path")))
    for path in sorted(set(old_ext) | set(new_ext)):
        before, after = old_ext.get(path), new_ext.get(path)
        if before is not None and after is not None and before.get("sha256") != after.get("sha256"):
            found.append({"kind": "executable_extension", "change": "changed", "path": path})
        elif before is not None and after is None:
            found.append({"kind": "executable_extension", "change": "removed", "path": path})
    # MCP configuration changes are widening (see widens), never code.
    for field, kind in (("scripts", "script"), ("hooks", "hook"), ("executable_extensions", "executable_extension")):
        if not (_complete(old, field) and _complete(new, field)) and (
            old.get(f"{field}_content_digest") != new.get(f"{field}_content_digest")
        ):
            found.append({"kind": kind, "change": "changed", "path": f"({field} beyond the stored list)"})
    return found


def manifest_known(manifest: Any) -> bool:
    return isinstance(manifest, dict) and manifest.get("manifest_version") == MANIFEST_VERSION


def diff_files(old_files: dict[str, str] | None, new_files: dict[str, str] | None) -> dict[str, list[str]]:
    old_files = old_files or {}
    new_files = new_files or {}
    return {
        "added": sorted(set(new_files) - set(old_files)),
        "removed": sorted(set(old_files) - set(new_files)),
        "changed": sorted(path for path in set(old_files) & set(new_files) if old_files[path] != new_files[path]),
    }


def classify_update(
    old: dict[str, Any] | None,
    new: dict[str, Any],
    *,
    old_content_hash: str | None = None,
    new_content_hash: str | None = None,
    old_provenance: dict | None = None,
    new_provenance: dict | None = None,
) -> dict[str, Any]:
    """D2 update class and the evidence for it.

    ``widening`` beats ``code`` beats ``text``. ``old`` None (or produced by
    another manifest version) is an unknown baseline: widening, fail closed.
    """
    if not manifest_known(old) or (old or {}).get("incomplete"):
        # Unknown, older-derivation, or incompletely analyzed baseline.
        return {
            "update_class": "widening",
            "baseline_known": False,
            "widens": [{"kind": "unknown_baseline", "item": "the installed version's access is unknown"}],
            "narrows": [],
            "code_changes": [],
            "changed_requirements": sorted(REQUIREMENT_FOR_CODE_KIND.values()),
            "capability_digest_changed": True,
        }
    assert old is not None
    widened = widens(old, new, old_provenance=old_provenance, new_provenance=new_provenance)
    for reason in new.get("incomplete") or []:
        # The new version could not be analyzed completely: fail closed.
        widened.append({"kind": "manifest_incomplete", "item": _clean(reason, 300)})
    code = code_changes(old, new)
    same_bytes = old_content_hash is not None and old_content_hash == new_content_hash
    if widened:
        update_class = "widening"
    elif code:
        update_class = "code"
    elif same_bytes:
        update_class = "unchanged"
    else:
        update_class = "text"
    return {
        "update_class": update_class,
        "baseline_known": True,
        # Digest of the consented baseline: carried receipts must be bound to it.
        "baseline_digest": capability_digest(old),
        "widens": widened,
        "narrows": narrows(old, new),
        "code_changes": code,
        "changed_requirements": sorted({REQUIREMENT_FOR_CODE_KIND[item["kind"]] for item in code}),
        "capability_digest_changed": capability_digest(old) != capability_digest(new),
    }


def carried_requirements(update: dict[str, Any], consented: Iterable[str]) -> set[str]:
    """Consent requirements that carry forward to the new version under D2:
    everything previously consented for a text update, everything except
    the changed capabilities for a code update, nothing for a widening.
    DLP findings never carry (they are bound to the exact content)."""
    previous = {str(item) for item in consented} - {"dlp-findings", "instructions"}
    kind = update.get("update_class")
    if kind in {"unchanged", "text"}:
        return previous
    if kind == "code":
        return previous - set(update.get("changed_requirements") or [])
    return set()


def receipt_valid_for(item: dict[str, Any], *, content_hash: str | None, digest: str | None) -> bool:
    """A receipt counts as consent for the installed content only when it is
    bound to that exact content hash and capability digest under the current
    consent rule. Legacy (pre-070) and stale receipts never count."""
    return (
        bool(content_hash)
        and bool(digest)
        and item.get("content_hash") == content_hash
        and item.get("capability_digest") == digest
        and item.get("consent_rule_version") == CONSENT_RULE_VERSION
    )


def carried_for_tool(
    update: dict[str, Any],
    receipts: Iterable[Any],
    *,
    tool: str,
    scope: str,
    content_hash: str | None,
) -> set[str]:
    """Requirements that carry forward for ONE target tool and scope: only
    valid receipts (bound to the installed hash, the baseline capability
    digest and the current rule) for that (capability, tool, scope), or
    unrestricted (tool None) ones in that scope. A receipt for one tool never
    covers another."""
    digest = update.get("baseline_digest")
    rows = [
        item for item in receipts
        if isinstance(item, dict)
        and receipt_valid_for(item, content_hash=content_hash, digest=digest if isinstance(digest, str) else None)
    ]
    eligible = carried_requirements(update, {str(item.get("capability")) for item in rows})
    return {
        str(item.get("capability"))
        for item in rows
        if str(item.get("capability")) in eligible
        and item.get("scope") == scope
        and item.get("tool") in (tool, None)
    }


_REQUIREMENT_FLAGS = {
    "skill-scripts": "allow_scripts",
    "mcp": "enable_mcp",
    "executable-extensions": "allow_executable_extensions",
    "skill-hooks": "allow_hooks",
}


def flags_for_tool(flags: dict[str, bool], carried: set[str]) -> dict[str, bool]:
    """Explicit consent flags plus the requirements carried for one tool."""
    result = {name: bool(flags.get(name, False)) for name in (*_REQUIREMENT_FLAGS.values(), "allow_dlp_findings")}
    for requirement in carried:
        flag = _REQUIREMENT_FLAGS.get(requirement)
        if flag:
            result[flag] = True
    return result


def trusted_code_update_allowed(
    *,
    org_id: str | None,
    org_tier: str | None = None,
    publisher: dict[str, Any] | None = None,
    old_provenance: dict[str, Any] | None = None,
    new_provenance: dict[str, Any] | None = None,
) -> bool:
    """D2 exception: an Enterprise org that pre-trusts a publisher's signed
    release workflow may roll out CODE updates without re-approval, when the
    new version is attested by the same trusted-publisher identity.

    Never applies to a widening update. Always False until verified
    publishers and trusted publishing (tk_a287f7d654f64b1b) record publisher
    identity and attestation on versions; switch it on there by checking
    ``org_tier == 'enterprise'``, the org's trusted-publisher list, and that
    ``old_provenance`` and ``new_provenance`` name the same attested workflow.
    """
    return False


# ---------------------------------------------------------------------------
# Disclosure card
# ---------------------------------------------------------------------------


def consent_requirements(manifest: dict[str, Any]) -> list[str]:
    """Plan consent requirements implied by a manifest (adapter-independent)."""
    return _requirements(manifest)


def _requirements(manifest: dict[str, Any]) -> list[str]:
    requirements: list[str] = []
    if manifest.get("scripts"):
        requirements.append("skill-scripts")
    if manifest.get("hooks") or manifest.get("allowed_tools"):
        requirements.append("skill-hooks")
    if any(item.get("kind") == "skill-hooks" for item in manifest.get("executable_extensions") or []):
        requirements.append("executable-extensions")
    if manifest.get("mcp_local") or manifest.get("mcp_remote") or manifest.get("mcp_declared"):
        # MCP servers are never activated by an install, but a reviewer must
        # see and accept them (separate enable-mcp flow on the device).
        requirements.append("mcp-review")
    return requirements


def _summary(tool: str, manifest: dict[str, Any], connects: list[str], auto: list[str], writes: int) -> str:
    parts = [f"Writes {writes} file{'s' if writes != 1 else ''}"]
    scripts = len(manifest.get("scripts") or [])
    if scripts:
        parts.append(f"adds {scripts} script{'s' if scripts != 1 else ''}")
    hooks = manifest.get("hooks") or []
    if hooks:
        events = sorted({str(item.get("event")) for item in hooks})
        parts.append(f"adds {len(hooks)} hook{'s' if len(hooks) != 1 else ''} ({', '.join(events[:4])})")
    if auto:
        parts.append(f"pre-approves {', '.join(auto[:4])}")
    sentence = ", ".join(parts) + "."
    if connects:
        sentence += f" References {', '.join(connects[:3])}" + (" and more." if len(connects) > 3 else ".")
    if manifest.get("mcp_declared"):
        sentence += " Declares MCP servers (not activated by this install)."
    return sentence


def tool_disclosure(manifest: dict[str, Any], tool: str, *, scope: str = "project") -> dict[str, Any]:
    if tool not in SUPPORTED_TOOLS:
        raise ValueError(f"Unsupported tool: {tool}")
    project_roots, user_roots = _roots()
    root = project_roots[tool] if scope == "project" else "~/" + user_roots[tool]
    writes = [
        f"{root}/{skill['name']}/{member}"
        for skill in manifest.get("skills") or []
        for member in skill.get("files") or []
    ]
    writes_total = sum(
        int(skill.get("files_total", len(skill.get("files") or []))) for skill in manifest.get("skills") or []
    )
    connects = sorted(
        {str(item.get("origin")) for item in manifest.get("mcp_remote") or []}
        | set(manifest.get("network_endpoints") or [])
    )
    auto = [
        str(item).split(":", 1)[1] for item in manifest.get("allowed_tools") or []
        if str(item).startswith("allowed-tools:")
    ]
    omitted: list[dict[str, str]] = []
    if manifest.get("mcp_declared"):
        omitted.append({"component": "portable-mcp", "reason": "MCP registration requires the separate enable-mcp consent flow"})
    if any(item.get("kind") != "skill-hooks" for item in manifest.get("executable_extensions") or []):
        omitted.append({"component": "executable-extensions", "reason": "Hooks, agents, commands, and vendor executables require verified adapters"})
    return {
        "delivery": "sessionfs-emission",
        "scope": scope,
        "summary": _summary(tool, manifest, connects, auto, writes_total),
        "writes": writes[:MAX_WRITES_SHOWN],
        "writes_total": writes_total,
        "runs": {
            "scripts": len(manifest.get("scripts") or []),
            "hooks": [
                {"skill": item.get("skill"), "event": item.get("event"), "matcher": item.get("matcher")}
                for item in manifest.get("hooks") or []
            ],
            "mcp_local": len(manifest.get("mcp_local") or []),
        },
        "connects_to": connects[:MAX_ENDPOINTS],
        "auto_allowed": auto,
        "omitted": omitted,
        "consent_required": _requirements(manifest),
    }


def card_leaves(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Every leaf of a disclosure card as (path, text): the exhaustive,
    schema-free rendering approval surfaces show, so nothing the disclosure
    digest covers is ever off-screen. Strings verbatim (callers render them
    as literal text); other scalars as JSON; empty containers as [] / {}."""
    if isinstance(value, dict):
        if not value:
            return [(path, "{}")]
        leaves: list[tuple[str, str]] = []
        for key in sorted(value):
            leaves.extend(card_leaves(value[key], f"{path}.{key}" if path else str(key)))
        return leaves
    if isinstance(value, list):
        if not value:
            return [(path, "[]")]
        leaves = []
        for index, item in enumerate(value):
            leaves.extend(card_leaves(item, f"{path}[{index}]"))
        return leaves
    if isinstance(value, str):
        return [(path, value)]
    return [(path, json.dumps(value))]


def hidden_unicode_blocks(manifest: dict[str, Any]) -> bool:
    """Critical hidden-Unicode / control characters, decided on the complete
    per-severity counts (never on the displayed list); a truncated scan fails
    closed."""
    counts = manifest.get("unicode_counts")
    if isinstance(counts, dict):
        if int(counts.get("critical") or 0) > 0:
            return True
    elif any(item.get("severity") == "critical" for item in manifest.get("unicode_findings") or []):
        return True
    return bool(manifest.get("unicode_truncated"))


def build_disclosure(
    manifest: dict[str, Any],
    *,
    tools: Iterable[str] | None = None,
    scope: str = "project",
    version_id: str | None = None,
    content_hash: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The disclosure card (marketplace v2 §3.3), rendered identically by the
    CLI, the dashboard and MCP. Every string is untrusted display text."""
    selected = [tool for tool in (tools or SUPPORTED_TOOLS) if tool in SUPPORTED_TOOLS]
    unicode = manifest.get("unicode_findings") or []
    scan = manifest.get("scan") or {}
    requirements = _requirements(manifest)
    consent = ["instructions", *requirements]
    if hidden_unicode_blocks(manifest):
        consent.append("hidden-unicode")
    if SEVERITY_RANK.get(str(scan.get("max_severity") or "none"), 0) >= SEVERITY_RANK["high"]:
        consent.append("scan-findings")
    card: dict[str, Any] = {
        "card_version": CARD_VERSION,
        "scope": scope,
        "manifest_version": manifest.get("manifest_version"),
        "version_id": version_id,
        "content_hash": content_hash,
        "capability_digest": capability_digest(manifest) if manifest_known(manifest) else None,
        "capabilities": capability_set(manifest),
        "provenance": {
            "publisher_verified": bool((provenance or {}).get("publisher_verified")),
            "attested": bool((provenance or {}).get("attested")),
        },
        "scan": scan,
        "unicode": {
            "flagged_files": len({item.get("path") for item in unicode}),
            "findings": unicode,
            "counts": manifest.get("unicode_counts") or {},
            "truncated": bool(manifest.get("unicode_truncated")),
        },
        "shell_commands": manifest.get("shell_commands") or [],
        # Display list (the manifest keeps the full set for comparison).
        "network_endpoints": (manifest.get("network_endpoints") or [])[:MAX_ENDPOINTS],
        "network_endpoints_total": int(
            manifest.get("network_endpoints_total") or len(manifest.get("network_endpoints") or [])
        ),
        "incomplete": manifest.get("incomplete") or [],
        "env_names": manifest.get("env_names") or [],
        "mcp_servers": {
            "local": [
                {key: item.get(key) for key in ("name", "command", "env_names")}
                for item in manifest.get("mcp_local") or []
            ],
            "remote": manifest.get("mcp_remote") or [],
            "declared": bool(manifest.get("mcp_declared")),
        },
        "allowed_tools": manifest.get("allowed_tools") or [],
        "persistence_targets": manifest.get("persistence_targets") or [],
        "executable_extensions": [
            {"path": item.get("path"), "kind": item.get("kind")}
            for item in manifest.get("executable_extensions") or []
        ],
        "tools": {tool: tool_disclosure(manifest, tool, scope=scope) for tool in selected},
        "consent_required": consent,
    }
    # Digest of everything the approver is shown (scanner ruleset and results,
    # hidden characters, provenance, tools, scope, card schema). Approvals
    # bind to it; capability_digest stays the access-equivalence digest.
    card["disclosure_digest"] = _sha(_canonical(card))
    return card


# ---------------------------------------------------------------------------
# Text diffs
# ---------------------------------------------------------------------------


def _read_text(root: Path, relative: str) -> str | None:
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_TEXT_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    from sessionfs.skills.package import looks_binary

    if looks_binary(raw):
        return None
    return raw.decode("utf-8", "replace")


def text_diffs(
    old_root: Path | None,
    new_root: Path | None,
    files: dict[str, list[str]],
    *,
    budget: int = TEXT_DIFF_BUDGET,
    old_files: dict[str, str] | None = None,
    new_files: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Bounded unified diffs for changed / added / removed text files, the
    longest first, until ``budget`` bytes. Binary files are listed by hash
    only: ``old_sha256`` / ``new_sha256`` come from the two manifests' file
    maps (None for the side that does not have the file, or when a baseline
    is unknown). Truncation is explicit; widening never depends on these
    diffs."""
    entries: list[dict[str, Any]] = []
    for change in ("changed", "added", "removed"):
        for relative in files.get(change, []):
            before = _read_text(old_root, relative) if old_root and change != "added" else ""
            after = _read_text(new_root, relative) if new_root and change != "removed" else ""
            if before is None or after is None:
                entries.append({
                    "path": relative,
                    "change": change,
                    "binary": True,
                    "diff": None,
                    "old_sha256": (old_files or {}).get(relative),
                    "new_sha256": (new_files or {}).get(relative),
                })
                continue
            old_lines = before.splitlines(keepends=True)
            new_lines = after.splitlines(keepends=True)
            if len(old_lines) > _MAX_DIFF_INPUT_LINES or len(new_lines) > _MAX_DIFF_INPUT_LINES:
                entries.append({"path": relative, "change": change, "binary": False, "diff": None, "too_large": True})
                continue
            body = "".join(
                difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{relative}", tofile=f"b/{relative}")
            )
            entries.append({"path": relative, "change": change, "binary": False, "diff": clean_untrusted_text(body, limit=len(body) + 1)})
    entries.sort(key=lambda item: (-len(item.get("diff") or ""), item["path"]))
    used = 0
    truncated = False
    for entry in entries:
        diff = entry.get("diff")
        if not diff:
            continue
        remaining = budget - used
        if remaining <= 0:
            entry["diff"] = None
            entry["truncated"] = True
            truncated = True
            continue
        if len(diff) > remaining:
            entry["diff"] = diff[:remaining] + "\n... [diff truncated]\n"
            entry["truncated"] = True
            truncated = True
            used = budget
        else:
            used += len(diff)
    entries.sort(key=lambda item: item["path"])
    return {"entries": entries, "truncated": truncated, "budget_bytes": budget}


def build_diff(
    old_manifest: dict[str, Any] | None,
    new_manifest: dict[str, Any],
    *,
    old_root: Path | None = None,
    new_root: Path | None = None,
    old_content_hash: str | None = None,
    new_content_hash: str | None = None,
    old_provenance: dict | None = None,
    new_provenance: dict | None = None,
    include_text: bool = True,
) -> dict[str, Any]:
    """Update diff (marketplace v2 §3.4): files, capability delta, scan and
    Unicode delta, update class."""
    update = classify_update(
        old_manifest,
        new_manifest,
        old_content_hash=old_content_hash,
        new_content_hash=new_content_hash,
        old_provenance=old_provenance,
        new_provenance=new_provenance,
    )
    old_files = (old_manifest or {}).get("files") if isinstance(old_manifest, dict) else None
    files = diff_files(old_files, new_manifest.get("files"))
    if old_files is None:
        files = {"added": [], "removed": [], "changed": [], "unknown": True}  # type: ignore[dict-item]
    old_unicode = {
        (item.get("path"), item.get("rule_id"), item.get("line"))
        for item in (old_manifest or {}).get("unicode_findings") or []
    } if isinstance(old_manifest, dict) else set()
    new_unicode = [
        item for item in new_manifest.get("unicode_findings") or []
        if (item.get("path"), item.get("rule_id"), item.get("line")) not in old_unicode
    ]
    old_scan = ((old_manifest or {}).get("scan") or {}) if isinstance(old_manifest, dict) else {}
    new_scan = new_manifest.get("scan") or {}
    result = {
        **update,
        "files": files,
        "scan_delta": {
            "old_max_severity": old_scan.get("max_severity"),
            "new_max_severity": new_scan.get("max_severity"),
            "old_finding_count": old_scan.get("finding_count"),
            "new_finding_count": new_scan.get("finding_count"),
            # The scanner ruleset each side was analyzed with: a ruleset
            # bump alone changes the diff, so an acknowledgement bound to
            # the pre-bump scan can never silently cover the re-scanned one.
            "old_ruleset": old_scan.get("ruleset"),
            "new_ruleset": new_scan.get("ruleset"),
        },
        "unicode_delta": {"new_findings": new_unicode},
        "provenance_delta": {
            "old": {k: bool((old_provenance or {}).get(k)) for k in ("publisher_verified", "attested")},
            "new": {k: bool((new_provenance or {}).get(k)) for k in ("publisher_verified", "attested")},
        },
        "consent": consent_rule(update["update_class"]),
    }
    if include_text and (old_root is not None or new_root is not None) and old_files is not None:
        result["text_diffs"] = text_diffs(
            old_root, new_root, files, old_files=old_files, new_files=new_manifest.get("files")
        )
    return result


def consent_rule(update_class: str) -> dict[str, Any]:
    """What each surface requires for an update of this class (D2)."""
    if update_class in {"unchanged", "text"}:
        return {
            "personal": "confirm",
            "org_required": "auto_apply_allowed",
            "description": "Text or asset changes only: one confirmation; existing consent carries forward.",
        }
    if update_class == "code":
        return {
            "personal": "reconsent_changed_capabilities",
            "org_required": "approval_required",
            "description": "Script, hook or MCP configuration bytes changed: approve the changed capability again.",
        }
    return {
        "personal": "explicit_reconsent",
        "org_required": "never_auto_apply",
        "description": "The update asks for more access: it always stops and requires explicit re-consent.",
    }
