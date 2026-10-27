"""Admin sweep: ``POST /api/v1/admin/sweeps/confluence-publish`` (GitHub #99).

The sweep pushes wiki pages opted into publishing (``knowledge_pages.publish``)
whose project mapping has ``auto_publish_pages`` and whose org has an enabled
Confluence connection. These tests drive the full route + service against the
same in-memory DB the route tests use, with a fake Confluence client patched
at the service module (the sweep imports the real client directly).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    DocPageLink,
    DocSpaceMapping,
    KnowledgePage,
    OrgAuditEvent,
    OrgDocConnection,
    OrgMember,
    Organization,
    Project,
    User,
)
from sessionfs.server.llm_keys import encrypt_secret
from tests.server.integration.test_confluence_routes import make_fake_confluence

_TOKEN = "atlassian-secret-token-1234"


def _hdrs(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _user(db: AsyncSession, tier: str) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:8]}@example.com",
        display_name="U",
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="k",
            is_active=True,
            key_kind="user",
            scopes=json.dumps(["*"]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


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


async def _project(db: AsyncSession, owner: User, org: Organization) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"p-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:10]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id,
    )
    db.add(project)
    await db.commit()
    return project


_HOUR_AGO = datetime.now(timezone.utc) - timedelta(hours=1)


async def _wiki_page(
    db: AsyncSession,
    project: Project,
    slug: str = "home",
    *,
    publish: bool = True,
    content: str = "hello world",
    updated_at: datetime | None = None,
) -> KnowledgePage:
    page = KnowledgePage(
        id=f"kp_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        slug=slug,
        title=slug.title(),
        page_type="wiki",
        content=content,
        publish=publish,
        updated_at=updated_at or datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(page)
    await db.commit()
    await db.refresh(page)
    return page


async def _connection(
    db: AsyncSession, org: Organization, user: User, *, enabled: bool = True
) -> None:
    db.add(
        OrgDocConnection(
            org_id=org.id,
            site_url="https://acme.atlassian.net",
            account_email="docs@acme.com",
            encrypted_api_token=encrypt_secret(_TOKEN),
            default_template="reference",
            batch_window_minutes=15,
            enabled=enabled,
            connected_by_user_id=user.id,
        )
    )
    await db.commit()


async def _mapping(
    db: AsyncSession,
    org: Organization,
    project: Project,
    creator: User,
    *,
    auto_publish_pages: bool = True,
    publish_context: bool = False,
) -> DocSpaceMapping:
    mapping = DocSpaceMapping(
        id=f"dsm_{uuid.uuid4().hex[:16]}",
        org_id=org.id,
        project_id=project.id,
        space_key="DEV",
        space_id="65538",
        parent_page_id="2001",
        template=None,
        publish_context=publish_context,
        auto_publish_pages=auto_publish_pages,
        created_by_user_id=creator.id,
    )
    db.add(mapping)
    await db.commit()
    return mapping


async def _org_admin(db: AsyncSession, org: Organization, role: str = "admin") -> User:
    user, _ = await _user(db, tier="team")
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role, joined_at=datetime.now(timezone.utc)))
    await db.commit()
    return user


@pytest.fixture
def fake_confluence(monkeypatch):
    return make_fake_confluence(monkeypatch)


async def _setup(
    db: AsyncSession, *, page_publish: bool = True, auto_publish: bool = True,
    conn_enabled: bool = True, slug: str = "home", publish_context: bool = False,
) -> tuple[Organization, Project, User, str, User]:
    org = await _org(db)
    operator, key = await _user(db, tier="admin")
    creator = await _org_admin(db, org)
    project = await _project(db, creator, org)
    await _wiki_page(db, project, slug=slug, publish=page_publish)
    await _connection(db, org, creator, enabled=conn_enabled)
    await _mapping(
        db, org, project, creator,
        auto_publish_pages=auto_publish, publish_context=publish_context,
    )
    return org, project, operator, key, creator


async def _run_sweep(client: AsyncClient, key: str, body: dict | None = None) -> dict:
    resp = await client.post(
        "/api/v1/admin/sweeps/confluence-publish",
        headers=_hdrs(key),
        json=body or {},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _links(db: AsyncSession, project: Project) -> list[DocPageLink]:
    return list((
        await db.execute(
            select(DocPageLink)
            .where(DocPageLink.project_id == project.id)
            .order_by(DocPageLink.source_kind, DocPageLink.source_key)
            .execution_options(populate_existing=True)
        )
    ).scalars().all())


@pytest.mark.asyncio
async def test_sweep_publishes_attributed_to_mapping_creator_and_logs_admin_action(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, project, operator, key, creator = await _setup(db_session)
    result = await _run_sweep(client, key)

    assert result["selected_mappings"] == 1
    assert result["published"] == 1
    assert result["failed"] == 0
    assert result["errors"] == []
    assert fake_confluence.state["calls"]["create_page"] == 1

    [link] = await _links(db_session, project)
    assert (link.status, link.page_id, link.version) == ("linked", "9001", 1)
    # Attributed to the mapping creator (an org admin), never the operator.
    assert link.last_pushed_by_user_id == creator.id
    audit = (await db_session.execute(
        select(OrgAuditEvent).where(
            OrgAuditEvent.org_id == org.id,
            OrgAuditEvent.event_type == "confluence_page_created",
        )
    )).scalar_one()
    assert audit.actor_user_id == creator.id

    action = (
        await db_session.execute(
            select(AdminAction).where(AdminAction.action == "confluence_publish_sweep")
        )
    ).scalar_one()
    assert action.admin_id == operator.id
    assert json.loads(action.details)["published"] == 1

    mapping = (await db_session.execute(
        select(DocSpaceMapping).where(DocSpaceMapping.project_id == project.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert mapping.last_attempted_at is not None


@pytest.mark.asyncio
async def test_sweep_skips_pages_not_opted_in(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, _, key, _ = await _setup(db_session, page_publish=False)
    result = await _run_sweep(client, key)
    assert result["published"] == 0
    assert result["attempted"] == 0


@pytest.mark.asyncio
async def test_sweep_requires_an_opted_in_mapping(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, _, key, _ = await _setup(db_session, auto_publish=False)
    result = await _run_sweep(client, key)
    assert result["selected_mappings"] == 0


@pytest.mark.asyncio
async def test_sweep_requires_enabled_connection(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, _, key, _ = await _setup(db_session, conn_enabled=False)
    result = await _run_sweep(client, key)
    assert result["selected_mappings"] == 0


@pytest.mark.asyncio
async def test_sweep_rerun_with_nothing_changed_is_a_no_op(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, _, key, _ = await _setup(db_session)
    assert (await _run_sweep(client, key))["published"] == 1
    calls_before = dict(fake_confluence.state["calls"])

    second = await _run_sweep(client, key)
    assert second["published"] == 0
    assert second["skipped_unchanged"] == 1
    assert second["attempted"] == 0
    # No Confluence call at all.
    assert fake_confluence.state["calls"] == calls_before


@pytest.mark.asyncio
async def test_sweep_republishes_changed_page_as_update_without_duplicate(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, project, _, key, _ = await _setup(db_session)
    assert (await _run_sweep(client, key))["published"] == 1

    page = (await db_session.execute(
        select(KnowledgePage).where(KnowledgePage.project_id == project.id)
    )).scalar_one()
    page.content = "hello changed world"
    page.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    second = await _run_sweep(client, key)
    assert second["published"] == 1
    calls = fake_confluence.state["calls"]
    assert calls["create_page"] == 1
    assert calls["update_page"] == 1
    assert "hello changed world" in fake_confluence.state["pages"]["9001"]["storage"]


@pytest.mark.asyncio
async def test_sweep_waits_out_the_batch_window(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, project, _, key, _ = await _setup(db_session)
    page = (await db_session.execute(
        select(KnowledgePage).where(KnowledgePage.project_id == project.id)
    )).scalar_one()
    page.updated_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    await db_session.commit()

    result = await _run_sweep(client, key)
    assert result["skipped_recent"] == 1
    assert result["published"] == 0
    assert fake_confluence.state["calls"]["create_page"] == 0


@pytest.mark.asyncio
async def test_sweep_drift_marks_page_drifted(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, project, _, key, _ = await _setup(db_session)
    assert (await _run_sweep(client, key))["published"] == 1
    page = (await db_session.execute(
        select(KnowledgePage).where(KnowledgePage.project_id == project.id)
    )).scalar_one()
    page.content = "edited in SessionFS"
    await db_session.commit()

    fake_confluence.state["page_version"] = 3
    result = await _run_sweep(client, key)
    assert result["drifted"] == 1
    assert result["published"] == 0
    assert result["drifted_pages"] == [
        {"project_id": project.id, "source_kind": "wiki_page", "source_key": "home"}
    ]
    [link] = await _links(db_session, project)
    assert link.status == "drifted"

    # A drifted page is left alone by later sweeps (needs a human resolve).
    again = await _run_sweep(client, key)
    assert again["drifted"] == 1
    assert fake_confluence.state["calls"]["update_page"] == 0


@pytest.mark.asyncio
async def test_sweep_skips_in_flight_claim(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, project, _, key, _ = await _setup(db_session)
    db_session.add(
        DocPageLink(
            id=f"dpl_{uuid.uuid4().hex[:20]}",
            org_id=project.org_id,
            project_id=project.id,
            site_url="https://acme.atlassian.net",
            source_kind="wiki_page",
            source_key="home",
            status="unknown",
            claim_token="x" * 32,
            claimed_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    result = await _run_sweep(client, key)
    assert result["skipped_in_progress"] == 1
    assert result["published"] == 0
    assert fake_confluence.state["calls"]["create_page"] == 0


@pytest.mark.asyncio
async def test_sweep_skips_when_creator_is_no_longer_org_admin(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, _, _, key, creator = await _setup(db_session)
    member = (await db_session.execute(
        select(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == creator.id)
    )).scalar_one()
    member.role = "member"
    await db_session.commit()

    result = await _run_sweep(client, key)
    assert result["skipped_creator_not_admin"] == 1
    assert result["published"] == 0
    assert fake_confluence.state["calls"]["create_page"] == 0


@pytest.mark.asyncio
async def test_sweep_skips_org_without_the_feature(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, _, _, key, _ = await _setup(db_session)
    org.tier = "team"
    await db_session.commit()
    result = await _run_sweep(client, key)
    assert result["skipped_unentitled"] == 1
    assert result["published"] == 0


@pytest.mark.asyncio
async def test_sweep_ignores_mapping_whose_project_left_the_org(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, project, _, key, _ = await _setup(db_session)
    other = await _org(db_session)
    project.org_id = other.id
    await db_session.commit()
    result = await _run_sweep(client, key)
    assert result["selected_mappings"] == 0
    assert fake_confluence.state["calls"]["create_page"] == 0


@pytest.mark.asyncio
async def test_sweep_publishes_the_context_tree(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, project, _, key, _ = await _setup(
        db_session, auto_publish=False, publish_context=True
    )
    project.context_document = "## Architecture\n\nAPI + DB.\n\n## Conventions\n\nruff.\n"
    project.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    result = await _run_sweep(client, key)
    assert result["published"] == 3, result
    links = {(link.source_kind, link.source_key): link for link in await _links(db_session, project)}
    root = links[("context_root", "root")]
    for key_ in ("architecture", "conventions"):
        section = links[("context_section", key_)]
        assert fake_confluence.state["pages"][section.page_id]["parent"] == root.page_id


@pytest.mark.asyncio
async def test_sweep_rotates_mappings_so_none_starves(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, p1, _, key, creator = await _setup(db_session)
    p2 = await _project(db_session, creator, org)
    await _wiki_page(db_session, p2)
    await _mapping(db_session, org, p2, creator)

    first = await _run_sweep(client, key, {"limit": 1})
    second = await _run_sweep(client, key, {"limit": 1})
    assert first["published"] == 1 and second["published"] == 1
    published = {p.id for p in (p1, p2) if await _links(db_session, p)}
    assert published == {p1.id, p2.id}


@pytest.mark.asyncio
async def test_sweep_failing_mapping_does_not_block_others(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, p1, _, key, creator = await _setup(db_session)
    p2 = await _project(db_session, creator, org)
    await _wiki_page(db_session, p2)
    await _mapping(db_session, org, p2, creator)

    fake_confluence.state["create_error"] = True
    first = await _run_sweep(client, key, {"limit": 1})
    assert first["failed"] == 1
    fake_confluence.state["create_error"] = False
    second = await _run_sweep(client, key, {"limit": 1})
    # The mapping that failed was stamped and rotated behind the other one.
    assert second["published"] == 1
    third = await _run_sweep(client, key, {"limit": 1})
    assert third["published"] == 1
    assert all([await _links(db_session, p1), await _links(db_session, p2)])


@pytest.mark.asyncio
async def test_sweep_bounds_work_per_run(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, project, _, key, _ = await _setup(db_session)
    for i in range(3):
        await _wiki_page(db_session, project, slug=f"extra-{i}")
    result = await _run_sweep(client, key, {"limit": 2})
    assert result["published"] == 2
    assert result["deferred"] == 2
    nxt = await _run_sweep(client, key, {"limit": 10})
    assert nxt["published"] == 2
    assert nxt["skipped_unchanged"] == 2


@pytest.mark.asyncio
async def test_sweep_applies_dlp(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    secret = "ghp_" + "c" * 36
    _, project, _, key, _ = await _setup(db_session)
    page = (await db_session.execute(
        select(KnowledgePage).where(KnowledgePage.project_id == project.id)
    )).scalar_one()
    page.content = f"use {secret}"
    await db_session.commit()
    assert (await _run_sweep(client, key))["published"] == 1
    storage = fake_confluence.state["pages"]["9001"]["storage"]
    assert secret not in storage
    assert "[REDACTED:github_token]" in storage


@pytest.mark.asyncio
async def test_sweep_rejects_non_admin_key(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, _, _, _, _ = await _setup(db_session)
    member, member_key = await _user(db_session, tier="team")
    db_session.add(
        OrgMember(
            org_id=org.id,
            user_id=member.id,
            role="admin",
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/admin/sweeps/confluence-publish",
        headers=_hdrs(member_key),
        json={},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_sweep_failing_sources_do_not_starve_the_rest_of_a_mapping(
    client: AsyncClient, db_session: AsyncSession, fake_confluence, monkeypatch
):
    monkeypatch.setattr("sessionfs.server.services.confluence_sweep._PER_MAPPING_MAX", 2)
    _, project, _, key, _ = await _setup(db_session, slug="a-fail")
    await _wiki_page(db_session, project, slug="b-fail")
    await _wiki_page(db_session, project, slug="c-good")
    fake_confluence.state["fail_titles"].update({"A-Fail", "B-Fail"})

    first = await _run_sweep(client, key, {"limit": 10})
    # Both failing pages come first alphabetically/by age and fail.
    assert first["failed"] == 2
    assert first["published"] == 0
    assert first["deferred"] == 1

    second = await _run_sweep(client, key, {"limit": 10})
    # The failures were stamped on their links, so the healthy page goes first.
    assert second["published"] == 1
    links = {link.source_key: link for link in await _links(db_session, project)}
    assert links["c-good"].status == "linked"
    assert links["a-fail"].last_push_failed_at is not None


@pytest.mark.asyncio
async def test_sweep_stops_retrying_a_page_deleted_in_confluence(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, project, _, key, _ = await _setup(db_session)
    assert (await _run_sweep(client, key))["published"] == 1
    page = (await db_session.execute(
        select(KnowledgePage).where(KnowledgePage.project_id == project.id)
    )).scalar_one()
    page.content = "changed"
    await db_session.commit()
    fake_confluence.state["deleted"].add("9001")

    second = await _run_sweep(client, key)
    assert second["failed"] == 1
    assert second["errors"][0]["error"] == "confluence_page_missing"
    [link] = await _links(db_session, project)
    assert link.status == "unlinked"

    calls = dict(fake_confluence.state["calls"])
    third = await _run_sweep(client, key)
    assert third["skipped_unlinked"] == 1
    assert fake_confluence.state["calls"] == calls


@pytest.mark.asyncio
async def test_sweep_republishes_into_the_new_space_after_a_mapping_move(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    from sessionfs.server.integrations.docs.base import DocSpace

    org, project, _, key, _ = await _setup(db_session)
    assert (await _run_sweep(client, key))["published"] == 1
    [old] = await _links(db_session, project)
    assert old.space_key == "DEV"

    admin, admin_key = await _user(db_session, tier="team")
    db_session.add(OrgMember(org_id=org.id, user_id=admin.id, role="admin",
                             joined_at=datetime.now(timezone.utc)))
    await db_session.commit()
    fake_confluence.state["spaces"].append(
        DocSpace(key="OPS", name="Ops", id="65538", homepage_id="1001")
    )
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence/mappings/{project.id}",
        headers=_hdrs(admin_key),
        json={"space_key": "OPS", "parent_page_id": "2001", "auto_publish_pages": True},
    )
    assert resp.status_code == 200, resp.text

    result = await _run_sweep(client, key)
    assert result["published"] == 1, result
    [new] = await _links(db_session, project)
    assert new.status == "linked"
    assert new.space_key == "OPS"
    assert new.page_id != old.page_id
    assert fake_confluence.state["calls"]["create_page"] == 2
