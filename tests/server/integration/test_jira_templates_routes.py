# ruff: noqa: F811 - fixtures are imported from test_jira_routes and used as parameters
"""GH #98 part A — org-custom Jira templates (routes, inheritance, footer).

Covers the template CRUD + versioning surface, the built-in read-only listing,
per-org isolation, and the ticket-promotion inheritance chain (per-ticket
override → mapping override → connection default) with the resolved slug +
version pinned on the link. The Jira client is monkeypatched; no real Jira
call is made.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    JiraTemplate,
    JiraTemplateVersion,
    TicketTrackerLink,
    TrackerProjectMapping,
)
from sessionfs.server.integrations.trackers.base import TrackerIssue

from tests.server.integration.test_jira_routes import (  # noqa: F401 - fixtures
    _hdrs,
    _insert_connection,
    _insert_mapping,
    _member,
    _org,
    _project,
    _ready,
    _ticket,
    fake_jira,
)

_TEMPLATES = "/api/v1/orgs/{org_id}/integrations/jira/templates"


def _body(**over) -> dict:
    body = {
        "slug": "design-lite",
        "name": "Design Lite",
        "description": "A trimmed design story",
        "sections": [
            {
                "key": "summary",
                "heading": "Summary",
                "mode": "rewrite",
                "source_fields": ["title", "description"],
                "guidance": "Lead with the why.",
            },
            {
                "key": "acceptance_criteria",
                "heading": "Acceptance Criteria",
                "mode": "verbatim",
                "source_fields": ["acceptance_criteria"],
            },
        ],
    }
    body.update(over)
    return body


async def _create_template(
    client: AsyncClient, org_id: str, key: str, body: dict | None = None
):
    return await client.post(
        _TEMPLATES.format(org_id=org_id), headers=_hdrs(key), json=body or _body()
    )


async def _link(db: AsyncSession, ticket_id: str) -> TicketTrackerLink | None:
    return (
        await db.execute(
            select(TicketTrackerLink)
            .where(TicketTrackerLink.ticket_id == ticket_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _adf_text(adf: dict) -> str:
    texts: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                texts.append(node["text"])
            for child in node.get("content", []):
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(adf)
    return " ".join(texts)


# ── template CRUD + versioning ──


@pytest.mark.asyncio
async def test_template_crud_lifecycle_and_versioning(
    client: AsyncClient, db_session: AsyncSession
):
    org, user, key = await _member_admin(db_session)

    resp = await _create_template(client, org.id, key)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["slug"] == "design-lite"
    assert created["builtin"] is False
    assert created["version"] == 1
    assert created["sections"][0]["guidance"] == "Lead with the why."

    # Built-ins are listed first, read-only, and the custom one appears too.
    listed = (await client.get(_TEMPLATES.format(org_id=org.id), headers=_hdrs(key))).json()
    builtin_slugs = {t["slug"] for t in listed if t["builtin"]}
    assert {"agile", "compliance", "sre", "bug", "design"} <= builtin_slugs
    custom = [t for t in listed if t["slug"] == "design-lite"]
    assert len(custom) == 1 and custom[0]["version"] == 1

    # GET by slug.
    got = (await client.get(
        f"{_TEMPLATES.format(org_id=org.id)}/design-lite", headers=_hdrs(key)
    )).json()
    assert got["version"] == 1

    # PUT bumps the version and snapshots the old one.
    updated = _body(name="Design Lite v2", sections=[
        {"key": "summary", "heading": "Summary", "mode": "rewrite",
         "source_fields": ["title"]},
    ])
    resp = await client.put(
        f"{_TEMPLATES.format(org_id=org.id)}/design-lite",
        headers=_hdrs(key), json=updated,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2
    assert resp.json()["name"] == "Design Lite v2"

    versions = (await client.get(
        f"{_TEMPLATES.format(org_id=org.id)}/design-lite/versions", headers=_hdrs(key)
    )).json()
    assert [v["version"] for v in versions] == [2, 1]

    # Archive (soft delete): the template is still fetchable, but no longer
    # editable.
    assert (await client.delete(
        f"{_TEMPLATES.format(org_id=org.id)}/design-lite", headers=_hdrs(key)
    )).status_code == 204
    got = (await client.get(
        f"{_TEMPLATES.format(org_id=org.id)}/design-lite", headers=_hdrs(key)
    )).json()
    assert got["archived_at"] is not None
    resp = await client.put(
        f"{_TEMPLATES.format(org_id=org.id)}/design-lite",
        headers=_hdrs(key), json=updated,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "template_archived"

    template_row = (
        await db_session.execute(
            select(JiraTemplate).where(
                JiraTemplate.org_id == org.id, JiraTemplate.slug == "design-lite"
            )
        )
    ).scalar_one()
    rows = (
        await db_session.execute(
            select(JiraTemplateVersion)
            .where(JiraTemplateVersion.template_id == template_row.id)
            .order_by(JiraTemplateVersion.version.desc())
        )
    ).scalars().all()
    # Each PUT snapshots the prior version, so v1 + v2 both survive.
    assert [r.version for r in rows] == [2, 1]


@pytest.mark.asyncio
async def test_template_validation_422(client: AsyncClient, db_session: AsyncSession):
    org, user, key = await _member_admin(db_session)

    # Built-in slug collision.
    resp = await _create_template(client, org.id, key, _body(slug="agile"))
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "template_validation"
    assert resp.json()["error"]["details"]["field"] == "slug"

    # Unknown source field.
    resp = await _create_template(client, org.id, key, _body(sections=[
        {"key": "s", "heading": "S", "mode": "rewrite", "source_fields": ["nope"]},
    ]))
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["field"] == "sections[0].source_fields[0]"

    # Duplicate slug within the org.
    assert (await _create_template(client, org.id, key)).status_code == 201
    resp = await _create_template(client, org.id, key)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["error"] == "template_slug_taken"


@pytest.mark.asyncio
async def test_template_routes_require_org_admin(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "member")
    assert (await client.get(
        _TEMPLATES.format(org_id=org.id), headers=_hdrs(key)
    )).status_code == 403
    assert (await client.post(
        _TEMPLATES.format(org_id=org.id), headers=_hdrs(key), json=_body()
    )).status_code == 403


@pytest.mark.asyncio
async def test_template_org_isolation_and_builtin_readonly(
    client: AsyncClient, db_session: AsyncSession
):
    org_a, _user_a, key_a = await _member_admin(db_session)
    org_b, _user_b, key_b = await _member_admin(db_session)
    await _create_template(client, org_a.id, key_a)

    # Cross-org reads/writes are 404 (a different org can reuse the slug).
    assert (await client.get(
        f"{_TEMPLATES.format(org_id=org_b.id)}/design-lite", headers=_hdrs(key_b)
    )).status_code == 404
    assert (await client.put(
        f"{_TEMPLATES.format(org_id=org_b.id)}/design-lite", headers=_hdrs(key_b),
        json=_body(),
    )).status_code == 404
    assert (await client.delete(
        f"{_TEMPLATES.format(org_id=org_b.id)}/design-lite", headers=_hdrs(key_b)
    )).status_code == 404

    # Built-ins are read-only.
    assert (await client.get(
        f"{_TEMPLATES.format(org_id=org_a.id)}/agile", headers=_hdrs(key_a)
    )).json()["builtin"] is True
    assert (await client.put(
        f"{_TEMPLATES.format(org_id=org_a.id)}/agile", headers=_hdrs(key_a), json=_body()
    )).status_code == 422
    assert (await client.delete(
        f"{_TEMPLATES.format(org_id=org_a.id)}/agile", headers=_hdrs(key_a)
    )).status_code == 422
    assert (await client.get(
        f"{_TEMPLATES.format(org_id=org_a.id)}/agile/versions", headers=_hdrs(key_a)
    )).status_code == 404


@pytest.mark.asyncio
async def test_template_preview_is_deterministic(
    client: AsyncClient, db_session: AsyncSession
):
    org, user, key = await _member_admin(db_session)
    project = await _project(db_session, user, org)
    await _insert_mapping(db_session, org, project, user)
    ticket = await _ticket(db_session, project, user)
    await _create_template(client, org.id, key)

    resp = await client.post(
        f"{_TEMPLATES.format(org_id=org.id)}/design-lite/preview",
        headers=_hdrs(key),
        json={"project_id": project.id, "ticket_id": ticket.id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"] == "Ship the auth migration"
    assert body["template"] == {"slug": "design-lite", "name": "Design Lite", "version": 1}
    keys = {s["key"] for s in body["sections"]}
    assert {"summary", "acceptance_criteria"} <= keys
    # Deterministic — every paragraph cites its source and no LLM field is set.
    summary_section = next(s for s in body["sections"] if s["key"] == "summary")
    assert summary_section["mode"] == "rewrite"
    assert any("title" in p["citations"] for p in summary_section["paragraphs"])
    assert "llm_used" not in body


# ── inheritance + link pinning ──


async def _member_admin(db: AsyncSession):
    org = await _org(db)
    user, key = await _member(db, org, "admin")
    return org, user, key


@pytest.mark.asyncio
async def test_inheritance_ticket_overrides_mapping_overrides_default(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key = await _member_admin(db_session)
    await _insert_connection(db_session, org, user)  # default_template = "agile"
    project = await _project(db_session, user, org)
    await _insert_mapping(db_session, org, project, user)  # template = None
    ticket = await _ticket(db_session, project, user)

    # Connection default (agile) resolves when no override is given.
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    assert preview["template"]["slug"] == "agile"
    assert preview["template"]["version"] is None

    # A per-ticket override for an unknown slug is a strict 422.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview",
        headers=_hdrs(key), json={"template": "nope"},
    )
    assert resp.status_code == 422

    # Create a custom template and pin it on the mapping; a per-ticket override
    # still wins.
    await _create_template(client, org.id, key)
    mapping = (
        await db_session.execute(
            select(TrackerProjectMapping).where(
                TrackerProjectMapping.project_id == project.id
            )
        )
    ).scalar_one()
    mapping.template = "design-lite"
    await db_session.commit()

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    assert preview["template"] == {"slug": "design-lite", "name": "Design Lite", "version": 1}

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview",
        headers=_hdrs(key), json={"template": "bug"},
    )).json()
    assert preview["template"]["slug"] == "bug"


@pytest.mark.asyncio
async def test_publish_pins_slug_and_version_and_re_publish_tracks_newer(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key = await _member_admin(db_session)
    await _insert_connection(db_session, org, user)
    project = await _project(db_session, user, org)
    await _insert_mapping(db_session, org, project, user)
    ticket = await _ticket(db_session, project, user)
    await _create_template(client, org.id, key)

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview",
        headers=_hdrs(key), json={"template": "design-lite"},
    )).json()
    publish_body = {"summary": preview["summary"], "sections": preview["sections"], "confirm": True,
                    "template": "design-lite",
                    "template_version": preview["template"]["version"]}
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=publish_body,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["template"] == {"slug": "design-lite", "name": "Design Lite", "version": 1}

    link = await _link(db_session, ticket.id)
    assert link.template_slug == "design-lite"
    assert link.template_version == 1

    # Bump the template to v2 and re-publish; the link pins the newer version.
    updated = _body(sections=[
        {"key": "summary", "heading": "Summary", "mode": "rewrite", "source_fields": ["title"]},
    ])
    await client.put(
        f"{_TEMPLATES.format(org_id=org.id)}/design-lite", headers=_hdrs(key), json=updated
    )
    # Re-preview against the bumped template so the sections match v2, then
    # publish again; the link now pins the newer version.
    preview2 = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview",
        headers=_hdrs(key), json={"template": "design-lite"},
    )).json()
    publish_body2 = {"summary": preview2["summary"], "sections": preview2["sections"], "confirm": True,
                     "template": "design-lite",
                     "template_version": preview2["template"]["version"]}
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=publish_body2,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["template"]["version"] == 2
    link = await _link(db_session, ticket.id)
    assert link.template_slug == "design-lite"
    assert link.template_version == 2


@pytest.mark.asyncio
async def test_archived_template_cannot_be_selected_but_link_keeps_working(
    client: AsyncClient, db_session: AsyncSession, fake_jira
):
    org, user, key = await _member_admin(db_session)
    await _insert_connection(db_session, org, user)
    project = await _project(db_session, user, org)
    await _insert_mapping(db_session, org, project, user)
    ticket = await _ticket(db_session, project, user)
    await _create_template(client, org.id, key)

    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview",
        headers=_hdrs(key), json={"template": "design-lite"},
    )).json()
    publish_body = {"summary": preview["summary"], "sections": preview["sections"], "confirm": True,
                    "template": "design-lite",
                    "template_version": preview["template"]["version"]}
    assert (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=publish_body,
    )).status_code == 200

    # Archive the template the link is pinned to.
    assert (await client.delete(
        f"{_TEMPLATES.format(org_id=org.id)}/design-lite", headers=_hdrs(key)
    )).status_code == 204

    # A new selection of the archived slug (another ticket) is refused; the
    # pinned ticket itself may keep it.
    other = await _ticket(db_session, project, user)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{other.id}/jira/preview",
        headers=_hdrs(key), json={"template": "design-lite"},
    )
    assert resp.status_code == 422
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview",
        headers=_hdrs(key), json={"template": "design-lite"},
    )
    assert resp.status_code == 200

    # The existing link keeps working and still carries its pinned provenance.
    assert (await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira", headers=_hdrs(key)
    )).status_code == 200
    link = await _link(db_session, ticket.id)
    assert link.template_slug == "design-lite"
    assert link.template_version == 1

    # Health surfaces the archived reference as a warning.
    health = (await client.get(
        f"/api/v1/orgs/{org.id}/integrations/jira/health", headers=_hdrs(key)
    )).json()
    assert {"ticket_id": ticket.id, "template_slug": "design-lite", "template_version": 1} in (
        health["archived_template_references"]
    )


# ── footer ──


@pytest.fixture
def recording_fake(monkeypatch):
    created: list[dict] = []

    class Fake:
        def __init__(self, site_url, account_email, api_token, *, transport=None):
            self.site_url = site_url
            self.account_email = account_email
            self.api_token = api_token

        async def aclose(self) -> None:
            pass

        async def validate_connection(self) -> dict:
            return {"account_id": "a1", "display_name": "N", "email": self.account_email}

        async def create_issue(self, project_key, issue_type, summary, adf, labels=None, extra_fields=None):
            created.append({"summary": summary, "adf": adf})
            return TrackerIssue(key="ACME-1", url="https://acme.atlassian.net/browse/ACME-1", status="")

        async def update_issue(self, issue_key, summary, adf, extra_fields=None):
            created.append({"summary": summary, "adf": adf})
            return TrackerIssue(key=issue_key, url=f"https://acme.atlassian.net/browse/{issue_key}", status="")

    monkeypatch.setattr("sessionfs.server.routes.jira.JiraClient", Fake)
    return created


def _footer(adf: dict) -> dict:
    """The last top-level block of a published description is the footer."""
    return adf["content"][-1]


@pytest.mark.asyncio
async def test_footer_links_the_source_ticket_when_dashboard_url_is_set(
    client: AsyncClient, db_session: AsyncSession, recording_fake, monkeypatch
):
    monkeypatch.setattr(
        "sessionfs.server.routes.jira.dashboard_link_base",
        lambda config: "https://dash.example.com",
    )
    org, user, key, project, ticket = await _ready(db_session)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview", headers=_hdrs(key)
    )).json()
    publish_body = {"summary": preview["summary"], "sections": preview["sections"], "confirm": True}
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key), json=publish_body,
    )
    assert resp.status_code == 200, resp.text
    url = f"https://dash.example.com/projects/{project.id}?tab=work&sub=tickets&ticket={ticket.id}"
    footer = _footer(recording_fake[0]["adf"])
    assert "".join(n["text"] for n in footer["content"]) == (
        f"Generated by SessionFS · template: Agile Standard · source ticket {url}"
    )
    # The URL is a real ADF link, not bare text.
    link_node = footer["content"][-1]
    assert link_node["text"] == url
    assert link_node["marks"] == [{"type": "link", "attrs": {"href": url}}]


@pytest.mark.asyncio
async def test_footer_is_always_present_without_a_dashboard_url(
    client: AsyncClient, db_session: AsyncSession, recording_fake, monkeypatch
):
    monkeypatch.setattr("sessionfs.server.routes.jira.dashboard_link_base", lambda config: "")
    org, user, key, project, ticket = await _ready(db_session)
    await _create_template(client, org.id, key)
    preview = (await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/preview",
        headers=_hdrs(key), json={"template": "design-lite"},
    )).json()
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key),
        json={"summary": preview["summary"], "sections": preview["sections"], "confirm": True,
              "template": "design-lite", "template_version": 1},
    )
    assert resp.status_code == 200, resp.text
    footer = _footer(recording_fake[0]["adf"])
    # Template + version provenance always; no link and no "source ticket".
    assert footer["content"] == [
        {"type": "text", "text": "Generated by SessionFS · template: Design Lite v1"}
    ]
    assert "source ticket" not in _adf_text(recording_fake[0]["adf"])


@pytest.mark.asyncio
async def test_footer_counts_toward_the_adf_budget(
    client: AsyncClient, db_session: AsyncSession, recording_fake, monkeypatch
):
    """A body that fits on its own but not with the footer is a 422 before
    any outbound call."""
    from sessionfs.server.integrations.trackers.custom_templates import builtin_resolved
    from sessionfs.server.integrations.trackers.promote import (
        adf_text_length,
        sections_to_adf,
    )
    from sessionfs.server.routes.jira import (
        _MAX_ADF_DESCRIPTION_CHARS,
        _footer_adf,
        _ticket_url,
    )

    dash = "https://dash.example.com"
    monkeypatch.setattr("sessionfs.server.routes.jira.dashboard_link_base", lambda config: dash)
    org, user, key, project, ticket = await _ready(db_session)
    mode_by_key = {"summary": "rewrite"}

    def body_sections(total: int) -> list[dict]:
        paragraphs = []
        remaining = total
        while remaining > 0:
            chunk = min(remaining, 10000)
            paragraphs.append({"text": "x" * chunk, "citations": ["title"]})
            remaining -= chunk
        return [{"key": "summary", "heading": "Summary", "paragraphs": paragraphs}]

    footer_len = adf_text_length({
        "type": "doc", "version": 1,
        "content": _footer_adf(
            builtin_resolved("agile"), _ticket_url(dash, project.id, ticket.id)
        ),
    })
    base_overhead = adf_text_length(sections_to_adf(body_sections(1), mode_by_key)) - 1
    # Largest raw size that still fits WITHOUT the footer, then confirm the
    # footer tips it over.
    raw = _MAX_ADF_DESCRIPTION_CHARS - base_overhead
    alone = adf_text_length(sections_to_adf(body_sections(raw), mode_by_key))
    assert alone <= _MAX_ADF_DESCRIPTION_CHARS < alone + footer_len

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key),
        json={"summary": "S", "sections": body_sections(raw), "confirm": True},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["details"]["error"] == "content_too_large_for_jira"
    assert recording_fake == []  # nothing reached Jira

    # With room for the footer it publishes.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/jira/publish",
        headers=_hdrs(key),
        json={"summary": "S", "sections": body_sections(raw - footer_len), "confirm": True},
    )
    assert resp.status_code == 200, resp.text
    assert adf_text_length(recording_fake[0]["adf"]) <= _MAX_ADF_DESCRIPTION_CHARS
