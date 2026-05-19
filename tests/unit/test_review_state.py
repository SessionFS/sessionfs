"""v0.10.11 — unit tests for compute_review_state.

The parser is exercised against the actual comment shapes the review
loop has been emitting for many releases — header line, Findings
bullets, severity tagging, closure detection across rounds.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sessionfs.server.services.review_state import (
    VERDICT_CHANGES,
    VERDICT_CLEAN,
    compute_review_state,
)


def _comment(
    cid: str,
    author: str | None,
    content: str,
    at: datetime,
) -> dict:
    return {
        "id": cid,
        "author_persona": author,
        "content": content,
        "created_at": at,
    }


CODEX_R1_REAL = """\
Codex R1 review on tk_44bc8c8862304051: CHANGES REQUESTED

Findings:

 • LOW — src/sessionfs/mcp/server.py:307-310: add_knowledge still documents confidence as default: 1.0, but the current MCP/API path no longer behaves that way.

Verified clean / no change needed:

 • Boolean confidence rejection is correct.
 • Custom 404 strings are acceptable.
"""

CODEX_R2_CLEAN = """\
Codex R2 review on tk_44bc8c8862304051: VERIFIED-CLEAN

Rechecked after Atlas closure comment tc_xyz.

Findings: none.

Verified:

 • The R1 LOW finding is closed.
"""


def test_compute_review_state_returns_none_when_no_codex_comments():
    state = compute_review_state([
        _comment("c1", "atlas", "first impl", datetime(2026, 5, 1, tzinfo=timezone.utc)),
        _comment("c2", "atlas", "another note", datetime(2026, 5, 2, tzinfo=timezone.utc)),
    ])
    assert state is None


def test_compute_review_state_returns_none_for_empty_input():
    assert compute_review_state([]) is None


def test_single_codex_round_with_open_finding():
    t0 = datetime(2026, 5, 19, 2, 1, tzinfo=timezone.utc)
    state = compute_review_state([
        _comment("c_codex1", "codex-reviewer", CODEX_R1_REAL, t0),
    ])
    assert state is not None
    assert state.last_verdict == VERDICT_CHANGES
    assert len(state.open_findings) == 1
    assert len(state.closed_findings) == 0
    f = state.open_findings[0]
    assert f.severity == "LOW"
    assert f.round == 1
    assert "add_knowledge" in f.text
    assert state.severity_counts == {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 1}
    assert state.last_review_comment_id == "c_codex1"
    assert state.last_implementer_comment_id is None
    assert state.updated_at == t0
    assert len(state.rounds) == 1
    assert state.rounds[0].verdict == VERDICT_CHANGES
    assert state.rounds[0].findings_raised == 1


def test_round_two_clean_closes_round_one_findings():
    """The actual v0.10.11 workflow: R1 raises a LOW, atlas closes it,
    R2 returns VERIFIED-CLEAN. The R1 finding should move to
    closed_findings with closed_round=2; severity_counts (open) zero."""
    t0 = datetime(2026, 5, 19, 2, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=7)
    state = compute_review_state([
        _comment("c_codex1", "codex-reviewer", CODEX_R1_REAL, t0),
        _comment(
            "c_atlas1",
            "atlas",
            "R1 LOW closure — fixed in 4b80cf3 on develop.",
            t1,
        ),
        _comment("c_codex2", "codex-reviewer", CODEX_R2_CLEAN, t2),
    ])
    assert state is not None
    assert state.last_verdict == VERDICT_CLEAN
    assert len(state.open_findings) == 0
    assert len(state.closed_findings) == 1
    closed = state.closed_findings[0]
    assert closed.severity == "LOW"
    assert closed.closed_round == 2
    assert closed.round == 1
    assert state.severity_counts == {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    assert state.last_review_comment_id == "c_codex2"
    assert state.last_implementer_comment_id == "c_atlas1"
    assert state.updated_at == t2


def test_multiple_severities_in_one_round():
    content = """\
Codex R1 review on tk_x: CHANGES REQUESTED

Findings:

 • CRITICAL — boom
 • HIGH — explodes occasionally
 • MEDIUM — bug A
 • MEDIUM — bug B
 • LOW — typo

Verified clean:

 • everything else
"""
    state = compute_review_state([
        _comment("c1", "codex-reviewer", content, datetime(2026, 5, 1, tzinfo=timezone.utc)),
    ])
    assert state is not None
    assert state.severity_counts == {
        "CRITICAL": 1,
        "HIGH": 1,
        "MEDIUM": 2,
        "LOW": 1,
    }
    assert len(state.open_findings) == 5


def test_round_three_clean_closes_unresolved_round_one_findings():
    """If R1 had findings, R2 had MORE findings (still CHANGES_REQUESTED),
    and R3 is VERIFIED-CLEAN, ALL prior findings close in R3."""
    r1 = """\
Codex R1 review on tk_x: CHANGES REQUESTED

Findings:

 • HIGH — issue A
 • MEDIUM — issue B
"""
    r2 = """\
Codex R2 review on tk_x: CHANGES REQUESTED

Findings:

 • LOW — issue C
"""
    r3 = """\
Codex R3 review on tk_x: VERIFIED-CLEAN

Findings: none.
"""
    t = datetime(2026, 5, 1, tzinfo=timezone.utc)
    state = compute_review_state([
        _comment("c1", "codex-reviewer", r1, t),
        _comment("c2", "codex-reviewer", r2, t + timedelta(hours=1)),
        _comment("c3", "codex-reviewer", r3, t + timedelta(hours=2)),
    ])
    assert state is not None
    assert state.last_verdict == VERDICT_CLEAN
    assert len(state.open_findings) == 0
    assert len(state.closed_findings) == 3
    # R1's HIGH and MEDIUM close in R3, NOT R2 (R2 was still CHANGES_REQUESTED)
    r1_findings = [f for f in state.closed_findings if f.round == 1]
    assert all(f.closed_round == 3 for f in r1_findings)
    # R2's LOW also closes in R3
    r2_findings = [f for f in state.closed_findings if f.round == 2]
    assert all(f.closed_round == 3 for f in r2_findings)


def test_unresolved_findings_stay_open_when_no_subsequent_clean():
    """R1 CHANGES_REQUESTED with findings, R2 also CHANGES_REQUESTED
    (more findings), no R3 yet. ALL findings stay open."""
    r1 = """\
Codex R1 review on tk_x: CHANGES REQUESTED

Findings:

 • HIGH — A
"""
    r2 = """\
Codex R2 review on tk_x: CHANGES REQUESTED

Findings:

 • MEDIUM — B
"""
    state = compute_review_state([
        _comment("c1", "codex-reviewer", r1, datetime(2026, 5, 1, tzinfo=timezone.utc)),
        _comment("c2", "codex-reviewer", r2, datetime(2026, 5, 1, 1, tzinfo=timezone.utc)),
    ])
    assert state is not None
    assert state.last_verdict == VERDICT_CHANGES
    assert len(state.open_findings) == 2
    assert len(state.closed_findings) == 0
    assert state.severity_counts == {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 1, "LOW": 0}


def test_doc_review_variant_is_parsed():
    """Codex emits 'doc review on' for documentation tickets, not just 'review on'."""
    content = """\
Codex R1 doc review on tk_ee765a03b69045eb: CHANGES REQUESTED

Findings:

 • MEDIUM — docs/api-keys.md:165 overstates actor_type
"""
    state = compute_review_state([
        _comment("c1", "codex-reviewer", content, datetime(2026, 5, 1, tzinfo=timezone.utc)),
    ])
    assert state is not None
    assert len(state.open_findings) == 1
    assert state.open_findings[0].severity == "MEDIUM"


def test_non_codex_comments_only_set_last_implementer_id():
    """Tickets where atlas commented multiple times but no Codex round
    fired yet have no review state — but if Codex DOES eventually
    comment, the last_implementer_comment_id should be set correctly."""
    t = datetime(2026, 5, 1, tzinfo=timezone.utc)
    state = compute_review_state([
        _comment("a1", "atlas", "thinking...", t),
        _comment("a2", "atlas", "still thinking...", t + timedelta(minutes=1)),
        _comment("c1", "codex-reviewer", CODEX_R1_REAL, t + timedelta(minutes=5)),
        _comment("a3", "atlas", "fixed in abc123", t + timedelta(minutes=10)),
    ])
    assert state is not None
    # Last NON-codex comment wins, even though it came after the codex one.
    assert state.last_implementer_comment_id == "a3"
    assert state.last_review_comment_id == "c1"


def test_iso_string_timestamps_accepted():
    """The server hands datetime objects but the CLI parses JSON so it
    sees ISO strings. The compute helper must accept both."""
    state = compute_review_state([
        {
            "id": "c1",
            "author_persona": "codex-reviewer",
            "content": CODEX_R1_REAL,
            "created_at": "2026-05-19T02:01:09.280667+00:00",
        }
    ])
    assert state is not None
    assert state.updated_at is not None
    assert state.updated_at.year == 2026


def test_iso_with_trailing_z_accepted():
    state = compute_review_state([
        {
            "id": "c1",
            "author_persona": "codex-reviewer",
            "content": CODEX_R1_REAL,
            "created_at": "2026-05-19T02:01:09Z",
        }
    ])
    assert state is not None


def test_malformed_codex_comment_contributes_nothing():
    """A codex comment that doesn't match the expected header pattern
    is ignored — better to under-report than to fabricate a round."""
    content = "Just some random codex chatter, not a review."
    state = compute_review_state([
        _comment("c1", "codex-reviewer", content, datetime(2026, 5, 1, tzinfo=timezone.utc)),
    ])
    # Even though there's a codex-reviewer comment, no parseable round
    # means no review state.
    assert state is None


def test_to_dict_shape_is_json_serializable():
    """The Pydantic-less ReviewState carries datetimes — to_dict must
    convert them to ISO strings so callers can json.dumps the result."""
    import json

    t = datetime(2026, 5, 19, 2, 1, tzinfo=timezone.utc)
    state = compute_review_state([
        _comment("c1", "codex-reviewer", CODEX_R1_REAL, t),
    ])
    assert state is not None
    payload = json.dumps(state.to_dict())
    parsed = json.loads(payload)
    assert parsed["last_verdict"] == VERDICT_CHANGES
    assert parsed["open_findings"][0]["severity"] == "LOW"
    assert parsed["updated_at"] == t.isoformat()


def test_long_thread_compression_size_bound():
    """A long review thread (10 rounds, several findings each) should
    still produce a compact state proportional to remaining-open
    findings — NOT to total comment count. This is the core value
    of the feature."""
    comments = []
    t = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i in range(1, 10):
        if i % 3 == 0:
            # Every 3rd round is VERIFIED-CLEAN — closes prior findings
            content = f"Codex R{i} review on tk_x: VERIFIED-CLEAN\nFindings: none.\n"
        else:
            content = f"""\
Codex R{i} review on tk_x: CHANGES REQUESTED

Findings:

 • MEDIUM — finding A in round {i}
 • LOW — finding B in round {i}
"""
        comments.append(
            _comment(f"c{i}", "codex-reviewer", content, t + timedelta(hours=i))
        )
        comments.append(
            _comment(f"a{i}", "atlas", f"closure for R{i}", t + timedelta(hours=i, minutes=5))
        )

    state = compute_review_state(comments)
    assert state is not None
    # After R3 (CLEAN), R6 (CLEAN), R9 (CLEAN): every prior round's
    # findings are closed. R7 and R8 have findings closed in R9.
    # No R10 yet, but R9 is clean → all open findings clear.
    assert state.last_verdict == VERDICT_CLEAN
    assert len(state.open_findings) == 0
    # Closed list captures the audit trail for everything ever raised.
    # 6 CHANGES_REQUESTED rounds × 2 findings each = 12 closed.
    assert len(state.closed_findings) == 12
