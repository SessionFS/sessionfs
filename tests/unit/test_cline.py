"""Tests for Cline VS Code extension converter, parser, and discovery.

Tests cover both Cline and the shared parsing logic used by Roo Code.
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


def _make_api_conversation(task_dir: Path) -> None:
    """Write a realistic Anthropic MessageParam conversation."""
    messages = [
        {
            "role": "user",
            "content": "Refactor the auth middleware to use JWT tokens.",
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "I need to analyze the current auth code."},
                {"type": "text", "text": "I'll refactor the middleware. Let me start by reading the current implementation."},
                {
                    "type": "tool_use",
                    "id": "toolu_01abc",
                    "name": "read_file",
                    "input": {"path": "src/auth/middleware.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01abc",
                    "content": "class AuthMiddleware:\n    def verify(self, token):\n        return True",
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I see the issue. The verify method always returns True."},
                {
                    "type": "tool_use",
                    "id": "toolu_02def",
                    "name": "write_to_file",
                    "input": {
                        "path": "src/auth/middleware.py",
                        "content": "import jwt\n\nclass AuthMiddleware:\n    def verify(self, token):\n        return jwt.decode(token, 'secret', algorithms=['HS256'])",
                    },
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_02def",
                    "content": "File written successfully.",
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Done! I've updated the auth middleware to properly verify JWT tokens."},
            ],
        },
    ]
    (task_dir / "api_conversation_history.json").write_text(json.dumps(messages))


def _make_ui_messages(task_dir: Path) -> None:
    """Write a Cline ui_messages.json file."""
    messages = [
        {"type": "say", "say": "user_feedback", "text": "Fix the login page", "ts": 1711100000000},
        {"type": "say", "say": "text", "text": "I'll fix the login page CSS.", "ts": 1711100005000},
        {"type": "say", "say": "tool", "text": '{"tool": "write_to_file", "path": "login.css"}', "ts": 1711100010000},
        {"type": "ask", "ask": "followup", "text": "Can you also add dark mode?", "ts": 1711100020000},
        {"type": "say", "say": "text", "text": "Sure, adding dark mode support now.", "ts": 1711100025000},
    ]
    (task_dir / "ui_messages.json").write_text(json.dumps(messages))


@pytest.fixture
def cline_task_dir(tmp_path: Path) -> Path:
    """Create a Cline task directory with API conversation history."""
    storage = tmp_path / "saoudrizwan.claude-dev"
    task_dir = storage / "tasks" / "1711100000000"
    task_dir.mkdir(parents=True)
    _make_api_conversation(task_dir)
    return task_dir


@pytest.fixture
def cline_storage_with_index(tmp_path: Path) -> Path:
    """Create a Cline storage dir with taskHistory.json index."""
    storage = tmp_path / "saoudrizwan.claude-dev"
    tasks_dir = storage / "tasks"
    state_dir = storage / "state"

    # Task 1
    task1 = tasks_dir / "1711100000000"
    task1.mkdir(parents=True)
    _make_api_conversation(task1)

    # Task 2 (UI messages only)
    task2 = tasks_dir / "1711200000000"
    task2.mkdir(parents=True)
    _make_ui_messages(task2)

    # Task history index
    state_dir.mkdir(parents=True)
    history = [
        {"id": "1711100000000", "task": "Refactor auth middleware"},
        {"id": "1711200000000", "task": "Fix login page"},
    ]
    (state_dir / "taskHistory.json").write_text(json.dumps(history))

    return storage


@pytest.fixture
def cline_task_ui_only(tmp_path: Path) -> Path:
    """Task dir with only ui_messages.json (no API history)."""
    storage = tmp_path / "saoudrizwan.claude-dev"
    task_dir = storage / "tasks" / "1711300000000"
    task_dir.mkdir(parents=True)
    _make_ui_messages(task_dir)
    return task_dir


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestParseClineSession:
    def test_basic_parse(self, cline_task_dir: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        assert session.session_id == "1711100000000"
        assert session.tool == "cline"
        assert session.message_count > 0

    def test_user_messages(self, cline_task_dir: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        user_msgs = [m for m in session.messages if m["role"] == "user"]
        assert len(user_msgs) >= 1
        assert "JWT" in user_msgs[0]["content"][0]["text"]

    def test_assistant_messages(self, cline_task_dir: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        assert len(asst_msgs) >= 2

    def test_thinking_blocks(self, cline_task_dir: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        first = asst_msgs[0]
        thinking = [c for c in first["content"] if c.get("type") == "thinking"]
        assert len(thinking) == 1
        assert "auth" in thinking[0]["text"].lower()

    def test_tool_use(self, cline_task_dir: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        tool_uses = []
        for msg in asst_msgs:
            for block in msg["content"]:
                if block.get("type") == "tool_use":
                    tool_uses.append(block)
        assert len(tool_uses) >= 2
        assert tool_uses[0]["name"] == "read_file"
        assert session.tool_use_count >= 2

    def test_tool_results(self, cline_task_dir: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        tool_msgs = [m for m in session.messages if m["role"] == "tool"]
        assert len(tool_msgs) >= 2
        assert tool_msgs[0]["content"][0]["type"] == "tool_result"
        assert "toolu_01abc" in tool_msgs[0]["content"][0]["tool_use_id"]

    def test_turn_count(self, cline_task_dir: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        # First user message = turn 1
        assert session.turn_count >= 1

    def test_missing_task_dir(self, tmp_path: Path):
        session = parse_cline_session(tmp_path / "nonexistent", tool="cline")
        assert session.message_count == 0
        assert len(session.parse_errors) > 0

    def test_empty_api_history(self, tmp_path: Path):
        task_dir = tmp_path / "tasks" / "empty"
        task_dir.mkdir(parents=True)
        (task_dir / "api_conversation_history.json").write_text("[]")
        session = parse_cline_session(task_dir, tool="cline")
        assert session.message_count == 0


class TestParseClineUiMessages:
    def test_fallback_to_ui_messages(self, cline_task_ui_only: Path):
        session = parse_cline_session(cline_task_ui_only, tool="cline")
        assert session.message_count > 0

    def test_ui_user_feedback(self, cline_task_ui_only: Path):
        session = parse_cline_session(cline_task_ui_only, tool="cline")
        user_msgs = [m for m in session.messages if m["role"] == "user"]
        assert any("login" in m["content"][0]["text"].lower() for m in user_msgs)

    def test_ui_assistant_text(self, cline_task_ui_only: Path):
        session = parse_cline_session(cline_task_ui_only, tool="cline")
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        assert len(asst_msgs) >= 2  # text + tool

    def test_ui_tool_use(self, cline_task_ui_only: Path):
        session = parse_cline_session(cline_task_ui_only, tool="cline")
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        tool_uses = [
            m for m in asst_msgs
            if any(c.get("type") == "tool_use" for c in m["content"])
        ]
        assert len(tool_uses) >= 1


# ---------------------------------------------------------------------------
# Converter tests
# ---------------------------------------------------------------------------


class TestConvertClineToSfs:
    def test_produces_valid_sfs(self, cline_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        sfs_dir = tmp_path / "output.sfs"
        convert_cline_to_sfs(session, sfs_dir)

        assert (sfs_dir / "manifest.json").exists()
        assert (sfs_dir / "messages.jsonl").exists()

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["source"]["tool"] == "cline"
        assert manifest["source"]["interface"] == "ide"
        assert manifest["source"]["original_session_id"] == "1711100000000"

    def test_message_count_in_manifest(self, cline_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        sfs_dir = tmp_path / "output.sfs"
        convert_cline_to_sfs(session, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["stats"]["message_count"] == session.message_count
        assert manifest["stats"]["tool_use_count"] >= 2

    def test_title_from_first_message(self, cline_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        sfs_dir = tmp_path / "output.sfs"
        convert_cline_to_sfs(session, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        # Title should be extracted from user message about JWT
        assert manifest["title"] is not None
        assert "JWT" in manifest["title"] or "auth" in manifest["title"].lower()

    def test_title_from_task_label(self, cline_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        session.task_label = "Refactor auth middleware"
        sfs_dir = tmp_path / "output.sfs"
        convert_cline_to_sfs(session, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["title"] == "Refactor auth middleware"

    def test_messages_jsonl_roundtrip(self, cline_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        sfs_dir = tmp_path / "output.sfs"
        convert_cline_to_sfs(session, sfs_dir)

        lines = (sfs_dir / "messages.jsonl").read_text().strip().split("\n")
        assert len(lines) == session.message_count
        for line in lines:
            msg = json.loads(line)
            assert "role" in msg
            assert "content" in msg

    def test_workspace_written(self, cline_task_dir: Path, tmp_path: Path):
        session = parse_cline_session(cline_task_dir, tool="cline")
        session.workspace_folder = "/Users/test/myproject"
        sfs_dir = tmp_path / "output.sfs"
        convert_cline_to_sfs(session, sfs_dir)

        assert (sfs_dir / "workspace.json").exists()
        ws = json.loads((sfs_dir / "workspace.json").read_text())
        assert ws["root_path"] == "/Users/test/myproject"


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


class TestDiscoverClineSessions:
    def test_discover_via_index(self, cline_storage_with_index: Path):
        sessions = discover_cline_sessions(cline_storage_with_index, tool="cline")
        assert len(sessions) == 2
        ids = [s["session_id"] for s in sessions]
        assert "1711100000000" in ids
        assert "1711200000000" in ids

    def test_discover_task_label(self, cline_storage_with_index: Path):
        sessions = discover_cline_sessions(cline_storage_with_index, tool="cline")
        by_id = {s["session_id"]: s for s in sessions}
        assert by_id["1711100000000"]["task_label"] == "Refactor auth middleware"

    def test_fallback_scan(self, tmp_path: Path):
        """When no index exists, falls back to directory scan."""
        storage = tmp_path / "saoudrizwan.claude-dev"
        task_dir = storage / "tasks" / "1711100000000"
        task_dir.mkdir(parents=True)
        _make_api_conversation(task_dir)

        sessions = discover_cline_sessions(storage, tool="cline")
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "1711100000000"

    def test_empty_storage(self, tmp_path: Path):
        sessions = discover_cline_sessions(tmp_path, tool="cline")
        assert sessions == []


# ---------------------------------------------------------------------------
# Anthropic format edge cases
# ---------------------------------------------------------------------------


class TestAnthropicFormatEdgeCases:
    def test_string_user_content(self, tmp_path: Path):
        """User content as plain string (common in Cline)."""
        task_dir = tmp_path / "tasks" / "t1"
        task_dir.mkdir(parents=True)
        (task_dir / "api_conversation_history.json").write_text(json.dumps([
            {"role": "user", "content": "Hello world"},
            {"role": "assistant", "content": [{"type": "text", "text": "Hi!"}]},
        ]))
        session = parse_cline_session(task_dir)
        assert session.message_count == 2
        user_msg = session.messages[0]
        assert user_msg["content"][0]["text"] == "Hello world"

    def test_assistant_string_content(self, tmp_path: Path):
        """Assistant content as plain string."""
        task_dir = tmp_path / "tasks" / "t2"
        task_dir.mkdir(parents=True)
        (task_dir / "api_conversation_history.json").write_text(json.dumps([
            {"role": "user", "content": "What's 2+2?"},
            {"role": "assistant", "content": "4"},
        ]))
        session = parse_cline_session(task_dir)
        assert session.message_count == 2

    def test_tool_result_with_error(self, tmp_path: Path):
        """Tool result with is_error flag."""
        task_dir = tmp_path / "tasks" / "t3"
        task_dir.mkdir(parents=True)
        (task_dir / "api_conversation_history.json").write_text(json.dumps([
            {"role": "user", "content": "Run the test"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "pytest"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "FAILED", "is_error": True},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "The test failed."}]},
        ]))
        session = parse_cline_session(task_dir)
        tool_msgs = [m for m in session.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"][0]["is_error"] is True

    def test_empty_text_blocks_skipped(self, tmp_path: Path):
        """Empty text blocks should be skipped."""
        task_dir = tmp_path / "tasks" / "t4"
        task_dir.mkdir(parents=True)
        (task_dir / "api_conversation_history.json").write_text(json.dumps([
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": [
                {"type": "text", "text": ""},
                {"type": "text", "text": "Real response"},
            ]},
        ]))
        session = parse_cline_session(task_dir)
        asst = [m for m in session.messages if m["role"] == "assistant"]
        assert len(asst) == 1
        assert len(asst[0]["content"]) == 1
        assert asst[0]["content"][0]["text"] == "Real response"
