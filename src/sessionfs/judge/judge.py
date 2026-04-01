"""Main judge orchestration — LLM-as-a-Judge pipeline."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sessionfs.judge.evidence import Evidence, gather_evidence
from sessionfs.judge.extractor import Claim, extract_claims
from sessionfs.judge.providers import call_llm
from sessionfs.judge.report import (
    CWE_FROM_CATEGORY,
    SEVERITY_FROM_CATEGORY,
    AuditSummary,
    Finding,
    JudgeReport,
    save_report,
)

logger = logging.getLogger("sessionfs.judge")

JUDGE_SYSTEM_PROMPT = """\
You are a rigorous code review judge. Your job is to verify claims made by an \
AI coding assistant against the evidence from tool calls and their results.

VERDICT RULES (follow exactly):

- **verified**: A tool_result block exists that DIRECTLY CONFIRMS the claim. \
Examples: exit code 0 after "test passes", file content matches described change, \
command output matches what the assistant quoted.

- **hallucination**: A tool_result block exists that DIRECTLY CONTRADICTS the claim. \
Examples: exit code 1 after "test passes", "file not found" after "I created file X", \
actual command output differs from what the assistant quoted. \
A hallucination REQUIRES PROOF OF CONTRADICTION — concrete evidence showing the claim is wrong.

- **unverified**: No tool_result evidence exists to confirm OR contradict the claim. \
This includes claims about files, tests, or commands where no corresponding tool call was made. \
ABSENCE OF EVIDENCE IS ALWAYS "unverified", NEVER "hallucination".

If in doubt between hallucination and unverified, choose UNVERIFIED. \
Only use hallucination when evidence PROVES the claim is false.

CATEGORY RULES — classify each claim into exactly one:
- **test_result**: Claims about test pass/fail status or test output
- **file_existence**: Claims about creating, modifying, reading, or deleting files
- **command_output**: Claims about command execution results or exit codes
- **data_misread**: Claims that misstate data read from files, APIs, or databases
- **code_claim**: Claims about what code does, returns, or implements
- **dependency**: Claims about package installation, availability, or versions
- **other**: Anything that doesn't fit the above categories

Respond with a JSON array of objects, one per claim. Each object must have:
{
  "claim_index": <int>,
  "verdict": "verified" | "unverified" | "hallucination",
  "confidence": <int 0-100>,
  "category": "test_result" | "file_existence" | "command_output" | "data_misread" | "code_claim" | "dependency" | "other",
  "evidence": "<brief quote or reference to the specific tool_result that supports/contradicts>",
  "explanation": "<1-2 sentence explanation citing the specific evidence>",
  "evidence_snippets": [{"source": "tool_result", "message_index": <int>, "text": "<relevant excerpt>"}]
}

- confidence: 0-100 how confident you are in the verdict. 90+ = strong evidence, 50-89 = moderate, <50 = weak.
- evidence_snippets: array of specific text excerpts from tool results or messages that support the verdict. Include the message_index and a short excerpt (max 200 chars). source is "tool_result" or "message".

Only output the JSON array, nothing else.
"""


def chunk_messages(
    messages: list[dict],
    window_size: int = 50,
    overlap: int = 5,
) -> list[list[dict]]:
    """Split messages into overlapping windows for processing."""
    if len(messages) <= window_size:
        return [messages]

    chunks: list[list[dict]] = []
    start = 0
    while start < len(messages):
        end = min(start + window_size, len(messages))
        chunks.append(messages[start:end])
        if end >= len(messages):
            break
        start = end - overlap

    return chunks


def build_judge_prompt(
    claims: list[Claim],
    evidence: list[Evidence],
    messages: list[dict],
) -> str:
    """Build the structured prompt for the judge LLM."""
    sections: list[str] = []

    # Build an index of evidence by message_index for efficient lookup
    evidence_by_index: dict[int, list[Evidence]] = {}
    for ev in evidence:
        evidence_by_index.setdefault(ev.message_index, []).append(ev)

    def _format_evidence(ev: Evidence) -> str:
        parts = [f"[msg {ev.message_index}] {ev.tool_name}"]
        if ev.file_path:
            parts.append(f"file={ev.file_path}")
        if ev.exit_code is not None:
            parts.append(f"exit_code={ev.exit_code}")
        parts.append(f"input: {ev.input_summary}")
        parts.append(f"output: {ev.output_summary}")
        return "- " + " | ".join(parts)

    # Claims section with linked evidence
    sections.append("## Claims to Verify\n")
    linked_evidence_indices: set[int] = set()
    for i, claim in enumerate(claims):
        sections.append(
            f"{i}. [msg {claim.message_index}] ({claim.category}, {claim.confidence}) "
            f"{claim.text}"
        )

        # Find relevant evidence: referenced by claim or within 3 messages
        relevant: list[Evidence] = []
        for ref_idx in claim.evidence_refs:
            relevant.extend(evidence_by_index.get(ref_idx, []))
        for offset in range(-3, 4):
            idx = claim.message_index + offset
            if idx not in claim.evidence_refs:
                relevant.extend(evidence_by_index.get(idx, []))

        # Deduplicate by identity
        seen_ev: set[int] = set()
        unique_relevant: list[Evidence] = []
        for ev in relevant:
            ev_id = id(ev)
            if ev_id not in seen_ev:
                seen_ev.add(ev_id)
                unique_relevant.append(ev)
                linked_evidence_indices.add(id(ev))

        if unique_relevant:
            sections.append("  Relevant Evidence:")
            for ev in unique_relevant:
                sections.append("  " + _format_evidence(ev))

    # Additional context: evidence not linked to any claim
    unlinked = [ev for ev in evidence if id(ev) not in linked_evidence_indices]
    if unlinked:
        sections.append("\n## Additional Context\n")
        for ev in unlinked:
            sections.append(_format_evidence(ev))

    # Relevant message context (abbreviated)
    claim_indices = {c.message_index for c in claims}
    evidence_indices = {e.message_index for e in evidence}
    relevant_indices = claim_indices | evidence_indices
    # Also include referenced evidence
    for c in claims:
        relevant_indices.update(c.evidence_refs)

    sections.append("\n## Message Context\n")
    for idx in sorted(relevant_indices):
        if idx < 0 or idx >= len(messages):
            continue
        msg = messages[idx]
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block[:300])
                elif isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        text_parts.append(block.get("text", "")[:300])
                    elif btype == "tool_use":
                        text_parts.append(f"[tool_use: {block.get('name', '')}]")
                    elif btype == "tool_result":
                        inner = block.get("content", "")
                        if isinstance(inner, str):
                            text_parts.append(f"[tool_result: {inner[:200]}]")
            content = "\n".join(text_parts)
        else:
            content = str(content)[:500]

        sections.append(f"[{idx}] {role}: {content}")

    sections.append(
        "\n## Instructions\n"
        "Evaluate each claim against the evidence and message context. "
        "Return a JSON array with one verdict object per claim."
    )

    return "\n".join(sections)


def _parse_judge_response(response: str, claims: list[Claim]) -> list[Finding]:
    """Parse the LLM judge response into Finding objects."""
    # Strip markdown code fences if present
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        verdicts = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse judge response as JSON")
        return []

    if not isinstance(verdicts, list):
        return []

    findings: list[Finding] = []
    for v in verdicts:
        claim_idx = v.get("claim_index", 0)
        if claim_idx < 0 or claim_idx >= len(claims):
            continue

        claim = claims[claim_idx]
        verdict = v.get("verdict", "unverified")
        if verdict not in ("verified", "unverified", "hallucination"):
            verdict = "unverified"

        # Category from LLM, severity auto-assigned from category
        category = v.get("category", "other")
        valid_categories = ("test_result", "file_existence", "command_output", "data_misread", "code_claim", "dependency", "other")
        if category not in valid_categories:
            category = "other"

        severity = SEVERITY_FROM_CATEGORY.get(category, "low")
        confidence = v.get("confidence", 50)
        if not isinstance(confidence, int):
            try:
                confidence = int(confidence)
            except (TypeError, ValueError):
                confidence = 50
        confidence = max(0, min(100, confidence))

        evidence_snippets = v.get("evidence_snippets", [])
        if not isinstance(evidence_snippets, list):
            evidence_snippets = []

        cwe_id = CWE_FROM_CATEGORY.get(category, "")

        findings.append(
            Finding(
                message_index=claim.message_index,
                claim=claim.text,
                verdict=verdict,
                severity=severity,
                evidence=v.get("evidence", ""),
                explanation=v.get("explanation", ""),
                category=category,
                confidence=confidence,
                evidence_snippets=evidence_snippets,
                cwe_id=cwe_id,
            )
        )

    return findings


def _compute_summary(findings: list[Finding]) -> AuditSummary:
    """Compute audit summary from findings."""
    total = len(findings)
    verified = sum(1 for f in findings if f.verdict == "verified")
    unverified = sum(1 for f in findings if f.verdict == "unverified")
    hallucinations = sum(1 for f in findings if f.verdict == "hallucination")
    critical = sum(1 for f in findings if f.severity == "critical" and f.verdict == "hallucination")
    high = sum(1 for f in findings if f.severity == "high" and f.verdict == "hallucination")
    low = sum(1 for f in findings if f.severity == "low" and f.verdict == "hallucination")

    trust_score = verified / total if total > 0 else 0.0

    return AuditSummary(
        total_claims=total,
        verified=verified,
        unverified=unverified,
        hallucinations=hallucinations,
        trust_score=round(trust_score, 3),
        major_findings=critical,  # backward compat
        moderate_findings=high,
        minor_findings=low,
        critical_count=critical,
        high_count=high,
        low_count=low,
    )


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Deduplicate findings by message_index and claim text."""
    seen: set[tuple[int, str]] = set()
    deduped: list[Finding] = []
    for f in findings:
        key = (f.message_index, f.claim)
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    return deduped


def _read_session_messages(sfs_dir: Path) -> list[dict]:
    """Read messages from a session's messages.jsonl."""
    messages_path = sfs_dir / "messages.jsonl"
    if not messages_path.exists():
        return []
    messages: list[dict] = []
    with open(messages_path) as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    return messages


async def judge_session(
    session_id: str,
    sfs_dir: Path,
    model: str = "claude-sonnet-4",
    api_key: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
) -> JudgeReport:
    """Run the full judge pipeline on a session.

    1. Read messages from the session directory
    2. Extract verifiable claims from assistant messages
    3. Gather evidence from tool calls
    4. Chunk messages if needed and send to LLM judge
    5. Parse verdicts, deduplicate, compute summary
    6. Save and return the report
    """
    if api_key is None:
        raise ValueError("An LLM API key is required to run the judge")

    messages = _read_session_messages(sfs_dir)
    if not messages:
        raise ValueError(f"No messages found in session {session_id}")

    # Check for zero tool calls and prepare warning
    _warnings: list[str] = []
    has_tool_calls = any(
        isinstance(b, dict) and b.get("type") == "tool_use"
        for msg in messages
        for b in (msg.get("content", []) if isinstance(msg.get("content"), list) else [])
    )
    if not has_tool_calls:
        _warnings.append(
            "This session contains no tool call data. The audit may produce limited results "
            "because there is no tool output evidence to verify claims against. "
            "Tools that don't expose tool calls: Gemini CLI, Amp."
        )

    # Extract claims and evidence from the full message list
    all_claims = extract_claims(messages)
    all_evidence = gather_evidence(messages)

    if not all_claims:
        # No verifiable claims — return an empty report
        report = JudgeReport(
            session_id=session_id,
            model=model,
            timestamp=datetime.now(timezone.utc).isoformat(),
            findings=[],
            summary=AuditSummary(
                total_claims=0, verified=0, unverified=0, hallucinations=0,
                trust_score=1.0, major_findings=0, moderate_findings=0,
                minor_findings=0,
            ),
            warnings=_warnings,
        )
        save_report(report, sfs_dir)
        return report

    # Process in chunks
    chunks = chunk_messages(messages)
    all_findings: list[Finding] = []

    for chunk in chunks:
        # Determine which claims and evidence fall within this chunk's range
        chunk_start = messages.index(chunk[0]) if chunk else 0
        chunk_end = chunk_start + len(chunk)

        chunk_claims = [
            c for c in all_claims
            if chunk_start <= c.message_index < chunk_end
        ]
        chunk_evidence = [
            e for e in all_evidence
            if chunk_start <= e.message_index < chunk_end
        ]

        if not chunk_claims:
            continue

        prompt = build_judge_prompt(chunk_claims, chunk_evidence, messages)
        response = await call_llm(
            model=model,
            system=JUDGE_SYSTEM_PROMPT,
            prompt=prompt,
            api_key=api_key,
            provider=provider,
            base_url=base_url,
        )

        findings = _parse_judge_response(response, chunk_claims)
        all_findings.extend(findings)

    # Deduplicate findings from overlapping windows
    all_findings = _deduplicate_findings(all_findings)

    summary = _compute_summary(all_findings)

    report = JudgeReport(
        session_id=session_id,
        model=model,
        timestamp=datetime.now(timezone.utc).isoformat(),
        findings=all_findings,
        summary=summary,
        warnings=_warnings,
    )

    save_report(report, sfs_dir)
    return report


async def judge_with_consensus(
    session_id: str,
    sfs_dir: Path,
    model: str = "claude-sonnet-4",
    api_key: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    passes: int = 3,
    threshold: int = 2,
) -> JudgeReport:
    """Run the judge multiple times and take consensus.

    Only reports findings where at least `threshold` out of `passes`
    agree on the verdict. Eliminates flaky single-run variance.
    Costs `passes`x more than a single run.
    """
    reports: list[JudgeReport] = []
    for i in range(passes):
        logger.info("Consensus pass %d/%d", i + 1, passes)
        report = await judge_session(session_id, sfs_dir, model, api_key, provider, base_url)
        reports.append(report)

    # Merge: group findings by (message_index, claim), take majority verdict
    from collections import Counter

    finding_votes: dict[tuple[int, str], list[Finding]] = {}
    for report in reports:
        for f in report.findings:
            key = (f.message_index, f.claim)
            finding_votes.setdefault(key, []).append(f)

    consensus_findings: list[Finding] = []
    for key, findings in finding_votes.items():
        verdicts = Counter(f.verdict for f in findings)
        top_verdict, top_count = verdicts.most_common(1)[0]

        if top_count < threshold:
            # No consensus — default to least severe: unverified
            top_verdict = "unverified"

        severities = Counter(f.severity for f in findings)
        top_severity = severities.most_common(1)[0][0]

        # Use the finding with the majority verdict for evidence/explanation
        representative = next(f for f in findings if f.verdict == top_verdict)
        consensus_findings.append(
            Finding(
                message_index=representative.message_index,
                claim=representative.claim,
                verdict=top_verdict,
                severity=top_severity,
                evidence=representative.evidence,
                explanation=representative.explanation,
            )
        )

    summary = _compute_summary(consensus_findings)

    report = JudgeReport(
        session_id=session_id,
        model=f"{model} (consensus {passes}x)",
        timestamp=datetime.now(timezone.utc).isoformat(),
        findings=consensus_findings,
        summary=summary,
    )

    save_report(report, sfs_dir)
    return report
