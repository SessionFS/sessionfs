"""Tests for the LLM-as-a-Judge system."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sessionfs.judge.evidence import Evidence, gather_evidence
from sessionfs.judge.extractor import Claim, extract_claims
from sessionfs.judge.judge import (
    _compute_summary,
    _deduplicate_findings,
    _parse_judge_response,
    build_judge_prompt,
    chunk_messages,
)
from sessionfs.judge.report import (
    AuditSummary,
    Finding,
    JudgeReport,
    load_report,
    save_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_messages() -> list[dict]:
    """Build a realistic message sequence for testing."""
    return [
        # 0: user request
        {"role": "user", "content": "Create a hello.py file that prints hello world"},
        # 1: assistant claims + tool_use
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I've created the file hello.py with the print statement."},
                {
                    "type": "tool_use",
                    "id": "tool_1",
                    "name": "Write",
                    "input": {"file_path": "/tmp/hello.py", "content": "print('hello world')"},
                },
            ],
        },
        # 2: tool result
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_1",
                    "content": "File created successfully at /tmp/hello.py",
                },
            ],
        },
        # 3: assistant runs test
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Running the script to verify. The test passes successfully."},
                {
                    "type": "tool_use",
                    "id": "tool_2",
                    "name": "Bash",
                    "input": {"command": "python /tmp/hello.py"},
                },
            ],
        },
        # 4: bash result
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_2",
                    "content": "hello world\nexit code: 0",
                },
            ],
        },
        # 5: assistant speculative claim
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "This might also work with Python 2, but I think it could have issues with the print function.",
                },
            ],
        },
        # 6: assistant modifies code
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "I've updated the function to handle edge cases and added error handling.",
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Claim extraction tests
# ---------------------------------------------------------------------------

class TestExtractClaims:
    def test_extracts_file_operation_claim(self):
        messages = _make_messages()
        claims = extract_claims(messages)
        file_claims = [c for c in claims if c.category == "file_operation"]
        assert len(file_claims) >= 1
        assert any("hello.py" in c.text for c in file_claims)

    def test_extracts_test_result_claim(self):
        messages = _make_messages()
        claims = extract_claims(messages)
        test_claims = [c for c in claims if c.category == "test_result"]
        assert len(test_claims) >= 1
        assert any("passes" in c.text.lower() for c in test_claims)

    def test_extracts_code_change_claim(self):
        messages = _make_messages()
        claims = extract_claims(messages)
        code_claims = [c for c in claims if c.category == "code_change"]
        assert len(code_claims) >= 1
        assert any("updated" in c.text.lower() for c in code_claims)

    def test_speculative_confidence(self):
        messages = _make_messages()
        claims = extract_claims(messages)
        speculative = [c for c in claims if c.confidence == "speculative"]
        assert len(speculative) >= 1
        assert any("might" in c.text.lower() or "could" in c.text.lower() for c in speculative)

    def test_certain_confidence(self):
        messages = [
            {"role": "assistant", "content": "I've definitely created the file successfully."},
        ]
        claims = extract_claims(messages)
        assert len(claims) >= 1
        assert claims[0].confidence == "certain"

    def test_evidence_refs_populated(self):
        messages = _make_messages()
        claims = extract_claims(messages)
        # Claims from message 1 should reference nearby tool_results
        msg1_claims = [c for c in claims if c.message_index == 1]
        assert len(msg1_claims) >= 1
        assert len(msg1_claims[0].evidence_refs) > 0

    def test_no_claims_from_user_messages(self):
        messages = [
            {"role": "user", "content": "I've created a file called test.py"},
        ]
        claims = extract_claims(messages)
        assert len(claims) == 0

    def test_empty_messages(self):
        claims = extract_claims([])
        assert claims == []

    def test_string_content(self):
        messages = [
            {"role": "assistant", "content": "I've successfully created the configuration file."},
        ]
        claims = extract_claims(messages)
        assert len(claims) >= 1


# ---------------------------------------------------------------------------
# Evidence gathering tests
# ---------------------------------------------------------------------------

class TestGatherEvidence:
    def test_gathers_tool_use_and_result(self):
        messages = _make_messages()
        evidence = gather_evidence(messages)
        assert len(evidence) >= 2  # Write result + Bash result

    def test_captures_file_path(self):
        messages = _make_messages()
        evidence = gather_evidence(messages)
        write_evidence = [e for e in evidence if e.tool_name == "Write"]
        assert len(write_evidence) >= 1
        assert write_evidence[0].file_path == "/tmp/hello.py"

    def test_captures_exit_code(self):
        messages = _make_messages()
        evidence = gather_evidence(messages)
        bash_evidence = [e for e in evidence if e.tool_name == "Bash"]
        assert len(bash_evidence) >= 1
        assert bash_evidence[0].exit_code == 0

    def test_captures_input_summary(self):
        messages = _make_messages()
        evidence = gather_evidence(messages)
        bash_evidence = [e for e in evidence if e.tool_name == "Bash"]
        assert len(bash_evidence) >= 1
        assert "python" in bash_evidence[0].input_summary

    def test_captures_output_summary(self):
        messages = _make_messages()
        evidence = gather_evidence(messages)
        bash_evidence = [e for e in evidence if e.tool_name == "Bash"]
        assert len(bash_evidence) >= 1
        assert "hello world" in bash_evidence[0].output_summary

    def test_role_tool_messages(self):
        """Test evidence gathering from role=tool messages (alternative format)."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.txt"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_use_id": "t1",
                "content": "file contents here",
            },
        ]
        evidence = gather_evidence(messages)
        assert len(evidence) >= 1
        read_ev = [e for e in evidence if e.tool_name == "Read"]
        assert len(read_ev) >= 1
        assert read_ev[0].file_path == "/tmp/test.txt"

    def test_empty_messages(self):
        evidence = gather_evidence([])
        assert evidence == []


# ---------------------------------------------------------------------------
# Report serialisation tests
# ---------------------------------------------------------------------------

class TestReport:
    def test_save_and_load_report(self, tmp_path: Path):
        report = JudgeReport(
            session_id="ses_test123",
            model="claude-sonnet-4",
            timestamp="2026-03-23T00:00:00Z",
            findings=[
                Finding(
                    message_index=1,
                    claim="I created hello.py",
                    verdict="verified",
                    severity="minor",
                    evidence="File created at /tmp/hello.py",
                    explanation="Tool result confirms file creation",
                ),
                Finding(
                    message_index=3,
                    claim="The test passes",
                    verdict="hallucination",
                    severity="major",
                    evidence="Exit code was 1",
                    explanation="The test actually failed with exit code 1",
                ),
            ],
            summary=AuditSummary(
                total_claims=2,
                verified=1,
                unverified=0,
                hallucinations=1,
                trust_score=0.5,
                major_findings=1,
                moderate_findings=0,
                minor_findings=1,
            ),
        )

        path = save_report(report, tmp_path)
        assert path.exists()
        assert path.name == "audit_report.json"

        loaded = load_report(tmp_path)
        assert loaded is not None
        assert loaded.session_id == "ses_test123"
        assert loaded.model == "claude-sonnet-4"
        assert len(loaded.findings) == 2
        assert loaded.findings[0].verdict == "verified"
        assert loaded.findings[1].verdict == "hallucination"
        assert loaded.summary.trust_score == 0.5
        assert loaded.summary.major_findings == 1

    def test_load_nonexistent_report(self, tmp_path: Path):
        report = load_report(tmp_path)
        assert report is None

    def test_load_corrupt_report(self, tmp_path: Path):
        (tmp_path / "audit_report.json").write_text("not json")
        report = load_report(tmp_path)
        assert report is None

    def test_report_json_roundtrip(self):
        report = JudgeReport(
            session_id="ses_abc",
            model="gpt-4o",
            timestamp="2026-03-23T12:00:00Z",
            findings=[],
            summary=AuditSummary(
                total_claims=0, verified=0, unverified=0, hallucinations=0,
                trust_score=1.0, major_findings=0, moderate_findings=0, minor_findings=0,
            ),
        )
        data = asdict(report)
        json_str = json.dumps(data)
        parsed = json.loads(json_str)
        assert parsed["session_id"] == "ses_abc"
        assert parsed["summary"]["trust_score"] == 1.0


# ---------------------------------------------------------------------------
# Chunking tests
# ---------------------------------------------------------------------------

class TestChunking:
    def test_small_message_list_single_chunk(self):
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
        chunks = chunk_messages(messages, window_size=50)
        assert len(chunks) == 1
        assert len(chunks[0]) == 10

    def test_large_message_list_multiple_chunks(self):
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(120)]
        chunks = chunk_messages(messages, window_size=50, overlap=5)
        assert len(chunks) >= 3
        # Each chunk except the last should be window_size
        assert len(chunks[0]) == 50

    def test_overlap_between_chunks(self):
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(100)]
        chunks = chunk_messages(messages, window_size=50, overlap=10)
        # The last message of chunk 0 and first messages of chunk 1 should overlap
        assert chunks[0][-1] == chunks[1][9]  # 10th element of chunk 1

    def test_empty_messages(self):
        chunks = chunk_messages([])
        assert len(chunks) == 1
        assert len(chunks[0]) == 0

    def test_exact_window_size(self):
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(50)]
        chunks = chunk_messages(messages, window_size=50)
        assert len(chunks) == 1


# ---------------------------------------------------------------------------
# Prompt building tests
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_includes_claims_section(self):
        claims = [
            Claim(message_index=1, text="I created foo.py", category="file_operation",
                  confidence="certain", evidence_refs=[2]),
        ]
        evidence = [
            Evidence(message_index=2, tool_name="Write", input_summary="file: /tmp/foo.py",
                     output_summary="File created", exit_code=None, file_path="/tmp/foo.py"),
        ]
        messages = [
            {"role": "user", "content": "make foo.py"},
            {"role": "assistant", "content": "I created foo.py"},
            {"role": "tool", "content": "File created"},
        ]

        prompt = build_judge_prompt(claims, evidence, messages)
        assert "Claims to Verify" in prompt
        assert "I created foo.py" in prompt

    def test_includes_evidence_section(self):
        claims = [
            Claim(message_index=1, text="Test passes", category="test_result",
                  confidence="likely", evidence_refs=[]),
        ]
        evidence = [
            Evidence(message_index=2, tool_name="Bash", input_summary="pytest",
                     output_summary="1 passed", exit_code=0, file_path=None),
        ]
        messages = [
            {"role": "user", "content": "run tests"},
            {"role": "assistant", "content": "Test passes"},
            {"role": "tool", "content": "1 passed"},
        ]

        prompt = build_judge_prompt(claims, evidence, messages)
        assert "Evidence from Tool Calls" in prompt
        assert "exit_code=0" in prompt

    def test_includes_message_context(self):
        claims = [
            Claim(message_index=0, text="I fixed the bug", category="code_change",
                  confidence="likely", evidence_refs=[]),
        ]
        prompt = build_judge_prompt(
            claims, [],
            [{"role": "assistant", "content": "I fixed the bug"}],
        )
        assert "Message Context" in prompt


# ---------------------------------------------------------------------------
# Judge response parsing tests
# ---------------------------------------------------------------------------

class TestParseJudgeResponse:
    def test_parses_valid_json(self):
        claims = [
            Claim(message_index=1, text="Created file", category="file_operation",
                  confidence="certain", evidence_refs=[]),
        ]
        response = json.dumps([
            {
                "claim_index": 0,
                "verdict": "verified",
                "severity": "minor",
                "evidence": "Tool result shows file created",
                "explanation": "The file was indeed created",
            }
        ])
        findings = _parse_judge_response(response, claims)
        assert len(findings) == 1
        assert findings[0].verdict == "verified"
        assert findings[0].severity == "minor"

    def test_handles_markdown_fences(self):
        claims = [
            Claim(message_index=0, text="Test passes", category="test_result",
                  confidence="likely", evidence_refs=[]),
        ]
        response = "```json\n" + json.dumps([
            {"claim_index": 0, "verdict": "hallucination", "severity": "major",
             "evidence": "exit code 1", "explanation": "test failed"}
        ]) + "\n```"
        findings = _parse_judge_response(response, claims)
        assert len(findings) == 1
        assert findings[0].verdict == "hallucination"

    def test_handles_invalid_json(self):
        claims = [
            Claim(message_index=0, text="claim", category="general",
                  confidence="likely", evidence_refs=[]),
        ]
        findings = _parse_judge_response("not json at all", claims)
        assert findings == []

    def test_handles_out_of_range_claim_index(self):
        claims = [
            Claim(message_index=0, text="claim", category="general",
                  confidence="likely", evidence_refs=[]),
        ]
        response = json.dumps([
            {"claim_index": 99, "verdict": "verified", "severity": "minor",
             "evidence": "", "explanation": ""},
        ])
        findings = _parse_judge_response(response, claims)
        assert len(findings) == 0

    def test_normalises_invalid_verdict(self):
        claims = [
            Claim(message_index=0, text="claim", category="general",
                  confidence="likely", evidence_refs=[]),
        ]
        response = json.dumps([
            {"claim_index": 0, "verdict": "BOGUS", "severity": "minor",
             "evidence": "", "explanation": ""},
        ])
        findings = _parse_judge_response(response, claims)
        assert len(findings) == 1
        assert findings[0].verdict == "unverified"


# ---------------------------------------------------------------------------
# Summary computation tests
# ---------------------------------------------------------------------------

class TestComputeSummary:
    def test_computes_trust_score(self):
        findings = [
            Finding(message_index=0, claim="a", verdict="verified", severity="minor",
                    evidence="", explanation=""),
            Finding(message_index=1, claim="b", verdict="verified", severity="minor",
                    evidence="", explanation=""),
            Finding(message_index=2, claim="c", verdict="hallucination", severity="major",
                    evidence="", explanation=""),
        ]
        summary = _compute_summary(findings)
        assert summary.total_claims == 3
        assert summary.verified == 2
        assert summary.hallucinations == 1
        assert summary.trust_score == pytest.approx(0.667, abs=0.001)
        assert summary.major_findings == 1

    def test_empty_findings(self):
        summary = _compute_summary([])
        assert summary.total_claims == 0
        assert summary.trust_score == 0.0


# ---------------------------------------------------------------------------
# Deduplication tests
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_removes_duplicates(self):
        findings = [
            Finding(message_index=1, claim="I created foo.py", verdict="verified",
                    severity="minor", evidence="", explanation=""),
            Finding(message_index=1, claim="I created foo.py", verdict="verified",
                    severity="minor", evidence="", explanation="different"),
        ]
        deduped = _deduplicate_findings(findings)
        assert len(deduped) == 1

    def test_keeps_different_claims(self):
        findings = [
            Finding(message_index=1, claim="claim A", verdict="verified",
                    severity="minor", evidence="", explanation=""),
            Finding(message_index=1, claim="claim B", verdict="hallucination",
                    severity="major", evidence="", explanation=""),
        ]
        deduped = _deduplicate_findings(findings)
        assert len(deduped) == 2


# ---------------------------------------------------------------------------
# Integration test (mocked LLM)
# ---------------------------------------------------------------------------

class TestJudgeSessionMocked:
    @pytest.mark.asyncio
    async def test_judge_session_end_to_end(self, tmp_path: Path):
        """Full pipeline with mocked LLM call."""
        # Write a messages.jsonl file
        messages = _make_messages()
        messages_path = tmp_path / "messages.jsonl"
        with open(messages_path, "w") as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")

        mock_response = json.dumps([
            {
                "claim_index": 0,
                "verdict": "verified",
                "severity": "minor",
                "evidence": "Tool result confirms creation",
                "explanation": "The file was created as claimed",
            },
        ])

        with patch("sessionfs.judge.judge.call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_response

            from sessionfs.judge.judge import judge_session

            report = await judge_session(
                session_id="ses_testmocked123",
                sfs_dir=tmp_path,
                model="claude-sonnet-4",
                api_key="test-key-not-real",
            )

        assert report.session_id == "ses_testmocked123"
        assert report.model == "claude-sonnet-4"
        assert len(report.findings) >= 1
        assert report.summary.total_claims >= 1

        # Verify report was saved
        assert (tmp_path / "audit_report.json").exists()

        # Verify LLM was called
        mock_llm.assert_called_once()

    @pytest.mark.asyncio
    async def test_judge_session_no_messages(self, tmp_path: Path):
        """Should raise ValueError when no messages exist."""
        from sessionfs.judge.judge import judge_session

        with pytest.raises(ValueError, match="No messages found"):
            await judge_session(
                session_id="ses_empty1234567",
                sfs_dir=tmp_path,
                model="claude-sonnet-4",
                api_key="test-key",
            )

    @pytest.mark.asyncio
    async def test_judge_session_no_claims(self, tmp_path: Path):
        """Should return empty report when no verifiable claims exist."""
        messages_path = tmp_path / "messages.jsonl"
        with open(messages_path, "w") as f:
            f.write(json.dumps({"role": "user", "content": "hello"}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": "Hi there!"}) + "\n")

        from sessionfs.judge.judge import judge_session

        report = await judge_session(
            session_id="ses_noclaims12345",
            sfs_dir=tmp_path,
            model="claude-sonnet-4",
            api_key="test-key",
        )

        assert report.summary.total_claims == 0
        assert report.summary.trust_score == 1.0
        assert len(report.findings) == 0

    @pytest.mark.asyncio
    async def test_judge_session_no_api_key(self, tmp_path: Path):
        """Should raise ValueError when no API key is provided."""
        from sessionfs.judge.judge import judge_session

        with pytest.raises(ValueError, match="API key is required"):
            await judge_session(
                session_id="ses_nokey12345678",
                sfs_dir=tmp_path,
                model="claude-sonnet-4",
                api_key=None,
            )
