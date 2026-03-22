"""Tests for Gemini CLI converters, parser, and watcher."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.converters.gemini_to_sfs import (
    GeminiParsedSession,
    parse_gemini_session,
    convert_gemini_to_sfs,
    discover_gemini_sessions,
)
from sessionfs.converters.sfs_to_gemini import convert_sfs_to_gemini


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gemini_session_file(tmp_path: Path) -> Path:
    """Create a realistic Gemini CLI session JSON file."""
    project_hash = "abc123def456"
    chats_dir = tmp_path / "tmp" / project_hash / "chats"
    chats_dir.mkdir(parents=True)

    session_data = {
        "sessionId": "a61f1632-3cef-4677-aca5-8c2d8eed841f",
        "projectHash": project_hash,
        "startTime": "2026-03-20T10:00:00.000Z",
        "lastUpdated": "2026-03-20T10:05:00.000Z",
        "summary": "Debug the auth flow",
        "messages": [
            {
                "id": "msg-001",
                "timestamp": "2026-03-20T10:00:00.000Z",
                "type": "user",
                "content": [{"text": "Why is the token refresh returning 401?"}],
            },
            {
                "id": "msg-002",
                "timestamp": "2026-03-20T10:00:05.000Z",
                "type": "gemini",
                "content": "I'll check the token expiry logic in your auth module.\n\nThe issue is in `src/auth/token.py` line 42.",
            },
            {
                "id": "msg-003",
                "timestamp": "2026-03-20T10:01:00.000Z",
                "type": "user",
                "content": [{"text": "Can you fix it?"}],
            },
            {
                "id": "msg-004",
                "timestamp": "2026-03-20T10:01:10.000Z",
                "type": "gemini",
                "content": "Done! I've fixed the off-by-one error in the token expiry check.",
            },
            {
                "id": "msg-005",
                "timestamp": "2026-03-20T10:01:15.000Z",
                "type": "info",
                "content": "Request cancelled.",
            },
            {
                "id": "msg-006",
                "timestamp": "2026-03-20T10:02:00.000Z",
                "type": "error",
                "content": "[API Error: rate limit exceeded]",
            },
        ],
    }

    path = chats_dir / "session-2026-03-20T10-00-a61f1632.json"
    path.write_text(json.dumps(session_data))

    # Also write projects.json for discovery
    (tmp_path / "projects.json").write_text(json.dumps({"projects": {}}))

    return path


@pytest.fixture
def sfs_session(tmp_path: Path) -> Path:
    """Create a minimal .sfs session for conversion to Gemini."""
    d = tmp_path / "ses_gemtest1234abcd.sfs"
    d.mkdir()

    (d / "manifest.json").write_text(json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_gemtest1234abcd",
        "title": "Debug auth flow",
        "created_at": "2026-03-20T10:00:00Z",
        "updated_at": "2026-03-20T10:05:00Z",
        "source": {"tool": "claude-code"},
        "model": {"provider": "anthropic", "model_id": "claude-opus-4-6"},
        "stats": {"message_count": 4, "turn_count": 2},
    }))

    messages = [
        {"msg_id": "m1", "role": "user",
         "content": [{"type": "text", "text": "Explain the middleware"}],
         "timestamp": "2026-03-20T10:00:00Z"},
        {"msg_id": "m2", "role": "assistant",
         "content": [
             {"type": "thinking", "text": "Analyzing the code..."},
             {"type": "text", "text": "The middleware validates JWT tokens."},
         ],
         "timestamp": "2026-03-20T10:00:05Z"},
        {"msg_id": "m3", "role": "assistant",
         "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "cat auth.py"}}],
         "timestamp": "2026-03-20T10:00:10Z"},
        {"msg_id": "m4", "role": "user",
         "content": [{"type": "text", "text": "Thanks!"}],
         "timestamp": "2026-03-20T10:01:00Z"},
    ]
    with open(d / "messages.jsonl", "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")

    (d / "workspace.json").write_text(json.dumps({"root_path": "/Users/test/project"}))
    return d


# ---------------------------------------------------------------------------
# Gemini -> .sfs tests
# ---------------------------------------------------------------------------


class TestParseGeminiSession:
    def test_basic_parse(self, gemini_session_file: Path):
        session = parse_gemini_session(gemini_session_file)
        assert session.session_id == "a61f1632-3cef-4677-aca5-8c2d8eed841f"
        assert session.summary == "Debug the auth flow"
        assert session.start_time == "2026-03-20T10:00:00.000Z"

    def test_message_count(self, gemini_session_file: Path):
        session = parse_gemini_session(gemini_session_file)
        assert session.message_count == 6  # 2 user + 2 gemini + 1 info + 1 error

    def test_turn_count(self, gemini_session_file: Path):
        session = parse_gemini_session(gemini_session_file)
        assert session.turn_count == 2

    def test_role_mapping(self, gemini_session_file: Path):
        session = parse_gemini_session(gemini_session_file)
        roles = [m["role"] for m in session.messages]
        assert "user" in roles
        assert "assistant" in roles
        assert "system" in roles  # error and info become system

    def test_user_content_parts(self, gemini_session_file: Path):
        session = parse_gemini_session(gemini_session_file)
        user_msgs = [m for m in session.messages if m["role"] == "user"]
        assert user_msgs[0]["content"][0]["type"] == "text"
        assert "401" in user_msgs[0]["content"][0]["text"]

    def test_gemini_content_string(self, gemini_session_file: Path):
        session = parse_gemini_session(gemini_session_file)
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        assert len(asst_msgs) == 2
        assert "token expiry" in asst_msgs[0]["content"][0]["text"]

    def test_error_mapped_to_system(self, gemini_session_file: Path):
        session = parse_gemini_session(gemini_session_file)
        system_msgs = [m for m in session.messages if m["role"] == "system"]
        assert any("[Error]" in m["content"][0]["text"] for m in system_msgs)

    def test_info_mapped_to_system(self, gemini_session_file: Path):
        session = parse_gemini_session(gemini_session_file)
        system_msgs = [m for m in session.messages if m["role"] == "system"]
        assert any("[Info]" in m["content"][0]["text"] for m in system_msgs)


class TestConvertGeminiToSfs:
    def test_produces_valid_sfs(self, gemini_session_file: Path, tmp_path: Path):
        session = parse_gemini_session(gemini_session_file)
        sfs_dir = tmp_path / "output.sfs"
        convert_gemini_to_sfs(session, sfs_dir)

        assert (sfs_dir / "manifest.json").exists()
        assert (sfs_dir / "messages.jsonl").exists()

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["source"]["tool"] == "gemini-cli"
        assert manifest["source"]["original_session_id"] == "a61f1632-3cef-4677-aca5-8c2d8eed841f"

    def test_summary_becomes_title(self, gemini_session_file: Path, tmp_path: Path):
        session = parse_gemini_session(gemini_session_file)
        sfs_dir = tmp_path / "output.sfs"
        convert_gemini_to_sfs(session, sfs_dir)

        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["title"] == "Debug the auth flow"


# ---------------------------------------------------------------------------
# .sfs -> Gemini tests
# ---------------------------------------------------------------------------


class TestConvertSfsToGemini:
    def test_produces_valid_json(self, sfs_session: Path, tmp_path: Path):
        output = tmp_path / "output.json"
        result = convert_sfs_to_gemini(sfs_session, output_path=output)

        assert Path(result["json_path"]).exists()
        data = json.loads(output.read_text())
        assert "sessionId" in data
        assert "messages" in data
        assert "startTime" in data

    def test_user_messages_preserved(self, sfs_session: Path, tmp_path: Path):
        output = tmp_path / "output.json"
        convert_sfs_to_gemini(sfs_session, output_path=output)

        data = json.loads(output.read_text())
        user_msgs = [m for m in data["messages"] if m["type"] == "user"]
        assert len(user_msgs) == 2
        assert user_msgs[0]["content"][0]["text"] == "Explain the middleware"

    def test_assistant_becomes_gemini(self, sfs_session: Path, tmp_path: Path):
        output = tmp_path / "output.json"
        convert_sfs_to_gemini(sfs_session, output_path=output)

        data = json.loads(output.read_text())
        gemini_msgs = [m for m in data["messages"] if m["type"] == "gemini"]
        assert len(gemini_msgs) >= 1
        # Thinking blocks are dropped, text blocks preserved
        assert "JWT tokens" in gemini_msgs[0]["content"]

    def test_tool_use_inlined(self, sfs_session: Path, tmp_path: Path):
        """Tool use blocks become text descriptions in Gemini format."""
        output = tmp_path / "output.json"
        convert_sfs_to_gemini(sfs_session, output_path=output)

        data = json.loads(output.read_text())
        gemini_msgs = [m for m in data["messages"] if m["type"] == "gemini"]
        all_text = " ".join(m["content"] for m in gemini_msgs)
        assert "cat auth.py" in all_text

    def test_summary_from_title(self, sfs_session: Path, tmp_path: Path):
        output = tmp_path / "output.json"
        convert_sfs_to_gemini(sfs_session, output_path=output)

        data = json.loads(output.read_text())
        assert data.get("summary") == "Debug auth flow"

    def test_sidechain_skipped(self, tmp_path: Path):
        d = tmp_path / "ses_side1234abcdefgh.sfs"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({
            "sfs_version": "0.1.0", "session_id": "ses_side1234abcdefgh",
            "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-20T10:00:00Z",
            "source": {"tool": "claude-code"},
        }))
        with open(d / "messages.jsonl", "w") as f:
            f.write(json.dumps({"role": "user", "content": [{"type": "text", "text": "hi"}],
                                "timestamp": "2026-03-20T10:00:00Z"}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": [{"type": "text", "text": "sub"}],
                                "timestamp": "2026-03-20T10:00:01Z", "is_sidechain": True}) + "\n")

        output = tmp_path / "output.json"
        result = convert_sfs_to_gemini(d, output_path=output)
        assert result["message_count"] == 1  # Only user msg


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_sessions(self, gemini_session_file: Path, tmp_path: Path):
        sessions = discover_gemini_sessions(tmp_path)
        assert len(sessions) >= 1
        assert sessions[0]["path"] == str(gemini_session_file)


# ---------------------------------------------------------------------------
# Cross-tool round trip: Gemini -> .sfs -> Gemini
# ---------------------------------------------------------------------------


class TestGeminiRoundTrip:
    def test_gemini_to_sfs_to_gemini(self, gemini_session_file: Path, tmp_path: Path):
        # Step 1: Parse Gemini
        session = parse_gemini_session(gemini_session_file)
        assert session.message_count >= 4

        # Step 2: Convert to .sfs
        sfs_dir = tmp_path / "intermediate.sfs"
        convert_gemini_to_sfs(session, sfs_dir)
        assert (sfs_dir / "messages.jsonl").exists()

        # Step 3: Convert back to Gemini
        output = tmp_path / "roundtrip.json"
        result = convert_sfs_to_gemini(sfs_dir, output_path=output)
        assert result["message_count"] >= 2

        data = json.loads(output.read_text())
        assert data["sessionId"]
        user_msgs = [m for m in data["messages"] if m["type"] == "user"]
        assert any("401" in m["content"][0]["text"] for m in user_msgs)

    def test_cc_sfs_to_gemini(self, sfs_session: Path, tmp_path: Path):
        """CC-originated .sfs -> Gemini: cross-tool."""
        output = tmp_path / "cross.json"
        result = convert_sfs_to_gemini(sfs_session, output_path=output)
        assert result["message_count"] >= 2

        data = json.loads(output.read_text())
        assert data["messages"][0]["type"] == "user"
        assert data["messages"][0]["content"][0]["text"] == "Explain the middleware"
