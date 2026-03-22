"""Cross-tool round trip: CC -> .sfs -> Codex -> .sfs -> verify."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.converters.sfs_to_codex import convert_sfs_to_codex
from sessionfs.watchers.codex import parse_codex_session, convert_codex_to_sfs


@pytest.fixture
def sfs_from_cc(tmp_path: Path) -> Path:
    """Create an .sfs session that looks like it came from Claude Code."""
    d = tmp_path / "ses_roundtrip1234ab.sfs"
    d.mkdir()

    manifest = {
        "sfs_version": "0.1.0",
        "session_id": "ses_roundtrip1234ab",
        "title": "Debug auth flow",
        "created_at": "2026-03-20T14:00:00Z",
        "updated_at": "2026-03-20T14:05:00Z",
        "source": {"tool": "claude-code", "tool_version": "1.5.0"},
        "model": {"provider": "anthropic", "model_id": "claude-opus-4-6"},
        "stats": {"message_count": 6, "turn_count": 2},
    }
    (d / "manifest.json").write_text(json.dumps(manifest))

    messages = [
        {"msg_id": "m1", "role": "user",
         "content": [{"type": "text", "text": "Why is the token refresh returning 401?"}],
         "timestamp": "2026-03-20T14:00:00Z"},
        {"msg_id": "m2", "role": "assistant",
         "content": [
             {"type": "thinking", "text": "Let me check the token expiry logic"},
             {"type": "tool_use", "tool_use_id": "tu1", "name": "Bash",
              "input": {"command": "cat src/auth/token.py"}},
         ],
         "timestamp": "2026-03-20T14:00:02Z", "model": "claude-opus-4-6"},
        {"msg_id": "m3", "role": "tool",
         "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "def refresh(): ..."}],
         "timestamp": "2026-03-20T14:00:03Z"},
        {"msg_id": "m4", "role": "assistant",
         "content": [{"type": "text", "text": "The expiry check is off by one."}],
         "timestamp": "2026-03-20T14:00:05Z", "model": "claude-opus-4-6"},
        {"msg_id": "m5", "role": "user",
         "content": [{"type": "text", "text": "Can you fix it?"}],
         "timestamp": "2026-03-20T14:01:00Z"},
        {"msg_id": "m6", "role": "assistant",
         "content": [{"type": "text", "text": "Done, the expiry is now correct."}],
         "timestamp": "2026-03-20T14:01:05Z", "model": "claude-opus-4-6"},
    ]
    with open(d / "messages.jsonl", "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")

    (d / "workspace.json").write_text(json.dumps({"root_path": "/Users/test/api"}))
    return d


def test_cc_to_codex_roundtrip(sfs_from_cc: Path, tmp_path: Path):
    """CC .sfs -> Codex JSONL -> parse -> .sfs: content preserved."""
    # Step 1: Convert .sfs -> Codex
    codex_out = tmp_path / "codex.jsonl"
    result = convert_sfs_to_codex(sfs_from_cc, output_path=codex_out)
    assert result["turn_count"] == 2

    # Step 2: Parse the Codex JSONL back
    codex_session = parse_codex_session(codex_out)
    assert codex_session.message_count > 0
    assert codex_session.cwd == "/Users/test/api"

    # Verify key content survived the round trip
    user_msgs = [m for m in codex_session.messages if m["role"] == "user"]
    assert any("401" in str(m.get("content", "")) for m in user_msgs)

    tool_msgs = [
        m for m in codex_session.messages
        if any(b.get("type") == "tool_use" for b in m.get("content", []))
    ]
    assert len(tool_msgs) >= 1

    # Step 3: Convert back to .sfs
    sfs_out = tmp_path / "roundtrip.sfs"
    convert_codex_to_sfs(codex_session, sfs_out)

    assert (sfs_out / "manifest.json").exists()
    assert (sfs_out / "messages.jsonl").exists()

    rt_manifest = json.loads((sfs_out / "manifest.json").read_text())
    assert rt_manifest["source"]["tool"] == "codex"

    # Messages survived
    rt_messages = []
    with open(sfs_out / "messages.jsonl") as f:
        for line in f:
            if line.strip():
                rt_messages.append(json.loads(line))
    assert len(rt_messages) >= 3


def test_codex_to_cc_roundtrip(tmp_path: Path):
    """Codex JSONL -> .sfs -> Codex JSONL: structure preserved."""
    # Create a Codex session
    codex_path = tmp_path / "original.jsonl"
    lines = [
        {"timestamp": "2026-03-20T10:00:00Z", "type": "session_meta", "payload": {
            "id": "test-uuid-1234", "cwd": "/tmp/test",
            "cli_version": "0.116.0", "source": "cli", "model_provider": "openai",
            "timestamp": "2026-03-20T10:00:00Z",
        }},
        {"timestamp": "2026-03-20T10:00:01Z", "type": "event_msg", "payload": {
            "type": "task_started", "turn_id": "t1",
        }},
        {"timestamp": "2026-03-20T10:00:02Z", "type": "event_msg", "payload": {
            "type": "user_message", "message": "Fix the bug",
        }},
        {"timestamp": "2026-03-20T10:00:03Z", "type": "response_item", "payload": {
            "type": "message", "id": "msg_1", "role": "assistant",
            "content": [{"type": "output_text", "text": "I'll fix it."}],
            "end_turn": True, "phase": "final_answer",
        }},
        {"timestamp": "2026-03-20T10:00:04Z", "type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "t1",
        }},
    ]
    with open(codex_path, "w") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")

    # Parse to intermediate
    codex_session = parse_codex_session(codex_path)
    assert codex_session.first_prompt == "Fix the bug"

    # Convert to .sfs
    sfs_dir = tmp_path / "intermediate.sfs"
    convert_codex_to_sfs(codex_session, sfs_dir)

    # Convert .sfs back to Codex
    codex_out = tmp_path / "roundtrip.jsonl"
    result = convert_sfs_to_codex(sfs_dir, output_path=codex_out)
    assert result["message_count"] >= 1

    # Verify structure
    out_lines = [json.loads(l) for l in codex_out.read_text().strip().split("\n")]
    assert out_lines[0]["type"] == "session_meta"
    user_events = [l for l in out_lines if l["type"] == "event_msg" and l["payload"].get("type") == "user_message"]
    assert any("Fix the bug" in e["payload"].get("message", "") for e in user_events)
