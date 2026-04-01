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
def gemini_session_with_tools(tmp_path: Path) -> Path:
    """Create a Gemini CLI session with toolCalls (agentic mode)."""
    project_hash = "toolhash123"
    chats_dir = tmp_path / "tmp" / project_hash / "chats"
    chats_dir.mkdir(parents=True)

    session_data = {
        "sessionId": "tool-sess-001",
        "projectHash": project_hash,
        "startTime": "2026-03-27T14:00:00.000Z",
        "lastUpdated": "2026-03-27T14:05:00.000Z",
        "messages": [
            {
                "id": "msg-u1",
                "timestamp": "2026-03-27T14:00:00.000Z",
                "type": "user",
                "content": [{"text": "List the files in src/"}],
            },
            {
                "id": "msg-a1",
                "timestamp": "2026-03-27T14:00:05.000Z",
                "type": "gemini",
                "content": "I will list the directory contents.",
                "thoughts": [
                    {
                        "subject": "Planning",
                        "description": "Listing directory.",
                        "timestamp": "2026-03-27T14:00:04.000Z",
                    }
                ],
                "tokens": {
                    "input": 100, "output": 20, "cached": 0,
                    "thoughts": 10, "tool": 0, "total": 130,
                },
                "model": "gemini-3-flash-preview",
                "toolCalls": [
                    {
                        "id": "list_directory_001",
                        "name": "list_directory",
                        "args": {"dir_path": "src/"},
                        "result": [
                            {
                                "functionResponse": {
                                    "id": "list_directory_001",
                                    "name": "list_directory",
                                    "response": {
                                        "output": "main.py\nutils.py\nconfig.py"
                                    },
                                }
                            }
                        ],
                        "status": "success",
                        "timestamp": "2026-03-27T14:00:05.100Z",
                        "resultDisplay": "Listed 3 item(s).",
                        "displayName": "ReadFolder",
                        "description": "Lists directory contents.",
                    }
                ],
            },
            {
                "id": "msg-a2",
                "timestamp": "2026-03-27T14:00:10.000Z",
                "type": "gemini",
                "content": "I will read and search in the main file.",
                "toolCalls": [
                    {
                        "id": "read_file_002",
                        "name": "read_file",
                        "args": {"file_path": "src/main.py"},
                        "result": [
                            {
                                "functionResponse": {
                                    "id": "read_file_002",
                                    "name": "read_file",
                                    "response": {
                                        "output": "import sys\ndef main(): pass"
                                    },
                                }
                            }
                        ],
                        "status": "success",
                        "timestamp": "2026-03-27T14:00:10.100Z",
                        "displayName": "ReadFile",
                    },
                    {
                        "id": "grep_search_003",
                        "name": "grep_search",
                        "args": {"pattern": "def main", "include": "*.py"},
                        "result": [
                            {
                                "functionResponse": {
                                    "id": "grep_search_003",
                                    "name": "grep_search",
                                    "response": {
                                        "output": "Found 1 match"
                                    },
                                }
                            }
                        ],
                        "status": "success",
                        "timestamp": "2026-03-27T14:00:10.200Z",
                        "displayName": "SearchText",
                    },
                ],
            },
            {
                "id": "msg-a3",
                "timestamp": "2026-03-27T14:00:15.000Z",
                "type": "gemini",
                "content": "The src/ directory has three Python files.",
            },
            {
                "id": "msg-u2",
                "timestamp": "2026-03-27T14:01:00.000Z",
                "type": "user",
                "content": [{"text": "Thanks!"}],
            },
        ],
    }

    path = chats_dir / "session-2026-03-27T14-00-tool0001.json"
    path.write_text(json.dumps(session_data))
    (tmp_path / "projects.json").write_text(json.dumps({"projects": {}}))
    return path


@pytest.fixture
def gemini_session_with_error_tool(tmp_path: Path) -> Path:
    """Create a Gemini CLI session with a failed tool call."""
    project_hash = "errhash456"
    chats_dir = tmp_path / "tmp" / project_hash / "chats"
    chats_dir.mkdir(parents=True)

    session_data = {
        "sessionId": "err-sess-001",
        "projectHash": project_hash,
        "startTime": "2026-03-27T15:00:00.000Z",
        "lastUpdated": "2026-03-27T15:00:10.000Z",
        "messages": [
            {
                "id": "msg-u1",
                "timestamp": "2026-03-27T15:00:00.000Z",
                "type": "user",
                "content": [{"text": "Read /nonexistent"}],
            },
            {
                "id": "msg-a1",
                "timestamp": "2026-03-27T15:00:05.000Z",
                "type": "gemini",
                "content": "I'll try to read that file.",
                "toolCalls": [
                    {
                        "id": "read_file_err",
                        "name": "read_file",
                        "args": {"file_path": "/nonexistent"},
                        "result": [],
                        "status": "error",
                        "timestamp": "2026-03-27T15:00:05.100Z",
                        "displayName": "ReadFile",
                    }
                ],
            },
        ],
    }

    path = chats_dir / "session-2026-03-27T15-00-errsess1.json"
    path.write_text(json.dumps(session_data))
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


class TestToolCallExtraction:
    """Tests for extracting Gemini CLI toolCalls into SFS tool_use/tool_result."""

    def test_tool_use_count(self, gemini_session_with_tools: Path):
        session = parse_gemini_session(gemini_session_with_tools)
        assert session.tool_use_count == 3  # 1 + 2 tool calls

    def test_tool_use_blocks_in_assistant_message(self, gemini_session_with_tools: Path):
        session = parse_gemini_session(gemini_session_with_tools)
        # First assistant message should have text + tool_use
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        first_asst = asst_msgs[0]
        types = [b["type"] for b in first_asst["content"]]
        assert "text" in types
        assert "tool_use" in types

    def test_tool_use_has_correct_fields(self, gemini_session_with_tools: Path):
        session = parse_gemini_session(gemini_session_with_tools)
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        tool_blocks = [
            b for m in asst_msgs for b in m["content"]
            if b["type"] == "tool_use"
        ]
        assert len(tool_blocks) == 3
        first_tool = tool_blocks[0]
        assert first_tool["id"] == "list_directory_001"
        assert first_tool["name"] == "ReadFolder"  # Uses displayName
        assert first_tool["input"] == {"dir_path": "src/"}

    def test_tool_result_messages(self, gemini_session_with_tools: Path):
        session = parse_gemini_session(gemini_session_with_tools)
        tool_msgs = [m for m in session.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 3
        # First result has directory listing
        first_result = tool_msgs[0]["content"][0]
        assert first_result["type"] == "tool_result"
        assert first_result["tool_use_id"] == "list_directory_001"
        assert "main.py" in first_result["content"]
        assert first_result["is_error"] is False

    def test_multiple_tool_calls_per_message(self, gemini_session_with_tools: Path):
        session = parse_gemini_session(gemini_session_with_tools)
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        # Second assistant message has 2 tool calls
        second_asst = asst_msgs[1]
        tool_blocks = [b for b in second_asst["content"] if b["type"] == "tool_use"]
        assert len(tool_blocks) == 2
        assert tool_blocks[0]["name"] == "ReadFile"
        assert tool_blocks[1]["name"] == "SearchText"

    def test_plain_assistant_message_still_works(self, gemini_session_with_tools: Path):
        session = parse_gemini_session(gemini_session_with_tools)
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        # Third assistant message has no tool calls
        third_asst = asst_msgs[2]
        assert len(third_asst["content"]) == 1
        assert third_asst["content"][0]["type"] == "text"

    def test_error_tool_call(self, gemini_session_with_error_tool: Path):
        session = parse_gemini_session(gemini_session_with_error_tool)
        assert session.tool_use_count == 1
        tool_msgs = [m for m in session.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"][0]["is_error"] is True

    def test_manifest_includes_tool_count(
        self, gemini_session_with_tools: Path, tmp_path: Path,
    ):
        session = parse_gemini_session(gemini_session_with_tools)
        sfs_dir = tmp_path / "output.sfs"
        convert_gemini_to_sfs(session, sfs_dir)
        manifest = json.loads((sfs_dir / "manifest.json").read_text())
        assert manifest["stats"]["tool_use_count"] == 3

    def test_displayname_preferred_over_name(self, gemini_session_with_tools: Path):
        """displayName (user-facing) is preferred over internal name."""
        session = parse_gemini_session(gemini_session_with_tools)
        asst_msgs = [m for m in session.messages if m["role"] == "assistant"]
        tool_blocks = [
            b for m in asst_msgs for b in m["content"]
            if b["type"] == "tool_use"
        ]
        # list_directory -> ReadFolder, read_file -> ReadFile, grep_search -> SearchText
        names = [b["name"] for b in tool_blocks]
        assert names == ["ReadFolder", "ReadFile", "SearchText"]

    def test_fallback_to_name_when_no_displayname(self, tmp_path: Path):
        """Falls back to internal name when displayName is absent."""
        project_hash = "fallback123"
        chats_dir = tmp_path / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)
        session_data = {
            "sessionId": "fb-sess-001",
            "startTime": "2026-03-27T16:00:00.000Z",
            "lastUpdated": "2026-03-27T16:00:10.000Z",
            "messages": [
                {
                    "id": "msg-u1", "timestamp": "2026-03-27T16:00:00.000Z",
                    "type": "user", "content": [{"text": "hi"}],
                },
                {
                    "id": "msg-a1", "timestamp": "2026-03-27T16:00:05.000Z",
                    "type": "gemini", "content": "Checking.",
                    "toolCalls": [{
                        "id": "tc1", "name": "shell",
                        "args": {"command": "ls"},
                        "result": [{"functionResponse": {"id": "tc1", "name": "shell",
                                    "response": {"output": "file.txt"}}}],
                        "status": "success",
                        "timestamp": "2026-03-27T16:00:05.100Z",
                    }],
                },
            ],
        }
        path = chats_dir / "session-2026-03-27T16-00-fbsess01.json"
        path.write_text(json.dumps(session_data))
        session = parse_gemini_session(path)
        tool_blocks = [
            b for m in session.messages if m["role"] == "assistant"
            for b in m["content"] if b["type"] == "tool_use"
        ]
        assert tool_blocks[0]["name"] == "shell"


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
