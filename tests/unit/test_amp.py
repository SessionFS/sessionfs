"""Tests for Amp converters, parser, and discovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.converters.amp_to_sfs import (
    AmpParsedSession,
    parse_amp_session,
    convert_amp_to_sfs,
    discover_amp_sessions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def amp_thread_file(tmp_path: Path) -> Path:
    """Create a realistic Amp thread JSON file."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)

    thread_data = {
        "id": "thr_abc123def456",
        "title": "Fix the auth middleware",
        "created": 1711100000000,  # ms since epoch
        "messages": [
            {
                "role": "user",
                "messageId": 1,
                "content": [{"type": "text", "text": "Why is the token refresh returning 401?"}],
            },
            {
                "role": "assistant",
                "messageId": 2,
                "content": [
                    {"type": "text", "text": "I'll check the token expiry logic in your auth module."},
                    {"type": "text", "text": "The issue is in `src/auth/token.py` line 42."},
                ],
            },
            {
                "role": "user",
                "messageId": 3,
                "content": [{"type": "text", "text": "Can you fix it?"}],
            },
            {
                "role": "assistant",
                "messageId": 4,
                "content": [{"type": "text", "text": "Done! I've fixed the off-by-one error."}],
            },
        ],
        "usageLedger": {
            "events": [
                {"inputTokens": 150, "outputTokens": 200},
                {"inputTokens": 100, "outputTokens": 180},
            ],
        },
        "env": {
            "initial": {
                "tags": ["model:claude-sonnet-4", "tool:amp"],
            },
        },
    }

    path = threads_dir / "thr_abc123def456.json"
    path.write_text(json.dumps(thread_data))
    return path


@pytest.fixture
def amp_thread_no_title(tmp_path: Path) -> Path:
    """Create an Amp thread with no title."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True, exist_ok=True)

    thread_data = {
        "id": "thr_notitle789",
        "created": 1711100000000,
        "messages": [
            {
                "role": "user",
                "messageId": 1,
                "content": [{"type": "text", "text": "Explain how sessions work"}],
            },
            {
                "role": "assistant",
                "messageId": 2,
                "content": [{"type": "text", "text": "Sessions are stored as JSON files."}],
            },
        ],
        "usageLedger": {"events": []},
        "env": {"initial": {"tags": []}},
    }

    path = threads_dir / "thr_notitle789.json"
    path.write_text(json.dumps(thread_data))
    return path


@pytest.fixture
def amp_thread_with_tools(tmp_path: Path) -> Path:
    """Create an Amp thread with tool_use and tool_result blocks."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True, exist_ok=True)

    thread_data = {
        "id": "thr_tools999",
        "title": "Refactor auth module",
        "created": 1711100000000,
        "messages": [
            {
                "role": "user",
                "messageId": 1,
                "content": [{"type": "text", "text": "Read the auth module and fix the bug"}],
            },
            {
                "role": "assistant",
                "messageId": 2,
                "content": [
                    {"type": "text", "text": "I'll read the auth module first."},
                    {
                        "type": "tool_use",
                        "id": "tu_001",
                        "name": "read_file",
                        "input": {"path": "src/auth/token.py"},
                    },
                ],
            },
            {
                "role": "user",
                "messageId": 3,
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_001",
                        "content": "def refresh_token(tok):\n    return tok",
                        "is_error": False,
                    },
                ],
            },
            {
                "role": "assistant",
                "messageId": 4,
                "content": [
                    {"type": "text", "text": "I see the issue. Let me fix it."},
                    {
                        "type": "tool_use",
                        "id": "tu_002",
                        "name": "edit_file",
                        "input": {"path": "src/auth/token.py", "content": "fixed"},
                    },
                ],
            },
            {
                "role": "user",
                "messageId": 5,
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_002",
                        "content": "File edited successfully",
                        "is_error": False,
                    },
                ],
            },
            {
                "role": "assistant",
                "messageId": 6,
                "content": [{"type": "text", "text": "Done! The bug is fixed."}],
            },
        ],
        "usageLedger": {
            "events": [
                {"inputTokens": 500, "outputTokens": 300},
            ],
        },
        "env": {
            "initial": {
                "tags": ["model:claude-sonnet-4"],
            },
        },
    }

    path = threads_dir / "thr_tools999.json"
    path.write_text(json.dumps(thread_data))
    return path


@pytest.fixture
def amp_thread_tool_error(tmp_path: Path) -> Path:
    """Create an Amp thread with a tool_result that has is_error=True."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True, exist_ok=True)

    thread_data = {
        "id": "thr_toolerr",
        "title": "Tool error thread",
        "created": 1711100000000,
        "messages": [
            {
                "role": "user",
                "messageId": 1,
                "content": [{"type": "text", "text": "Delete the temp files"}],
            },
            {
                "role": "assistant",
                "messageId": 2,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_err1",
                        "name": "delete_file",
                        "input": {"path": "/tmp/nonexistent"},
                    },
                ],
            },
            {
                "role": "user",
                "messageId": 3,
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_err1",
                        "content": "Error: file not found",
                        "is_error": True,
                    },
                ],
            },
            {
                "role": "assistant",
                "messageId": 4,
                "content": [{"type": "text", "text": "The file doesn't exist."}],
            },
        ],
        "usageLedger": {"events": []},
        "env": {"initial": {"tags": []}},
    }

    path = threads_dir / "thr_toolerr.json"
    path.write_text(json.dumps(thread_data))
    return path


@pytest.fixture
def amp_thread_empty(tmp_path: Path) -> Path:
    """Create an Amp thread with no messages."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True, exist_ok=True)

    thread_data = {
        "id": "thr_empty000",
        "title": "Empty thread",
        "created": 1711100000000,
        "messages": [],
        "usageLedger": {"events": []},
        "env": {"initial": {"tags": ["model:claude-sonnet-4"]}},
    }

    path = threads_dir / "thr_empty000.json"
    path.write_text(json.dumps(thread_data))
    return path


# ---------------------------------------------------------------------------
# Parse tests
# ---------------------------------------------------------------------------


class TestParseAmpSession:
    def test_basic_parse(self, amp_thread_file: Path):
        session = parse_amp_session(amp_thread_file)
        assert session.session_id == "thr_abc123def456"
        assert session.title == "Fix the auth middleware"

    def test_message_count(self, amp_thread_file: Path):
        session = parse_amp_session(amp_thread_file)
        assert session.message_count == 4  # 2 user + 2 assistant

    def test_turn_count(self, amp_thread_file: Path):
        session = parse_amp_session(amp_thread_file)
        assert session.turn_count == 2

    def test_role_mapping(self, amp_thread_file: Path):
        session = parse_amp_session(amp_thread_file)
        roles = [m["role"] for m in session.messages]
        assert roles == ["user", "assistant", "user", "assistant"]

    def test_user_content(self, amp_thread_file: Path):
        session = parse_amp_session(amp_thread_file)
        user_msgs = [m for m in session.messages if m["role"] == "user"]
        assert "401" in user_msgs[0]["content"][0]["text"]

    def test_assistant_content(self, amp_thread_file: Path):
        session = parse_amp_session(amp_thread_file)
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        # First assistant message has two text blocks
        assert len(asst_msgs[0]["content"]) == 2
        assert "token expiry" in asst_msgs[0]["content"][0]["text"]

    def test_model_extraction(self, amp_thread_file: Path):
        session = parse_amp_session(amp_thread_file)
        assert session.model == "claude-sonnet-4"

    def test_token_usage(self, amp_thread_file: Path):
        session = parse_amp_session(amp_thread_file)
        assert session.total_input_tokens == 250
        assert session.total_output_tokens == 380

    def test_created_time(self, amp_thread_file: Path):
        session = parse_amp_session(amp_thread_file)
        assert session.start_time is not None
        assert "2024-03-22" in session.start_time

    def test_no_title(self, amp_thread_no_title: Path):
        session = parse_amp_session(amp_thread_no_title)
        assert session.title is None

    def test_empty_messages(self, amp_thread_empty: Path):
        session = parse_amp_session(amp_thread_empty)
        assert session.message_count == 0
        assert session.turn_count == 0

    def test_no_model_tags(self, amp_thread_no_title: Path):
        session = parse_amp_session(amp_thread_no_title)
        assert session.model is None


# ---------------------------------------------------------------------------
# Converter tests
# ---------------------------------------------------------------------------


class TestConvertAmpToSfs:
    def test_produces_valid_sfs(self, amp_thread_file: Path, tmp_path: Path):
        sfs_dir = tmp_path / "output.sfs"
        convert_amp_to_sfs(amp_thread_file, sfs_dir)

        assert (sfs_dir / "manifest.json").exists()
        assert (sfs_dir / "messages.jsonl").exists()

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["source"]["tool"] == "amp"
        assert manifest["source"]["original_session_id"] == "thr_abc123def456"

    def test_title_from_thread(self, amp_thread_file: Path, tmp_path: Path):
        sfs_dir = tmp_path / "output.sfs"
        convert_amp_to_sfs(amp_thread_file, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["title"] == "Fix the auth middleware"

    def test_model_in_manifest(self, amp_thread_file: Path, tmp_path: Path):
        sfs_dir = tmp_path / "output.sfs"
        convert_amp_to_sfs(amp_thread_file, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["model"]["model_id"] == "claude-sonnet-4"
        assert manifest["model"]["provider"] == "anthropic"

    def test_token_counts_in_stats(self, amp_thread_file: Path, tmp_path: Path):
        sfs_dir = tmp_path / "output.sfs"
        convert_amp_to_sfs(amp_thread_file, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["stats"]["total_input_tokens"] == 250
        assert manifest["stats"]["total_output_tokens"] == 380

    def test_messages_jsonl_content(self, amp_thread_file: Path, tmp_path: Path):
        sfs_dir = tmp_path / "output.sfs"
        convert_amp_to_sfs(amp_thread_file, sfs_dir)

        lines = (sfs_dir / "messages.jsonl").read_text().strip().split("\n")
        assert len(lines) == 4
        first = json.loads(lines[0])
        assert first["role"] == "user"
        assert "401" in first["content"][0]["text"]

    def test_session_id_override(self, amp_thread_file: Path, tmp_path: Path):
        sfs_dir = tmp_path / "output.sfs"
        convert_amp_to_sfs(amp_thread_file, sfs_dir, session_id="ses_custom12345678")

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["session_id"] == "ses_custom12345678"

    def test_no_title_uses_message(self, amp_thread_no_title: Path, tmp_path: Path):
        sfs_dir = tmp_path / "output.sfs"
        convert_amp_to_sfs(amp_thread_no_title, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        # Should extract title from first user message
        assert manifest["title"] is not None
        assert "session" in manifest["title"].lower() or "explain" in manifest["title"].lower()


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_sessions(self, amp_thread_file: Path, tmp_path: Path):
        sessions = discover_amp_sessions(tmp_path)
        assert len(sessions) >= 1
        assert sessions[0]["session_id"] == "thr_abc123def456"
        assert sessions[0]["path"] == str(amp_thread_file)

    def test_discover_multiple(self, amp_thread_file: Path, amp_thread_empty: Path, tmp_path: Path):
        sessions = discover_amp_sessions(tmp_path)
        assert len(sessions) >= 2
        ids = {s["session_id"] for s in sessions}
        assert "thr_abc123def456" in ids
        assert "thr_empty000" in ids

    def test_discover_empty_dir(self, tmp_path: Path):
        sessions = discover_amp_sessions(tmp_path)
        assert sessions == []

    def test_discover_no_threads_dir(self, tmp_path: Path):
        sessions = discover_amp_sessions(tmp_path / "nonexistent")
        assert sessions == []

    def test_session_has_mtime(self, amp_thread_file: Path, tmp_path: Path):
        sessions = discover_amp_sessions(tmp_path)
        assert sessions[0]["mtime"] > 0

    def test_session_has_size(self, amp_thread_file: Path, tmp_path: Path):
        sessions = discover_amp_sessions(tmp_path)
        assert sessions[0]["size_bytes"] > 0


# ---------------------------------------------------------------------------
# Tool call extraction tests
# ---------------------------------------------------------------------------


class TestToolCallExtraction:
    def test_tool_use_count(self, amp_thread_with_tools: Path):
        session = parse_amp_session(amp_thread_with_tools)
        assert session.tool_use_count == 2

    def test_tool_use_blocks_in_messages(self, amp_thread_with_tools: Path):
        session = parse_amp_session(amp_thread_with_tools)
        # Second message (assistant) should have text + tool_use
        asst_msg = session.messages[1]
        assert asst_msg["role"] == "assistant"
        tool_blocks = [b for b in asst_msg["content"] if b["type"] == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0]["name"] == "read_file"
        assert tool_blocks[0]["tool_use_id"] == "tu_001"
        assert tool_blocks[0]["input"] == {"path": "src/auth/token.py"}

    def test_tool_result_blocks_in_messages(self, amp_thread_with_tools: Path):
        session = parse_amp_session(amp_thread_with_tools)
        # Third message (user) should have tool_result
        user_msg = session.messages[2]
        assert user_msg["role"] == "user"
        result_blocks = [b for b in user_msg["content"] if b["type"] == "tool_result"]
        assert len(result_blocks) == 1
        assert result_blocks[0]["tool_use_id"] == "tu_001"
        assert "refresh_token" in result_blocks[0]["content"]

    def test_tool_result_no_error_flag(self, amp_thread_with_tools: Path):
        """Non-error tool results should not have is_error key."""
        session = parse_amp_session(amp_thread_with_tools)
        user_msg = session.messages[2]
        result_block = user_msg["content"][0]
        assert "is_error" not in result_block

    def test_tool_error_flag(self, amp_thread_tool_error: Path):
        session = parse_amp_session(amp_thread_tool_error)
        # Message index 2 = tool_result with error
        user_msg = session.messages[2]
        result_block = user_msg["content"][0]
        assert result_block["type"] == "tool_result"
        assert result_block["is_error"] is True
        assert "file not found" in result_block["content"]

    def test_tool_use_count_in_manifest(self, amp_thread_with_tools: Path, tmp_path: Path):
        sfs_dir = tmp_path / "output.sfs"
        convert_amp_to_sfs(amp_thread_with_tools, sfs_dir)
        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["stats"]["tool_use_count"] == 2

    def test_tool_use_count_zero_for_text_only(self, amp_thread_file: Path):
        session = parse_amp_session(amp_thread_file)
        assert session.tool_use_count == 0

    def test_message_count_includes_tool_messages(self, amp_thread_with_tools: Path):
        session = parse_amp_session(amp_thread_with_tools)
        # 6 messages total: user, assistant(text+tool), user(result),
        # assistant(text+tool), user(result), assistant(text)
        assert session.message_count == 6

    def test_tool_messages_in_jsonl(self, amp_thread_with_tools: Path, tmp_path: Path):
        sfs_dir = tmp_path / "output.sfs"
        convert_amp_to_sfs(amp_thread_with_tools, sfs_dir)
        lines = (sfs_dir / "messages.jsonl").read_text().strip().split("\n")
        assert len(lines) == 6
        # Check that tool_use block is present in JSONL
        second_msg = json.loads(lines[1])
        tool_blocks = [b for b in second_msg["content"] if b["type"] == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0]["name"] == "read_file"
