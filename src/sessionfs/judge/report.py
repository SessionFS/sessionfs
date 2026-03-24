"""Judge report data model and persistence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Finding:
    message_index: int
    claim: str
    verdict: str  # verified, unverified, hallucination
    severity: str  # minor, moderate, major
    evidence: str
    explanation: str


@dataclass
class AuditSummary:
    total_claims: int
    verified: int
    unverified: int
    hallucinations: int
    trust_score: float  # 0.0 to 1.0
    major_findings: int
    moderate_findings: int
    minor_findings: int


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
        )
    )


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

    findings = [Finding(**f) for f in data.get("findings", [])]
    summary_data = data.get("summary", {})
    summary = AuditSummary(**summary_data) if summary_data else AuditSummary(
        total_claims=0, verified=0, unverified=0, hallucinations=0,
        trust_score=0.0, major_findings=0, moderate_findings=0, minor_findings=0,
    )

    return JudgeReport(
        session_id=data["session_id"],
        model=data["model"],
        timestamp=data["timestamp"],
        findings=findings,
        summary=summary,
    )
