"""Unit tests for the promotion pipeline (promote.py).

Covers the deterministic draft, the citation grammar, and the hostile-model
defenses (prose instead of JSON, invented/out-of-range/missing citations, a
rewritten verbatim section). DB-backed cases use the in-memory engine; the
LLM is always monkeypatched — never a real provider call.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    OrgLlmSettings,
    OrgMember,
    Organization,
    Session,
    Ticket,
    User,
    UserJudgeSettings,
)
from sessionfs.server.integrations.trackers.promote import (
    ContentTooLarge,
    TicketSources,
    _extract_rewrites,
    _pack_acceptance_criteria,
    _parse_llm_json,
    _present_source,
    _redact_for_llm,
    _redact_sources,
    _segment_text,
    _split_long_text,
    _validate_citation,
    adf_text_length,
    build_deterministic,
    build_preview,
    collect_sources,
    payload_hash,
    sections_to_adf,
)
from sessionfs.server.integrations.trackers.templates import get_template
from sessionfs.server.llm_keys import encrypt_secret

_AGILE = get_template("agile")


def _sources(**over) -> TicketSources:
    base = dict(
        title="Ship the auth migration",
        description="line one\nline two\nline three",
        description_lines=["line one", "line two", "line three"],
        acceptance_criteria=["users can log in", "sessions persist"],
        priority="high",
        kind="task",
        persona="atlas",
        completion_notes="shipped to staging",
        linked_sessions=["ses_1 (title A)"],
    )
    base.update(over)
    return TicketSources(**base)


# ── citation grammar ──


def test_validate_citation_description_ranges():
    s = _sources()
    assert _validate_citation("description:L1-L3", s)
    assert _validate_citation("description:L2-L2", s)
    assert _validate_citation("description:L2", s)
    assert not _validate_citation("description:L0-L1", s)
    assert not _validate_citation("description:L2-L9", s)  # out of range
    assert not _validate_citation("description:L3-L2", s)  # inverted


def test_validate_citation_acceptance_criteria():
    s = _sources()
    assert _validate_citation("acceptance_criteria[0]", s)
    assert _validate_citation("acceptance_criteria[1]", s)
    assert not _validate_citation("acceptance_criteria[2]", s)  # invented
    assert not _validate_citation("acceptance_criteria[-1]", s)


def test_validate_citation_named_fields():
    s = _sources()
    for field in ("title", "priority", "kind", "persona", "completion_notes", "linked_sessions"):
        assert _validate_citation(field, s)
    assert not _validate_citation("summary", s)
    # persona/completion_notes are falsy → citation fails.
    empty = _sources(persona=None, completion_notes=None)
    assert not _validate_citation("persona", empty)
    assert not _validate_citation("completion_notes", empty)


# ── deterministic draft ──


def test_build_deterministic_verbatim_copies_exact():
    sections = build_deterministic(_AGILE, _sources())
    by_key = {s["key"]: s for s in sections}
    ac = by_key["acceptance_criteria"]
    assert ac["mode"] == "verbatim"
    texts = [p["text"] for p in ac["paragraphs"]]
    # Packed into one Markdown-list paragraph, verbatim and in order.
    assert texts == ["- users can log in\n- sessions persist"]
    cites = [p["citations"] for p in ac["paragraphs"]]
    assert cites == [["acceptance_criteria[0]", "acceptance_criteria[1]"]]


def test_build_deterministic_summary_is_rewrite():
    sections = build_deterministic(_AGILE, _sources())
    summary = sections[0]
    assert summary["mode"] == "rewrite"
    # title paragraph is always present with a citation.
    assert any(p["text"] == "Ship the auth migration" for p in summary["paragraphs"])


# ── JSON parsing ──


def test_parse_llm_json_accepts_plain_object():
    assert _parse_llm_json('{"sections": []}') == {"sections": []}


def test_parse_llm_json_strips_fences():
    raw = "```json\n{\"sections\": []}\n```"
    assert _parse_llm_json(raw) == {"sections": []}


def test_parse_llm_json_accepts_prose_wrapping():
    raw = "Sure! Here you go: {\"sections\": []} hope it helps"
    assert _parse_llm_json(raw) == {"sections": []}


def test_parse_llm_json_rejects_prose_and_non_dict():
    assert _parse_llm_json("no JSON here at all") is None
    assert _parse_llm_json("") is None
    assert _parse_llm_json("[1, 2, 3]") is None  # list, not object


# ── hostile rewrite extraction ──


def _rewrite(data: dict):
    return _extract_rewrites(data, _AGILE, _sources())


def test_extract_rewrites_accepts_valid_rewrite():
    out = _rewrite({
        "sections": [
            {"key": "summary", "paragraphs": [
                {"text": "Ship auth migration", "citations": ["description:L1-L2"]},
            ]},
        ]
    })
    assert set(out) == {"summary"}
    assert out["summary"][0].text == "Ship auth migration"


def test_extract_rewrites_drops_verbatim_and_unknown_keys():
    out = _rewrite({
        "sections": [
            {"key": "acceptance_criteria", "paragraphs": [
                {"text": "invented AC", "citations": ["acceptance_criteria[0]"]},
            ]},
            {"key": "totally_unknown", "paragraphs": [
                {"text": "x", "citations": ["title"]},
            ]},
        ]
    })
    assert out == {}


def test_extract_rewrites_drops_invented_ac_index():
    out = _rewrite({
        "sections": [
            {"key": "summary", "paragraphs": [
                {"text": "needs five ACs", "citations": ["acceptance_criteria[5]"]},
            ]},
        ]
    })
    assert out == {}


def test_extract_rewrites_drops_missing_citations():
    out = _rewrite({
        "sections": [
            {"key": "summary", "paragraphs": [{"text": "no cites"}]},
            {"key": "context", "paragraphs": [{"text": "empty cites", "citations": []}]},
        ]
    })
    assert out == {}


def test_extract_rewrites_drops_out_of_range_description():
    out = _rewrite({
        "sections": [
            {"key": "summary", "paragraphs": [
                {"text": "too long", "citations": ["description:L1-L99"]},
            ]},
        ]
    })
    assert out == {}


def test_extract_rewrites_keeps_only_resolvable_citations():
    out = _rewrite({
        "sections": [
            {"key": "summary", "paragraphs": [
                {
                    "text": "mixed",
                    "citations": ["description:L1-L2", "acceptance_criteria[9]"],
                },
            ]},
        ]
    })
    assert out["summary"][0].citations == ["description:L1-L2"]


def test_extract_rewrites_drops_non_dict_paragraphs_and_empty_text():
    out = _rewrite({
        "sections": [
            {"key": "summary", "paragraphs": [
                "not a dict",
                {"text": "   ", "citations": ["title"]},
                {"text": "ok", "citations": ["title"]},
            ]},
        ]
    })
    assert [p.text for p in out["summary"]] == ["ok"]


# ── payload hash ──


def test_payload_hash_is_stable_and_order_independent():
    sections = [{"key": "a", "paragraphs": []}]
    assert payload_hash("s", sections) == payload_hash("s", sections)
    assert payload_hash("s", sections) != payload_hash("t", sections)


# ── DB-backed helpers ──


async def _org(db: AsyncSession, tier: str = "enterprise") -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:10]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:8]}",
        tier=tier,
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.commit()
    return org


async def _user(db: AsyncSession) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:8]}@example.com",
        display_name="U",
        tier="pro",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    return user


async def _ticket(db: AsyncSession, project_id: str, user_id: str, **over) -> Ticket:
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        title="Ship the auth migration",
        description="line one\nline two\nline three",
        acceptance_criteria=json.dumps(["users can log in", "sessions persist"]),
        priority="high",
        assigned_to="atlas",
        created_by_user_id=user_id,
        **over,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


@pytest.mark.asyncio
async def test_collect_sources_resolves_linked_session_titles(db_session: AsyncSession):
    user = await _user(db_session)
    ticket = await _ticket(
        db_session,
        "proj_x",
        user.id,
        related_sessions=json.dumps(["ses_1"]),
    )
    db_session.add(Session(
        id="ses_1", user_id=user.id, title="Migrate auth flows",
        source_tool="claude-code", blob_key="blobs/x", etag="abc",
        created_at=datetime.now(timezone.utc), project_id="proj_x",
    ))
    await db_session.commit()

    sources = await collect_sources(ticket, db_session)
    assert sources.linked_sessions == ["Migrate auth flows (ses_1)"]
    assert sources.description_lines == ["line one", "line two", "line three"]
    assert sources.acceptance_criteria == ["users can log in", "sessions persist"]
    assert sources.persona == "atlas"


@pytest.mark.asyncio
async def test_build_preview_falls_back_to_deterministic_without_key(db_session: AsyncSession):
    org = await _org(db_session)
    user = await _user(db_session)
    ticket = await _ticket(db_session, "proj_x", user.id)

    preview = await build_preview(
        ticket=ticket, template=_AGILE, issue_type="Story",
        jira_project_key="ACME", org=org, user=user, db=db_session,
    )
    assert preview.summary == "Ship the auth migration"
    assert preview.issue_type == "Story"
    assert preview.jira_project_key == "ACME"
    assert preview.llm_used is False
    assert preview.key_source == "none"
    assert "acceptance_criteria[0]" in preview.citations


@pytest.mark.asyncio
async def test_build_preview_uses_llm_for_rewrite_sections(db_session: AsyncSession, monkeypatch):
    org = await _org(db_session)
    user = await _user(db_session)
    ticket = await _ticket(db_session, "proj_x", user.id)
    db_session.add(UserJudgeSettings(
        user_id=user.id, provider="openai", model="gpt-x",
        encrypted_api_key=encrypt_secret("sk-123"),
    ))
    await db_session.commit()

    async def fake_call_llm(*, model, system, prompt, api_key, provider=None, temperature=0, base_url=None):
        assert api_key == "sk-123"
        return json.dumps({
            "sections": [
                {"key": "summary", "paragraphs": [
                    {"text": "Ship the auth migration", "citations": ["description:L1-L3"]},
                ]},
            ]
        })

    monkeypatch.setattr("sessionfs.judge.providers.call_llm", fake_call_llm)

    preview = await build_preview(
        ticket=ticket, template=_AGILE, issue_type="Story",
        jira_project_key="ACME", org=org, user=user, db=db_session,
    )
    assert preview.llm_used is True
    assert preview.key_source == "user"
    summary = next(s for s in preview.sections if s["key"] == "summary")
    assert [p["text"] for p in summary["paragraphs"]] == ["Ship the auth migration"]
    # Verbatim section is untouched by the model.
    ac = next(s for s in preview.sections if s["key"] == "acceptance_criteria")
    assert [p["text"] for p in ac["paragraphs"]] == ["- users can log in\n- sessions persist"]


@pytest.mark.asyncio
async def test_build_preview_resolves_org_helper_key(db_session: AsyncSession, monkeypatch):
    org = await _org(db_session)
    user = await _user(db_session)
    ticket = await _ticket(db_session, "proj_x", user.id)
    db_session.add(OrgMember(
        org_id=org.id, user_id=user.id, role="member",
        joined_at=datetime.now(timezone.utc),
    ))
    db_session.add(OrgLlmSettings(
        org_id=org.id, provider="openai", model="gpt-x",
        encrypted_api_key=encrypt_secret("sk-org"), enabled=True,
    ))
    await db_session.commit()

    async def fake_call_llm(*, model, system, prompt, api_key, provider=None, temperature=0, base_url=None):
        assert api_key == "sk-org"
        return json.dumps({
            "sections": [
                {"key": "summary", "paragraphs": [
                    {"text": "Ship the auth migration", "citations": ["description:L1-L3"]},
                ]},
            ]
        })

    monkeypatch.setattr("sessionfs.judge.providers.call_llm", fake_call_llm)

    preview = await build_preview(
        ticket=ticket, template=_AGILE, issue_type="Story",
        jira_project_key="ACME", org=org, user=user, db=db_session,
    )
    assert preview.llm_used is True
    assert preview.key_source == "org"


@pytest.mark.asyncio
async def test_build_preview_llm_failure_falls_back(db_session: AsyncSession, monkeypatch):
    org = await _org(db_session)
    user = await _user(db_session)
    ticket = await _ticket(db_session, "proj_x", user.id)
    db_session.add(UserJudgeSettings(
        user_id=user.id, provider="openai", model="gpt-x",
        encrypted_api_key=encrypt_secret("sk-123"),
    ))
    await db_session.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("sessionfs.judge.providers.call_llm", boom)

    preview = await build_preview(
        ticket=ticket, template=_AGILE, issue_type="Story",
        jira_project_key="ACME", org=org, user=user, db=db_session,
    )
    assert preview.llm_used is False
    assert preview.key_source == "none"
    assert preview.summary == "Ship the auth migration"


# ── DLP redaction before source material reaches the model (finding #3) ──

# ``sk-`` + 20 chars matches the ``openai_api_key`` secret pattern.
_SECRET = "sk-" + "a" * 20


def _org_with_settings(settings: dict | None) -> Organization:
    return Organization(
        id="org_dlp", name="Org", slug="o-dlp", tier="enterprise",
        settings=json.dumps(settings) if settings is not None else "{}",
    )


def test_redact_for_llm_redacts_secrets_without_policy():
    org = _org_with_settings(None)
    out = _redact_for_llm(f"title: {_SECRET}", org)
    assert _SECRET not in out
    assert "[REDACTED" in out


def test_redact_for_llm_redacts_secrets_when_dlp_disabled():
    org = _org_with_settings({"dlp": {"enabled": False}})
    out = _redact_for_llm(f"title: {_SECRET}", org)
    assert _SECRET not in out
    assert "[REDACTED" in out


def test_redact_for_llm_redacts_secrets_even_when_policy_is_phi_only():
    org = _org_with_settings({"dlp": {"enabled": True, "categories": ["phi"]}})
    out = _redact_for_llm(f"title: {_SECRET}", org)
    assert _SECRET not in out
    assert "[REDACTED" in out


def test_present_source_caps_description_and_other_fields():
    sources = _sources(description="d" * 9000, title="t" * 2500)
    text = _present_source(sources)
    assert "d" * 9000 not in text
    assert "t" * 2500 not in text


def test_redact_sources_redacts_secret_straddling_description_cap():
    # A secret that begins just before the 8,000-char description cap must be
    # redacted from the FULL text first. Capping first would truncate it into a
    # non-matching "sk-" prefix and leak that prefix to the model unredacted.
    org = _org_with_settings(None)
    description = "a" * 7995 + _SECRET
    sources = _sources(description=description)

    redacted = _redact_sources(sources, org)
    assert _SECRET not in redacted.description
    assert "[REDACTED" in redacted.description

    text = _present_source(redacted)
    assert _SECRET not in text
    assert "sk-" not in text


# ── ADF rendering: rewrite text is never parsed as Markdown (finding #4) ──


def test_sections_to_adf_emits_rewrite_paragraphs_as_plain_text():
    forged = "# forged heading\n- fake list\n[x](javascript:alert(1))"
    sections = [
        {
            "key": "summary", "heading": "Summary",
            "paragraphs": [{"text": forged, "citations": []}],
        },
    ]
    doc = sections_to_adf(sections, {"summary": "rewrite"})
    blocks = doc["content"]
    assert blocks[0]["type"] == "heading"  # the section heading only
    para = blocks[1]
    assert para["type"] == "paragraph"
    # No markdown parsing: the forged heading/list/link stay literal text. The
    # single newlines become hardBreak nodes (no raw `\n` inside a text node).
    assert para["content"] == [
        {"type": "text", "text": "# forged heading"},
        {"type": "hardBreak"},
        {"type": "text", "text": "- fake list"},
        {"type": "hardBreak"},
        {"type": "text", "text": "[x](javascript:alert(1))"},
    ]


def test_sections_to_adf_parses_verbatim_as_markdown():
    sections = [
        {
            "key": "notes", "heading": "Notes",
            "paragraphs": [{"text": "**bold**", "citations": []}],
        },
    ]
    doc = sections_to_adf(sections, {"notes": "verbatim"})
    blocks = doc["content"]
    assert blocks[0]["type"] == "heading"
    para = blocks[1]
    assert para["type"] == "paragraph"
    assert para["content"][0]["marks"] == [{"type": "strong"}]


def test_sections_to_adf_raises_content_too_large_instead_of_truncating():
    # 55 paragraphs and a 12000-char paragraph both overflow the caps; nothing
    # is silently dropped — the section is named in the error instead.
    paragraphs = [{"text": "x" * 12000, "citations": []}] * 55
    sections = [
        {"key": "k", "heading": "h", "paragraphs": paragraphs},
    ]
    with pytest.raises(ContentTooLarge) as exc_info:
        sections_to_adf(sections, {"k": "rewrite"})
    assert exc_info.value.section == "k"


def test_sections_to_adf_rewrite_overlong_line_stays_one_paragraph():
    # A rewrite section's single unbroken 30,000-char line becomes ONE ADF
    # paragraph whose content holds consecutive ≤10,000-char text nodes.
    text = "x" * 30000
    sections = [
        {"key": "k", "heading": "h", "paragraphs": [{"text": text, "citations": []}]},
    ]
    doc = sections_to_adf(sections, {"k": "rewrite"})
    blocks = doc["content"]
    assert blocks[0]["type"] == "heading"
    paras = [b for b in blocks if b["type"] == "paragraph"]
    assert len(paras) == 1
    content = paras[0]["content"]
    assert [n["type"] for n in content] == ["text", "text", "text"]
    assert "".join(n["text"] for n in content) == text
    assert all(len(n["text"]) == 10000 for n in content)


def test_sections_to_adf_verbatim_overlong_line_becomes_consecutive_blocks():
    # A verbatim section's single unbroken 30,000-char line is split into
    # consecutive ≤10,000-char Markdown blocks, preserving every character.
    text = "x" * 30000
    sections = [
        {"key": "k", "heading": "h", "paragraphs": [{"text": text, "citations": []}]},
    ]
    doc = sections_to_adf(sections, {"k": "verbatim"})
    blocks = doc["content"]
    paras = [b for b in blocks if b["type"] == "paragraph"]
    assert len(paras) == 3
    assert "".join(n["text"] for b in paras for n in b["content"]) == text


def test_adf_text_length_sums_every_text_node():
    doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "heading", "content": [{"type": "text", "text": "Hi"}]},
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "abc"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "de"},
                ],
            },
        ],
    }
    assert adf_text_length(doc) == 7


# ── long-text splitting (finding L2) ──


def test_split_long_text_splits_only_on_paragraph_boundaries():
    text = "aaa\nbbb\n\nccc\n\nddd"
    chunks = _split_long_text(text, limit=8)
    assert "".join(chunks) == text
    assert all(len(c) <= 8 for c in chunks)


def test_split_long_text_long_paragraph_splits_on_lines_keeping_newlines():
    text = "aaaa\nbbbb\ncccc\ndddd"
    chunks = _split_long_text(text, limit=9)
    assert "".join(chunks) == text
    assert all(len(c) <= 9 for c in chunks)
    # Split only at line boundaries: every non-final chunk keeps its newline.
    assert all(c.endswith("\n") for c in chunks[:-1])


def test_split_long_text_splits_code_block_into_complete_fences():
    text = "```\nline1\nline2\nline3\n```"
    chunks = _split_long_text(text, limit=12)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.startswith("```\n")
        assert chunk.endswith("\n```")
    inner_lines = [line for c in chunks for line in c.splitlines()[1:-1]]
    assert "\n".join(inner_lines) == "line1\nline2\nline3"


def test_split_long_text_keeps_blank_lines_inside_fences():
    text = "para\n\n```\ncode\n\nmore code\n```\n\nafter"
    segments = _segment_text(text)
    kinds = [kind for kind, _ in segments]
    # The blank line inside the fence stays inside the code segment.
    assert kinds == ["para", "code", "para", "para"]
    assert "".join(content for _, content in segments) == text


def test_split_long_text_splits_single_unbroken_line_into_pieces():
    text = "x" * 30000
    chunks = _split_long_text(text, limit=10000)
    assert chunks == ["x" * 10000, "x" * 10000, "x" * 10000]
    assert "".join(chunks) == text
    assert all(len(c) <= 10000 for c in chunks)


# ── acceptance-criteria packing (finding M1) ──


def test_pack_acceptance_criteria_packs_60_verbatim_in_order():
    criteria = [f"criterion {i}" for i in range(60)]
    paragraphs = _pack_acceptance_criteria(criteria)
    rendered = "\n".join(p.text for p in paragraphs)
    assert rendered.splitlines() == [f"- criterion {i}" for i in range(60)]
    assert all(len(p.text) <= 10000 for p in paragraphs)
    assert all(len(p.citations) <= 20 for p in paragraphs)


def test_pack_acceptance_criteria_packs_500_verbatim_in_order():
    criteria = [f"criterion {i}" for i in range(500)]
    paragraphs = _pack_acceptance_criteria(criteria)
    rendered = "\n".join(p.text for p in paragraphs)
    assert rendered.splitlines() == [f"- criterion {i}" for i in range(500)]
    assert len(paragraphs) <= 50
    assert all(len(p.text) <= 10000 for p in paragraphs)
    assert all(len(p.citations) <= 20 for p in paragraphs)


def test_pack_acceptance_criteria_raises_when_single_criterion_too_large():
    criteria = ["x" * 10001]
    with pytest.raises(ContentTooLarge) as exc_info:
        _pack_acceptance_criteria(criteria)
    assert exc_info.value.section == "acceptance_criteria"


def test_pack_acceptance_criteria_indents_continuation_lines():
    criteria = ["line one\nline two", "next"]
    (paragraph,) = _pack_acceptance_criteria(criteria)
    assert paragraph.text == "- line one\n  line two\n- next"


def test_sections_to_adf_keeps_multiline_criterion_in_one_list_item():
    criteria = ["users can log in\nwith their email", "sessions persist"]
    paragraphs = _pack_acceptance_criteria(criteria)
    sections = [
        {
            "key": "acceptance_criteria", "heading": "Acceptance Criteria",
            "paragraphs": [p.to_dict() for p in paragraphs],
        },
    ]
    doc = sections_to_adf(sections, {"acceptance_criteria": "verbatim"})
    bullet = next(b for b in doc["content"] if b["type"] == "bulletList")
    assert len(bullet["content"]) == 2
    first = bullet["content"][0]["content"][0]["content"]
    texts = [n["text"] for n in first if n["type"] == "text"]
    assert texts == ["users can log in", "with their email"]


# ── source collection scoping (finding #13) ──


@pytest.mark.asyncio
async def test_collect_sources_excludes_other_project_and_deleted_sessions(
    db_session: AsyncSession,
):
    user = await _user(db_session)
    ticket = await _ticket(
        db_session, "proj_x", user.id,
        related_sessions=json.dumps(["ses_1", "ses_2", "ses_3"]),
    )
    db_session.add_all([
        Session(
            id="ses_1", user_id=user.id, title="In project",
            source_tool="claude-code", blob_key="blobs/x", etag="abc",
            created_at=datetime.now(timezone.utc), project_id="proj_x",
        ),
        Session(
            id="ses_2", user_id=user.id, title="Other project",
            source_tool="claude-code", blob_key="blobs/x", etag="abc",
            created_at=datetime.now(timezone.utc), project_id="proj_y",
        ),
        Session(
            id="ses_3", user_id=user.id, title="Deleted",
            source_tool="claude-code", blob_key="blobs/x", etag="abc",
            created_at=datetime.now(timezone.utc), project_id="proj_x",
            is_deleted=True,
        ),
    ])
    await db_session.commit()

    sources = await collect_sources(ticket, db_session)
    assert sources.linked_sessions == ["In project (ses_1)"]
