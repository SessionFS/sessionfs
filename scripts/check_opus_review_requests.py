#!/usr/bin/env python3
"""Poll the SessionFS KB for new review-request entries.

Designed for cron use. It:
1. resolves the current repo's project on the configured SessionFS server
2. fetches recent discovery entries matching "REVIEW REQUEST"
3. filters to entity_type == "review-request"
4. logs only newly seen entry IDs

State and logs live under ~/.sessionfs/ so repeated cron runs stay quiet
unless a new review request appears.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sessionfs.daemon.config import load_config
from sessionfs.server.github_app import normalize_git_remote

STATE_DIR = Path.home() / ".sessionfs"
STATE_PATH = STATE_DIR / "opus_review_watch_state.json"
LOG_PATH = STATE_DIR / "opus_review_watch.log"


def _git_remote() -> str:
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _load_seen() -> set[int]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return set()
    if not isinstance(data, list):
        return set()
    seen: set[int] = set()
    for item in data:
        if isinstance(item, int):
            seen.add(item)
    return seen


def _save_seen(seen: set[int]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(sorted(seen), indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _append_log(lines: list[str]) -> None:
    if not lines:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
    except OSError:
        # Best effort only — the watcher should stay alive even if the
        # host disallows writing the state/log directory.
        pass


def _log_status(message: str) -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    _append_log([f"{timestamp} STATUS {message}"])


def main() -> int:
    config = load_config()
    if not config.sync.api_key:
        _log_status("skipping: no API key configured")
        return 0

    remote = _git_remote()
    normalized = normalize_git_remote(remote)
    if not normalized:
        _log_status("skipping: could not resolve git remote")
        return 0

    api_url = config.sync.api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {config.sync.api_key}"}

    try:
        with httpx.Client(timeout=15) as client:
            project_resp = client.get(
                f"{api_url}/api/v1/projects/{normalized}",
                headers=headers,
            )
            if project_resp.status_code != 200:
                _log_status(f"project lookup failed: status={project_resp.status_code}")
                return 0
            project_id = project_resp.json().get("id")
            if not project_id:
                _log_status("project lookup returned no id")
                return 0

            entries_resp = client.get(
                f"{api_url}/api/v1/projects/{project_id}/entries"
                f"?search={quote('REVIEW REQUEST')}&type=discovery&limit=100",
                headers=headers,
            )
            if entries_resp.status_code != 200:
                _log_status(f"entries lookup failed: status={entries_resp.status_code}")
                return 0
    except Exception as exc:
        _log_status(f"request failed: {exc.__class__.__name__}: {exc}")
        return 0

    payload = entries_resp.json()
    entries = payload if isinstance(payload, list) else payload.get("entries", [])
    if not isinstance(entries, list):
        _log_status(f"unexpected entries payload type: {type(payload).__name__}")
        return 0

    current_ids: set[int] = set()
    new_lines: list[str] = []
    timestamp = datetime.now(timezone.utc).isoformat()
    seen = _load_seen()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("entity_type") != "review-request":
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, int):
            continue
        current_ids.add(entry_id)
        if entry_id in seen:
            continue
        entity_ref = entry.get("entity_ref") or "?"
        content = str(entry.get("content") or "").splitlines()[0][:200]
        new_lines.append(
            f"{timestamp} NEW_REVIEW_REQUEST id={entry_id} ref={entity_ref} {content}"
        )

    if new_lines:
        _append_log(new_lines)

    _save_seen(current_ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
