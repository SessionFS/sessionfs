"""v0.10.11 — compute structured ReviewState from a ticket's comment history.

Review tickets (the ones an agent files for codex-reviewer to chew on)
accumulate long comment threads as findings get raised, fixed, and
re-verified. Re-reading the raw comments to know "what's still open"
is expensive and burns LLM context.

This module parses the structured comment shapes the review loop has
been using consistently for many releases:

  reviewer (any tool — Codex, Claude, Gemini, …):
    <Reviewer> R{N} review on tk_X: VERDICT
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
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional


# One shared reviewer-name token — the review header is tool-agnostic, so a
# reviewer in any tool writes its own single-word name (Codex, Claude, Gemini,
# Reviewer, …). Trust is NOT derived from this name: verdict authority comes
# from the server-stamped ``verdict_trusted`` flag and the trusted-reviewer
# registry. The name is captured (group ``reviewer``) only so callers can see
# which tool posted a verdict.
REVIEWER_NAME_PATTERN = r"(?P<reviewer>[A-Za-z][A-Za-z0-9._-]{0,31})"

# Review header pattern. Matches (any reviewer name, case-insensitive):
#   "Codex R3 review on tk_abc: VERIFIED-CLEAN"
#   "Claude R1 doc review on tk_xyz: CHANGES REQUESTED"
#   "Gemini R2 review on tk_q: CHANGES_REQUESTED"  (underscore variant)
_HEADER_RE = re.compile(
    rf"^{REVIEWER_NAME_PATTERN}\s+R(?P<round>\d+)\s+(?:doc\s+)?review\s+on\s+tk_\w+\s*:\s*"
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

# Harm tag embedded in a finding's text: "harm: crash", "harm: data-exposed".
# Review Loop B2 — a finding's harm decides whether an open finding blocks
# merge readiness. `none` means "checked, harmless"; every other value is
# harmful. Accepted tokens match agent_runs.findings' validated `harm` key.
_HARM_RE = re.compile(
    r"\bharm\s*:\s*(data[-_]exposed|money[-_]spent|wrong[-_]actor|crash|other|none)\b",
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
    # Review Loop B2 — harm tag parsed from the finding text
    # (data-exposed|money-spent|wrong-actor|crash|other|none). None when the
    # reviewer did not tag a harm; merge readiness treats an untagged finding
    # of MEDIUM or higher as harmful (fail closed).
    harm: Optional[str] = None

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
            "harm": self.harm,
        }


@dataclass(frozen=True)
class ReviewRound:
    round: int
    verdict: str  # VERIFIED-CLEAN / CHANGES_REQUESTED / ... (alias-folded)
    comment_id: str
    timestamp: datetime
    findings_raised: int
    # WQ-P4 (Sentinel R2 LOW) — the STRICT seam. `verdict` above is the
    # alias-FOLDED canonical pair (APPROVED / NO CHANGES NEEDED → VERIFIED-
    # CLEAN) used for human display. `raw_verdict` preserves the literal
    # phrase Codex actually wrote (e.g. "APPROVED", "VERIFIED-CLEAN") so the
    # autonomous stop oracle can demand the EXACT canonical phrase before
    # auto-closing a ticket unattended. See `is_strict_verified_clean`.
    raw_verdict: str = ""

    @property
    def is_strict_verified_clean(self) -> bool:
        """True ONLY for the literal canonical `VERIFIED-CLEAN` phrase.

        Alias-folded rounds (`APPROVED`, `NO CHANGES NEEDED`) report
        `verdict == VERDICT_CLEAN` for display but are NOT strict — they
        return False here. The work-queue stop oracle uses this; the
        human-facing renderer uses `verdict`.
        """
        return is_strict_verified_clean(self.raw_verdict)

    def to_dict(self) -> dict:
        return {
            "round": self.round,
            "verdict": self.verdict,
            "raw_verdict": self.raw_verdict,
            "is_strict_verified_clean": self.is_strict_verified_clean,
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
    # WQ-P4 (Sentinel R2 LOW) — STRICT seam, additive. `last_verdict` above
    # stays ALIAS-FOLDED for human display (the renderer at
    # routes/tickets.py:get_ticket_review_state shows it unchanged).
    # `last_raw_verdict` is the literal phrase of the latest trusted round,
    # and `last_verdict_is_strict_verified_clean` is True ONLY when that
    # latest round wrote the exact canonical `VERIFIED-CLEAN` (NOT an alias).
    # The autonomous work-queue stop oracle reads the strict signal; nothing
    # in the human-display path consults it.
    last_raw_verdict: Optional[str] = None
    last_verdict_is_strict_verified_clean: bool = False
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
            "last_raw_verdict": self.last_raw_verdict,
            "last_verdict_is_strict_verified_clean": (
                self.last_verdict_is_strict_verified_clean
            ),
            "severity_counts": dict(self.severity_counts),
            "last_review_comment_id": self.last_review_comment_id,
            "last_implementer_comment_id": self.last_implementer_comment_id,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "rounds": [r.to_dict() for r in self.rounds],
        }


def _normalize_verdict(raw: str) -> str:
    """Map verdict variants to a canonical pair (alias-folding).

    This is the HUMAN-DISPLAY normalization — `APPROVED` and
    `NO CHANGES NEEDED` fold into `VERIFIED-CLEAN`. It is intentionally
    loose. For the autonomous auto-close gate use `_is_strict_verified_clean`
    on the RAW phrase instead (WQ-P4 / §5.0 strict-only stop oracle).
    """
    cleaned = re.sub(r"[\s_-]+", "_", raw.strip().upper())
    if cleaned in {"VERIFIED_CLEAN", "NO_CHANGES_NEEDED", "APPROVED"}:
        return VERDICT_CLEAN
    return VERDICT_CHANGES


def verdict_from_header(content: str) -> Optional[str]:
    """Return the canonical verdict when ``content`` carries a recognized
    review verdict header, else ``None``.

    Mirrors ``_parse_codex_comment``'s header gate so the notification layer
    can decide whether a trusted reviewer's comment is actually a verdict
    (``<Reviewer> R{N} review on tk_...: VERDICT``) rather than free-form
    prose.
    """
    match = _HEADER_RE.search(content or "")
    if not match:
        return None
    return _normalize_verdict(match.group("verdict"))


def is_strict_verified_clean(raw: str) -> bool:
    """True ONLY for the literal canonical `VERIFIED-CLEAN` phrase.

    WQ-P4 / design §5.0 (Sentinel #5): an UNATTENDED auto-stop demands the
    exact verdict the team uses for "done". The display aliases
    (`APPROVED`, `NO CHANGES NEEDED`) fold to VERIFIED-CLEAN for rendering
    but are NOT sufficient to end a loop without a human in the seat. This
    accepts only `VERIFIED-CLEAN` / `VERIFIED_CLEAN` (case-insensitive,
    hyphen-or-underscore) — nothing else.

    Public seam for Review Loop B2 merge-readiness (and the work-queue
    oracle); `ReviewRound.is_strict_verified_clean` delegates here.
    """
    return _is_strict_verified_clean(raw)


def _is_strict_verified_clean(raw: str) -> bool:
    """True ONLY for the literal canonical `VERIFIED-CLEAN` phrase."""
    cleaned = re.sub(r"[\s_-]+", "_", raw.strip().upper())
    return cleaned == "VERIFIED_CLEAN"


def _parse_harm(text: str) -> Optional[str]:
    """Extract a normalized harm tag from a finding bullet's text, else None.

    Review Loop B2. Normalizes `data_exposed`/`data-exposed` etc. to the
    canonical hyphen form; returns None when the reviewer wrote no harm tag.
    """
    m = _HARM_RE.search(text or "")
    if not m:
        return None
    return m.group(1).lower().replace("_", "-")


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
    raw_verdict = header_match.group("verdict").strip()
    verdict = _normalize_verdict(raw_verdict)

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
                harm=_parse_harm(text),
            )
        )

    round_meta = ReviewRound(
        round=round_num,
        verdict=verdict,
        raw_verdict=raw_verdict,
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
      - verdict_trusted: bool  (server-stamped at write — the authority
        signal; see tk_d42170b4670f4448 /
        docs/security/review-verdict-provenance.md)

    Returns `None` if no TRUSTED review-verdict comments are present —
    review state only applies to review tickets, and the absence of any
    trusted round means there's nothing authoritative to report.

    Verdict authority is keyed off `verdict_trusted` (server-stamped from
    the trusted_reviewers registry), NOT off `author_persona`. A forged
    `author_persona="codex-reviewer"` comment from an arbitrary
    tickets:write caller carries `verdict_trusted=false`, still renders in
    the thread, but contributes NOTHING to verdict/closure derivation.

    `codex_persona` is retained for back-compat / renderer labelling only;
    it no longer gates rounds or findings.

    Strict seam (WQ-P4, agent-work-queue stop oracle, docs/design/
    agent-work-queues.md §5.0/§6): autonomous closure must additionally
    require the STRICT literal `VERIFIED-CLEAN` verdict — aliases
    (`APPROVED`, `NO CHANGES NEEDED`) folded by `_normalize_verdict` are
    acceptable for human-facing display (`last_verdict`) but are NOT
    sufficient for unattended auto-close. This module exposes the strict
    signal ADDITIVELY: `ReviewState.last_verdict_is_strict_verified_clean`
    (and `ReviewState.last_raw_verdict` / `ReviewRound.raw_verdict` /
    `ReviewRound.is_strict_verified_clean`). `_normalize_verdict`'s display
    aliasing is intentionally left unchanged so `last_verdict` and the
    `get_ticket_review_state` renderer keep folding aliases; the consuming
    oracle (`services/work_queues.py`) reads the strict signal instead.
    """
    sorted_comments = sorted(
        list(comments),
        key=lambda c: _coerce_dt(c.get("created_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
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
        content = c.get("content") or ""
        created_at = _coerce_dt(c.get("created_at"))
        if created_at is None:
            continue
        latest_ts = created_at if latest_ts is None or created_at > latest_ts else latest_ts

        # tk_d42170b4670f4448 — verdict authority comes from the
        # server-stamped trust flag, NOT from author_persona. Untrusted
        # comments (including a forged "codex-reviewer" post from an
        # arbitrary tickets:write caller) still render in the thread but
        # never become a round / close findings / set last_verdict.
        if bool(c.get("verdict_trusted")):
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
    # LATER round's verdict is VERIFIED-CLEAN. "Later" means later in
    # (created_at, comment id) order — round numbers are labels only, and a
    # reviewer can post out-of-order (R9 clean, then R2 changes). Annotate
    # findings with the closing round so renderers can show "closed in R{N}".
    rounds_sorted = sorted(rounds, key=lambda r: (r.timestamp, r.comment_id))
    clean_rounds = [r for r in rounds_sorted if r.verdict == VERDICT_CLEAN]
    annotated: list[ReviewFinding] = []
    for f in all_findings:
        raised_key = (f.raised_at, f.raised_comment_id)
        closing = next(
            (
                r.round
                for r in clean_rounds
                if (r.timestamp, r.comment_id) > raised_key
            ),
            None,
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
                    harm=f.harm,
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

    # Last verdict is the verdict of the chronologically latest Codex round
    # (by created_at, comment id) — not the highest round number.
    last_round = rounds_sorted[-1]
    last_verdict = last_round.verdict

    return ReviewState(
        open_findings=open_findings,
        closed_findings=closed_findings,
        last_verdict=last_verdict,
        # WQ-P4 strict seam — the literal phrase + strict flag of the latest
        # trusted round. The autonomous stop oracle (services/work_queues.py)
        # reads these; the human renderer reads last_verdict (alias-folded).
        last_raw_verdict=last_round.raw_verdict,
        last_verdict_is_strict_verified_clean=last_round.is_strict_verified_clean,
        severity_counts=severity_counts,
        last_review_comment_id=last_review_id,
        last_implementer_comment_id=last_implementer_id,
        updated_at=latest_ts,
        rounds=rounds_sorted,
    )


def _coerce_dt(v) -> Optional[datetime]:
    """Accept datetime or ISO-8601 string; tolerate trailing Z.

    Always returns a tz-AWARE datetime (naive inputs are assumed UTC). The
    server stores UTC; SQLite round-trips can yield naive datetimes while
    other rows carry tzinfo, so normalizing here keeps the sort/compare paths
    from raising "can't compare offset-naive and offset-aware datetimes"
    (WQ-P4 — the stop oracle re-derives over DB-sourced comments).
    """
    if v is None:
        return None
    dt: datetime | None = None
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str):
        s = v.rstrip("Z")
        # datetime.fromisoformat handles "2026-05-19T03:14:12.285338+00:00"
        # but not bare "Z" until 3.11. Strip Z and assume UTC.
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def finding_is_harmful(finding: ReviewFinding) -> bool:
    """Review Loop B2 — a finding is harmful unless it is explicitly tagged
    ``harm: none``. An untagged finding of MEDIUM or higher fails closed
    (harmful). Merge readiness and the loop status rollup share this predicate
    so "what is still harmful" never drifts between the two surfaces."""
    if finding.harm is not None:
        return finding.harm != "none"
    return finding.severity in ("MEDIUM", "HIGH", "CRITICAL")


def compute_merge_readiness(
    *,
    comments: Iterable[dict],
    sha: str,
    implementer_identities: Iterable[tuple[Optional[str], Optional[str]]],
    key_minters: Optional[Mapping[str, Optional[frozenset[str]]]] = None,
) -> dict:
    """Derive the merge-readiness dict from pre-fetched comment rows.

    Pure function — no DB access. Shared by the readiness route and the loop
    status rollup so the two can never drift. ``comments`` are the same dicts
    ``compute_review_state`` consumes (``id``, ``author_persona``, ``content``,
    ``created_at``, ``verdict_trusted``, ``service_key_id``, ``author_user_id``,
    ``reviewed_commit``). ``implementer_identities`` is an iterable of
    ``(service_key_id, user_id)`` tuples from every implementer source
    (``load_implementer_identities``); when none records anyone, readiness
    fails closed with "implementer unknown". ``key_minters`` maps a service
    key id to the user who minted it (``load_key_minters``): a service key is
    its minter, so a service-key reviewer and a user-only implementer record
    for that minter are the same identity, and an unresolvable minter fails
    closed.

    Returns ``{"ready", "reasons", "verdict"}``; ``verdict`` is ``None`` when
    there is no trusted strict clean verdict. See
    ``routes/tickets.py:get_ticket_merge_readiness`` for the caller contract.
    """
    state = compute_review_state(comments)
    if state is None or not state.last_verdict_is_strict_verified_clean:
        return {
            "ready": False,
            "reasons": ["no trusted strict VERIFIED-CLEAN verdict"],
            "verdict": None,
        }

    clean_comment_id: Optional[str] = None
    for rnd in reversed(state.rounds):
        if rnd.is_strict_verified_clean:
            clean_comment_id = rnd.comment_id
            break
    closing = next((c for c in comments if c.get("id") == clean_comment_id), None)
    if closing is None:
        return {
            "ready": False,
            "reasons": ["closing verdict comment not found"],
            "verdict": None,
        }

    closing_svc = closing.get("service_key_id") or None
    closing_usr = closing.get("author_user_id") or None
    reviewed_commit = closing.get("reviewed_commit")

    reasons: list[str] = []
    if not reviewed_commit or reviewed_commit != sha:
        reasons.append("trusted clean verdict is not bound to the requested sha")

    identities = [
        (svc or None, usr or None)
        for svc, usr in implementer_identities
        if svc or usr
    ]
    closing_minter = (key_minters or {}).get(closing_svc) if closing_svc else None
    if not identities:
        reasons.append("implementer unknown")
    for impl_svc, impl_usr in identities:
        if closing_svc and impl_svc:
            if closing_svc == impl_svc:
                self_close = True
            else:
                # Two different service keys: the same identity only when the
                # same user minted both. Fail closed (self-review) when either
                # key's minter can't be resolved.
                impl_minter = (key_minters or {}).get(impl_svc)
                self_close = (
                    closing_minter is None
                    or impl_minter is None
                    or bool(closing_minter & impl_minter)
                )
        elif closing_svc:
            # Service-key reviewer vs a user-only implementer record: the user
            # may be the key's minter (the same identity). Fail closed when
            # the minter can't be resolved.
            self_close = closing_minter is None or bool(
                impl_usr and impl_usr in closing_minter
            )
        elif impl_svc:
            # Human reviewer vs service-key implementer. The key IS its
            # minter, so a human reviewing work their own key did is
            # self-review. Compare against the recorded backing user and the
            # key's minter; fail closed when the minter can't be resolved.
            impl_minter = (key_minters or {}).get(impl_svc)
            self_close = (
                impl_minter is None
                or bool(closing_usr and closing_usr in impl_minter)
                or bool(closing_usr and impl_usr and closing_usr == impl_usr)
            )
        else:
            self_close = bool(closing_usr and closing_usr == impl_usr)
        if self_close:
            reasons.append("reviewer identity matches an implementer identity")
            break

    harmful = [f for f in state.open_findings if finding_is_harmful(f)]
    if harmful:
        reasons.append(f"{len(harmful)} open harmful finding(s)")

    closing_at = _coerce_dt(closing.get("created_at"))
    return {
        "ready": not reasons,
        "reasons": reasons,
        "verdict": {
            "reviewer": closing_svc or closing_usr or "",
            "reviewed_commit": reviewed_commit,
            "at": closing_at.isoformat() if closing_at is not None else None,
        },
    }


async def load_implementer_identities(
    db: Any, tickets: Iterable[Any]
) -> dict[str, list[tuple[Optional[str], Optional[str]]]]:
    """Every recorded implementer ``(service_key_id, user_id)`` per ticket.

    Four sources: work-queue items, implementer-role agent runs, the start
    context of the ticket's CURRENT lease (written by start_ticket), and the
    resolver. Batched: one query per source for all tickets.
    """
    from sqlalchemy import select

    from sessionfs.server.db.models import (
        AgentRun,
        RetrievalAuditContext,
        WorkQueueItem,
    )

    by_id = {t.id: t for t in tickets}
    out: dict[str, list[tuple[Optional[str], Optional[str]]]] = {
        tid: [] for tid in by_id
    }
    if not by_id:
        return out
    ids = list(by_id)
    for tid, svc, usr in (
        await db.execute(
            select(
                WorkQueueItem.ticket_id,
                WorkQueueItem.implementer_service_key_id,
                WorkQueueItem.implementer_user_id,
            ).where(WorkQueueItem.ticket_id.in_(ids))
        )
    ).all():
        out[tid].append((svc, usr))
    for tid, svc, usr in (
        await db.execute(
            select(
                AgentRun.ticket_id,
                AgentRun.service_key_id,
                AgentRun.triggered_by_user_id,
            ).where(
                AgentRun.ticket_id.in_(ids),
                AgentRun.role.in_(("implementer", "ui-implementer")),
            )
        )
    ).all():
        out[tid].append((svc, usr))
    for tid, epoch, svc, usr in (
        await db.execute(
            select(
                RetrievalAuditContext.ticket_id,
                RetrievalAuditContext.lease_epoch,
                RetrievalAuditContext.service_key_id,
                RetrievalAuditContext.created_by_user_id,
            ).where(RetrievalAuditContext.ticket_id.in_(ids))
        )
    ).all():
        if epoch == by_id[tid].lease_epoch:
            out[tid].append((svc, usr))
    for tid, ticket in by_id.items():
        out[tid].append((None, ticket.resolver_user_id))
    return out


_MAX_ROTATION_DEPTH = 50


async def load_key_minters(
    db: Any,
    comments: Iterable[dict],
    implementer_identities: Iterable[tuple[Optional[str], Optional[str]]] = (),
) -> dict[str, Optional[frozenset[str]]]:
    """The identity set of every relevant service key.

    A key's identity is EVERY user who minted or rotated it: its ``user_id``,
    its ``created_by_user_id``, and the same for each key it was rotated from
    (``rotated_from_key_id`` chain). An admin who rotates a member's key is
    therefore part of that key's identity. Keys rotated before the link
    existed carry no ``rotated_from_key_id`` and resolve to
    ``{user_id, created_by_user_id}``. A missing link row, a cycle, an
    over-long chain or a key without a ``user_id`` maps to ``None`` (an
    unresolvable identity, which every caller treats as not independent).

    Covers both reviewer keys (every service key that authored a trusted
    comment) and implementer keys (every service_key_id in
    ``implementer_identities``).
    """
    from sqlalchemy import select

    from sessionfs.server.db.models import ApiKey

    key_ids = {
        c["service_key_id"]
        for c in comments
        if c.get("verdict_trusted") and c.get("service_key_id")
    }
    key_ids.update(svc for svc, _usr in implementer_identities if svc)
    if not key_ids:
        return {}

    rows: dict[str, tuple[Optional[str], Optional[str], Optional[str]]] = {}
    queried: set[str] = set()
    pending = set(key_ids)
    for _ in range(_MAX_ROTATION_DEPTH + 1):
        pending -= queried
        if not pending:
            break
        queried |= pending
        fetched = (
            await db.execute(
                select(
                    ApiKey.id,
                    ApiKey.user_id,
                    ApiKey.created_by_user_id,
                    ApiKey.rotated_from_key_id,
                ).where(ApiKey.id.in_(pending))
            )
        ).all()
        for kid, uid, cby, parent in fetched:
            rows[kid] = (uid, cby, parent)
        pending = {parent for _u, _c, parent in rows.values() if parent}

    result: dict[str, Optional[frozenset[str]]] = {}
    for kid in key_ids:
        users: set[str] = set()
        seen: set[str] = set()
        cur: Optional[str] = kid
        ok = True
        while cur:
            if cur in seen or len(seen) > _MAX_ROTATION_DEPTH or cur not in rows:
                ok = False
                break
            seen.add(cur)
            uid, cby, parent = rows[cur]
            if not uid:
                ok = False
                break
            users.add(uid)
            if cby:
                users.add(cby)
            cur = parent
        result[kid] = frozenset(users) if ok and users else None
    return result
