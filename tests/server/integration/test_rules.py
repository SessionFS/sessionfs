"""Integration tests for the rules API (migration 028)."""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    KnowledgeEntry,
    Project,
    User,
)


@pytest.fixture
async def project(db_session: AsyncSession, test_user: User) -> Project:
    p = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="TestProj",
        git_remote_normalized="github.com/example/rules-test",
        context_document=(
            "# Project Context\n\n"
            "## Overview\nSession portability.\n\n"
            "## Architecture\nFastAPI + PG.\n\n"
            "## Conventions\nSnake case.\n"
        ),
        owner_id=test_user.id,
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


class TestRulesCRUD:
    @pytest.mark.asyncio
    async def test_get_rules_creates_default(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == project.id
        assert body["version"] == 1
        assert body["static_rules"] == ""
        assert body["enabled_tools"] == []
        assert set(body["knowledge_types"]) == {"convention", "decision"}
        assert set(body["context_sections"]) == {"overview", "architecture"}
        assert "etag" in body
        assert "ETag" in resp.headers

    @pytest.mark.asyncio
    async def test_update_rules_requires_if_match(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        # First fetch to create + grab etag
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag = resp.json()["etag"]

        # PUT without If-Match -> 428
        resp2 = await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers=auth_headers,
            json={"static_rules": "Hello"},
        )
        assert resp2.status_code == 428

        # PUT with correct If-Match -> 200
        resp3 = await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"static_rules": "Hello"},
        )
        assert resp3.status_code == 200
        assert resp3.json()["static_rules"] == "Hello"

    @pytest.mark.asyncio
    async def test_stale_etag_returns_409(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag = resp.json()["etag"]

        # First write succeeds (consumes the etag)
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"static_rules": "First"},
        )
        # Second write with the old etag returns 409
        resp2 = await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"static_rules": "Second"},
        )
        assert resp2.status_code == 409

    @pytest.mark.asyncio
    async def test_enabled_tools_validated(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag = resp.json()["etag"]
        resp2 = await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"enabled_tools": ["claude-code", "windsurf"]},
        )
        assert resp2.status_code == 422

        # Valid subset accepted
        resp3 = await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"enabled_tools": ["claude-code", "cursor"]},
        )
        assert resp3.status_code == 200


class TestCompile:
    @pytest.mark.asyncio
    async def test_compile_produces_outputs_for_enabled_tools(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        # Enable claude-code + cursor
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag = resp.json()["etag"]
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={
                "enabled_tools": ["claude-code", "cursor"],
                "static_rules": "Prefer tabs.",
            },
        )
        resp2 = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers,
            json={},
        )
        assert resp2.status_code == 200
        body = resp2.json()
        assert body["created_new_version"] is True
        tools = {o["tool"] for o in body["outputs"]}
        assert tools == {"claude-code", "cursor"}
        # Each output is managed
        for o in body["outputs"]:
            assert "sessionfs-managed" in o["content"].lower()
            assert "Prefer tabs" in o["content"]

    @pytest.mark.asyncio
    async def test_no_version_bump_when_unchanged(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag = resp.json()["etag"]
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"enabled_tools": ["claude-code"], "static_rules": "X"},
        )
        r1 = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )
        v1 = r1.json()["version"]
        r2 = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )
        assert r2.json()["created_new_version"] is False
        assert r2.json()["version"] == v1

    @pytest.mark.asyncio
    async def test_version_bumps_on_content_change(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag = resp.json()["etag"]
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"enabled_tools": ["claude-code"], "static_rules": "First"},
        )
        r1 = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )
        v1 = r1.json()["version"]

        # Update static_rules -> must bump
        resp2 = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag2 = resp2.json()["etag"]
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag2},
            json={"static_rules": "Second"},
        )
        r2 = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )
        assert r2.json()["created_new_version"] is True
        assert r2.json()["version"] > v1

    @pytest.mark.asyncio
    async def test_no_op_after_real_version_bump(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        """After a bump, a follow-up compile with identical inputs must NOT
        bump again. Regression test: content_hash used to include the marker,
        which embeds rules.version → every post-bump compile falsely detected
        a change and bumped the version indefinitely.
        """
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag = resp.json()["etag"]
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"enabled_tools": ["claude-code"], "static_rules": "A"},
        )
        r1 = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )
        v1 = r1.json()["version"]
        assert r1.json()["created_new_version"] is True

        # Change something then compile -> bump.
        resp2 = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": resp2.json()["etag"]},
            json={"static_rules": "B"},
        )
        r2 = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )
        v2 = r2.json()["version"]
        assert r2.json()["created_new_version"] is True
        assert v2 > v1

        # Third compile with no change must be a no-op, staying at v2.
        r3 = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )
        assert r3.json()["created_new_version"] is False, (
            "No-op detection broke after a real version bump"
        )
        assert r3.json()["version"] == v2

    @pytest.mark.asyncio
    async def test_partial_tool_compile_does_not_version(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        """--tool <name> must not write a partial rules_versions row.

        If a user compiles just one tool out of many enabled, the canonical
        version snapshot would otherwise contain only that one tool's output,
        breaking history semantics.
        """
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": resp.json()["etag"]},
            json={
                "enabled_tools": ["claude-code", "codex"],
                "static_rules": "multi-tool",
            },
        )
        r_full = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )
        v_after_full = r_full.json()["version"]

        # Partial compile for just one tool.
        r_partial = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers,
            json={"tools": ["claude-code"]},
        )
        body = r_partial.json()
        assert body["created_new_version"] is False, (
            "--tool compile must not version"
        )
        assert body["version"] == v_after_full
        # Only the requested tool came back.
        assert {o["tool"] for o in body["outputs"]} == {"claude-code"}

    @pytest.mark.asyncio
    async def test_single_tool_override_matching_canonical_still_partial(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        """Regression: if canonical enabled_tools=[codex] and a caller compiles
        with tools=[codex], an earlier set-equality ``is_partial`` check marked
        this as a non-partial compile and bumped canonical history. Resume-time
        sync (which always passes the target tool as an override) would have
        created a canonical version row per resume — violating the brief. Any
        explicit ``tools`` override must now be treated as partial, regardless
        of whether it matches enabled_tools.
        """
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": resp.json()["etag"]},
            json={"enabled_tools": ["codex"], "static_rules": "solo-tool"},
        )
        # Seed version history via a canonical (no-override) compile.
        r_seed = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )
        v_seed = r_seed.json()["version"]

        # Partial compile with the SAME tool that's the only enabled one.
        # This used to version. It must now be a no-op at the canonical level.
        # Change static_rules first so the compiled content actually differs —
        # this way we detect a spurious bump instead of just hitting the
        # same-hash short-circuit.
        resp2 = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": resp2.json()["etag"]},
            json={"static_rules": "solo-tool-updated"},
        )
        r_partial = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers,
            json={"tools": ["codex"]},
        )
        assert r_partial.json()["created_new_version"] is False, (
            "tools override must always be partial, even if set equals enabled_tools"
        )
        # Version list unchanged since seed.
        vlist = await client.get(
            f"/api/v1/projects/{project.id}/rules/versions",
            headers=auth_headers,
        )
        assert len(vlist.json()) == 1
        assert vlist.json()[0]["version"] == v_seed


class TestOptimisticConcurrency:
    @pytest.mark.asyncio
    async def test_stale_etag_rejected_atomically(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        """Two writers with the same prior ETag: only one commits."""
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag = resp.json()["etag"]

        # First writer wins.
        r1 = await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"static_rules": "winner"},
        )
        assert r1.status_code == 200

        # Second writer, same prior ETag — must 409.
        r2 = await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"static_rules": "loser"},
        )
        assert r2.status_code == 409

        # Final state is the winner's value.
        resp_final = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        assert resp_final.json()["static_rules"] == "winner"

    @pytest.mark.asyncio
    async def test_get_or_create_rules_is_idempotent(
        self,
        db_session: AsyncSession,
        project: Project,
        test_user: User,
    ):
        """Calling ``get_or_create_rules`` twice returns the same row — the
        second call observes the first's INSERT and returns via the SELECT
        branch instead of trying to re-INSERT (which would raise on the
        project_id unique constraint). This exercises the common half of
        the race-safe creation path.
        """
        from sessionfs.server.services.rules import get_or_create_rules

        rules1 = await get_or_create_rules(db_session, project, test_user.id)
        rules2 = await get_or_create_rules(db_session, project, test_user.id)

        assert rules1.id == rules2.id
        assert rules1.project_id == rules2.project_id == project.id

        # And the IntegrityError branch is a defensive ``except`` — document
        # that intent here so future refactors don't drop it. Full concurrent
        # testing would require running two real sessions against the same
        # engine, which the shared ``db_session`` fixture cannot offer.

    @pytest.mark.asyncio
    async def test_compile_retries_on_version_collision(
        self,
        db_session: AsyncSession,
        project: Project,
        test_user: User,
    ):
        """If the version-N insert collides with a concurrent winner, the
        compile must retry, observe the new latest, and either short-circuit
        no-op or bump to N+1. Must not surface IntegrityError to the caller.
        """
        import json
        import uuid
        from datetime import datetime, timezone

        from sessionfs.server.db.models import ProjectRules, RulesVersion
        from sessionfs.server.services.rules import (
            compile_rules,
            get_or_create_rules,
        )

        rules = await get_or_create_rules(db_session, project, test_user.id)
        rules.enabled_tools = json.dumps(["claude-code"])
        rules.static_rules = "race-base"
        await db_session.commit()
        await db_session.refresh(rules)

        # Pre-insert version 1 as if a concurrent compile won the race.
        racing_winner = RulesVersion(
            id=f"rv_{uuid.uuid4().hex[:16]}",
            rules_id=rules.id,
            version=1,
            static_rules="other-content",
            compiled_outputs=json.dumps({}),
            knowledge_snapshot=json.dumps([]),
            context_snapshot=json.dumps({}),
            compiled_at=datetime.now(timezone.utc),
            compiled_by=test_user.id,
            content_hash="deadbeef" * 8,
        )
        db_session.add(racing_winner)
        await db_session.commit()
        rules.version = 1
        await db_session.commit()

        # This compile sees a different content_hash than the pre-existing
        # version, so it must bump to version 2 — retry path exercised.
        outcome = await compile_rules(
            db_session, project, rules, test_user.id
        )
        assert outcome.created_version is not None
        assert outcome.created_version.version == 2

    @pytest.mark.asyncio
    async def test_compile_noop_after_concurrent_winner_aligns_version(
        self,
        db_session: AsyncSession,
        project: Project,
        test_user: User,
    ):
        """Race-loser path: another request already committed a new version
        with the same body hash. Our compile must short-circuit AND align
        rules.version + rendered outputs to the winner's version — not
        return the stale pre-race version with stale markers.
        """
        import json
        import uuid
        from datetime import datetime, timezone

        from sessionfs.server.db.models import RulesVersion
        from sessionfs.server.services.rules import (
            build_compile_context,
            compile_for_tools,
            compile_rules,
            get_or_create_rules,
        )
        from sessionfs.server.services.rules_compiler import aggregate_hash

        rules = await get_or_create_rules(db_session, project, test_user.id)
        rules.enabled_tools = json.dumps(["claude-code"])
        rules.static_rules = "noop-base"
        await db_session.commit()
        await db_session.refresh(rules)

        # Compute what compile would produce right now — that's what the
        # winner would have stored.
        ctx_preview = await build_compile_context(db_session, project, rules)
        # Simulate the winner rendering with version = 2 (the post-bump view).
        ctx_preview.version = 2
        winner_results = compile_for_tools(ctx_preview, ["claude-code"])
        winner_hash = aggregate_hash(winner_results)

        # Pre-insert the winner's version 2 with matching content_hash.
        winner_row = RulesVersion(
            id=f"rv_{uuid.uuid4().hex[:16]}",
            rules_id=rules.id,
            version=2,
            static_rules="noop-base",
            compiled_outputs=json.dumps({}),
            knowledge_snapshot=json.dumps([]),
            context_snapshot=json.dumps({}),
            compiled_at=datetime.now(timezone.utc),
            compiled_by=test_user.id,
            content_hash=winner_hash,
        )
        db_session.add(winner_row)
        await db_session.commit()
        # Leave rules.version at 1 locally — we are the race loser who still
        # thinks we're at version 1 while the DB already has version 2.
        assert rules.version == 1

        outcome = await compile_rules(
            db_session, project, rules, test_user.id
        )

        # Short-circuit (no new version created)...
        assert outcome.created_version is None
        # ...but the local rules.version was aligned to the committed winner.
        assert rules.version == 2
        # And the returned results carry the winner's version in their markers.
        for r in outcome.results:
            assert "version=2" in r.content.lower()


class TestKnowledgeInjection:
    @pytest.mark.asyncio
    async def test_only_active_claims_injected(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        test_user: User,
        project: Project,
    ):
        # Create one active + one dismissed + one superseded claim
        active = KnowledgeEntry(
            project_id=project.id,
            session_id="ses_a",
            user_id=test_user.id,
            entry_type="convention",
            content="Active convention claim content here for testing injection",
            claim_class="claim",
            freshness_class="current",
            dismissed=False,
        )
        dismissed = KnowledgeEntry(
            project_id=project.id,
            session_id="ses_b",
            user_id=test_user.id,
            entry_type="convention",
            content="Dismissed content — should never appear in compiled output",
            claim_class="claim",
            freshness_class="current",
            dismissed=True,
        )
        note = KnowledgeEntry(
            project_id=project.id,
            session_id="ses_c",
            user_id=test_user.id,
            entry_type="convention",
            content="Note-class entry — should not be injected into rules",
            claim_class="note",
            freshness_class="current",
            dismissed=False,
        )
        bug = KnowledgeEntry(
            project_id=project.id,
            session_id="ses_d",
            user_id=test_user.id,
            entry_type="bug",
            content="A bug entry — bug type must not be in default knowledge types",
            claim_class="claim",
            freshness_class="current",
            dismissed=False,
        )
        db_session.add_all([active, dismissed, note, bug])
        await db_session.commit()

        # Enable claude-code + compile
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag = resp.json()["etag"]
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"enabled_tools": ["claude-code"]},
        )
        resp2 = await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )
        body = resp2.json()
        content = body["outputs"][0]["content"]
        assert "Active convention claim" in content
        assert "Dismissed content" not in content
        assert "Note-class entry" not in content
        assert "A bug entry" not in content


class TestVersionsList:
    @pytest.mark.asyncio
    async def test_list_versions_and_fetch_detail(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        etag = resp.json()["etag"]
        await client.put(
            f"/api/v1/projects/{project.id}/rules",
            headers={**auth_headers, "If-Match": etag},
            json={"enabled_tools": ["claude-code"], "static_rules": "V1"},
        )
        await client.post(
            f"/api/v1/projects/{project.id}/rules/compile",
            headers=auth_headers, json={},
        )

        list_resp = await client.get(
            f"/api/v1/projects/{project.id}/rules/versions", headers=auth_headers
        )
        assert list_resp.status_code == 200
        assert len(list_resp.json()) >= 1
        v = list_resp.json()[0]
        detail = await client.get(
            f"/api/v1/projects/{project.id}/rules/versions/{v['version']}",
            headers=auth_headers,
        )
        assert detail.status_code == 200
        data = detail.json()
        assert data["version"] == v["version"]
        assert "claude-code" in data["compiled_outputs"]

    @pytest.mark.asyncio
    async def test_fetch_missing_version_returns_404(
        self, client: AsyncClient, auth_headers, project: Project
    ):
        # Ensure rules exist
        await client.get(
            f"/api/v1/projects/{project.id}/rules", headers=auth_headers
        )
        resp = await client.get(
            f"/api/v1/projects/{project.id}/rules/versions/999",
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestSessionProvenanceFields:
    """Migration 028 added 4 fields to sessions — verify they exist and default."""

    @pytest.mark.asyncio
    async def test_default_values(
        self, db_session: AsyncSession, uploaded_session
    ):
        assert uploaded_session.rules_source == "none"
        assert uploaded_session.rules_version is None
        assert uploaded_session.rules_hash is None
        assert uploaded_session.instruction_artifacts == "[]"
