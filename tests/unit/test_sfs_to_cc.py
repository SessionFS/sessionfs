"""Tests for the reverse converter (.sfs → Claude Code JSONL)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.cli.sfs_to_cc import (
    _make_assistant_content,
    _make_user_content,
    _reverse_content_block,
    _reverse_message,
    add_index_entry,
    encode_project_path,
    read_sessions_index,
    reverse_convert_session,
    write_sessions_index,
)


class TestEncodeProjectPath:
    def test_basic(self):
        assert encode_project_path("/Users/ola/project") == "-Users-ola-project"

    def test_root(self):
        assert encode_project_path("/") == "-"


class TestReverseContentBlock:
    def test_text(self):
        block = {"type": "text", "text": "Hello"}
        result = _reverse_content_block(block)
        assert result == {"type": "text", "text": "Hello"}

    def test_thinking(self):
        block = {"type": "thinking", "text": "Let me think...", "signature": "sig123"}
        result = _reverse_content_block(block)
        assert result == {"type": "thinking", "thinking": "Let me think...", "signature": "sig123"}

    def test_thinking_no_signature(self):
        block = {"type": "thinking", "text": "hmm"}
        result = _reverse_content_block(block)
        assert result == {"type": "thinking", "thinking": "hmm"}
        assert "signature" not in result

    def test_tool_use(self):
        block = {
            "type": "tool_use",
            "tool_use_id": "toolu_123",
            "name": "Read",
            "input": {"file_path": "/foo"},
        }
        result = _reverse_content_block(block)
        assert result == {
            "type": "tool_use",
            "id": "toolu_123",
            "name": "Read",
            "input": {"file_path": "/foo"},
        }

    def test_tool_result(self):
        block = {
            "type": "tool_result",
            "tool_use_id": "toolu_123",
            "content": "file contents",
            "is_error": True,
        }
        result = _reverse_content_block(block)
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "toolu_123"
        assert result["is_error"] is True

    def test_summary_becomes_text(self):
        block = {"type": "summary", "text": "This session discussed..."}
        result = _reverse_content_block(block)
        assert result == {"type": "text", "text": "This session discussed..."}


class TestMakeUserContent:
    def test_simple_text(self):
        blocks = [{"type": "text", "text": "Hello"}]
        result = _make_user_content(blocks)
        assert result == "Hello"

    def test_with_tool_results(self):
        blocks = [
            {"type": "tool_result", "tool_use_id": "t1", "content": "output"},
        ]
        result = _make_user_content(blocks)
        assert isinstance(result, list)
        assert len(result) == 1


class TestMakeAssistantContent:
    def test_always_list(self):
        blocks = [{"type": "text", "text": "Response"}]
        result = _make_assistant_content(blocks)
        assert isinstance(result, list)
        assert len(result) == 1


class TestReverseMessage:
    def test_user_message(self):
        msg = {
            "msg_id": "uuid-1",
            "role": "user",
            "content": [{"type": "text", "text": "Hello"}],
            "timestamp": "2026-03-20T10:00:00Z",
            "metadata": {},
        }
        result = _reverse_message(msg, "cc-sess-1", "/tmp", "main", "2.1.59", "")
        assert result is not None
        assert result["type"] == "user"
        assert result["message"]["role"] == "user"
        assert result["message"]["content"] == "Hello"
        assert result["sessionId"] == "cc-sess-1"

    def test_assistant_message(self):
        msg = {
            "msg_id": "uuid-2",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hi there"}],
            "timestamp": "2026-03-20T10:00:01Z",
            "model": "claude-opus-4-6",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": 10,
                "cache_write_tokens": 5,
            },
            "metadata": {"cc_request_id": "req_abc"},
        }
        result = _reverse_message(msg, "cc-sess-1", "/tmp", "main", "2.1.59", "")
        assert result is not None
        assert result["type"] == "assistant"
        assert result["message"]["model"] == "claude-opus-4-6"
        assert result["message"]["usage"]["cache_creation_input_tokens"] == 5
        assert result["message"]["usage"]["cache_read_input_tokens"] == 10
        assert result["requestId"] == "req_abc"

    def test_tool_role_becomes_user(self):
        msg = {
            "msg_id": "uuid-3",
            "role": "tool",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "output"},
            ],
            "timestamp": "2026-03-20T10:00:02Z",
            "metadata": {},
        }
        result = _reverse_message(msg, "cc-sess-1", "/tmp", "main", "2.1.59", "")
        assert result is not None
        assert result["type"] == "user"
        assert isinstance(result["message"]["content"], list)

    def test_system_summary_becomes_cc_summary(self):
        msg = {
            "msg_id": "uuid-4",
            "role": "system",
            "content": [{"type": "summary", "text": "Previous conversation..."}],
            "timestamp": "2026-03-20T10:00:00Z",
            "parent_msg_id": "leaf-uuid",
            "metadata": {},
        }
        result = _reverse_message(msg, "cc-sess-1", "/tmp", "main", "2.1.59", "")
        assert result is not None
        assert result["type"] == "summary"
        assert result["summary"] == "Previous conversation..."
        assert result["leafUuid"] == "leaf-uuid"

    def test_developer_becomes_user_with_is_meta(self):
        msg = {
            "msg_id": "uuid-5",
            "role": "developer",
            "content": [{"type": "text", "text": "System context"}],
            "timestamp": "2026-03-20T10:00:00Z",
            "metadata": {},
        }
        result = _reverse_message(msg, "cc-sess-1", "/tmp", "main", "2.1.59", "")
        assert result is not None
        assert result["type"] == "user"
        assert result["isMeta"] is True

    def test_metadata_round_trip(self):
        msg = {
            "msg_id": "uuid-6",
            "role": "user",
            "content": [{"type": "text", "text": "test"}],
            "timestamp": "2026-03-20T10:00:00Z",
            "metadata": {
                "cc_cwd": "/special/path",
                "cc_git_branch": "feature-x",
            },
        }
        result = _reverse_message(msg, "cc-sess-1", "/default", "main", "2.1.59", "")
        assert result["cwd"] == "/special/path"
        assert result["gitBranch"] == "feature-x"

    def test_unknown_role_returns_none(self):
        msg = {
            "msg_id": "uuid-7",
            "role": "alien",
            "content": [{"type": "text", "text": "???"}],
            "timestamp": "2026-03-20T10:00:00Z",
            "metadata": {},
        }
        result = _reverse_message(msg, "cc-sess-1", "/tmp", "main", "2.1.59", "")
        assert result is None


class TestSessionsIndex:
    def test_read_write(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        index = read_sessions_index(project_dir)
        assert index == {"version": 1, "entries": []}

        add_index_entry(project_dir, "sess-1", "/path", "Hello", 5, "main")

        index = read_sessions_index(project_dir)
        assert len(index["entries"]) == 1
        assert index["entries"][0]["sessionId"] == "sess-1"
        assert index["entries"][0]["firstPrompt"] == "Hello"

    def test_upsert_replaces(self, tmp_path: Path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        add_index_entry(project_dir, "sess-1", "/path", "First", 5)
        add_index_entry(project_dir, "sess-1", "/path", "Updated", 10)

        index = read_sessions_index(project_dir)
        assert len(index["entries"]) == 1
        assert index["entries"][0]["firstPrompt"] == "Updated"


class TestReverseConvertSession:
    def _make_sfs_session(self, tmp_path: Path) -> Path:
        """Create a minimal .sfs session for testing."""
        session_dir = tmp_path / "test-session.sfs"
        session_dir.mkdir()

        manifest = {
            "sfs_version": "0.1.0",
            "session_id": "test-123",
            "title": "Test Session",
            "created_at": "2026-03-20T10:00:00Z",
            "source": {"tool": "claude-code"},
            "model": {"provider": "anthropic", "model_id": "claude-opus-4-6"},
            "stats": {"message_count": 2},
        }
        (session_dir / "manifest.json").write_text(json.dumps(manifest))

        messages = [
            {
                "msg_id": "msg-1",
                "role": "user",
                "content": [{"type": "text", "text": "Hello"}],
                "timestamp": "2026-03-20T10:00:00Z",
            },
            {
                "msg_id": "msg-2",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hi!"}],
                "timestamp": "2026-03-20T10:00:01Z",
                "model": "claude-opus-4-6",
                "stop_reason": "end_turn",
            },
        ]
        with open(session_dir / "messages.jsonl", "w") as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")

        workspace = {"root_path": "/Users/test/project", "git": {"branch": "main"}}
        (session_dir / "workspace.json").write_text(json.dumps(workspace))

        return session_dir

    def test_export_mode(self, tmp_path: Path):
        session_dir = self._make_sfs_session(tmp_path)
        output_path = tmp_path / "output.jsonl"

        result = reverse_convert_session(session_dir, output_path=output_path)

        assert result["message_count"] == 3  # 2 original + 1 handoff context
        assert output_path.exists()

        lines = output_path.read_text().strip().split("\n")
        assert len(lines) == 3  # 2 original + 1 handoff
        first = json.loads(lines[0])
        assert first["type"] == "user"
        second = json.loads(lines[1])
        assert second["type"] == "assistant"
        handoff = json.loads(lines[2])
        assert handoff["type"] == "user"
        assert "[SessionFS Resume]" in handoff["message"]["content"]

    def test_resume_mode(self, tmp_path: Path):
        session_dir = self._make_sfs_session(tmp_path)
        cc_home = tmp_path / ".claude"

        result = reverse_convert_session(
            session_dir,
            target_project_path="/Users/test/project",
            cc_home=cc_home,
        )

        assert result["message_count"] == 3  # 2 original + 1 handoff context
        assert result["project_dir"] is not None

        # Check JSONL was written
        jsonl_path = Path(result["jsonl_path"])
        assert jsonl_path.exists()

        # Check index was updated
        project_dir = Path(result["project_dir"])
        index = read_sessions_index(project_dir)
        assert len(index["entries"]) == 1
        assert index["entries"][0]["sessionId"] == result["cc_session_id"]

    def test_sidechain_filtered(self, tmp_path: Path):
        session_dir = self._make_sfs_session(tmp_path)

        # Add a sidechain message
        with open(session_dir / "messages.jsonl", "a") as f:
            sidechain_msg = {
                "msg_id": "msg-3",
                "role": "assistant",
                "content": [{"type": "text", "text": "Sidechain response"}],
                "timestamp": "2026-03-20T10:00:02Z",
                "is_sidechain": True,
                "agent_id": "agent-1",
            }
            f.write(json.dumps(sidechain_msg) + "\n")

        output_path = tmp_path / "output.jsonl"
        result = reverse_convert_session(session_dir, output_path=output_path)

        # Sidechain should be filtered out
        assert result["message_count"] == 3  # 2 original + 1 handoff context
