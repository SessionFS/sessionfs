"""Tests for session summarizer."""

from __future__ import annotations

from sessionfs.server.services.summarizer import summarize_session


def _make_messages():
    return [
        {"role": "user", "content": [{"type": "text", "text": "Fix the auth bug"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "src/auth.py"}},
        ]},
        {"role": "tool", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "import jwt\ndef verify():\n    pass"},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "src/auth.py", "old_string": "pass", "new_string": "return True"}},
        ]},
        {"role": "tool", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": "File edited"},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "pytest tests/test_auth.py -v"}},
        ]},
        {"role": "tool", "content": [
            {"type": "tool_result", "tool_use_id": "t3", "content": "3 passed, 1 failed in 0.5s"},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t4", "name": "Bash", "input": {"command": "pip install pyjwt redis"}},
        ]},
        {"role": "tool", "content": [
            {"type": "tool_result", "tool_use_id": "t4", "content": "Successfully installed pyjwt-2.8.0 redis-5.0.0"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "Fixed."}]},
    ]


class TestDeterministicSummarizer:
    def test_extracts_files_modified(self):
        summary = summarize_session(_make_messages())
        assert "src/auth.py" in summary.files_modified

    def test_extracts_files_read(self):
        summary = summarize_session(_make_messages())
        # auth.py is both read and edited, so it should be in modified, not read
        assert "src/auth.py" not in summary.files_read

    def test_counts_tool_calls(self):
        summary = summarize_session(_make_messages())
        assert summary.tool_call_count >= 4

    def test_extracts_test_results(self):
        summary = summarize_session(_make_messages())
        assert summary.tests_passed == 3
        assert summary.tests_failed == 1
        assert summary.tests_run == 4

    def test_extracts_packages(self):
        summary = summarize_session(_make_messages())
        assert "pyjwt" in summary.packages_installed
        assert "redis" in summary.packages_installed

    def test_counts_commands(self):
        summary = summarize_session(_make_messages())
        assert summary.commands_executed == 2  # pytest + pip

    def test_message_count(self):
        summary = summarize_session(_make_messages())
        assert summary.message_count == 10

    def test_empty_session(self):
        summary = summarize_session([])
        assert summary.message_count == 0
        assert summary.tool_call_count == 0
        assert summary.files_modified == []

    def test_with_manifest(self):
        summary = summarize_session(
            _make_messages(),
            manifest={"title": "Fix auth", "source_tool": "claude-code"},
        )
        assert summary.title == "Fix auth"
        assert summary.tool == "claude-code"


class TestTestResultParsing:
    def test_pytest_format(self):
        msgs = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}},
            ]},
            {"role": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "5 passed in 1.2s"},
            ]},
        ]
        s = summarize_session(msgs)
        assert s.tests_passed == 5
        assert s.tests_failed == 0

    def test_jest_format(self):
        msgs = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "npm test"}},
            ]},
            {"role": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "Tests: 10 passed, 2 failed, 12 total"},
            ]},
        ]
        s = summarize_session(msgs)
        assert s.tests_passed == 10
        assert s.tests_failed == 2


class TestModels:
    def test_summary_record_model(self):
        from sessionfs.server.db.models import SessionSummaryRecord
        assert hasattr(SessionSummaryRecord, "session_id")
        assert hasattr(SessionSummaryRecord, "files_modified")
        assert hasattr(SessionSummaryRecord, "what_happened")

    def test_user_has_summarize_trigger(self):
        from sessionfs.server.db.models import User
        assert hasattr(User, "summarize_trigger")
