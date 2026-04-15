"""Resume-time target-tool rules preflight (v0.9.9).

`sfs resume --in <tool>` uses this helper to materialize the **target tool's**
project rules file from the **current canonical SessionFS rules** before
launching the target tool. This is a small bridge between:

- canonical project rules (the /rules + /rules/compile API)
- cross-tool resume
- session instruction provenance

The helper is intentionally narrow. It is NOT a second rules system:

- only one file is ever written (the target tool's)
- it reuses the existing managed-marker detection + partial-compile endpoint
- it never creates a canonical `rules_versions` history row
- it never mutates canonical `enabled_tools`
- failure is non-fatal — resume proceeds regardless

See ``v0.9.9-resume-rules-sync-brief`` for the full product spec.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Reuse the single source of truth for managed-marker detection,
# remote normalization, project resolution, API config, and HTTP plumbing.
# Factoring these into a separate helpers module is out of scope for v0.9.9 —
# we intentionally import from cmd_rules to keep that behavior identical.
from sessionfs.cli.cmd_rules import (
    TOOL_FILES,
    _api_request,
    _get_api_config,
    _is_managed,
    _normalize_remote,
    _safe_target_path,
)

# Resume targets that also have SessionFS compilers. Cursor, cline, roo-code,
# and amp are capture-only and must skip rules sync cleanly.
SUPPORTED_RESUME_TOOLS: tuple[str, ...] = (
    "claude-code",
    "codex",
    "copilot",
    "gemini",
)


@dataclass
class RulesSyncResult:
    """What happened during a resume-time rules preflight.

    Used by the CLI to decide which (≤3) stderr lines to print.
    """

    applied: bool
    skipped_reason: str | None
    target_tool: str
    filename: str | None = None
    source_rules_version: int | None = None
    source_rules_source: str | None = None
    current_rules_version: int | None = None
    used_force: bool = False
    warning: str | None = None


# ---------------------------------------------------------------------------
# Small helpers (kept local — no new public CLI surface)
# ---------------------------------------------------------------------------


def _git_root_at(path: str | Path) -> Path | None:
    """Return the git toplevel for `path`, or None if `path` isn't in a repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _git_remote_at(path: str | Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _extract_source_provenance(
    source_manifest: dict | None,
) -> tuple[int | None, str | None]:
    """Pull rules_version + rules_source out of ``manifest.instruction_provenance``.

    Missing keys or malformed payloads collapse to ``(None, None)`` — the
    caller surfaces a fallback line.
    """
    if not isinstance(source_manifest, dict):
        return None, None
    prov = source_manifest.get("instruction_provenance")
    if not isinstance(prov, dict):
        return None, None
    version = prov.get("rules_version")
    source = prov.get("rules_source")
    if not isinstance(version, int):
        version = None
    if not isinstance(source, str) or source not in (
        "sessionfs",
        "manual",
        "mixed",
        "none",
    ):
        source = None
    return version, source


def _resolve_project_id_soft(
    api_url: str, api_key: str, normalized: str
) -> tuple[str | None, str | None]:
    """Like cmd_rules._resolve_project_id but returns (id, error) instead of exiting."""
    try:
        status, body, _ = asyncio.run(
            _api_request("GET", f"/api/v1/projects/{normalized}", api_url, api_key)
        )
    except Exception as exc:  # network error, DNS failure, etc.
        return None, f"api-error: {exc}"
    if status == 404:
        return None, "no-project"
    if status >= 400 or not isinstance(body, dict):
        return None, f"api-error: {status}"
    project_id = body.get("id")
    if not isinstance(project_id, str):
        return None, "api-error: malformed project response"
    return project_id, None


def _rules_are_initialized(rules_body: dict) -> bool:
    """Match the brief's "meaningfully initialized" check.

    Any of:
      - ``enabled_tools`` non-empty
      - ``static_rules`` non-empty
      - ``tool_overrides`` non-empty

    The ``version`` field is NOT a valid proxy: ``get_or_create_rules`` seeds
    brand-new default rows at ``version = 1`` regardless of content, so a
    version check would flag every untouched project as initialized. The
    ``rules_versions`` count could distinguish those, but even if a versions
    row exists, compiling against empty canonical content would produce a
    marker-only file — not useful to write during resume. Requiring actual
    editable content (enabled_tools/static_rules/tool_overrides) gives the
    correct "the user has curated something worth materializing" signal
    without a second API call.
    """
    if rules_body.get("enabled_tools"):
        return True
    if (rules_body.get("static_rules") or "").strip():
        return True
    overrides = rules_body.get("tool_overrides")
    if isinstance(overrides, dict) and overrides:
        return True
    return False


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def preflight_target_tool_rules(
    *,
    project_path: str,
    target_tool: str,
    source_manifest: dict | None = None,
    force: bool = False,
    skip: bool = False,
) -> RulesSyncResult:
    """Materialize the target tool's project rules file from current canonical rules.

    Non-fatal — always returns a ``RulesSyncResult`` describing what happened.
    The caller (``sfs resume``) uses the result to print ≤3 stderr lines.

    Skip ladder (first match wins):

      - skip=True                                  → ``no-rules-sync``
      - target_tool not in SUPPORTED_RESUME_TOOLS  → ``unsupported-tool``
      - ``project_path`` not inside a git repo     → ``not-in-git``
      - no SessionFS project for this remote        → ``no-project``
      - rules not meaningfully initialized          → ``not-initialized``
      - file exists, unmanaged, force=False         → ``unmanaged-skip``
      - API / write failure                         → ``api-error`` / ``write-error``

    Otherwise: write the compiled output to disk and return ``applied=True``.

    Note: ``force=True`` is a **one-time permission**. Once it writes
    SessionFS-managed content over an unmanaged file, the next preflight
    (without force) will see the managed marker and treat the file as
    Case B (managed → refresh). This is intentional: the user opted in.

    Parameters
    ----------
    project_path:
        Absolute or workspace-relative path to the target project root.
    target_tool:
        ``claude-code`` | ``codex`` | ``copilot`` | ``gemini``. Anything else
        short-circuits cleanly.
    source_manifest:
        The source session's parsed ``manifest.json`` (or ``None`` if unreadable).
        Used only for provenance messaging; never drives the compile.
    force:
        Allow overwrite of an unmanaged target tool file. One-time permission —
        see note above.
    skip:
        Caller passed ``--no-rules-sync``.
    """
    src_version, src_source = _extract_source_provenance(source_manifest)

    def _base(reason: str, *, warning: str | None = None) -> RulesSyncResult:
        return RulesSyncResult(
            applied=False,
            skipped_reason=reason,
            target_tool=target_tool,
            filename=TOOL_FILES.get(target_tool),
            source_rules_version=src_version,
            source_rules_source=src_source,
            used_force=force,
            warning=warning,
        )

    # 1. --no-rules-sync wins over everything.
    if skip:
        return _base("no-rules-sync")

    # 2. Resume-only supported tools.
    if target_tool not in SUPPORTED_RESUME_TOOLS:
        return _base("unsupported-tool")

    # 3. Must be a git-backed project.
    project_root = Path(project_path) if project_path else None
    if project_root is None or not project_root.exists():
        return _base("not-in-git")
    git_root = _git_root_at(project_root)
    if git_root is None:
        return _base("not-in-git")
    remote = _git_remote_at(project_root)
    if not remote:
        return _base("not-in-git")
    normalized = _normalize_remote(remote)
    if not normalized:
        return _base("not-in-git")

    # 4. Resolve API config + project ID (soft — no typer.Exit).
    try:
        api_url, api_key = _get_api_config()
    except SystemExit:
        # Not authenticated is effectively "no SessionFS project available".
        return _base("no-project")
    except Exception as exc:
        return _base("api-error", warning=f"API config error: {exc}")

    project_id, err = _resolve_project_id_soft(api_url, api_key, normalized)
    if err:
        if err == "no-project":
            return _base("no-project")
        return _base("api-error", warning=err)

    # 5. Fetch rules + check meaningful initialization.
    try:
        status, rules_body, _ = asyncio.run(
            _api_request(
                "GET",
                f"/api/v1/projects/{project_id}/rules",
                api_url,
                api_key,
            )
        )
    except Exception as exc:
        return _base("api-error", warning=f"rules fetch failed: {exc}")
    if status >= 400 or not isinstance(rules_body, dict):
        return _base("api-error", warning=f"rules fetch returned {status}")

    current_version = rules_body.get("version")
    current_version = current_version if isinstance(current_version, int) else None

    if not _rules_are_initialized(rules_body):
        result = _base("not-initialized")
        result.current_rules_version = current_version
        return result

    # 6. Partial compile for the target tool only. The server treats a
    # subset-of-enabled_tools request as non-versioning, so this never writes
    # a canonical history row. It also works when target_tool is NOT in
    # enabled_tools (see brief §"Key Product Decision") — enabled_tools is
    # not mutated server-side.
    try:
        status, compile_body, _ = asyncio.run(
            _api_request(
                "POST",
                f"/api/v1/projects/{project_id}/rules/compile",
                api_url,
                api_key,
                json_data={"tools": [target_tool]},
            )
        )
    except Exception as exc:
        return _base("api-error", warning=f"compile failed: {exc}")
    if status >= 400 or not isinstance(compile_body, dict):
        return _base("api-error", warning=f"compile returned {status}")

    compiled_version = compile_body.get("version")
    if isinstance(compiled_version, int):
        current_version = compiled_version

    outputs = compile_body.get("outputs")
    if not isinstance(outputs, list):
        result = _base("api-error", warning="compile response missing outputs array")
        result.current_rules_version = current_version
        return result
    output = None
    for out in outputs:
        if isinstance(out, dict) and out.get("tool") == target_tool:
            output = out
            break
    if output is None:
        result = _base("api-error", warning="compile returned no matching tool output")
        result.current_rules_version = current_version
        return result

    # Validate payload types up front — an earlier version trusted the shape
    # and would raise TypeError on `{"filename": []}` or `{"content": null}`,
    # breaking the helper's "non-fatal, return a RulesSyncResult" contract.
    # The outer `_run_rules_preflight` catch-all in cmd_ops would have caught
    # the exception, but the helper is used directly by tests and future
    # callers, so validate locally.
    raw_filename = output.get("filename")
    if raw_filename is not None and not isinstance(raw_filename, str):
        result = _base("api-error", warning="compile output.filename is not a string")
        result.current_rules_version = current_version
        return result

    raw_content = output.get("content")
    if not isinstance(raw_content, str):
        result = _base("api-error", warning="compile output.content is not a string")
        result.current_rules_version = current_version
        return result
    content = raw_content

    # Defence-in-depth: validate target path against the canonical filename
    # for `target_tool` (TOOL_FILES) and confirm it stays inside git_root.
    # A hostile / buggy compile API response cannot make resume write
    # outside the repo or under an absolute path.
    target_path = _safe_target_path(git_root, target_tool, raw_filename)
    if target_path is None:
        result = _base("write-error", warning="unsafe compile output filename")
        result.current_rules_version = current_version
        return result
    filename = TOOL_FILES[target_tool]

    # 7. Managed-file safety (shared with `sfs rules compile`).
    if target_path.exists():
        if not _is_managed(target_path) and not force:
            result = _base("unmanaged-skip")
            result.current_rules_version = current_version
            return result

    # 8. Write the target file. This is the only file we touch.
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        result = _base("write-error", warning=f"write failed: {exc}")
        result.current_rules_version = current_version
        return result

    return RulesSyncResult(
        applied=True,
        skipped_reason=None,
        target_tool=target_tool,
        filename=filename,
        source_rules_version=src_version,
        source_rules_source=src_source,
        current_rules_version=current_version,
        used_force=force,
        warning=None,
    )


# ---------------------------------------------------------------------------
# Stderr messaging
# ---------------------------------------------------------------------------


def format_messages(result: RulesSyncResult) -> list[str]:
    """Return ≤3 muted/dim stderr lines describing `result`.

    Kept as a pure function so tests can assert on the exact output without
    capturing Rich markup from a live Console.
    """
    # --no-rules-sync: silent (respect the opt-out).
    if result.skipped_reason == "no-rules-sync":
        return []

    # Unsupported tool / not in git / no project: single informational line.
    if result.skipped_reason == "unsupported-tool":
        return []  # resume command itself handles the capture-only error path.
    if result.skipped_reason == "not-in-git":
        return ["No git-backed project detected; skipping rules sync."]
    if result.skipped_reason == "no-project":
        return [
            "No SessionFS project found for this repo.",
            "Continuing resume without rules sync.",
        ]
    if result.skipped_reason == "not-initialized":
        return [
            "No initialized SessionFS project rules found for this repo.",
            "Continuing resume without rules sync.",
        ]

    # Everything below shares the first "source session" line.
    lines: list[str] = []
    if result.source_rules_version is not None and result.source_rules_source:
        lines.append(
            f"Source session used rules v{result.source_rules_version} "
            f"({result.source_rules_source})."
        )
    elif result.source_rules_source:
        lines.append(f"Source session used {result.source_rules_source} rules.")
    else:
        lines.append("Source session had no recorded rules provenance.")

    if result.current_rules_version is not None:
        lines.append(f"Current project rules are v{result.current_rules_version}.")

    filename = result.filename or TOOL_FILES.get(result.target_tool, "rules file")

    if result.applied:
        version_str = (
            f" v{result.current_rules_version}"
            if result.current_rules_version is not None
            else ""
        )
        lines.append(f"Synced {filename} from SessionFS rules{version_str}.")
    elif result.skipped_reason == "unmanaged-skip":
        lines.append(
            f"Unmanaged {filename} detected; canonical rules not applied. "
            f"Use --force-rules to replace it."
        )
    elif result.skipped_reason in ("api-error", "write-error"):
        suffix = f" ({result.warning})" if result.warning else ""
        lines.append(f"Rules sync failed; continuing resume{suffix}.")

    # Hard cap at 3 lines per brief.
    return lines[:3]
