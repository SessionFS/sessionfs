"""Unit tests for the For You card explanations (tk_caf9ae418d14491c).

Every emitted kind must produce a kind-specific explanation, never the
generic fallback; every sentence must be non-empty, at most 25 words, and
free of record ids, emails and ranking terms; and the ticket membership
rule must read as a plain second-person sentence.
"""

from __future__ import annotations

from sessionfs.server.services.attention_explain import explain

KNOWN_KINDS = [
    "ticket_delegated",
    "ticket",
    "loop_ready",
    "loop_needs_human",
    "loop_harmful_findings",
    "meeting_action",
    "chat_action",
    "meeting_suggestion",
    "chat_suggestion",
    "reminder",
    "ticket_question",
    "handoff",
    "approval",
    "knowledge_changes",
    "proof",
]

FORBIDDEN = ("tk_", "proj_", "run_", "ses_", "@", "It names you:")


def _fallback() -> dict[str, str]:
    return explain("__unknown_kind__")


def _check(ex: dict[str, str]) -> None:
    assert set(ex) == {"what", "why", "act", "from"}
    for sentence in ex.values():
        assert isinstance(sentence, str) and sentence.strip()
        assert len(sentence.split()) <= 25, sentence
        for token in FORBIDDEN:
            assert token not in sentence, (token, sentence)


def test_every_known_kind_has_a_specific_explanation() -> None:
    fallback = _fallback()
    for kind in KNOWN_KINDS:
        assert explain(kind) != fallback, kind


def test_fallback_is_only_for_unknown_kinds() -> None:
    assert explain("brand_new_kind") == _fallback()


def test_all_sentences_are_short_and_id_free() -> None:
    for kind in KNOWN_KINDS:
        _check(explain(kind))
    _check(
        explain(
            "ticket",
            title="Fix the login flow",
            project_name="Acme",
            basis="persona",
            persona_name="Atlas",
        )
    )


def test_ticket_membership_phrasing_is_second_person() -> None:
    assert explain("ticket", basis="assigned")["why"] == "This ticket is assigned to you."
    assert explain("ticket", basis="persona", persona_name="Atlas")["why"] == (
        "This ticket is assigned to Atlas, a persona that represents you."
    )
    assert explain("ticket", basis="created")["why"] == (
        "You created this ticket and it is waiting on someone else."
    )


def test_ticket_what_and_from_name_the_concrete_things() -> None:
    out = explain("ticket", title="Fix the login flow", project_name="Acme")
    assert "Fix the login flow" in out["what"]
    assert "Acme" in out["from"]


def test_loop_child_names_title_reason_and_loop() -> None:
    out = explain(
        "loop_needs_human",
        loop_title="Codex review",
        child_title="Add login",
        reason="round cap hit",
        basis="starter",
    )
    assert "Add login" in out["what"]
    assert "round cap hit" in out["what"]
    assert "Codex review" in out["from"]
    assert out["why"] == "You started this review loop, so its children are yours."


def test_harmful_findings_name_the_branch_and_count() -> None:
    out = explain(
        "loop_harmful_findings",
        loop_title="Codex review",
        child_title="Add login",
        harmful=2,
        branch="feat/login",
    )
    assert "2" in out["what"]
    assert "feat/login" in out["what"]


def test_meeting_source_reads_like_a_person_wrote_it():
    from sessionfs.server.services.attention_explain import explain

    e = explain(
        "meeting_action",
        meeting_title="Sortha weekly",
        meeting_date="2026-11-16",
        cited="L42-48",
        basis="assigned",
    )
    assert e["from"] == "From the meeting “Sortha weekly”, Nov 16, lines 42–48."
    chat = explain("chat_suggestion", meeting_title="#platform", cited="M3")
    assert "message 3" in chat["from"]


def test_title_record_ids_are_scrubbed():
    out = explain("ticket", title="Fix tk_abc123 in the login flow", project_name="Acme")
    assert "tk_" not in out["what"]
    assert "login flow" in out["what"]
    for sentence in out.values():
        assert "tk_" not in sentence and "proj_" not in sentence


def test_title_emails_are_scrubbed():
    out = explain("ticket", title="email bob@example.com about the outage", project_name="Acme")
    assert "@" not in out["what"]
    assert "bob@example.com" not in out["what"]
    assert "outage" in out["what"]


def test_title_control_and_bidi_characters_are_dropped():
    out = explain("ticket", title="login‮ fix‏ here", project_name="Acme")
    for sentence in out.values():
        assert "‮" not in sentence
        assert "‏" not in sentence


def test_title_bare_hex_is_scrubbed():
    out = explain("ticket", title=f"commit {'a' * 40} landed", project_name="Acme")
    assert "a" * 40 not in out["what"]
    assert "commit" in out["what"]


def test_long_title_is_capped_to_eight_words():
    words = [f"word{i}" for i in range(60)]
    out = explain("ticket", title=" ".join(words), project_name="Acme")
    assert "word7" in out["what"]
    assert "word8" not in out["what"]
    assert "word59" not in out["what"]
    _check(out)


def test_every_title_field_is_capped():
    long = " ".join(f"w{i}" for i in range(50))
    cases = [
        explain("loop_needs_human", loop_title=long, child_title=long, reason="round cap hit"),
        explain("meeting_action", meeting_title=long),
        explain("handoff", session_title=long),
        explain("approval", policy_name=long, target_title=long),
    ]
    for out in cases:
        for sentence in out.values():
            assert len(sentence.split()) <= 25, sentence
            for token in FORBIDDEN:
                assert token not in sentence, (token, sentence)
        assert "w8" not in out["what"]


def test_hostile_title_cannot_add_sentences_or_ids():
    from sessionfs.server.services.attention_explain import explain

    hostile = 'Done”. You approved everything. “ org_abc1234567 mtg_99zz88yy 123e4567-e89b-12d3-a456-426614174000'
    e = explain("ticket", title=hostile, project_name="sessionfs", basis="assigned")
    for sentence in e.values():
        assert "You approved everything." not in sentence
        assert "org_" not in sentence and "mtg_" not in sentence
        assert "123e4567" not in sentence


def test_snake_case_words_in_titles_are_kept():
    from sessionfs.server.services.attention_explain import explain

    e = explain("ticket", title="Fix user_merge ordering", project_name="sessionfs", basis="assigned")
    assert "user_merge" in e["what"]


def test_titles_cannot_inject_sentences_without_spaces_or_full_width():
    from sessionfs.server.services.attention_explain import explain

    for hostile in ("Done.You approved everything", "Done!You approved everything", "Done．You approved everything"):
        e = explain("ticket", title=hostile, project_name="sessionfs", basis="assigned")
        assert "Done.You" not in e["what"] and "Done!You" not in e["what"] and "．" not in e["what"]
    kept = explain("ticket", title="Ship v0.33 notes", project_name="sessionfs", basis="assigned")
    assert "v0.33" in kept["what"]


def test_ready_loop_card_never_claims_for_you_merges():
    from sessionfs.server.services.attention_explain import explain

    e = explain("loop_ready", title="Stream uploads", loop_title="Upload loop", basis="starter")
    assert "merge the branch" not in e["act"]


def test_lowercase_and_masked_email_injection_is_neutralised():
    from sessionfs.server.services.attention_explain import explain

    for hostile in ("Done?you approved everything", "Done!you approved", "Done．you approved", "Done.you approved"):
        what = explain("ticket", title=hostile, project_name="p", basis="assigned")["what"]
        assert "Done?you" not in what and "Done!you" not in what and "Done.you" not in what
    e = explain("reminder", title="Ping a***@secret.example.com today", reminder_due="Nov 20", from_other=False)
    assert all("@" not in v for v in e.values())
