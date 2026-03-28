"""Build PR comment markdown from matched sessions."""

from __future__ import annotations


def build_pr_comment(
    sessions: list[dict],
    include_trust: bool = True,
    include_links: bool = True,
) -> str:
    """Build the markdown comment for a PR."""
    if len(sessions) == 1:
        return _build_single_session(sessions[0], include_trust, include_links)
    return _build_multi_session(sessions, include_trust, include_links)


def _build_single_session(
    s: dict, include_trust: bool, include_links: bool,
) -> str:
    lines = ["### AI Context (via SessionFS)\n"]
    lines.append(
        "This PR was built with AI assistance. "
        "View the reasoning behind the changes:\n"
    )

    # Table header
    cols = ["Session", "Tool", "Messages"]
    if include_trust and s.get("trust_score") is not None:
        cols.append("Trust Score")
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"

    # Row
    title = s.get("title", "Untitled")
    session_id = s["session_id"]
    if include_links:
        title_cell = f"[{title}](https://app.sessionfs.dev/sessions/{session_id})"
    else:
        title_cell = title

    tool = s.get("source_tool", "unknown")
    model = s.get("model_id", "")
    if model:
        tool = f"{tool} ({model})"
    msgs = str(s.get("message_count", 0))

    row_vals = [title_cell, tool, msgs]
    if include_trust and s.get("trust_score") is not None:
        score = int(s["trust_score"] * 100)
        emoji = "pass" if score >= 90 else "warn" if score >= 70 else "fail"
        row_vals.append(f"{score}% {emoji}")

    row = "| " + " | ".join(row_vals) + " |"

    lines.extend([header, sep, row, ""])
    # Add contradictions table if audit data available
    contradictions = s.get("contradictions", [])
    if contradictions:
        lines.append(f"\n**{len(contradictions)} contradictions found:**\n")
        lines.append("| Severity | Claim | Reality |")
        lines.append("| --- | --- | --- |")
        for c in contradictions[:5]:
            sev = c.get("severity", "low").upper()
            claim = c.get("claim", "")[:80]
            evidence = c.get("evidence", "")[:80]
            lines.append(f"| {sev} | {claim} | {evidence} |")
        if len(contradictions) > 5:
            lines.append(f"\n*...and {len(contradictions) - 5} more. See full audit.*")
        lines.append("")
    elif s.get("trust_score") is not None:
        score = int(s["trust_score"] * 100)
        total = s.get("total_claims", 0)
        if total > 0:
            lines.append(f"\nTrust: {score}% — 0 contradictions found across {total} claims.\n")

    lines.append("---")
    lines.append(
        '<sub>Added by <a href="https://sessionfs.dev">SessionFS</a> '
        '· <a href="https://sessionfs.dev/docs/github-app">What is this?</a> '
        "· This comment updates automatically</sub>"
    )

    return "\n".join(lines)


def _build_multi_session(
    sessions: list[dict], include_trust: bool, include_links: bool,
) -> str:
    lines = ["### AI Context (via SessionFS)\n"]
    lines.append(
        f"This PR was built with AI assistance across {len(sessions)} sessions:\n"
    )

    cols = ["Session", "Tool", "Messages"]
    if include_trust:
        cols.append("Trust Score")
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    lines.extend([header, sep])

    total_msgs = 0
    for s in sessions:
        title = s.get("title", "Untitled")
        session_id = s["session_id"]
        if include_links:
            title_cell = (
                f"[{title}](https://app.sessionfs.dev/sessions/{session_id})"
            )
        else:
            title_cell = title

        tool = s.get("source_tool", "unknown")
        msgs = s.get("message_count", 0)
        total_msgs += msgs

        row_vals = [title_cell, tool, str(msgs)]
        if include_trust:
            ts = s.get("trust_score")
            if ts is not None:
                score = int(ts * 100)
                emoji = "pass" if score >= 90 else "warn" if score >= 70 else "fail"
                row_vals.append(f"{score}% {emoji}")
            else:
                row_vals.append("\u2014")

        lines.append("| " + " | ".join(row_vals) + " |")

    lines.append(
        f"\nTotal: {total_msgs} messages across {len(sessions)} sessions."
    )
    lines.append("\n---")
    lines.append(
        '<sub>Added by <a href="https://sessionfs.dev">SessionFS</a> '
        '· <a href="https://sessionfs.dev/docs/github-app">What is this?</a></sub>'
    )

    return "\n".join(lines)
