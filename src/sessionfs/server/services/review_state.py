"""v0.10.11 — compute structured ReviewState from a ticket's comment history.

Review tickets (the ones an agent files for codex-reviewer to chew on)
accumulate long comment threads as findings get raised, fixed, and
re-verified. Re-reading the raw comments to know "what's still open"
is expensive and burns LLM context.

This module parses the structured comment shapes the review loop has
been using consistently for many releases:

  codex-reviewer:
    Codex R{N} review on tk_X: VERDICT
    Findings:
     • SEVERITY — descriptive text...
     • SEVERITY — descriptive text...
    Verified clean / no change needed:
     • text...

  atlas (implementer closure):
    R{N} closure — ... fixed in {sha} ...
    R{N} {severity-or-finding} closure — fixed in {sha} ...

It returns a `ReviewState` with:
  - open_findings  (raised in some round, no later verdict cleared them)
  - closed_findings  (cleared by a subsequent VERIFIED-CLEAN round)
  - last_verdict  (most recent Codex round's verdict)
  - severity_counts  (over OPEN findings only — what still needs work)
  - last_review_comment_id  (most recent codex-reviewer comment)
  - last_implementer_comment_id  (most recent non-codex comment, typically atlas)
  - updated_at  (most recent comment timestamp on the thread)
  - rounds  (chronological list — useful for renderers)

Closure rule (intentionally simple): findings raised in round N are
closed when ANY subsequent Codex round on the same ticket has verdict
VERIFIED-CLEAN. This matches the actual workflow — Codex re-reviews
after the implementer's closure comment, and a clean verdict means
every prior round's findings are resolved. If the loop never hits a
VERIFIED-CLEAN, the findings stay open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional


# Codex's review header pattern. Matches:
#   "Codex R3 review on tk_abc: VERIFIED-CLEAN"
#   "Codex R1 doc review on tk_xyz: CHANGES REQUESTED"
#   "Codex R2 review on tk_q: CHANGES_REQUESTED"  (underscore variant)
_HEADER_RE = re.compile(
    r"^Codex\s+R(?P<round>\d+)\s+(?:doc\s+)?review\s+on\s+tk_\w+\s*:\s*"
    r"(?P<verdict>VERIFIED[-_]CLEAN|CHANGES[\s_-]+REQUESTED|"
    r"NO[\s-]+CHANGES[\s-]+NEEDED|APPROVED)",
    re.IGNORECASE | re.MULTILINE,
)

# Finding bullet pattern. Matches "  • SEVERITY — text..." or " - SEVERITY — text"
# Codex sometimes uses an em-dash (—), sometimes a hyphen — accept either.
_FINDING_RE = re.compile(
    r"^[\s]*[•\-\*]\s+(?P<severity>CRITICAL|HIGH|MEDIUM|LOW)\s*[—\-:]\s*"
    r"(?P<text>.+?)$",
    re.MULTILINE,
)

# Section header that ends the Findings list. Codex consistently
# follows Findings with "Verified clean / no change needed:" or
# "No change needed" or similar. We use these to STOP collecting
# findings (otherwise we'd pick up the false-positive bullets in the
# verified-clean section).
_FINDINGS_END_RE = re.compile(
    r"(Verified\s+clean|No\s+change\s+needed|Verification\s+(run|commands)|"
    r"Out\s+of\s+scope|Residual\s+risk|Cross-checks)",
    re.IGNORECASE,
)


VERDICT_CLEAN = "VERIFIED-CLEAN"
VERDICT_CHANGES = "CHANGES_REQUESTED"


@dataclass(frozen=True)
class ReviewFinding:
    """One finding raised by Codex in some round."""

    severity: str  # CRITICAL / HIGH / MEDIUM / LOW
    text: str  # the bullet body (trimmed, first line)
    round: int  # which review round raised it
    raised_at: datetime  # timestamp of the raising comment
    raised_comment_id: str
    closed_round: Optional[int] = None  # round that cleared it (None = open)

    @property
    def status(self) -> str:
        return "closed" if self.closed_round is not None else "open"

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "text": self.text,
            "round": self.round,
            "raised_at": self.raised_at.isoformat(),
            "raised_comment_id": self.raised_comment_id,
            "status": self.status,
            "closed_round": self.closed_round,
        }


@dataclass(frozen=True)
class ReviewRound:
    round: int
    verdict: str  # VERIFIED-CLEAN / CHANGES_REQUESTED / ...
    comment_id: str
    timestamp: datetime
    findings_raised: int

    def to_dict(self) -> dict:
        return {
            "round": self.round,
            "verdict": self.verdict,
            "comment_id": self.comment_id,
            "timestamp": self.timestamp.isoformat(),
            "findings_raised": self.findings_raised,
        }


@dataclass
class ReviewState:
    """Structured summary of a review-ticket comment thread.

    `None`-ish if the ticket has no codex-reviewer comments — callers
    should expose ReviewState only when at least one Codex round has
    happened. See `compute_review_state` for that guard."""

    open_findings: list[ReviewFinding] = field(default_factory=list)
    closed_findings: list[ReviewFinding] = field(default_factory=list)
    last_verdict: Optional[str] = None
    severity_counts: dict[str, int] = field(default_factory=dict)
    last_review_comment_id: Optional[str] = None
    last_implementer_comment_id: Optional[str] = None
    updated_at: Optional[datetime] = None
    rounds: list[ReviewRound] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "open_findings": [f.to_dict() for f in self.open_findings],
            "closed_findings": [f.to_dict() for f in self.closed_findings],
            "last_verdict": self.last_verdict,
            "severity_counts": dict(self.severity_counts),
            "last_review_comment_id": self.last_review_comment_id,
            "last_implementer_comment_id": self.last_implementer_comment_id,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "rounds": [r.to_dict() for r in self.rounds],
        }


def _normalize_verdict(raw: str) -> str:
    """Map verdict variants to a canonical pair."""
    cleaned = re.sub(r"[\s_-]+", "_", raw.strip().upper())
    if cleaned in {"VERIFIED_CLEAN", "NO_CHANGES_NEEDED", "APPROVED"}:
        return VERDICT_CLEAN
    return VERDICT_CHANGES


def _parse_codex_comment(
    content: str, comment_id: str, created_at: datetime
) -> tuple[Optional[ReviewRound], list[ReviewFinding]]:
    """Parse one codex-reviewer comment into (round_metadata, findings).

    Returns (None, []) if the comment doesn't match the expected header
    pattern — non-conforming codex comments contribute nothing to state.
    """
    header_match = _HEADER_RE.search(content)
    if not header_match:
        return None, []

    round_num = int(header_match.group("round"))
    verdict = _normalize_verdict(header_match.group("verdict"))

    # Locate the Findings section. Bound the slice from end of header
    # through whichever section header comes next.
    findings_start = header_match.end()
    end_match = _FINDINGS_END_RE.search(content, pos=findings_start)
    findings_end = end_match.start() if end_match else len(content)

    # CHANGES_REQUESTED rounds have Findings: bullets; VERIFIED-CLEAN
    # rounds typically have "Findings: none." — no bullets to extract.
    findings_block = content[findings_start:findings_end]

    findings: list[ReviewFinding] = []
    for m in _FINDING_RE.finditer(findings_block):
        text = m.group("text").strip()
        # Strip a trailing colon if Codex left one before the file:line:
        # which would otherwise be a hint that there's more on the next
        # line — we keep just the first line for compactness.
        text = text.split("\n", 1)[0].strip()
        findings.append(
            ReviewFinding(
                severity=m.group("severity").upper(),
                text=text,
                round=round_num,
                raised_at=created_at,
                raised_comment_id=comment_id,
            )
        )

    round_meta = ReviewRound(
        round=round_num,
        verdict=verdict,
        comment_id=comment_id,
        timestamp=created_at,
        findings_raised=len(findings),
    )
    return round_meta, findings


def compute_review_state(
    comments: Iterable[dict],
    codex_persona: str = "codex-reviewer",
) -> Optional[ReviewState]:
    """Derive a ReviewState from a chronological list of ticket comments.

    Each `comment` dict must have:
      - id: str
      - author_persona: str | None
      - content: str
      - created_at: datetime  (or ISO-8601 string)

    Returns `None` if no codex-reviewer comments are present — review
    state only applies to review tickets, and the absence of any Codex
    round means there's nothing structured to report.
    """
    sorted_comments = sorted(
        list(comments),
        key=lambda c: _coerce_dt(c.get("created_at")) or datetime.min,
    )

    rounds: list[ReviewRound] = []
    all_findings: list[ReviewFinding] = []
    last_review_id: Optional[str] = None
    last_implementer_id: Optional[str] = None
    latest_ts: Optional[datetime] = None

    for c in sorted_comments:
        cid = c.get("id")
        if not isinstance(cid, str):
            continue
        author = c.get("author_persona")
        content = c.get("content") or ""
        created_at = _coerce_dt(c.get("created_at"))
        if created_at is None:
            continue
        latest_ts = created_at if latest_ts is None or created_at > latest_ts else latest_ts

        if author == codex_persona:
            last_review_id = cid
            round_meta, findings = _parse_codex_comment(content, cid, created_at)
            if round_meta is not None:
                rounds.append(round_meta)
                all_findings.extend(findings)
        else:
            last_implementer_id = cid

    if not rounds:
        return None

    # Closure rule: every finding raised in round N is closed if ANY
    # later round's verdict is VERIFIED-CLEAN. Annotate findings with
    # the closing round so renderers can show "closed in R{N}".
    rounds_sorted = sorted(rounds, key=lambda r: r.round)
    clean_rounds = [r for r in rounds_sorted if r.verdict == VERDICT_CLEAN]
    annotated: list[ReviewFinding] = []
    for f in all_findings:
        closing = next(
            (r.round for r in clean_rounds if r.round > f.round), None
        )
        if closing is not None:
            annotated.append(
                ReviewFinding(
                    severity=f.severity,
                    text=f.text,
                    round=f.round,
                    raised_at=f.raised_at,
                    raised_comment_id=f.raised_comment_id,
                    closed_round=closing,
                )
            )
        else:
            annotated.append(f)

    open_findings = [f for f in annotated if f.closed_round is None]
    closed_findings = [f for f in annotated if f.closed_round is not None]

    severity_counts = {
        sev: sum(1 for f in open_findings if f.severity == sev)
        for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    }

    # Last verdict is the verdict of the latest (highest-round) Codex comment.
    last_verdict = rounds_sorted[-1].verdict

    return ReviewState(
        open_findings=open_findings,
        closed_findings=closed_findings,
        last_verdict=last_verdict,
        severity_counts=severity_counts,
        last_review_comment_id=last_review_id,
        last_implementer_comment_id=last_implementer_id,
        updated_at=latest_ts,
        rounds=rounds_sorted,
    )


def _coerce_dt(v) -> Optional[datetime]:
    """Accept datetime or ISO-8601 string; tolerate trailing Z."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.rstrip("Z")
        # datetime.fromisoformat handles "2026-05-19T03:14:12.285338+00:00"
        # but not bare "Z" until 3.11. Strip Z and assume UTC.
        try:
            dt = datetime.fromisoformat(s)
            return dt
        except ValueError:
            return None
    return None
