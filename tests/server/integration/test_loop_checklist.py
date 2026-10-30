"""Review Loop R3 track E1 — the project checklist that learns.

Covers the loop_checklist_items surface end to end:

- seed: six generic traps offered as ``proposed``, idempotent on re-seed.
- accept / retire lifecycle (and the proposed→active acceptor stamp).
- manual add + duplicate-slug 409.
- markdown renders ACTIVE items only (briefs).
- reader (captured session) vs editor, and the outsider existence-oracle 404.
- proposals derived from ``class="new"`` findings (created then deduped via a
  ``times_found`` bump) and from ``class`` matching an existing slug.
- the complete_agent_run hook wires proposals in without failing completion.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    LoopChecklistItem,
    Organization,
    Project,
    Session,
    User,
)
from sessionfs.server.routes import loop_checklist
from sessionfs.server.routes.loop_checklist import propose_from_findings


async def _make_user(
    db: AsyncSession, name: str = "owner", tier: str = "team"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_project(
    db: AsyncSession, owner: User, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"loop-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/loop-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_org(db: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _make_service_key(
    db: AsyncSession, org: Organization, minter: User, scopes: list[str]
) -> tuple[str, str]:
    """Create a service key bound to an org; returns (raw_key, api_key_id)."""
    raw = generate_api_key()
    key_id = str(uuid.uuid4())
    db.add(
        ApiKey(
            id=key_id,
            user_id=minter.id,
            key_hash=hash_api_key(raw),
            name=f"svc-{uuid.uuid4().hex[:6]}",
            is_active=True,
            key_kind="service",
            org_id=org.id,
            scopes=json.dumps(scopes),
            created_by_user_id=minter.id,
            service_key_name=f"svc-{uuid.uuid4().hex[:6]}",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw, key_id


async def _capture_session(db: AsyncSession, user: User, project: Project) -> None:
    """A synced session on the project's repo → read (not edit) standing."""
    db.add(
        Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=user.id,
            title="synced from the same repo",
            tags="[]",
            source_tool="claude-code",
            blob_key="x",
            blob_size_bytes=0,
            etag="x",
            git_remote_normalized=project.git_remote_normalized,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            uploaded_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def _make_persona(db: AsyncSession, project: Project, owner: User) -> AgentPersona:
    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name="atlas",
        role="Backend",
        content="# Atlas\n\nBackend persona.",
        created_by=owner.id,
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


async def _seed(client: AsyncClient, project: Project, key: str) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-checklist/seed", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _items(db: AsyncSession, project: Project) -> list[LoopChecklistItem]:
    rows = (
        await db.execute(
            select(LoopChecklistItem)
            .where(LoopChecklistItem.project_id == project.id)
            .order_by(LoopChecklistItem.created_at, LoopChecklistItem.id)
        )
    ).scalars().all()
    return list(rows)


# ── Seed ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_creates_six_proposed_and_is_idempotent(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    first = await _seed(client, project, key)
    assert len(first["created"]) == 6
    assert first["skipped"] == []
    assert all(i["status"] == "proposed" for i in first["created"])
    assert all(i["source"] == "seed" for i in first["created"])

    # Re-seeding is a no-op: every seed slug already exists.
    second = await _seed(client, project, key)
    assert second["created"] == []
    assert len(second["skipped"]) == 6


# ── Lifecycle: accept / retire / manual add ───────────────────────


@pytest.mark.asyncio
async def test_accept_records_acceptor_and_retire_moves_to_retired(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _seed(client, project, key)

    rows = await _items(db_session, project)
    target = next(r for r in rows if r.slug == "existence-oracles")

    acc = await client.patch(
        f"/api/v1/projects/{project.id}/loop-checklist/{target.id}",
        headers=_hdrs(key),
        json={"status": "active"},
    )
    assert acc.status_code == 200, acc.text
    body = acc.json()
    assert body["status"] == "active"
    assert body["accepted_by_user_id"] == user.id

    # Re-accepting keeps the original acceptor.
    acc2 = await client.patch(
        f"/api/v1/projects/{project.id}/loop-checklist/{target.id}",
        headers=_hdrs(key),
        json={"status": "active"},
    )
    assert acc2.json()["accepted_by_user_id"] == user.id

    ret = await client.patch(
        f"/api/v1/projects/{project.id}/loop-checklist/{target.id}",
        headers=_hdrs(key),
        json={"status": "retired"},
    )
    assert ret.status_code == 200
    assert ret.json()["status"] == "retired"


@pytest.mark.asyncio
async def test_accept_stamps_accepted_at_on_first_transition_only(
    client: AsyncClient, db_session: AsyncSession
):
    """The proposed→active transition stamps ``accepted_at`` (the "joined the
    checklist" date) once; a re-accept preserves the original stamp."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _seed(client, project, key)

    rows = await _items(db_session, project)
    target = next(r for r in rows if r.slug == "existence-oracles")

    acc = await client.patch(
        f"/api/v1/projects/{project.id}/loop-checklist/{target.id}",
        headers=_hdrs(key),
        json={"status": "active"},
    )
    assert acc.status_code == 200, acc.text
    body = acc.json()
    assert body["accepted_at"] is not None

    # Re-accepting keeps the original accepted_at.
    acc2 = await client.patch(
        f"/api/v1/projects/{project.id}/loop-checklist/{target.id}",
        headers=_hdrs(key),
        json={"status": "active"},
    )
    assert acc2.json()["accepted_at"] == body["accepted_at"]

    # A still-proposed item has no accepted_at yet.
    listed = await client.get(
        f"/api/v1/projects/{project.id}/loop-checklist", headers=_hdrs(key)
    )
    unaccepted = next(
        i for i in listed.json() if i["slug"] != "existence-oracles"
    )
    assert unaccepted["accepted_at"] is None


@pytest.mark.asyncio
async def test_manual_add_and_duplicate_slug_409(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-checklist",
        headers=_hdrs(key),
        json={
            "slug": "my-trap",
            "title": "My trap",
            "check_text": "Check it.",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "proposed"
    assert body["source"] == "manual"
    assert body["times_found"] == 0

    dup = await client.post(
        f"/api/v1/projects/{project.id}/loop-checklist",
        headers=_hdrs(key),
        json={
            "slug": "my-trap",
            "title": "My trap again",
            "check_text": "Check it again.",
        },
    )
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_title_and_check_text_collapse_whitespace_on_write(
    client: AsyncClient, db_session: AsyncSession
):
    """Newlines/tabs/multiple spaces collapse to single spaces on write so
    brief markdown can't be restructured by multi-line input."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/loop-checklist",
        headers=_hdrs(key),
        json={
            "slug": "ws-trap",
            "title": "Bad\n   title\twith  spaces",
            "check_text": "line one\n\n\nline two   spaced",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Bad title with spaces"
    assert body["check_text"] == "line one line two spaced"

    patched = await client.patch(
        f"/api/v1/projects/{project.id}/loop-checklist/{body['id']}",
        headers=_hdrs(key),
        json={"title": "New\n  title", "check_text": "a\nb  c"},
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "New title"
    assert patched.json()["check_text"] == "a b c"


# ── Markdown (briefs) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_markdown_renders_active_items_only(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _seed(client, project, key)

    rows = await _items(db_session, project)
    active = next(r for r in rows if r.slug == "existence-oracles")
    await client.patch(
        f"/api/v1/projects/{project.id}/loop-checklist/{active.id}",
        headers=_hdrs(key),
        json={"status": "active"},
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/loop-checklist.md", headers=_hdrs(key)
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    text = resp.text
    assert "# Checklist" in text
    assert "**Existence oracles**" in text
    # Proposed and retired items never reach a brief.
    assert "Access rechecked after slow steps" not in text


# ── Access tiers ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_captured_session_reader_reads_but_cannot_write(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)

    reader, reader_key = await _make_user(db_session, name="reader")
    await _capture_session(db_session, reader, project)

    got = await client.get(
        f"/api/v1/projects/{project.id}/loop-checklist", headers=_hdrs(reader_key)
    )
    assert got.status_code == 200, got.text

    write = await client.post(
        f"/api/v1/projects/{project.id}/loop-checklist",
        headers=_hdrs(reader_key),
        json={"slug": "x", "title": "X", "check_text": "Check x."},
    )
    assert write.status_code == 403
    assert write.json()["error"]["code"] == "project_edit_forbidden"


@pytest.mark.asyncio
async def test_outsider_gets_byte_identical_404(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_key = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner)

    outsider, outsider_key = await _make_user(db_session, name="outsider")

    # A missing project and a project-you-can't-see return the same 404 body.
    missing = await client.get(
        f"/api/v1/projects/proj_{uuid.uuid4().hex[:16]}/loop-checklist",
        headers=_hdrs(owner_key),
    )
    hidden = await client.get(
        f"/api/v1/projects/{project.id}/loop-checklist",
        headers=_hdrs(outsider_key),
    )
    assert missing.status_code == 404
    assert hidden.status_code == 404
    assert missing.content == hidden.content


# ── Proposals from findings ───────────────────────────────────────


@pytest.mark.asyncio
async def test_finding_title_redacted_before_slug_and_refs(
    db_session: AsyncSession,
):
    """A secret in a finding title must never reach the slug, and
    ``source_finding_refs`` keeps only ``{run_id, title(redacted)}``."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    secret = "AKIAIOSFODNN7EXAMPLE"
    await propose_from_findings(
        db_session,
        project.id,
        "run_1",
        [
            {
                "class": "new",
                "title": f"Leaked key {secret} on read",
                "file": "src/x.py",
                "line": 12,
                "severity": "high",
            }
        ],
        user.id,
    )

    rows = await _items(db_session, project)
    assert len(rows) == 1
    item = rows[0]
    assert secret.lower() not in item.slug
    assert secret not in item.title
    refs = json.loads(item.source_finding_refs)
    assert refs == [{"run_id": "run_1", "title": item.title}]
    assert all("finding" not in r for r in refs)


@pytest.mark.asyncio
async def test_new_findings_propose_then_dedupe_by_slug(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)

    await propose_from_findings(
        db_session,
        project.id,
        "run_1",
        [{"class": "new", "title": "Missing existence oracle on read"}],
        user.id,
    )
    rows = await _items(db_session, project)
    assert len(rows) == 1
    item = rows[0]
    assert item.slug == "missing-existence-oracle-on-read"
    assert item.source == "finding"
    assert item.status == "proposed"
    assert item.times_found == 1
    assert item.last_found_at is not None

    # A second hit with the same title dedupes onto the same row.
    await propose_from_findings(
        db_session,
        project.id,
        "run_2",
        [{"class": "new", "title": "Missing existence oracle on read"}],
        user.id,
    )
    rows = await _items(db_session, project)
    assert len(rows) == 1
    assert rows[0].times_found == 2


@pytest.mark.asyncio
async def test_slug_race_between_select_and_insert_bumps_not_duplicates(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """A rival proposal landing between our SELECT and INSERT must not raise
    nor duplicate the row — the savepoint rolls back our losing INSERT and we
    bump the winner's ``times_found`` instead."""
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)

    real_prefetch = loop_checklist._prefetch_by_slugs
    rival_inserted = False

    async def prefetch_then_rival(db, project_id, slugs):
        nonlocal rival_inserted
        rows = await real_prefetch(db, project_id, slugs)
        if not rival_inserted:
            rival_inserted = True
            db.add(
                LoopChecklistItem(
                    id=f"lci_{uuid.uuid4().hex[:16]}",
                    project_id=project_id,
                    slug=slugs[0],
                    title="Rival trap",
                    check_text="Rival check.",
                    status="proposed",
                    source="finding",
                    source_finding_refs="[]",
                    times_found=1,
                    last_found_at=datetime.now(timezone.utc),
                    created_by_user_id=None,
                    accepted_by_user_id=None,
                )
            )
            await db.flush()
        return rows

    monkeypatch.setattr(loop_checklist, "_prefetch_by_slugs", prefetch_then_rival)

    await propose_from_findings(
        db_session,
        project.id,
        "run_race",
        [{"class": "new", "title": "Missing existence oracle on read"}],
        user.id,
    )

    rows = await _items(db_session, project)
    assert len(rows) == 1
    item = rows[0]
    assert item.slug == "missing-existence-oracle-on-read"
    # Our losing INSERT was rolled back; the winner took the re-hit bump.
    assert item.times_found == 2
    refs = json.loads(item.source_finding_refs)
    assert refs == [{"run_id": "run_race", "title": "Missing existence oracle on read"}]


@pytest.mark.asyncio
async def test_title_truncated_after_redaction_not_before(db_session: AsyncSession):
    """A title that redaction lengthens past 200 chars must be truncated after
    redaction, so the stored title never exceeds the column width."""
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)

    secret = "AKIAIOSFODNN7EXAMPLE"
    # 178 chars of padding + " <secret> " = 200 raw chars. Redaction turns the
    # 22-char secret (incl. surrounding spaces) into a 28-char marker, so the
    # sanitized title is 206 chars and must be re-truncated to 200.
    raw_title = ("A" * 178) + f" {secret} "
    assert len(raw_title) == 200

    await loop_checklist._propose_new(
        db_session,
        project.id,
        "grown-title",
        raw_title,
        {"run_id": "run_4", "title": "grown-title"},
        user.id,
        datetime.now(timezone.utc),
    )
    await db_session.commit()

    rows = await _items(db_session, project)
    assert len(rows) == 1
    item = rows[0]
    assert secret not in item.title
    assert len(item.title) <= 200


@pytest.mark.asyncio
async def test_at_most_twenty_new_proposals_per_completion(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)

    await propose_from_findings(
        db_session,
        project.id,
        "run_cap",
        [{"class": "new", "title": f"Trap {i}"} for i in range(25)],
        user.id,
    )

    rows = await _items(db_session, project)
    assert len(rows) == 20
    slugs = {r.slug for r in rows}
    assert slugs == {f"trap-{i}" for i in range(20)}


@pytest.mark.asyncio
async def test_at_most_two_hundred_proposed_per_project(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)

    for i in range(200):
        db_session.add(
            LoopChecklistItem(
                id=f"lci_bulk_{uuid.uuid4().hex[:16]}",
                project_id=project.id,
                slug=f"bulk-{i}",
                title=f"Bulk {i}",
                check_text="Bulk.",
                status="proposed",
                source="manual",
                source_finding_refs=None,
                times_found=0,
                last_found_at=None,
                created_by_user_id=user.id,
                accepted_by_user_id=None,
            )
        )
    await db_session.commit()

    await propose_from_findings(
        db_session,
        project.id,
        "run_cap_project",
        [{"class": "new", "title": "One more trap"}],
        user.id,
    )

    rows = await _items(db_session, project)
    assert len(rows) == 200
    assert "one-more-trap" not in {r.slug for r in rows}


# ── Service-key provenance (review checklist #3) ──────────────────


@pytest.mark.asyncio
async def test_service_key_seed_and_accept_stamp_actor_not_user(
    client: AsyncClient, db_session: AsyncSession
):
    """A service key leaves the user ids NULL and records its key provenance
    on the creator/acceptor columns instead of the minter's id."""
    org = await _make_org(db_session)
    owner, _ = await _make_user(db_session, name="owner")
    project = await _make_project(db_session, owner, org)
    raw, key_id = await _make_service_key(db_session, org, owner, ["loops:write"])

    seeded = await _seed(client, project, raw)
    assert len(seeded["created"]) == 6
    for item in seeded["created"]:
        assert item["created_by_user_id"] is None
        assert item["created_actor_type"] == "service_key"
        assert item["created_service_key_id"] == key_id
        assert item["created_service_key_name"]
        assert item["accepted_by_user_id"] is None
        assert item["accepted_actor_type"] is None

    target = seeded["created"][0]
    acc = await client.patch(
        f"/api/v1/projects/{project.id}/loop-checklist/{target['id']}",
        headers=_hdrs(raw),
        json={"status": "active"},
    )
    assert acc.status_code == 200, acc.text
    body = acc.json()
    assert body["accepted_by_user_id"] is None
    assert body["accepted_actor_type"] == "service_key"
    assert body["accepted_service_key_id"] == key_id
    assert body["accepted_service_key_name"]


@pytest.mark.asyncio
async def test_service_key_proposal_leaves_creator_user_id_null(
    db_session: AsyncSession,
):
    """A finding-derived proposal attributed to a service key keeps the
    creator user id NULL and records the key provenance."""
    owner, _ = await _make_user(db_session)
    project = await _make_project(db_session, owner)

    await propose_from_findings(
        db_session,
        project.id,
        "run_svc",
        [{"class": "new", "title": "Service identity acting as its minter"}],
        None,
        created_actor_type="service_key",
        created_service_key_id="sk_123",
        created_service_key_name="svc-proposer",
    )

    rows = await _items(db_session, project)
    assert len(rows) == 1
    item = rows[0]
    assert item.created_by_user_id is None
    assert item.created_actor_type == "service_key"
    assert item.created_service_key_id == "sk_123"
    assert item.created_service_key_name == "svc-proposer"
    assert item.accepted_by_user_id is None
    assert item.accepted_actor_type is None


@pytest.mark.asyncio
async def test_class_matching_existing_slug_bumps_times_found(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _seed(client, project, key)

    before = (await _items(db_session, project))[0]
    assert before.times_found == 0

    await propose_from_findings(
        db_session,
        project.id,
        "run_3",
        [{"class": before.slug, "title": "whatever", "severity": "medium"}],
        user.id,
    )
    rows = await _items(db_session, project)
    hit = next(r for r in rows if r.slug == before.slug)
    assert hit.times_found == 1
    assert hit.last_found_at is not None


@pytest.mark.asyncio
async def test_status_filter_lists_only_matching_rows(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _seed(client, project, key)

    rows = await _items(db_session, project)
    await client.patch(
        f"/api/v1/projects/{project.id}/loop-checklist/{rows[0].id}",
        headers=_hdrs(key),
        json={"status": "active"},
    )

    active = await client.get(
        f"/api/v1/projects/{project.id}/loop-checklist?status=active",
        headers=_hdrs(key),
    )
    assert active.status_code == 200
    assert [i["status"] for i in active.json()] == ["active"]

    bad = await client.get(
        f"/api/v1/projects/{project.id}/loop-checklist?status=nope",
        headers=_hdrs(key),
    )
    assert bad.status_code == 422


# ── complete_agent_run hook ───────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_agent_run_proposes_from_new_findings(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user)

    cr = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas"},
    )
    run_id = cr.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start",
        headers=_hdrs(key),
    )

    cp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={
            "status": "passed",
            "findings": [{"class": "new", "title": "Service identity acting as its minter"}],
        },
    )
    assert cp.status_code == 200, cp.text

    rows = await _items(db_session, project)
    slugs = {r.slug for r in rows}
    assert "service-identity-acting-as-its-minter" in slugs
    item = next(r for r in rows if r.slug == "service-identity-acting-as-its-minter")
    assert item.source == "finding"
    assert item.times_found == 1
