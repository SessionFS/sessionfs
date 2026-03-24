"""Tests for judge export, OpenRouter provider detection, and Fernet encryption."""

from __future__ import annotations

import csv
import io
import json

import pytest

from sessionfs.judge.export import export_csv, export_json, export_markdown
from sessionfs.judge.report import AuditSummary, Finding, JudgeReport


def _make_report() -> JudgeReport:
    """Create a sample report for testing."""
    return JudgeReport(
        session_id="ses_abc123",
        model="claude-sonnet-4",
        timestamp="2026-03-23T12:00:00+00:00",
        findings=[
            Finding(
                message_index=3,
                claim="Tests pass after the change",
                verdict="verified",
                severity="minor",
                evidence="exit code 0",
                explanation="Test runner exited with code 0.",
            ),
            Finding(
                message_index=7,
                claim="Created file utils.py",
                verdict="hallucination",
                severity="major",
                evidence="file not found error",
                explanation="No tool call created the file.",
            ),
            Finding(
                message_index=10,
                claim="Refactored the parser module",
                verdict="unverified",
                severity="moderate",
                evidence="",
                explanation="No evidence of parser changes in tool calls.",
            ),
        ],
        summary=AuditSummary(
            total_claims=3,
            verified=1,
            unverified=1,
            hallucinations=1,
            trust_score=0.333,
            major_findings=1,
            moderate_findings=1,
            minor_findings=1,
        ),
    )


class TestExportMarkdown:
    def test_contains_header(self):
        report = _make_report()
        md = export_markdown(report)
        assert "# Audit Report: ses_abc123" in md

    def test_contains_summary_table(self):
        report = _make_report()
        md = export_markdown(report)
        assert "| Trust Score |" in md
        assert "| Total Claims | 3 |" in md

    def test_contains_findings_table(self):
        report = _make_report()
        md = export_markdown(report)
        assert "## Findings" in md
        assert "| Msg | Verdict |" in md
        assert "verified" in md
        assert "hallucination" in md

    def test_includes_session_metadata(self):
        report = _make_report()
        md = export_markdown(report, session_title="My Session", session_tool="claude-code", message_count=42)
        assert "**Title:** My Session" in md
        assert "**Tool:** claude-code" in md
        assert "**Messages:** 42" in md

    def test_empty_findings(self):
        report = JudgeReport(
            session_id="ses_empty",
            model="gpt-4o",
            timestamp="2026-03-23T12:00:00+00:00",
            findings=[],
            summary=AuditSummary(
                total_claims=0, verified=0, unverified=0, hallucinations=0,
                trust_score=0.0, major_findings=0, moderate_findings=0, minor_findings=0,
            ),
        )
        md = export_markdown(report)
        assert "No verifiable claims" in md


class TestExportCSV:
    def test_header_row(self):
        report = _make_report()
        output = export_csv(report)
        reader = csv.reader(io.StringIO(output))
        header = next(reader)
        assert header == ["message_index", "verdict", "severity", "claim", "evidence", "explanation"]

    def test_row_count(self):
        report = _make_report()
        output = export_csv(report)
        reader = csv.reader(io.StringIO(output))
        rows = list(reader)
        # 1 header + 3 data rows
        assert len(rows) == 4

    def test_row_values(self):
        report = _make_report()
        output = export_csv(report)
        reader = csv.reader(io.StringIO(output))
        next(reader)  # skip header
        first_row = next(reader)
        assert first_row[0] == "3"
        assert first_row[1] == "verified"
        assert first_row[2] == "minor"


class TestExportJSON:
    def test_valid_json(self):
        report = _make_report()
        output = export_json(report)
        data = json.loads(output)
        assert isinstance(data, dict)

    def test_fields_present(self):
        report = _make_report()
        output = export_json(report)
        data = json.loads(output)
        assert data["session_id"] == "ses_abc123"
        assert data["model"] == "claude-sonnet-4"
        assert "findings" in data
        assert "summary" in data
        assert len(data["findings"]) == 3

    def test_summary_fields(self):
        report = _make_report()
        output = export_json(report)
        data = json.loads(output)
        summary = data["summary"]
        assert summary["total_claims"] == 3
        assert summary["trust_score"] == 0.333
        assert summary["hallucinations"] == 1


class TestOpenRouterDetection:
    def test_slash_model_routes_to_openrouter(self):
        from sessionfs.judge.providers import _detect_provider

        assert _detect_provider("meta-llama/llama-3-70b") == "openrouter"

    def test_unknown_model_routes_to_openrouter(self):
        from sessionfs.judge.providers import _detect_provider

        assert _detect_provider("mistral-large-2") == "openrouter"

    def test_known_models_still_work(self):
        from sessionfs.judge.providers import _detect_provider

        assert _detect_provider("claude-sonnet-4") == "anthropic"
        assert _detect_provider("gpt-4o") == "openai"
        assert _detect_provider("gemini-1.5-pro") == "google"

    def test_openai_prefixes(self):
        from sessionfs.judge.providers import _detect_provider

        assert _detect_provider("o1-mini") == "openai"
        assert _detect_provider("o3-mini") == "openai"


class TestFernetEncryption:
    def test_round_trip(self):
        """Encrypt then decrypt an API key using the same derivation."""
        import base64
        import hashlib

        from cryptography.fernet import Fernet

        secret = "test-secret-value"
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        fernet = Fernet(key)

        original = "sk-test-1234567890abcdef"
        encrypted = fernet.encrypt(original.encode())
        decrypted = fernet.decrypt(encrypted).decode()

        assert decrypted == original
        assert encrypted != original.encode()

    def test_different_secrets_different_keys(self):
        """Different secrets produce different encryptions."""
        import base64
        import hashlib

        from cryptography.fernet import Fernet

        original = "sk-test-key"

        key1 = base64.urlsafe_b64encode(hashlib.sha256(b"secret-a").digest())
        key2 = base64.urlsafe_b64encode(hashlib.sha256(b"secret-b").digest())

        encrypted1 = Fernet(key1).encrypt(original.encode())
        encrypted2 = Fernet(key2).encrypt(original.encode())

        # Can decrypt with matching key
        assert Fernet(key1).decrypt(encrypted1).decode() == original
        assert Fernet(key2).decrypt(encrypted2).decode() == original

        # Cannot decrypt with wrong key
        with pytest.raises(Exception):
            Fernet(key2).decrypt(encrypted1)
