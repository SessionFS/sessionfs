"""Judge report data model and persistence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Category → severity mapping (deterministic, not LLM-judged)
SEVERITY_FROM_CATEGORY = {
    "test_result": "critical",
    "command_output": "critical",
    "dependency": "critical",
    "file_existence": "high",
    "data_misread": "high",
    "code_claim": "high",
    "other": "low",
}

CWE_FROM_CATEGORY = {
    "test_result": "CWE-393",      # Return of Wrong Status Code
    "command_output": "CWE-684",    # Incorrect Provision of Specified Functionality
    "dependency": "CWE-1104",       # Use of Unmaintained Third Party Components
    "file_existence": "CWE-552",    # Files or Directories Accessible to External Parties
    "data_misread": "CWE-135",      # Incorrect Calculation of Multi-Byte String Length (data misinterpretation)
    "code_claim": "CWE-710",        # Improper Adherence to Coding Standards
    "other": "",
}


@dataclass
class Finding:
    message_index: int
    claim: str
    verdict: str  # verified, unverified, hallucination
    severity: str  # critical, high, low (auto-assigned from category)
    evidence: str
    explanation: str
    category: str = "other"  # test_result, file_existence, command_output, data_misread, code_claim, dependency, other
    confidence: int = 0  # 0-100, LLM-judged confidence in the verdict
    evidence_snippets: list[dict] = field(default_factory=list)  # [{"source": "tool_result|message", "message_index": int, "text": str}]
    cwe_id: str = ""  # CWE mapping from category
    dismissed: bool = False
    dismissed_by: str = ""
    dismissed_reason: str = ""


@dataclass
class AuditSummary:
    total_claims: int
    verified: int
    unverified: int
    hallucinations: int
    trust_score: float  # 0.0 to 1.0
    major_findings: int  # kept for backward compat — maps to critical
    moderate_findings: int  # maps to high
    minor_findings: int  # maps to low
    critical_count: int = 0
    high_count: int = 0
    low_count: int = 0


@dataclass
class JudgeReport:
    session_id: str
    model: str
    timestamp: str
    findings: list[Finding] = field(default_factory=list)
    summary: AuditSummary = field(
        default_factory=lambda: AuditSummary(
            total_claims=0,
            verified=0,
            unverified=0,
            hallucinations=0,
            trust_score=0.0,
            major_findings=0,
            moderate_findings=0,
            minor_findings=0,
            critical_count=0,
            high_count=0,
            low_count=0,
        )
    )
    provider: str = ""
    base_url: str = ""
    execution_time_ms: int = 0
    warnings: list[str] = field(default_factory=list)


def save_report(report: JudgeReport, sfs_dir: Path) -> Path:
    """Save report as audit_report.json alongside the session."""
    report_path = sfs_dir / "audit_report.json"
    data = asdict(report)
    report_path.write_text(json.dumps(data, indent=2))
    return report_path


def load_report(sfs_dir: Path) -> JudgeReport | None:
    """Load existing report if it exists."""
    report_path = sfs_dir / "audit_report.json"
    if not report_path.exists():
        return None

    try:
        data = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    findings = [Finding(**{k: v for k, v in f.items() if k in Finding.__dataclass_fields__}) for f in data.get("findings", [])]
    summary_data = data.get("summary", {})
    summary_fields = {k: v for k, v in summary_data.items() if k in AuditSummary.__dataclass_fields__}
    summary = AuditSummary(**summary_fields) if summary_fields else AuditSummary(
        total_claims=0, verified=0, unverified=0, hallucinations=0,
        trust_score=0.0, major_findings=0, moderate_findings=0, minor_findings=0,
    )

    return JudgeReport(
        session_id=data["session_id"],
        model=data["model"],
        timestamp=data["timestamp"],
        findings=findings,
        summary=summary,
        provider=data.get("provider", ""),
        base_url=data.get("base_url", ""),
        execution_time_ms=data.get("execution_time_ms", 0),
    )
