"""Tests for Kilo Code VS Code extension — Roo Code fork differences.

Kilo Code is a fork of Roo Code (which is a Cline fork). Storage format
is identical: UUID-named task directories under tasks/ with
api_conversation_history.json. These tests cover the Kilo-specific
wiring:

- tool="kilo-code" in manifests
- Per-task UUID IDs (same as Roo)
- Storage path: kilocode.kilo-code (lowercased; marketplace ID is
  kilocode.Kilo-Code but the on-disk directory is lowercased by VS Code)

The shared parsing/discovery logic is exercised in tests/unit/test_cline.py
and tests/unit/test_roo.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.converters.cline_to_sfs import (
    convert_cline_to_sfs,
    discover_cline_sessions,
    parse_cline_session,
)


def _make_kilo_api_conversation(task_dir: Path) -> None:
    messages = [
        {"role": "user", "content": "Refactor the rate limiter to use Redis."},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll switch the in-memory counter to Redis INCR."},
                {
                    "type": "tool_use",
                    "id": "toolu_kilo_01",
                    "name": "read_file",
                    "input": {"path": "src/middleware/rate_limit.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_kilo_01",
                    "content": "class RateLimiter: ...",
                },
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Done — Redis-backed now."}],
        },
    ]
    (task_dir / "api_conversation_history.json").write_text(json.dumps(messages))


def _make_history_item(task_dir: Path, task_label: str, workspace: str = "") -> None:
    meta = {
        "id": task_dir.name,
        "task": task_label,
        "workspace": workspace,
        "ts": 1714000000000,
        "totalCost": {"inputTokens": 7200, "outputTokens": 2100},
    }
    (task_dir / "history_item.json").write_text(json.dumps(meta))


@pytest.fixture
def kilo_task_dir(tmp_path: Path) -> Path:
    storage = tmp_path / "kilocode.kilo-code"
    task_id = "f7a8b9c0-1234-5678-90ab-cdef01234567"
    task_dir = storage / "tasks" / task_id
    task_dir.mkdir(parents=True)
    _make_kilo_api_conversation(task_dir)
    _make_history_item(task_dir, "Refactor rate limiter to Redis", "/Users/dev/api")
    return task_dir


class TestKiloCodeParsing:
    def test_uuid_task_id_parsed(self, kilo_task_dir: Path):
        session = parse_cline_session(kilo_task_dir, tool="kilo-code")
        assert session.session_id == "f7a8b9c0-1234-5678-90ab-cdef01234567"

    def test_tool_set_to_kilo_code(self, kilo_task_dir: Path):
        session = parse_cline_session(kilo_task_dir, tool="kilo-code")
        assert session.tool == "kilo-code"

    def test_history_item_metadata_resolved(self, kilo_task_dir: Path):
        session = parse_cline_session(kilo_task_dir, tool="kilo-code")
        assert session.task_label == "Refactor rate limiter to Redis"
        assert session.workspace_folder == "/Users/dev/api"
        assert session.total_input_tokens == 7200
        assert session.total_output_tokens == 2100


class TestKiloCodeDiscovery:
    def test_fallback_scan_with_uuid_dirs(self, tmp_path: Path):
        storage = tmp_path / "kilocode.kilo-code"
        t_id = "0123abcd-4567-89ef-0123-456789abcdef"
        task_dir = storage / "tasks" / t_id
        task_dir.mkdir(parents=True)
        _make_kilo_api_conversation(task_dir)
        _make_history_item(task_dir, "Kilo fallback scan")

        sessions = discover_cline_sessions(storage, tool="kilo-code")
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == t_id
        assert sessions[0]["task_label"] == "Kilo fallback scan"


class TestKiloCodeConverter:
    def test_manifest_tool_is_kilo_code(self, kilo_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(kilo_task_dir, tool="kilo-code")
        sfs_dir = tmp_path / "out.sfs"
        convert_cline_to_sfs(session, sfs_dir)
        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["source"]["tool"] == "kilo-code"
        assert manifest["source"]["interface"] == "ide"
        assert manifest["source"]["original_session_id"] == "f7a8b9c0-1234-5678-90ab-cdef01234567"


class TestKiloCodeWatcherConfig:
    def test_default_storage_path_uses_kilocode_dir(self):
        from sessionfs.daemon.config import KiloCodeWatcherConfig

        cfg = KiloCodeWatcherConfig()
        assert "kilocode.kilo-code" in str(cfg.storage_dir)

    def test_default_storage_path_is_platform_aware(self):
        import platform

        from sessionfs.daemon.config import _default_kilo_code_storage

        path = _default_kilo_code_storage()
        if platform.system() == "Darwin":
            assert "Library/Application Support" in str(path)
        else:
            assert ".config/Code" in str(path)
