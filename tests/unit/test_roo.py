"""Tests for Roo Code VS Code extension — Cline fork differences.

Roo Code uses the same Anthropic MessageParam format as Cline, so shared
parsing is tested in test_cline.py. These tests focus on Roo-specific
differences:

- UUID task IDs (vs timestamp-based in Cline)
- Per-task history_item.json metadata
- tasks/_index.json for discovery
- tool="roo-code" in manifests
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.converters.cline_to_sfs import (
    ClineParsedSession,
    parse_cline_session,
    convert_cline_to_sfs,
    discover_cline_sessions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_roo_api_conversation(task_dir: Path) -> None:
    """Write a Roo Code API conversation (same Anthropic format)."""
    messages = [
        {
            "role": "user",
            "content": "Add input validation to the signup form.",
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll add validation for the email and password fields."},
                {
                    "type": "tool_use",
                    "id": "toolu_roo_01",
                    "name": "read_file",
                    "input": {"path": "src/components/Signup.tsx"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_roo_01",
                    "content": "export function Signup() { return <form>...</form> }",
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Done! Added email regex and password length checks."},
            ],
        },
    ]
    (task_dir / "api_conversation_history.json").write_text(json.dumps(messages))


def _make_history_item(task_dir: Path, task_label: str, workspace: str = "") -> None:
    """Write a Roo Code per-task history_item.json."""
    meta = {
        "id": task_dir.name,
        "task": task_label,
        "workspace": workspace,
        "ts": 1711100000000,
        "totalCost": {
            "inputTokens": 5400,
            "outputTokens": 1200,
        },
    }
    (task_dir / "history_item.json").write_text(json.dumps(meta))


@pytest.fixture
def roo_task_dir(tmp_path: Path) -> Path:
    """Create a Roo Code task directory with UUID-based task ID."""
    storage = tmp_path / "rooveterinaryinc.roo-cline"
    task_id = "a3f1b2c4-d5e6-7890-abcd-ef1234567890"
    task_dir = storage / "tasks" / task_id
    task_dir.mkdir(parents=True)
    _make_roo_api_conversation(task_dir)
    _make_history_item(task_dir, "Add signup validation", "/Users/dev/webapp")
    return task_dir


@pytest.fixture
def roo_storage_with_index(tmp_path: Path) -> Path:
    """Create a Roo Code storage dir with _index.json."""
    storage = tmp_path / "rooveterinaryinc.roo-cline"
    tasks_dir = storage / "tasks"

    # Task 1
    t1_id = "a3f1b2c4-d5e6-7890-abcd-ef1234567890"
    t1 = tasks_dir / t1_id
    t1.mkdir(parents=True)
    _make_roo_api_conversation(t1)
    _make_history_item(t1, "Add signup validation")

    # Task 2
    t2_id = "b4e2c3d5-f6a7-8901-bcde-f12345678901"
    t2 = tasks_dir / t2_id
    t2.mkdir(parents=True)
    _make_roo_api_conversation(t2)
    _make_history_item(t2, "Fix dark mode toggle")

    # _index.json
    index = [
        {"id": t1_id, "task": "Add signup validation"},
        {"id": t2_id, "task": "Fix dark mode toggle"},
    ]
    (tasks_dir / "_index.json").write_text(json.dumps(index))

    return storage


# ---------------------------------------------------------------------------
# UUID task ID tests
# ---------------------------------------------------------------------------


class TestRooUuidTaskIds:
    def test_uuid_task_id_parsed(self, roo_task_dir: Path):
        session = parse_cline_session(roo_task_dir, tool="roo-code")
        assert session.session_id == "a3f1b2c4-d5e6-7890-abcd-ef1234567890"

    def test_tool_set_to_roo_code(self, roo_task_dir: Path):
        session = parse_cline_session(roo_task_dir, tool="roo-code")
        assert session.tool == "roo-code"


# ---------------------------------------------------------------------------
# history_item.json tests
# ---------------------------------------------------------------------------


class TestRooHistoryItem:
    def test_task_label_from_history_item(self, roo_task_dir: Path):
        session = parse_cline_session(roo_task_dir, tool="roo-code")
        assert session.task_label == "Add signup validation"

    def test_workspace_from_history_item(self, roo_task_dir: Path):
        session = parse_cline_session(roo_task_dir, tool="roo-code")
        assert session.workspace_folder == "/Users/dev/webapp"

    def test_token_counts_from_history_item(self, roo_task_dir: Path):
        session = parse_cline_session(roo_task_dir, tool="roo-code")
        assert session.total_input_tokens == 5400
        assert session.total_output_tokens == 1200

    def test_created_at_from_history_item(self, roo_task_dir: Path):
        session = parse_cline_session(roo_task_dir, tool="roo-code")
        assert session.created_at is not None
        assert "2024" in session.created_at  # ts=1711100000000 -> 2024


# ---------------------------------------------------------------------------
# Discovery via _index.json
# ---------------------------------------------------------------------------


class TestRooDiscovery:
    def test_discover_via_index(self, roo_storage_with_index: Path):
        sessions = discover_cline_sessions(roo_storage_with_index, tool="roo-code")
        assert len(sessions) == 2
        ids = [s["session_id"] for s in sessions]
        assert "a3f1b2c4-d5e6-7890-abcd-ef1234567890" in ids
        assert "b4e2c3d5-f6a7-8901-bcde-f12345678901" in ids

    def test_discover_task_labels(self, roo_storage_with_index: Path):
        sessions = discover_cline_sessions(roo_storage_with_index, tool="roo-code")
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id["a3f1b2c4-d5e6-7890-abcd-ef1234567890"]["task_label"] == "Add signup validation"

    def test_fallback_scan_with_uuid_dirs(self, tmp_path: Path):
        """When no _index.json, scans task directories."""
        storage = tmp_path / "rooveterinaryinc.roo-cline"
        t_id = "c5d6e7f8-a9b0-1234-cdef-567890abcdef"
        task_dir = storage / "tasks" / t_id
        task_dir.mkdir(parents=True)
        _make_roo_api_conversation(task_dir)
        _make_history_item(task_dir, "Scan fallback test")

        sessions = discover_cline_sessions(storage, tool="roo-code")
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == t_id
        assert sessions[0]["task_label"] == "Scan fallback test"

    def test_index_file_skipped_as_task(self, roo_storage_with_index: Path):
        """_index.json should not appear as a task directory."""
        sessions = discover_cline_sessions(roo_storage_with_index, tool="roo-code")
        ids = [s["session_id"] for s in sessions]
        assert "_index.json" not in ids


# ---------------------------------------------------------------------------
# Converter with Roo Code tool name
# ---------------------------------------------------------------------------


class TestConvertRooToSfs:
    def test_manifest_tool_is_roo_code(self, roo_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(roo_task_dir, tool="roo-code")
        sfs_dir = tmp_path / "output.sfs"
        convert_cline_to_sfs(session, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["source"]["tool"] == "roo-code"
        assert manifest["source"]["interface"] == "ide"

    def test_token_counts_in_manifest(self, roo_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(roo_task_dir, tool="roo-code")
        sfs_dir = tmp_path / "output.sfs"
        convert_cline_to_sfs(session, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["stats"]["total_input_tokens"] == 5400
        assert manifest["stats"]["total_output_tokens"] == 1200

    def test_workspace_from_history_item(self, roo_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(roo_task_dir, tool="roo-code")
        sfs_dir = tmp_path / "output.sfs"
        convert_cline_to_sfs(session, sfs_dir)

        assert (sfs_dir / "workspace.json").exists()
        ws = json.loads((sfs_dir / "workspace.json").read_text())
        assert ws["root_path"] == "/Users/dev/webapp"

    def test_uuid_original_session_id(self, roo_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(roo_task_dir, tool="roo-code")
        sfs_dir = tmp_path / "output.sfs"
        convert_cline_to_sfs(session, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["source"]["original_session_id"] == "a3f1b2c4-d5e6-7890-abcd-ef1234567890"
