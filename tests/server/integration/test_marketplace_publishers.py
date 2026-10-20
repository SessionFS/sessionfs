"""Marketplace publishers (§4): namespace claims, lookalike gate, verification
(DNS TXT and GitHub App), re-check sweeper, public publishing moderation
(D1), suspension, and authorization (cross-org, service keys)."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import tarfile
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server import github_app
from sessionfs.server.db.models import (
    GitHubInstallation,
    OrgAuditEvent,
    OrgMember,
    OrgDomainVerification,
    SkillMarketplaceListing,
    SkillPackage,
    SkillPublisher,
    SkillPublisherVerification,
    User,
)
from sessionfs.server.routes import webhooks
from sessionfs.server.services import domain_verification
from sessionfs.server.services import publishers as svc
from sessionfs.skills.package import AGENT_PLUGIN_SCHEMA
from tests.server.integration.test_marketplace_moderation import _join, _org, _service, _user

pytestmark = pytest.mark.asyncio


def _installation_event(action: str, installation_id: int, login: str, account_id: int) -> dict:
    """A full installation webhook payload (the webhook validates its shape)."""
    return {
        "action": action,
        "installation": {
            "id": installation_id,
            "app_id": 1,
            "target_type": "Organization",
            "account": {"id": account_id, "login": login, "type": "Organization"},
        },
    }


@pytest.fixture(autouse=True)
def _fresh_publisher_limiters(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app-layer limiters are process-wide; give every test its own."""
    from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
    from sessionfs.server.routes import marketplace_publishers as routes

    monkeypatch.setattr(routes, "_exchange_ip_limiter", SlidingWindowRateLimiter(60, 60))
    monkeypatch.setattr(routes, "_exchange_repo_limiter", SlidingWindowRateLimiter(30, 600))
    # Claims are rate-limited per user (10/hour in production); the suite
    # exercises many refused claims per test, so it uses a roomier window.
    monkeypatch.setattr(routes, "_claim_limiter", SlidingWindowRateLimiter(1000, 3600))


def _pkg(*, name: str = "review-helper", scripts: bool = False, body: str = "Inspect the diff.") -> bytes:
    output = io.BytesIO()
    manifest = {
        "$schema": AGENT_PLUGIN_SCHEMA,
        "name": name,
        "version": "1.0.0",
        "description": "Review a change.",
        "license": "MIT",
    }
    files = {
        "plugin.json": json.dumps(manifest).encode(),
        f"skills/{name}/SKILL.md": (
            f"---\nname: {name}\ndescription: Review changes.\n---\n\n# Helper\n\n{body}\n"
        ).encode(),
    }
    if scripts:
        files[f"skills/{name}/scripts/run.sh"] = b"#!/bin/sh\necho hi\n"
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, payload in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


async def _claim(client: AsyncClient, headers: dict, namespace: str, **extra) -> dict:
    response = await client.post("/api/v1/publishers", headers=headers, json={"namespace": namespace, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _error(response) -> dict:
    body = response.json()
    return body.get("error") or body.get("detail") or body


async def _verify_dns(
    client: AsyncClient, headers: dict, publisher_id: str, domain: str, monkeypatch: pytest.MonkeyPatch
) -> dict:
    started = await client.post(
        f"/api/v1/publishers/{publisher_id}/verifications",
        headers=headers,
        json={"method": "dns_txt", "domain": domain},
    )
    assert started.status_code == 201, started.text
    record = started.json()["txt_record"]

    async def resolve(name: str) -> list[str]:
        return [record] if name == domain else []

    monkeypatch.setattr(domain_verification, "resolve_txt", resolve)
    checked = await client.post(
        f"/api/v1/publishers/{publisher_id}/verifications/{started.json()['id']}/check", headers=headers
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] == "verified"
    return checked.json()


async def _enable_public(client: AsyncClient, admin_headers: dict, publisher_id: str) -> None:
    response = await client.post(
        f"/api/v1/admin/marketplace/publishers/{publisher_id}/public-publishing",
        headers=admin_headers,
        json={"enabled": True},
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Claims and the lookalike gate
# ---------------------------------------------------------------------------


async def test_personal_claim_and_global_uniqueness(client: AsyncClient, auth_headers: dict, db_session: AsyncSession) -> None:
    publisher = await _claim(client, auth_headers, "widgetco", display_name="Widget Co")
    assert publisher["namespace"] == "widgetco"
    assert publisher["owner_type"] == "user"
    assert publisher["verified"] is False and publisher["badges"] == []

    _, other = await _user(db_session)
    for squat in ("widgetco", "vvidgetco", "widget-co", "widgetc0", "wіdgetco"):
        response = await client.post("/api/v1/publishers", headers=other, json={"namespace": squat})
        if squat == "wіdgetco":  # Cyrillic: refused by the shape check before the gate
            assert response.status_code == 422, response.text
            continue
        assert response.status_code == 409, (squat, response.text)
        assert _error(response)["code"] == "namespace_confusable"


async def test_reserved_names_refused_and_unlocked_by_verified_domain(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("anthropic", "anthr0pic", "4nthropic", "anthropic-official", "official-anthropic",
                 "anthropics", "personal", "sessionfs", "0penai", "ch4tgpt", "c1aude"):
        response = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": name})
        assert response.status_code == 409, (name, response.text)
        assert _error(response)["code"] == "namespace_confusable"
    # An org that proved anthropic.com (e.g. via SSO) may claim the brand.
    org, _ = await _org(db_session, test_user)
    db_session.add(
        OrgDomainVerification(
            id="odv_anthropic", org_id=org.id, domain="anthropic.com", method="dns_txt",
            verification_token="t" * 40, status="verified",
        )
    )
    await db_session.commit()

    # A stored SSO row alone is stale evidence: the TXT record is gone.
    async def gone(_name: str) -> list[str]:
        return []

    monkeypatch.setattr(domain_verification, "resolve_txt", gone)
    stale = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": "anthropic", "org_id": org.id})
    assert stale.status_code == 409, stale.text

    async def live(name: str) -> list[str]:
        return [domain_verification.txt_record("t" * 40)] if name == "anthropic.com" else []

    monkeypatch.setattr(domain_verification, "resolve_txt", live)
    publisher = await _claim(client, auth_headers, "anthropic", org_id=org.id)
    assert publisher["owner_type"] == "org"
    # ...and its own affixed names, which still refuse everyone else.
    await _claim(client, auth_headers, "anthropic-labs", org_id=org.id)
    _, other = await _user(db_session)
    refused = await client.post("/api/v1/publishers", headers=other, json={"namespace": "anthropic-team"})
    assert refused.status_code == 409


async def test_verified_publisher_gets_distance_and_affix_protection(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "brightloom")
    _, other = await _user(db_session)
    # Unverified: only exact skeleton collisions are blocked.
    allowed = await client.post("/api/v1/publishers", headers=other, json={"namespace": "brightlooms"})
    assert allowed.status_code == 201, allowed.text
    await _verify_dns(client, auth_headers, publisher["id"], "brightloom.dev", monkeypatch)
    _, third = await _user(db_session)
    for squat in ("brihtloom", "br1ghtloom", "brightloom-official", "brightloom-ai", "real-brightloom"):
        response = await client.post("/api/v1/publishers", headers=third, json={"namespace": squat})
        assert response.status_code == 409, (squat, response.text)
    # Two edits from a long verified name still read as the brand: blocked.
    blocked = await client.post("/api/v1/publishers", headers=third, json={"namespace": "brxghtlxom"})
    assert blocked.status_code == 409
    # Two edits from a shorter one are flagged for moderators instead.
    short = await _claim(client, auth_headers, "orbitz")
    await _verify_dns(client, auth_headers, short["id"], "orbitz.example", monkeypatch)
    flagged = await client.post("/api/v1/publishers", headers=third, json={"namespace": "orxitx"})
    assert flagged.status_code == 201, flagged.text
    assert flagged.json()["review_flags"] == [{"conflict": "orbitz", "rule": "review"}]


async def test_whole_script_and_leetspeak_impersonation(
    client: AsyncClient, auth_headers: dict
) -> None:
    # Cherokee letters that spell CHATGPT: single-script, so the restriction
    # level allows it, but the UTS #39 skeleton is "chatgpt" (reserved).
    cherokee = await client.post(
        "/api/v1/publishers", headers=auth_headers,
        json={"namespace": "goodname", "display_name": "ᏟᎻᎪᎢᏀᏢᎢ"},
    )
    assert cherokee.status_code == 409
    assert _error(cherokee)["code"] == "display_name_confusable"
    mixed = await client.post(
        "/api/v1/publishers", headers=auth_headers,
        json={"namespace": "goodname", "display_name": "Ꭺcme Tools"},
    )
    assert mixed.status_code == 422
    for name in ("g00gl3", "m1str4l", "5tr1pe"):
        response = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": name})
        assert response.status_code == 409, (name, response.text)


async def test_claims_are_rate_limited(
    client: AsyncClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
    from sessionfs.server.routes import marketplace_publishers as routes

    monkeypatch.setattr(routes, "_claim_limiter", SlidingWindowRateLimiter(2, 3600))
    await _claim(client, auth_headers, "ratea-one")
    refused = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": "anthropic"})
    assert refused.status_code == 409  # refused attempts count too
    limited = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": "ratea-two"})
    assert limited.status_code == 429


async def test_lifetime_claim_quota_counts_tombstones(client: AsyncClient, auth_headers: dict, db_session: AsyncSession) -> None:
    from sessionfs.server.routes.marketplace_publishers import MAX_PUBLISHER_CLAIMS_LIFETIME

    for index in range(MAX_PUBLISHER_CLAIMS_LIFETIME):
        # Distinct names: retired names keep lookalike protection.
        words = ("amberline", "birchwood", "cobaltray", "deltafox", "fjordsong", "garnetbay",
                 "harborlight", "indigomoon", "juniperhill", "quartzlab")
        created = await _claim(client, auth_headers, words[index])
        await client.delete(f"/api/v1/publishers/{created['id']}", headers=auth_headers)
    over = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": "churn-more"})
    assert over.status_code == 409
    assert _error(over)["code"] == "publisher_limit"
    # A platform admin can still assign one.
    me = (await db_session.execute(select(User).where(User.email == "test@example.com"))).scalar_one()
    _, admin_headers = await _user(db_session, tier="admin")
    assigned = await client.post(
        "/api/v1/admin/marketplace/publishers", headers=admin_headers,
        json={"namespace": "churn-more", "owner_type": "user", "owner_id": me.id, "reason": "raised limit"},
    )
    assert assigned.status_code == 201, assigned.text


async def test_display_name_mixed_script_and_impersonation(client: AsyncClient, auth_headers: dict) -> None:
    mixed = await client.post(
        "/api/v1/publishers", headers=auth_headers, json={"namespace": "goodname", "display_name": "Gοοgle"}
    )
    assert mixed.status_code == 422
    assert _error(mixed)["code"] == "display_name_mixed_script"
    spoof = await client.post(
        "/api/v1/publishers", headers=auth_headers, json={"namespace": "goodname", "display_name": "OpenAI"}
    )
    assert spoof.status_code == 409
    assert _error(spoof)["code"] == "display_name_confusable"


async def test_shape_rules_and_limits(client: AsyncClient, auth_headers: dict) -> None:
    for bad in ("ab", "acme.io", "-acme", "acme--x", "a" * 40, "Acme_Inc"):
        response = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": bad})
        assert response.status_code == 422, (bad, response.text)
    for index in range(svc_limit()):
        await _claim(client, auth_headers, f"limitns-{index}x")
    over = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": "limitns-over"})
    assert over.status_code == 409
    assert _error(over)["code"] == "publisher_limit"


def svc_limit() -> int:
    from sessionfs.server.routes.marketplace_publishers import MAX_PUBLISHERS_PER_OWNER

    return MAX_PUBLISHERS_PER_OWNER


async def test_tombstoned_namespace_is_never_released(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    publisher = await _claim(client, auth_headers, "retiredco")
    deleted = await client.delete(f"/api/v1/publishers/{publisher['id']}", headers=auth_headers)
    assert deleted.status_code == 200 and deleted.json()["status"] == "tombstoned"
    _, other = await _user(db_session)
    again = await client.post("/api/v1/publishers", headers=other, json={"namespace": "retiredco"})
    assert again.status_code == 409
    assert "retired" in _error(again)["message"]
    # The public view hides a tombstoned publisher.
    assert (await client.get(f"/api/v1/publishers/{publisher['id']}", headers=other)).status_code == 404


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


async def test_tier_gates(client: AsyncClient, db_session: AsyncSession) -> None:
    _, free = await _user(db_session, tier="free")
    response = await client.post("/api/v1/publishers", headers=free, json={"namespace": "freebie"})
    assert response.status_code == 403
    assert _error(response)["code"] == "upgrade_required"


async def test_org_claim_needs_admin_and_cross_org_is_404(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    member, member_headers = await _user(db_session)
    await _join(db_session, org, member, "member")
    denied = await client.post("/api/v1/publishers", headers=member_headers, json={"namespace": "orgspace", "org_id": org.id})
    assert denied.status_code == 403
    publisher = await _claim(client, auth_headers, "orgspace", org_id=org.id)
    # A member sees the public view only; management is 403.
    view = await client.get(f"/api/v1/publishers/{publisher['id']}", headers=member_headers)
    assert view.status_code == 200 and "verifications" not in view.json()
    add = await client.post(
        f"/api/v1/publishers/{publisher['id']}/trusted-publishers",
        headers=member_headers,
        json={"repository": "o/r", "workflow": "release.yml", "repository_id": 1, "repository_owner_id": 2},
    )
    assert add.status_code == 403
    # An outsider gets 404 on every management route.
    _, outsider = await _user(db_session)
    for method, url, payload in (
        ("post", f"/api/v1/publishers/{publisher['id']}/verifications", {"method": "dns_txt", "domain": "x.com"}),
        ("get", f"/api/v1/publishers/{publisher['id']}/trusted-publishers", None),
        ("delete", f"/api/v1/publishers/{publisher['id']}", None),
        ("get", f"/api/v1/publishers/{publisher['id']}/listings", None),
    ):
        response = await getattr(client, method)(url, headers=outsider, **({"json": payload} if payload else {}))
        assert response.status_code == 404, (url, response.text)
    other_org, _ = await _org(db_session, member)
    cross = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": "crossorg", "org_id": other_org.id})
    assert cross.status_code == 404


async def test_service_keys_cannot_manage_publishers(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    publisher = await _claim(client, auth_headers, "svcguard", org_id=org.id)
    service = await _service(db_session, user=test_user, org_id=org.id, scopes=["marketplace:write", "marketplace:read", "skills:admin"])
    for method, url, payload in (
        ("post", "/api/v1/publishers", {"namespace": "svcclaim", "org_id": org.id}),
        ("post", f"/api/v1/publishers/{publisher['id']}/verifications", {"method": "dns_txt", "domain": "svc.dev"}),
        ("post", f"/api/v1/publishers/{publisher['id']}/trusted-publishers",
         {"repository": "o/r", "workflow": "release.yml", "repository_id": 1, "repository_owner_id": 2}),
        ("delete", f"/api/v1/publishers/{publisher['id']}", None),
        ("get", "/api/v1/publishers", None),
    ):
        response = await getattr(client, method)(url, headers=service, **({"json": payload} if payload else {}))
        assert response.status_code == 403, (url, response.text)
    # Read of the public view is fine for any marketplace:read key.
    assert (await client.get(f"/api/v1/publishers/{publisher['id']}", headers=service)).status_code == 200
    # Service keys cannot publish public listings even in their org.
    response = await client.post(
        f"/api/v1/orgs/{org.id}/skills/packages",
        headers=service,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "svcguard", "collection": "svcguard", "visibility": "public"},
    )
    assert response.status_code == 403


async def test_admin_routes_require_platform_admin(client: AsyncClient, auth_headers: dict) -> None:
    for method, url, payload in (
        ("get", "/api/v1/admin/marketplace/publishers", None),
        ("post", "/api/v1/admin/marketplace/publishers/recheck", {}),
        ("get", "/api/v1/admin/marketplace/review-queue", None),
        ("post", "/api/v1/admin/marketplace/reserved-names", {"name": "foobar"}),
    ):
        response = await getattr(client, method)(url, headers=auth_headers, **({"json": payload} if payload is not None else {}))
        assert response.status_code == 403, (url, response.text)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


async def test_dns_verification_lapses_after_seven_days_and_recovers(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "dnsco")
    verification = await _verify_dns(client, auth_headers, publisher["id"], "dnsco.example", monkeypatch)
    assert verification["badge"] == "verified: dnsco.example"
    view = (await client.get(f"/api/v1/publishers/{publisher['id']}", headers=auth_headers)).json()
    assert view["verified"] is True and view["badges"] == ["verified: dnsco.example"]

    async def gone(_name: str) -> list[str]:
        return []

    monkeypatch.setattr(domain_verification, "resolve_txt", gone)
    row = await db_session.get(SkillPublisherVerification, verification["id"])
    first = await svc.check_verification(db_session, row)
    assert first == "verified" and row.consecutive_failures == 1
    row.first_failure_at = svc.now_utc() - timedelta(days=7, minutes=1)
    assert await svc.check_verification(db_session, row) == "lapsed"
    await db_session.commit()
    view = (await client.get(f"/api/v1/publishers/{publisher['id']}", headers=auth_headers)).json()
    assert view["verified"] is False

    async def back(name: str) -> list[str]:
        return [domain_verification.txt_record(row.verification_token or "")]

    monkeypatch.setattr(domain_verification, "resolve_txt", back)
    assert await svc.check_verification(db_session, row) == "verified"
    assert row.consecutive_failures == 0
    await db_session.commit()


async def test_dns_one_publisher_per_domain_and_other_org_domain(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, test_user: User
) -> None:
    first = await _claim(client, auth_headers, "firstco")
    await _verify_dns(client, auth_headers, first["id"], "shared.example", monkeypatch)
    _, other = await _user(db_session)
    second = await _claim(client, other, "secondco")
    response = await client.post(
        f"/api/v1/publishers/{second['id']}/verifications", headers=other,
        json={"method": "dns_txt", "domain": "shared.example"},
    )
    assert response.status_code == 409
    assert _error(response)["code"] == "subject_verified_elsewhere"
    # A domain another ORG verified for SSO is not claimable either.
    org, _ = await _org(db_session, test_user)
    db_session.add(OrgDomainVerification(id="odv_x", org_id=org.id, domain="orgowned.example",
                                         method="dns_txt", verification_token="a" * 40, status="verified"))
    await db_session.commit()
    response = await client.post(
        f"/api/v1/publishers/{second['id']}/verifications", headers=other,
        json={"method": "dns_txt", "domain": "orgowned.example"},
    )
    assert response.status_code == 409
    consumer = await client.post(
        f"/api/v1/publishers/{second['id']}/verifications", headers=other,
        json={"method": "dns_txt", "domain": "gmail.com"},
    )
    assert consumer.status_code == 422


async def test_pending_dns_cannot_take_a_domain_verified_meanwhile(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = await _claim(client, auth_headers, "slowco")
    pending = await client.post(
        f"/api/v1/publishers/{slow['id']}/verifications", headers=auth_headers,
        json={"method": "dns_txt", "domain": "race.example"},
    )
    assert pending.status_code == 201 and pending.json()["status"] == "pending"
    _, other = await _user(db_session)
    fast = await _claim(client, other, "fastco")
    await _verify_dns(client, other, fast["id"], "race.example", monkeypatch)
    slow_record = pending.json()["txt_record"]

    async def both(_name: str) -> list[str]:
        return [slow_record]

    monkeypatch.setattr(domain_verification, "resolve_txt", both)
    checked = await client.post(
        f"/api/v1/publishers/{slow['id']}/verifications/{pending.json()['id']}/check", headers=auth_headers
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] == "pending"
    assert checked.json()["status_reason"] == "subject_verified_by_another_publisher"
    public_view = (await client.get(f"/api/v1/publishers/{fast['id']}", headers=auth_headers)).json()
    assert "org_id" not in public_view and "verifications" not in public_view


async def test_lapsed_publisher_cannot_publish_public(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "lapseco")
    verification = await _verify_dns(client, auth_headers, publisher["id"], "lapse.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])
    row = await db_session.get(SkillPublisherVerification, verification["id"])
    row.status = "lapsed"
    await db_session.commit()
    response = await client.post(
        "/api/v1/users/me/skills/packages", headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "lapseco", "collection": "lapseco", "visibility": "public"},
    )
    assert response.status_code == 403
    assert _error(response)["code"] == "publisher_not_verified"


async def test_sso_domain_reuse_without_oidc_sso_gate(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, _ = await _org(db_session, test_user)
    db_session.add(OrgDomainVerification(id="odv_reuse", org_id=org.id, domain="reuse.example",
                                         method="dns_txt", verification_token="b" * 40, status="verified"))
    await db_session.commit()
    publisher = await _claim(client, auth_headers, "reuseco", org_id=org.id)

    async def resolve(name: str) -> list[str]:
        return [domain_verification.txt_record("b" * 40)] if name == "reuse.example" else []

    monkeypatch.setattr(domain_verification, "resolve_txt", resolve)
    response = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications", headers=auth_headers,
        json={"method": "dns_txt", "domain": "reuse.example"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "verified"
    assert response.json()["reused_org_domain_verification"] is True


async def _owned(db, user_id, installation_id) -> bool:
    """Stand-in for the verified claim binding (tk_b6c5e184ba8d4aa1): the
    user's own installation row, as the verified-claim flow will record."""
    from sqlalchemy import select as _select

    row = (
        await db.execute(
            _select(GitHubInstallation.id).where(
                GitHubInstallation.id == int(installation_id), GitHubInstallation.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def test_github_verification_refused_on_legacy_installation_claim(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    org, _ = await _org(db_session, test_user)
    publisher = await _claim(client, auth_headers, "legacyclaim", org_id=org.id)
    db_session.add(GitHubInstallation(id=5005, user_id=test_user.id, account_login="legacyclaim", account_type="Organization"))
    await db_session.commit()

    async def account(_installation_id: int):
        return {"id": 77, "login": "legacyclaim", "type": "Organization", "suspended": False}

    monkeypatch.setattr(github_app, "get_installation_account", account)
    # A legacy claim (no OAuth-verified binding) never earns a badge.
    assert await svc.installation_owned_by(db_session, test_user.id, 5005) is False
    response = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications", headers=auth_headers,
        json={"method": "github_org", "installation_id": 5005},
    )
    assert response.status_code == 409
    assert _error(response)["code"] == "installation_claim_unverified"
    # A badge granted earlier is revoked by the next re-check.
    row = SkillPublisherVerification(
        id="spv_legacy", publisher_id=publisher["id"], method="github_org", subject="77",
        subject_display="github.com/legacyclaim", github_installation_id=5005, status="verified",
        created_by_user_id=test_user.id,
    )
    db_session.add(row)
    await db_session.commit()
    assert await svc.check_verification(db_session, row) == "revoked"
    assert row.status_reason == "installation_claim_unverified"


async def test_installation_ownership_needs_a_recent_privileged_claim(
    db_session: AsyncSession, test_user: User
) -> None:
    from datetime import datetime, timedelta, timezone

    from sessionfs.server.services.github_oauth import CLAIM_PROOF_MAX_AGE

    now = datetime.now(timezone.utc)
    row = GitHubInstallation(
        id=6001, user_id=test_user.id, account_login="acct", account_type="Organization",
    )
    db_session.add(row)
    await db_session.commit()
    cases = [
        ("org_admin", now, True),
        ("account_owner", now - timedelta(hours=1), True),
        ("member", now, False),
        ("installer", now, False),
        ("org_admin", now - CLAIM_PROOF_MAX_AGE - timedelta(minutes=1), False),
        (None, None, False),
    ]
    for role, verified_at, expected in cases:
        row.claim_role = role
        row.claim_verified_at = verified_at
        await db_session.commit()
        assert await svc.installation_owned_by(db_session, test_user.id, 6001) is expected, role
    row.claim_role, row.claim_verified_at = "org_admin", now
    await db_session.commit()
    assert await svc.installation_owned_by(db_session, "someone-else", 6001) is False


async def test_github_verification_uses_callers_own_installation(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "installation_owned_by", _owned)
    org, _ = await _org(db_session, test_user)
    publisher = await _claim(client, auth_headers, "ghorg", org_id=org.id)
    other_user, other_headers = await _user(db_session)
    db_session.add_all([
        GitHubInstallation(id=1001, user_id=test_user.id, account_login="ghorg", account_type="Organization"),
        GitHubInstallation(id=2002, user_id=other_user.id, account_login="victim", account_type="Organization"),
    ])
    await db_session.commit()

    async def account(installation_id: int):
        return {
            1001: {"id": 555, "login": "ghorg", "type": "Organization", "suspended": False},
            2002: {"id": 777, "login": "victim", "type": "Organization", "suspended": False},
        }.get(installation_id)

    monkeypatch.setattr(github_app, "get_installation_account", account)
    # N1: someone else's installation never counts.
    stolen = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications", headers=auth_headers,
        json={"method": "github_org", "installation_id": 2002},
    )
    assert stolen.status_code == 404
    ok = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications", headers=auth_headers,
        json={"method": "github_org", "installation_id": 1001},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["subject"] == "555"
    assert ok.json()["badge"] == "verified: github.com/ghorg (org #555)"
    # The same numeric account cannot back a second publisher.
    second = await _claim(client, auth_headers, "ghorg-two", org_id=org.id)
    dup = await client.post(
        f"/api/v1/publishers/{second['id']}/verifications", headers=auth_headers,
        json={"method": "github_org", "installation_id": 1001},
    )
    assert dup.status_code == 409
    # Wrong account type for the method.
    personal = await _claim(client, other_headers, "ghuser")
    wrong = await client.post(
        f"/api/v1/publishers/{personal['id']}/verifications", headers=other_headers,
        json={"method": "github_user", "installation_id": 2002},
    )
    assert wrong.status_code == 422


async def test_github_rename_resurrection_and_uninstall(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "installation_owned_by", _owned)
    org, _ = await _org(db_session, test_user)
    publisher = await _claim(client, auth_headers, "renameco", org_id=org.id)
    db_session.add(GitHubInstallation(id=3003, user_id=test_user.id, account_login="renameco", account_type="Organization"))
    await db_session.commit()
    state = {"account": {"id": 900, "login": "renameco", "type": "Organization", "suspended": False}}

    async def account(_installation_id: int):
        return state["account"]

    monkeypatch.setattr(github_app, "get_installation_account", account)
    created = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications", headers=auth_headers,
        json={"method": "github_org", "installation_id": 3003},
    )
    row = await db_session.get(SkillPublisherVerification, created.json()["id"])
    # Rename keeps the badge (numeric id) and refreshes the display.
    state["account"] = {"id": 900, "login": "renamed-co", "type": "Organization", "suspended": False}
    assert await svc.check_verification(db_session, row) == "verified"
    assert row.subject_display == "github.com/renamed-co"
    # A different account behind the same id (resurrection) revokes.
    state["account"] = {"id": 901, "login": "renameco", "type": "Organization", "suspended": False}
    assert await svc.check_verification(db_session, row) == "revoked"
    await db_session.commit()


async def test_signed_uninstall_webhook_revokes_badge(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "installation_owned_by", _owned)
    org, _ = await _org(db_session, test_user)
    publisher = await _claim(client, auth_headers, "hookco", org_id=org.id)
    db_session.add(GitHubInstallation(id=4004, user_id=test_user.id, account_login="hookco", account_type="Organization"))
    await db_session.commit()

    async def account(_installation_id: int):
        return {"id": 42, "login": "hookco", "type": "Organization", "suspended": False}

    monkeypatch.setattr(github_app, "get_installation_account", account)
    created = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications", headers=auth_headers,
        json={"method": "github_org", "installation_id": 4004},
    )
    assert created.status_code == 201
    secret = "whsec-test"
    monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", secret)
    payload = json.dumps(_installation_event("suspend", 4004, "hookco", 42)).encode()
    signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    response = await client.post(
        "/webhooks/github", content=payload,
        headers={"X-GitHub-Event": "installation", "X-Hub-Signature-256": signature, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    db_session.expire_all()
    row = await db_session.get(SkillPublisherVerification, created.json()["id"])
    assert row.status == "revoked" and row.status_reason == "github_installation_suspended"


async def test_recheck_sweeper_endpoint(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "sweepco")
    await _verify_dns(client, auth_headers, publisher["id"], "sweep.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    response = await client.post("/api/v1/admin/marketplace/publishers/recheck", headers=admin_headers, json={})
    assert response.status_code == 200, response.text
    assert response.json()["checked"] >= 1


# ---------------------------------------------------------------------------
# Publishing: namespace ownership, public publishing, moderation
# ---------------------------------------------------------------------------


async def test_claimed_namespace_refuses_new_packages_from_others_but_grandfathers(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    early_user, early = await _user(db_session)
    first = await client.post(
        "/api/v1/users/me/skills/packages", headers=early,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "contested", "collection": "c"},
    )
    assert first.status_code == 201, first.text
    # A legacy namespace is reserved to the owners already publishing in it.
    for attempt in ("contested", "c0ntested"):
        taken = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": attempt})
        assert taken.status_code == 409, taken.text
        assert _error(taken)["code"] == "namespace_in_use"
    # A platform admin can still assign it (appeal outcome).
    me = (await db_session.execute(select(User).where(User.email == "test@example.com"))).scalar_one()
    _, admin_headers = await _user(db_session, tier="admin")
    assigned = await client.post(
        "/api/v1/admin/marketplace/publishers", headers=admin_headers,
        json={"namespace": "contested", "owner_type": "user", "owner_id": me.id, "reason": "appeal"},
    )
    assert assigned.status_code == 201, assigned.text
    publisher = assigned.json()
    # The legacy owner can claim their own namespace.
    mine = await _claim(client, early, "earlyspace-x")
    assert mine["namespace"] == "earlyspace-x"
    assert early_user.id
    # The early user keeps publishing new versions of their EXISTING package.
    update = await client.post(
        "/api/v1/users/me/skills/packages", headers=early,
        files={"package": ("p.tar.gz", _pkg(body="v2"), "application/gzip")},
        data={"namespace": "contested", "collection": "c"},
    )
    assert update.status_code == 201, update.text
    # ...but no new package identity, nor a lookalike namespace.
    for namespace in ("contested", "c0ntested"):
        new = await client.post(
            "/api/v1/users/me/skills/packages", headers=early,
            files={"package": ("p.tar.gz", _pkg(name="other-skill"), "application/gzip")},
            data={"namespace": namespace, "collection": "c"},
        )
        assert new.status_code == 409, (namespace, new.text)
        assert _error(new)["code"] == "namespace_claimed"
    own = await client.post(
        "/api/v1/users/me/skills/packages", headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "contested", "collection": "c"},
    )
    assert own.status_code == 201
    assert own.json()["publisher_id"] == publisher["id"]
    assert own.json()["publisher"]["namespace"] == "contested"


async def test_public_publishing_requires_verified_cohort_and_reviews_first_version(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    def publish(namespace: str, *, scripts: bool = False, body: str = "Inspect.", name: str = "review-helper"):
        return client.post(
            "/api/v1/users/me/skills/packages", headers=auth_headers,
            files={"package": ("p.tar.gz", _pkg(name=name, scripts=scripts, body=body), "application/gzip")},
            data={"namespace": namespace, "collection": namespace, "visibility": "public"},
        )

    unclaimed = await publish("nobodyns")
    assert unclaimed.status_code == 403
    assert _error(unclaimed)["code"] == "public_publishing_not_allowed"
    publisher = await _claim(client, auth_headers, "pubco")
    unverified = await publish("pubco")
    assert _error(unverified)["code"] == "publisher_not_verified"
    await _verify_dns(client, auth_headers, publisher["id"], "pubco.example", monkeypatch)
    not_cohort = await publish("pubco")
    assert _error(not_cohort)["code"] == "public_publishing_not_enabled"
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])

    first = await publish("pubco")
    assert first.status_code == 201, first.text
    assert first.json()["moderation_state"] == "pending_review"
    assert first.json()["publisher"]["badges"] == ["verified: pubco.example"]
    # Not deliverable while pending: absent from search, 404 on get.
    _, viewer = await _user(db_session)
    search = (await client.get("/api/v1/marketplace/packages", headers=viewer, params={"q": "review"})).json()
    assert first.json()["listing_id"] not in [item["listing_id"] for item in search["items"]]
    assert (await client.get(f"/api/v1/marketplace/packages/{first.json()['package_id']}", headers=viewer)).status_code == 404
    queue = (await client.get("/api/v1/admin/marketplace/review-queue", headers=admin_headers)).json()
    assert [item["moderation_reason"] for item in queue["items"]] == ["first_public_version"]
    approved = await client.post(f"/api/v1/admin/marketplace/listings/{first.json()['listing_id']}/approve", headers=admin_headers)
    assert approved.status_code == 200
    got = await client.get(f"/api/v1/marketplace/packages/{first.json()['package_id']}", headers=viewer)
    assert got.status_code == 200 and got.json()["moderation_state"] == "approved"
    assert got.json()["publisher"]["verified"] is True

    # A text-only update auto-approves; one that adds scripts widens.
    text_update = await publish("pubco", body="Inspect the change carefully.")
    assert text_update.json()["moderation_state"] == "approved"
    widening = await publish("pubco", scripts=True, body="Now with a script.")
    assert widening.json()["moderation_state"] == "pending_review"
    queue = (await client.get("/api/v1/admin/marketplace/review-queue", headers=admin_headers)).json()
    assert queue["items"][0]["moderation_reason"].startswith("widens_access:scripts")
    rejected = await client.post(
        f"/api/v1/admin/marketplace/listings/{widening.json()['listing_id']}/reject",
        headers=admin_headers, json={"reason": "script not justified"},
    )
    assert rejected.status_code == 200
    listing = await db_session.get(SkillMarketplaceListing, widening.json()["listing_id"])
    await db_session.refresh(listing)
    assert listing.moderation_state == "rejected"
    # New public package names must not imitate protected names.
    squat = await publish("pubco", name="anthropic-skills")
    assert squat.status_code == 409
    assert _error(squat)["code"] == "package_name_confusable"


async def test_platform_admin_public_listing_attaches_system_publisher(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_headers = await _user(db_session, tier="admin")
    org, _ = await _org(db_session, admin)
    response = await client.post(
        f"/api/v1/orgs/{org.id}/skills/packages", headers=admin_headers,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "sessionfs", "collection": "catalog", "visibility": "public"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["moderation_state"] == "approved"
    assert body["publisher_id"] == svc.SYSTEM_PUBLISHER_ID
    assert body["publisher"]["badges"] == ["verified: SessionFS platform"]
    # Platform-admin public content outside the sessionfs namespace is not
    # SessionFS's own: no platform publisher, no badge.
    other = await client.post(
        f"/api/v1/orgs/{org.id}/skills/packages", headers=admin_headers,
        files={"package": ("p.tar.gz", _pkg(name="other-skill"), "application/gzip")},
        data={"namespace": "community", "collection": "catalog", "visibility": "public"},
    )
    assert other.status_code == 201, other.text
    assert other.json()["publisher_id"] is None and other.json()["publisher"] is None
    # Nobody can claim the platform namespace.
    _, user_headers = await _user(db_session)
    claim = await client.post("/api/v1/publishers", headers=user_headers, json={"namespace": "sessionfs"})
    assert claim.status_code == 409


async def test_suspension_holds_publishing_and_reviews_listings(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "suspendco")
    await _verify_dns(client, auth_headers, publisher["id"], "suspend.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])
    first = await client.post(
        "/api/v1/users/me/skills/packages", headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "suspendco", "collection": "suspendco", "visibility": "public"},
    )
    await client.post(f"/api/v1/admin/marketplace/listings/{first.json()['listing_id']}/approve", headers=admin_headers)
    suspended = await client.post(
        f"/api/v1/admin/marketplace/publishers/{publisher['id']}/suspend", headers=admin_headers,
        json={"reason": "malware report"},
    )
    assert suspended.status_code == 200 and suspended.json()["status"] == "suspended"
    listing = await db_session.get(SkillMarketplaceListing, first.json()["listing_id"])
    await db_session.refresh(listing)
    assert listing.moderation_state == "pending_review"
    blocked = await client.post(
        "/api/v1/users/me/skills/packages", headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(body="v2"), "application/gzip")},
        data={"namespace": "suspendco", "collection": "x"},
    )
    assert blocked.status_code == 403
    assert _error(blocked)["code"] == "publisher_suspended"
    restored = await client.post(
        f"/api/v1/admin/marketplace/publishers/{publisher['id']}/restore", headers=admin_headers,
        json={"reason": "report was false"},
    )
    assert restored.status_code == 200
    await db_session.refresh(listing)
    assert listing.moderation_state == "approved"
    package = (await db_session.execute(select(SkillPackage).where(SkillPackage.id == first.json()["package_id"]))).scalar_one()
    assert package.publisher_id == publisher["id"]


async def test_publisher_listings_shows_pending_to_manager_only(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "listco")
    await _verify_dns(client, auth_headers, publisher["id"], "list.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])
    await client.post(
        "/api/v1/users/me/skills/packages", headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "listco", "collection": "listco", "visibility": "public"},
    )
    mine = (await client.get(f"/api/v1/publishers/{publisher['id']}/listings", headers=auth_headers)).json()
    assert [item["moderation_state"] for item in mine["items"]] == ["pending_review"]
    publisher_row = await db_session.get(SkillPublisher, publisher["id"])
    assert publisher_row.public_publishing is True


async def test_private_package_gets_name_gate_and_review_when_it_first_goes_public(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "sneakyco")
    await _verify_dns(client, auth_headers, publisher["id"], "sneaky.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])

    def publish(name: str, visibility: str):
        return client.post(
            "/api/v1/users/me/skills/packages", headers=auth_headers,
            files={"package": ("p.tar.gz", _pkg(name=name), "application/gzip")},
            data={"namespace": "sneakyco", "collection": f"sneaky-{visibility}", "visibility": visibility},
        )

    # Establish the publisher with one approved public package.
    first = await publish("review-helper", "public")
    await client.post(f"/api/v1/admin/marketplace/listings/{first.json()['listing_id']}/approve", headers=admin_headers)
    # A lookalike name parked privately first...
    private = await publish("openai-official", "user")
    assert private.status_code == 201, private.text
    # ...is still refused when it goes public (row and version already exist).
    public = await publish("openai-official", "public")
    assert public.status_code == 409, public.text
    assert _error(public)["code"] == "package_name_confusable"
    # D1: a harmless, text-only package from an established publisher that
    # passes the name gate auto-approves when it first goes public...
    await publish("diff-notes", "user")
    later = await publish("diff-notes", "public")
    assert later.status_code == 201, later.text
    assert later.json()["moderation_state"] == "approved"
    # ...while one that asks for more access is held for review.
    scripted = await client.post(
        "/api/v1/users/me/skills/packages", headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(name="run-helper", scripts=True), "application/gzip")},
        data={"namespace": "sneakyco", "collection": "sneaky-public", "visibility": "public"},
    )
    assert scripted.status_code == 201, scripted.text
    assert scripted.json()["moderation_state"] == "pending_review"


async def test_platform_namespace_lookalikes_never_get_the_platform_badge(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_headers = await _user(db_session, tier="admin")
    org, _ = await _org(db_session, admin)
    for namespace in ("session-fs", "ses5ionfs", "sessi0nfs"):
        response = await client.post(
            f"/api/v1/orgs/{org.id}/skills/packages", headers=admin_headers,
            files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
            data={"namespace": namespace, "collection": "catalog", "visibility": "public"},
        )
        assert response.status_code == 409, (namespace, response.text)
        assert _error(response)["code"] == "namespace_confusable"


async def test_claim_quota_holds_under_repeated_claims_in_one_window(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """The quota is counted under row locks (the PG concurrency proof is in
    test_publisher_quota_pg.py); the durable hourly cap counts publishers the
    claimant created, so it holds across processes."""
    from sessionfs.server.db.models import SkillPublisher as Publisher

    now = svc.now_utc()
    for index in range(svc.MAX_CLAIMS_PER_USER_PER_HOUR):
        db_session.add(Publisher(
            id=f"pub_rate{index}", owner_type="org", org_id=f"org_rate{index}", namespace=f"rate-org-{index}x",
            namespace_normalized=f"rateorg{index}x", display_name="r", status="tombstoned",
            created_by_user_id=test_user.id, created_at=now,
        ))
    await db_session.commit()
    response = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": "fresh-claim"})
    assert response.status_code == 429, response.text


async def test_reserved_brand_namespace_is_bound_to_the_proof_that_unlocked_it(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, _ = await _org(db_session, test_user)
    db_session.add(OrgDomainVerification(
        id="odv_brand", org_id=org.id, domain="anthropic.com", method="dns_txt",
        verification_token="b" * 40, status="verified",
    ))
    await db_session.commit()
    records = {"anthropic.com": [domain_verification.txt_record("b" * 40)]}

    async def resolve(name: str) -> list[str]:
        return list(records.get(name, []))

    monkeypatch.setattr(domain_verification, "resolve_txt", resolve)
    publisher = await _claim(client, auth_headers, "anthropic", org_id=org.id)
    row = await db_session.get(SkillPublisher, publisher["id"])
    await db_session.refresh(row)
    assert svc.required_domains(row) == ["anthropic.com"]

    # Verified, but only through an UNRELATED domain.
    other = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications", headers=auth_headers,
        json={"method": "dns_txt", "domain": "unrelated.example"},
    )
    records["unrelated.example"] = [other.json()["txt_record"]]
    checked = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications/{other.json()['id']}/check", headers=auth_headers
    )
    assert checked.json()["status"] == "verified"
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])

    def publish(body: str = "Inspect."):
        return client.post(
            f"/api/v1/orgs/{org.id}/skills/packages", headers=auth_headers,
            files={"package": ("p.tar.gz", _pkg(body=body), "application/gzip")},
            data={"namespace": "anthropic", "collection": "anthropic", "visibility": "public"},
        )

    blocked = await publish()
    assert blocked.status_code == 403, blocked.text
    assert _error(blocked)["code"] == "reserved_name_proof_lapsed"
    # A current proof of the EXACT unlocking domain on this publisher works.
    brand = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications", headers=auth_headers,
        json={"method": "dns_txt", "domain": "anthropic.com"},
    )
    assert brand.status_code == 201 and brand.json()["status"] == "verified", brand.text
    ok = await publish()
    assert ok.status_code == 201, ok.text
    # When that proof lapses, new public versions stop, other proofs notwithstanding.
    lapsed = await db_session.get(SkillPublisherVerification, brand.json()["id"])
    lapsed.status = "lapsed"
    await db_session.commit()
    again = await publish(body="v2")
    assert again.status_code == 403
    assert _error(again)["code"] == "reserved_name_proof_lapsed"


async def test_suspension_bumps_generation_so_restore_does_not_revive_tokens(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    from sessionfs.server.db.models import SkillPublishToken, SkillTrustedPublisher

    publisher = await _claim(client, auth_headers, "genco")
    pub = await db_session.get(SkillPublisher, publisher["id"])
    trusted = SkillTrustedPublisher(
        id="stp_gen", publisher_id=pub.id, repository_owner_id=1, repository_id=2,
        repository="o/r", workflow_path=".github/workflows/r.yml",
    )
    db_session.add(trusted)
    raw = svc.PUBLISH_TOKEN_PREFIX + "g" * 43
    db_session.add(SkillPublishToken(
        id="spt_gen", token_hash=svc.token_hash(raw), trusted_publisher_id="stp_gen", publisher_id=pub.id,
        oidc_issuer="iss", oidc_jti="jti-gen", claims="{}", auth_generation=pub.auth_generation,
        expires_at=svc.now_utc() + svc.PUBLISH_TOKEN_TTL,
    ))
    await db_session.commit()
    await svc.load_publish_token(db_session, raw)  # valid now
    _, admin_headers = await _user(db_session, tier="admin")
    await client.post(f"/api/v1/admin/marketplace/publishers/{pub.id}/suspend", headers=admin_headers, json={"reason": "check"})
    await client.post(f"/api/v1/admin/marketplace/publishers/{pub.id}/restore", headers=admin_headers, json={"reason": "ok now"})
    restored = (
        await db_session.execute(
            select(SkillPublisher).where(SkillPublisher.id == publisher["id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert restored.status == "active" and restored.auth_generation == 1
    with pytest.raises(Exception) as excinfo:
        await svc.load_publish_token(db_session, raw)
    assert getattr(excinfo.value, "status_code", None) == 401


async def test_retired_namespace_keeps_lookalike_protection(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    retired = await _claim(client, auth_headers, "riverstone")
    # Verified before it is retired: keeps strong (distance / affix) protection.
    await _verify_dns(client, auth_headers, retired["id"], "riverstone.example", monkeypatch)
    await client.delete(f"/api/v1/publishers/{retired['id']}", headers=auth_headers)
    _, other = await _user(db_session)
    for squat in ("riverstones", "riverstone-official", "rivers7one", "real-riverstone"):
        response = await client.post("/api/v1/publishers", headers=other, json={"namespace": squat})
        assert response.status_code == 409, (squat, response.text)
        assert "retired" in _error(response)["message"]
    # Its former owner gets no exemption either.
    own = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": "riverstone-labs"})
    assert own.status_code == 409


async def test_verification_transition_applies_to_the_row_reread_under_the_lock(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe taken before a concurrent change (here: the owner removes the
    verification) is applied to the re-read row, so it cannot resurrect it."""
    publisher = await _claim(client, auth_headers, "probeco")
    verification = await _verify_dns(client, auth_headers, publisher["id"], "probe.example", monkeypatch)
    original_probe = svc.probe_verification

    async def probe_then_remove(db, row):
        result = await original_probe(db, row)
        removed = await client.delete(
            f"/api/v1/publishers/{publisher['id']}/verifications/{verification['id']}", headers=auth_headers
        )
        assert removed.status_code == 200
        return result

    monkeypatch.setattr(svc, "probe_verification", probe_then_remove)
    row = await db_session.get(SkillPublisherVerification, verification["id"])
    assert await svc.check_verification(db_session, row) == "revoked"
    current = await svc.reload_verification(db_session, verification["id"])
    assert current.status == "revoked" and current.status_reason == "removed_by_owner"


async def test_apply_probe_ignores_a_stale_dns_token(db_session: AsyncSession, test_user: User) -> None:
    db_session.add(SkillPublisher(id="pub_stale", owner_type="user", user_id=test_user.id,
                                  namespace="staleco", namespace_normalized="staleco"))
    row = SkillPublisherVerification(id="spv_stale", publisher_id="pub_stale", method="dns_txt",
                                     subject="stale.example", subject_display="stale.example",
                                     verification_token="new-token", status="pending")
    db_session.add(row)
    await db_session.commit()
    probe = svc.VerificationProbe("dns_txt", "old-token", dns_ok=True)
    assert await svc.apply_probe(db_session, row, probe) == "pending"


async def test_bidi_and_format_controls_are_refused_before_the_skeleton(
    client: AsyncClient, auth_headers: dict
) -> None:
    for display in ("\u202eIAnepO", "Open\u200bAI Labs", "Acme\u2066 Tools", "Acme\ue000"):
        response = await client.post(
            "/api/v1/publishers", headers=auth_headers, json={"namespace": "goodname", "display_name": display}
        )
        assert response.status_code == 422, (display, response.text)
        assert _error(response)["code"] == "invalid_display_name"
    for namespace in ("ac\u200dme", "acme\u202e"):
        response = await client.post("/api/v1/publishers", headers=auth_headers, json={"namespace": namespace})
        assert response.status_code == 422, (namespace, response.text)
        assert _error(response)["code"] == "invalid_namespace"
    # The package-name gate rejects them too.
    with pytest.raises(Exception) as excinfo:
        svc.reject_forbidden_code_points("review\u202ehelper", field="package_name")
    assert getattr(excinfo.value, "status_code", None) == 422


async def test_github_method_must_match_the_publisher_owner_both_ways(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    org_pub = await _claim(client, auth_headers, "pairorg", org_id=org.id)
    user_pub = await _claim(client, auth_headers, "pairuser")
    for publisher_id, method in ((org_pub["id"], "github_user"), (user_pub["id"], "github_org")):
        response = await client.post(
            f"/api/v1/publishers/{publisher_id}/verifications", headers=auth_headers,
            json={"method": method, "installation_id": 1},
        )
        assert response.status_code == 422, response.text
        assert _error(response)["code"] == "method_owner_mismatch"


async def test_recorded_installation_suspension_refuses_new_verifications(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, _ = await _org(db_session, test_user)
    publisher = await _claim(client, auth_headers, "suspinst", org_id=org.id)
    db_session.add(GitHubInstallation(id=6006, user_id=test_user.id, account_login="suspinst", account_type="Organization"))
    await db_session.commit()
    monkeypatch.setattr(svc, "installation_owned_by", _owned)

    async def account(_installation_id: int):
        # GitHub's API has not caught up: it still reports the installation live.
        return {"id": 66, "login": "suspinst", "type": "Organization", "suspended": False}

    monkeypatch.setattr(github_app, "get_installation_account", account)
    secret = "whsec-test"
    monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", secret)

    async def deliver(action: str) -> None:
        payload = json.dumps(_installation_event(action, 6006, "suspinst", 66)).encode()
        signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        response = await client.post(
            "/webhooks/github", content=payload,
            headers={"X-GitHub-Event": "installation", "X-Hub-Signature-256": signature,
                     "Content-Type": "application/json"},
        )
        assert response.status_code == 200

    await deliver("suspend")
    refused = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications", headers=auth_headers,
        json={"method": "github_org", "installation_id": 6006},
    )
    assert refused.status_code == 409, refused.text
    assert _error(refused)["code"] == "installation_suspended"
    await deliver("unsuspend")
    allowed = await client.post(
        f"/api/v1/publishers/{publisher['id']}/verifications", headers=auth_headers,
        json={"method": "github_org", "installation_id": 6006},
    )
    assert allowed.status_code == 201, allowed.text


async def test_sso_and_publisher_dns_cannot_both_hold_a_domain(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    # 1. A personal publisher verifies the domain; an org's SSO promotion of
    #    the same domain is then refused.
    personal = await _claim(client, auth_headers, "domainowner")
    await _verify_dns(client, auth_headers, personal["id"], "shared-domain.example", monkeypatch)
    other_owner, other_headers = await _user(db_session)
    org, _ = await _org(db_session, other_owner)
    requested = await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains", headers=other_headers, json={"domain": "shared-domain.example"}
    )
    assert requested.status_code == 200, requested.text
    token = requested.json()["verification_token"]

    async def sso_dns(_domain: str) -> list[str]:
        return [f"sessionfs-verification={token}"]

    with patch("sessionfs.server.routes.sso_admin._resolve_txt", new=sso_dns):
        promoted = await client.post(
            f"/api/v1/orgs/{org.id}/sso/domains/{requested.json()['id']}/verify", headers=other_headers
        )
    assert promoted.status_code == 409, promoted.text

    # 2. The other way: an org's SSO proof lands while a publisher's DNS
    #    verification is pending; the publisher's promotion is then refused.
    pending_pub = await _claim(client, auth_headers, "latecomer")
    pending = await client.post(
        f"/api/v1/publishers/{pending_pub['id']}/verifications", headers=auth_headers,
        json={"method": "dns_txt", "domain": "late.example"},
    )
    assert pending.status_code == 201
    db_session.add(OrgDomainVerification(id="odv_late", org_id=org.id, domain="late.example",
                                         method="dns_txt", verification_token="c" * 40, status="verified"))
    await db_session.commit()
    record = pending.json()["txt_record"]

    async def pub_dns(_name: str) -> list[str]:
        return [record]

    monkeypatch.setattr(domain_verification, "resolve_txt", pub_dns)
    checked = await client.post(
        f"/api/v1/publishers/{pending_pub['id']}/verifications/{pending.json()['id']}/check", headers=auth_headers
    )
    assert checked.json()["status"] == "pending"
    assert checked.json()["status_reason"] == "domain_verified_by_another_org"


async def test_personal_public_listing_requires_a_license(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "licenseco")
    await _verify_dns(client, auth_headers, publisher["id"], "license.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])
    output = io.BytesIO()
    manifest = {"$schema": AGENT_PLUGIN_SCHEMA, "name": "no-license", "version": "1.0.0", "description": "d"}
    files = {
        "plugin.json": json.dumps(manifest).encode(),
        "skills/no-license/SKILL.md": b"---\nname: no-license\ndescription: d.\n---\n\n# N\n\nBody.\n",
    }
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, payload in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    def publish(visibility: str):
        return client.post(
            "/api/v1/users/me/skills/packages", headers=auth_headers,
            files={"package": ("p.tar.gz", output.getvalue(), "application/gzip")},
            data={"namespace": "licenseco", "collection": f"lic-{visibility}", "visibility": visibility},
        )

    assert (await publish("user")).status_code == 201  # personal listing: no license needed
    public = await publish("public")
    assert public.status_code == 422, public.text
    assert _error(public)["details"]["reason"] == "license_required"


def test_license_required_by_visibility() -> None:
    from sessionfs.skills.publish_validation import license_required_for

    assert license_required_for("user", "public") is True
    assert license_required_for("user", "user") is False
    assert license_required_for("org", "org") is True


async def test_lookalike_unverified_claims_cannot_both_become_verified(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await _claim(client, auth_headers, "northstar")
    _, other = await _user(db_session)
    # Both unverified: only exact skeleton collisions block, so this claim works.
    second = await _claim(client, other, "northstar-official")
    org_owner, org_headers = await _user(db_session)
    org, _ = await _org(db_session, org_owner)
    third = await _claim(client, org_headers, "northstar-labs", org_id=org.id)
    await _verify_dns(client, auth_headers, first["id"], "northstar.example", monkeypatch)
    started = await client.post(
        f"/api/v1/publishers/{second['id']}/verifications", headers=other,
        json={"method": "dns_txt", "domain": "northstar-official.example"},
    )
    record = started.json()["txt_record"]

    async def resolve(_name: str) -> list[str]:
        return [record]

    monkeypatch.setattr(domain_verification, "resolve_txt", resolve)
    checked = await client.post(
        f"/api/v1/publishers/{second['id']}/verifications/{started.json()['id']}/check", headers=other
    )
    assert checked.json()["status"] == "pending"
    assert checked.json()["status_reason"] == "namespace_confusable_with_verified_publisher"
    view = (await client.get(f"/api/v1/publishers/{second['id']}", headers=other)).json()
    assert view["verified"] is False
    # The GitHub path refuses the same promotion.
    db_session.add(GitHubInstallation(id=7007, user_id=org_owner.id, account_login="ns", account_type="Organization"))
    await db_session.commit()
    monkeypatch.setattr(svc, "installation_owned_by", _owned)

    async def account(_installation_id: int):
        return {"id": 77, "login": "ns", "type": "Organization", "suspended": False}

    monkeypatch.setattr(github_app, "get_installation_account", account)
    refused = await client.post(
        f"/api/v1/publishers/{third['id']}/verifications", headers=org_headers,
        json={"method": "github_org", "installation_id": 7007},
    )
    assert refused.status_code == 409, refused.text
    assert _error(refused)["code"] == "namespace_confusable"


async def test_public_package_names_cannot_imitate_siblings_in_the_namespace(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "siblingco")
    await _verify_dns(client, auth_headers, publisher["id"], "sibling.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])

    def publish(name: str, body: str = "Inspect."):
        return client.post(
            "/api/v1/users/me/skills/packages", headers=auth_headers,
            files={"package": ("p.tar.gz", _pkg(name=name, body=body), "application/gzip")},
            data={"namespace": "siblingco", "collection": "siblingco", "visibility": "public"},
        )

    assert (await publish("review-helper")).status_code == 201
    for squat in ("review-helpers", "revievv-helper", "review-helper-official"):
        response = await publish(squat)
        assert response.status_code == 409, (squat, response.text)
        assert _error(response)["code"] == "package_name_confusable"
    # The package itself is not its own sibling: a new version still publishes.
    assert (await publish("review-helper", body="v2")).status_code == 201
    assert (await publish("deploy-notes")).status_code == 201


async def test_failed_installation_lifecycle_is_not_acknowledged(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "whsec-test"
    monkeypatch.setattr(webhooks, "GITHUB_WEBHOOK_SECRET", secret)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(svc, "handle_installation_event", boom)
    for action in ("suspend", "deleted"):
        payload = json.dumps(_installation_event(action, 8008, "x", 88)).encode()
        signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        response = await client.post(
            "/webhooks/github", content=payload,
            headers={"X-GitHub-Event": "installation", "X-Hub-Signature-256": signature,
                     "Content-Type": "application/json"},
        )
        assert response.status_code == 500, (action, response.text)


async def test_control_characters_are_checked_on_the_raw_input(
    client: AsyncClient, auth_headers: dict
) -> None:
    # U+001F is whitespace to str.strip(): it must be refused, not stripped.
    response = await client.post(
        "/api/v1/publishers", headers=auth_headers, json={"namespace": "rawcheck", "display_name": "Acme\x1f"}
    )
    assert response.status_code == 422, response.text
    assert _error(response)["code"] == "invalid_display_name"
    publish = await client.post(
        "/api/v1/users/me/skills/packages", headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "rawcheck\x1f", "collection": "c"},
    )
    assert publish.status_code == 422, publish.text
    assert _error(publish)["code"] == "invalid_namespace"


async def test_exact_duplicate_public_name_from_another_package_is_refused(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = await _claim(client, auth_headers, "dupeco")
    await _verify_dns(client, auth_headers, publisher["id"], "dupe.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, publisher["id"])

    def publish(locator: str, body: str = "Inspect."):
        return client.post(
            "/api/v1/users/me/skills/packages", headers=auth_headers,
            files={"package": ("p.tar.gz", _pkg(body=body), "application/gzip")},
            data={"namespace": "dupeco", "collection": "dupeco", "visibility": "public",
                  "source_locator": locator},
        )

    first = await publish("https://github.com/dupeco/one")
    assert first.status_code == 201, first.text
    # Same name, DIFFERENT package identity (another source): refused.
    clone = await publish("https://github.com/dupeco/two")
    assert clone.status_code == 409, clone.text
    assert _error(clone)["code"] == "package_name_confusable"
    # The same package's next version is fine.
    assert (await publish("https://github.com/dupeco/one", body="v2")).status_code == 201


async def test_platform_admin_public_publishes_pass_the_name_gate_unless_overridden(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    admin, admin_headers = await _user(db_session, tier="admin")
    org, _ = await _org(db_session, admin)

    def publish(namespace: str, name: str, **extra):
        return client.post(
            f"/api/v1/orgs/{org.id}/skills/packages", headers=admin_headers,
            files={"package": ("p.tar.gz", _pkg(name=name), "application/gzip")},
            data={"namespace": namespace, "collection": f"c-{namespace}", "visibility": "public", **extra},
        )

    for namespace in ("sessionfs", "community"):
        refused = await publish(namespace, "openai-official")
        assert refused.status_code == 409, (namespace, refused.text)
        assert _error(refused)["code"] == "package_name_confusable"
    overridden = await publish("community", "openai-official", allow_confusable_name="true")
    assert overridden.status_code == 201, overridden.text
    audit = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id, OrgAuditEvent.event_type == "skill_package_published"
            )
        )
    ).scalars().all()
    assert any(json.loads(row.after)["confusable_name_override"] is True for row in audit)
    # Nobody else may use the override.
    own_org, _ = await _org(db_session, test_user)
    denied = await client.post(
        f"/api/v1/orgs/{own_org.id}/skills/packages", headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(name="openai-official"), "application/gzip")},
        data={"namespace": "mine", "collection": "mine", "visibility": "org", "allow_confusable_name": "true"},
    )
    assert denied.status_code == 403, denied.text


async def test_promotion_is_refused_when_a_public_package_already_imitates_the_namespace(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, other = await _user(db_session)
    late = await _claim(client, other, "polaris")  # claimed, NOT verified yet
    established = await _claim(client, auth_headers, "starmaps")
    await _verify_dns(client, auth_headers, established["id"], "starmaps.example", monkeypatch)
    _, admin_headers = await _user(db_session, tier="admin")
    await _enable_public(client, admin_headers, established["id"])
    squat = await client.post(
        "/api/v1/users/me/skills/packages", headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(name="polaris-official"), "application/gzip")},
        data={"namespace": "starmaps", "collection": "starmaps", "visibility": "public"},
    )
    assert squat.status_code == 201, squat.text  # `polaris` was not protected yet
    started = await client.post(
        f"/api/v1/publishers/{late['id']}/verifications", headers=other,
        json={"method": "dns_txt", "domain": "polaris.example"},
    )
    record = started.json()["txt_record"]

    async def resolve(_name: str) -> list[str]:
        return [record]

    monkeypatch.setattr(domain_verification, "resolve_txt", resolve)
    checked = await client.post(
        f"/api/v1/publishers/{late['id']}/verifications/{started.json()['id']}/check", headers=other
    )
    assert checked.json()["status"] == "pending"
    assert checked.json()["status_reason"] == "namespace_confusable_with_verified_publisher"


async def test_trusted_publishers_are_refused_for_the_platform_publisher(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_headers = await _user(db_session, tier="admin")
    org, _ = await _org(db_session, admin)
    await client.post(
        f"/api/v1/orgs/{org.id}/skills/packages", headers=admin_headers,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "sessionfs", "collection": "catalog", "visibility": "public"},
    )
    response = await client.post(
        f"/api/v1/publishers/{svc.SYSTEM_PUBLISHER_ID}/trusted-publishers", headers=admin_headers,
        json={"repository": "o/r", "workflow": "release.yml", "repository_id": 1, "repository_owner_id": 2},
    )
    assert response.status_code == 409, response.text
    assert _error(response)["code"] == "system_publisher"


async def test_trusted_publisher_add_rechecks_membership_after_the_github_lookup(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, _ = await _org(db_session, test_user)
    admin_user, admin_headers = await _user(db_session)
    await _join(db_session, org, admin_user, "admin")
    publisher = await _claim(client, auth_headers, "fenceco", org_id=org.id)

    async def resolve_and_remove(_db, _user_id, owner, repo):
        # The admin is removed while the GitHub lookup is in flight.
        from sqlalchemy import delete as sa_delete

        await db_session.execute(
            sa_delete(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == admin_user.id)
        )
        await db_session.commit()
        return {"id": 11, "owner_id": 22, "full_name": f"{owner}/{repo}"}

    monkeypatch.setattr(github_app, "resolve_repository", resolve_and_remove)
    response = await client.post(
        f"/api/v1/publishers/{publisher['id']}/trusted-publishers", headers=admin_headers,
        json={"repository": "fenceco/skills", "workflow": "release.yml"},
    )
    assert response.status_code in (403, 404), response.text
    from sessionfs.server.db.models import SkillTrustedPublisher

    rows = (await db_session.execute(
        select(SkillTrustedPublisher).where(SkillTrustedPublisher.publisher_id == publisher["id"])
    )).scalars().all()
    assert rows == []


async def test_sso_domain_verify_rechecks_the_caller_after_dns(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    from unittest.mock import patch

    org, _ = await _org(db_session, test_user)
    admin_user, admin_headers = await _user(db_session)
    await _join(db_session, org, admin_user, "admin")
    requested = await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains", headers=admin_headers, json={"domain": "fenced.example"}
    )
    assert requested.status_code == 200, requested.text
    token = requested.json()["verification_token"]

    async def dns_then_remove(_domain: str) -> list[str]:
        from sqlalchemy import delete as sa_delete

        await db_session.execute(
            sa_delete(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == admin_user.id)
        )
        await db_session.commit()
        return [f"sessionfs-verification={token}"]

    with patch("sessionfs.server.routes.sso_admin._resolve_txt", new=dns_then_remove):
        response = await client.post(
            f"/api/v1/orgs/{org.id}/sso/domains/{requested.json()['id']}/verify", headers=admin_headers
        )
    assert response.status_code == 404, response.text
    row = await db_session.get(OrgDomainVerification, requested.json()["id"])
    await db_session.refresh(row)
    assert row.status == "pending"


async def test_unverified_retired_namespace_keeps_only_exact_reservation(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    retired = await _claim(client, auth_headers, "lakeshore")
    await client.delete(f"/api/v1/publishers/{retired['id']}", headers=auth_headers)
    _, other = await _user(db_session)
    exact = await client.post("/api/v1/publishers", headers=other, json={"namespace": "lakesh0re"})
    assert exact.status_code == 409
    assert "retired" in _error(exact)["message"]
    # Never verified, so retiring it does not create strong protection.
    affix = await client.post("/api/v1/publishers", headers=other, json={"namespace": "lakeshore-official"})
    assert affix.status_code == 201, affix.text


async def test_sso_domains_use_the_canonical_spelling(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import patch

    org, _ = await _org(db_session, test_user)
    requested = await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains", headers=auth_headers, json={"domain": " Canon.Example. "}
    )
    assert requested.status_code == 200, requested.text
    assert requested.json()["domain"] == "canon.example"
    bad = await client.post(f"/api/v1/orgs/{org.id}/sso/domains", headers=auth_headers, json={"domain": "not a domain"})
    assert bad.status_code == 422
    # A publisher of another owner verifies the canonical spelling first...
    _, other = await _user(db_session)
    pub = await _claim(client, other, "canonco")
    await _verify_dns(client, other, pub["id"], "canon.example", monkeypatch)
    # ...so the SSO promotion of the (formerly trailing-dot) spelling is refused.
    token = requested.json()["verification_token"]

    async def dns(_domain: str) -> list[str]:
        return [f"sessionfs-verification={token}"]

    with patch("sessionfs.server.routes.sso_admin._resolve_txt", new=dns):
        promoted = await client.post(
            f"/api/v1/orgs/{org.id}/sso/domains/{requested.json()['id']}/verify", headers=auth_headers
        )
    assert promoted.status_code == 409, promoted.text


async def test_platform_admin_cannot_create_trusts_for_others(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    personal = await _claim(client, auth_headers, "mytrustco")
    org, _ = await _org(db_session, test_user)
    org_pub = await _claim(client, auth_headers, "orgtrustco", org_id=org.id)
    _, admin_headers = await _user(db_session, tier="admin")
    body = {"repository": "o/r", "workflow": "release.yml", "repository_id": 1, "repository_owner_id": 2}
    for publisher_id in (personal["id"], org_pub["id"]):
        response = await client.post(
            f"/api/v1/publishers/{publisher_id}/trusted-publishers", headers=admin_headers, json=body
        )
        assert response.status_code == 403, response.text
    own = await client.post(f"/api/v1/publishers/{personal['id']}/trusted-publishers", headers=auth_headers, json=body)
    assert own.status_code == 201, own.text


async def test_org_claim_and_org_publish_reauthorize_under_the_locks(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin removed after the initial authorization but before the claim
    / publish commits is refused by the final fenced re-authorization."""
    from sqlalchemy import delete as sa_delete

    from sessionfs.server.routes import marketplace as mp

    org, _ = await _org(db_session, test_user)
    admin_user, admin_headers = await _user(db_session)
    await _join(db_session, org, admin_user, "admin")
    original_lock = svc.lock_namespace

    async def lock_then_remove(db, namespace):
        await original_lock(db, namespace)
        await db_session.execute(
            sa_delete(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == admin_user.id)
        )
        await db_session.commit()

    monkeypatch.setattr(svc, "lock_namespace", lock_then_remove)
    claim = await client.post(
        "/api/v1/publishers", headers=admin_headers, json={"namespace": "fencedclaim", "org_id": org.id}
    )
    assert claim.status_code == 403, claim.text
    await _join(db_session, org, admin_user, "admin")
    monkeypatch.setattr(mp.publishers_service, "lock_namespace", lock_then_remove)
    publish = await client.post(
        f"/api/v1/orgs/{org.id}/skills/packages", headers=admin_headers,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "fencedpub", "collection": "c"},
    )
    assert publish.status_code == 403, publish.text


async def test_personal_claim_refused_when_deactivated_mid_flight(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim's quota lock re-reads is_active: a deactivation committed
    after authentication must not acquire a permanent namespace."""
    from sqlalchemy import update as sa_update

    original_lock = svc.lock_namespace

    async def lock_then_deactivate(db, namespace):
        await original_lock(db, namespace)
        await db_session.execute(sa_update(User).where(User.id == test_user.id).values(is_active=False))
        await db_session.commit()

    monkeypatch.setattr(svc, "lock_namespace", lock_then_deactivate)
    response = await client.post(
        "/api/v1/publishers", headers=auth_headers, json={"namespace": "deactivated-claim"}
    )
    assert response.status_code == 403, response.text


async def test_personal_claim_rechecks_the_tier_under_the_lock(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downgrade committed after authentication is seen by the claim's
    final fenced tier re-check (under the quota locks, held through commit)."""
    from sqlalchemy import update as sa_update

    original_lock = svc.lock_namespace

    async def lock_then_downgrade(db, namespace):
        await original_lock(db, namespace)
        await db_session.execute(sa_update(User).where(User.id == test_user.id).values(tier="free"))
        await db_session.commit()

    monkeypatch.setattr(svc, "lock_namespace", lock_then_downgrade)
    response = await client.post(
        "/api/v1/publishers", headers=auth_headers, json={"namespace": "downgraded-claim"}
    )
    assert response.status_code == 403, response.text
    assert _error(response)["code"] == "upgrade_required"


async def test_personal_publish_refused_when_deactivated_mid_flight(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Personal publishing re-authorizes inside the final transaction: a
    deactivation committed after authentication refuses the publish."""
    from sqlalchemy import update as sa_update

    original_lock = svc.lock_namespace

    async def lock_then_deactivate(db, namespace):
        await original_lock(db, namespace)
        await db_session.execute(sa_update(User).where(User.id == test_user.id).values(is_active=False))
        await db_session.commit()

    monkeypatch.setattr(svc, "lock_namespace", lock_then_deactivate)
    publish = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "personal", "collection": "c"},
    )
    assert publish.status_code == 403, publish.text


async def test_personal_publish_rechecks_the_tier_in_the_final_transaction(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A downgrade committed after authentication is seen by the publish's
    reauthorize fence, so an in-flight publish cannot commit."""
    from sqlalchemy import update as sa_update

    original_lock = svc.lock_namespace

    async def lock_then_downgrade(db, namespace):
        await original_lock(db, namespace)
        await db_session.execute(sa_update(User).where(User.id == test_user.id).values(tier="free"))
        await db_session.commit()

    monkeypatch.setattr(svc, "lock_namespace", lock_then_downgrade)
    publish = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _pkg(), "application/gzip")},
        data={"namespace": "personal", "collection": "c"},
    )
    assert publish.status_code == 403, publish.text
    assert _error(publish)["code"] == "upgrade_required"
