"""Tests for Codex session parser and watcher."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.watchers.codex import (
    CodexParsedSession,
    parse_codex_session,
    convert_codex_to_sfs,
    discover_codex_sessions,
    _extract_session_id_from_path,
)


@pytest.fixture
def codex_session_file(tmp_path: Path) -> Path:
    """Create a realistic Codex JSONL rollout file."""
    sessions_dir = tmp_path / "sessions" / "2026" / "03" / "20"
    sessions_dir.mkdir(parents=True)
    path = sessions_dir / "rollout-2026-03-20T09-12-00-019d0a84-0c2f-7163-8491-0dd9ff93f4b8.jsonl"

    lines = [
        {
            "timestamp": "2026-03-20T09:12:00.019Z",
            "type": "session_meta",
            "payload": {
                "id": "019d0a84-0c2f-7163-8491-0dd9ff93f4b8",
                "timestamp": "2026-03-20T09:11:59.302Z",
                "cwd": "/tmp/test_repo",
                "originator": "codex_cli",
                "cli_version": "0.116.0",
                "source": "cli",
                "model_provider": "openai",
                "base_instructions": None,
                "git": {"commit_hash": "abc123", "branch": "main", "repository_url": None},
                "forked_from_id": None,
            },
        },
        {
            "timestamp": "2026-03-20T09:12:01Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-001",
                "cwd": "/tmp/test_repo",
                "model": "gpt-4.1",
            },
        },
        {
            "timestamp": "2026-03-20T09:12:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-001"},
        },
        {
            "timestamp": "2026-03-20T09:12:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Say hello world", "images": []},
        },
        {
            "timestamp": "2026-03-20T09:12:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_001",
                "role": "user",
                "content": [{"type": "input_text", "text": "Say hello world"}],
            },
        },
        {
            "timestamp": "2026-03-20T09:12:04Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "id": "rs_001",
                "summary": [{"type": "summary_text", "text": "Thinking about greeting"}],
                "content": [{"type": "text", "text": "I should print hello world"}],
            },
        },
        {
            "timestamp": "2026-03-20T09:12:05Z",
            "type": "response_item",
            "payload": {
                "type": "local_shell_call",
                "id": "fc_001",
                "call_id": "call_001",
                "status": "completed",
                "action": {"type": "exec", "command": ["bash", "-c", "echo hello world"]},
            },
        },
        {
            "timestamp": "2026-03-20T09:12:06Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_001",
                "output": {"text": "hello world", "metadata": None},
            },
        },
        {
            "timestamp": "2026-03-20T09:12:07Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_002",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done! I printed hello world."}],
                "end_turn": True,
                "phase": "final_answer",
            },
        },
        {
            "timestamp": "2026-03-20T09:12:08Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 500, "output_tokens": 100},
                    "last_token_usage": {"input_tokens": 500, "output_tokens": 100},
                },
            },
        },
        {
            "timestamp": "2026-03-20T09:12:09Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-001"},
        },
    ]

    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")

    return path


def test_parse_session_meta(codex_session_file: Path):
    session = parse_codex_session(codex_session_file)
    assert session.session_id == "019d0a84-0c2f-7163-8491-0dd9ff93f4b8"
    assert session.cwd == "/tmp/test_repo"
    assert session.cli_version == "0.116.0"
    assert session.model_provider == "openai"
    assert session.git_branch == "main"


def test_parse_messages(codex_session_file: Path):
    session = parse_codex_session(codex_session_file)
    assert session.message_count >= 4  # user_event + user_msg + reasoning + shell + output + assistant


def test_parse_user_message(codex_session_file: Path):
    session = parse_codex_session(codex_session_file)
    assert session.first_prompt == "Say hello world"
    user_msgs = [m for m in session.messages if m["role"] == "user"]
    assert len(user_msgs) >= 1


def test_parse_tool_use(codex_session_file: Path):
    session = parse_codex_session(codex_session_file)
    assert session.tool_use_count >= 1
    tool_msgs = [
        m for m in session.messages
        if any(b.get("type") == "tool_use" for b in m.get("content", []))
    ]
    assert len(tool_msgs) >= 1
    assert tool_msgs[0]["content"][0]["name"] == "Bash"


def test_parse_token_count(codex_session_file: Path):
    session = parse_codex_session(codex_session_file)
    assert session.total_input_tokens == 500
    assert session.total_output_tokens == 100


def test_parse_turn_count(codex_session_file: Path):
    session = parse_codex_session(codex_session_file)
    assert session.turn_count == 1


def test_convert_to_sfs(codex_session_file: Path, tmp_path: Path):
    session = parse_codex_session(codex_session_file)
    sfs_dir = tmp_path / "output.sfs"
    convert_codex_to_sfs(session, sfs_dir)

    assert (sfs_dir / "manifest.json").exists()
    assert (sfs_dir / "messages.jsonl").exists()
    assert (sfs_dir / "workspace.json").exists()

    manifest = json.loads((sfs_dir / "manifest.json").read_text())
    assert manifest["source"]["tool"] == "codex"
    assert manifest["source"]["original_session_id"] == "019d0a84-0c2f-7163-8491-0dd9ff93f4b8"
    assert manifest["stats"]["tool_use_count"] >= 1


def test_extract_session_id_from_path():
    p = Path("rollout-2026-03-20T09-12-00-019d0a84-0c2f-7163-8491-0dd9ff93f4b8.jsonl")
    sid = _extract_session_id_from_path(p)
    assert sid == "019d0a84-0c2f-7163-8491-0dd9ff93f4b8"


def test_discover_via_filesystem(codex_session_file: Path, tmp_path: Path):
    # tmp_path contains sessions/YYYY/MM/DD/rollout-*.jsonl
    sessions = discover_codex_sessions(tmp_path)
    assert len(sessions) >= 1
    assert sessions[0]["session_id"] == "019d0a84-0c2f-7163-8491-0dd9ff93f4b8"
