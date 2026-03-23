"""Tests for Copilot CLI session parser, converters, discovery, and watcher."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.converters.copilot_to_sfs import (
    CopilotParsedSession,
    parse_copilot_session,
    convert_copilot_to_sfs,
    discover_copilot_sessions,
)
from sessionfs.converters.sfs_to_copilot import convert_sfs_to_copilot
from sessionfs.converters.copilot_injector import inject_session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def copilot_session_dir(tmp_path: Path) -> Path:
    """Create a realistic Copilot CLI session directory."""
    session_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    session_dir = tmp_path / "session-state" / session_id
    session_dir.mkdir(parents=True)

    # workspace.yaml
    (session_dir / "workspace.yaml").write_text(
        "cwd: /tmp/test_project\n"
        "model: gpt-4o\n"
        "model_provider: github\n"
        "cli_version: 1.2.0\n"
    )

    # events.jsonl
    events = [
        {
            "type": "user.message",
            "data": {"content": "Create a hello world script"},
            "id": "evt-001",
            "timestamp": "2026-03-20T10:00:00.000Z",
            "parentId": None,
        },
        {
            "type": "assistant.message",
            "data": {
                "content": "I'll create a hello world script for you.",
                "model": "gpt-4o",
            },
            "id": "evt-002",
            "timestamp": "2026-03-20T10:00:01.000Z",
            "parentId": "evt-001",
        },
        {
            "type": "tool.execution_start",
            "data": {
                "tool": "Bash",
                "input": {"command": "echo 'print(\"hello world\")' > hello.py"},
            },
            "id": "evt-003",
            "timestamp": "2026-03-20T10:00:02.000Z",
            "parentId": "evt-001",
        },
        {
            "type": "tool.execution_result",
            "data": {"output": ""},
            "id": "evt-004",
            "timestamp": "2026-03-20T10:00:03.000Z",
            "parentId": "evt-003",
        },
        {
            "type": "assistant.message",
            "data": {
                "content": "Done! I created hello.py with a hello world script.",
                "model": "gpt-4o",
            },
            "id": "evt-005",
            "timestamp": "2026-03-20T10:00:04.000Z",
            "parentId": "evt-001",
        },
        {
            "type": "user.message",
            "data": {"content": "Run it"},
            "id": "evt-006",
            "timestamp": "2026-03-20T10:00:10.000Z",
            "parentId": None,
        },
        {
            "type": "tool.execution_start",
            "data": {
                "tool": "Bash",
                "input": {"command": "python hello.py"},
            },
            "id": "evt-007",
            "timestamp": "2026-03-20T10:00:11.000Z",
            "parentId": "evt-006",
        },
        {
            "type": "tool.execution_result",
            "data": {"output": "hello world"},
            "id": "evt-008",
            "timestamp": "2026-03-20T10:00:12.000Z",
            "parentId": "evt-007",
        },
        {
            "type": "assistant.message",
            "data": {
                "content": "The script ran successfully and printed 'hello world'.",
                "model": "gpt-4o",
            },
            "id": "evt-009",
            "timestamp": "2026-03-20T10:00:13.000Z",
            "parentId": "evt-006",
        },
    ]

    with open(session_dir / "events.jsonl", "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")

    return session_dir


@pytest.fixture
def sfs_session_dir(tmp_path: Path) -> Path:
    """Create a minimal .sfs session directory for reverse conversion tests."""
    sfs_dir = tmp_path / "sfs_session"
    sfs_dir.mkdir()

    manifest = {
        "sfs_version": "0.1.0",
        "session_id": "ses_abc123def456gh",
        "title": "Test session",
        "tags": [],
        "created_at": "2026-03-20T10:00:00.000Z",
        "updated_at": "2026-03-20T10:00:10.000Z",
        "source": {
            "tool": "claude-code",
            "sfs_converter_version": "0.1.0",
        },
        "model": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-6",
        },
        "stats": {
            "message_count": 4,
            "turn_count": 1,
            "tool_use_count": 1,
        },
    }
    (sfs_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    messages = [
        {
            "msg_id": "msg_0000",
            "role": "user",
            "content": [{"type": "text", "text": "Say hello"}],
            "timestamp": "2026-03-20T10:00:00.000Z",
        },
        {
            "msg_id": "msg_0001",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello! How can I help?"}],
            "timestamp": "2026-03-20T10:00:01.000Z",
            "model": "claude-sonnet-4-6",
        },
        {
            "msg_id": "msg_0002",
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "tool_use_id": "tool_001",
                "name": "Bash",
                "input": {"command": "echo hello"},
            }],
            "timestamp": "2026-03-20T10:00:02.000Z",
            "model": "claude-sonnet-4-6",
        },
        {
            "msg_id": "msg_0003",
            "role": "tool",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tool_001",
                "content": "hello",
            }],
            "timestamp": "2026-03-20T10:00:03.000Z",
        },
    ]

    with open(sfs_dir / "messages.jsonl", "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")

    workspace = {"root_path": "/tmp/test_project"}
    (sfs_dir / "workspace.json").write_text(json.dumps(workspace, indent=2))

    return sfs_dir


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestCopilotParser:
    def test_parse_session_meta(self, copilot_session_dir: Path):
        session = parse_copilot_session(copilot_session_dir)
        assert session.session_id == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert session.cwd == "/tmp/test_project"
        assert session.model == "gpt-4o"
        assert session.model_provider == "github"
        assert session.cli_version == "1.2.0"

    def test_parse_messages(self, copilot_session_dir: Path):
        session = parse_copilot_session(copilot_session_dir)
        assert session.message_count == 9  # 2 user + 3 assistant + 2 tool_start + 2 tool_result

    def test_parse_user_message(self, copilot_session_dir: Path):
        session = parse_copilot_session(copilot_session_dir)
        assert session.first_prompt == "Create a hello world script"
        user_msgs = [m for m in session.messages if m["role"] == "user"]
        assert len(user_msgs) == 2

    def test_parse_assistant_message(self, copilot_session_dir: Path):
        session = parse_copilot_session(copilot_session_dir)
        assistant_msgs = [m for m in session.messages if m["role"] == "assistant"]
        # 3 assistant.message + 2 tool.execution_start (role=assistant)
        assert len(assistant_msgs) == 5

    def test_parse_tool_use(self, copilot_session_dir: Path):
        session = parse_copilot_session(copilot_session_dir)
        assert session.tool_use_count == 2
        tool_msgs = [
            m for m in session.messages
            if any(b.get("type") == "tool_use" for b in m.get("content", []))
        ]
        assert len(tool_msgs) == 2
        assert tool_msgs[0]["content"][0]["name"] == "Bash"

    def test_parse_tool_result(self, copilot_session_dir: Path):
        session = parse_copilot_session(copilot_session_dir)
        tool_results = [m for m in session.messages if m["role"] == "tool"]
        assert len(tool_results) == 2

    def test_parse_turn_count(self, copilot_session_dir: Path):
        session = parse_copilot_session(copilot_session_dir)
        assert session.turn_count == 2

    def test_parse_missing_events(self, tmp_path: Path):
        session_dir = tmp_path / "empty-session"
        session_dir.mkdir()
        session = parse_copilot_session(session_dir)
        assert session.message_count == 0
        assert len(session.parse_errors) == 1
        assert "events.jsonl not found" in session.parse_errors[0]

    def test_parse_malformed_json(self, tmp_path: Path):
        session_dir = tmp_path / "bad-session"
        session_dir.mkdir()
        (session_dir / "events.jsonl").write_text("not json\n{invalid}\n")
        session = parse_copilot_session(session_dir)
        assert len(session.parse_errors) == 2


# ---------------------------------------------------------------------------
# Copilot -> .sfs converter tests
# ---------------------------------------------------------------------------


class TestCopilotToSfs:
    def test_convert_creates_files(self, copilot_session_dir: Path, tmp_path: Path):
        output = tmp_path / "output.sfs"
        convert_copilot_to_sfs(copilot_session_dir, output)

        assert (output / "manifest.json").exists()
        assert (output / "messages.jsonl").exists()
        assert (output / "workspace.json").exists()

    def test_manifest_source(self, copilot_session_dir: Path, tmp_path: Path):
        output = tmp_path / "output.sfs"
        convert_copilot_to_sfs(copilot_session_dir, output)

        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["source"]["tool"] == "copilot-cli"
        assert manifest["source"]["original_session_id"] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert manifest["source"]["interface"] == "cli"

    def test_manifest_model(self, copilot_session_dir: Path, tmp_path: Path):
        output = tmp_path / "output.sfs"
        convert_copilot_to_sfs(copilot_session_dir, output)

        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["model"]["provider"] == "github"
        assert manifest["model"]["model_id"] == "gpt-4o"

    def test_manifest_stats(self, copilot_session_dir: Path, tmp_path: Path):
        output = tmp_path / "output.sfs"
        convert_copilot_to_sfs(copilot_session_dir, output)

        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["stats"]["tool_use_count"] == 2
        assert manifest["stats"]["turn_count"] == 2
        assert manifest["stats"]["message_count"] == 9

    def test_workspace_json(self, copilot_session_dir: Path, tmp_path: Path):
        output = tmp_path / "output.sfs"
        convert_copilot_to_sfs(copilot_session_dir, output)

        workspace = json.loads((output / "workspace.json").read_text())
        assert workspace["root_path"] == "/tmp/test_project"

    def test_messages_jsonl(self, copilot_session_dir: Path, tmp_path: Path):
        output = tmp_path / "output.sfs"
        convert_copilot_to_sfs(copilot_session_dir, output)

        messages = []
        with open(output / "messages.jsonl") as f:
            for line in f:
                if line.strip():
                    messages.append(json.loads(line))
        assert len(messages) == 9

    def test_custom_session_id(self, copilot_session_dir: Path, tmp_path: Path):
        output = tmp_path / "output.sfs"
        convert_copilot_to_sfs(copilot_session_dir, output, session_id="ses_custom12345678")

        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["session_id"] == "ses_custom12345678"

    def test_duration_calculated(self, copilot_session_dir: Path, tmp_path: Path):
        output = tmp_path / "output.sfs"
        convert_copilot_to_sfs(copilot_session_dir, output)

        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["stats"]["duration_ms"] is not None
        assert manifest["stats"]["duration_ms"] > 0


# ---------------------------------------------------------------------------
# .sfs -> Copilot converter tests
# ---------------------------------------------------------------------------


class TestSfsToCopilot:
    def test_convert_creates_events(self, sfs_session_dir: Path, tmp_path: Path):
        output_path = tmp_path / "copilot_output" / "events.jsonl"
        result = convert_sfs_to_copilot(sfs_session_dir, output_path=output_path)

        assert Path(result["events_path"]).exists()
        assert result["message_count"] >= 1

    def test_convert_creates_workspace_yaml(self, sfs_session_dir: Path, tmp_path: Path):
        output_path = tmp_path / "copilot_output" / "events.jsonl"
        result = convert_sfs_to_copilot(sfs_session_dir, output_path=output_path)

        workspace_path = Path(result["workspace_yaml_path"])
        assert workspace_path.exists()
        text = workspace_path.read_text()
        assert "cwd: /tmp/test_project" in text

    def test_user_message_mapping(self, sfs_session_dir: Path, tmp_path: Path):
        output_path = tmp_path / "copilot_output" / "events.jsonl"
        convert_sfs_to_copilot(sfs_session_dir, output_path=output_path)

        events = _read_events(output_path)
        user_events = [e for e in events if e["type"] == "user.message"]
        assert len(user_events) >= 1
        assert user_events[0]["data"]["content"] == "Say hello"

    def test_assistant_message_mapping(self, sfs_session_dir: Path, tmp_path: Path):
        output_path = tmp_path / "copilot_output" / "events.jsonl"
        convert_sfs_to_copilot(sfs_session_dir, output_path=output_path)

        events = _read_events(output_path)
        assistant_events = [e for e in events if e["type"] == "assistant.message"]
        assert len(assistant_events) >= 1
        assert "Hello" in assistant_events[0]["data"]["content"]

    def test_tool_execution_mapping(self, sfs_session_dir: Path, tmp_path: Path):
        output_path = tmp_path / "copilot_output" / "events.jsonl"
        convert_sfs_to_copilot(sfs_session_dir, output_path=output_path)

        events = _read_events(output_path)
        tool_events = [e for e in events if e["type"] == "tool.execution_start"]
        assert len(tool_events) >= 1
        assert tool_events[0]["data"]["tool"] == "Bash"

    def test_tool_result_mapping(self, sfs_session_dir: Path, tmp_path: Path):
        output_path = tmp_path / "copilot_output" / "events.jsonl"
        convert_sfs_to_copilot(sfs_session_dir, output_path=output_path)

        events = _read_events(output_path)
        result_events = [e for e in events if e["type"] == "tool.execution_result"]
        assert len(result_events) >= 1
        assert result_events[0]["data"]["output"] == "hello"

    def test_event_ids_are_uuids(self, sfs_session_dir: Path, tmp_path: Path):
        import uuid
        output_path = tmp_path / "copilot_output" / "events.jsonl"
        convert_sfs_to_copilot(sfs_session_dir, output_path=output_path)

        events = _read_events(output_path)
        for event in events:
            # Should be valid UUID
            uuid.UUID(event["id"])

    def test_cwd_override(self, sfs_session_dir: Path, tmp_path: Path):
        output_path = tmp_path / "copilot_output" / "events.jsonl"
        result = convert_sfs_to_copilot(
            sfs_session_dir, output_path=output_path, cwd="/override/path",
        )

        workspace_path = Path(result["workspace_yaml_path"])
        text = workspace_path.read_text()
        assert "cwd: /override/path" in text

    def test_default_output_path(self, sfs_session_dir: Path):
        result = convert_sfs_to_copilot(sfs_session_dir)
        assert Path(result["events_path"]).exists()
        assert result["copilot_session_id"]


# ---------------------------------------------------------------------------
# Injector tests
# ---------------------------------------------------------------------------


class TestCopilotInjector:
    def test_inject_creates_session_dir(self, tmp_path: Path):
        # Create a source events.jsonl
        source = tmp_path / "source" / "events.jsonl"
        source.parent.mkdir()
        source.write_text('{"type":"user.message","data":{"content":"hi"},"id":"1","timestamp":"2026-03-20T10:00:00Z","parentId":null}\n')

        copilot_home = tmp_path / "copilot_home"

        result = inject_session(
            events_jsonl=source,
            copilot_session_id="test-session-id",
            cwd="/tmp/project",
            title="Test Session",
            copilot_home=copilot_home,
        )

        session_path = Path(result["session_path"])
        assert session_path.exists()
        assert (session_path / "events.jsonl").exists()
        assert (session_path / "workspace.yaml").exists()

    def test_inject_copies_events(self, tmp_path: Path):
        source = tmp_path / "source" / "events.jsonl"
        source.parent.mkdir()
        event_line = '{"type":"user.message","data":{"content":"hello"},"id":"1","timestamp":"2026-03-20T10:00:00Z","parentId":null}\n'
        source.write_text(event_line)

        copilot_home = tmp_path / "copilot_home"

        result = inject_session(
            events_jsonl=source,
            copilot_session_id="inject-test",
            cwd="/tmp/project",
            copilot_home=copilot_home,
        )

        events_path = Path(result["events_path"])
        assert events_path.read_text() == event_line

    def test_inject_workspace_yaml(self, tmp_path: Path):
        source = tmp_path / "source" / "events.jsonl"
        source.parent.mkdir()
        source.write_text("{}\n")

        copilot_home = tmp_path / "copilot_home"

        result = inject_session(
            events_jsonl=source,
            copilot_session_id="ws-test",
            cwd="/my/project",
            title="My Session",
            copilot_home=copilot_home,
        )

        workspace = Path(result["session_path"]) / "workspace.yaml"
        text = workspace.read_text()
        assert "cwd: /my/project" in text
        assert "title: My Session" in text


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discover_sessions(self, copilot_session_dir: Path, tmp_path: Path):
        # copilot_session_dir is at tmp_path/session-state/{id}/
        sessions = discover_copilot_sessions(tmp_path)
        assert len(sessions) >= 1
        assert sessions[0]["session_id"] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_discover_reads_cwd(self, copilot_session_dir: Path, tmp_path: Path):
        sessions = discover_copilot_sessions(tmp_path)
        assert sessions[0]["cwd"] == "/tmp/test_project"

    def test_discover_empty_dir(self, tmp_path: Path):
        sessions = discover_copilot_sessions(tmp_path)
        assert sessions == []

    def test_discover_skips_non_dirs(self, tmp_path: Path):
        state_dir = tmp_path / "session-state"
        state_dir.mkdir()
        (state_dir / "not-a-session.txt").write_text("garbage")
        sessions = discover_copilot_sessions(tmp_path)
        assert sessions == []

    def test_discover_skips_missing_events(self, tmp_path: Path):
        state_dir = tmp_path / "session-state"
        session_dir = state_dir / "some-session"
        session_dir.mkdir(parents=True)
        # No events.jsonl
        sessions = discover_copilot_sessions(tmp_path)
        assert sessions == []


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_copilot_to_sfs_to_copilot(self, copilot_session_dir: Path, tmp_path: Path):
        """Verify that Copilot -> .sfs -> Copilot preserves messages."""
        # Step 1: Copilot -> .sfs
        sfs_dir = tmp_path / "roundtrip.sfs"
        convert_copilot_to_sfs(copilot_session_dir, sfs_dir)

        # Step 2: .sfs -> Copilot
        copilot_output = tmp_path / "copilot_roundtrip" / "events.jsonl"
        result = convert_sfs_to_copilot(sfs_dir, output_path=copilot_output)

        events = _read_events(copilot_output)

        # Verify key events are preserved
        user_events = [e for e in events if e["type"] == "user.message"]
        assistant_events = [e for e in events if e["type"] == "assistant.message"]
        tool_events = [e for e in events if e["type"] == "tool.execution_start"]

        assert len(user_events) >= 2
        assert len(assistant_events) >= 3
        assert len(tool_events) >= 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_events(path: Path) -> list[dict]:
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
