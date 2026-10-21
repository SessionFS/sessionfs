"""Advisory delivery of the org skills policy on the device (marketplace v2 §8.4).

During ``sfs skill pull`` (and the daemon's reconcile) the server already
withholds grants the org's current policy refuses; this module turns what
the pull saw into the device's compliance report:

- per tool: whether IT deployed the compiled managed settings to the
  tool's system path (``system``: the file's hash matches the current
  revision), or whether only SessionFS's own refusals apply
  (``advisory``);
- SessionFS-managed installs the policy now refuses
  (``noncompliant_installs``) and grants this pull refused (``refused``).

Only names, ids, versions and policy codes are sent - never file contents.
``sfs`` never writes system paths and never asks for elevated rights here.
"""

from __future__ import annotations

import hashlib
import platform
import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sessionfs.skills.models import SkillError, validate_device_id

if TYPE_CHECKING:  # pragma: no cover
    from sessionfs.skills.hosted import HostedClient, PullReport
    from sessionfs.skills.manager import SkillManager

DEVICE_ID_FILE = "policy_device_id"
MAX_FILE_BYTES = 1024 * 1024


def os_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def local_device_id(store_root: Path) -> str:
    """A stable per-store device id for reports when SESSIONFS_DEVICE_ID is
    not set (random, created once)."""
    path = Path(store_root) / DEVICE_ID_FILE
    try:
        existing = path.read_text("utf-8").strip()
        if existing:
            return validate_device_id(existing)
    except (OSError, ValueError):
        pass
    value = f"local-{secrets.token_hex(8)}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", "utf-8")
    except OSError:
        pass
    return value


def _expand(path: str) -> Path:
    import os

    return Path(os.path.expandvars(path))


def deployed_hash(paths: dict[str, Any], os_name: str | None = None) -> tuple[str | None, bool]:
    """(sha256 of the managed file at this OS's system path, file exists)."""
    raw = paths.get(os_name or os_key()) if isinstance(paths, dict) else None
    if not isinstance(raw, str) or not raw:
        return None, False
    path = _expand(raw)
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None, path.exists()
        data = path.read_bytes()
    except OSError:
        return None, False
    return "sha256:" + hashlib.sha256(data).hexdigest(), True


def tool_report(summary: dict[str, Any], os_name: str | None = None) -> dict[str, Any]:
    enforcement = summary.get("enforcement")
    files = [a for a in summary.get("artifacts") or [] if isinstance(a, dict)]
    violations: list[str] = []
    found_hash: str | None = None
    mode = "advisory"
    if enforcement in {"managed"} and files:
        expected = files[0].get("content_hash")
        found_hash, exists = deployed_hash(files[0].get("paths") or {}, os_name)
        if found_hash is not None and found_hash == expected:
            mode = "system"
        elif exists:
            mode = "system"
            violations.append("managed_settings_outdated")
    return {"enforced_mode": mode, "artifact_hash_found": found_hash, "violations": violations}


def _installed_versions(manager: "SkillManager") -> set[str]:
    from sessionfs.skills import hosted

    found: set[str] = set()
    for scope in ("user", "project"):
        try:
            rows = hosted._installed_rows(manager, scope)
        except SkillError:
            continue
        for row in rows:
            source = row.get("source") if isinstance(row, dict) else None
            if isinstance(source, dict) and isinstance(source.get("commit"), str):
                found.add(source["commit"])
    return found


def build_reports(
    manager: "SkillManager", policy_body: dict[str, Any], report: "PullReport", os_name: str | None = None
) -> list[dict[str, Any]]:
    installed = _installed_versions(manager)
    reports = []
    for entry in policy_body.get("items") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("org_id"), str):
            continue
        org_id = entry["org_id"]
        refused = [item for item in report.policy_refused if item.get("org_id") == org_id]
        per_tool = {}
        for tool, summary in (entry.get("tools") or {}).items():
            if isinstance(summary, dict) and summary.get("enforcement") not in {"off", "unsupported"}:
                per_tool[tool] = tool_report(summary, os_name)
        reports.append(
            {
                "org_id": org_id,
                "policy_revision": int(entry.get("revision") or 0),
                "per_tool": per_tool,
                "refused": [
                    {
                        "origin": str(item.get("origin") or ""),
                        "version_id": str(item.get("version_id") or ""),
                        "package_id": str(item.get("package_id") or ""),
                        "violations": ",".join(item.get("violations") or []),
                    }
                    for item in refused
                ][:200],
                "noncompliant_installs": [
                    {
                        "version_id": str(item.get("version_id") or ""),
                        "package_id": str(item.get("package_id") or ""),
                        "violations": ",".join(item.get("violations") or []),
                    }
                    for item in refused
                    if item.get("version_id") in installed
                ][:200],
                "unmanaged_plugins": [],
            }
        )
    return reports


def report_after_pull(
    manager: "SkillManager",
    client: "HostedClient",
    report: "PullReport",
    device_id: str | None,
) -> list[dict[str, Any]]:
    body = client.device_policy()
    if not body or not body.get("items"):
        return []
    reports = build_reports(manager, body, report)
    if not reports:
        return []
    payload = {
        "device_id": device_id or local_device_id(manager.store_root),
        "display_name": platform.node()[:200],
        "platform": os_key(),
        "reports": reports,
    }
    result = client.post_policy_report(payload)
    accepted = result.get("accepted") if isinstance(result, dict) else None
    return [item for item in accepted or [] if isinstance(item, dict)]
