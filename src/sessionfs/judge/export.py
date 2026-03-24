"""Export judge reports in multiple formats."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict

from sessionfs.judge.report import JudgeReport


def export_markdown(
    report: JudgeReport,
    session_title: str = "",
    session_tool: str = "",
    message_count: int = 0,
) -> str:
    """Export audit report as markdown."""
    s = report.summary
    lines: list[str] = []

    lines.append(f"# Audit Report: {report.session_id}")
    lines.append("")

    if session_title:
        lines.append(f"**Title:** {session_title}")
    if session_tool:
        lines.append(f"**Tool:** {session_tool}")
    if message_count:
        lines.append(f"**Messages:** {message_count}")
    lines.append(f"**Model:** {report.model}")
    lines.append(f"**Timestamp:** {report.timestamp}")
    lines.append("")

    # Summary
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Trust Score | {s.trust_score:.0%} |")
    lines.append(f"| Total Claims | {s.total_claims} |")
    lines.append(f"| Verified | {s.verified} |")
    lines.append(f"| Unverified | {s.unverified} |")
    lines.append(f"| Hallucinations | {s.hallucinations} |")
    lines.append(f"| Major | {s.major_findings} |")
    lines.append(f"| Moderate | {s.moderate_findings} |")
    lines.append(f"| Minor | {s.minor_findings} |")
    lines.append("")

    # Findings
    if report.findings:
        lines.append("## Findings")
        lines.append("")
        lines.append("| Msg | Verdict | Severity | Claim | Evidence | Explanation |")
        lines.append("|-----|---------|----------|-------|----------|-------------|")
        for f in report.findings:
            claim = f.claim.replace("|", "\\|")
            evidence = f.evidence.replace("|", "\\|")
            explanation = f.explanation.replace("|", "\\|")
            lines.append(
                f"| {f.message_index} | {f.verdict} | {f.severity} "
                f"| {claim} | {evidence} | {explanation} |"
            )
        lines.append("")
    else:
        lines.append("No verifiable claims found in this session.")
        lines.append("")

    return "\n".join(lines)


def export_csv(report: JudgeReport) -> str:
    """Export as CSV: message_index,verdict,severity,claim,evidence,explanation"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["message_index", "verdict", "severity", "claim", "evidence", "explanation"])
    for f in report.findings:
        writer.writerow([f.message_index, f.verdict, f.severity, f.claim, f.evidence, f.explanation])
    return output.getvalue()


def export_json(report: JudgeReport) -> str:
    """Export as indented JSON."""
    return json.dumps(asdict(report), indent=2)
