"""Tests for .sfs -> Codex CLI converter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.converters.sfs_to_codex import convert_sfs_to_codex


@pytest.fixture
def sfs_session(tmp_path: Path) -> Path:
    """Create a minimal .sfs session with all content block types."""
    d = tmp_path / "ses_test1234abcdef.sfs"
    d.mkdir()

    manifest = {
        "sfs_version": "0.1.0",
        "session_id": "ses_test1234abcdef",
        "title": "Test session",
        "created_at": "2026-03-20T10:00:00Z",
        "updated_at": "2026-03-20T10:05:00Z",
        "source": {"tool": "claude-code", "tool_version": "1.0"},
        "model": {"provider": "anthropic", "model_id": "claude-opus-4-6"},
        "stats": {"message_count": 6, "turn_count": 2},
    }
    (d / "manifest.json").write_text(json.dumps(manifest))

    messages = [
        {
            "msg_id": "msg_001", "role": "user",
            "content": [{"type": "text", "text": "Explain the auth flow"}],
            "timestamp": "2026-03-20T10:00:00Z",
        },
        {
            "msg_id": "msg_002", "role": "assistant",
            "content": [{"type": "thinking", "text": "Let me analyze the code..."}],
            "timestamp": "2026-03-20T10:00:01Z",
            "model": "claude-opus-4-6",
        },
        {
            "msg_id": "msg_003", "role": "assistant",
            "content": [{"type": "text", "text": "The auth flow uses JWT tokens."}],
            "timestamp": "2026-03-20T10:00:02Z",
            "model": "claude-opus-4-6",
        },
        {
            "msg_id": "msg_004", "role": "assistant",
            "content": [{"type": "tool_use", "tool_use_id": "tu_001", "name": "Bash", "input": {"command": "cat auth.py"}}],
            "timestamp": "2026-03-20T10:00:03Z",
            "model": "claude-opus-4-6",
        },
        {
            "msg_id": "msg_005", "role": "tool",
            "content": [{"type": "tool_result", "tool_use_id": "tu_001", "content": "def login(): ..."}],
            "timestamp": "2026-03-20T10:00:04Z",
        },
        {
            "msg_id": "msg_006", "role": "user",
            "content": [{"type": "text", "text": "Now fix the token refresh"}],
            "timestamp": "2026-03-20T10:01:00Z",
        },
    ]
    with open(d / "messages.jsonl", "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")

    workspace = {"root_path": "/Users/test/project", "git": {"branch": "main"}}
    (d / "workspace.json").write_text(json.dumps(workspace))

    return d


def test_convert_produces_valid_jsonl(sfs_session: Path, tmp_path: Path):
    output = tmp_path / "output.jsonl"
    result = convert_sfs_to_codex(sfs_session, output_path=output)

    assert Path(result["jsonl_path"]).exists()
    assert result["message_count"] > 0
    assert result["turn_count"] == 2

    lines = output.read_text().strip().split("\n")
    for line in lines:
        entry = json.loads(line)
        assert "timestamp" in entry
        assert "type" in entry
        assert "payload" in entry
        assert entry["type"] in ("session_meta", "response_item", "event_msg", "turn_context")


def test_session_meta_is_first_line(sfs_session: Path, tmp_path: Path):
    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(sfs_session, output_path=output)

    first = json.loads(output.read_text().split("\n")[0])
    assert first["type"] == "session_meta"
    assert first["payload"]["source"] == "cli"
    assert first["payload"]["cwd"] == "/Users/test/project"


def test_user_messages_produce_events(sfs_session: Path, tmp_path: Path):
    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(sfs_session, output_path=output)

    lines = [json.loads(l) for l in output.read_text().strip().split("\n")]

    user_events = [l for l in lines if l["type"] == "event_msg" and l["payload"].get("type") == "user_message"]
    assert len(user_events) == 2
    assert user_events[0]["payload"]["message"] == "Explain the auth flow"


def test_thinking_becomes_reasoning(sfs_session: Path, tmp_path: Path):
    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(sfs_session, output_path=output)

    lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
    reasoning = [l for l in lines if l["type"] == "response_item" and l["payload"].get("type") == "reasoning"]
    assert len(reasoning) == 1
    assert "analyze" in reasoning[0]["payload"]["summary"][0]["text"]


def test_tool_use_becomes_shell_call(sfs_session: Path, tmp_path: Path):
    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(sfs_session, output_path=output)

    lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
    shell_calls = [l for l in lines if l["type"] == "response_item" and l["payload"].get("type") == "local_shell_call"]
    assert len(shell_calls) == 1
    assert shell_calls[0]["payload"]["action"]["command"][-1] == "cat auth.py"


def test_tool_result_becomes_function_output(sfs_session: Path, tmp_path: Path):
    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(sfs_session, output_path=output)

    lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
    outputs = [l for l in lines if l["type"] == "response_item" and l["payload"].get("type") == "function_call_output"]
    assert len(outputs) == 1
    assert "def login" in outputs[0]["payload"]["output"]["text"]


def test_turns_have_start_and_complete(sfs_session: Path, tmp_path: Path):
    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(sfs_session, output_path=output)

    lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
    starts = [l for l in lines if l["type"] == "event_msg" and l["payload"].get("type") == "task_started"]
    completes = [l for l in lines if l["type"] == "event_msg" and l["payload"].get("type") == "task_complete"]
    assert len(starts) == 2
    assert len(completes) == 2


def test_developer_role_mapped(tmp_path: Path):
    """System/developer messages map to Codex developer role."""
    d = tmp_path / "ses_dev1234abcdefgh.sfs"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "sfs_version": "0.1.0", "session_id": "ses_dev1234abcdefgh",
        "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-20T10:00:00Z",
        "source": {"tool": "claude-code"},
    }))
    with open(d / "messages.jsonl", "w") as f:
        f.write(json.dumps({
            "msg_id": "msg_001", "role": "developer",
            "content": [{"type": "text", "text": "You are a coding assistant"}],
            "timestamp": "2026-03-20T10:00:00Z",
        }) + "\n")

    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(d, output_path=output)

    lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
    dev_msgs = [
        l for l in lines
        if l["type"] == "response_item"
        and l["payload"].get("type") == "message"
        and l["payload"].get("role") == "developer"
    ]
    assert len(dev_msgs) == 1


def test_sidechain_messages_skipped(tmp_path: Path):
    d = tmp_path / "ses_side1234abcdefg.sfs"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "sfs_version": "0.1.0", "session_id": "ses_side1234abcdefg",
        "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-20T10:00:00Z",
        "source": {"tool": "claude-code"},
    }))
    with open(d / "messages.jsonl", "w") as f:
        f.write(json.dumps({
            "msg_id": "m1", "role": "user", "content": [{"type": "text", "text": "hi"}],
            "timestamp": "2026-03-20T10:00:00Z",
        }) + "\n")
        f.write(json.dumps({
            "msg_id": "m2", "role": "assistant", "content": [{"type": "text", "text": "sub"}],
            "timestamp": "2026-03-20T10:00:01Z", "is_sidechain": True,
        }) + "\n")

    output = tmp_path / "output.jsonl"
    result = convert_sfs_to_codex(d, output_path=output)
    assert result["message_count"] == 1  # Only the user message, not sidechain


def test_custom_tool_call_round_trips(tmp_path: Path):
    """A Codex-watcher custom_tool_call must round-trip as a custom_tool_call with
    the raw ``input`` string, not a function_call with JSON-encoded args."""
    d = tmp_path / "ses_ctc1234abcdefgh.sfs"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "sfs_version": "0.1.0", "session_id": "ses_ctc1234abcdefgh",
        "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-20T10:00:00Z",
        "source": {"tool": "codex"},
    }))
    patch = "*** Begin Patch\n*** Add File: /repo/new.py\n+print(1)\n*** End Patch\n"
    with open(d / "messages.jsonl", "w") as f:
        f.write(json.dumps({
            "msg_id": "msg_001", "role": "assistant",
            "content": [{
                "type": "tool_use", "tool_use_id": "call_001", "name": "apply_patch",
                "codex_item_type": "custom_tool_call",
                "input": {"input": patch},
            }],
            "timestamp": "2026-03-20T10:00:00Z",
        }) + "\n")
    (d / "workspace.json").write_text(json.dumps({"root_path": "/repo"}))

    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(d, output_path=output)

    lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
    ctc = [
        l for l in lines
        if l["type"] == "response_item" and l["payload"].get("type") == "custom_tool_call"
    ]
    assert len(ctc) == 1
    assert ctc[0]["payload"]["name"] == "apply_patch"
    assert ctc[0]["payload"]["input"] == patch

    # It must not be collapsed into a function_call with JSON-encoded args.
    fc = [
        l for l in lines
        if l["type"] == "response_item" and l["payload"].get("type") == "function_call"
    ]
    assert fc == []


def test_tool_use_call_id_reused_from_tool_use_id(tmp_path: Path):
    """A tool_use must keep its original id as call_id so the matching
    tool_result (which keeps its tool_use_id) lines up on resume."""
    d = tmp_path / "ses_callid1234abcdef.sfs"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "sfs_version": "0.1.0", "session_id": "ses_callid1234abcdef",
        "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-20T10:00:00Z",
        "source": {"tool": "codex"},
    }))
    with open(d / "messages.jsonl", "w") as f:
        f.write(json.dumps({
            "msg_id": "m1", "role": "assistant",
            "content": [{"type": "tool_use", "tool_use_id": "call_abc", "name": "Bash",
                        "input": {"command": "ls"}}],
        }) + "\n")
        f.write(json.dumps({
            "msg_id": "m2", "role": "tool",
            "content": [{"type": "tool_result", "tool_use_id": "call_abc", "content": "out"}],
        }) + "\n")

    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(d, output_path=output)

    lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
    calls = [l for l in lines if l["type"] == "response_item" and l["payload"].get("type") == "local_shell_call"]
    outputs = [l for l in lines if l["type"] == "response_item" and l["payload"].get("type") == "function_call_output"]
    assert len(calls) == 1 and len(outputs) == 1
    assert calls[0]["payload"]["call_id"] == "call_abc"
    assert outputs[0]["payload"]["call_id"] == "call_abc"


def test_tool_use_call_id_reused_from_id_field(tmp_path: Path):
    """Cursor-style tool_use blocks carry ``id`` (not ``tool_use_id``); that id
    must still be reused as the call_id."""
    d = tmp_path / "ses_callid2bcdefghij.sfs"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "sfs_version": "0.1.0", "session_id": "ses_callid2bcdefghij",
        "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-20T10:00:00Z",
        "source": {"tool": "cursor"},
    }))
    with open(d / "messages.jsonl", "w") as f:
        f.write(json.dumps({
            "msg_id": "m1", "role": "assistant",
            "content": [{"type": "tool_use", "id": "tc_001", "name": "Read",
                        "input": {"path": "a.py"}}],
        }) + "\n")
        f.write(json.dumps({
            "msg_id": "m2", "role": "tool",
            "content": [{"type": "tool_result", "tool_use_id": "tc_001", "content": "data"}],
        }) + "\n")

    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(d, output_path=output)

    lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
    calls = [l for l in lines if l["type"] == "response_item" and l["payload"].get("type") == "function_call"]
    outputs = [l for l in lines if l["type"] == "response_item" and l["payload"].get("type") == "function_call_output"]
    assert len(calls) == 1 and len(outputs) == 1
    assert calls[0]["payload"]["call_id"] == "tc_001"
    assert outputs[0]["payload"]["call_id"] == "tc_001"


def test_custom_tool_call_output_round_trips(tmp_path: Path):
    """A tool_result captured from custom_tool_call_output must round-trip as a
    custom_tool_call_output with a string output."""
    d = tmp_path / "ses_ctco1234abcdefg.sfs"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({
        "sfs_version": "0.1.0", "session_id": "ses_ctco1234abcdefg",
        "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-20T10:00:00Z",
        "source": {"tool": "codex"},
    }))
    with open(d / "messages.jsonl", "w") as f:
        f.write(json.dumps({
            "msg_id": "m1", "role": "assistant",
            "content": [{"type": "tool_use", "tool_use_id": "call_001", "name": "apply_patch",
                        "codex_item_type": "custom_tool_call",
                        "input": {"input": "*** Begin Patch\n*** End Patch\n"}}],
        }) + "\n")
        f.write(json.dumps({
            "msg_id": "m2", "role": "tool",
            "content": [{"type": "tool_result", "tool_use_id": "call_001",
                        "codex_item_type": "custom_tool_call_output",
                        "content": "patch applied"}],
        }) + "\n")

    output = tmp_path / "output.jsonl"
    convert_sfs_to_codex(d, output_path=output)

    lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
    outputs = [l for l in lines if l["type"] == "response_item" and l["payload"].get("type") == "custom_tool_call_output"]
    assert len(outputs) == 1
    assert outputs[0]["payload"]["call_id"] == "call_001"
    assert outputs[0]["payload"]["output"] == "patch applied"

    # It must not also be emitted as a function_call_output.
    fco = [l for l in lines if l["type"] == "response_item" and l["payload"].get("type") == "function_call_output"]
    assert fco == []
