"""GH #99 — Confluence integration routes (org connection/mapping/health +
page/context promotion).

The Confluence client is monkeypatched with an in-memory fake (never a real
Confluence call). Focus:
- org auth matrix: non-Enterprise 403 envelope, member vs admin, other-org
  and outsider 404, service-key rejection;
- page routes: unmapped/not-connected 409, 1:1 link, publish create vs
  update, drift 409 + resolve, unlink audit, timeout → unknown → adopt, and
  the token never appearing in responses or audit.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
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
from sessionfs.server.integrations.docs.base import (
    DocChildPage,
    DocPage,
    DocSpace,
    DocSpacePage,
    DocumentationError,
    DocumentationNotFound,
)
from sessionfs.server.integrations.docs.confluence import validate_site_url

_TOKEN = "atlassian-secret-token-1234"


def _hdrs(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _user(db: AsyncSession, tier: str = "team") -> tuple[User, str]:
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


async def _member(db: AsyncSession, org: Organization, role: str) -> tuple[User, str]:
    user, raw = await _user(db)
    db.add(
        OrgMember(
            org_id=org.id, user_id=user.id, role=role,
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _service_key(
    db: AsyncSession,
    org: Organization,
    minter: User,
    scopes: list[str],
    project_ids: list[str] | None = None,
) -> str:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=minter.id,
            key_hash=hash_api_key(raw),
            name="svc",
            is_active=True,
            key_kind="service",
            org_id=org.id,
            scopes=json.dumps(scopes),
            created_by_user_id=minter.id,
            service_key_name="svc",
            project_ids=json.dumps(project_ids) if project_ids else None,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


async def _project(db: AsyncSession, owner: User, org: Organization | None) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"p-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:10]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
    )
    db.add(project)
    await db.commit()
    return project


async def _wiki_page(
    db: AsyncSession, project: Project, slug: str = "home", content: str = "hello world"
) -> KnowledgePage:
    page = KnowledgePage(
        id=f"kp_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        slug=slug,
        title=slug.title(),
        page_type="wiki",
        content=content,
    )
    db.add(page)
    await db.commit()
    await db.refresh(page)
    return page


async def _insert_connection(db: AsyncSession, org: Organization, user: User) -> None:
    from sessionfs.server.llm_keys import encrypt_secret

    db.add(OrgDocConnection(
        org_id=org.id,
        site_url="https://acme.atlassian.net",
        account_email="docs@acme.com",
        encrypted_api_token=encrypt_secret(_TOKEN),
        default_template="reference",
        enabled=True,
        connected_by_user_id=user.id,
    ))
    await db.commit()


async def _insert_mapping(
    db: AsyncSession, org: Organization, project: Project, user: User
) -> None:
    db.add(DocSpaceMapping(
        id=f"dsm_{uuid.uuid4().hex[:16]}",
        org_id=org.id,
        project_id=project.id,
        space_key="DEV",
        space_id="65538",
        parent_page_id="2001",
        template=None,
        publish_context=False,
        auto_publish_pages=False,
        created_by_user_id=user.id,
    ))
    await db.commit()


# ── fake Confluence client ──


class _FakeList(list):
    """A list of fake clients plus a shared ``state`` dict the tests tune."""

    state: dict


_SPACE_ID = "65538"
_ACCOUNT = "a1"


def make_fake_confluence(monkeypatch, target: str = "sessionfs.server.routes.confluence.ConfluenceClient"):
    """A stateful in-memory Confluence (pages, versions, labels).

    ``state`` knobs: ``page_version`` (forces get_page's version),
    ``create_timeout`` / ``create_error`` / ``label_error`` /
    ``label_timeout`` / ``update_error`` / ``update_hang`` (failure
    injection), ``label_search`` (override label search result),
    ``foreign_parent`` (parent page id reported in another space).
    """
    captured: _FakeList = _FakeList()
    state: dict = {
        "spaces": [DocSpace(key="DEV", name="Dev", id=_SPACE_ID, homepage_id="1001")],
        "space_home": "1001",
        "pages_listing": [DocChildPage(id="2001", title="Child")],
        "page_version": None,
        "pages": {},  # id -> dict(version, title, storage, parent, author, created)
        "labels": {},  # label -> page id
        "next_id": 9001,
        "create_timeout": False,
        "create_error": False,
        "label_error": False,
        "label_timeout": False,
        "update_error": False,
        "update_hang": False,
        "foreign_parent": set(),
        "deleted": set(),  # page ids that 404
        "fail_titles": set(),  # titles whose create is refused
        "unique_titles": False,  # refuse a create whose title already exists (400)
        "create_lost": False,  # create times out and nothing was created
        "calls": {
            "create_page": 0, "update_page": 0, "get_page": 0, "add_label": 0,
            "find_page_by_label": 0, "find_page_by_title": 0,
        },
    }

    class Fake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = validate_site_url(site_url)
            self.account_email = account_email
            self.api_token = api_token
            self.state = state
            self.calls = state["calls"]
            captured.append(self)

        async def aclose(self) -> None:
            pass

        async def validate_connection(self) -> dict:
            return {"account_id": _ACCOUNT, "display_name": "Docs Bot", "email": self.account_email}

        async def search_spaces(self, query, limit) -> DocSpacePage:
            spaces = state["spaces"]
            matched = [
                s for s in spaces
                if not query
                or query.lower() in s.key.lower()
                or query.lower() in s.name.lower()
            ]
            return DocSpacePage(spaces=matched[:limit], has_more=len(matched) > limit)

        async def get_space(self, space_key) -> DocSpace:
            for s in state["spaces"]:
                if s.key == space_key:
                    return s
            raise DocumentationError("Confluence space not found")

        async def search_pages(self, space_key, query, limit) -> list[DocChildPage]:
            pages = state["pages_listing"]
            return [p for p in pages if not query or query.lower() in p.title.lower()][:limit]

        async def get_page(self, page_id, *, include_body=False) -> DocPage:
            state["calls"]["get_page"] += 1
            if page_id in state["deleted"]:
                raise DocumentationNotFound("Confluence API 404: the request was rejected")
            page = state["pages"].get(page_id)
            if page is None:
                # Parent pages the mapping picker validates.
                space = "999" if page_id in state["foreign_parent"] else _SPACE_ID
                return DocPage(id=page_id, version=1, title="Parent", space_id=space)
            version = state["page_version"] or page["version"]
            return DocPage(
                id=page_id,
                version=version,
                title=page["title"],
                space_id=_SPACE_ID,
                parent_id=page["parent"],
                author_id=page["author"],
                created_at=page["created"],
                storage=page["storage"] if include_body else None,
            )

        async def create_page(self, space_id, parent_page_id, title, storage) -> DocPage:
            state["calls"]["create_page"] += 1
            assert space_id == _SPACE_ID
            if state["create_lost"]:
                raise asyncio.TimeoutError("timed out")
            if state["unique_titles"] and any(
                p["title"] == title for p in state["pages"].values()
            ):
                raise DocumentationError(
                    "Confluence API 400: A page with this title already exists",
                    status_code=400,
                )
            if state["create_error"] or title in state["fail_titles"]:
                raise DocumentationError(
                    "Confluence API 400: A page with this title already exists",
                    status_code=400,
                )
            page_id = str(state["next_id"])
            state["next_id"] += 1
            state["pages"][page_id] = {
                "version": 1, "title": title, "storage": storage,
                "parent": parent_page_id, "author": _ACCOUNT,
                "created": datetime.now(timezone.utc).isoformat(),
            }
            if state["create_timeout"]:
                # The page landed but the response never came back.
                raise asyncio.TimeoutError("timed out")
            return DocPage(id=page_id, version=1, title=title, space_id=_SPACE_ID)

        async def add_label(self, page_id, label) -> None:
            state["calls"]["add_label"] += 1
            if state["label_timeout"]:
                raise asyncio.TimeoutError("timed out")
            if state["label_error"]:
                raise DocumentationError("Confluence API 500: label failed")
            state["labels"][label] = page_id

        async def update_page(self, page_id, current_version, title, storage) -> DocPage:
            state["calls"]["update_page"] += 1
            if state["update_hang"]:
                await asyncio.sleep(3600)
            if state["update_error"]:
                raise DocumentationError("Confluence API 500: boom")
            page = state["pages"].setdefault(page_id, {
                "version": current_version, "title": title, "storage": "",
                "parent": "2001", "author": _ACCOUNT,
                "created": datetime.now(timezone.utc).isoformat(),
            })
            page.update(version=current_version + 1, title=title, storage=storage)
            state["page_version"] = None
            return DocPage(id=page_id, version=current_version + 1, title=title, space_id=_SPACE_ID)

        async def find_page_by_label(self, space_key, label) -> str | None:
            state["calls"]["find_page_by_label"] += 1
            if "label_search" in state:
                return state["label_search"]
            return state["labels"].get(label)

        async def find_page_by_title(self, space_id, title) -> str | None:
            state["calls"]["find_page_by_title"] += 1
            for pid, page in state["pages"].items():
                if page["title"] == title:
                    return pid
            return None

    monkeypatch.setattr(target, Fake)
    monkeypatch.setattr(
        "sessionfs.server.routes.confluence._ADOPT_RETRY_DELAY_SECONDS", 0
    )
    captured.state = state
    return captured


@pytest.fixture
def fake_confluence(monkeypatch):
    return make_fake_confluence(monkeypatch)


# ── org connection: auth matrix ──


@pytest.mark.asyncio
async def test_non_enterprise_org_is_gated_with_upgrade_envelope(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session, tier="free")
    _, key = await _member(db_session, org, "admin")
    resp = await client.get(f"/api/v1/orgs/{org.id}/integrations/confluence", headers=_hdrs(key))
    assert resp.status_code == 403
    body = resp.json()["error"]
    assert body["details"]["error"] == "upgrade_required"
    assert body["details"]["feature"] == "confluence_integration"
    assert body["details"]["plan_scope"] == "org"
    assert body["details"]["required_tier"] == "enterprise"


@pytest.mark.asyncio
async def test_member_cannot_read_or_write(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    _, member_key = await _member(db_session, org, "member")

    resp = await client.get(f"/api/v1/orgs/{org.id}/integrations/confluence", headers=_hdrs(member_key))
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "insufficient_role"

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence",
        headers=_hdrs(member_key),
        json={"site_url": "https://acme.atlassian.net", "account_email": "docs@acme.com",
              "api_token": _TOKEN, "default_template": "reference"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "insufficient_role"


@pytest.mark.asyncio
async def test_other_org_and_outsider_get_404(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    other = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    _, other_admin_key = await _member(db_session, other, "admin")
    _, outsider_key = await _user(db_session)

    for key in (other_admin_key, outsider_key):
        resp = await client.get(
            f"/api/v1/orgs/{org.id}/integrations/confluence", headers=_hdrs(key)
        )
        assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_service_keys_are_rejected_on_all_org_confluence_routes(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    admin, _ = await _member(db_session, org, "admin")
    write_key = await _service_key(db_session, org, admin, ["knowledge:read", "knowledge:write"])
    read_key = await _service_key(db_session, org, admin, ["knowledge:read"])

    for key in (write_key, read_key):
        assert (await client.get(
            f"/api/v1/orgs/{org.id}/integrations/confluence", headers=_hdrs(key)
        )).status_code == 403
        assert (await client.get(
            f"/api/v1/orgs/{org.id}/integrations/confluence/mappings", headers=_hdrs(key)
        )).status_code == 403
        assert (await client.get(
            f"/api/v1/orgs/{org.id}/integrations/confluence/health", headers=_hdrs(key)
        )).status_code == 403

    resp = await client.get(f"/api/v1/orgs/{org.id}/integrations/confluence", headers=_hdrs(write_key))
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "service_key_not_allowed"


# ── connection lifecycle + token secrecy ──


@pytest.mark.asyncio
async def test_connection_put_returns_hint_not_token_and_audits(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence",
        headers=_hdrs(key),
        json={"site_url": "https://acme.atlassian.net", "account_email": "docs@acme.com",
              "api_token": _TOKEN, "default_template": "reference"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connected"] is True
    assert body["token_hint"] == "••••1234"
    assert body["has_token"] is True
    assert _TOKEN not in resp.text
    assert "api_token" not in body

    assert fake_confluence and fake_confluence[0].api_token == _TOKEN
    events = (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all()
    assert len(events) == 1
    assert events[0].event_type == "confluence_connection_updated"
    assert _TOKEN not in (events[0].after or "")
    assert _TOKEN not in (events[0].before or "")

    resp = await client.get(f"/api/v1/orgs/{org.id}/integrations/confluence", headers=_hdrs(key))
    assert resp.status_code == 200
    assert _TOKEN not in resp.text


@pytest.mark.asyncio
async def test_connection_put_rejects_unknown_template_and_invalid_site(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    base = {"site_url": "https://acme.atlassian.net", "account_email": "docs@acme.com",
            "api_token": _TOKEN, "default_template": "reference"}

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence", headers=_hdrs(key),
        json={**base, "default_template": "nope"},
    )
    assert resp.status_code == 422

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence", headers=_hdrs(key),
        json={**base, "site_url": "https://evil.example.com"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "confluence_connection_failed"


@pytest.mark.asyncio
async def test_connection_delete_removes_and_audits(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    resp = await client.delete(f"/api/v1/orgs/{org.id}/integrations/confluence", headers=_hdrs(key))
    assert resp.status_code == 204
    assert (await db_session.execute(
        select(OrgDocConnection).where(OrgDocConnection.org_id == org.id)
    )).scalar_one_or_none() is None
    events = (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all()
    assert events[-1].event_type == "confluence_connection_deleted"
    assert _TOKEN not in (events[-1].before or "")


# ── mappings + pickers + health ──


@pytest.mark.asyncio
async def test_mapping_lifecycle_and_audit(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    project = await _project(db_session, user, org)

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence/mappings/{project.id}",
        headers=_hdrs(key),
        json={"space_key": "DEV", "parent_page_id": "2001", "template": "runbook",
              "publish_context": True, "auto_publish_pages": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["space_key"] == "DEV"
    assert body["parent_page_id"] == "2001"
    assert body["template"] == "runbook"

    listed = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/confluence/mappings", headers=_hdrs(key)
    )).json()
    assert [m["project_id"] for m in listed] == [project.id]

    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/integrations/confluence/mappings/{project.id}", headers=_hdrs(key)
    )
    assert resp.status_code == 204
    types = {e.event_type for e in (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all()}
    assert {"confluence_mapping_updated", "confluence_mapping_deleted"} <= types


@pytest.mark.asyncio
async def test_mapping_rejects_parent_page_is_space_root(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    project = await _project(db_session, user, org)

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence/mappings/{project.id}",
        headers=_hdrs(key),
        json={"space_key": "DEV", "parent_page_id": "1001"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "parent_page_is_space_root"


@pytest.mark.asyncio
async def test_spaces_pages_and_templates_pickers(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)

    spaces = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/confluence/spaces", headers=_hdrs(key)
    )).json()
    assert spaces == [{"key": "DEV", "name": "Dev"}]

    pages = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/confluence/spaces/DEV/pages", headers=_hdrs(key)
    )).json()
    assert pages == [{"id": "2001", "title": "Child"}]

    templates = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/confluence/templates", headers=_hdrs(key)
    )).json()
    assert {t["slug"] for t in templates} == {"reference", "architecture", "runbook", "onboarding", "concept"}


@pytest.mark.asyncio
async def test_health_reports_unmapped_and_empty_sets(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    mapped = await _project(db_session, user, org)
    await _project(db_session, user, org)  # unmapped
    await _insert_mapping(db_session, org, mapped, user)

    health = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/confluence/health", headers=_hdrs(key)
    )).json()
    assert health["unmapped_project_count"] == 1
    assert health["last_push_failures"] == []
    assert health["publishes_in_progress"] == []
    assert health["publishes_unknown"] == []
    assert health["drifted_links"] == []


# ── page promotion ──


async def _ready(
    db: AsyncSession, *, tier: str = "enterprise"
) -> tuple[Organization, User, str, Project, KnowledgePage]:
    org = await _org(db, tier=tier)
    user, key = await _member(db, org, "admin")
    await _insert_connection(db, org, user)
    project = await _project(db, user, org)
    await _insert_mapping(db, org, project, user)
    page = await _wiki_page(db, project)
    return org, user, key, project, page


def _publish_body(slug: str = "home", *, title: str = "Home") -> dict:
    return {
        "kind": "wiki_page",
        "slug": slug,
        "title": title,
        "sections": [
            {"key": "overview", "heading": "Overview",
             "paragraphs": [{"text": "hello world", "citations": ["page_body"]}]},
        ],
        "confirm": True,
    }


@pytest.mark.asyncio
async def test_unmapped_project_409(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    project = await _project(db_session, user, org)
    await _wiki_page(db_session, project)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/preview",
        headers=_hdrs(key),
        json={"kind": "wiki_page", "slug": "home"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "confluence_mapping_required"


@pytest.mark.asyncio
async def test_not_connected_409(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    project = await _project(db_session, user, org)
    await _wiki_page(db_session, project)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/preview",
        headers=_hdrs(key),
        json={"kind": "wiki_page", "slug": "home"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "confluence_not_connected"


@pytest.mark.asyncio
async def test_project_route_gates_on_org_tier(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session, tier="free")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/preview",
        headers=_hdrs(key),
        json={"kind": "wiki_page", "slug": "home"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "upgrade_required"
    assert resp.json()["error"]["details"]["plan_scope"] == "project"


@pytest.mark.asyncio
async def test_preview_returns_deterministic_draft(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/preview",
        headers=_hdrs(key),
        json={"kind": "wiki_page", "slug": "home"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Home"
    assert body["source_kind"] == "wiki_page"
    assert body["source_key"] == "home"
    assert body["template"]["slug"] == "reference"
    assert body["llm_used"] is False
    assert body["key_source"] == "none"
    assert [s["key"] for s in body["sections"]] == [
        "overview", "details", "claims", "evidence", "related",
    ]
    assert "existing" not in body


@pytest.mark.asyncio
async def test_publish_creates_then_updates_with_1to1_link_and_audit(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, user, key, project, _ = await _ready(db_session)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/publish", headers=_hdrs(key),
        json=_publish_body(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["page_id"] == "9001"
    assert body["page_url"] == "https://acme.atlassian.net/wiki/spaces/DEV/pages/9001"

    links = (
        await db_session.execute(select(DocPageLink).where(DocPageLink.project_id == project.id))
    ).scalars().all()
    assert len(links) == 1
    assert links[0].status == "linked"
    assert links[0].page_id == "9001"

    # Second publish updates (not creates).
    resp2 = await client.post(
        f"/api/v1/projects/{project.id}/confluence/publish", headers=_hdrs(key),
        json=_publish_body(),
    )
    assert resp2.status_code == 200
    assert resp2.json()["page_id"] == "9001"
    links = (
        await db_session.execute(select(DocPageLink).where(DocPageLink.project_id == project.id))
    ).scalars().all()
    assert len(links) == 1

    types = [e.event_type for e in (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all()]
    assert types == ["confluence_page_created", "confluence_page_updated"]

    for e in (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all():
        assert _TOKEN not in (e.after or "")
        assert _TOKEN not in (e.before or "")


@pytest.mark.asyncio
async def test_publish_requires_confirm_and_valid_sections(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    url = f"/api/v1/projects/{project.id}/confluence/publish"

    resp = await client.post(url, headers=_hdrs(key), json={**_publish_body(), "confirm": False})
    assert resp.status_code == 400

    resp = await client.post(url, headers=_hdrs(key), json={
        **_publish_body(), "sections": [{"key": "not_a_section", "paragraphs": []}],
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_drift_detection_and_resolve(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, user, key, project, _ = await _ready(db_session)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/publish", headers=_hdrs(key),
        json=_publish_body(),
    )
    assert resp.status_code == 200, resp.text

    # Remote page edited since the last push → drift 409.
    fake_confluence[-1].state["page_version"] = 3
    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/publish", headers=_hdrs(key),
        json=_publish_body(),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["error"] == "confluence_page_drifted"
    assert resp.json()["error"]["details"]["remote_version"] == 3

    link = (
        await db_session.execute(select(DocPageLink).where(DocPageLink.project_id == project.id))
    ).scalars().one()
    assert link.status == "drifted"

    # Overwrite resolves the drift.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/links/{link.id}/resolve",
        headers=_hdrs(key),
        json={"action": "overwrite"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "linked"
    assert resp.json()["version"] == 3
    await db_session.refresh(link)
    assert link.status == "linked"
    assert link.version == 3


@pytest.mark.asyncio
async def test_unlink_flow_requires_project_admin(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, user, key, project, _ = await _ready(db_session)
    member, member_key = await _member(db_session, org, "member")

    await client.post(
        f"/api/v1/projects/{project.id}/confluence/publish", headers=_hdrs(key),
        json=_publish_body(),
    )
    link = (
        await db_session.execute(select(DocPageLink).where(DocPageLink.project_id == project.id))
    ).scalars().one()

    resp = await client.delete(
        f"/api/v1/projects/{project.id}/confluence/links/{link.id}", headers=_hdrs(member_key)
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["error"] == "insufficient_role"

    resp = await client.delete(
        f"/api/v1/projects/{project.id}/confluence/links/{link.id}", headers=_hdrs(key)
    )
    assert resp.status_code == 204
    assert (await db_session.execute(
        select(DocPageLink).where(DocPageLink.project_id == project.id)
    )).scalar_one_or_none() is None

    types = [e.event_type for e in (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id))
    ).scalars().all()]
    assert "confluence_unlinked" in types


@pytest.mark.asyncio
async def test_timeout_marks_unknown_then_adopts(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, user, key, project, _ = await _ready(db_session)
    url = f"/api/v1/projects/{project.id}/confluence/publish"

    fake_confluence.state["create_timeout"] = True
    resp = await client.post(url, headers=_hdrs(key), json=_publish_body())
    assert resp.status_code == 504
    assert resp.json()["error"]["details"]["error"] == "confluence_timeout"

    link = (
        await db_session.execute(select(DocPageLink).where(DocPageLink.project_id == project.id))
    ).scalars().one()
    assert link.status == "unknown"
    assert link.page_id is None
    assert link.claim_token is None

    # The create landed remotely but never answered and never got its label:
    # the retry finds it by exact title (authored by our account, created
    # after the unknown publish began), labels it and updates — no duplicate.
    fake_confluence.state["create_timeout"] = False
    resp = await client.post(url, headers=_hdrs(key), json=_publish_body())
    assert resp.status_code == 200, resp.text
    assert resp.json()["page_id"] == "9001"

    await db_session.refresh(link)
    assert link.status == "linked"
    assert link.page_id == "9001"
    calls = fake_confluence.state["calls"]
    assert calls["create_page"] == 1
    assert calls["update_page"] == 1
    assert len(fake_confluence.state["pages"]) == 1
    assert list(fake_confluence.state["labels"].values()) == ["9001"]


# ── helpers for the review-fix tests ──


async def _link(db: AsyncSession, project: Project) -> DocPageLink:
    return (
        await db.execute(
            select(DocPageLink)
            .where(DocPageLink.project_id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().one()


async def _publish(client: AsyncClient, key: str, project: Project, **overrides):
    return await client.post(
        f"/api/v1/projects/{project.id}/confluence/publish",
        headers=_hdrs(key),
        json={**_publish_body(), **overrides},
    )


def _err(resp) -> str | None:
    body = resp.json().get("error") or {}
    details = body.get("details")
    if isinstance(details, dict):
        return details.get("error") or details.get("code")
    return body.get("code")


# ── finding 1: escaping through the route ──


@pytest.mark.asyncio
async def test_publish_escapes_hostile_heading_title_and_link_text(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    hostile = '<ac:structured-macro ac:name="html"><ac:plain-text-body>]]><x/></ac:plain-text-body></ac:structured-macro> & co'
    resp = await _publish(
        client, key, project,
        title=f"T {hostile}"[:200],
        sections=[{
            "key": "overview",
            "heading": hostile[:200],
            "paragraphs": [{"text": f"[{hostile[:300]}](https://a.b) and {hostile}", "citations": ["page_body"]}],
        }],
    )
    assert resp.status_code == 200, resp.text
    storage = fake_confluence.state["pages"]["9001"]["storage"]
    assert '<ac:structured-macro ac:name="html"' not in storage
    assert "<x/>" not in storage
    assert "&amp; co" in storage
    assert "&lt;ac:structured-macro" in storage


# ── finding 2: transfer/merge drop + sweep filter + stale mapping row ──


@pytest.mark.asyncio
async def test_drop_integration_mappings_removes_mapping_and_links(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    from sessionfs.server.services.tracker_scope import drop_project_integration_mappings

    _, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    await drop_project_integration_mappings(db_session, project.id)
    await db_session.commit()
    assert (await db_session.execute(
        select(DocSpaceMapping).where(DocSpaceMapping.project_id == project.id)
    )).scalar_one_or_none() is None
    assert (await db_session.execute(
        select(DocPageLink).where(DocPageLink.project_id == project.id)
    )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_project_transfer_drops_confluence_mapping(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, user, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    # Owner moves the project to personal scope (auto-accepted).
    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        headers=_hdrs(key),
        json={"to": "personal"},
    )
    assert resp.status_code in (200, 201), resp.text
    assert (await db_session.execute(
        select(DocSpaceMapping).where(DocSpaceMapping.project_id == project.id)
    )).scalar_one_or_none() is None
    assert (await db_session.execute(
        select(DocPageLink).where(DocPageLink.project_id == project.id)
    )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_mapping_put_replaces_stale_row_from_another_org(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, user, key, project, _ = await _ready(db_session)
    old_org = await _org(db_session)
    # A row left under another org for the same project (unique project_id).
    mapping = (await db_session.execute(
        select(DocSpaceMapping).where(DocSpaceMapping.project_id == project.id)
    )).scalar_one()
    mapping.org_id = old_org.id
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence/mappings/{project.id}",
        headers=_hdrs(key),
        json={"space_key": "DEV", "parent_page_id": "2001"},
    )
    assert resp.status_code == 200, resp.text
    rows = (await db_session.execute(
        select(DocSpaceMapping)
        .where(DocSpaceMapping.project_id == project.id)
        .execution_options(populate_existing=True)
    )).scalars().all()
    assert [r.org_id for r in rows] == [org.id]
    assert rows[0].space_id == "65538"


# ── finding 3: links bound to org + site ──


@pytest.mark.asyncio
async def test_link_on_another_site_is_stale_everywhere_but_unlink(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, user, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    link = await _link(db_session, project)
    assert link.site_url == "https://acme.atlassian.net"
    link.site_url = "https://other.atlassian.net"
    await db_session.commit()
    before = dict(fake_confluence.state["calls"])

    resp = await _publish(client, key, project)
    assert resp.status_code == 409
    assert _err(resp) == "confluence_link_stale"

    # Preview still answers so the dashboard can offer Unlink: the link is
    # reported stale, nothing remote is rendered, no Confluence call.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/preview",
        headers=_hdrs(key), json={"kind": "wiki_page", "slug": "home"},
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["link"]["stale"] is True
    assert preview["link"]["page_url"] is None
    assert preview["link"]["id"] == link.id
    assert "existing" not in preview
    assert "other.atlassian.net" not in resp.text

    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/links/{link.id}/resolve",
        headers=_hdrs(key), json={"action": "overwrite"},
    )
    assert resp.status_code == 409
    assert _err(resp) == "confluence_link_stale"
    # No outbound call touched the (other site's) page.
    assert fake_confluence.state["calls"] == before

    links = (await client.get(
        f"/api/v1/projects/{project.id}/confluence/links", headers=_hdrs(key)
    )).json()
    assert links["links"][0]["stale"] is True
    assert links["links"][0]["page_url"] is None
    assert links["mapping"]["space_key"] == "DEV"

    resp = await client.delete(
        f"/api/v1/projects/{project.id}/confluence/links/{link.id}", headers=_hdrs(key)
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_link_from_another_org_is_stale(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, user, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    other = await _org(db_session)
    link = await _link(db_session, project)
    link.org_id = other.id
    await db_session.commit()
    resp = await _publish(client, key, project)
    assert resp.status_code == 409
    assert _err(resp) == "confluence_link_stale"


# ── finding 4/5: adoption, failures after create, the overall deadline ──


@pytest.mark.asyncio
async def test_create_and_label_send_real_shapes(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    from sessionfs.server.integrations.docs.confluence import sessionfs_label

    _, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    label = sessionfs_label(project.id, "wiki_page", "home")
    assert fake_confluence.state["labels"] == {label: "9001"}
    assert fake_confluence.state["pages"]["9001"]["parent"] == "2001"


@pytest.mark.asyncio
async def test_failure_after_create_keeps_link_unknown_then_adopts_by_id(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    fake_confluence.state["label_error"] = True
    resp = await _publish(client, key, project)
    assert resp.status_code == 502
    link = await _link(db_session, project)
    # The page exists: the row is never deleted, it remembers the page id.
    assert link.status == "unknown"
    assert link.page_id == "9001"
    assert link.claim_token is None

    fake_confluence.state["label_error"] = False
    resp = await _publish(client, key, project)
    assert resp.status_code == 200, resp.text
    assert resp.json()["page_id"] == "9001"
    assert fake_confluence.state["calls"]["create_page"] == 1
    # Adopted by stored id: no label/title search needed.
    assert fake_confluence.state["calls"]["find_page_by_label"] == 0
    link = await _link(db_session, project)
    assert (link.status, link.page_id) == ("linked", "9001")


@pytest.mark.asyncio
async def test_timeout_after_create_keeps_page_id_and_retry_never_duplicates(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    fake_confluence.state["label_timeout"] = True
    resp = await _publish(client, key, project)
    assert resp.status_code == 504
    link = await _link(db_session, project)
    assert (link.status, link.page_id) == ("unknown", "9001")

    fake_confluence.state["label_timeout"] = False
    for _ in range(2):
        resp = await _publish(client, key, project)
        assert resp.status_code == 200, resp.text
    assert fake_confluence.state["calls"]["create_page"] == 1
    assert len(fake_confluence.state["pages"]) == 1


@pytest.mark.asyncio
async def test_adopt_refuses_page_by_another_author(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    fake_confluence.state["create_timeout"] = True
    assert (await _publish(client, key, project)).status_code == 504
    fake_confluence.state["create_timeout"] = False
    fake_confluence.state["pages"]["9001"]["author"] = "someone-else"
    resp = await _publish(client, key, project)
    assert resp.status_code == 409
    assert _err(resp) == "confluence_adopt_rejected"
    link = await _link(db_session, project)
    assert link.status == "unknown"
    assert link.claim_token is None
    assert fake_confluence.state["calls"]["create_page"] == 1


@pytest.mark.asyncio
async def test_whole_outbound_sequence_shares_one_deadline(
    client: AsyncClient, db_session: AsyncSession, fake_confluence, monkeypatch
):
    _, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    monkeypatch.setattr(
        "sessionfs.server.routes.confluence._PUBLISH_DEADLINE_SECONDS", 0.2
    )
    fake_confluence.state["update_hang"] = True
    resp = await _publish(client, key, project)
    assert resp.status_code == 504
    assert _err(resp) == "confluence_timeout"
    link = await _link(db_session, project)
    # An update that timed out releases its claim; the link stays linked.
    assert link.status == "linked"
    assert link.claim_token is None
    assert link.last_push_failure


# ── finding 7: update claims ──


@pytest.mark.asyncio
async def test_live_claim_on_linked_row_blocks_publish_without_false_drift(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    link = await _link(db_session, project)
    # Another publish of the same source is mid-flight and has already bumped
    # the remote version.
    link.claim_token = "c" * 32
    link.claimed_at = datetime.now(timezone.utc)
    await db_session.commit()
    fake_confluence.state["page_version"] = 2

    resp = await _publish(client, key, project)
    assert resp.status_code == 409
    assert _err(resp) == "publish_in_progress"
    link = await _link(db_session, project)
    assert link.status == "linked"
    assert fake_confluence.state["calls"]["get_page"] == 0


@pytest.mark.asyncio
async def test_concurrent_publishes_of_one_source_never_report_drift(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200

    original = fake_confluence.state
    gate = asyncio.Event()

    results = []

    async def slow_publish():
        results.append(await _publish(client, key, project))

    # First publish holds its claim inside update_page until released.
    orig_hang = original["update_hang"]
    original["update_hang"] = False

    import sessionfs.server.routes.confluence as cr

    real_cls = cr.ConfluenceClient

    class Slow(real_cls):  # type: ignore[misc,valid-type]
        async def update_page(self, *a, **kw):
            await gate.wait()
            return await super().update_page(*a, **kw)

    cr.ConfluenceClient = Slow
    try:
        first = asyncio.create_task(slow_publish())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if (await _link(db_session, project)).claim_token is not None:
                break
        second = await _publish(client, key, project)
        gate.set()
        await first
    finally:
        cr.ConfluenceClient = real_cls
        original["update_hang"] = orig_hang

    assert results[0].status_code == 200, results[0].text
    assert second.status_code == 409
    assert _err(second) == "publish_in_progress"
    link = await _link(db_session, project)
    assert link.status == "linked"
    assert link.version == 2


@pytest.mark.asyncio
async def test_unlink_and_resolve_refuse_live_claim_but_honour_stale_timeout(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    link = await _link(db_session, project)
    link.claim_token = "c" * 32
    link.claimed_at = datetime.now(timezone.utc)
    await db_session.commit()
    base = f"/api/v1/projects/{project.id}/confluence/links/{link.id}"

    resp = await client.delete(base, headers=_hdrs(key))
    assert resp.status_code == 409
    resp = await client.post(f"{base}/resolve", headers=_hdrs(key), json={"action": "unlink"})
    assert resp.status_code == 409

    from datetime import timedelta

    link = await _link(db_session, project)
    link.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await db_session.commit()
    resp = await client.post(f"{base}/resolve", headers=_hdrs(key), json={"action": "unlink"})
    assert resp.status_code == 200, resp.text
    link = await _link(db_session, project)
    assert link.status == "unlinked"
    assert link.claim_token is None


# ── finding 8: template echo ──


@pytest.mark.asyncio
async def test_publish_template_echo_mismatch_is_409(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/confluence/preview",
        headers=_hdrs(key), json={"kind": "wiki_page", "slug": "home"},
    )).json()
    assert preview["template"] == {"slug": "reference", "name": "Reference", "version": None}

    resp = await _publish(client, key, project, template_version=2)
    assert resp.status_code == 409
    assert _err(resp) == "template_changed"

    # The mapping's template moved after the preview: the echo no longer
    # matches the resolution → 409, nothing sent.
    mapping = (await db_session.execute(
        select(DocSpaceMapping).where(DocSpaceMapping.project_id == project.id)
    )).scalar_one()
    mapping.template = "concept"
    await db_session.commit()
    resp = await _publish(client, key, project, template="reference", template_version=None)
    assert resp.status_code == 409
    assert _err(resp) == "template_changed"
    assert resp.json()["error"]["details"]["template"]["slug"] == "concept"
    assert fake_confluence.state["calls"]["create_page"] == 0

    # Echoing the current resolution publishes.
    resp = await _publish(client, key, project, template="concept", template_version=None)
    assert resp.status_code == 200, resp.text
    assert resp.json()["template"]["slug"] == "concept"


@pytest.mark.asyncio
async def test_publish_template_echo_detects_connection_default_change(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, _, key, project, _ = await _ready(db_session)
    conn = (await db_session.execute(
        select(OrgDocConnection).where(OrgDocConnection.org_id == org.id)
    )).scalar_one()
    conn.default_template = "runbook"
    await db_session.commit()
    resp = await _publish(client, key, project, template="reference", template_version=None)
    assert resp.status_code == 409
    assert _err(resp) == "template_changed"


@pytest.mark.asyncio
async def test_publish_explicit_template_override_is_distinct_from_the_echo(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    # A deliberate pick, echoed back as previewed with that override.
    resp = await _publish(
        client, key, project,
        template_override="concept", template="concept", template_version=None,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["template"]["slug"] == "concept"
    link = await _link(db_session, project)
    assert link.template_slug == "concept"

    # An override with a stale echo still 409s.
    resp = await _publish(
        client, key, project, template_override="concept", template="reference",
    )
    assert resp.status_code == 409
    assert _err(resp) == "template_changed"

    resp = await _publish(client, key, project, template_override="nope")
    assert resp.status_code == 422


# ── finding 9: DLP ──


@pytest.mark.asyncio
async def test_publish_and_preview_apply_dlp_redaction(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    secret = "ghp_" + "b" * 36
    _, _, key, project, page = await _ready(db_session)
    page.content = f"deploy with {secret}"
    await db_session.commit()

    preview = await client.post(
        f"/api/v1/projects/{project.id}/confluence/preview",
        headers=_hdrs(key), json={"kind": "wiki_page", "slug": "home"},
    )
    assert preview.status_code == 200
    assert secret not in preview.text

    resp = await _publish(
        client, key, project,
        title=f"Home {secret}",
        sections=[{"key": "overview", "heading": "Overview",
                   "paragraphs": [{"text": f"token {secret}", "citations": ["page_body"]}]}],
    )
    assert resp.status_code == 200, resp.text
    stored = fake_confluence.state["pages"]["9001"]
    assert secret not in stored["storage"]
    assert secret not in stored["title"]
    assert "[REDACTED:github_token]" in stored["storage"]


# ── finding 11/12/13: drift diff, preview extras, parent space, footer ──


@pytest.mark.asyncio
async def test_drift_409_carries_bounded_diff(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    page = fake_confluence.state["pages"]["9001"]
    page["storage"] = page["storage"].replace("hello world", "hello edited world")
    page["version"] = 2
    resp = await _publish(client, key, project)
    assert resp.status_code == 409
    details = resp.json()["error"]["details"]
    assert details["error"] == "confluence_page_drifted"
    assert details["link_id"]
    assert "-hello world" in details["diff"]
    assert "+hello edited world" in details["diff"]
    assert details["diff_truncated"] is False


@pytest.mark.asyncio
async def test_preview_reports_notes_dropped_and_link(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    url = f"/api/v1/projects/{project.id}/confluence/preview"
    body = (await client.post(url, headers=_hdrs(key), json={"kind": "wiki_page", "slug": "home"})).json()
    assert body["dropped"] == []
    assert any("No model key" in n for n in body["notes"])
    assert body["link"] is None

    assert (await _publish(client, key, project)).status_code == 200
    body = (await client.post(url, headers=_hdrs(key), json={"kind": "wiki_page", "slug": "home"})).json()
    assert body["link"]["status"] == "linked"
    assert body["link"]["stale"] is False
    assert body["existing"]["page_id"] == "9001"


@pytest.mark.asyncio
async def test_mapping_parent_must_be_in_the_mapped_space(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org = await _org(db_session)
    user, key = await _member(db_session, org, "admin")
    await _insert_connection(db_session, org, user)
    project = await _project(db_session, user, org)
    fake_confluence.state["foreign_parent"].add("3003")
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence/mappings/{project.id}",
        headers=_hdrs(key),
        json={"space_key": "DEV", "parent_page_id": "3003"},
    )
    assert resp.status_code == 422
    assert _err(resp) == "parent_page_not_in_space"

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence/mappings/{project.id}",
        headers=_hdrs(key),
        json={"space_key": "DEV", "parent_page_id": "not-a-number"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_footer_counts_only_active_claims(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    from sessionfs.server.db.models import KnowledgeEntry

    _, user, key, project, _ = await _ready(db_session)

    def entry(**kw):
        base = dict(
            project_id=project.id, session_id="manual", user_id=user.id,
            entry_type="decision", content=f"c-{uuid.uuid4().hex}",
            confidence=0.9, claim_class="claim", freshness_class="current",
            dismissed=False,
        )
        base.update(kw)
        return KnowledgeEntry(**base)

    db_session.add_all([
        entry(), entry(), entry(freshness_class="aging"), entry(freshness_class="stale"),
        entry(claim_class="note"),
        entry(dismissed=True),
        entry(freshness_class="superseded", superseded_by=1),
    ])
    await db_session.commit()
    assert (await _publish(client, key, project)).status_code == 200
    storage = fake_confluence.state["pages"]["9001"]["storage"]
    assert "Shaped by 4 active claims (2 current, 1 aging, 1 stale)" in storage


# ── finding 14/15/16 ──


@pytest.mark.asyncio
async def test_resolve_unlink_writes_the_unlink_audit_event(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    link = await _link(db_session, project)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/links/{link.id}/resolve",
        headers=_hdrs(key), json={"action": "unlink"},
    )
    assert resp.status_code == 200
    events = (await db_session.execute(
        select(OrgAuditEvent).where(
            OrgAuditEvent.org_id == org.id,
            OrgAuditEvent.event_type == "confluence_unlinked",
        )
    )).scalars().all()
    assert len(events) == 1
    assert json.loads(events[0].before)["page_id"] == "9001"


@pytest.mark.asyncio
async def test_oversized_raw_content_is_422_before_any_outbound_call(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    big = "x" * 10000
    sections = [
        {"key": k, "heading": k, "paragraphs": [{"text": big, "citations": []}] * 50}
        for k in ("overview", "details", "claims")
    ]
    resp = await _publish(client, key, project, sections=sections)
    assert resp.status_code == 422
    assert _err(resp) == "content_too_large_for_confluence"
    assert fake_confluence.state["calls"]["create_page"] == 0
    assert (await db_session.execute(
        select(DocPageLink).where(DocPageLink.project_id == project.id)
    )).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_health_failures_cover_last_24h_only(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    from datetime import timedelta

    org, user, key, project, _ = await _ready(db_session)
    now = datetime.now(timezone.utc)
    for slug, when in (("recent", now - timedelta(hours=2)), ("old", now - timedelta(days=2))):
        db_session.add(DocPageLink(
            id=f"dpl_{uuid.uuid4().hex[:20]}", org_id=org.id, project_id=project.id,
            site_url="https://acme.atlassian.net",
            source_kind="wiki_page", source_key=slug, status="linked",
            page_id="1" if slug == "recent" else "2",
            version=1, last_push_failed_at=when, last_push_failure="boom",
        ))
    await db_session.commit()
    health = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/confluence/health", headers=_hdrs(key)
    )).json()
    assert [f["source_key"] for f in health["last_push_failures"]] == ["recent"]


# ── finding 17: multi-repo session evidence ──


@pytest.mark.asyncio
async def test_session_evidence_only_lists_sessions_attributed_to_the_project(
    db_session: AsyncSession,
):
    """Sessions synced into the project (from any of its repos) are listed;
    sessions that only share a remote — another user's, possibly outside the
    org or without a grant — are never published."""
    from sessionfs.server.db.models import Session
    from sessionfs.server.routes.confluence import _session_evidence

    org = await _org(db_session)
    user, _ = await _member(db_session, org, "admin")
    outsider, _ = await _user(db_session)
    project = await _project(db_session, user, org)
    now = datetime.now(timezone.utc)
    for title, owner, remote, project_id in (
        ("primary work", user, project.git_remote_normalized, project.id),
        ("second repo work", user, "github.com/acme/second-repo", project.id),
        ("outsider on the same remote", outsider, project.git_remote_normalized, None),
        ("unrelated", user, "github.com/other/x", None),
    ):
        db_session.add(Session(
            id=f"ses_{uuid.uuid4().hex[:16]}", user_id=owner.id, title=title, tags="[]",
            source_tool="claude-code", blob_key="x", blob_size_bytes=0, etag="x",
            git_remote_normalized=remote, project_id=project_id,
            created_at=now, updated_at=now, uploaded_at=now,
        ))
    await db_session.commit()
    evidence = await _session_evidence(project, db_session)
    assert set(evidence) == {"primary work", "second repo work"}


# ── route authorization ──


@pytest.mark.asyncio
async def test_restricted_member_needs_edit_standing_and_group_editor_is_allowed(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    from sessionfs.server.db.models import (
        ProjectGroup,
        ProjectGroupAccess,
        ProjectGroupMember,
    )

    org, _, _, project, _ = await _ready(db_session)
    reader, reader_key = await _member(db_session, org, "member")
    editor, editor_key = await _member(db_session, org, "member")
    plain, plain_key = await _member(db_session, org, "member")
    group = ProjectGroup(
        id=f"pg_{uuid.uuid4().hex[:12]}", org_id=org.id, slug="g", name="G",
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    db_session.add(group)
    await db_session.flush()
    db_session.add(ProjectGroupAccess(group_id=group.id, project_id=project.id))
    db_session.add(ProjectGroupMember(group_id=group.id, user_id=reader.id, role="reader"))
    db_session.add(ProjectGroupMember(group_id=group.id, user_id=editor.id, role="editor"))
    await db_session.commit()

    url = f"/api/v1/projects/{project.id}/confluence"
    # Unrestricted org: a plain member is an editor.
    resp = await client.post(f"{url}/preview", headers=_hdrs(plain_key),
                             json={"kind": "wiki_page", "slug": "home"})
    assert resp.status_code == 200, resp.text

    org.settings = json.dumps({"general": {"restrict_project_visibility": True}})
    await db_session.commit()

    # A group reader can see the project but has no edit standing.
    resp = await client.post(f"{url}/preview", headers=_hdrs(reader_key),
                             json={"kind": "wiki_page", "slug": "home"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "project_edit_forbidden"
    # A plain member can no longer even see it: 404, nothing leaks.
    resp = await client.post(f"{url}/preview", headers=_hdrs(plain_key),
                             json={"kind": "wiki_page", "slug": "home"})
    assert resp.status_code == 404, resp.text
    resp = await client.post(f"{url}/publish", headers=_hdrs(reader_key), json=_publish_body())
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "project_edit_forbidden"

    resp = await client.post(f"{url}/publish", headers=_hdrs(editor_key), json=_publish_body())
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_cross_org_project_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, _, project, _ = await _ready(db_session)
    other = await _org(db_session)
    _, other_key = await _member(db_session, other, "admin")
    for path, body in (("preview", {"kind": "wiki_page", "slug": "home"}), ("publish", _publish_body())):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/confluence/{path}",
            headers=_hdrs(other_key), json=body,
        )
        assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_service_key_is_denied_on_project_publish_routes(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, admin, _, project, _ = await _ready(db_session)
    svc = await _service_key(db_session, org, admin, ["knowledge:read", "knowledge:write"])
    for path, body in (("preview", {"kind": "wiki_page", "slug": "home"}), ("publish", _publish_body())):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/confluence/{path}",
            headers=_hdrs(svc), json=body,
        )
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "service_key_not_allowed"
    assert fake_confluence.state["calls"]["create_page"] == 0


@pytest.mark.asyncio
async def test_publish_while_another_publish_holds_fresh_claim_409(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, _, key, project, _ = await _ready(db_session)
    db_session.add(DocPageLink(
        id=f"dpl_{uuid.uuid4().hex[:20]}", org_id=org.id, project_id=project.id,
        site_url="https://acme.atlassian.net", source_kind="wiki_page", source_key="home",
        status="unknown", claim_token="z" * 32, claimed_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    resp = await _publish(client, key, project)
    assert resp.status_code == 409
    assert _err(resp) == "publish_in_progress"
    assert fake_confluence.state["calls"]["create_page"] == 0


@pytest.mark.asyncio
async def test_context_section_nests_under_linked_context_root(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    project.context_document = "## Architecture\n\nAPI + DB.\n\n## Conventions\n\nruff.\n"
    await db_session.commit()
    url = f"/api/v1/projects/{project.id}/confluence"
    root = (await client.post(f"{url}/preview", headers=_hdrs(key), json={"kind": "context"})).json()
    resp = await client.post(f"{url}/publish", headers=_hdrs(key), json={
        "kind": "context", "title": root["title"],
        "sections": [{"key": s["key"], "heading": s["heading"], "paragraphs": s["paragraphs"]} for s in root["sections"]],
        "confirm": True, "template": root["template"]["slug"], "template_version": None,
    })
    assert resp.status_code == 200, resp.text
    root_page = resp.json()["page_id"]

    sec = (await client.post(f"{url}/preview", headers=_hdrs(key),
                             json={"kind": "context", "slug": "architecture"})).json()
    resp = await client.post(f"{url}/publish", headers=_hdrs(key), json={
        "kind": "context", "slug": "architecture", "title": sec["title"],
        "sections": [{"key": s["key"], "heading": s["heading"], "paragraphs": s["paragraphs"]} for s in sec["sections"]],
        "confirm": True,
    })
    assert resp.status_code == 200, resp.text
    assert fake_confluence.state["pages"][resp.json()["page_id"]]["parent"] == root_page
    assert sec["title"].startswith(project.name)


# ── re-review fixes ──


@pytest.mark.asyncio
async def test_preview_on_stale_link_returns_200_with_stale_link(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    link = await _link(db_session, project)
    link.site_url = "https://old.atlassian.net"
    await db_session.commit()
    calls = dict(fake_confluence.state["calls"])
    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/preview",
        headers=_hdrs(key), json={"kind": "wiki_page", "slug": "home"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["link"]["stale"] is True
    assert "existing" not in resp.json()
    assert fake_confluence.state["calls"] == calls
    # Publish still refuses it.
    assert _err(await _publish(client, key, project)) == "confluence_link_stale"


@pytest.mark.asyncio
async def test_mapping_resave_reattributes_to_the_saving_admin(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, creator, _, project, _ = await _ready(db_session)
    other, other_key = await _member(db_session, org, "admin")
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/integrations/confluence/mappings/{project.id}",
        headers=_hdrs(other_key),
        json={"space_key": "DEV", "parent_page_id": "2001"},
    )
    assert resp.status_code == 200, resp.text
    mapping = (await db_session.execute(
        select(DocSpaceMapping)
        .where(DocSpaceMapping.project_id == project.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert mapping.created_by_user_id == other.id != creator.id


@pytest.mark.asyncio
async def test_deleted_confluence_page_unlinks_with_audit_then_republish_creates(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    fake_confluence.state["deleted"].add("9001")

    resp = await _publish(client, key, project)
    assert resp.status_code == 409
    assert _err(resp) == "confluence_page_missing"
    link = await _link(db_session, project)
    assert (link.status, link.page_id, link.claim_token) == ("unlinked", None, None)
    events = (await db_session.execute(
        select(OrgAuditEvent).where(
            OrgAuditEvent.org_id == org.id,
            OrgAuditEvent.event_type == "confluence_page_missing",
        )
    )).scalars().all()
    assert len(events) == 1
    assert json.loads(events[0].before)["page_id"] == "9001"

    # The next publish creates a fresh page.
    resp = await _publish(client, key, project)
    assert resp.status_code == 200, resp.text
    assert resp.json()["page_id"] == "9002"
    assert resp.json()["outcome"] == "created"


@pytest.mark.asyncio
async def test_deleted_page_on_unknown_link_is_unlinked_too(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    fake_confluence.state["label_error"] = True
    assert (await _publish(client, key, project)).status_code == 502
    link = await _link(db_session, project)
    assert (link.status, link.page_id) == ("unknown", "9001")
    fake_confluence.state["label_error"] = False
    fake_confluence.state["deleted"].add("9001")
    resp = await _publish(client, key, project)
    assert _err(resp) == "confluence_page_missing"
    link = await _link(db_session, project)
    assert link.status == "unlinked"


@pytest.mark.asyncio
async def test_resolve_overwrite_on_deleted_page_unlinks(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    fake_confluence.state["page_version"] = 4
    assert _err(await _publish(client, key, project)) == "confluence_page_drifted"
    link = await _link(db_session, project)
    fake_confluence.state["deleted"].add("9001")
    resp = await client.post(
        f"/api/v1/projects/{project.id}/confluence/links/{link.id}/resolve",
        headers=_hdrs(key), json={"action": "overwrite"},
    )
    assert resp.status_code == 409
    assert _err(resp) == "confluence_page_missing"
    assert (await _link(db_session, project)).status == "unlinked"


@pytest.mark.asyncio
async def test_refused_create_is_failed_and_retry_never_adopts(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    fake_confluence.state["create_error"] = True
    resp = await _publish(client, key, project)
    assert resp.status_code == 409
    assert _err(resp) == "confluence_create_refused"
    link = await _link(db_session, project)
    assert link.status == "failed"
    assert link.claim_token is None
    assert link.last_push_failed_at is not None
    assert "title already exists" in (link.last_push_failure or "")

    fake_confluence.state["create_error"] = False
    resp = await _publish(client, key, project)
    assert resp.status_code == 200, resp.text
    assert resp.json()["outcome"] == "created"
    calls = fake_confluence.state["calls"]
    # A definitive refusal never leads to a label/title adoption.
    assert calls["find_page_by_label"] == 0
    assert calls["find_page_by_title"] == 0


@pytest.mark.asyncio
async def test_same_title_in_two_projects_never_shares_a_page(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, user, key, project_a, _ = await _ready(db_session)
    project_b = await _project(db_session, user, org)
    await _insert_mapping(db_session, org, project_b, user)
    await _wiki_page(db_session, project_b)  # also "Home" under the same parent
    fake_confluence.state["unique_titles"] = True

    assert (await _publish(client, key, project_a)).status_code == 200
    for _ in range(2):
        resp = await _publish(client, key, project_b)
        assert resp.status_code == 409
        assert _err(resp) == "confluence_create_refused"
    link_b = await _link(db_session, project_b)
    assert link_b.status == "failed"
    assert link_b.page_id is None
    assert fake_confluence.state["calls"]["update_page"] == 0
    assert len(fake_confluence.state["pages"]) == 1


@pytest.mark.asyncio
async def test_timeout_then_title_match_owned_by_another_source_is_409(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, user, key, project_a, _ = await _ready(db_session)
    project_b = await _project(db_session, user, org)
    await _insert_mapping(db_session, org, project_b, user)
    await _wiki_page(db_session, project_b)
    assert (await _publish(client, key, project_a)).status_code == 200

    # B's create times out with nothing created: B is unknown, so the retry
    # may adopt — but the only title match is A's page.
    fake_confluence.state["create_lost"] = True
    assert (await _publish(client, key, project_b)).status_code == 504
    assert (await _link(db_session, project_b)).status == "unknown"
    fake_confluence.state["create_lost"] = False
    resp = await _publish(client, key, project_b)
    assert resp.status_code == 409
    assert _err(resp) == "confluence_title_conflict"
    link_b = await _link(db_session, project_b)
    assert (link_b.status, link_b.page_id) == ("failed", None)
    assert fake_confluence.state["calls"]["update_page"] == 0
    assert (await _link(db_session, project_a)).page_id == "9001"


@pytest.mark.asyncio
async def test_duplicate_wiki_titles_in_one_project_get_distinct_pages(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, page = await _ready(db_session)
    twin = await _wiki_page(db_session, project, slug="home-2")
    twin.title = page.title
    await db_session.commit()
    fake_confluence.state["unique_titles"] = True
    url = f"/api/v1/projects/{project.id}/confluence"
    titles = []
    for slug in ("home", "home-2"):
        preview = (await client.post(f"{url}/preview", headers=_hdrs(key),
                                     json={"kind": "wiki_page", "slug": slug})).json()
        titles.append(preview["title"])
        resp = await _publish(client, key, project, slug=slug, title=preview["title"])
        assert resp.status_code == 200, resp.text
    assert titles == ["Home (home)", "Home (home-2)"]
    assert len(fake_confluence.state["pages"]) == 2


@pytest.mark.asyncio
async def test_mapping_space_change_drops_old_space_links_with_audit(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    org, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    link = await _link(db_session, project)
    assert link.space_key == "DEV"

    fake_confluence.state["spaces"].append(
        DocSpace(key="OPS", name="Ops", id="65538", homepage_id="1001")
    )
    url = f"/api/v1/orgs/{org.id}/integrations/confluence/mappings/{project.id}"

    # An in-flight publish blocks the move.
    link.claim_token = "q" * 32
    link.claimed_at = datetime.now(timezone.utc)
    await db_session.commit()
    resp = await client.put(url, headers=_hdrs(key), json={"space_key": "OPS", "parent_page_id": "2001"})
    assert resp.status_code == 409
    assert _err(resp) == "publish_in_progress"
    # The conditional delete refused the claimed row and the whole save
    # rolled back: the mapping and the in-flight claim are untouched.
    mapping = (await db_session.execute(
        select(DocSpaceMapping)
        .where(DocSpaceMapping.project_id == project.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert mapping.space_key == "DEV"
    assert (await _link(db_session, project)).claim_token == "q" * 32

    link = await _link(db_session, project)
    link.claim_token = None
    link.claimed_at = None
    await db_session.commit()
    resp = await client.put(url, headers=_hdrs(key), json={"space_key": "OPS", "parent_page_id": "2001"})
    assert resp.status_code == 200, resp.text
    # The old-space link row is gone (audited), so the next publish — or the
    # sweep — creates a fresh page in the new space.
    assert (await db_session.execute(
        select(DocPageLink).where(DocPageLink.project_id == project.id)
    )).scalar_one_or_none() is None
    event = (await db_session.execute(
        select(OrgAuditEvent).where(
            OrgAuditEvent.org_id == org.id,
            OrgAuditEvent.event_type == "confluence_links_dropped",
        )
    )).scalar_one()
    assert json.loads(event.before)["space_key"] == "DEV"
    assert json.loads(event.before)["links"][0]["page_id"] == "9001"
    assert json.loads(event.after)["space_key"] == "OPS"

    # Re-saving the same space touches nothing.
    resp = await client.put(url, headers=_hdrs(key), json={"space_key": "OPS", "parent_page_id": "2001"})
    assert resp.status_code == 200

    # The next publish creates a page in the new space, and its URL uses it.
    resp = await _publish(client, key, project)
    assert resp.status_code == 200, resp.text
    assert "/wiki/spaces/OPS/pages/" in resp.json()["page_url"]


@pytest.mark.asyncio
async def test_link_page_url_uses_the_links_own_space(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    assert (await _publish(client, key, project)).status_code == 200
    link = await _link(db_session, project)
    link.space_key = "LEGACY"
    await db_session.commit()
    body = (await client.get(
        f"/api/v1/projects/{project.id}/confluence/links", headers=_hdrs(key)
    )).json()
    assert body["links"][0]["page_url"].endswith("/wiki/spaces/LEGACY/pages/9001")


@pytest.mark.asyncio
async def test_preview_template_override_and_deprecated_alias(
    client: AsyncClient, db_session: AsyncSession, fake_confluence
):
    _, _, key, project, _ = await _ready(db_session)
    url = f"/api/v1/projects/{project.id}/confluence/preview"
    for body in ({"template_override": "concept"}, {"template": "concept"}):
        resp = await client.post(url, headers=_hdrs(key),
                                 json={"kind": "wiki_page", "slug": "home", **body})
        assert resp.status_code == 200, resp.text
        assert resp.json()["template"]["slug"] == "concept"
    resp = await client.post(url, headers=_hdrs(key),
                             json={"kind": "wiki_page", "slug": "home", "template_override": "nope"})
    assert resp.status_code == 422
