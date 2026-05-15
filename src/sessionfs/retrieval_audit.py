"""MCP retrieval audit helpers.

Server-side audit events are preferred when a ticket start created a
``retrieval_audit_id``. The local JSONL writer remains a fallback for
offline MCP clients or explicit ``audit_session_id`` callers.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("sessionfs.retrieval_audit")

SAFE_AUDIT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_REF_WALK_DEPTH = 50
MAX_LOCAL_RETRIEVAL_LOG_BYTES = 10 * 1024 * 1024


RETRIEVAL_TOOLS = {
    "search_project_knowledge",
    "get_wiki_page",
    "get_persona",
    "get_compiled_rules",
    "get_context_section",
    "find_related_sessions",
    "get_session_context",
}


def _log_dir() -> Path:
    return Path.home() / ".sessionfs" / "retrieval_logs"


def is_safe_audit_id(value: str | None) -> bool:
    return bool(value and SAFE_AUDIT_ID_RE.fullmatch(value))


def _clean_audit_id(value: Any, *, source: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if not is_safe_audit_id(value):
        logger.debug("Ignoring unsafe retrieval audit id from %s", source)
        return None
    return value


def audit_session_id(args: dict[str, Any] | None = None) -> str | None:
    args = args or {}
    for key in ("audit_session_id", "current_session_id"):
        value = _clean_audit_id(args.get(key), source=key)
        if value:
            return value
    for key in ("SESSIONFS_SESSION_ID", "SFS_SESSION_ID"):
        value = _clean_audit_id(os.environ.get(key), source=key)
        if value:
            return value
    return None


def audit_context_id(args: dict[str, Any] | None = None) -> str | None:
    args = args or {}
    for key in ("retrieval_audit_id", "audit_context_id"):
        value = _clean_audit_id(args.get(key), source=key)
        if value:
            return value
    value = _clean_audit_id(
        os.environ.get("SESSIONFS_RETRIEVAL_AUDIT_ID"),
        source="SESSIONFS_RETRIEVAL_AUDIT_ID",
    )
    if value:
        return value
    try:
        from sessionfs.active_ticket import read_bundle

        bundle = read_bundle()
    except Exception:
        bundle = None
    if isinstance(bundle, dict):
        value = _clean_audit_id(
            bundle.get("retrieval_audit_id"),
            source="active_ticket.retrieval_audit_id",
        )
        if value:
            return value
    return None


def sanitize_arguments(args: dict[str, Any]) -> dict[str, Any]:
    sensitive_fragments = ("api_key", "token", "secret", "password", "auth", "credential")
    return {
        key: value
        for key, value in args.items()
        if key not in {"git_remote"}
        and not any(fragment in key.lower() for fragment in sensitive_fragments)
    }


def collect_returned_refs(value: Any) -> dict[str, list[str]]:
    refs: dict[str, set[str]] = {
        "ids": set(),
        "kb_entry_ids": set(),
        "slugs": set(),
        "session_ids": set(),
        "persona_names": set(),
    }

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > MAX_REF_WALK_DEPTH:
            return
        if isinstance(obj, dict):
            for key, item in obj.items():
                if item is not None:
                    text = str(item)
                    if key == "id":
                        refs["ids"].add(text)
                    elif key == "kb_entry_id":
                        refs["kb_entry_ids"].add(text)
                    elif key == "slug":
                        refs["slugs"].add(text)
                    elif key == "session_id":
                        refs["session_ids"].add(text)
                    elif key == "name":
                        refs["persona_names"].add(text)
                walk(item, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    walk(value)
    return {key: sorted(values) for key, values in refs.items() if values}


def record_retrieval(
    *,
    tool_name: str,
    args: dict[str, Any],
    result: Any,
) -> bool:
    log_id = audit_session_id(args) or audit_context_id(args)
    if not is_safe_audit_id(log_id):
        logger.debug("Skipping retrieval audit write for missing/unsafe id")
        return False

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "arguments": sanitize_arguments(args),
        "returned_refs": collect_returned_refs(result),
    }
    path = _log_dir() / f"{log_id}.jsonl"
    if path.exists() and path.stat().st_size >= MAX_LOCAL_RETRIEVAL_LOG_BYTES:
        logger.warning("Skipping retrieval audit write; log file is over size cap")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
    return True


def read_retrieval_log(session_id: str) -> list[dict[str, Any]]:
    if not is_safe_audit_id(session_id):
        logger.debug("Refusing retrieval audit read for unsafe id")
        return []
    path = _log_dir() / f"{session_id}.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows
