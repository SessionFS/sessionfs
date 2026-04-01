"""Tests for slash-command skill detection."""

from __future__ import annotations

from sessionfs.converters.skill_detector import (
    DetectedSkill,
    detect_skills,
    skills_to_tools_json,
)


def _user_msg(text: str) -> dict:
    """Helper: create a user message in .sfs format."""
    return {
        "msg_id": "msg_0000",
        "role": "user",
        "content": [{"type": "text", "text": text}],
    }


def _assistant_msg(text: str) -> dict:
    """Helper: create an assistant message in .sfs format."""
    return {
        "msg_id": "msg_0001",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }


class TestDetectSkills:
    def test_commit_detected(self):
        messages = [_user_msg("/commit")]
        result = detect_skills(messages, "claude-code")
        assert len(result) == 1
        assert result[0].name == "commit"
        assert result[0].source == "claude-code"
        assert result[0].message_index == 0
        assert result[0].raw_text == "/commit"

    def test_hyphenated_command(self):
        messages = [_user_msg("/review-pr")]
        result = detect_skills(messages, "claude-code")
        assert len(result) == 1
        assert result[0].name == "review-pr"

    def test_ignores_assistant_messages(self):
        messages = [_assistant_msg("/commit")]
        result = detect_skills(messages, "claude-code")
        assert len(result) == 0

    def test_ignores_file_paths(self):
        messages = [
            _user_msg("/Users/ola/file.py"),
            _user_msg("/api/v1/sessions"),
            _user_msg("/usr/bin/python"),
        ]
        result = detect_skills(messages, "claude-code")
        assert len(result) == 0

    def test_empty_messages(self):
        result = detect_skills([], "claude-code")
        assert result == []

    def test_multiple_skills_in_session(self):
        messages = [
            _user_msg("/commit"),
            _assistant_msg("Done."),
            _user_msg("/review-pr"),
            _assistant_msg("Reviewed."),
            _user_msg("/release"),
        ]
        result = detect_skills(messages, "claude-code")
        assert len(result) == 3
        names = [s.name for s in result]
        assert names == ["commit", "review-pr", "release"]

    def test_skill_mid_text(self):
        messages = [_user_msg("Please run /commit now")]
        result = detect_skills(messages, "claude-code")
        assert len(result) == 1
        assert result[0].name == "commit"

    def test_string_content_format(self):
        """Messages with content as plain string (not list)."""
        messages = [{"role": "user", "content": "/release"}]
        result = detect_skills(messages, "claude-code")
        assert len(result) == 1
        assert result[0].name == "release"


class TestSkillsToToolsJson:
    def test_deduplication_and_count(self):
        skills = [
            DetectedSkill(name="commit", message_index=0, source="claude-code", raw_text="/commit"),
            DetectedSkill(name="commit", message_index=5, source="claude-code", raw_text="/commit"),
            DetectedSkill(name="review-pr", message_index=3, source="claude-code", raw_text="/review-pr"),
        ]
        result = skills_to_tools_json(skills)
        assert len(result) == 2

        by_name = {t["name"]: t for t in result}
        assert by_name["commit"]["invocation_count"] == 2
        assert by_name["commit"]["type"] == "slash_command"
        assert by_name["commit"]["source"] == "claude-code"
        assert by_name["review-pr"]["invocation_count"] == 1

    def test_empty_skills(self):
        result = skills_to_tools_json([])
        assert result == []
