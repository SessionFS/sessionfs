"""Hosted SessionFS Marketplace consumer flows: search, add, sync, update,
pull (assignments + approved install requests), revocations, daemon."""

from __future__ import annotations

import errno
import io
import json
import os
import re
import shutil
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from sessionfs.cli.cmd_skill import skill_app
from sessionfs.daemon.skill_reconcile import FAILURE_BACKOFF_S, SkillReconciler
from sessionfs.server.services.skill_packages import validate_hosted_package
from sessionfs.skills import hosted
from sessionfs.skills import state as state_module
from sessionfs.skills.hosted import HostedClient, HostedState, parse_hosted_ref
from sessionfs.skills.manager import SkillManager
from sessionfs.skills.models import SkillError
from sessionfs.skills.package import AGENT_PLUGIN_SCHEMA

runner = CliRunner()


@pytest.fixture(autouse=True)
def _plain_rich_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import typer.rich_utils

    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", False)
    monkeypatch.delenv("SESSIONFS_DEVICE_ID", raising=False)
    # Every test here also asserts the global lock order
    # (scope -> operation -> hosted-state) on every acquisition.
    monkeypatch.setenv("SESSIONFS_LOCK_ORDER_CHECK", "1")


def _upload(
    name: str, version: str, *, body: str = "Do the thing.", scripts: bool = False,
    hook_extension: bool = False,
) -> bytes:
    manifest = json.dumps(
        {
            "$schema": AGENT_PLUGIN_SCHEMA,
            "name": name,
            "version": version,
            "description": f"{name} fixture",
        }
    ).encode()
    skill = f"---\nname: {name}\ndescription: {name} skill.\n---\n\n# {name}\n\n{body}\n".encode()
    members = [("plugin.json", manifest), (f"skills/{name}/SKILL.md", skill)]
    if scripts:
        members.append((f"skills/{name}/scripts/check.py", b"print('ok')\n"))
    if hook_extension:  # an inert hooks.json: still an executable extension
        members.append((f"skills/{name}/hooks.json", b'{"hooks": {}}\n'))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, payload in members:
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


class FakeMarketplace:
    """In-memory stand-in for the hosted marketplace API."""

    def __init__(self) -> None:
        self.versions: dict[str, dict[str, Any]] = {}
        self.current: dict[str, str] = {}
        self.assignments: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        # Revocation event log: (changed_at, event, visible_to api keys | None=public).
        self.feed_enabled = False
        self.events: list[tuple[str, dict[str, Any], set[str] | None]] = []
        self.revocation_calls: list[str | None] = []
        self.users = {"sk_test": "usr_me", "sk_other": "usr_other"}
        self.service_keys: set[str] = set()
        self.projects: dict[str, str] = {"github.com/acme/repo": "proj_1"}
        self.has_resolve_route = True  # False simulates an older server
        # New feed contract (has_more); False simulates an older server.
        self.has_more_field = True
        self.tamper: dict[str, bytes] = {}
        self.header_override: dict[str, str] = {}
        self.downloads: list[str] = []
        self.user_id = "usr_me"
        # Extra listing fields per source (e.g. the server's `incomplete`).
        self.listing_extra: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def publish(
        self,
        name: str,
        version: str,
        *,
        namespace: str = "acme",
        package_id: str | None = None,
        owner_org_id: Any = "absent",
        visibility: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        hosted_pkg = validate_hosted_package(_upload(name, version, **kwargs), source_locator="upload:test")
        self._seq += 1
        package_id = package_id or "spk_" + f"{self._seq:024x}"
        version_id = "skv_" + f"{self._seq:024x}"
        item = {
            "package_id": package_id,
            "namespace": namespace,
            "name": name,
            "description": f"{name} fixture",
            "version_id": version_id,
            "version": version,
            "content_hash": hosted_pkg.content_hash,
            "risk": hosted_pkg.risk_summary,
        }
        if owner_org_id != "absent":  # newer server: owner exposed (None = personal/public)
            item["owner_org_id"] = owner_org_id
        if visibility is not None:  # the listing's visibility (_package_response)
            item["visibility"] = visibility
        self.versions[version_id] = {"item": item, "archive": hosted_pkg.canonical_archive}
        self.current[package_id] = version_id
        return item

    def _page(self, items: list[Any], params: Any) -> dict[str, Any]:
        """Grant-list paging (`page_size` set: cursor / has_more /
        next_cursor; unset: an older server's single unpaged response)."""
        size = getattr(self, "page_size", None)
        if not size:
            return {"items": list(items)}
        self.grant_page_calls = getattr(self, "grant_page_calls", 0) + 1
        cursor = params.get("cursor") or "0"
        start = int(cursor) if cursor.isdigit() else 0
        page = list(items[start:start + size])
        more = start + size < len(items)
        return {"items": page, "incomplete": False, "omitted_count": 0, "has_more": more,
                "next_cursor": str(start + size) if more else None}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if path == "/api/v1/marketplace/packages":
            q = (params.get("q") or "").casefold()
            items = [
                entry["item"] for entry in self.versions.values()
                if q in entry["item"]["name"] or q in entry["item"]["description"].casefold()
            ]
            return httpx.Response(200, json={"items": items, "count": len(items), "has_more": False,
                                             "next_cursor": None})
        match = re.fullmatch(r"/api/v1/marketplace/packages/(spk_\w+)", path)
        if match:
            vid = self.current.get(match.group(1))
            if vid is None:
                return httpx.Response(404, json={"detail": "Marketplace package not found"})
            return httpx.Response(200, json=self.versions[vid]["item"])
        match = re.fullmatch(r"/api/v1/marketplace/versions/(skv_\w+)/(preview|download)", path)
        if match:
            entry = self.versions.get(match.group(1))
            if entry is None:
                return httpx.Response(404, json={"detail": "Marketplace package not found"})
            if match.group(2) == "preview":
                return httpx.Response(200, json=entry["item"])
            self.downloads.append(match.group(1))
            body = self.tamper.get(match.group(1), entry["archive"])
            header = self.header_override.get(match.group(1), entry["item"]["content_hash"])
            return httpx.Response(200, content=body, headers={"X-SessionFS-Content-Hash": header})
        if path == "/api/v1/auth/me":
            key = request.headers["authorization"].removeprefix("Bearer ")
            if key in self.service_keys:
                # The server's final envelope (older shapes: see the parser tests).
                return httpx.Response(403, json={"error": {
                    "code": "service_key_not_allowed", "message": "user keys only",
                    "details": {"error": "service_key_not_allowed", "service_key_name": "svc"}}})
            return httpx.Response(200, json={"user_id": self.users.get(key, self.user_id)})
        if path == "/api/v1/marketplace/projects/resolve" and self.has_resolve_route:
            project = self.projects.get(params.get("git_remote") or "")
            if project is None:
                return httpx.Response(404, json={"error": {
                    "code": "404", "message": "Project not found", "details": {}}})
            return httpx.Response(200, json={"project_id": project, "name": "Repo"})
        if path.startswith("/api/v1/projects/") and path.count("/") > 4 and "skill-install" not in path:
            # Older user-only lookup by remote.
            key = request.headers["authorization"].removeprefix("Bearer ")
            if key in self.service_keys:
                return httpx.Response(403, json={"error": {
                    "code": "403", "message": "m", "details": {"error": "service_key_not_allowed"}}})
            project = self.projects.get(path.removeprefix("/api/v1/projects/"))
            if project is None:
                return httpx.Response(404, json={"error": {"code": "404", "message": "Not Found"}})
            return httpx.Response(200, json={"id": project})
        if path == "/api/v1/users/me/skills/assignments":
            device = params.get("device_id")
            items = [
                row for row in self.assignments
                if not isinstance(row, dict)  # malformed rows are served as-is
                or row["target_type"] != "device" or row["target_id"] == device
            ]
            return httpx.Response(200, json={**self._page(items, params),
                                             **self.listing_extra.get("assignment", {})})
        if path == "/api/v1/projects/proj_1/skill-install-requests":
            return httpx.Response(200, json={**self._page(self.requests, params),
                                             "can_approve": False,
                                             **self.listing_extra.get("request", {})})
        if path == "/api/v1/marketplace/revocations":
            since = params.get("since")
            self.revocation_calls.append(since)
            if not self.feed_enabled:
                return httpx.Response(404, json={"detail": "Not Found"})
            key = request.headers["authorization"].removeprefix("Bearer ")
            # Visibility-scoped, ordered by changed_at, paged 2 at a time.
            pending = [
                event for changed_at, event, audience in sorted(self.events, key=lambda e: e[0])
                if (audience is None or key in audience) and (since is None or changed_at > since)
            ]
            visible = pending[:2]
            next_since = visible[-1]["changed_at"] if visible else (since or "1970-01-01T00:00:00+00:00")
            body: dict[str, Any] = {"revocations": visible, "next_since": next_since}
            if self.has_more_field:
                body["has_more"] = len(pending) > 2
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"detail": "Not Found"})

    def client(self, key: str = "sk_test", url: str = "https://sfs.test") -> HostedClient:
        self.calls_by_url = getattr(self, "calls_by_url", {})

        def _handler(request: httpx.Request) -> httpx.Response:
            self.calls_by_url[url] = self.calls_by_url.get(url, 0) + 1
            return self.handler(request)

        return HostedClient(url, key, transport=httpx.MockTransport(_handler))

    def revoke(
        self,
        item: dict[str, Any],
        state: str,
        reason: str = "",
        *,
        at: str | None = None,
        visible_to: set[str] | None = None,
        kind: str | None = None,
        scope_org_id: str | None = None,
        block_scope: str | None = None,
    ) -> str:
        self.feed_enabled = True
        changed_at = at or f"2026-10-15T00:{len(self.events):02d}:00+00:00"
        event: dict[str, Any] = {
            "package_id": item["package_id"],
            "version_id": item["version_id"],
            "content_hash": item["content_hash"],
            "state": state,
            "reason": reason,
            "changed_at": changed_at,
        }
        if kind is not None:
            event["kind"] = kind
            event["block_scope"] = block_scope
            event["scope_org_id"] = scope_org_id
            if kind == "content":
                event["package_id"] = None
                event["version_id"] = None
            elif kind == "package":
                event["version_id"] = None
                event["content_hash"] = None
        self.events.append((changed_at, event, visible_to))
        return changed_at

    def assign(self, item: dict[str, Any], *, mode: str = "required", tools: list[str] | None = None,
               target_type: str = "org", target_id: str = "*") -> dict[str, Any]:
        self._assign_seq = getattr(self, "_assign_seq", 0) + 1  # never reuses an id
        row = {
            "id": f"osa_{self._assign_seq:024x}",
            "org_id": "org_1",
            "version_id": item["version_id"],
            "content_hash": item["content_hash"],
            "target_type": target_type,
            "target_id": target_id,
            "mode": mode,
            "target_tools": tools if tools is not None else ["codex"],
        }
        self.assignments.append(row)
        return row


@pytest.fixture
def market() -> FakeMarketplace:
    return FakeMarketplace()


def _manager(tmp_path: Path, market: FakeMarketplace) -> tuple[SkillManager, Path, Path]:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    manager = SkillManager(
        project_root=project,
        home_dir=home,
        store_root=home / ".sessionfs" / "skills",
        hosted_client=market.client(),
    )
    return manager, project, home


def _no_project(monkeypatch: pytest.MonkeyPatch, project_id: str | None = None) -> None:
    def _resolve(manager, client):
        if project_id is None:
            raise SkillError("this directory is not inside a git repository")
        return project_id

    monkeypatch.setattr(hosted, "_project_id", _resolve)


_DROP = object()


def _with_provenance(row: dict, **changes) -> dict:
    """A copy of an installed row with its row-local provenance changed;
    `None` for visibility (or `_DROP`) removes the field (unknown)."""
    provenance = dict(row.get("provenance") or {})
    for key, value in changes.items():
        if value is _DROP or (key == "visibility" and value is None):
            provenance.pop(key, None)
        else:
            provenance[key] = value
    return {**row, "provenance": provenance}


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def test_parse_hosted_refs() -> None:
    ref = parse_hosted_ref("sessionfs:acme/review-loop@1.2.0")
    assert (ref.namespace, ref.name, ref.version, ref.package_id) == ("acme", "review-loop", "1.2.0", None)
    by_id = parse_hosted_ref("sessionfs:spk_" + "a" * 24)
    assert by_id.package_id == "spk_" + "a" * 24 and by_id.version is None
    for bad in ("sessionfs:acme", "sessionfs:Acme/x", "sessionfs:acme/x@../y", "sessionfs:../x/y"):
        with pytest.raises(SkillError):
            parse_hosted_ref(bad)


# ---------------------------------------------------------------------------
# add / lockfile / sync / update / rollback
# ---------------------------------------------------------------------------


def test_add_hosted_verifies_hash_and_records_hosted_source(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"])
    # Hash recomputed locally from the downloaded bytes == server's publish hash.
    assert package.content_hash == item["content_hash"]
    assert (project / ".codex" / "skills" / "review-loop" / "SKILL.md").is_file()
    lock = json.loads((project / ".sessionfs" / "skills.lock.json").read_text())
    row = lock["packages"][package.package_id]
    assert row["content_hash"] == item["content_hash"]
    assert row["source"]["source_type"] == "hosted"
    # Bound to the server it came from.
    assert row["source"]["locator"] == f"sessionfs:{item['package_id']}#https://sfs.test"
    assert row["source"]["commit"] == item["version_id"]
    assert row["source"]["listing_name"] == "acme/review-loop"


def test_add_hosted_pinned_version_and_ambiguity(tmp_path: Path, market: FakeMarketplace):
    old = market.publish("review-loop", "1.0.0", body="old")
    market.publish("review-loop", "2.0.0", body="new", package_id=old["package_id"])
    manager, _, _ = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop@1.0.0", targets=["codex"])
    assert package.content_hash == old["content_hash"]
    # Same namespace/name owned by two packages is refused, never guessed.
    market.publish("review-loop", "1.0.0", body="other owner")
    with pytest.raises(SkillError, match="matches 2 hosted packages"):
        manager.import_source("sessionfs:acme/review-loop")


def test_add_hosted_rejects_header_and_content_mismatch(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    other = market.publish("other-skill", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    market.header_override[item["version_id"]] = other["content_hash"]
    with pytest.raises(SkillError, match="hash header"):
        manager.add("sessionfs:acme/review-loop", targets=["codex"])
    # Header agrees with the catalog but the bytes are different content.
    market.header_override.clear()
    market.tamper[item["version_id"]] = market.versions[other["version_id"]]["archive"]
    with pytest.raises(SkillError, match="content hash mismatch"):
        manager.add("sessionfs:acme/review-loop", targets=["codex"])
    assert not (project / ".codex").exists()
    assert not (project / ".sessionfs" / "skills.lock.json").exists()


def test_extract_archive_rejects_unsafe_members(tmp_path: Path):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(SkillError, match="unsafe path"):
        hosted.extract_archive(output.getvalue(), tmp_path)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        link = tarfile.TarInfo("plugin.json")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    with pytest.raises(SkillError, match="regular files"):
        hosted.extract_archive(output.getvalue(), tmp_path)
    with pytest.raises(SkillError, match="tar.gz"):
        hosted.extract_archive(b"not a gzip", tmp_path)


def test_sync_locked_redownloads_exact_locked_version(tmp_path: Path, market: FakeMarketplace):
    old = market.publish("review-loop", "1.0.0", body="old")
    manager, project, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"])
    # Server moves on; local cache and outputs disappear (fresh checkout).
    market.publish("review-loop", "2.0.0", body="new", package_id=old["package_id"])
    shutil.rmtree(home / ".sessionfs" / "skills" / "packages")
    shutil.rmtree(project / ".codex")
    (project / ".sessionfs" / "skills.state.json").unlink()
    manager.sync(scope="project")
    assert market.downloads[-1] == old["version_id"]
    state = json.loads((project / ".sessionfs" / "skills.state.json").read_text())
    assert state["packages"][package.package_id]["content_hash"] == old["content_hash"]
    assert "old" in (project / ".codex" / "skills" / "review-loop" / "SKILL.md").read_text()


def test_update_and_rollback_hosted(tmp_path: Path, market: FakeMarketplace):
    old = market.publish("review-loop", "1.0.0", body="old")
    manager, project, _ = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"])
    new = market.publish("review-loop", "2.0.0", body="new", package_id=old["package_id"])
    prepared = manager.prepare_update(package.package_id, scope="project")
    assert prepared.package.content_hash == new["content_hash"]
    assert prepared.package.package_id == package.package_id
    manager.apply_update(prepared)
    lock = json.loads((project / ".sessionfs" / "skills.lock.json").read_text())
    assert lock["packages"][package.package_id]["source"]["commit"] == new["version_id"]
    manager.rollback(package.package_id, old["content_hash"], scope="project")
    lock = json.loads((project / ".sessionfs" / "skills.lock.json").read_text())
    assert lock["packages"][package.package_id]["content_hash"] == old["content_hash"]
    assert lock["packages"][package.package_id]["source"]["commit"] == old["version_id"]


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: HostedClient | None) -> Path:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(hosted, "client_from_auth", lambda **_: client)
    return project


def test_search_lists_hosted_results_labelled(tmp_path: Path, monkeypatch, market: FakeMarketplace):
    market.publish("review-loop", "1.0.0")
    _cli_env(tmp_path, monkeypatch, market.client())
    result = runner.invoke(skill_app, ["search", "review", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["results"] == [
        {
            "source": "hosted",
            "marketplace": "sessionfs",
            "name": "review-loop",
            "description": "review-loop fixture",
            "install": "sessionfs:acme/review-loop@1.0.0",
        }
    ]
    table = runner.invoke(skill_app, ["search", "review"], env={"COLUMNS": "200"})
    assert "hosted" in table.output and "sessionfs:acme/review-loop" in table.output


def test_search_signed_out_or_offline_skips_hosted_with_note(tmp_path: Path, monkeypatch):
    _cli_env(tmp_path, monkeypatch, None)
    result = runner.invoke(skill_app, ["search", "review"])
    assert result.exit_code == 0, result.output
    assert "not signed in" in result.output

    def _offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    offline = HostedClient("https://sfs.test", "k", transport=httpx.MockTransport(_offline))
    monkeypatch.setattr(hosted, "client_from_auth", lambda **_: offline)
    result = runner.invoke(skill_app, ["search", "review"])
    assert result.exit_code == 0, result.output
    assert "Hosted SessionFS Marketplace skipped" in result.output


def test_cli_add_hosted_end_to_end(tmp_path: Path, monkeypatch, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    project = _cli_env(tmp_path, monkeypatch, market.client())
    result = runner.invoke(
        skill_app, ["add", "sessionfs:acme/review-loop", "--for", "codex", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert item["content_hash"] in result.output
    assert (project / ".codex" / "skills" / "review-loop" / "SKILL.md").is_file()
    signed_out = _cli_env(tmp_path, monkeypatch, None)
    assert signed_out == project
    result = runner.invoke(skill_app, ["add", "sessionfs:acme/review-loop", "--yes"], env={"COLUMNS": "200"})
    assert result.exit_code != 0
    assert "signed-in account" in result.output


# ---------------------------------------------------------------------------
# revocations
# ---------------------------------------------------------------------------


def _namespace(manager: SkillManager, user: str = "usr_me") -> dict[str, Any]:
    return HostedState(manager.store_root).read()["namespaces"][f"https://sfs.test|{user}"]


def test_revocations_block_yank_restore_and_cursor(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    manager, project, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex", "claude-code"])
    client = market.client()

    yanked_at = market.revoke(item, "yanked", "superseded")
    summary = hosted.sync_revocations(client, manager.store_root)
    assert summary["supported"] is True and summary["full_replay"] is True
    warnings = hosted.revocation_warnings(manager)
    assert [(w["state"], w["reason"]) for w in warnings] == [("yanked", "superseded")]
    # Yanked: kept (compile of the installed version still works) ...
    manager.compile(package.package_id, scope="project")
    # ... but new installs of that version are refused.
    manager.remove(package.package_id, scope="project")
    with pytest.raises(SkillError, match="yanked"):
        manager.add("sessionfs:acme/review-loop@1.0.0", targets=["codex"])
    # Cursor persisted per identity and sent back incrementally.
    assert _namespace(manager)["revocations_since"] == yanked_at

    market.revoke(item, "restored")
    summary = hosted.sync_revocations(client, manager.store_root)
    assert summary["full_replay"] is False
    assert yanked_at in market.revocation_calls
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex", "claude-code"])

    # Blocked: every output, including one the user edited, leaves the tool
    # directories for a recoverable quarantine.
    edited = project / ".codex" / "skills" / "review-loop" / "SKILL.md"
    edited.write_text("my local notes\n", encoding="utf-8")
    market.revoke(item, "blocked", "malware")
    hosted.sync_revocations(client, manager.store_root)
    removed = hosted.enforce_blocked(manager)
    assert [(r["name"], r["reason"]) for r in removed] == [("review-loop", "malware")]
    assert removed[0]["dirty"] == [str(edited)]
    quarantine = Path(removed[0]["quarantine_dir"])
    assert quarantine.is_relative_to(home / ".sessionfs" / "skills" / "quarantine")
    assert (quarantine / ".codex" / "skills" / "review-loop" / "SKILL.md").read_text() == (
        "my local notes\n"
    )
    assert (quarantine / ".claude" / "skills" / "review-loop" / "SKILL.md").is_file()
    assert manager.list_installed() == []
    assert not (project / ".codex" / "skills" / "review-loop").exists()
    assert not (project / ".claude" / "skills" / "review-loop").exists()
    assert hosted.quarantine_leftovers(manager) == []
    # Restoring a quarantined file by hand is reported as a leftover.
    edited.parent.mkdir(parents=True)
    edited.write_text("restored\n", encoding="utf-8")
    assert [item["path"] for item in hosted.quarantine_leftovers(manager)] == [str(edited)]
    # Blocked content can never be reinstalled, even from the content cache.
    with pytest.raises(SkillError, match="BLOCKED"):
        manager.add("sessionfs:acme/review-loop", targets=["codex"])


def test_blocked_removal_keeps_state_while_an_output_is_still_active(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex", "claude-code"])
    market.revoke(item, "blocked", "malware")
    hosted.sync_revocations(market.client(), manager.store_root)
    from sessionfs.skills import state as state_module

    stuck = project / ".claude" / "skills" / "review-loop" / "SKILL.md"
    real_rename = state_module._rename_entry

    def _rename(src, dst):
        if Path(src) == stuck:
            raise PermissionError("locked")
        return real_rename(src, dst)

    monkeypatch.setattr(state_module, "_rename_entry", _rename)
    removed = hosted.enforce_blocked(manager)
    assert removed[0]["failed"] == [str(stuck)] and "still active" in removed[0]["error"]
    assert stuck.is_file()
    assert not (project / ".codex" / "skills" / "review-loop").exists()
    # The row stays (only the still-active output), so it is reported and retried.
    rows = manager.list_installed("project")
    assert [output["path"] for output in rows[0]["outputs"]] == [str(stuck)]
    assert [w["state"] for w in hosted.revocation_warnings(manager)] == ["blocked"]
    monkeypatch.setattr(state_module, "_rename_entry", real_rename)
    removed = hosted.enforce_blocked(manager)
    assert not removed[0].get("failed") and manager.list_installed() == []
    assert not stuck.exists()


def test_revocations_are_scoped_per_identity_and_replayed(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    other = market.publish("lint-helper", "1.0.0")
    org_only = market.publish("org-only", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    alice, bob = market.client("sk_test"), market.client("sk_other")
    # Private to Alice's org; Bob's identity sees a GLOBAL block and an org one.
    market.revoke(item, "yanked", "alice-org private reason", visible_to={"sk_test"},
                  kind="version", block_scope=None)
    market.revoke(other, "blocked", "bob-visible global reason", visible_to={"sk_other"},
                  kind="version", block_scope="global")
    market.revoke(org_only, "blocked", "bob-org private reason", visible_to={"sk_other"},
                  kind="version", block_scope="org", scope_org_id="org_x")
    hosted.sync_revocations(alice, manager.store_root)
    alice_cursor = _namespace(manager)["revocations_since"]
    # Bob on the same API URL starts from scratch, not from Alice's cursor.
    summary = hosted.sync_revocations(bob, manager.store_root)
    assert summary["full_replay"] is True
    assert market.revocation_calls[-1] is None
    assert _namespace(manager, "usr_other")["revoked"].keys() == {
        f"{other['version_id']}#platform", f"{org_only['version_id']}#owner:org_x"}
    assert _namespace(manager)["revocations_since"] == alice_cursor

    state = HostedState(manager.store_root).read()
    bob_fp = hosted.identity_fingerprint("https://sfs.test", "sk_other")
    alice_fp = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    bob_view = hosted.effective_flags(state, bob_fp)
    # Bob never sees Alice's yank or her private reason ...
    assert all("alice" not in str(flag.get("reason")) for flag in bob_view)
    assert sorted(flag["state"] for flag in bob_view) == ["blocked", "blocked"]
    # ... and Bob's global VERSION block stays in Bob's visibility-scoped
    # namespace (only hash-wide content events are shared device-wide); Bob's
    # org-scoped block is not merged.
    alice_view = hosted.effective_flags(state, alice_fp)
    foreign = [flag for flag in alice_view if flag.get("foreign")]
    assert [flag for flag in foreign if not flag.get("kept")] == []
    # Bob's org-scoped VERSION block is not kept device-wide either (only
    # content blocks are): Alice's view holds nothing of Bob's.
    assert [flag for flag in foreign if flag.get("kept")] == []
    assert all("bob" not in str(flag.get("reason")) for flag in alice_view)
    hosted.assert_install_allowed(
        manager.store_root, content_hash=other["content_hash"], version_id=other["version_id"],
        fingerprint=alice_fp,
    )
    hosted.assert_install_allowed(
        manager.store_root, content_hash=org_only["content_hash"],
        version_id=org_only["version_id"], fingerprint=alice_fp, owner_org_id=None,
    )


def test_content_restore_clears_only_the_hash_wide_block(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    client = market.client()
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    market.revoke(item, "blocked", "version takedown", kind="version", block_scope="global")
    market.revoke(item, "blocked", "bad bytes", kind="content", block_scope="global")
    market.revoke(item, "restored", kind="content", block_scope="global")
    # Unknown future states are ignored, not misread.
    market.revoke(item, "some-future-state", "future", kind="version")
    hosted.sync_revocations(client, manager.store_root)
    namespace = _namespace(manager)
    assert namespace["content_blocks"] == {}
    assert namespace["revoked"][f"{item['version_id']}#platform"]["reason"] == "version takedown"
    with pytest.raises(SkillError, match="version takedown"):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=item["content_hash"],
            version_id=item["version_id"], fingerprint=fp,
        )
    # A content block (no ids) matches any source with the same bytes.
    market.revoke(item, "blocked", "bad bytes again", kind="content", block_scope="org")
    market.revoke(item, "restored", kind="version")
    hosted.sync_revocations(client, manager.store_root)
    with pytest.raises(SkillError, match="bad bytes again"):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=item["content_hash"], version_id=None, fingerprint=fp,
        )


def test_feed_pages_until_has_more_is_false_and_never_commits_partial(
    tmp_path: Path, market: FakeMarketplace
):
    items = [market.publish(f"pkg-{n}", "1.0.0") for n in range(5)]
    manager, _, _ = _manager(tmp_path, market)
    client = market.client()
    for item in items:
        market.revoke(item, "yanked", "old", kind="version")
    # 5 events, 2 per page: the budget runs out first -> nothing committed.
    summary = hosted.sync_revocations(client, manager.store_root, max_pages=2)
    assert summary["complete"] is False
    assert HostedState(manager.store_root).read()["namespaces"] == {}
    summary = hosted.sync_revocations(client, manager.store_root)
    assert summary["complete"] is True
    assert len(_namespace(manager)["revoked"]) == 5
    # Old server (no has_more): pages until an empty page.
    manager2, _, _ = _manager(tmp_path / "old", market)
    market.has_more_field = False
    summary = hosted.sync_revocations(client, manager2.store_root)
    assert summary["complete"] is True and len(_namespace(manager2)["revoked"]) == 5
    # A deadline in the past stops before any page and commits nothing.
    manager3, _, _ = _manager(tmp_path / "late", market)
    summary = hosted.sync_revocations(client, manager3.store_root, deadline=0.0)
    assert summary["complete"] is False
    assert HostedState(manager3.store_root).read()["namespaces"] == {}


def test_service_key_identity_and_pull(tmp_path: Path, monkeypatch, market: FakeMarketplace):
    _no_project(monkeypatch, "proj_1")
    item = market.publish("review-loop", "1.0.0")
    market.assign(item)
    market.service_keys.add("sk_service")
    market.revoke(market.publish("x", "1.0.0"), "yanked", "public", kind="version")
    manager, _, home = _manager(tmp_path, market)
    project_item = market.publish("project-skill", "1.0.0")
    personal_item = market.publish("personal-skill", "1.0.0")
    base = {"status": "approved", "target_device_id": None, "target_tools": ["codex"],
            "requested_capabilities": ["instructions"]}
    market.requests = [
        # Project-scoped: applies to whoever installs in the project, incl. a
        # service key, whatever target_user_id says.
        {**base, "id": "sir_" + "1" * 24, "version_id": project_item["version_id"],
         "content_hash": project_item["content_hash"], "target_scope": "project",
         "target_user_id": "usr_someone"},
        # User-scoped: belongs to a person, never to a service key.
        {**base, "id": "sir_" + "2" * 24, "version_id": personal_item["version_id"],
         "content_hash": personal_item["content_hash"], "target_scope": "user",
         "target_user_id": "usr_me"},
    ]
    manager, project, home = _manager(tmp_path, market)
    client = market.client("sk_service")
    report = hosted.pull(manager, client)
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_service")
    namespaces = HostedState(manager.store_root).read()["namespaces"]
    assert list(namespaces) == [f"https://sfs.test|key:{fp}"]
    statuses = {entry.version_id: entry.status for entry in report.items}
    assert statuses == {item["version_id"]: "installed", project_item["version_id"]: "installed"}
    assert report.complete_sources == {"assignment": True, "request": True}
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    assert (project / ".codex" / "skills" / "project-skill").is_dir()
    assert not (home / ".codex" / "skills" / "personal-skill").exists()


def test_service_key_on_a_server_without_service_project_lookup(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    market.service_keys.add("sk_service")
    manager, _, _ = _manager(tmp_path, market)

    def _refused(manager, client):
        raise hosted.ServiceKeyNotAllowed("Project lookup refused")

    monkeypatch.setattr(hosted, "_project_id", _refused)
    notes: list[str] = []
    report = hosted.pull(manager, market.client("sk_service"), notify=notes.append)
    assert report.complete_sources["request"] is False
    assert any("--project" in note for note in notes)
    # An explicit project id works around it.
    report = hosted.pull(manager, market.client("sk_service"), project_id="proj_1")
    assert report.complete_sources["request"] is True

def test_membership_change_is_picked_up_by_periodic_full_replay(
    tmp_path: Path, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    client = market.client()
    t0 = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    # An old revocation in an org Alice is not (yet) a member of.
    market.revoke(item, "blocked", "older private block", at="2026-10-15T00:00:00+00:00",
                  visible_to={"sk_other"})
    market.revoke(market.publish("x", "1.0.0"), "yanked", "public", at="2026-10-15T00:05:00+00:00")
    hosted.sync_revocations(client, manager.store_root, now=t0)
    assert f"{item['version_id']}#platform" not in _namespace(manager)["revoked"]
    # Alice joins the org: the old event is now visible but older than her cursor.
    market.events = [
        (at, event, None if audience == {"sk_other"} else audience)
        for at, event, audience in market.events
    ]
    hosted.sync_revocations(client, manager.store_root, now=t0 + timedelta(hours=1))
    assert f"{item['version_id']}#platform" not in _namespace(manager)["revoked"]  # incremental misses it
    summary = hosted.sync_revocations(client, manager.store_root, now=t0 + timedelta(hours=25))
    assert summary["full_replay"] is True
    assert _namespace(manager)["revoked"][f"{item['version_id']}#platform"]["state"] == "blocked"


def test_revocation_feed_404_is_skipped_quietly(tmp_path: Path, market: FakeMarketplace):
    manager, _, _ = _manager(tmp_path, market)
    summary = hosted.sync_revocations(market.client(), manager.store_root)
    assert summary["supported"] is False and summary["changed"] == []


def test_doctor_and_list_warn_on_yanked(tmp_path: Path, monkeypatch, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    _cli_env(tmp_path, monkeypatch, market.client())
    assert runner.invoke(
        skill_app, ["add", "sessionfs:acme/review-loop", "--for", "codex", "--yes"]
    ).exit_code == 0
    market.revoke(item, "yanked", "superseded by 1.0.1")
    store = Path.home() / ".sessionfs" / "skills"
    hosted.sync_revocations(market.client(), store)
    listed = runner.invoke(skill_app, ["list"])
    assert "YANKED" in listed.output and "superseded by 1.0.1" in listed.output
    doctor = runner.invoke(skill_app, ["doctor", "--scope", "project"])
    assert "YANKED" in doctor.output
    assert doctor.exit_code == 0, doctor.output


def test_quarantine_warns_refuses_new_installs_and_never_removes(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    """`quarantined` (an automated scan stopped new installs): warns in list,
    doctor and pull, refuses NEW installs of the version, keeps the existing
    install (only a human block removes), and a later restore clears it."""
    item = market.publish("review-loop", "1.0.0")
    _cli_env(tmp_path, monkeypatch, market.client())
    assert runner.invoke(
        skill_app, ["add", "sessionfs:acme/review-loop", "--for", "codex", "--yes"]
    ).exit_code == 0
    market.revoke(
        item, "quarantined", "Automated instruction scan: fetch_and_run.pipe_to_interpreter",
        kind="version", block_scope="global",
    )
    store = Path.home() / ".sessionfs" / "skills"
    hosted.sync_revocations(market.client(), store)
    listed = runner.invoke(skill_app, ["list"])
    assert "QUARANTINED" in listed.output and "fetch_and_run" in listed.output
    doctor = runner.invoke(skill_app, ["doctor", "--scope", "project"])
    assert "QUARANTINED" in doctor.output
    assert doctor.exit_code == 0, doctor.output  # a warning, not a failure

    manager = SkillManager()
    assert hosted.enforce_blocked(manager) == []  # never uninstalled
    assert [row["name"] for row in manager.list_installed()] == ["review-loop"]
    # The installed copy keeps compiling; a NEW install of the version is refused.
    hosted.assert_install_allowed(
        store, content_hash=item["content_hash"], version_id=item["version_id"],
        installed_hash=item["content_hash"],
        fingerprint=hosted.identity_fingerprint("https://sfs.test", "sk_test"),
    )
    with pytest.raises(SkillError, match="QUARANTINED"):
        hosted.assert_install_allowed(
            store, content_hash=item["content_hash"], version_id=item["version_id"],
            fingerprint=hosted.identity_fingerprint("https://sfs.test", "sk_test"),
        )

    market.revoke(item, "restored", "false positive", kind="version", block_scope="global")
    hosted.sync_revocations(market.client(), store)
    assert hosted.revocation_warnings(manager) == []
    hosted.assert_install_allowed(
        store, content_hash=item["content_hash"], version_id=item["version_id"],
        fingerprint=hosted.identity_fingerprint("https://sfs.test", "sk_test"),
    )


def test_quarantine_allows_only_an_exact_reapply(tmp_path: Path, market: FakeMarketplace):
    """While quarantined, the installed copy re-applies exactly as recorded;
    growing onto another tool (or more capabilities) is refused."""
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"])
    market.revoke(item, "quarantined", "scan", kind="version", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)
    manager.compile(package.package_id, scope="project")  # exact re-apply: kept
    with pytest.raises(SkillError, match="QUARANTINED.*more tools"):
        manager.compile(package.package_id, scope="project", targets=["claude-code"])
    assert [row["targets"] for row in manager.list_installed()] == [["codex"]]
    # Dropping a tool is not an expansion.
    market.revoke(item, "restored", "fp", kind="version", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)
    manager.compile(package.package_id, scope="project", targets=["claude-code"])


def test_quarantine_reapply_requires_the_recorded_plan_hashes(
    tmp_path: Path, market: FakeMarketplace, monkeypatch: pytest.MonkeyPatch
):
    """An exact re-apply means the same emission plan per kept tool: a
    changed plan (an adapter update, a newly resolved placeholder) is refused
    while quarantined; dropping a tool whose siblings match is allowed."""
    from sessionfs.skills import adapters

    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex", "claude-code"])
    market.revoke(item, "quarantined", "scan", kind="version", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)
    manager.compile(package.package_id, scope="project")  # identical plans: kept
    original = adapters._plan_hash
    with monkeypatch.context() as patched:
        patched.setattr(adapters, "_plan_hash", lambda value: "0" * 64)
        with pytest.raises(SkillError, match="QUARANTINED.*changed output"):
            manager.compile(package.package_id, scope="project")
    assert adapters._plan_hash is original
    manager.compile(package.package_id, scope="project", targets=["codex"])  # an exact subset: allowed


def test_quarantine_and_yank_are_separate_flags(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    client = market.client()
    market.revoke(item, "yanked", "superseded", kind="version")
    market.revoke(item, "quarantined", "scan", kind="version", block_scope="global")
    hosted.sync_revocations(client, manager.store_root)
    revoked = _namespace(manager)["revoked"]
    assert revoked[f"{item['version_id']}#platform"]["state"] == "yanked"
    assert revoked[f"{item['version_id']}#quarantine"]["state"] == "quarantined"
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    with pytest.raises(SkillError, match="QUARANTINED"):  # quarantine outranks a yank
        hosted.assert_install_allowed(
            manager.store_root, content_hash=item["content_hash"],
            version_id=item["version_id"], fingerprint=fp,
        )
    # Lifting the quarantine: the server restores, then re-emits the platform
    # state that remains (the yank), so the yank survives on the client.
    market.revoke(item, "restored", "fp", kind="version", block_scope="global")
    market.revoke(item, "yanked", "superseded", kind="version")
    hosted.sync_revocations(client, manager.store_root)
    revoked = _namespace(manager)["revoked"]
    assert list(revoked) == [f"{item['version_id']}#platform"]
    assert revoked[f"{item['version_id']}#platform"]["state"] == "yanked"
    # An owner-scoped "quarantined" event is malformed (quarantine is
    # platform-only) and ignored.
    market.revoke(item, "quarantined", "x", kind="version", block_scope="org", scope_org_id="org_1")
    hosted.sync_revocations(client, manager.store_root)
    assert f"{item['version_id']}#quarantine" not in _namespace(manager)["revoked"]


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


def test_pull_required_optional_idempotent_and_unassigned(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    required = market.publish("review-loop", "1.0.0")
    optional = market.publish("lint-helper", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    client = market.client()
    required_row = market.assign(required, mode="required")
    market.assign(optional, mode="optional")

    report = hosted.pull(manager, client, install_optional=lambda item: False)
    statuses = {item.version_id: item.status for item in report.items}
    assert statuses == {required["version_id"]: "installed", optional["version_id"]: "offered"}
    assert (home / ".codex" / "skills" / "review-loop" / "SKILL.md").is_file()
    assert not (home / ".codex" / "skills" / "lint-helper").exists()

    # Idempotent: nothing is downloaded again.
    downloads = len(market.downloads)
    report = hosted.pull(manager, client, install_optional=lambda item: False)
    assert {item.status for item in report.items} == {"up-to-date", "offered"}
    assert len(market.downloads) == downloads

    # Optional installs when accepted.
    report = hosted.pull(manager, client, install_optional=lambda item: True)
    assert (home / ".codex" / "skills" / "lint-helper" / "SKILL.md").is_file()

    # A user-chosen install is never reported or removed.
    manual = market.publish("my-own", "1.0.0")
    manager.add("sessionfs:acme/my-own", targets=["codex"], scope="user")
    assert manual

    # Assignment removed: reported as no longer assigned, never uninstalled
    # (and a user's own install is never reported).
    market.assignments.remove(required_row)
    for _pull in range(2):
        report = hosted.pull(manager, client)
        assert [c["name"] for c in report.unassigned] == ["review-loop"]
        assert (home / ".codex" / "skills" / "review-loop").exists()
    assert [c["name"] for c in hosted.unassigned_packages(manager)] == ["review-loop"]
    names = sorted(row["name"] for row in manager.list_installed("user"))
    assert names == ["lint-helper", "my-own", "review-loop"]
    # Assigned again: no longer reported.
    market.assignments.append(required_row)
    report = hosted.pull(manager, client)
    assert report.unassigned == [] and hosted.unassigned_packages(manager) == []


def test_pull_replaces_previously_pulled_version_but_not_user_installs(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    client = market.client()
    first = market.assign(v1)
    hosted.pull(manager, client)
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2)
    report = hosted.pull(manager, client)
    assert [item.status for item in report.items] == ["installed"]
    assert report.unassigned == []
    assert "two" in (home / ".codex" / "skills" / "review-loop" / "SKILL.md").read_text()

    # A user's own different version of an assigned package is left alone.
    other = market.publish("pinned", "1.0.0", body="mine")
    manager.add("sessionfs:acme/pinned", targets=["codex"], scope="user")
    newer = market.publish("pinned", "2.0.0", body="org", package_id=other["package_id"])
    market.assign(newer)
    report = hosted.pull(manager, client)
    pinned = next(item for item in report.items if item.version_id == newer["version_id"])
    assert pinned.status == "skipped" and "by the user" in pinned.detail


def test_pull_requests_respect_local_and_server_consent(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch, "proj_1")
    item = market.publish("scripted", "1.0.0", scripts=True)
    manager, project, _ = _manager(tmp_path, market)
    client = market.client()
    request = {
        "id": "sir_" + "1" * 24,
        "status": "approved",
        "version_id": item["version_id"],
        "content_hash": item["content_hash"],
        "target_user_id": "usr_me",
        "target_scope": "project",
        "target_device_id": None,
        "target_tools": ["codex"],
        "requested_capabilities": ["instructions"],
    }
    market.requests = [
        request,
        {**request, "id": "sir_" + "2" * 24, "target_user_id": "usr_someone_else"},
    ]
    report = hosted.pull(manager, client)
    assert len(report.items) == 1
    assert report.items[0].status == "skipped" and "--allow-scripts" in report.items[0].detail
    # Local flag alone is not enough: the approver never approved scripts.
    report = hosted.pull(manager, client, flags={"allow_scripts": True})
    assert "not approved on an install request" in report.items[0].detail
    request["requested_capabilities"] = ["instructions", "scripts"]
    report = hosted.pull(manager, client, flags={"allow_scripts": True})
    assert report.items[0].status == "installed"
    assert (project / ".codex" / "skills" / "scripted" / "scripts" / "check.py").is_file()


def test_pull_device_assignments_need_device_id(tmp_path: Path, monkeypatch, market: FakeMarketplace):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, target_type="device", target_id="laptop-1")
    assert hosted.pull(manager, market.client()).items == []
    report = hosted.pull(manager, market.client(), device_id="laptop-1")
    assert [entry.status for entry in report.items] == ["installed"]


def test_cli_pull(tmp_path: Path, monkeypatch, market: FakeMarketplace):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    optional = market.publish("lint-helper", "1.0.0")
    market.assign(item)
    market.assign(optional, mode="optional")
    _cli_env(tmp_path, monkeypatch, market.client())
    result = runner.invoke(skill_app, ["pull", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert sorted(entry["status"] for entry in body["items"]) == ["installed", "offered"]
    result = runner.invoke(skill_app, ["pull", "--yes"])
    assert result.exit_code == 0, result.output
    assert (Path.home() / ".codex" / "skills" / "lint-helper").is_dir()
    _cli_env(tmp_path, monkeypatch, None)
    result = runner.invoke(skill_app, ["pull"], env={"COLUMNS": "200"})
    assert result.exit_code != 0 and "signed-in account" in result.output


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


def test_daemon_reconcile_required_only_and_blocked(tmp_path: Path, market: FakeMarketplace):
    required = market.publish("review-loop", "1.0.0")
    optional = market.publish("lint-helper", "1.0.0")
    scripted = market.publish("scripted", "1.0.0", scripts=True)
    market.assign(required)
    market.assign(optional, mode="optional")
    market.assign(scripted)
    manager, _, home = _manager(tmp_path, market)
    now = [1000.0]
    reconciler = SkillReconciler(
        clock=lambda: now[0],
        manager_factory=lambda: manager,
        client_factory=lambda url, key: market.client(),
    )
    assert reconciler.maybe_run(enabled=False, api_url="u", api_key="k", background=False) is None
    report = reconciler.maybe_run(enabled=True, api_url="u", api_key="k", background=False)
    statuses = {item.version_id: item.status for item in report.items}
    assert statuses[required["version_id"]] == "installed"
    assert statuses[optional["version_id"]] == "offered"
    assert statuses[scripted["version_id"]] == "skipped"  # never grants consent
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    # Not due again until the interval passes.
    assert reconciler.maybe_run(enabled=True, api_url="u", api_key="k", background=False) is None

    market.revoke(required, "blocked", "malware")
    now[0] += 16 * 60
    report = reconciler.maybe_run(enabled=True, api_url="u", api_key="k", background=False)
    assert [item["reason"] for item in report.blocked_removed] == ["malware"]
    assert not (home / ".codex" / "skills" / "review-loop").exists()
    # The blocked assignment is not reinstalled.
    assert {item.version_id: item.status for item in report.items}[required["version_id"]] == "failed"


def test_daemon_reconcile_backs_off_on_failure(tmp_path: Path):
    calls = []

    def _broken(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("down", request=request)

    now = [0.0]
    manager = SkillManager(project_root=tmp_path, home_dir=tmp_path, store_root=tmp_path / ".sessionfs" / "skills")
    reconciler = SkillReconciler(
        clock=lambda: now[0],
        manager_factory=lambda: manager,
        client_factory=lambda url, key: HostedClient(url, key, transport=httpx.MockTransport(_broken)),
    )
    assert reconciler.maybe_run(enabled=True, api_url="https://x", api_key="k", background=False) is None
    assert reconciler.failures == 1
    first = len(calls)
    now[0] += FAILURE_BACKOFF_S[0] - 1
    reconciler.maybe_run(enabled=True, api_url="https://x", api_key="k", background=False)
    assert len(calls) == first  # still backing off
    now[0] += 2
    reconciler.maybe_run(enabled=True, api_url="https://x", api_key="k", background=False)
    assert reconciler.failures == 2


# ---------------------------------------------------------------------------
# removal TOCTOU + multi-origin aggregation
# ---------------------------------------------------------------------------


def test_remove_hashes_outputs_only_after_moving_them(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    """An edit landing between a check and the unlink can never be lost: each
    output is renamed out of the tool directory first, and only the renamed
    bytes are hashed."""
    from sessionfs.skills import state as state_module

    market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"])
    output = project / ".codex" / "skills" / "review-loop" / "SKILL.md"
    hashed: list[Path] = []
    real_hash = state_module._sha256_file

    def _spy(path: Path):
        hashed.append(path)
        # Simulate the user saving an edit right after any check: once the
        # file has been moved, the tool-directory path is already empty.
        assert not output.exists(), "hashed while the output was still in the tool directory"
        return real_hash(path)

    monkeypatch.setattr(state_module, "_sha256_file", _spy)
    result = manager.remove("review-loop", scope="project")
    assert result["removed"] == [str(output)]
    assert hashed and all(".skills-txn-" in str(path) for path in hashed)


def test_remove_puts_a_modified_output_back_in_place(tmp_path: Path, market: FakeMarketplace):
    market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"])
    output = project / ".codex" / "skills" / "review-loop" / "SKILL.md"
    output.write_text("edited\n", encoding="utf-8")
    result = manager.remove("review-loop", scope="project")
    assert result["dirty"] == [str(output)] and result["removed"] == []
    assert output.read_text() == "edited\n"
    assert not list((project / ".sessionfs").glob(".skills-txn-*"))


def test_pull_aggregates_assignments_for_the_same_version(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    client = market.client()
    org_a = market.assign(item, tools=["codex"])
    org_b = market.assign(item, tools=["claude-code"])
    org_b["org_id"] = "org_2"
    report = hosted.pull(manager, client)
    assert len(report.items) == 1
    assert sorted(report.items[0].targets) == ["claude-code", "codex"]
    assert {origin_id for _, origin_id in report.items[0].origins} == {org_a["id"], org_b["id"]}
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    assert (home / ".claude" / "skills" / "review-loop").is_dir()

    # One org drops its assignment: the other still references the package.
    market.assignments.remove(org_a)
    report = hosted.pull(manager, client)
    assert report.unassigned == []
    assert [row["name"] for row in manager.list_installed("user")] == ["review-loop"]

    # Both gone: now it is reported as no longer assigned (never removed).
    market.assignments.remove(org_b)
    hosted.pull(manager, client)
    report = hosted.pull(manager, client)
    assert [c["name"] for c in report.unassigned] == ["review-loop"]


def test_pull_records_live_origins_when_already_installed(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    client = market.client()
    first = market.assign(item)
    hosted.pull(manager, client)
    # A second org assigns the SAME version later: content is already there.
    second = market.assign(item)
    report = hosted.pull(manager, client)
    assert report.items[0].status == "up-to-date"
    market.assignments.remove(first)
    report = hosted.pull(manager, client)
    assert report.unassigned == []
    market.assignments.remove(second)
    hosted.pull(manager, client)
    report = hosted.pull(manager, client)
    assert [c["name"] for c in report.unassigned] == ["review-loop"]


def _targets(manager: SkillManager) -> list[str]:
    return sorted(manager.list_installed("user")[0]["targets"])


def test_pull_reconciles_tools_even_when_the_hash_matches(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    client = market.client()
    org_a = market.assign(item, tools=["codex"])
    hosted.pull(manager, client)
    assert _targets(manager) == ["codex"]

    # A second assignment for the same version adds a tool.
    org_b = market.assign(item, tools=["claude-code"])
    report = hosted.pull(manager, client)
    assert report.items[0].status == "updated" and "+claude-code" in report.items[0].detail
    assert _targets(manager) == ["claude-code", "codex"]
    assert (home / ".claude" / "skills" / "review-loop" / "SKILL.md").is_file()

    # The user adds a tool by hand.
    manager.compile("review-loop", scope="user", targets=["gemini"])
    # Org A's assignment is removed: codex (wanted only by it) is NOT
    # removed - it is reported as no longer assigned (the user's gemini is
    # not reported at all).
    codex_skill = home / ".codex" / "skills" / "review-loop" / "SKILL.md"
    market.assignments.remove(org_a)
    report = hosted.pull(manager, client)
    assert report.items[0].status == "up-to-date"
    assert _targets(manager) == ["claude-code", "codex", "gemini"]
    assert codex_skill.is_file()
    package_id = manager.list_installed("user")[0]["package_id"]
    assert report.unassigned_tools == [
        {"scope": "user", "package_id": package_id, "name": "review-loop", "tools": ["codex"]}
    ]
    # Stable: a further pull changes nothing and keeps reporting it.
    report = hosted.pull(manager, client)
    assert report.items[0].status == "up-to-date"
    assert [entry["tools"] for entry in report.unassigned_tools] == [["codex"]]
    assert org_b


def test_remove_cross_filesystem_fallback_never_hashes_the_live_path(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    import errno

    from sessionfs.skills import state as state_module

    market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex", "claude-code"])
    clean = project / ".claude" / "skills" / "review-loop" / "SKILL.md"
    edited = project / ".codex" / "skills" / "review-loop" / "SKILL.md"
    edited.write_text("edited\n", encoding="utf-8")
    real_rename = state_module._rename_entry
    exdev_raised: list[Path] = []

    def _no_cross_device(src, dst):
        # Renames straight into the transaction dir fail like EXDEV; renames
        # within the output's own directory work.
        if ".skills-txn-" in str(dst) and ".skills-txn-" not in str(src):
            exdev_raised.append(Path(src))
            raise OSError(errno.EXDEV, "cross-device link")
        return real_rename(src, dst)

    hashed: list[Path] = []
    real_hash = state_module._sha256_file

    def _spy(path: Path):
        hashed.append(Path(path))
        return real_hash(path)

    monkeypatch.setattr(state_module, "_rename_entry", _no_cross_device)
    monkeypatch.setattr(state_module, "_sha256_file", _spy)
    result = manager.remove("review-loop", scope="project")
    assert sorted(exdev_raised) == sorted([clean, edited])  # the fallback really ran
    assert result["removed"] == [str(clean)] and result["dirty"] == [str(edited)]
    assert hashed and all(path not in {clean, edited} for path in hashed)
    assert not clean.exists()
    assert edited.read_text() == "edited\n"
    assert not list(edited.parent.glob(".*sfs-removing*"))
    assert not list((project / ".sessionfs").glob(".skills-txn-*"))


def test_recovery_restores_an_interrupted_staged_removal(tmp_path: Path, market: FakeMarketplace):
    from sessionfs.skills.state import atomic_json

    market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"])
    store = manager.state_store("project")
    output = project / ".codex" / "skills" / "review-loop" / "SKILL.md"
    content = output.read_bytes()
    tx_dir = store.root / ".skills-txn-deadbeefcafe"
    (tx_dir / "moved").mkdir(parents=True)
    staged = output.with_name(".SKILL.md.sfs-removing-deadbeefcafe")
    os.replace(output, staged)  # crash right after staging
    atomic_json(store.journal_path, {
        "version": 1, "phase": "removing", "tx_dir": str(tx_dir),
        "actions": [{"destination": str(output), "moved": str(tx_dir / "moved" / "0"),
                     "staged": str(staged)}],
        "state_backup_existed": True, "lock_backup_existed": True,
    })
    store.recover()
    assert output.read_bytes() == content and not staged.exists()


def test_daemon_reconcile_runs_single_flight_in_the_background(
    tmp_path: Path, market: FakeMarketplace
):
    import threading

    market.assign(market.publish("review-loop", "1.0.0"))
    manager, _, home = _manager(tmp_path, market)
    gate = threading.Event()
    started = threading.Event()

    def _slow_client(url: str, key: str) -> HostedClient:
        started.set()
        gate.wait(5)
        return market.client()

    reconciler = SkillReconciler(manager_factory=lambda: manager, client_factory=_slow_client)
    # Returns immediately; the work happens in a worker thread.
    assert reconciler.maybe_run(enabled=True, api_url="u", api_key="k") is None
    assert started.wait(5) and reconciler.running
    # A second tick while it runs never starts another one.
    assert reconciler.maybe_run(enabled=True, api_url="u", api_key="k") is None
    assert threading.active_count() >= 2
    gate.set()
    reconciler.wait(10)
    assert not reconciler.running
    assert reconciler.last_report.items[0].status == "installed"
    assert (home / ".codex" / "skills" / "review-loop").is_dir()


def test_daemon_deadline_defers_remaining_work(tmp_path: Path, market: FakeMarketplace):
    market.assign(market.publish("review-loop", "1.0.0"))
    manager, _, _ = _manager(tmp_path, market)
    report = hosted.daemon_reconcile(manager, market.client(), deadline=0.0)
    assert report.items == [] and any("time limit" in note for note in report.notes)


def test_org_scoped_content_block_hits_only_that_orgs_packages(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    # Identical bytes published by two orgs.
    org_a = market.publish("shared", "1.0.0", owner_org_id="org_a")
    org_b = market.publish("shared", "1.0.0", owner_org_id="org_b", namespace="beta")
    assert org_a["content_hash"] == org_b["content_hash"]
    manager, project, home = _manager(tmp_path, market)
    manager.add(f"sessionfs:{org_a['package_id']}", targets=["codex"], scope="project")
    manager.add(f"sessionfs:{org_b['package_id']}", targets=["codex"], scope="user")
    # Provenance is row-local, written with each install (no shared map).
    assert manager.list_installed("project")[0]["provenance"]["owner_org_id"] == "org_a"
    assert manager.list_installed("user")[0]["provenance"]["owner_org_id"] == "org_b"
    assert HostedState(manager.store_root).read()["package_owners"] == {}
    market.revoke(org_a, "blocked", "org a takedown", kind="content",
                  block_scope="org", scope_org_id="org_a")
    report = hosted.pull(manager, market.client())
    assert [(item["scope"], item["reason"]) for item in report.blocked_removed] == [
        ("project", "org a takedown")]
    assert not (project / ".codex" / "skills" / "shared").exists()
    assert (home / ".codex" / "skills" / "shared").is_dir()  # org_b's copy is untouched
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    hosted.assert_install_allowed(
        manager.store_root, content_hash=org_a["content_hash"], version_id=None,
        fingerprint=fp, owner_org_id=None,  # personal package with the same bytes
    )
    with pytest.raises(SkillError, match="BLOCKED"):
        hosted.assert_install_allowed(  # owner unknown (older server): as before
            manager.store_root, content_hash=org_a["content_hash"], version_id=None,
            fingerprint=fp,
        )


def test_package_owners_are_learned_for_existing_installs(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")  # older server: no owner field
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    assert HostedState(manager.store_root).read()["package_owners"] == {}
    market.versions[item["version_id"]]["item"]["owner_org_id"] = "org_z"  # server upgraded
    hosted.pull(manager, market.client())
    assert HostedState(manager.store_root).read()["package_owners"] == {
        "https://sfs.test": {f"{item['package_id']}@{item['version_id']}": "org_z"}}


def test_newer_global_restore_lifts_a_block_held_by_a_stale_identity(
    tmp_path: Path, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    alice, bob = market.client("sk_test"), market.client("sk_other")
    market.revoke(item, "blocked", "global takedown", kind="content", block_scope="global",
                  at="2026-10-15T01:00:00+00:00")
    hosted.sync_revocations(bob, manager.store_root)  # Bob's namespace then goes stale
    market.revoke(item, "restored", kind="content", block_scope="global",
                  at="2026-10-15T02:00:00+00:00", visible_to={"sk_test"})
    hosted.sync_revocations(alice, manager.store_root)
    state = HostedState(manager.store_root).read()
    alice_fp = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    bob_fp = hosted.identity_fingerprint("https://sfs.test", "sk_other")
    for fp in (alice_fp, bob_fp):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=item["content_hash"], version_id=None, fingerprint=fp,
        )
    assert hosted.effective_flags(state, bob_fp) == []
    # An OLDER block replayed later never resurrects it.
    hosted.sync_revocations(bob, manager.store_root, force_full=True)
    hosted.assert_install_allowed(
        manager.store_root, content_hash=item["content_hash"], version_id=None, fingerprint=alice_fp,
    )


def test_unrestricted_assignments_expand_to_the_default_tools(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    client = market.client()
    defaults = ["codex", "claude-code", "gemini"]
    row = market.assign(item, tools=["codex"])
    hosted.pull(manager, client, targets=defaults)
    assert _targets(manager) == ["codex"]
    row["target_tools"] = []  # now unrestricted: every default tool
    report = hosted.pull(manager, client, targets=defaults)
    assert report.items[0].status == "updated"
    assert _targets(manager) == sorted(defaults)
    row["target_tools"] = ["codex"]  # restricted again: reported, never removed
    report = hosted.pull(manager, client, targets=defaults)
    assert report.items[0].status == "up-to-date"
    assert _targets(manager) == sorted(defaults)
    assert report.unassigned_tools[0]["tools"] == ["claude-code", "gemini"]


# ---------------------------------------------------------------------------
# round 4: server-bound provenance, error envelopes, per-server state,
# incomplete listings, enforcement races
# ---------------------------------------------------------------------------


def test_canonical_origin_and_bound_locator():
    assert hosted.canonical_origin("HTTPS://Api.Example.com:443/") == "https://api.example.com"
    assert hosted.canonical_origin("http://localhost:8000/sfs/") == "http://localhost:8000/sfs"
    source = {"source_type": "hosted", "locator": "sessionfs:spk_" + "a" * 24 + "#https://x.test"}
    assert hosted.hosted_package_id(source) == "spk_" + "a" * 24
    assert hosted.hosted_origin(source) == "https://x.test"
    legacy = {"source_type": "hosted", "locator": "sessionfs:spk_" + "a" * 24}
    assert hosted.hosted_package_id(legacy) == "spk_" + "a" * 24
    assert hosted.hosted_origin(legacy) is None


def test_update_and_locked_sync_only_use_the_server_the_package_came_from(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    good = market.publish("review-loop", "1.0.0", body="good")
    manager, project, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"])
    # A different (malicious) server reuses the SAME package id with its own,
    # self-consistent bytes and hashes.
    evil = FakeMarketplace()
    evil.publish("review-loop", "6.6.6", body="evil", package_id=good["package_id"])
    manager._hosted_client = evil.client(url="https://evil.test")
    monkeypatch.setattr(hosted, "clients_from_profiles", lambda **_: [])
    with pytest.raises(SkillError, match="installed from https://sfs.test"):
        manager.prepare_update(package.package_id, scope="project")
    shutil.rmtree(home / ".sessionfs" / "skills" / "packages")
    shutil.rmtree(project / ".codex")
    (project / ".sessionfs" / "skills.state.json").unlink()
    with pytest.raises(SkillError, match="installed from https://sfs.test"):
        manager.sync(scope="project")
    assert not evil.downloads
    # With a profile for the original server, its own client is used.
    market.publish("review-loop", "1.1.0", body="good update", package_id=good["package_id"])
    monkeypatch.setattr(hosted, "clients_from_profiles", lambda **_: [market.client()])
    manager.sync(scope="project")
    prepared = manager.prepare_update(package.package_id, scope="project")
    assert "good update" in (prepared.package.package_root / "skills" / "review-loop" / "SKILL.md").read_text()
    assert not evil.downloads


def test_legacy_unbound_rows_need_an_explicit_rebind(tmp_path: Path, market: FakeMarketplace):
    from sessionfs.skills.state import atomic_json

    item = market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"])
    store = manager.state_store("project")
    legacy = f"sessionfs:{item['package_id']}"
    for path in (store.state_path, store.lock_path):
        data = json.loads(path.read_text())
        data["packages"][package.package_id]["source"]["locator"] = legacy
        atomic_json(path, data)
    with pytest.raises(SkillError, match="--rebind-origin"):
        manager.prepare_update(package.package_id, scope="project")
    with pytest.raises(SkillError, match="--rebind-origin"):
        manager._restore_hosted_package(
            package.package_id,
            json.loads(store.lock_path.read_text())["packages"][package.package_id],
            {"source_type": "hosted", "locator": legacy, "commit": item["version_id"]},
            scope="project",
        )
    prepared = manager.prepare_update(package.package_id, scope="project", rebind_origin=True)
    assert prepared.package.source.locator == f"{legacy}#https://sfs.test"
    manager.apply_update(prepared)
    rows = manager.list_installed("project")
    assert [row["source"]["locator"] for row in rows] == [f"{legacy}#https://sfs.test"]


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"code": "service_key_not_allowed", "message": "user keys only", "details": {}}},
        {"error": {"code": "403", "message": "m", "details": {"error": "service_key_not_allowed"}}},
        {"detail": {"error": "service_key_not_allowed", "message": "m"}},
    ],
)
def test_parse_api_error_understands_every_service_key_envelope(body):
    response = httpx.Response(403, json=body)
    assert hosted.parse_api_error(response).code == "service_key_not_allowed"
    client = HostedClient(
        "https://sfs.test", "k", transport=httpx.MockTransport(lambda request: response)
    )
    assert client.identity() == {"kind": "service", "user_id": None}


def test_parse_api_error_plain_and_unparseable_bodies():
    assert hosted.parse_api_error(httpx.Response(500, text="boom")).code == "500"
    error = hosted.parse_api_error(httpx.Response(404, json={"detail": "Not Found"}))
    assert (error.code, error.message) == ("404", "Not Found")


def test_flags_and_owners_never_mix_between_servers(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0", owner_org_id="org_a")
    manager, _, _ = _manager(tmp_path, market)
    other = FakeMarketplace()
    other.revoke(item, "blocked", "other server", kind="content", block_scope="global")
    other_client = other.client(url="https://other.test")
    hosted.sync_revocations(other_client, manager.store_root)
    hosted.sync_revocations(market.client(), manager.store_root)
    state = HostedState(manager.store_root).read()
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    # The other server's global block never applies to this server's install.
    assert hosted.effective_flags(state, fp, "https://sfs.test") == []
    assert [flag["state"] for flag in hosted.effective_flags(state, fp, "https://other.test")] == [
        "blocked"]
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    assert manager.list_installed("user")[0]["provenance"]["owner_org_id"] == "org_a"
    # The same package id seen on another server has an unknown owner there.
    other_source = {"source_type": "hosted",
                    "locator": f"sessionfs:{item['package_id']}#https://other.test"}
    assert hosted.package_owner(HostedState(manager.store_root).read(), other_source) is (
        hosted.UNKNOWN_OWNER)


def test_unreadable_listing_is_never_an_empty_inventory(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item)
    hosted.pull(manager, market.client())
    assert (home / ".codex" / "skills" / "review-loop").is_dir()

    real = market.handler

    def _broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/me/skills/assignments":
            return httpx.Response(503, json={"error": {"code": "503", "message": "down"}})
        return real(request)

    market.handler = _broken  # type: ignore[method-assign]
    events: list[str] = []
    report = hosted.pull(
        manager,
        market.client(),
        notify=lambda note: events.append(f"note:{note}"),
    )
    assert report.complete_sources["assignment"] is False
    assert report.unassigned == []  # an unreadable listing never reports "unassigned"
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    assert any("Org assignments could not be read" in event for event in events)


def test_enforcement_never_quarantines_a_concurrently_updated_safe_version(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    bad = market.publish("review-loop", "1.0.0", body="bad")
    manager, project, _ = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"])
    market.revoke(bad, "blocked", "malware", kind="version")
    hosted.sync_revocations(market.client(), manager.store_root)
    stale = hosted.revocation_warnings(manager)
    assert [w["state"] for w in stale] == ["blocked"]
    # Meanwhile the user updates to a safe version.
    market.publish("review-loop", "1.0.1", body="safe", package_id=bad["package_id"])
    manager.apply_update(manager.prepare_update(package.package_id, scope="project"))
    real = hosted.revocation_warnings
    calls = {"n": 0}

    def _first_stale(*args, **kwargs):
        calls["n"] += 1
        return [dict(w) for w in stale] if calls["n"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr(hosted, "revocation_warnings", _first_stale)
    assert hosted.enforce_blocked(manager) == []
    assert calls["n"] == 2  # the pass restarted against the current row
    skill = project / ".codex" / "skills" / "review-loop" / "SKILL.md"
    assert "safe" in skill.read_text()


def test_enforcement_rechecks_the_flag_under_the_lock(tmp_path: Path, market: FakeMarketplace):
    bad = market.publish("review-loop", "1.0.0", body="bad")
    manager, project, _ = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"])
    result = manager.remove(
        package.package_id, scope="project", quarantine_dir=tmp_path / "q",
        expected_hash=package.content_hash, recheck=lambda row: False,
    )
    assert result == {"removed": [], "dirty": [], "skipped": True}
    assert (project / ".codex" / "skills" / "review-loop" / "SKILL.md").is_file()
    from sessionfs.skills.models import PackageChangedConcurrently

    with pytest.raises(PackageChangedConcurrently):
        manager.remove(package.package_id, scope="project", expected_hash="sha256:" + "0" * 64)
    assert bad


def test_project_resolution_uses_the_service_capable_route(market: FakeMarketplace):
    market.service_keys.add("sk_service")
    for key in ("sk_test", "sk_service"):
        assert market.client(key).project_id_for_remote("github.com/acme/repo") == "proj_1"
    # The constant "Project not found" 404 is final: no fallback to the old route.
    with pytest.raises(SkillError, match="no SessionFS project"):
        market.client().project_id_for_remote("github.com/acme/unknown")


def test_project_resolution_falls_back_only_when_the_route_is_missing(market: FakeMarketplace):
    market.has_resolve_route = False  # older server: route-not-found 404
    assert market.client().project_id_for_remote("github.com/acme/repo") == "proj_1"
    market.service_keys.add("sk_service")
    with pytest.raises(hosted.ServiceKeyNotAllowed):
        market.client("sk_service").project_id_for_remote("github.com/acme/repo")


def test_service_key_pull_resolves_the_project_from_the_remote(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    item = market.publish("project-skill", "1.0.0")
    market.service_keys.add("sk_service")
    market.requests = [{
        "id": "sir_" + "3" * 24, "status": "approved", "version_id": item["version_id"],
        "content_hash": item["content_hash"], "target_scope": "project",
        "target_user_id": "usr_someone", "target_device_id": None, "target_tools": ["codex"],
        "requested_capabilities": ["instructions"],
    }]
    manager, project, _ = _manager(tmp_path, market)
    # Real _project_id, with the git remote lookup stubbed.
    monkeypatch.setattr(
        hosted,
        "_project_id",
        lambda manager, client: client.project_id_for_remote("github.com/acme/repo"),
    )
    report = hosted.pull(manager, market.client("sk_service"))
    assert report.complete_sources["request"] is True
    assert (project / ".codex" / "skills" / "project-skill").is_dir()


# ---------------------------------------------------------------------------
# round 5: per-origin capability authorization, namespace-scoped ownership,
# identity-based dedupe, bound unassigned removal, light imports
# ---------------------------------------------------------------------------


def _request(item: dict[str, Any], rid: str, *, tools: list[str], caps: list[str],
             scope: str = "user") -> dict[str, Any]:
    return {
        "id": "sir_" + rid * 24, "status": "approved", "version_id": item["version_id"],
        "content_hash": item["content_hash"], "target_scope": scope, "target_user_id": "usr_me",
        "target_device_id": None, "target_tools": tools, "requested_capabilities": caps,
    }


def test_approvals_never_spread_across_origins_or_tools(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch, "proj_1")
    item = market.publish("scripted", "1.0.0", scripts=True)
    market.requests = [
        _request(item, "a", tools=["codex"], caps=["instructions", "scripts"]),
        _request(item, "b", tools=["claude-code"], caps=["instructions"]),
    ]
    # An assignment authorizes ONLY its own tool.
    market.assign(item, tools=["gemini"])
    manager, _, _ = _manager(tmp_path, market)
    report = hosted.pull(manager, market.client(), flags={"allow_scripts": True})
    [entry] = report.items
    assert entry.status == "installed"
    assert _targets(manager) == ["codex", "gemini"]
    assert "claude-code: not approved" in entry.detail
    # Without the local flag nothing with scripts is installed at all.
    manager2, _, _ = _manager(tmp_path / "noflag", market)
    report = hosted.pull(manager2, market.client())
    assert report.items[0].status == "skipped" and "--allow-scripts" in report.items[0].detail


def test_pull_ownership_is_scoped_to_server_and_account(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.pull(manager, market.client("sk_test"))
    pulled = HostedState(manager.store_root).read()["pulled"]
    assert [record["namespace"] for record in pulled.values()] == ["https://sfs.test|usr_me"]
    # Another account on the same server sees no assignments: A's install is
    # neither offered nor removed.
    market.assignments.remove(row)
    report = hosted.pull(
        manager, market.client("sk_other")
    )
    assert report.unassigned == []
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    # Another SERVER with the same account id behaves the same.
    report = hosted.pull(
        manager, market.client(url="https://other.test")
    )
    assert (home / ".codex" / "skills" / "review-loop").is_dir()


def test_legacy_unscoped_records_are_protected_until_adopted(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.pull(manager, market.client())
    with HostedState(manager.store_root).edit() as state:
        for record in state["pulled"].values():
            record.pop("namespace")  # what an older build wrote
    market.assignments.remove(row)
    hosted.pull(manager, market.client())
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    # Assigned again: this namespace adopts it (legacy record replaced).
    market.assign(item)
    hosted.pull(manager, market.client())
    pulled = HostedState(manager.store_root).read()["pulled"]
    assert [record.get("namespace") for record in pulled.values()] == ["https://sfs.test|usr_me"]


def test_identical_bytes_from_another_package_are_a_conflict_not_up_to_date(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    mine = market.publish("review-loop", "1.0.0")
    other = market.publish("review-loop", "1.0.0", namespace="other")
    assert mine["content_hash"] == other["content_hash"]
    manager, _, _ = _manager(tmp_path, market)
    manager.add(f"sessionfs:{mine['package_id']}", targets=["codex"], scope="user")
    market.assign(other)
    report = hosted.pull(manager, market.client())
    [entry] = report.items
    assert entry.status == "skipped" and entry.detail.startswith("conflict:")
    assert mine["package_id"] in entry.detail
    assert HostedState(manager.store_root).read()["pulled"] == {}


def test_hosted_cli_path_imports_no_server_dependencies(tmp_path: Path):
    """The base install has no server extra: importing the hosted client, the
    skill CLI and normalizing a git remote must never pull in PyJWT, FastAPI,
    SQLAlchemy or the server package."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import importlib.abc
        import sys

        BLOCKED = ("jwt", "fastapi", "sqlalchemy", "sessionfs.server")

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if any(name == b or name.startswith(b + ".") for b in BLOCKED):
                    raise ImportError(f"blocked import: {name}")
                return None

        sys.meta_path.insert(0, Block())
        import sessionfs.skills.hosted as hosted
        import sessionfs.skills.manager  # noqa: F401
        import sessionfs.cli.cmd_skill  # noqa: F401
        import sessionfs.daemon.skill_reconcile  # noqa: F401
        from sessionfs.git_remote import normalize_git_remote

        assert normalize_git_remote("git@github.com:Acme/Repo.git") == "acme/repo"
        leaked = [m for m in sys.modules if any(m == b or m.startswith(b + ".") for b in BLOCKED)]
        assert not leaked, leaked
        print("ok")
        """
    )
    src = Path(hosted.__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(src)},
        cwd=tmp_path,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "ok"
    # Lazy (function-level) imports are not exercised by importing a module:
    # check the CLI skill path's sources never reference the server package.
    import sessionfs.cli.cmd_skill as cmd_skill
    import sessionfs.daemon.skill_reconcile as skill_reconcile
    import sessionfs.skills.manager as manager_module

    for module in (hosted, manager_module, cmd_skill, skill_reconcile):
        text = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("sessionfs.server", "import jwt", "import fastapi", "sqlalchemy"):
            assert forbidden not in text, f"{module.__name__} references {forbidden}"


def test_server_keeps_the_same_remote_normalization():
    from sessionfs.git_remote import normalize_git_remote as shared
    from sessionfs.server.github_app import normalize_git_remote as server

    assert server is shared
    for value in ("git@github.com:SessionFS/sessionfs.git", "https://github.com/A/B",
                  "https://gitlab.com/g/sub/repo.git", "", "  Plain  "):
        assert server(value) == shared(value)


# ---------------------------------------------------------------------------
# round 6: reference vs installer records, locked removal re-check,
# canonical fingerprints, deadline/cancel
# ---------------------------------------------------------------------------


def _roles(manager: SkillManager) -> list[tuple[str | None, str | None]]:
    return sorted(
        (record.get("origin_id"), record.get("role"))
        for record in HostedState(manager.store_root).read()["pulled"].values()
    )


def test_optional_assignment_replacing_the_installer_keeps_the_package(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(item)  # required: installs it
    hosted.pull(manager, market.client())
    market.assignments.remove(first)
    second = market.assign(item, mode="optional")  # same version, already installed
    report = hosted.pull(manager, market.client())
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    # Being installed is not consent: the optional B is not recorded (and
    # inherits nothing); A's record stays, marked no longer assigned.
    assert _roles(manager) == [(first["id"], "installer")]
    assert report.items[0].status == "up-to-date"
    assert [c["name"] for c in report.unassigned] == ["review-loop"]
    # Once B is accepted, it is recorded with its acceptance.
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    record = next(
        r for r in HostedState(manager.store_root).read()["pulled"].values()
        if r.get("origin_id") == second["id"]
    )
    assert (record["mode"], record["accepted_via"]) == ("optional", "yes")


def test_a_reference_protects_but_never_grants_authority(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    # The user installed it; an assignment then references the same version.
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    row = market.assign(item)
    hosted.pull(manager, market.client())
    assert _roles(manager) == [(row["id"], "reference")]
    market.assignments.remove(row)
    report = hosted.pull(manager, market.client())
    assert report.unassigned == []
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    assert _roles(manager) == []


def test_service_key_fingerprints_are_canonical_and_legacy_state_is_adopted(
    tmp_path: Path, market: FakeMarketplace
):
    assert hosted.identity_fingerprint("HTTPS://SFS.test:443/", "k") == (
        hosted.identity_fingerprint("https://sfs.test", "k")
    )
    market.service_keys.add("sk_service")
    manager, _, _ = _manager(tmp_path, market)
    raw_url = "https://SFS.test:443/"
    client = HostedClient(raw_url, "sk_service",
                          transport=httpx.MockTransport(market.handler))
    old_fp = hosted._legacy_fingerprint(raw_url, "sk_service")
    old_ns = f"https://SFS.test:443|key:{old_fp}"
    with HostedState(manager.store_root).edit() as state:
        state["identities"][old_fp] = old_ns
        state["namespaces"][old_ns] = {"api_url": "https://sfs.test", "revoked": {},
                                        "content_blocks": {}}
        state["pulled"][f"{old_ns}|user|~|assignment|osa_1"] = {
            "namespace": old_ns, "scope": "user", "project_root": None, "role": "installer",
            "origin": "assignment", "origin_id": "osa_1", "local_package_id": "skp_x",
        }
    hosted.sync_revocations(client, manager.store_root)
    state = HostedState(manager.store_root).read()
    new_fp = hosted.identity_fingerprint(raw_url, "sk_service")
    new_ns = f"https://sfs.test|key:{new_fp}"
    assert old_fp not in state["identities"] and state["identities"][new_fp] == new_ns
    assert old_ns not in state["namespaces"] and new_ns in state["namespaces"]
    [(key, record)] = state["pulled"].items()
    assert key.startswith(new_ns + "|") and record["namespace"] == new_ns


def test_owner_refresh_honours_the_deadline_and_cancel(
    tmp_path: Path, market: FakeMarketplace
):
    market.publish("review-loop", "1.0.0")  # older server: no owner field
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    calls_before = market.calls_by_url.get("https://sfs.test", 0)
    assert hosted.refresh_package_owners(manager, market.client(), cancel=lambda: True) is False
    assert hosted.refresh_package_owners(manager, market.client(), deadline=0.0) is False
    assert market.calls_by_url.get("https://sfs.test", 0) == calls_before
    assert hosted.refresh_package_owners(manager, market.client()) is True


def test_daemon_stop_cancels_and_retries_are_scheduled_from_completion(
    tmp_path: Path, market: FakeMarketplace
):
    import threading

    market.assign(market.publish("review-loop", "1.0.0"))
    manager, _, home = _manager(tmp_path, market)
    clock = [100.0]
    entered = threading.Event()
    release = threading.Event()

    def _slow_client(url: str, key: str) -> HostedClient:
        entered.set()
        release.wait(5)
        clock[0] += 500.0  # the run takes a long time
        return market.client()

    reconciler = SkillReconciler(
        clock=lambda: clock[0], manager_factory=lambda: manager, client_factory=_slow_client
    )
    reconciler.maybe_run(enabled=True, api_url="u", api_key="k")
    assert entered.wait(5)
    reconciler._stop.set()  # shutdown requested mid-run
    release.set()
    reconciler.wait(10)
    report = reconciler.last_report
    assert report is not None and report.items == []  # stopped before applying
    assert not (home / ".codex" / "skills" / "review-loop").exists()
    # Next attempt counted from COMPLETION (600), not from the start (100).
    assert reconciler._next_attempt == 600.0 + hosted_reconcile_interval()
    # A stopped reconciler never starts again.
    assert reconciler.maybe_run(enabled=True, api_url="u", api_key="k", background=False) is None


def hosted_reconcile_interval() -> float:
    from sessionfs.daemon.skill_reconcile import RECONCILE_INTERVAL_S

    return float(RECONCILE_INTERVAL_S)


# ---------------------------------------------------------------------------
# round 7: atomic record-vs-remove, install-id ABA, cancellation in enforcement
# ---------------------------------------------------------------------------


def test_a_reinstalled_package_is_never_removed_by_stale_pull_authority(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.pull(manager, market.client())
    first = manager.list_installed("user")[0]
    # The user removes it and installs the SAME version again: same hash and
    # (fresh row) same revision - only the installation id differs.
    manager.remove("review-loop", scope="user")
    package = manager.import_source("sessionfs:acme/review-loop")
    manager.install_prepared(package, manager.plan(package, targets=["codex"], scope="user"))
    second = manager.list_installed("user")[0]
    assert (second["content_hash"], second["revision"]) == (first["content_hash"], first["revision"])
    assert second["install_id"] != first["install_id"]
    market.assignments.remove(row)
    report = hosted.pull(manager, market.client())
    assert report.unassigned == []
    assert (home / ".codex" / "skills" / "review-loop").is_dir()


def test_explicit_add_and_remove_retire_pull_authority(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.pull(manager, market.client())
    assert [role for _, role in _roles(manager)] == ["installer"]
    # Explicitly adding the package pull manages makes it the user's.
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    assert [role for _, role in _roles(manager)] == ["reference"]
    market.assignments.remove(row)
    hosted.pull(manager, market.client())
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    # `sfs skill remove` retires it too.
    market.assign(item)
    hosted.pull(manager, market.client())
    package_id = manager.list_installed("user")[0]["package_id"]
    with manager.explicit_change("user"):
        manager.remove(package_id, scope="user")
        assert hosted.retire_pull_authority(manager, "user", package_id, opt_out=True) > 0
    state = HostedState(manager.store_root).read()
    assert all(r["role"] == "reference" for r in state["pulled"].values())
    [(key, entry)] = state["opt_outs"].items()
    assert key.endswith(f"|user|~|assignment|{item['version_id']}") and "pending" not in entry


def test_a_package_removed_before_recording_is_reinstalled_not_dangling(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item)
    real_install = hosted._install_item
    calls = {"n": 0}

    def _install_then_vanish(*args, **kwargs):
        real_install(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            # Another pull removes it between install and record.
            manager.remove("review-loop", scope="user")

    monkeypatch.setattr(hosted, "_install_item", _install_then_vanish)
    report = hosted.pull(manager, market.client())
    assert calls["n"] == 2  # installed again
    assert report.items[0].status == "installed"
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    records = HostedState(manager.store_root).read()["pulled"]
    installed = {row["package_id"]: row["install_id"] for row in manager.list_installed("user")}
    assert all(r["local_package_id"] in installed for r in records.values())
    assert all(r["install_id"] == installed[r["local_package_id"]] for r in records.values())


def test_a_package_that_keeps_vanishing_is_deferred_without_a_reference(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    market.assign(item)
    real_install = hosted._install_item

    def _always_vanish(*args, **kwargs):
        real_install(*args, **kwargs)
        manager.remove("review-loop", scope="user")

    monkeypatch.setattr(hosted, "_install_item", _always_vanish)
    notes: list[str] = []
    report = hosted.pull(manager, market.client(), notify=notes.append)
    assert report.items[0].status == "deferred", (report.items[0].detail, report.notes)
    assert any("removed concurrently" in note for note in notes)
    assert HostedState(manager.store_root).read()["pulled"] == {}


def test_cancellation_stops_before_quarantine_enforcement(
    tmp_path: Path, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"])
    market.revoke(item, "blocked", "malware", kind="version")
    hosted.sync_revocations(market.client(), manager.store_root)
    # Directly: a passed deadline / cancel never starts a package mutation.
    assert hosted.enforce_blocked(manager, deadline=0.0) == []
    assert hosted.enforce_blocked(manager, cancel=lambda: True) == []
    assert (project / ".codex" / "skills" / "review-loop").is_dir()
    # Through pull: a cancel stops before enforcement.
    notes: list[str] = []
    report = hosted.pull(manager, market.client(), cancel=lambda: True, notify=notes.append)
    assert report.blocked_removed == []
    assert any("before enforcing" in note for note in notes)
    assert (project / ".codex" / "skills" / "review-loop").is_dir()
    # Uncancelled, it is quarantined.
    assert [w["name"] for w in hosted.enforce_blocked(manager)] == ["review-loop"]


# ---------------------------------------------------------------------------
# round 8: symlinked outputs, optional opt-out tombstones, lock order
# ---------------------------------------------------------------------------


def test_blocked_output_replaced_by_a_symlink_is_moved_not_followed(
    tmp_path: Path, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, project, home = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"])
    output = project / ".codex" / "skills" / "review-loop" / "SKILL.md"
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do not touch\n", encoding="utf-8")
    output.unlink()
    output.symlink_to(outside)  # the malicious skill swaps its file for a link
    market.revoke(item, "blocked", "malware", kind="version")
    hosted.sync_revocations(market.client(), manager.store_root)
    [result] = hosted.enforce_blocked(manager)
    assert not result.get("error") and not result.get("failed")
    assert not output.exists() and not output.is_symlink()
    assert outside.read_text() == "do not touch\n"  # the target is untouched
    quarantined = Path(result["quarantine_dir"]) / ".codex" / "skills" / "review-loop" / "SKILL.md"
    assert quarantined.is_symlink() and os.readlink(quarantined) == str(outside)
    assert manager.list_installed("project") == []


def test_a_symlinked_parent_is_still_refused(tmp_path: Path, market: FakeMarketplace):
    market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"])
    skill_dir = project / ".codex" / "skills" / "review-loop"
    elsewhere = tmp_path / "elsewhere"
    shutil.move(str(skill_dir), str(elsewhere))
    skill_dir.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(SkillError, match="symlink"):
        manager.remove("review-loop", scope="project")
    assert (elsewhere / "SKILL.md").is_file()


def test_explicit_remove_opts_out_of_an_optional_assignment(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    market.assign(item, mode="optional")
    project = _cli_env(tmp_path, monkeypatch, market.client())
    home = Path.home()
    result = runner.invoke(skill_app, ["pull", "--yes"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    result = runner.invoke(skill_app, ["remove", "review-loop", "--scope", "user"])
    assert result.exit_code == 0, result.output
    # Neither `pull --yes` nor the daemon reinstalls the opted-out version.
    result = runner.invoke(skill_app, ["pull", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    assert [entry["status"] for entry in json.loads(result.output)["items"]] == ["opted-out"]
    manager = SkillManager(project_root=project)
    assert hosted.daemon_reconcile(manager, market.client()).items[0].status == "opted-out"
    assert not (home / ".codex" / "skills" / "review-loop").exists()
    # An explicit interactive "yes" opts back in.
    report = hosted.pull(manager, market.client(), install_optional=lambda item: True)
    assert report.items[0].status == "installed"
    assert (home / ".codex" / "skills" / "review-loop").is_dir()


def test_required_assignments_reinstall_after_an_explicit_remove(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    market.assign(item)  # required
    manager, _, home = _manager(tmp_path, market)
    hosted.pull(manager, market.client())
    with manager.explicit_change("user"):
        package_id = manager.list_installed("user")[0]["package_id"]
        manager.remove(package_id, scope="user")
        hosted.retire_pull_authority(manager, "user", package_id, opt_out=True)
    report = hosted.pull(manager, market.client())
    assert report.items[0].status == "installed"
    assert (home / ".codex" / "skills" / "review-loop").is_dir()


def test_lock_order_checker_rejects_an_inverted_acquisition(tmp_path: Path, market: FakeMarketplace):
    manager, _, _ = _manager(tmp_path, market)
    state = HostedState(manager.store_root)
    with state.edit():
        with pytest.raises(AssertionError, match="lock order violation"):
            with hosted._scope_lock(manager, "user"):
                pass
    with pytest.raises(AssertionError, match="lock order violation"):
        with manager._operation_lock("user"):
            with hosted._scope_lock(manager, "user"):
                pass
    # The documented order is accepted, including re-entry.
    with hosted._scope_lock(manager, "user"), manager._operation_lock("user"), state.edit():
        with hosted._scope_lock(manager, "user"), manager._operation_lock("user"):
            pass


@pytest.mark.parametrize("kind", ["version", "package", "content"])
def test_org_block_never_reaches_a_public_copy(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, kind: str
):
    _no_project(monkeypatch)
    item = market.publish("shared", "1.0.0", owner_org_id="org_a", visibility="org")
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/shared", targets=["codex"], scope="user")
    market.revoke(item, "blocked", "org takedown", kind=kind,
                  block_scope="org", scope_org_id="org_a")
    hosted.sync_revocations(market.client(), manager.store_root)
    state = HostedState(manager.store_root).read()
    flags = hosted.effective_flags(state, hosted.identity_fingerprint("https://sfs.test", "sk_test"))
    row = manager.list_installed("user")[0]
    # Visibility and owner are recorded on the installed row itself.
    assert row["provenance"]["visibility"] == "org"
    assert row["provenance"]["owner_org_id"] == "org_a"
    # The org's own non-public copy is hit.
    assert hosted._row_flag(flags, row, state)["state"] == "blocked"
    # A PUBLIC copy of the same package never is.
    assert hosted._row_flag(flags, _with_provenance(row, visibility="public"), state) is None
    # Unknown visibility (legacy row, no shared fallback): cautious, applies.
    assert hosted._row_flag(flags, _with_provenance(row, visibility=None), state)["state"] == "blocked"
    # Another org's copy: never.
    assert hosted._row_flag(flags, _with_provenance(row, owner_org_id="org_b"), state) is None
    # Enforcement follows the same rule.
    assert [w["name"] for w in hosted.enforce_blocked(manager)] == ["shared"]


def test_install_check_spares_a_public_copy_from_an_org_block(
    tmp_path: Path, market: FakeMarketplace
):
    item = market.publish("shared", "1.0.0", owner_org_id="org_a", visibility="public")
    manager, _, _ = _manager(tmp_path, market)
    market.revoke(item, "blocked", "org takedown", kind="version",
                  block_scope="org", scope_org_id="org_a")
    hosted.sync_revocations(market.client(), manager.store_root)
    # Installing from the PUBLIC listing is not refused by the org block.
    package, _, _ = manager.add("sessionfs:acme/shared", targets=["codex"], scope="user")
    assert package.content_hash == item["content_hash"]
    # The same version from an org listing is.
    market.versions[item["version_id"]]["item"]["visibility"] = "org"
    manager.remove(package.package_id, scope="user")
    with pytest.raises(SkillError, match="BLOCKED"):
        manager.add("sessionfs:acme/shared", targets=["codex"], scope="user")


# ---------------------------------------------------------------------------
# round 9: per-version visibility, kept scoped blocks, durable opt-outs,
# lock-free feed fetch, crash-consistent opt-out
# ---------------------------------------------------------------------------


def test_visibility_is_tracked_per_exact_version(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("shared", "1.0.0", body="one", owner_org_id="org_a", visibility="org")
    v2 = market.publish("shared", "2.0.0", body="two", owner_org_id="org_a", visibility="public",
                        package_id=v1["package_id"])
    manager, _, _ = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/shared@1.0.0", targets=["codex"], scope="user")
    market.revoke(v1, "blocked", "org takedown", kind="package",
                  block_scope="org", scope_org_id="org_a")
    hosted.sync_revocations(market.client(), manager.store_root)
    assert [w["state"] for w in hosted.revocation_warnings(manager)] == ["blocked"]  # private v1
    manager.apply_update(manager.prepare_update(package.package_id, scope="user"))
    row = manager.list_installed("user")[0]
    assert row["source"]["commit"] == v2["version_id"]
    assert row["provenance"]["visibility"] == "public"  # the row of the exact version
    # The public v2 of the same package is NOT hit by the org's package block.
    assert hosted.revocation_warnings(manager) == []


def test_owner_refresh_uses_the_installed_versions_metadata(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("shared", "1.0.0", body="one")  # older server: no owner/visibility
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/shared@1.0.0", targets=["codex"], scope="user")
    market.publish("shared", "2.0.0", body="two", owner_org_id="org_a", visibility="public",
                   package_id=v1["package_id"])  # current version: public
    market.versions[v1["version_id"]]["item"].update(owner_org_id="org_a", visibility="org")
    assert hosted.refresh_package_owners(manager, market.client()) is True
    state = HostedState(manager.store_root).read()
    key = f"{v1['package_id']}@{v1['version_id']}"
    assert state["package_visibility"]["https://sfs.test"] == {key: "org"}  # not the current v2's
    assert state["package_owners"]["https://sfs.test"] == {key: "org_a"}


def test_leaving_an_org_never_lifts_its_content_block(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("shared", "1.0.0", owner_org_id="org_a", visibility="org")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/shared", targets=["codex"])  # project-scope copy
    market.revoke(item, "blocked", "org takedown", kind="content", block_scope="org",
                  scope_org_id="org_a", visible_to={"sk_test"})
    hosted.sync_revocations(market.client(), manager.store_root)
    # The user leaves the org: the event is no longer visible, and a full
    # replay sees nothing about it.
    market.events = [(at, event, {"sk_nobody"}) for at, event, _ in market.events]
    summary = hosted.sync_revocations(market.client(), manager.store_root, force_full=True)
    assert summary["full_replay"] is True
    warnings = hosted.revocation_warnings(manager)
    assert [(w["state"], w["reason"]) for w in warnings] == [("blocked", hosted.KEPT_SCOPED_REASON)]
    # ... and it is still enforced (also for a different identity on the device).
    assert [w["name"] for w in hosted.enforce_blocked(manager)] == ["shared"]
    assert not (project / ".codex" / "skills" / "shared").exists()
    # Only an explicit restore lifts it.
    market.revoke(item, "restored", kind="content", block_scope="org", scope_org_id="org_a")
    hosted.sync_revocations(market.client(), manager.store_root)
    hosted.assert_install_allowed(
        manager.store_root, content_hash=item["content_hash"], version_id=item["version_id"],
        fingerprint=hosted.identity_fingerprint("https://sfs.test", "sk_test"),
        owner_org_id="org_a", visibility="org",
    )


def test_opt_outs_survive_assignment_recreation_and_cleanup(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item, mode="optional")
    hosted.pull(manager, market.client(), install_optional=lambda i: True)
    package_id = manager.list_installed("user")[0]["package_id"]
    with manager.explicit_change("user"):
        keys = hosted.begin_opt_out(manager, "user", package_id)
        manager.remove(package_id, scope="user")
        hosted.finish_opt_out(manager, "user", keys, removed=True)
    # The admin deletes the assignment (cleanup) and recreates it identically.
    market.assignments.remove(row)
    hosted.pull(manager, market.client())
    market.assign(item, mode="optional")
    report = hosted.pull(manager, market.client(), install_optional=lambda i: not i.opted_out)
    assert report.items[0].status == "opted-out"
    assert not (home / ".codex" / "skills" / "review-loop").exists()
    # An explicit `sfs skill add` lifts it.
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    assert HostedState(manager.store_root).read()["opt_outs"] == {}


def test_a_crash_after_removal_still_leaves_the_opt_out(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional")
    hosted.pull(manager, market.client(), install_optional=lambda i: True)
    package_id = manager.list_installed("user")[0]["package_id"]
    with manager.explicit_change("user"):
        hosted.begin_opt_out(manager, "user", package_id)
        manager.remove(package_id, scope="user")
        # ... crash: finish_opt_out never runs.
    entries = list(HostedState(manager.store_root).read()["opt_outs"].values())
    assert [entry.get("pending") for entry in entries] == [True]
    report = hosted.pull(manager, market.client(), install_optional=lambda i: not i.opted_out)
    assert report.items[0].status == "opted-out"
    assert not (home / ".codex" / "skills" / "review-loop").exists()


def test_a_failed_removal_rolls_the_pending_opt_out_back(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    market.assign(item, mode="optional")
    _cli_env(tmp_path, monkeypatch, market.client())
    assert runner.invoke(skill_app, ["pull", "--yes"]).exit_code == 0

    def _fail(self, *args, **kwargs):
        raise SkillError("disk full")

    monkeypatch.setattr(SkillManager, "remove", _fail)
    result = runner.invoke(skill_app, ["remove", "review-loop", "--scope", "user"])
    assert result.exit_code != 0
    store = Path.home() / ".sessionfs" / "skills"
    assert HostedState(store).read()["opt_outs"] == {}


def test_the_feed_is_fetched_without_the_hosted_lock_and_merged_by_cas(
    tmp_path: Path, market: FakeMarketplace
):
    from sessionfs.skills import state as state_module

    items = [market.publish(f"pkg-{n}", "1.0.0") for n in range(3)]
    for item in items:
        market.revoke(item, "yanked", "old", kind="version")
    manager, _, _ = _manager(tmp_path, market)
    real = market.handler
    calls = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/marketplace/revocations":
            calls["n"] += 1
            # No hosted-state lock is held while paging the feed.
            assert state_module.LOCK_RANKS["hosted"] not in (
                getattr(state_module._LOCK_STACK, "stack", None) or []
            )
            if calls["n"] == 1:
                # Another process moves this identity's cursor meanwhile.
                with HostedState(manager.store_root).edit() as state:
                    state["namespaces"]["https://sfs.test|usr_me"] = {
                        "api_url": "https://sfs.test", "revocations_since": "x",
                        "last_full_replay": datetime.now(timezone.utc).isoformat(),
                        "revoked": {}, "content_blocks": {},
                    }
        return real(request)

    market.handler = _handler  # type: ignore[method-assign]
    summary = hosted.sync_revocations(market.client(), manager.store_root, force_full=True)
    assert summary["complete"] is True
    # The first fetch was discarded (fence moved) and redone.
    assert calls["n"] > 2
    assert len(HostedState(manager.store_root).read()["namespaces"][
        "https://sfs.test|usr_me"]["revoked"]) == 3


# ---------------------------------------------------------------------------
# owner-layer yanks (block_scope "owner")
# ---------------------------------------------------------------------------


def test_an_org_yank_of_the_private_listing_spares_the_public_copy(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("shared", "1.0.0", owner_org_id="org_a", visibility="org")
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/shared", targets=["codex"], scope="user")
    # The org yanks its PRIVATE listing of this version (owner layer).
    market.revoke(item, "yanked", "superseded internally", kind="version",
                  block_scope="owner", scope_org_id="org_a")
    hosted.sync_revocations(market.client(), manager.store_root)
    state = HostedState(manager.store_root).read()
    flags = hosted.effective_flags(state, hosted.identity_fingerprint("https://sfs.test", "sk_test"))
    row = manager.list_installed("user")[0]
    # A copy installed from the org listing is yanked ...
    assert hosted._row_flag(flags, row, state)["state"] == "yanked"
    # ... the SAME version installed from the public listing is not.
    assert hosted._row_flag(flags, _with_provenance(row, visibility="public"), state) is None
    # Another org's copy is not; unknown visibility stays cautious.
    assert hosted._row_flag(flags, _with_provenance(row, owner_org_id="org_b"), state) is None
    assert hosted._row_flag(flags, _with_provenance(row, visibility=None), state)["state"] == "yanked"


def test_a_personal_owner_yank_only_reaches_non_public_copies(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("mine", "1.0.0", owner_org_id=None, visibility="user")
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/mine", targets=["codex"], scope="user")
    market.revoke(item, "yanked", "my mistake", kind="version", block_scope="owner",
                  scope_org_id=None)
    hosted.sync_revocations(market.client(), manager.store_root)
    state = HostedState(manager.store_root).read()
    flags = hosted.effective_flags(state, hosted.identity_fingerprint("https://sfs.test", "sk_test"))
    row = manager.list_installed("user")[0]
    assert hosted._row_flag(flags, row, state)["state"] == "yanked"
    assert hosted._row_flag(flags, _with_provenance(row, visibility="public"), state) is None


def test_an_owner_yank_lapses_after_leaving_the_org_and_never_reaches_another_profile(
    tmp_path: Path, market: FakeMarketplace
):
    item = market.publish("shared", "1.0.0", owner_org_id="org_a", visibility="org")
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/shared", targets=["codex"])
    market.revoke(item, "yanked", "org yank", kind="version", block_scope="owner",
                  scope_org_id="org_a", visible_to={"sk_test"})
    hosted.sync_revocations(market.client(), manager.store_root)
    assert [w["state"] for w in hosted.revocation_warnings(manager)] == ["yanked"]
    # Another profile on the device never imports Alice's yank.
    hosted.sync_revocations(market.client("sk_other"), manager.store_root)
    bob = hosted.identity_fingerprint("https://sfs.test", "sk_other")
    assert HostedState(manager.store_root).read()["scoped_blocks"].get("https://sfs.test", {}) == {}
    hosted.assert_install_allowed(
        manager.store_root, content_hash=item["content_hash"], version_id=item["version_id"],
        fingerprint=bob, owner_org_id="org_a", visibility="org",
    )
    # Alice leaves the org; a full replay drops the yank she can no longer see.
    market.events = [(at, event, {"sk_nobody"}) for at, event, _ in market.events]
    hosted.sync_revocations(market.client(), manager.store_root, force_full=True)
    assert hosted.revocation_warnings(manager) == []
    hosted.assert_install_allowed(
        manager.store_root, content_hash=item["content_hash"], version_id=item["version_id"],
        fingerprint=hosted.identity_fingerprint("https://sfs.test", "sk_test"),
        owner_org_id="org_a", visibility="org",
    )


def test_target_entries_are_purged_from_kept_scoped_blocks_on_load(
    tmp_path: Path, market: FakeMarketplace
):
    manager, _, _ = _manager(tmp_path, market)
    with HostedState(manager.store_root).edit() as state:
        state["scoped_blocks"]["https://sfs.test"] = {
            "skv_" + "1" * 24 + "#owner:org_a": {"state": "yanked", "kind": "version"},
            "content:sha256:" + "a" * 64 + "|org|org_a": {"state": "blocked", "kind": "content"},
        }
    table = HostedState(manager.store_root).read()["scoped_blocks"]["https://sfs.test"]
    assert list(table) == ["content:sha256:" + "a" * 64 + "|org|org_a"]


# ---------------------------------------------------------------------------
# round 10: layered state, durable quarantine/put-back, unsupported tools,
# malformed rows
# ---------------------------------------------------------------------------


def _warn_states(manager: SkillManager) -> list[str]:
    return [w["state"] for w in hosted.revocation_warnings(manager)]


def test_a_platform_restore_never_clears_an_owner_yank(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("shared", "1.0.0", owner_org_id="org_a", visibility="org")
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/shared", targets=["codex"])
    market.revoke(item, "yanked", "owner yank", kind="version", block_scope="owner",
                  scope_org_id="org_a")
    market.revoke(item, "blocked", "platform block", kind="version", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)
    assert _warn_states(manager) == ["blocked"]
    market.revoke(item, "restored", kind="version", block_scope="global")  # platform restore
    hosted.sync_revocations(market.client(), manager.store_root)
    assert _warn_states(manager) == ["yanked"]  # the owner layer is still yanked
    market.revoke(item, "restored", kind="version", block_scope="owner", scope_org_id="org_a")
    hosted.sync_revocations(market.client(), manager.store_root)
    assert _warn_states(manager) == []


def test_an_owner_restore_never_clears_a_platform_yank(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("shared", "1.0.0", owner_org_id="org_a", visibility="org")
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/shared", targets=["codex"])
    market.revoke(item, "yanked", "platform yank", kind="version")  # block_scope null
    market.revoke(item, "yanked", "owner yank", kind="version", block_scope="owner",
                  scope_org_id="org_a")
    market.revoke(item, "restored", kind="version", block_scope="owner", scope_org_id="org_a")
    hosted.sync_revocations(market.client(), manager.store_root)
    assert [(w["state"], w["reason"]) for w in hosted.revocation_warnings(manager)] == [
        ("yanked", "platform yank")]
    # The same holds when the events arrive across separate syncs.
    manager2, _, _ = _manager(tmp_path / "second", market)
    market.events.clear()
    market.revoke(item, "yanked", "platform yank", kind="version")
    hosted.sync_revocations(market.client(), manager2.store_root)
    market.revoke(item, "restored", kind="version", block_scope="owner", scope_org_id="org_a")
    hosted.sync_revocations(market.client(), manager2.store_root)
    assert hosted.revocation_for(
        hosted.effective_flags(
            HostedState(manager2.store_root).read(),
            hosted.identity_fingerprint("https://sfs.test", "sk_test"),
        ),
        content_hash=item["content_hash"], version_id=item["version_id"],
    )["state"] == "yanked"


def test_quarantine_copy_is_durable_before_the_commit(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills import state as state_module

    item = market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"])
    market.revoke(item, "blocked", "malware", kind="version")
    hosted.sync_revocations(market.client(), manager.store_root)
    events: list[tuple[str, str]] = []
    real_dir, real_json = state_module._fsync_directory, state_module.atomic_json

    def _spy_dir(path: Path) -> None:
        events.append(("fsync-dir", str(path)))
        real_dir(path)

    def _spy_json(path: Path, value: Any) -> None:
        events.append(("write", f"{Path(path).name}:{value.get('phase', '')}"))
        real_json(path, value)

    monkeypatch.setattr(state_module, "_fsync_directory", _spy_dir)
    monkeypatch.setattr(state_module, "atomic_json", _spy_json)
    [result] = hosted.enforce_blocked(manager)
    quarantined = Path(result["quarantine_dir"]) / ".codex" / "skills" / "review-loop"
    commit = events.index(("write", "skills.journal.json:committed"))
    dirs = [path for kind, path in events[:commit] if kind == "fsync-dir"]
    # The quarantine directory (and its parents up to the quarantine root)
    # were made durable BEFORE the journal commit.
    assert str(quarantined) in dirs
    assert str(Path(result["quarantine_dir"]).parent) in dirs


def test_put_back_is_durable_before_the_commit(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills import state as state_module

    market.publish("review-loop", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"])
    edited = project / ".codex" / "skills" / "review-loop" / "SKILL.md"
    edited.write_text("mine\n", encoding="utf-8")
    events: list[tuple[str, str]] = []
    real_dir, real_json = state_module._fsync_directory, state_module.atomic_json

    def _spy_dir(path: Path) -> None:
        events.append(("fsync-dir", str(path)))
        real_dir(path)

    def _spy_json(path: Path, value: Any) -> None:
        events.append(("write", f"{Path(path).name}:{value.get('phase', '')}"))
        real_json(path, value)

    monkeypatch.setattr(state_module, "_fsync_directory", _spy_dir)
    monkeypatch.setattr(state_module, "atomic_json", _spy_json)
    result = manager.remove("review-loop", scope="project")
    assert result["dirty"] == [str(edited)] and edited.read_text() == "mine\n"
    commit = events.index(("write", "skills.journal.json:committed"))
    state_write = events.index(("write", "skills.state.json:"))
    # After the put-back rename, BOTH the destination and the transaction
    # directory were fsynced before state and journal were committed.
    put_back = [path for kind, path in events[:state_write] if kind == "fsync-dir"]
    assert str(edited.parent) in put_back
    assert any(".skills-txn-" in path and path.endswith("moved") for path in put_back)
    assert state_write < commit


def test_unsupported_target_tools_are_skipped_not_widened(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    other = market.publish("lint-helper", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(other)
    hosted.pull(manager, market.client())
    assert (home / ".codex" / "skills" / "lint-helper").is_dir()
    market.assignments.remove(first)
    market.assign(item, tools=["future-tool"])  # only tools this sfs doesn't know
    notes: list[str] = []
    report = hosted.pull(
        manager, market.client(), notify=notes.append
    )
    assert report.items == []
    assert any("doesn't support: future-tool" in note for note in notes)
    assert report.complete_sources["assignment"] is False
    # Cleanup was suppressed: the earlier pulled package stays.
    assert (home / ".codex" / "skills" / "lint-helper").is_dir()
    assert not (home / ".claude" / "skills" / "review-loop").exists()
    # A partially unsupported list installs only the supported tools.
    market.assignments.clear()
    market.assign(item, tools=["codex", "future-tool"])
    report = hosted.pull(manager, market.client())
    assert [entry.targets for entry in report.items] == [["codex"]]
    assert _targets_for(manager, "review-loop") == ["codex"]


def _targets_for(manager: SkillManager, name: str) -> list[str]:
    row = next(row for row in manager.list_installed("user") if row["name"] == name)
    return sorted(row["targets"])


def test_malformed_rows_mark_the_source_incomplete(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    keep = market.publish("review-loop", "1.0.0")
    fresh = market.publish("lint-helper", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(keep)
    hosted.pull(manager, market.client())
    market.assignments.remove(first)
    market.assign(fresh)
    market.assignments.append({**first, "version_id": "not-a-version"})  # malformed
    notes: list[str] = []
    report = hosted.pull(
        manager, market.client(), notify=notes.append
    )
    assert report.complete_sources["assignment"] is False
    assert any("could not be interpreted" in note for note in notes)
    # The valid row still applies; the earlier package is not removed.
    assert (home / ".codex" / "skills" / "lint-helper").is_dir()
    assert (home / ".codex" / "skills" / "review-loop").is_dir()


# ---------------------------------------------------------------------------
# round 11: strict listing rows, row-local provenance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("malformed", [
    "not-a-row",  # a non-object item
    {"target_tools": None},  # null targeting is never "unrestricted"
    {"target_tools": "codex"},  # a string is not a list
    {"mode": None},  # no default mode
    {"mode": "sometimes"},
])
def test_any_invalid_assignment_row_marks_the_source_incomplete(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, malformed: Any
):
    _no_project(monkeypatch)
    keep = market.publish("review-loop", "1.0.0")
    fresh = market.publish("lint-helper", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(keep)
    hosted.pull(manager, market.client())
    market.assignments.remove(first)
    market.assign(fresh)
    # The raw item reaches the validator unfiltered.
    market.assignments.append(malformed if isinstance(malformed, str) else {**first, **malformed})
    report = hosted.pull(manager, market.client())
    assert report.complete_sources["assignment"] is False
    assert (home / ".codex" / "skills" / "lint-helper").is_dir()
    assert (home / ".codex" / "skills" / "review-loop").is_dir()


@pytest.mark.parametrize("malformed", [
    "not-a-row",
    {"status": "pending"},  # only approved requests are acted on
    {"status": None},
    {"target_tools": None},
    {"requested_capabilities": None},
])
def test_any_invalid_request_row_marks_the_source_incomplete(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, malformed: Any
):
    _no_project(monkeypatch, "proj_1")
    item = market.publish("project-skill", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    row = {"id": "sir_" + "1" * 24, "status": "approved", "target_device_id": None,
           "target_tools": ["codex"], "requested_capabilities": ["instructions"],
           "version_id": item["version_id"], "content_hash": item["content_hash"],
           "target_scope": "project", "target_user_id": "usr_me"}
    market.requests = [row]
    hosted.pull(manager, market.client())
    assert (project / ".codex" / "skills" / "project-skill").is_dir()
    market.requests = [malformed if isinstance(malformed, str) else {**row, **malformed}]
    report = hosted.pull(manager, market.client())
    assert report.complete_sources["request"] is False
    assert (project / ".codex" / "skills" / "project-skill").is_dir()


def test_provenance_is_per_installed_row_and_never_changed_by_an_import(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    # One version, first installed from org_a's PRIVATE listing ...
    item = market.publish("shared", "1.0.0", owner_org_id="org_a", visibility="org")
    entry = market.versions[item["version_id"]]["item"]
    entry["listing_id"] = "lst_private"
    manager, project, home = _manager(tmp_path, market)
    manager.add("sessionfs:acme/shared", targets=["codex"], scope="user")
    # ... then the SAME version installed (project scope) from its public listing.
    entry.update(visibility="public", listing_id="lst_public")
    manager.add("sessionfs:acme/shared", targets=["codex"], scope="project")
    # A failed import of the version (from the private listing again) changes nothing.
    entry.update(visibility="org", listing_id="lst_private")
    market.tamper[item["version_id"]] = b"not the archive"
    with pytest.raises(SkillError):
        manager.add("sessionfs:acme/shared", targets=["claude-code"], scope="project")
    del market.tamper[item["version_id"]]
    user_row = manager.list_installed("user")[0]
    project_row = manager.list_installed("project")[0]
    assert user_row["provenance"] == {
        "listing_id": "lst_private", "visibility": "org", "owner_org_id": "org_a"}
    assert project_row["provenance"] == {
        "listing_id": "lst_public", "visibility": "public", "owner_org_id": "org_a"}
    state = HostedState(manager.store_root).read()
    assert state["package_owners"] == {} and state["package_visibility"] == {}
    # An owner-layer yank and an org block each hit only the private copy.
    for state_name, block_scope in (("yanked", "owner"), ("blocked", "org")):
        market.revoke(item, state_name, "org action", kind="version",
                      block_scope=block_scope, scope_org_id="org_a")
        hosted.sync_revocations(market.client(), manager.store_root)
        warnings = hosted.revocation_warnings(manager)
        assert [(w["scope"], w["state"]) for w in warnings] == [("user", state_name)]
        market.revoke(item, "restored", kind="version", block_scope=block_scope,
                      scope_org_id="org_a")
        hosted.sync_revocations(market.client(), manager.store_root)
        assert hosted.revocation_warnings(manager) == []
    market.revoke(item, "blocked", "org takedown", kind="version",
                  block_scope="org", scope_org_id="org_a")
    hosted.sync_revocations(market.client(), manager.store_root)
    assert [w["name"] for w in hosted.enforce_blocked(manager)] == ["shared"]
    assert not (home / ".codex" / "skills" / "shared").exists()
    assert (project / ".codex" / "skills" / "shared").is_dir()


# ---------------------------------------------------------------------------
# round 11: removal never deletes or copies the moved inode
# ---------------------------------------------------------------------------


def _skill_file(home: Path, name: str = "review-loop") -> Path:
    return home / ".codex" / "skills" / name / "SKILL.md"


def test_removal_renames_clean_outputs_into_the_trash(
    tmp_path: Path, market: FakeMarketplace
):
    market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    inode = _skill_file(home).stat().st_ino
    result = manager.remove(package.package_id, scope="user")
    assert not _skill_file(home).exists()
    trash_dir = Path(result["trash_dir"])
    assert trash_dir.parent.parent == state_module.trash_root(manager.store_root)
    parked = Path(next(item["to"] for item in result["trashed"] if item["to"].endswith("SKILL.md")))
    assert parked.is_relative_to(trash_dir)
    assert parked.stat().st_ino == inode  # the same inode: renamed, not copied
    usage = manager.trash_usage()
    assert usage["files"] >= 1 and usage["bytes"] > 0 and usage["retention_days"] == 7


def test_removal_puts_a_modified_output_back_instead_of_trashing_it(
    tmp_path: Path, market: FakeMarketplace
):
    market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    _skill_file(home).write_text("my edits")
    result = manager.remove(package.package_id, scope="user")
    assert result["dirty"] == [str(_skill_file(home))]
    assert _skill_file(home).read_text() == "my edits"
    assert all(not item["to"].endswith("SKILL.md") for item in result.get("trashed", []))


def test_quarantine_renames_the_inode_and_falls_back_to_a_copy_across_filesystems(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    import errno

    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    inode = _skill_file(home).stat().st_ino
    market.revoke(item, "blocked", "malware", kind="content", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)
    [warning] = hosted.enforce_blocked(manager)
    quarantined = Path(warning["quarantine_dir"]) / ".codex" / "skills" / "review-loop" / "SKILL.md"
    assert quarantined.stat().st_ino == inode  # renamed into quarantine
    # Another filesystem: the rename into quarantine fails with EXDEV, so a
    # durable copy is made and the moved entry is only dropped at commit.
    real_rename = state_module.rename_noreplace
    crossed: list[Path] = []

    def _cross_fs(source: Path, destination: Path) -> None:
        if ".skills-txn-" in str(source) and "quarantine" in Path(destination).parts:
            crossed.append(Path(source))
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_rename(source, destination)

    monkeypatch.setattr(state_module, "rename_noreplace", _cross_fs)
    other = market.publish("lint-helper", "1.0.0")
    manager.add("sessionfs:acme/lint-helper", targets=["codex"], scope="user")
    market.revoke(other, "blocked", "malware", kind="content", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)
    [warning] = [w for w in hosted.enforce_blocked(manager) if w["name"] == "lint-helper"]
    copy = Path(warning["quarantine_dir"]) / ".codex" / "skills" / "lint-helper" / "SKILL.md"
    assert copy.is_file() and not _skill_file(home, "lint-helper").exists()
    assert crossed  # the copy fallback really ran


def test_a_crash_after_parking_puts_the_output_back_from_the_trash(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills.state import SkillStateStore

    market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    inode = _skill_file(home).stat().st_ino

    def _crash(self, paths):
        raise OSError("power loss")

    monkeypatch.setattr(SkillStateStore, "_prune_empty_parents", _crash)
    with pytest.raises(OSError):
        manager.remove(package.package_id, scope="user")
    monkeypatch.undo()
    # Recovery brought the parked inode back; the package is still installed.
    assert _skill_file(home).stat().st_ino == inode
    assert [row["package_id"] for row in manager.list_installed("user")] == [package.package_id]
    assert manager.doctor("user", repair_cache=False) == []


def test_old_trash_is_pruned_on_a_later_removal(tmp_path: Path, market: FakeMarketplace):
    market.publish("review-loop", "1.0.0")
    market.publish("lint-helper", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    first, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    second, _, _ = manager.add("sessionfs:acme/lint-helper", targets=["codex"], scope="user")
    old = Path(manager.remove(first.package_id, scope="user")["trash_dir"])
    eight_days = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y%m%dT%H%M%S%fZ")
    aged = old.with_name(f"{eight_days}-deadbeef")
    old.rename(aged)
    recent = Path(manager.remove(second.package_id, scope="user")["trash_dir"])
    assert not aged.exists()
    assert recent.is_dir()


def test_parking_directories_are_fsynced_up_to_the_pre_existing_ancestor(
    tmp_path: Path, monkeypatch
):
    synced: list[Path] = []
    monkeypatch.setattr(state_module, "_fsync_directory", lambda path: synced.append(Path(path)))
    store = tmp_path / "store"
    store.mkdir()
    target = store / "quarantine" / "spk_1" / "stamp" / ".codex" / "skills"
    state_module.make_durable_dirs(target, store)
    assert target.is_dir()
    expected = [target, target.parent, store / "quarantine" / "spk_1" / "stamp",
                store / "quarantine" / "spk_1", store / "quarantine", store]
    assert set(synced) == set(expected) and synced[-1] == store
    (store / "link").symlink_to(tmp_path)
    with pytest.raises(SkillError, match="symlink"):
        state_module.make_durable_dirs(store / "link" / "x", store)


def test_doctor_reports_the_trash_size(tmp_path: Path, monkeypatch, market: FakeMarketplace):
    _no_project(monkeypatch)
    market.publish("review-loop", "1.0.0")
    _cli_env(tmp_path, monkeypatch, market.client())
    assert runner.invoke(
        skill_app, ["add", "sessionfs:acme/review-loop", "--for", "codex", "--scope", "user",
                    "--yes"]).exit_code == 0
    assert runner.invoke(skill_app, ["remove", "review-loop", "--scope", "user"]).exit_code == 0
    result = runner.invoke(skill_app, ["doctor"])
    assert "Trash: " in result.output and "kept 7 days" in result.output


# ---------------------------------------------------------------------------
# round 11: the server's own `incomplete` flag on the listings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extra, complete", [
    ({"incomplete": True, "omitted_count": 2}, False),
    ({"incomplete": "yes"}, False),  # not a boolean: fail safe
    ({"incomplete": None}, False),
    ({"incomplete": False, "omitted_count": 0}, True),
    ({}, True),  # older server: judged by its rows alone
])
def test_server_omitted_assignments_suppress_the_unassigned_report(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, extra: dict, complete: bool
):
    _no_project(monkeypatch, "proj_1")  # requests readable (none)
    keep = market.publish("review-loop", "1.0.0")
    fresh = market.publish("lint-helper", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(keep)
    hosted.pull(manager, market.client())
    market.assignments.remove(first)
    market.assign(fresh)
    market.listing_extra["assignment"] = extra
    notes: list[str] = []
    report = hosted.pull(
        manager, market.client(), notify=notes.append
    )
    assert report.complete_sources["assignment"] is complete
    # The rows the server did return always apply; nothing is uninstalled,
    # and only a COMPLETE listing reports the package as no longer assigned.
    assert (home / ".codex" / "skills" / "lint-helper").is_dir()
    assert (home / ".codex" / "skills" / "review-loop").is_dir()
    assert [c["name"] for c in report.unassigned] == (["review-loop"] if complete else [])
    if not complete:
        assert any("omitted" in note for note in notes)
        if extra.get("omitted_count"):
            assert any("omitted 2 org assignment(s)" in note for note in notes)


def test_server_omitted_install_requests_suppress_the_unassigned_report(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch, "proj_1")
    item = market.publish("project-skill", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    market.requests = [{
        "id": "sir_" + "1" * 24, "status": "approved", "target_device_id": None,
        "target_tools": ["codex"], "requested_capabilities": ["instructions"],
        "version_id": item["version_id"], "content_hash": item["content_hash"],
        "target_scope": "project", "target_user_id": "usr_me"}]
    hosted.pull(manager, market.client())
    # The grant's record became unreadable: the server omits it and says so.
    market.requests = []
    market.listing_extra["request"] = {"incomplete": True, "omitted_count": 1}
    report = hosted.pull(manager, market.client())
    assert report.complete_sources["request"] is False
    assert report.unassigned == []
    assert (project / ".codex" / "skills" / "project-skill").is_dir()
    # Once the listing is complete again it is reported - never uninstalled.
    market.listing_extra["request"] = {"incomplete": False, "omitted_count": 0}
    report = hosted.pull(manager, market.client())
    assert report.complete_sources["request"] is True
    assert [c["name"] for c in report.unassigned] == ["project-skill"]
    assert (project / ".codex" / "skills" / "project-skill").is_dir()


def test_listing_keeps_raw_rows_and_the_server_flag(market: FakeMarketplace):
    market.assignments.append("junk")
    market.listing_extra["assignment"] = {"incomplete": True, "omitted_count": 3}
    listing = market.client().assignments()
    assert list(listing) == ["junk"]
    assert (listing.server_incomplete, listing.omitted_count) == (True, 3)
    market.listing_extra["assignment"] = {"omitted_count": True}  # bool is not a count
    listing = market.client().assignments()
    assert (listing.server_incomplete, listing.omitted_count) == (False, 0)


# ---------------------------------------------------------------------------
# round 12: every output-dropping path is rename-first and trashed
# ---------------------------------------------------------------------------


def _trashed_files(manager: SkillManager) -> list[Path]:
    root = state_module.trash_root(manager.store_root)
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else []


def _edit_after_check(monkeypatch, target: Path, text: str = "my late edit") -> None:
    """Simulate an edit landing AFTER every preflight hash, just before the
    output is moved out of its tool directory."""
    from sessionfs.skills.state import SkillStateStore

    real = SkillStateStore._move_out

    def _late_edit(self, path, action):
        if Path(path) == target and target.exists():
            target.write_text(text)
        return real(self, path, action)

    monkeypatch.setattr(SkillStateStore, "_move_out", _late_edit)


def _versions(market: FakeMarketplace) -> tuple[dict, dict]:
    v1 = market.publish("shared", "1.0.0", body="one")
    v2 = market.publish("shared", "2.0.0", body="two", package_id=v1["package_id"])
    return v1, v2


def test_an_update_moves_the_replaced_output_into_the_trash(
    tmp_path: Path, market: FakeMarketplace
):
    _versions(market)
    manager, _, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/shared@1.0.0", targets=["codex"], scope="user")
    skill = _skill_file(home, "shared")
    inode, old_text = skill.stat().st_ino, skill.read_text()
    manager.apply_update(manager.prepare_update(package.package_id, scope="user"))
    assert "two" in skill.read_text()
    [trashed] = [path for path in _trashed_files(manager) if path.name == "SKILL.md"]
    assert trashed.stat().st_ino == inode and trashed.read_text() == old_text


def test_an_update_never_overwrites_an_edit_made_after_the_check(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _versions(market)
    manager, _, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/shared@1.0.0", targets=["codex"], scope="user")
    skill = _skill_file(home, "shared")
    prepared = manager.prepare_update(package.package_id, scope="user")
    _edit_after_check(monkeypatch, skill)
    with pytest.raises(SkillError, match="modified managed output"):
        manager.apply_update(prepared)
    assert skill.read_text() == "my late edit"
    assert manager.list_installed("user")[0]["content_hash"] == package.content_hash
    assert _trashed_files(manager) == []
    assert not (manager.store_root / "skills.journal.json").exists()


def test_dropping_a_target_trashes_its_outputs(tmp_path: Path, market: FakeMarketplace):
    market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex", "claude-code"], scope="user")
    claude = home / ".claude" / "skills" / "review-loop" / "SKILL.md"
    inode = claude.stat().st_ino
    manager.compile("review-loop", scope="user", drop=["claude-code"])
    assert not claude.exists() and _skill_file(home).is_file()
    [trashed] = [path for path in _trashed_files(manager) if ".claude" in path.parts]
    assert trashed.stat().st_ino == inode


def test_a_dropped_target_edited_after_the_check_is_kept(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex", "claude-code"], scope="user")
    claude = home / ".claude" / "skills" / "review-loop" / "SKILL.md"
    _edit_after_check(monkeypatch, claude)
    manager.compile("review-loop", scope="user", drop=["claude-code"])
    assert claude.read_text() == "my late edit"
    row = manager.list_installed("user")[0]
    assert row["dirty_stale_outputs"] == [str(claude)]
    assert _trashed_files(manager) == []


def _sync_removing_everything(manager: SkillManager) -> list[dict]:
    lock = manager.project_root / ".sessionfs" / "skills.lock.json"
    state_module.atomic_json(lock, {"version": 1, "packages": {}})
    return manager.sync(scope="project")


def test_sync_removal_trashes_clean_outputs_and_keeps_late_edits(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    market.publish("review-loop", "1.0.0")
    market.publish("lint-helper", "1.0.0")
    manager, project, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="project")
    manager.add("sessionfs:acme/lint-helper", targets=["codex"], scope="project")
    clean = _skill_file(project, "review-loop")
    edited = _skill_file(project, "lint-helper")
    inode = clean.stat().st_ino
    _edit_after_check(monkeypatch, edited)
    results = _sync_removing_everything(manager)
    by_name = {item["name"]: item for item in results}
    assert by_name["review-loop"]["removed"] == [str(clean)]
    assert by_name["lint-helper"]["dirty"] == [str(edited)]
    assert not clean.exists() and edited.read_text() == "my late edit"
    [trashed] = _trashed_files(manager)
    assert trashed.stat().st_ino == inode
    assert manager.list_installed("project") == []


def test_a_crash_mid_update_puts_the_replaced_output_back(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills.state import SkillStateStore

    _versions(market)
    manager, _, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/shared@1.0.0", targets=["codex"], scope="user")
    skill = _skill_file(home, "shared")
    inode = skill.stat().st_ino
    prepared = manager.prepare_update(package.package_id, scope="user")

    def _crash(self, *args, **kwargs):
        raise OSError("power loss")

    # A hard crash after the drop (the old output already sits in the trash)
    # and before the write; in-line recovery does not run.
    monkeypatch.setattr(SkillStateStore, "_write_output", _crash)
    monkeypatch.setattr(SkillStateStore, "_recover_locked", lambda self: None)
    with pytest.raises(OSError):
        manager.apply_update(prepared)
    assert not skill.exists() and len(_trashed_files(manager)) == 1
    monkeypatch.undo()
    manager.state_store("user").recover()
    assert skill.stat().st_ino == inode and "one" in skill.read_text()
    assert _trashed_files(manager) == []
    assert manager.list_installed("user")[0]["content_hash"] == package.content_hash


def test_a_crash_after_writing_removes_only_our_write_and_restores_the_original(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills.state import SkillStateStore

    _versions(market)
    manager, _, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/shared@1.0.0", targets=["codex"], scope="user")
    skill = _skill_file(home, "shared")
    inode = skill.stat().st_ino
    prepared = manager.prepare_update(package.package_id, scope="user")

    def _crash(path, value):
        if Path(path).name == "skills.state.json":
            raise OSError("power loss")
        return real_atomic(path, value)

    real_atomic = state_module.atomic_json
    monkeypatch.setattr(state_module, "atomic_json", _crash)
    monkeypatch.setattr(SkillStateStore, "_recover_locked", lambda self: None)
    with pytest.raises(OSError):
        manager.apply_update(prepared)
    assert "two" in skill.read_text()  # the new version had been written
    monkeypatch.undo()
    manager.state_store("user").recover()
    assert skill.stat().st_ino == inode and "one" in skill.read_text()
    assert _trashed_files(manager) == []


# ---------------------------------------------------------------------------
# round 13: every consent requirement maps to an approved capability
# ---------------------------------------------------------------------------


def test_a_hook_extension_needs_hooks_approved_on_the_request(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch, "proj_1")
    item = market.publish("hooked", "1.0.0", hook_extension=True)
    manager, project, _ = _manager(tmp_path, market)
    request = {
        "id": "sir_" + "1" * 24, "status": "approved", "version_id": item["version_id"],
        "content_hash": item["content_hash"], "target_user_id": "usr_me",
        "target_scope": "project", "target_device_id": None, "target_tools": ["codex"],
        "requested_capabilities": ["instructions"],
    }
    market.requests = [request]
    flags = {"allow_executable_extensions": True}
    report = hosted.pull(manager, market.client(), flags=flags)
    assert report.items[0].status == "skipped"
    assert "not approved on an install request" in report.items[0].detail
    assert not (project / ".codex" / "skills" / "hooked").exists()
    request["requested_capabilities"] = ["instructions", "hooks"]
    report = hosted.pull(manager, market.client(), flags=flags)
    assert report.items[0].status == "installed"


def test_every_consent_requirement_is_mapped_or_refused():
    from types import SimpleNamespace

    # Every requirement the planner (adapters.plan_package) emits has a local flag.
    for requirement in ("skill-scripts", "skill-hooks", "executable-extensions", "dlp-findings", "mcp"):
        assert requirement in hosted._FLAG_FOR_REQUIREMENT
    request_item = SimpleNamespace(
        origin_targets={"request:x": ["codex"]},
        origin_caps={"request:x": {"instructions", "assets", "scripts", "mcp", "hooks"}},
    )
    assignment_item = SimpleNamespace(
        origin_targets={"assignment:y": ["codex"]}, origin_caps={"assignment:y": None}
    )
    # Mapped requirements are authorized by a request that approved them ...
    assert hosted._origin_authorizes(request_item, "codex", ["executable-extensions"])
    # ... an unmapped one (no server capability) never is, whatever was approved,
    assert not hosted._origin_authorizes(request_item, "codex", ["dlp-findings"])
    assert not hosted._origin_authorizes(request_item, "codex", ["something-new"])
    # ... while an assignment (no capability list) still can.
    assert hosted._origin_authorizes(assignment_item, "codex", ["dlp-findings"])


def test_pull_never_claims_an_install_an_explicit_add_made_in_the_gap(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item)
    real_plan = SkillManager.plan
    fired = {"n": 0}

    def _explicit_add_first(self, *args, **kwargs):
        # The user's own `sfs skill add` lands between pull's decision and
        # its install (same thread: the locks are re-entrant).
        if fired["n"] == 0:
            fired["n"] += 1
            self.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
        return real_plan(self, *args, **kwargs)

    monkeypatch.setattr(SkillManager, "plan", _explicit_add_first)
    hosted.pull(manager, market.client())
    monkeypatch.setattr(SkillManager, "plan", real_plan)
    assert fired["n"] == 1
    assert _roles(manager) == [(market.assignments[0]["id"], "reference")]
    # So pull can never uninstall the user's own installation.
    market.assignments.clear()
    report = hosted.pull(manager, market.client())
    assert report.unassigned == []
    assert _skill_file(home).is_file()


def test_install_and_origin_recording_hold_the_operation_lock_together(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    import threading
    import time

    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    market.assign(item)
    events: list[str] = []
    real_record = hosted._record_origins
    threads: list[threading.Thread] = []

    def _concurrent_explicit_change():
        other = SkillManager(
            project_root=manager.project_root, home_dir=manager.home_dir,
            store_root=manager.store_root, hosted_client=market.client(),
        )
        with other.explicit_change("user"):
            events.append("explicit")

    def _record(*args, **kwargs):
        if not threads:
            thread = threading.Thread(target=_concurrent_explicit_change)
            threads.append(thread)
            thread.start()
            time.sleep(0.3)  # it must be waiting on the locks pull holds
            events.append("record")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(hosted, "_record_origins", _record)
    hosted.pull(manager, market.client())
    threads[0].join(timeout=10)
    assert events == ["record", "explicit"]
    assert _roles(manager) == [(market.assignments[0]["id"], "installer")]


# ---------------------------------------------------------------------------
# round 13: atomic no-replace placement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("native", [True, False])
def test_rename_noreplace_never_replaces(tmp_path: Path, monkeypatch, native: bool):
    if not native:  # the hard-link fallback
        monkeypatch.setattr(state_module, "_NATIVE_NOREPLACE", False)
    source, destination = tmp_path / "a", tmp_path / "b"
    source.write_text("ours")
    destination.write_text("theirs")
    with pytest.raises(FileExistsError):
        state_module.rename_noreplace(source, destination)
    assert source.read_text() == "ours" and destination.read_text() == "theirs"
    destination.unlink()
    inode = source.stat().st_ino
    state_module.rename_noreplace(source, destination)
    assert not source.exists() and destination.stat().st_ino == inode


def test_rename_noreplace_refuses_where_no_atomic_primitive_exists(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(state_module, "_NATIVE_NOREPLACE", False)

    def _no_links(*args, **kwargs):
        raise OSError(errno.EPERM, "hard links not supported")

    monkeypatch.setattr(state_module.os, "link", _no_links)
    source = tmp_path / "a"
    source.write_text("ours")
    with pytest.raises(SkillError, match="without risking a replace"):
        state_module.rename_noreplace(source, tmp_path / "b")
    assert source.read_text() == "ours"
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(SkillError):
        state_module.rename_noreplace(link, tmp_path / "c")  # never followed or replaced


@pytest.mark.parametrize("native", [True, False])
def test_put_back_claims_a_sidecar_when_the_place_got_occupied(
    tmp_path: Path, monkeypatch, native: bool
):
    from sessionfs.skills.state import SkillStateStore

    if not native:
        monkeypatch.setattr(state_module, "_NATIVE_NOREPLACE", False)
    moved = tmp_path / "moved"
    moved.write_text("managed")
    destination = tmp_path / "out" / "SKILL.md"
    destination.parent.mkdir()
    real = state_module.rename_noreplace
    calls = {"n": 0}

    def _racy(source, target):
        calls["n"] += 1
        if calls["n"] == 1:
            Path(target).write_text("appeared just now")  # wins the race
        return real(source, target)

    monkeypatch.setattr(state_module, "rename_noreplace", _racy)
    SkillStateStore._put_back(moved, destination)
    assert destination.read_text() == "appeared just now"
    [sidecar] = [p for p in destination.parent.iterdir() if ".sfs-restored-" in p.name]
    assert sidecar.read_text() == "managed" and not moved.exists()


def test_write_without_hard_links_creates_the_name_exclusively(tmp_path: Path, monkeypatch):
    from sessionfs.skills.state import SkillStateStore

    monkeypatch.setattr(state_module, "_NATIVE_NOREPLACE", False)

    def _no_links(*args, **kwargs):
        raise OSError(errno.EPERM, "hard links not supported")

    monkeypatch.setattr(state_module.os, "link", _no_links)
    store = SkillStateStore(tmp_path / "state")
    source = tmp_path / "src.md"
    source.write_text("new content")
    destination = tmp_path / "out" / "SKILL.md"
    store._write_output(source, destination)
    assert destination.read_text() == "new content"
    assert [p.name for p in destination.parent.iterdir()] == ["SKILL.md"]  # no temp left
    destination.write_text("user file")
    with pytest.raises(SkillError, match="not overwritten"):
        store._write_output(source, destination)
    assert destination.read_text() == "user file"


# ---------------------------------------------------------------------------
# round 15: version/package events match by id; hashes only for content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["yanked", "blocked"])
def test_a_version_event_never_reaches_other_versions_with_the_same_bytes(
    tmp_path: Path, market: FakeMarketplace, state: str
):
    a = market.publish("shared", "1.0.0")
    b = market.publish("shared", "1.0.0", namespace="beta")  # same bytes, other version
    assert a["content_hash"] == b["content_hash"] and a["version_id"] != b["version_id"]
    manager, _, _ = _manager(tmp_path, market)
    market.revoke(a, state, "withdrawn", kind="version", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    # B, and a local/Git package with identical bytes, still install.
    hosted.assert_install_allowed(
        manager.store_root, content_hash=b["content_hash"], version_id=b["version_id"],
        package_id=b["package_id"], fingerprint=fp,
    )
    hosted.assert_install_allowed(
        manager.store_root, content_hash=a["content_hash"], version_id=None, fingerprint=fp,
    )
    manager.add(f"sessionfs:{b['package_id']}", targets=["codex"], scope="user")
    assert hosted.revocation_warnings(manager) == []
    # A itself is refused.
    with pytest.raises(SkillError):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=a["content_hash"], version_id=a["version_id"],
            package_id=a["package_id"], fingerprint=fp,
        )


def test_a_content_block_reaches_every_copy_of_the_bytes(
    tmp_path: Path, market: FakeMarketplace
):
    a = market.publish("shared", "1.0.0")
    b = market.publish("shared", "1.0.0", namespace="beta")
    manager, _, _ = _manager(tmp_path, market)
    market.revoke(a, "blocked", "malware", kind="content", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    for version_id in (b["version_id"], None):
        with pytest.raises(SkillError, match="BLOCKED"):
            hosted.assert_install_allowed(
                manager.store_root, content_hash=b["content_hash"], version_id=version_id,
                fingerprint=fp,
            )


def test_an_old_server_block_without_kind_still_matches_by_hash_but_a_yank_does_not(
    tmp_path: Path, market: FakeMarketplace
):
    a = market.publish("shared", "1.0.0")
    b = market.publish("shared", "1.0.0", namespace="beta")
    manager, _, _ = _manager(tmp_path, market)
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    market.revoke(a, "yanked", "old yank")  # older server: no kind field
    hosted.sync_revocations(market.client(), manager.store_root)
    hosted.assert_install_allowed(
        manager.store_root, content_hash=b["content_hash"], version_id=b["version_id"],
        fingerprint=fp,
    )
    market.revoke(a, "blocked", "old block")  # older server: no kind field
    hosted.sync_revocations(market.client(), manager.store_root)
    with pytest.raises(SkillError, match="BLOCKED"):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=b["content_hash"], version_id=b["version_id"],
            fingerprint=fp,
        )


# ---------------------------------------------------------------------------
# round 16: another credential's version block stays scoped to its id
# ---------------------------------------------------------------------------


def _twin_versions(market: FakeMarketplace) -> tuple[dict, dict]:
    a = market.publish("shared", "1.0.0")
    b = market.publish("shared", "1.0.0", namespace="beta")  # same bytes, other version
    assert a["content_hash"] == b["content_hash"]
    return a, b


def test_another_credentials_version_block_never_goes_hash_wide(
    tmp_path: Path, market: FakeMarketplace
):
    a, b = _twin_versions(market)
    manager, _, _ = _manager(tmp_path, market)
    # Only Bob can see the explicit VERSION block of A.
    market.revoke(a, "blocked", "bob only", visible_to={"sk_other"}, kind="version",
                  block_scope="global")
    hosted.sync_revocations(market.client("sk_other"), manager.store_root)
    hosted.sync_revocations(market.client("sk_test"), manager.store_root)
    alice = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    bob = hosted.identity_fingerprint("https://sfs.test", "sk_other")
    # Bob: A is blocked (his own visibility-scoped namespace) ...
    with pytest.raises(SkillError, match="BLOCKED"):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=a["content_hash"], version_id=a["version_id"],
            fingerprint=bob,
        )
    # ... a target event is never shared device-wide, and never widened to
    # B or a local package with the same bytes.
    assert HostedState(manager.store_root).read()["global_content"].get("https://sfs.test", {}) == {}
    hosted.assert_install_allowed(
        manager.store_root, content_hash=a["content_hash"], version_id=a["version_id"],
        fingerprint=alice,
    )
    hosted.assert_install_allowed(
        manager.store_root, content_hash=b["content_hash"], version_id=b["version_id"],
        fingerprint=alice,
    )
    hosted.assert_install_allowed(
        manager.store_root, content_hash=a["content_hash"], version_id=None, fingerprint=alice,
    )


def test_another_credentials_content_and_legacy_blocks_still_match_by_hash(
    tmp_path: Path, market: FakeMarketplace
):
    a, b = _twin_versions(market)
    manager, _, _ = _manager(tmp_path, market)
    alice = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    bob = market.client("sk_other")
    # A yank Bob can see never reaches Alice at all.
    market.revoke(a, "yanked", "bob yank", visible_to={"sk_other"})
    hosted.sync_revocations(bob, manager.store_root)
    hosted.sync_revocations(market.client("sk_test"), manager.store_root)
    hosted.assert_install_allowed(
        manager.store_root, content_hash=a["content_hash"], version_id=a["version_id"],
        fingerprint=alice,
    )
    # A kindless (older server) global BLOCK in the shared table: hash-wide
    # for Alice too.
    with HostedState(manager.store_root).edit() as state:
        state["global_content"]["https://sfs.test"] = {
            a["version_id"]: {
                "state": "blocked", "changed_at": "2026-10-15T00:00:00+00:00",
                "content_hash": a["content_hash"], "kind": "version", "kind_inferred": True,
                "version_id": a["version_id"], "package_id": a["package_id"],
            },
        }
    with pytest.raises(SkillError, match="BLOCKED"):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=b["content_hash"], version_id=b["version_id"],
            fingerprint=alice, origin="https://sfs.test",
        )


def test_a_content_block_from_another_credential_is_hash_wide(
    tmp_path: Path, market: FakeMarketplace
):
    a, b = _twin_versions(market)
    manager, _, _ = _manager(tmp_path, market)
    market.revoke(a, "blocked", "bad bytes", visible_to={"sk_other"}, kind="content",
                  block_scope="global")
    hosted.sync_revocations(market.client("sk_other"), manager.store_root)
    alice = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    hosted.sync_revocations(market.client("sk_test"), manager.store_root)
    with pytest.raises(SkillError, match="BLOCKED"):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=b["content_hash"], version_id=None,
            fingerprint=alice,
        )


def test_target_entries_written_by_earlier_builds_are_dropped_on_load(
    tmp_path: Path, market: FakeMarketplace
):
    a, b = _twin_versions(market)
    manager, _, _ = _manager(tmp_path, market)
    with HostedState(manager.store_root).edit() as state:
        # Written by earlier builds: target (version / package) entries,
        # with and without a kind, next to a content entry.
        state["global_content"]["https://sfs.test"] = {
            a["version_id"]: {"state": "blocked", "changed_at": "2026-10-15T00:00:00+00:00",
                              "content_hash": a["content_hash"]},
            b["version_id"]: {"state": "blocked", "changed_at": "2026-10-15T00:00:00+00:00",
                              "content_hash": b["content_hash"], "kind": "version",
                              "kind_inferred": False, "version_id": b["version_id"]},
            "content:sha256:" + "f" * 64: {"state": "blocked", "changed_at": "x",
                                          "content_hash": "sha256:" + "f" * 64},
        }
    table = HostedState(manager.store_root).read()["global_content"]["https://sfs.test"]
    assert list(table) == ["content:sha256:" + "f" * 64]
    alice = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    hosted.assert_install_allowed(
        manager.store_root, content_hash=a["content_hash"], version_id=a["version_id"],
        fingerprint=alice, origin="https://sfs.test",
    )
    hosted.assert_install_allowed(
        manager.store_root, content_hash=b["content_hash"], version_id=b["version_id"],
        fingerprint=alice, origin="https://sfs.test",
    )


# ---------------------------------------------------------------------------
# round 16: paged grant listings
# ---------------------------------------------------------------------------


def _many_rows(market: FakeMarketplace, item: dict, count: int) -> None:
    for _ in range(count):
        market.assign(item)


def test_pull_pages_through_every_grant_before_reporting_unassigned(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch, "proj_1")
    filler = market.publish("lint-helper", "1.0.0")
    keep = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.page_size = 50
    _many_rows(market, filler, 149)
    last = market.assign(keep)  # on the third page
    report = hosted.pull(manager, market.client())
    assert market.grant_page_calls >= 3 and report.complete_sources["assignment"] is True
    assert _skill_file(home).is_file()  # found only on the last page
    market.assignments.remove(last)
    real = market.handler

    def _third_page_fails(request: httpx.Request) -> httpx.Response:
        if (
            request.url.path == "/api/v1/users/me/skills/assignments"
            and request.url.params.get("cursor") == "100"
        ):
            return httpx.Response(503, json={"error": {"code": "503", "message": "down"}})
        return real(request)

    # A page that cannot be read: the source is incomplete, nothing is removed.
    market.handler = _third_page_fails  # type: ignore[method-assign]
    report = hosted.pull(manager, market.client())
    assert report.complete_sources["assignment"] is False
    assert _skill_file(home).is_file()
    # A page budget too small to reach the end: incomplete as well.
    market.handler = real  # type: ignore[method-assign]
    monkeypatch.setattr(hosted, "MAX_GRANT_PAGES", 2)
    report = hosted.pull(manager, market.client())
    assert report.complete_sources["assignment"] is False
    assert _skill_file(home).is_file()
    assert report.unassigned == []
    # Every page read: only now is the package reported as no longer assigned.
    monkeypatch.setattr(hosted, "MAX_GRANT_PAGES", 200)
    report = hosted.pull(manager, market.client())
    assert report.complete_sources["assignment"] is True
    assert [c["name"] for c in report.unassigned] == ["review-loop"]
    assert _skill_file(home).is_file()


def test_a_cursor_that_does_not_advance_marks_the_listing_truncated(market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    market.page_size = 1
    _many_rows(market, item, 3)
    market.listing_extra["assignment"] = {"has_more": True, "next_cursor": "same"}
    listing = market.client().assignments()
    assert listing.truncated is True


@pytest.mark.parametrize("rows, complete", [(hosted.LEGACY_GRANT_LIST_CAP, False), (99, True)])
def test_an_older_server_listing_at_its_cap_is_treated_as_truncated(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, rows: int, complete: bool
):
    _no_project(monkeypatch, "proj_1")
    filler = market.publish("lint-helper", "1.0.0")
    keep = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(keep)
    hosted.pull(manager, market.client())
    market.assignments.remove(first)
    _many_rows(market, filler, rows)  # unpaged: no has_more (older server)
    report = hosted.pull(manager, market.client())
    assert report.complete_sources["assignment"] is complete
    assert [c["name"] for c in report.unassigned] == (["review-loop"] if complete else [])
    assert _skill_file(home).is_file()  # never uninstalled


def test_grant_paging_contract(market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    market.page_size = 2
    _many_rows(market, item, 5)
    seen: list[dict[str, str]] = []
    real = market.handler

    def _spy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/me/skills/assignments":
            seen.append(dict(request.url.params))
            if request.url.params.get("cursor") == "2":
                # A short page that is NOT the end, flagged incomplete.
                body = {"items": market.assignments[2:3], "incomplete": True, "omitted_count": 1,
                        "has_more": True, "next_cursor": "4"}
                return httpx.Response(200, json=body)
        return real(request)

    market.handler = _spy  # type: ignore[method-assign]
    listing = market.client().assignments()
    # Limit sent on every page; the cursor is the previous page's next_cursor.
    assert [params.get("cursor") for params in seen] == [None, "2", "4"]
    assert all(params["limit"] == str(hosted.GRANT_PAGE_LIMIT) for params in seen)
    assert len(listing) == 4 and listing.truncated is False
    assert (listing.server_incomplete, listing.omitted_count) == (True, 1)


def test_an_invalid_cursor_is_an_error_never_a_silent_restart(market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    market.page_size = 2
    _many_rows(market, item, 5)
    real = market.handler

    def _reject(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor"):
            return httpx.Response(422, json={"error": {"code": "invalid_cursor", "message": "bad"}})
        return real(request)

    market.handler = _reject  # type: ignore[method-assign]
    with pytest.raises(SkillError):
        market.client().assignments()


# ---------------------------------------------------------------------------
# round 17: path safety, interruptible paging, listing changes, absence belt
# ---------------------------------------------------------------------------


def _recording_client(responses: dict[str, httpx.Response] | None = None):
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.raw_path.decode())
        return (responses or {}).get(request.url.path, httpx.Response(404, json={}))

    return HostedClient("https://sfs.test", "sk_test", transport=httpx.MockTransport(handler)), sent


@pytest.mark.parametrize("remote", [
    "../admin/users", "acme/../../admin/users", "acme/%2e%2e/admin", "acme/..%2fadmin",
    "acme\\..\\admin", "acme/repo?x=1", "acme/repo#frag", "acme/re\x00po",
])
def test_the_legacy_project_lookup_never_sends_a_traversal(remote: str):
    client, sent = _recording_client()
    with pytest.raises(SkillError):
        client._legacy_project_id_for_remote(remote)
    assert sent == []


def test_the_legacy_project_lookup_encodes_each_segment():
    ok = httpx.Response(200, json={"id": "proj_1"})
    client, sent = _recording_client({"/api/v1/projects/github.com/acme/re+po@x": ok})
    assert client._legacy_project_id_for_remote("github.com/acme/re+po@x") == "proj_1"
    assert sent == ["/api/v1/projects/github.com/acme/re%2Bpo%40x"]


@pytest.mark.parametrize("path", [
    "/api/v1/../admin/users", "/api/v1/projects/%2e%2e/x", "/api/v1/a%2fb", "/admin",
    "/api/v1/x?y", "/api/v1/x#y", "/api/v1/x\\y", "/api/v1//x",
])
def test_no_request_reaches_an_unsafe_path(path: str):
    client, sent = _recording_client()
    with pytest.raises(SkillError, match="unsafe API path"):
        client._request(path)
    assert sent == []


@pytest.mark.parametrize("bad", ["../x", "spk_../admin", "skv_%2e%2e", "proj/../x", "a?b"])
def test_ids_from_anywhere_are_validated_before_any_request(bad: str):
    client, sent = _recording_client()
    for call in (
        lambda: client.get_package(bad),
        lambda: client.version_metadata(bad),
        lambda: client.download(bad),
        lambda: client.approved_install_requests(bad),
    ):
        with pytest.raises(SkillError):
            call()
    assert sent == []


def test_paging_stops_at_the_deadline_or_a_cancel(market: FakeMarketplace, monkeypatch):
    item = market.publish("review-loop", "1.0.0")
    market.page_size = 1
    _many_rows(market, item, 4)
    calls = {"n": 0}
    listing = market.client().assignments(cancel=lambda: calls.__setitem__("n", calls["n"] + 1)
                                          or calls["n"] > 2)
    assert listing.truncated and listing.interrupted and len(listing) == 2
    import time as time_module

    timeouts: list[float | None] = []
    real = HostedClient._request

    def _spy(self, path, params=None, *, timeout=None):
        timeouts.append(timeout)
        return real(self, path, params, timeout=timeout)

    monkeypatch.setattr(HostedClient, "_request", _spy)
    deadline = time_module.monotonic() + 5.0
    listing = market.client().assignments(deadline=deadline)
    assert not listing.truncated and len(listing) == 4
    assert timeouts and all(t is not None and 0 < t <= 5.0 for t in timeouts)
    listing = market.client().assignments(deadline=time_module.monotonic() - 1)
    assert listing.interrupted and list(listing) == []


def test_an_interrupted_listing_never_reports_unassigned(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch, "proj_1")
    keep = market.publish("review-loop", "1.0.0")
    filler = market.publish("lint-helper", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(keep)
    hosted.pull(manager, market.client())
    market.assignments.remove(first)
    market.page_size = 1
    _many_rows(market, filler, 3)
    stop = {"pages": 0}
    real = market.handler

    def _count(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/users/me/skills/assignments":
            stop["pages"] += 1
        return real(request)

    market.handler = _count  # type: ignore[method-assign]
    for _pull in range(3):
        stop["pages"] = 0
        report = hosted.pull(
            manager, market.client(),
            cancel=lambda: stop["pages"] >= 2,
        )
        assert report.complete_sources["assignment"] is False
    assert _skill_file(home).is_file()


def _generational(market: FakeMarketplace, generations: list[Any]) -> None:
    """Serve assignment pages whose `generation` follows `generations`
    (one entry per page request)."""
    real = market.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = real(request)
        if request.url.path != "/api/v1/users/me/skills/assignments":
            return response
        body = response.json()
        generation = generations.pop(0) if generations else "stable"
        if generation == 409:
            return httpx.Response(409, json={"error": {"code": "listing_changed", "message": "m"}})
        body["generation"] = generation
        return httpx.Response(200, json=body)

    market.handler = handler  # type: ignore[method-assign]


def test_a_listing_that_changes_while_paging_is_read_again(market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    market.page_size = 2
    _many_rows(market, item, 4)  # two pages
    _generational(market, ["g1", "g2", "g2", "g2"])  # changed under the cursor once
    listing = market.client().assignments()
    assert not listing.truncated and len(listing) == 4
    _generational(market, ["g1", 409, "g2", "g2"])  # the server's 409
    listing = market.client().assignments()
    assert not listing.truncated and len(listing) == 4


def test_a_listing_that_keeps_changing_is_incomplete(market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    market.page_size = 2
    _many_rows(market, item, 4)
    _generational(market, ["a", "b", "c", "d", "e", "f", "g", "h"])
    listing = market.client().assignments()
    assert listing.truncated and list(listing) == []


# ---------------------------------------------------------------------------
# round 18: a failed cross-filesystem copy never strands an output
# ---------------------------------------------------------------------------


def _blocked_install(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    market.revoke(item, "blocked", "malware", kind="content", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)
    return manager, home


def _cross_fs_without_space(monkeypatch, *, restore_fails: bool = False) -> None:
    """Renames into the transaction dir fail like EXDEV; copying into it
    fails with ENOSPC; optionally the first put-back rename fails too."""
    real_rename, real_copy = state_module._rename_entry, state_module.copy_backup
    real_noreplace = state_module.rename_noreplace
    failures = {"restore": 1 if restore_fails else 0}

    def _rename(src, dst):
        if ".skills-txn-" in str(dst) and ".skills-txn-" not in str(src):
            raise OSError(errno.EXDEV, "cross-device link")
        return real_rename(src, dst)

    def _copy(src, dst):
        if ".skills-txn-" in str(dst) and "moved" in Path(dst).parts:
            Path(dst).write_bytes(b"partial")  # a partial copy is left behind
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_copy(src, dst)

    def _noreplace(src, dst):
        if ".sfs-removing-" in Path(src).name and failures["restore"]:
            failures["restore"] -= 1
            raise OSError(errno.EIO, "I/O error")
        return real_noreplace(src, dst)

    monkeypatch.setattr(state_module, "_rename_entry", _rename)
    monkeypatch.setattr(state_module, "copy_backup", _copy)
    monkeypatch.setattr(state_module, "rename_noreplace", _noreplace)


def _staged_leftovers(home: Path) -> list[Path]:
    return [p for p in (home / ".codex").rglob("*") if ".sfs-removing-" in p.name]


def test_a_failed_copy_puts_the_output_back_before_quarantine_goes_on(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, home = _blocked_install(tmp_path, market)
    skill = _skill_file(home)
    inode = skill.stat().st_ino
    _cross_fs_without_space(monkeypatch)
    [warning] = hosted.enforce_blocked(manager)
    assert warning["failed"] == [str(skill)] and "still active" in warning["error"]
    # The original is back at its path (same inode), nothing is stranded,
    # and the state record is kept so the removal is retried.
    assert skill.stat().st_ino == inode
    assert _staged_leftovers(home) == []
    assert [row["name"] for row in manager.list_installed("user")] == ["review-loop"]
    assert not list(manager.store_root.glob(".skills-txn-*"))


def test_when_the_put_back_fails_the_whole_transaction_is_restored(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, home = _blocked_install(tmp_path, market)
    skill = _skill_file(home)
    inode = skill.stat().st_ino
    _cross_fs_without_space(monkeypatch, restore_fails=True)
    [warning] = hosted.enforce_blocked(manager)
    assert "aborting" in warning["error"]
    assert skill.stat().st_ino == inode  # recovery restored the staged entry
    assert _staged_leftovers(home) == []
    assert [row["name"] for row in manager.list_installed("user")] == ["review-loop"]


def test_a_normal_removal_with_a_failed_copy_changes_nothing(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    _cross_fs_without_space(monkeypatch)
    with pytest.raises(SkillError, match="No space left"):
        manager.remove(package.package_id, scope="user")
    assert _skill_file(home).is_file() and _staged_leftovers(home) == []
    assert [row["name"] for row in manager.list_installed("user")] == ["review-loop"]


def test_doctor_reports_a_stranded_removal_copy(tmp_path: Path, market: FakeMarketplace):
    market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    stranded = _skill_file(home).with_name(".SKILL.md.sfs-removing-0123456789ab")
    stranded.write_text("orphan")
    findings = manager.doctor("user", repair_cache=False)
    assert [(f["path"], f["actual"]) for f in findings] == [
        (str(stranded), "stranded-removal-copy")]
    assert "SKILL.md" in findings[0]["expected"]


# ---------------------------------------------------------------------------
# round 19: target events stay identity-scoped; opt-out keeps roles
# ---------------------------------------------------------------------------


def test_leaving_the_org_never_leaves_a_platform_target_block_stuck(
    tmp_path: Path, market: FakeMarketplace
):
    item = market.publish("shared", "1.0.0", owner_org_id="org_a", visibility="org")
    manager, _, _ = _manager(tmp_path, market)
    alice, bob = market.client("sk_test"), market.client("sk_other")
    # Platform block of the PRIVATE package: its version event is visible to
    # org members only; the content block is public.
    market.revoke(item, "blocked", "platform takedown", kind="version", block_scope="global",
                  visible_to={"sk_test"})
    market.revoke(item, "blocked", "bad bytes", kind="content", block_scope="global")
    hosted.sync_revocations(alice, manager.store_root)
    hosted.sync_revocations(bob, manager.store_root)
    fp = {key: hosted.identity_fingerprint("https://sfs.test", key) for key in ("sk_test", "sk_other")}

    def _allowed(key: str) -> bool:
        try:
            hosted.assert_install_allowed(
                manager.store_root, content_hash=item["content_hash"],
                version_id=item["version_id"], fingerprint=fp[key],
            )
        except SkillError:
            return False
        return True

    assert not _allowed("sk_test") and not _allowed("sk_other")
    # Alice leaves the org: the version events (including its restore) are
    # no longer visible to her; the public content restore is.
    market.events = [
        (at, event, {"sk_nobody"} if event.get("kind") == "version" else visible)
        for at, event, visible in market.events
    ]
    market.revoke(item, "restored", kind="version", block_scope="global",
                  visible_to={"sk_nobody"})
    market.revoke(item, "restored", kind="content", block_scope="global")
    hosted.sync_revocations(alice, manager.store_root, force_full=True)
    hosted.sync_revocations(bob, manager.store_root, force_full=True)
    assert _allowed("sk_test") and _allowed("sk_other")  # installable again


def test_a_failed_opt_out_removal_keeps_installer_roles(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item, mode="optional")
    hosted.pull(manager, market.client(), install_optional=lambda i: True)
    roles_before = _roles(manager)
    assert roles_before == [(row["id"], "installer")]
    package_id = manager.list_installed("user")[0]["package_id"]

    def _no_space(self, *args, **kwargs):
        raise SkillError("No space left on device")

    with manager.explicit_change("user"):
        keys = hosted.begin_opt_out(manager, "user", package_id)
        assert _roles(manager) == roles_before  # not demoted before the removal
        monkeypatch.setattr(SkillManager, "remove", _no_space)
        with pytest.raises(SkillError):
            manager.remove(package_id, scope="user")
        hosted.finish_opt_out(manager, "user", keys, removed=False)
    monkeypatch.undo()
    _no_project(monkeypatch)
    assert _roles(manager) == roles_before
    assert HostedState(manager.store_root).read()["opt_outs"] == {}
    # The kept installer role still makes a later unassignment reportable.
    market.assignments.remove(row)
    report = hosted.pull(manager, market.client())
    assert [c["name"] for c in report.unassigned] == ["review-loop"]
    assert _skill_file(home).exists()


def test_a_successful_opt_out_removal_drops_the_installer_role(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    market.assign(item, mode="optional")
    hosted.pull(manager, market.client(), install_optional=lambda i: True)
    package_id = manager.list_installed("user")[0]["package_id"]
    with manager.explicit_change("user"):
        keys = hosted.begin_opt_out(manager, "user", package_id)
        manager.remove(package_id, scope="user")
        hosted.finish_opt_out(manager, "user", keys, removed=True)
    assert all(role != "installer" for _origin, role in _roles(manager))


# ---------------------------------------------------------------------------
# round 20: a partial quarantine survives `sfs skill sync`
# ---------------------------------------------------------------------------


def test_sync_never_finishes_a_partial_quarantine_as_a_removal(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, home = _blocked_install(tmp_path, market)
    skill = _skill_file(home)
    skill.write_text("locally edited blocked content")  # dirty AND blocked
    _cross_fs_without_space(monkeypatch)
    [warning] = hosted.enforce_blocked(manager)
    assert warning["failed"] == [str(skill)]
    monkeypatch.undo()
    lock = json.loads((manager.store_root / "skills.lock.json").read_text())
    [lock_row] = lock["packages"].values()
    assert lock_row["quarantine_pending"] is True
    # `sfs skill sync` neither reinstalls nor "removes" it ...
    manager.sync(scope="user")
    [row] = manager.list_installed("user")
    assert row["quarantine_pending"] is True and row["outputs"]
    assert skill.read_text() == "locally edited blocked content"
    # ... a normal remove refuses ...
    with pytest.raises(SkillError, match="quarantine is not finished"):
        manager.remove(row["package_id"], scope="user")
    # ... warnings and doctor still see it ...
    assert [w["state"] for w in hosted.revocation_warnings(manager)] == ["blocked"]
    # ... and the next quarantine pass finishes it: the file leaves the tool
    # directory, then the installed and desired state are dropped.
    [warning] = hosted.enforce_blocked(manager)
    assert not skill.exists() and warning["failed"] == []
    assert manager.list_installed("user") == []
    lock = json.loads((manager.store_root / "skills.lock.json").read_text())
    assert lock["packages"] == {}


# ---------------------------------------------------------------------------
# round 20: an intermediate directory swapped for a symlink never redirects
# ---------------------------------------------------------------------------


def _swap_for_symlink(directory: Path, outside: Path) -> None:
    """Replace `directory` by a symlink to `outside` (after validation)."""
    directory.rename(directory.with_name(directory.name + ".real"))
    directory.symlink_to(outside, target_is_directory=True)


def test_an_install_never_writes_through_a_swapped_directory(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills.state import SkillStateStore

    market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".codex" / "skills").mkdir(parents=True)  # passes validation
    real = SkillStateStore._write_output

    def _swap_then_write(self, source, destination, **kwargs):
        skills = home / ".codex" / "skills"
        if not skills.is_symlink():
            _swap_for_symlink(skills, outside)
        return real(self, source, destination, **kwargs)

    monkeypatch.setattr(SkillStateStore, "_write_output", _swap_then_write)
    with pytest.raises(SkillError, match="symlink"):
        manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    assert list(outside.rglob("*")) == []  # nothing created outside the root
    assert manager.list_installed("user") == []


def test_a_removal_never_moves_through_a_swapped_directory(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills.state import SkillStateStore

    market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    outside = tmp_path / "outside"
    (outside / "review-loop").mkdir(parents=True)
    victim = outside / "review-loop" / "SKILL.md"
    victim.write_text(_skill_file(home).read_text())  # identical bytes elsewhere
    real = SkillStateStore._move_out

    def _swap_then_move(self, path, action):
        skills = home / ".codex" / "skills"
        if not skills.is_symlink():
            _swap_for_symlink(skills, outside)
        return real(self, path, action)

    monkeypatch.setattr(SkillStateStore, "_move_out", _swap_then_move)
    with pytest.raises(SkillError, match="symlink"):
        manager.remove(package.package_id, scope="user")
    assert victim.read_text().startswith("---")  # untouched, not moved to trash
    assert _trashed_files(manager) == []
    assert [row["name"] for row in manager.list_installed("user")] == ["review-loop"]


def test_platforms_without_directory_descriptors_fail_closed(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    monkeypatch.setattr(state_module, "_DIR_FD_SUPPORTED", False)
    with pytest.raises(SkillError, match="cannot open directories"):
        manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    assert not (home / ".codex" / "skills" / "review-loop").exists()


# ---------------------------------------------------------------------------
# round 21: no auto-uninstall; incomplete feeds keep known blocks; legacy
# kindless blocks are hash-wide device-wide
# ---------------------------------------------------------------------------


def test_the_daemon_never_uninstalls_an_unassigned_package(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.daemon_reconcile(manager, market.client())
    assert _skill_file(home).is_file()
    market.assignments.remove(row)
    for _run in range(3):
        report = hosted.daemon_reconcile(manager, market.client())
    assert [c["name"] for c in report.unassigned] == ["review-loop"]
    assert _skill_file(home).is_file()
    assert [r["name"] for r in manager.list_installed("user")] == ["review-loop"]


def test_doctor_lists_packages_that_are_no_longer_assigned(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    row = market.assign(item)
    _cli_env(tmp_path, monkeypatch, market.client())
    assert runner.invoke(skill_app, ["pull"]).exit_code == 0
    market.assignments.remove(row)
    pulled = runner.invoke(skill_app, ["pull", "--yes"])
    assert "no longer assigned" in pulled.output and "sfs skill remove" in pulled.output
    doctor = runner.invoke(skill_app, ["doctor"])
    assert "review-loop (user) is no longer assigned" in doctor.output
    assert (Path.home() / ".codex" / "skills" / "review-loop").is_dir()


def test_an_incomplete_revocation_sync_still_enforces_known_blocks(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.pull(manager, market.client())
    market.revoke(item, "blocked", "malware", kind="version", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)  # the block is known
    market.assignments.remove(row)
    monkeypatch.setattr(
        hosted, "sync_revocations",
        lambda *a, **k: {"supported": True, "complete": False, "changed": []},
    )
    notes: list[str] = []
    report = hosted.pull(manager, market.client(), notify=notes.append)
    assert any("previously known blocks" in note for note in notes)
    # Quarantined (not a plain uninstall), and its state is reported as such.
    assert [w["name"] for w in report.blocked_removed] == ["review-loop"]
    assert report.blocked_removed[0]["quarantine_dir"]
    assert not _skill_file(home).exists()


def test_a_legacy_kindless_block_without_scope_is_hash_wide_device_wide(
    tmp_path: Path, market: FakeMarketplace
):
    a = market.publish("shared", "1.0.0")
    b = market.publish("shared", "1.0.0", namespace="beta")  # same bytes
    manager, _, _ = _manager(tmp_path, market)
    market.revoke(a, "yanked", "old yank", visible_to={"sk_other"})  # no kind, no scope
    market.revoke(a, "blocked", "old block", visible_to={"sk_other"})
    hosted.sync_revocations(market.client("sk_other"), manager.store_root)
    hosted.sync_revocations(market.client("sk_test"), manager.store_root)
    table = HostedState(manager.store_root).read()["global_content"]["https://sfs.test"]
    assert [entry["state"] for entry in table.values()] == ["blocked"]  # the yank is not shared
    alice = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    with pytest.raises(SkillError, match="BLOCKED"):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=b["content_hash"], version_id=b["version_id"],
            fingerprint=alice,
        )
    # Its kindless restore lifts it device-wide again.
    market.revoke(a, "restored", "", visible_to={"sk_other"})
    hosted.sync_revocations(market.client("sk_other"), manager.store_root)
    hosted.assert_install_allowed(
        manager.store_root, content_hash=b["content_hash"], version_id=b["version_id"],
        fingerprint=alice,
    )


def test_a_legacy_restore_seen_by_another_identity_lifts_a_stale_kindless_block(
    tmp_path: Path, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    alice_fp = hosted.identity_fingerprint("https://sfs.test", "sk_test")
    # Alice sees an older server's kindless, scopeless block ...
    market.revoke(item, "blocked", "old block", visible_to={"sk_test"})
    hosted.sync_revocations(market.client("sk_test"), manager.store_root)
    [flag] = HostedState(manager.store_root).read()["namespaces"]["https://sfs.test|usr_me"][
        "revoked"].values()
    assert flag["block_scope"] == "global" and flag["kind_inferred"] is True
    with pytest.raises(SkillError, match="BLOCKED"):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=item["content_hash"],
            version_id=item["version_id"], fingerprint=alice_fp,
        )
    # ... then loses visibility; only Bob sees the kindless restore.
    market.events = [(at, event, {"sk_nobody"}) for at, event, _ in market.events]
    market.revoke(item, "restored", "", visible_to={"sk_other"})
    hosted.sync_revocations(market.client("sk_other"), manager.store_root)
    # Back to Alice (her namespace still holds the stale block): installable.
    hosted.assert_install_allowed(
        manager.store_root, content_hash=item["content_hash"],
        version_id=item["version_id"], fingerprint=alice_fp,
    )


def test_a_scopeless_kindless_flag_from_an_earlier_build_counts_as_global():
    flag = {"state": "blocked", "kind": "version", "kind_inferred": True, "block_scope": None,
            "version_id": "skv_" + "1" * 24}
    assert hosted._global_key(flag) == "skv_" + "1" * 24
    modern = {**flag, "kind_inferred": False}
    assert hosted._global_key(modern) is None  # explicit events need "global"


# ---------------------------------------------------------------------------
# round 23: a rotated API key keeps its user's known blocks
# ---------------------------------------------------------------------------


def _offline_client(key: str) -> HostedClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    return HostedClient("https://sfs.test", key, transport=httpx.MockTransport(handler))


def _blocked_after_rotation(tmp_path: Path, market: FakeMarketplace):
    """V installed and blocked for usr_me (synced with the old key), then the
    key is rotated: a NEW credential of the same user that never synced."""
    v1 = market.publish("review-loop", "1.0.0", body="one")
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    manager, _, home = _manager(tmp_path, market)
    package, _, _ = manager.add("sessionfs:acme/review-loop@1.0.0", targets=["codex"],
                                scope="user")
    manager.apply_update(manager.prepare_update(package.package_id, scope="user"))
    market.revoke(v1, "blocked", "platform block", kind="version", block_scope="global")
    hosted.sync_revocations(market.client("sk_test"), manager.store_root)
    market.users["sk_rotated"] = "usr_me"
    return v1, v2, manager, home


def test_an_online_rotated_key_is_registered_and_its_known_block_enforced(
    tmp_path: Path, market: FakeMarketplace
):
    v1, _v2, manager, _home = _blocked_after_rotation(tmp_path, market)
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_rotated")
    assert fp not in HostedState(manager.store_root).read()["identities"]
    manager2 = SkillManager(
        project_root=manager.project_root, home_dir=manager.home_dir,
        store_root=manager.store_root, hosted_client=market.client("sk_rotated"),
    )
    # Rollback to the blocked V (from the local cache) registers the new key
    # first (one identity lookup) and is then refused ...
    package_id = manager2.list_installed("user")[0]["package_id"]
    with pytest.raises(SkillError, match="BLOCKED"):
        manager2.rollback(package_id, v1["content_hash"], scope="user")
    assert HostedState(manager.store_root).read()["identities"][fp] == "https://sfs.test|usr_me"
    # ... and so is a cached reinstall of it.
    with pytest.raises(SkillError, match="BLOCKED"):
        hosted.assert_install_allowed(
            manager.store_root, content_hash=v1["content_hash"], version_id=v1["version_id"],
            fingerprint=fp, origin="https://sfs.test",
        )
    assert hosted.identity_unknown(manager2) is None


def test_an_offline_rotated_key_is_unknown_and_doctor_and_list_say_so(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    v1 = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    market.revoke(v1, "blocked", "platform block", kind="version", block_scope="global")
    hosted.sync_revocations(market.client("sk_test"), manager.store_root)
    rotated = SkillManager(
        project_root=manager.project_root, home_dir=manager.home_dir,
        store_root=manager.store_root, hosted_client=_offline_client("sk_rotated"),
    )
    # An unregistered credential cannot be classified offline (user and
    # service keys share one format): no namespace is guessed, so its
    # account's version block is not known locally ...
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_rotated")
    assert hosted._resolve_namespace(
        HostedState(manager.store_root).read(), fp, "https://sfs.test"
    ) is None
    assert hosted.revocation_warnings(rotated) == []
    # ... but doctor and list say the identity is unknown ...
    assert hosted.identity_unknown(rotated) == "https://sfs.test"
    _cli_env(tmp_path, monkeypatch, _offline_client("sk_rotated"))
    monkeypatch.setattr(SkillManager, "__init__", _bind_manager(manager, "sk_rotated"))
    for command in (["doctor"], ["list"]):
        output = runner.invoke(skill_app, command).output
        assert "Identity unknown for https://sfs.test" in output, command
    # ... and a fresh hosted download needs the server (which refuses a
    # blocked version) - offline it cannot happen at all.
    with pytest.raises(SkillError):
        rotated.add("sessionfs:acme/review-loop@1.0.0", targets=["claude-code"], scope="user")


def test_with_two_accounts_an_offline_rotated_key_never_gets_the_other_accounts_flags(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    a = market.publish("alpha", "1.0.0")
    b = market.publish("beta", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    market.revoke(a, "blocked", "alice's reason", kind="version", block_scope="global",
                  visible_to={"sk_test"})
    market.revoke(b, "yanked", "bob's reason", kind="version", block_scope="global",
                  visible_to={"sk_other"})
    hosted.sync_revocations(market.client("sk_test"), manager.store_root)   # Alice
    hosted.sync_revocations(market.client("sk_other"), manager.store_root)  # Bob syncs last
    # Alice rotates her key and works offline.
    rotated = SkillManager(
        project_root=manager.project_root, home_dir=manager.home_dir,
        store_root=manager.store_root, hosted_client=_offline_client("sk_alice_new"),
    )
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_alice_new")
    flags = hosted.effective_flags(
        HostedState(manager.store_root).read(), fp, "https://sfs.test"
    )
    assert all("bob" not in str(flag.get("reason")) for flag in flags)
    assert not any(flag.get("version_id") == b["version_id"] for flag in flags)
    # No guess is made; doctor says so.
    assert hosted.identity_unknown(rotated) == "https://sfs.test"
    _cli_env(tmp_path, monkeypatch, _offline_client("sk_alice_new"))
    monkeypatch.setattr(SkillManager, "__init__", _bind_manager(manager, "sk_alice_new"))
    result = runner.invoke(skill_app, ["doctor"])
    assert "Identity unknown for https://sfs.test" in result.output


def _bind_manager(manager: SkillManager, key: str):
    real_init = SkillManager.__init__

    def _init(self, **kwargs):
        real_init(
            self, project_root=manager.project_root, home_dir=manager.home_dir,
            store_root=manager.store_root, hosted_client=_offline_client(key),
        )

    return _init


def test_a_service_key_never_inherits_a_users_flags_and_gets_its_own_after_registration(
    tmp_path: Path, market: FakeMarketplace
):
    v = market.publish("review-loop", "1.0.0")
    market.service_keys.add("sk_ci")
    manager, _, _ = _manager(tmp_path, market)
    # The only user on this store has V blocked, with a private reason.
    market.revoke(v, "blocked", "alice private reason", kind="version", block_scope="global",
                  visible_to={"sk_test"})
    hosted.sync_revocations(market.client("sk_test"), manager.store_root)
    ci = SkillManager(
        project_root=manager.project_root, home_dir=manager.home_dir,
        store_root=manager.store_root, hosted_client=market.client("sk_ci"),
    )
    # Online: the service key is registered to its OWN namespace before the
    # check, so the user's block (and reason) never applies to it.
    ci.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    fp = hosted.identity_fingerprint("https://sfs.test", "sk_ci")
    assert HostedState(manager.store_root).read()["identities"][fp] == f"https://sfs.test|key:{fp}"
    flags = hosted.effective_flags(HostedState(manager.store_root).read(), fp, "https://sfs.test")
    assert all("alice" not in str(flag.get("reason")) for flag in flags)
    # Its own blocks apply once synced.
    ci.remove("review-loop", scope="user")
    market.revoke(v, "blocked", "ci reason", kind="version", block_scope="global",
                  visible_to={"sk_ci"})
    hosted.sync_revocations(market.client("sk_ci"), manager.store_root)
    with pytest.raises(SkillError, match="BLOCKED"):
        ci.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")


def test_a_failed_registration_never_falls_back_to_the_users_namespace(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    v = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    market.revoke(v, "blocked", "alice private reason", kind="version", block_scope="global",
                  visible_to={"sk_test"})
    hosted.sync_revocations(market.client("sk_test"), manager.store_root)

    def _fails(client, store_root):
        raise hosted.HostedUnavailable("identity lookup failed")

    monkeypatch.setattr(hosted, "register_identity", _fails)
    market.service_keys.add("sk_ci")
    package = hosted.import_hosted_version(
        market.client("sk_ci"), v, manager.store_root,
    )
    assert package.content_hash == v["content_hash"]  # the user's block was not applied


def test_doctor_warns_for_a_rotated_service_key(tmp_path: Path, monkeypatch, market: FakeMarketplace):
    v = market.publish("review-loop", "1.0.0")
    market.service_keys.add("sk_ci")
    manager, _, _ = _manager(tmp_path, market)
    market.revoke(v, "yanked", "ci yank", kind="version", visible_to={"sk_ci"})
    hosted.sync_revocations(market.client("sk_ci"), manager.store_root)
    rotated = SkillManager(
        project_root=manager.project_root, home_dir=manager.home_dir,
        store_root=manager.store_root, hosted_client=_offline_client("sk_ci_new"),
    )
    assert hosted.identity_unknown(rotated) == "https://sfs.test"
    _cli_env(tmp_path, monkeypatch, _offline_client("sk_ci_new"))
    monkeypatch.setattr(SkillManager, "__init__", _bind_manager(manager, "sk_ci_new"))
    assert "Identity unknown for https://sfs.test" in runner.invoke(skill_app, ["doctor"]).output


def test_an_earlier_builds_last_user_record_is_dropped_on_load(
    tmp_path: Path, market: FakeMarketplace
):
    manager, _, _ = _manager(tmp_path, market)
    with HostedState(manager.store_root).edit() as state:
        state["last_user_id"] = {"https://sfs.test": "usr_me"}
    assert "last_user_id" not in HostedState(manager.store_root).read()


# ---------------------------------------------------------------------------
# per-origin modes: a required assignment never pulls in an optional one's tools
# ---------------------------------------------------------------------------


def _claude_file(home: Path, name: str = "review-loop") -> Path:
    return home / ".claude" / "skills" / name / "SKILL.md"


def test_the_daemon_installs_only_the_required_origins_tools(
    tmp_path: Path, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, tools=["codex"])                       # required
    market.assign(item, mode="optional", tools=["claude-code"])  # optional
    report = hosted.daemon_reconcile(manager, market.client())
    assert _skill_file(home).is_file() and not _claude_file(home).exists()
    assert sorted(manager.list_installed("user")[0]["targets"]) == ["codex"]
    assert "not installed: claude-code (optional, not accepted)" in report.items[0].detail
    assert len(market.downloads) == 1
    # Running again never adds it either.
    hosted.daemon_reconcile(manager, market.client())
    assert not _claude_file(home).exists()


def test_interactive_pull_offers_the_optional_origins_tools(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, tools=["codex"])
    market.assign(item, mode="optional", tools=["claude-code"])
    offers: list[list[str]] = []

    def _accept(view):
        offers.append(list(view.targets))
        return True

    hosted.pull(manager, market.client(), install_optional=_accept)
    assert offers == [["claude-code"]]  # asked about the optional origin's tool only
    assert _skill_file(home).is_file() and _claude_file(home).is_file()
    assert len(market.downloads) == 1  # one download for both
    # Accepted once: kept on later pulls without asking again.
    offers.clear()
    hosted.pull(manager, market.client(), install_optional=_accept)
    assert offers == [] and _claude_file(home).is_file()


def test_declining_the_optional_origin_keeps_only_the_required_tools(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, tools=["codex"])
    market.assign(item, mode="optional", tools=["claude-code"])
    hosted.pull(manager, market.client(), install_optional=lambda view: False)
    assert _skill_file(home).is_file() and not _claude_file(home).exists()


def test_an_opt_out_of_the_optional_version_is_respected_next_to_a_required_one(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["claude-code"])
    hosted.pull(manager, market.client(), install_optional=lambda view: True)
    package_id = manager.list_installed("user")[0]["package_id"]
    with manager.explicit_change("user"):
        keys = hosted.begin_opt_out(manager, "user", package_id)
        manager.remove(package_id, scope="user")
        hosted.finish_opt_out(manager, "user", keys, removed=True)
    # A required assignment for the same version now arrives.
    market.assign(item, tools=["codex"])
    seen: list[bool] = []

    def _yes_unless_opted_out(view):  # what `pull --yes` does
        seen.append(view.opted_out)
        return not view.opted_out

    report = hosted.pull(manager, market.client(), install_optional=_yes_unless_opted_out)
    assert seen == [True]
    assert _skill_file(home).is_file() and not _claude_file(home).exists()
    assert "opted out" in report.items[0].detail


def test_two_optional_assignments_are_offered_one_by_one(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex"])
    market.assign(item, mode="optional", tools=["claude-code"])
    offers: list[list[str]] = []

    def _only_codex(view):
        offers.append(list(view.targets))
        return view.targets == ["codex"]

    report = hosted.pull(manager, market.client(), install_optional=_only_codex)
    assert offers == [["codex"], ["claude-code"]]
    assert _skill_file(home).is_file() and not _claude_file(home).exists()
    assert report.items[0].status == "installed"
    # The daemon never adds the declined one.
    hosted.daemon_reconcile(manager, market.client())
    assert not _claude_file(home).exists()


# ---------------------------------------------------------------------------
# optional consent: explicit provenance, revalidated under the locks
# ---------------------------------------------------------------------------


def _record_for(manager: SkillManager, origin_id: str) -> dict | None:
    return next(
        (
            r for r in HostedState(manager.store_root).read()["pulled"].values()
            if r.get("origin_id") == origin_id
        ),
        None,
    )


def test_a_declined_optional_origin_is_never_activated_because_the_version_is_installed(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    required = market.assign(item, tools=["codex"])
    optional = market.assign(item, mode="optional", tools=["claude-code"])
    hosted.pull(manager, market.client(), install_optional=lambda view: False)
    market.assignments.remove(required)  # only the declined optional one is left
    report = hosted.pull(manager, market.client(), install_optional=lambda view: False)
    assert report.items[0].status == "up-to-date"
    assert _record_for(manager, optional["id"]) is None  # installed is not consent
    hosted.daemon_reconcile(manager, market.client())
    assert not _claude_file(home).exists()


def test_records_without_optional_acceptance_never_count_as_consent(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item, tools=["codex"])  # required: recorded as required
    hosted.pull(manager, market.client())
    assert _record_for(manager, row["id"])["mode"] == "required"
    # The admin turns the same assignment into an optional one for more tools.
    row.update(mode="optional", target_tools=["codex", "claude-code"])
    hosted.daemon_reconcile(manager, market.client())
    assert not _claude_file(home).exists()  # a required-mode record is not consent
    # A legacy record (no mode / acceptance) fails closed too.
    with HostedState(manager.store_root).edit() as state:
        for record in state["pulled"].values():
            for name in ("mode", "accepted_at", "accepted_via"):
                record.pop(name, None)
    hosted.daemon_reconcile(manager, market.client())
    assert not _claude_file(home).exists()


def test_an_opt_out_written_after_the_prompt_drops_that_origin(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, tools=["codex"])
    market.assign(item, mode="optional", tools=["claude-code"])
    namespace = "https://sfs.test|usr_me"

    def _yes_then_opted_out(view):
        # A concurrent `sfs skill remove` writes the opt-out meanwhile.
        with HostedState(manager.store_root).edit() as state:
            key = hosted._opt_out_key(manager, namespace, "user", "assignment", item["version_id"])
            state["opt_outs"][key] = {"version_id": item["version_id"]}
        return "yes"

    report = hosted.pull(manager, market.client(), install_optional=_yes_then_opted_out)
    assert _skill_file(home).is_file() and not _claude_file(home).exists()
    assert "removed or opted out meanwhile" in report.items[0].detail


def test_an_acceptance_voided_meanwhile_drops_that_origin(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, tools=["codex"])
    market.assign(item, mode="optional", tools=["claude-code"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert _claude_file(home).is_file()
    market.assign(item, mode="optional", tools=["gemini"])

    def _reinstall_meanwhile(view):
        # The user removes and re-adds the package meanwhile: a NEW
        # installation, which the earlier acceptance does not cover.
        manager.remove("review-loop", scope="user")
        manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
        return False

    report = hosted.pull(manager, market.client(), install_optional=_reinstall_meanwhile)
    assert not _claude_file(home).exists()
    assert "removed or opted out meanwhile" in report.items[0].detail


def test_re_accepting_an_opted_out_origin_clears_the_opt_out_also_when_up_to_date(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    package_id = manager.list_installed("user")[0]["package_id"]
    with manager.explicit_change("user"):
        keys = hosted.begin_opt_out(manager, "user", package_id)
        manager.remove(package_id, scope="user")
        hosted.finish_opt_out(manager, "user", keys, removed=True)
    assert HostedState(manager.store_root).read()["opt_outs"]
    # Installed again by a required assignment; then the user says yes to the
    # opted-out optional one interactively (status stays up-to-date).
    market.assign(item, tools=["codex", "claude-code"])
    hosted.pull(manager, market.client(), install_optional=lambda view: False)
    hosted.pull(
        manager, market.client(),
        install_optional=lambda view: "prompt" if view.opted_out else False,
    )
    # The optional origin's tools are covered by the required one, so it is
    # never offered; accept it explicitly through a pull with only it left.
    market.assignments[:] = [a for a in market.assignments if a.get("mode") == "optional"]
    report = hosted.pull(
        manager, market.client(),
        install_optional=lambda view: "prompt" if view.opted_out else False,
    )
    assert report.items[0].status in {"up-to-date", "updated"}
    assert HostedState(manager.store_root).read()["opt_outs"] == {}


def test_an_optional_origin_is_redundant_only_if_active_origins_authorize_as_much():
    base = dict(version_id="skv_" + "1" * 24, content_hash="sha256:" + "a" * 64, scope="user")
    active_request = hosted.PullItem(
        mode="required", targets=["codex"], origins=[("request", "r")],
        origin_targets={"request|r": ["codex"]}, origin_caps={"request|r": {"instructions"}},
        **base,
    )
    optional_assignment = hosted.PullItem(
        mode="optional", targets=["codex"], origins=[("assignment", "a")],
        origin_targets={"assignment|a": ["codex"]}, origin_caps={"assignment|a": None},
        **base,
    )
    # Same tool, but the assignment authorizes more than the request: offered.
    assert not hosted._covered(active_request, optional_assignment)
    active_assignment = hosted.PullItem(
        mode="required", targets=["codex"], origins=[("assignment", "b")],
        origin_targets={"assignment|b": ["codex"]}, origin_caps={"assignment|b": None},
        **base,
    )
    assert hosted._covered(active_assignment, optional_assignment)
    # A different tool is never covered.
    other = hosted.PullItem(
        mode="optional", targets=["claude-code"], origins=[("assignment", "c")],
        origin_targets={"assignment|c": ["claude-code"]}, origin_caps={"assignment|c": None},
        **base,
    )
    assert not hosted._covered(active_assignment, other)


# ---------------------------------------------------------------------------
# opt-out tombstones win over acceptances; acceptances bind a real install id
# ---------------------------------------------------------------------------


def _optional_origin_accepted(manager: SkillManager, item: dict, origin_id: str) -> bool:
    state = HostedState(manager.store_root).read()
    key = next(k for k, r in state["pulled"].items() if r.get("origin_id") == origin_id)
    view = hosted.PullItem(
        mode="optional", version_id=item["version_id"], content_hash=item["content_hash"],
        scope="user", targets=[], origins=[("assignment", origin_id)],
    )
    return hosted._optional_accepted(
        manager, state["pulled"], [key], view,
        namespace="https://sfs.test|usr_me", opt_outs=state["opt_outs"],
    )


def test_a_crash_between_the_opt_out_and_the_removal_voids_the_acceptance(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, tools=["codex"])
    optional = market.assign(item, mode="optional", tools=["claude-code"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert _optional_origin_accepted(manager, item, optional["id"])
    package_id = manager.list_installed("user")[0]["package_id"]
    with manager.explicit_change("user"):
        hosted.begin_opt_out(manager, "user", package_id)
        # ... crash: the removal never ran, the tombstone stays pending.
    assert not _optional_origin_accepted(manager, item, optional["id"])
    report = hosted.daemon_reconcile(manager, market.client())
    # Its tool is no longer active: reported, never removed by pull.
    assert report.unassigned_tools[0]["tools"] == ["claude-code"]
    assert _claude_file(home).is_file()


def test_a_removal_after_the_re_acceptance_prompt_keeps_the_opt_out(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, tools=["codex"])
    market.assign(item, mode="optional", tools=["claude-code"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    package_id = manager.list_installed("user")[0]["package_id"]
    with manager.explicit_change("user"):
        keys = hosted.begin_opt_out(manager, "user", package_id)
        manager.remove(package_id, scope="user")
        hosted.finish_opt_out(manager, "user", keys, removed=True)
    [(opt_key, tombstone)] = HostedState(manager.store_root).read()["opt_outs"].items()
    first_generation = tombstone["generation"]

    def _yes_but_removed_again(view):
        if not view.opted_out:
            return False
        # Another removal intent lands after the user said yes.
        with HostedState(manager.store_root).edit() as state:
            state["opt_out_generation"] = int(state.get("opt_out_generation") or 0) + 1
            state["opt_outs"][opt_key]["generation"] = state["opt_out_generation"]
        return "prompt"

    report = hosted.pull(manager, market.client(), install_optional=_yes_but_removed_again)
    assert not _claude_file(home).exists()
    assert "removed or opted out meanwhile" in report.items[0].detail
    tombstone = HostedState(manager.store_root).read()["opt_outs"][opt_key]
    assert tombstone["generation"] > first_generation  # never cleared


def test_every_removal_intent_bumps_the_tombstone_generation(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    package_id = manager.list_installed("user")[0]["package_id"]
    with manager.explicit_change("user"):
        hosted.begin_opt_out(manager, "user", package_id)
    first = list(HostedState(manager.store_root).read()["opt_outs"].values())[0]["generation"]
    with manager.explicit_change("user"):
        hosted.begin_opt_out(manager, "user", package_id)  # an existing tombstone
    second = list(HostedState(manager.store_root).read()["opt_outs"].values())[0]["generation"]
    assert second > first


def test_a_remove_of_an_interrupted_optional_install_still_opts_out(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["claude-code"])
    # Crash after the install committed but before its origin was recorded:
    # the installed version now exists only in a live pull operation.
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    installed = manager.list_installed("user")[0]
    assert installed["pull_op"] in HostedState(manager.store_root).read()["pull_ops"]
    assert _roles(manager) == []  # installed, but no origin recorded yet
    # The user removes it explicitly; the opt-out must cover the version only
    # the live intent knows about, so recovery cannot later clear the way for
    # `pull --yes` to reinstall it.
    package_id = installed["package_id"]
    with manager.explicit_change("user"):
        keys = hosted.begin_opt_out(manager, "user", package_id)
        manager.remove(package_id, scope="user")
        hosted.finish_opt_out(manager, "user", keys, removed=True)
    assert keys
    tombstones = HostedState(manager.store_root).read()["opt_outs"]
    assert any(t.get("version_id") == item["version_id"] for t in tombstones.values())
    # Recovery discards the now-rowless intent; --yes must not reinstall it.
    report = hosted.pull(
        manager, market.client(),
        install_optional=lambda view: "yes" if not view.opted_out else False,
    )
    assert report.items[0].status == "opted-out"
    assert manager.list_installed("user") == []
    assert not _claude_file(home).exists()


def test_a_remove_of_an_unstamped_optional_origin_still_opts_out(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch, "proj_1")
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    client = market.client()
    # Install via an approved request (origin kind "request", always
    # required). The recorded origin's kind is then "request" - distinct from
    # the optional assignment accepted later.
    request = {
        "id": "sir_" + "1" * 24,
        "status": "approved",
        "version_id": item["version_id"],
        "content_hash": item["content_hash"],
        "target_user_id": "usr_me",
        "target_scope": "user",
        "target_device_id": None,
        "target_tools": ["codex"],
        "requested_capabilities": [],
    }
    market.requests = [request]
    report = hosted.pull(manager, client)
    assert report.items[0].status == "installed"
    # The request goes away and a fresh optional assignment asks for the very
    # same already-installed tool. Accepting it adds no new tool, so
    # `_reconcile_targets` skips the compile and the row is never stamped
    # with the intent. Crash before the origin is recorded: the accepted
    # optional origin then survives only in the live intent's fresh_keys.
    market.requests = []
    market.assign(item, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, client, install_optional=lambda view: "yes")
    installed = manager.list_installed("user")[0]
    live = HostedState(manager.store_root).read()["pull_ops"]
    assert installed.get("pull_op") not in live  # the no-op acceptance never stamped it
    assert live
    assert any(
        "assignment" in key
        for op in live.values()
        for key in op.get("fresh_keys", [])
    )
    # The user removes it explicitly; the opt-out must cover the optional
    # origin only the live intent knows about (kind "assignment"), so
    # recovery cannot later clear the way for `pull --yes` to reinstall it.
    package_id = installed["package_id"]
    with manager.explicit_change("user"):
        keys = hosted.begin_opt_out(manager, "user", package_id)
        manager.remove(package_id, scope="user")
        hosted.finish_opt_out(manager, "user", keys, removed=True)
    assert keys
    tombstones = HostedState(manager.store_root).read()["opt_outs"]
    assert any("|assignment|" in key for key in tombstones)
    # Recovery discards the now-rowless intent; --yes must not reinstall it.
    report = hosted.pull(
        manager, client,
        install_optional=lambda view: "yes" if not view.opted_out else False,
    )
    assert report.items[0].status == "opted-out"
    assert manager.list_installed("user") == []
    assert not _skill_file(home).exists()


def test_an_acceptance_without_a_real_install_id_never_counts(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    optional = market.assign(item, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    store = manager.state_store("user")
    # An older row and record: no install id on either side (None == None).
    state = store.state()
    for row in state["packages"].values():
        row.pop("install_id", None)
    state_module.atomic_json(store.state_path, state)
    with HostedState(manager.store_root).edit() as hosted_state:
        for record in hosted_state["pulled"].values():
            record["install_id"] = None
    assert not _optional_origin_accepted(manager, item, optional["id"])
    # Accepting it again assigns the row an install id first, then binds to it.
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    install_id = list(store.state()["packages"].values())[0]["install_id"]
    assert isinstance(install_id, str) and install_id
    assert _record_for(manager, optional["id"])["install_id"] == install_id
    assert _optional_origin_accepted(manager, item, optional["id"])


# ---------------------------------------------------------------------------
# crash-atomic install + recording, install-id binding, revocation race
# ---------------------------------------------------------------------------


def _crash_once(monkeypatch, target, name: str) -> None:
    real = getattr(target, name)
    calls = {"n": 0}

    def _maybe_crash(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, _maybe_crash)


def test_a_crash_before_the_install_commits_leaves_nothing_to_finish(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item)
    _crash_once(monkeypatch, SkillManager, "install_prepared")
    with pytest.raises(RuntimeError):
        hosted.daemon_reconcile(manager, market.client())
    assert HostedState(manager.store_root).read()["pull_ops"]  # the intent survived
    hosted.daemon_reconcile(manager, market.client())
    assert HostedState(manager.store_root).read()["pull_ops"] == {}
    assert _skill_file(home).is_file()


def test_a_crash_between_install_and_recording_is_finished_next_time(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item)
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.daemon_reconcile(manager, market.client())
    installed = manager.list_installed("user")[0]
    assert installed["pull_op"] in HostedState(manager.store_root).read()["pull_ops"]
    assert _roles(manager) == []  # installed, but no origin recorded yet
    hosted.daemon_reconcile(manager, market.client())
    assert _roles(manager) == [(row["id"], "installer")]
    record = _record_for(manager, row["id"])
    assert record["install_id"] == installed["install_id"]
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_an_unrecorded_fresh_optional_install_is_rolled_back_to_the_trash(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert _skill_file(home).is_file()
    # The daemon finds the intent: the acceptance was never recorded, so the
    # install that existed only for it is taken out again (fail closed).
    hosted.daemon_reconcile(manager, market.client())
    assert manager.list_installed("user") == []
    assert not _skill_file(home).exists()
    assert any(path.name == "SKILL.md" for path in _trashed_files(manager))
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_recording_and_ending_the_intent_are_one_write(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(item)

    def _never(*args, **kwargs):
        raise AssertionError("a recorded install must not need a separate intent cleanup")

    monkeypatch.setattr(hosted, "_finish_pull_op", _never)
    hosted.daemon_reconcile(manager, market.client())
    assert HostedState(manager.store_root).read()["pull_ops"] == {}
    assert _roles(manager) == [(row["id"], "installer")]


def _strip_install_ids(manager: SkillManager) -> None:
    store = manager.state_store("user")
    state = store.state()
    for row in state["packages"].values():
        row.pop("install_id", None)
    state_module.atomic_json(store.state_path, state)
    with HostedState(manager.store_root).edit() as hosted_state:
        for record in hosted_state["pulled"].values():
            record["install_id"] = None


def test_two_missing_install_ids_never_grant_authority():
    record = {"role": "installer", "local_package_id": "pkg", "install_id": None}
    assert not hosted._has_authority(record, {"pkg": None})
    assert not hosted._has_authority({**record, "install_id": ""}, {"pkg": ""})
    assert hosted._has_authority({**record, "install_id": "sfi_1"}, {"pkg": "sfi_1"})


def test_legacy_authority_is_bound_to_a_generated_install_id(
    tmp_path: Path, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.daemon_reconcile(manager, market.client())
    _strip_install_ids(manager)
    hosted.daemon_reconcile(manager, market.client())
    install_id = manager.list_installed("user")[0]["install_id"]
    assert isinstance(install_id, str) and install_id
    record = _record_for(manager, row["id"])
    assert (record["install_id"], record["role"]) == (install_id, "installer")


def test_a_legacy_unassigned_package_is_still_reported(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    # A package installed by a pre-install-id build whose assignment vanishes
    # before any pull could migrate it (so no item loop ever runs for it) must
    # still be REPORTED as no longer assigned: pull never silently drops the
    # only installer evidence it manages the package.
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.daemon_reconcile(manager, market.client())
    assert _skill_file(home).is_file()
    _strip_install_ids(manager)  # an older build: no install id on row or record
    market.assignments.remove(row)  # the assignment disappears before a pull migrates it
    report = hosted.daemon_reconcile(manager, market.client())
    assert [c["name"] for c in report.unassigned] == ["review-loop"]
    assert [c["name"] for c in hosted.unassigned_packages(manager)] == ["review-loop"]
    # The hard invariant still holds: nothing is removed, only reported.
    assert _skill_file(home).is_file()
    assert [r["name"] for r in manager.list_installed("user")] == ["review-loop"]


def test_a_legacy_managed_package_adds_tools_on_its_migrating_pull(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(item, tools=["codex"])
    hosted.daemon_reconcile(manager, market.client())
    assert _targets(manager) == ["codex"]
    _strip_install_ids(manager)  # an older build: the installed row has no install id
    row["target_tools"] = ["codex", "claude-code"]  # the assignment gains a tool
    report = hosted.pull(manager, market.client())
    assert report.items[0].status == "updated", (report.items[0].detail, report.notes)
    installed = manager.list_installed("user")[0]
    assert isinstance(installed["install_id"], str) and installed["install_id"]
    # The tool-add landed on the SAME pull that migrated the install id.
    assert _targets(manager) == ["claude-code", "codex"]
    record = _record_for(manager, row["id"])
    assert (record["install_id"], record["role"]) == (installed["install_id"], "installer")


def test_a_legacy_managed_package_updates_version_on_its_migrating_pull(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(v1, tools=["codex"])
    hosted.daemon_reconcile(manager, market.client())
    _strip_install_ids(manager)  # an older build: the installed row has no install id
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(row)
    row = market.assign(v2, tools=["codex"])
    report = hosted.pull(manager, market.client())
    assert report.items[0].status == "installed", (report.items[0].detail, report.notes)
    installed = manager.list_installed("user")[0]
    assert isinstance(installed["install_id"], str) and installed["install_id"]
    # The version update landed on the SAME pull that migrated the install id.
    assert installed["content_hash"] == v2["content_hash"]
    assert "two" in _skill_file(home).read_text()
    record = _record_for(manager, row["id"])
    assert (record["install_id"], record["role"]) == (installed["install_id"], "installer")


def test_a_failed_install_id_migration_keeps_the_tombstone_and_defers(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills.state import SkillStateStore

    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    package_id = manager.list_installed("user")[0]["package_id"]
    with manager.explicit_change("user"):
        keys = hosted.begin_opt_out(manager, "user", package_id)  # still installed
    _strip_install_ids(manager)  # an older row without an install id
    before = HostedState(manager.store_root).read()["pulled"]
    def _fails(self, package_id, candidate=None):
        raise SkillError("state unreadable")  # an error, not "the row is gone"

    monkeypatch.setattr(SkillStateStore, "ensure_install_id", _fails)
    report = hosted.pull(
        manager, market.client(), install_optional=lambda view: "prompt" if view.opted_out else False,
    )
    assert report.items[0].status == "deferred", (report.items[0].detail, report.notes)
    state = HostedState(manager.store_root).read()
    assert set(state["opt_outs"]) == set(keys)  # the tombstone was not touched
    # No origin was (re)recorded: only legacy installer records got the
    # migration's candidate id (finished by a later pull).
    strip = lambda records: {k: {**r, "install_id": None} for k, r in records.items()}  # noqa: E731
    assert strip(state["pulled"]) == strip(before)
    assert state["install_id_migrations"]


def test_retrying_an_install_id_migration_never_replaces_its_candidate(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills.state import SkillStateStore

    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.daemon_reconcile(manager, market.client())
    _strip_install_ids(manager)  # an older row without an install id
    real_ensure = SkillStateStore.ensure_install_id
    attempts = {"n": 0}

    def _flaky(self, package_id, candidate=None):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise SkillError("state unreadable")  # the row write fails twice
        return real_ensure(self, package_id, candidate)

    monkeypatch.setattr(SkillStateStore, "ensure_install_id", _flaky)
    # First pull: the candidate is stamped on the legacy installer records,
    # then writing it to the row fails - the migration stays pending.
    first = hosted.daemon_reconcile(manager, market.client())
    assert first.items[0].status == "deferred"
    [migration] = HostedState(manager.store_root).read()["install_id_migrations"].values()
    assert _record_for(manager, row["id"])["install_id"] == migration["candidate"]
    # Second pull: the completion pass fails once more, then the migration is
    # retried. It must reuse the pending candidate, not mint a new one that
    # strands the records on the old id and downgrades them to references.
    second = hosted.daemon_reconcile(manager, market.client())
    assert second.items[0].status == "up-to-date"
    install_id = manager.list_installed("user")[0]["install_id"]
    assert install_id == migration["candidate"]
    record = _record_for(manager, row["id"])
    assert (record["install_id"], record["role"]) == (install_id, "installer")
    assert HostedState(manager.store_root).read()["install_id_migrations"] == {}


def test_a_block_merged_during_the_install_commit_is_serialized_with_it(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    import threading
    import time

    from sessionfs.skills.state import SkillStateStore

    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    events: list[str] = []
    real_apply = SkillStateStore.apply
    threads: list[threading.Thread] = []

    def _sync_block():
        market.revoke(item, "blocked", "malware", kind="version", block_scope="global")
        hosted.sync_revocations(market.client(), manager.store_root)
        events.append("block merged")

    def _apply(self, *args, **kwargs):
        if not threads:
            thread = threading.Thread(target=_sync_block)
            threads.append(thread)
            thread.start()
            time.sleep(0.3)  # the merge must wait for this commit
        result = real_apply(self, *args, **kwargs)
        events.append("install committed")
        return result

    monkeypatch.setattr(SkillStateStore, "apply", _apply)
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    threads[0].join(timeout=10)
    assert events == ["install committed", "block merged"]
    # Landing after the commit, the block is enforced like any other.
    assert [w["state"] for w in hosted.revocation_warnings(manager)] == ["blocked"]
    hosted.enforce_blocked(manager)
    assert not _skill_file(home).exists()


# ---------------------------------------------------------------------------
# round 31: recovery before network, after journal recovery, and complete
# ---------------------------------------------------------------------------


def test_an_offline_daemon_still_rolls_back_an_unrecorded_optional_install(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert _skill_file(home).is_file()
    offline = SkillManager(
        project_root=manager.project_root, home_dir=manager.home_dir,
        store_root=manager.store_root, hosted_client=_offline_client("sk_test"),
    )
    with pytest.raises(SkillError):  # the feed cannot be read offline ...
        hosted.daemon_reconcile(offline, _offline_client("sk_test"))
    # ... but the interrupted install was settled first.
    assert manager.list_installed("user") == []
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_an_install_rolled_back_by_the_skill_journal_is_never_recorded(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills.state import SkillStateStore

    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item)
    real_atomic = state_module.atomic_json

    def _crash_on_commit(path, value):
        if Path(path).name == "skills.journal.json" and value.get("phase") == "committed":
            raise RuntimeError("power loss before the journal commit")
        return real_atomic(path, value)

    monkeypatch.setattr(state_module, "atomic_json", _crash_on_commit)
    monkeypatch.setattr(SkillStateStore, "_recover_locked", lambda self: None)
    with pytest.raises(RuntimeError):
        hosted.daemon_reconcile(manager, market.client())
    monkeypatch.undo()
    # Recovery runs the journal first: the uncommitted install is undone, so
    # its intent records nothing; the normal pull then installs it cleanly.
    hosted._recover_locally(manager, market.client(), ("user",))
    assert manager.list_installed("user") == []
    assert HostedState(manager.store_root).read()["pulled"] == {}
    hosted.daemon_reconcile(manager, market.client())
    assert _skill_file(home).is_file() and _roles(manager)


def test_recovery_drops_tools_added_only_for_an_unrecorded_optional_acceptance(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    required = market.assign(item, tools=["codex"])
    optional = market.assign(item, mode="optional", tools=["claude-code"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert _claude_file(home).is_file()
    hosted._recover_locally(manager, market.client(), ("user",))
    assert sorted(manager.list_installed("user")[0]["targets"]) == ["codex"]
    assert not _claude_file(home).exists() and _skill_file(home).is_file()
    assert _record_for(manager, required["id"]) is not None
    assert _record_for(manager, optional["id"]) is None


def test_recovery_rolls_back_an_update_made_only_for_an_unrecorded_acceptance(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert "two" in _skill_file(home).read_text()
    hosted._recover_locally(manager, market.client(), ("user",))
    assert manager.list_installed("user")[0]["content_hash"] == v1["content_hash"]
    assert "one" in _skill_file(home).read_text()


def test_an_explicit_add_voids_pull_authority_even_if_its_cleanup_crashes(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.daemon_reconcile(manager, market.client())
    before = manager.list_installed("user")[0]["install_id"]

    def _crash(*args, **kwargs):
        raise RuntimeError("crash before the cleanup")

    monkeypatch.setattr(hosted, "retire_pull_authority", _crash)
    with pytest.raises(RuntimeError):
        manager.add("sessionfs:acme/review-loop", targets=["codex", "claude-code"], scope="user")
    after = manager.list_installed("user")[0]["install_id"]
    assert after != before  # rotated in the install's own commit
    record = _record_for(manager, row["id"])
    assert not hosted._has_authority(record, hosted._install_ids(manager, "user"))


def test_an_interrupted_install_id_migration_is_completed_next_time(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills.state import SkillStateStore

    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.daemon_reconcile(manager, market.client())
    _strip_install_ids(manager)
    _crash_once(monkeypatch, SkillStateStore, "ensure_install_id")
    with pytest.raises(RuntimeError):
        hosted.daemon_reconcile(manager, market.client())
    state = HostedState(manager.store_root).read()
    [migration] = state["install_id_migrations"].values()
    assert _record_for(manager, row["id"])["install_id"] == migration["candidate"]
    monkeypatch.undo()
    hosted.daemon_reconcile(manager, market.client())
    install_id = manager.list_installed("user")[0]["install_id"]
    assert install_id == migration["candidate"]
    record = _record_for(manager, row["id"])
    assert (record["install_id"], record["role"]) == (install_id, "installer")
    assert HostedState(manager.store_root).read()["install_id_migrations"] == {}


def test_a_restore_merged_during_a_quarantine_is_serialized_with_it(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    import threading
    import time

    from sessionfs.skills.state import SkillStateStore

    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    market.revoke(item, "blocked", "malware", kind="version", block_scope="global")
    hosted.sync_revocations(market.client(), manager.store_root)
    events: list[str] = []
    real_remove = SkillStateStore.remove
    threads: list[threading.Thread] = []

    def _restore():
        market.revoke(item, "restored", kind="version", block_scope="global")
        hosted.sync_revocations(market.client(), manager.store_root)
        events.append("restore merged")

    def _remove(self, *args, **kwargs):
        if not threads:
            thread = threading.Thread(target=_restore)
            threads.append(thread)
            thread.start()
            time.sleep(0.3)  # the merge must wait for the quarantine
        result = real_remove(self, *args, **kwargs)
        events.append("quarantined")
        return result

    monkeypatch.setattr(SkillStateStore, "remove", _remove)
    hosted.enforce_blocked(manager)
    threads[0].join(timeout=10)
    assert events == ["quarantined", "restore merged"]


# ---------------------------------------------------------------------------
# round 32: recovery acts only on proven evidence
# ---------------------------------------------------------------------------


def _capture_intents(monkeypatch) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []
    real = hosted._begin_pull_op

    def _spy(manager, hosted_state, **kwargs):
        token = real(manager, hosted_state, **kwargs)
        captured.append((token, dict(hosted_state.read()["pull_ops"][token])))
        return token

    monkeypatch.setattr(hosted, "_begin_pull_op", _spy)
    return captured


def test_recovery_never_touches_an_install_the_user_re_added_after_the_crash(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    token = next(iter(HostedState(manager.store_root).read()["pull_ops"]))
    assert manager.list_installed("user")[0]["install_id"] == token  # the op's install
    # The user re-adds it explicitly before the next pull: a NEW installation.
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    hosted._recover_locally(manager, market.client(), ("user",))
    [row] = manager.list_installed("user")
    assert row["install_id"] != token and _skill_file(home).is_file()  # left alone
    assert HostedState(manager.store_root).read()["pull_ops"] == {}  # stale intent dropped
    assert _roles(manager) == []  # and no pull authority was granted


def test_an_acceptance_recorded_before_the_crash_is_never_undone(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    optional = market.assign(item, mode="optional", tools=["codex"])
    intents = _capture_intents(monkeypatch)
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert _record_for(manager, optional["id"])["accepted_via"] == "yes"
    # An intent left behind although its recording landed (e.g. an older build).
    token, intent = intents[0]
    with HostedState(manager.store_root).edit() as state:
        state["pull_ops"][token] = intent
    hosted._recover_locally(manager, market.client(), ("user",))
    assert _skill_file(home).is_file()
    assert _record_for(manager, optional["id"])["accepted_via"] == "yes"
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_recovery_restores_the_pre_op_version_and_tools_in_one_install(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, mode="optional", tools=["codex", "claude-code"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert "two" in _claude_file(home).read_text()
    applies: list[str] = []
    real_install = SkillManager.install_prepared

    def _count(self, *args, **kwargs):
        applies.append("install")
        return real_install(self, *args, **kwargs)

    monkeypatch.setattr(SkillManager, "install_prepared", _count)
    hosted._recover_locally(manager, market.client(), ("user",))
    [row] = manager.list_installed("user")
    assert row["content_hash"] == v1["content_hash"] and row["targets"] == ["codex"]
    assert "one" in _skill_file(home).read_text() and not _claude_file(home).exists()
    assert applies == ["install"]  # version and tools restored together


def test_a_recovery_restore_needs_durable_consent_or_stays_pending(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("scripted", "1.0.0", body="one", scripts=True)
    manager, _, _ = _manager(tmp_path, market)
    first = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes",
                flags={"allow_scripts": True})
    v2 = market.publish("scripted", "2.0.0", body="two", scripts=True,
                        package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes",
                    flags={"allow_scripts": True})
    # Without the durable pre-op consent the restore is refused and reported.
    with HostedState(manager.store_root).edit() as state:
        [op] = state["pull_ops"].values()
        saved = (dict(op["consent"]), json.loads(json.dumps(op["before"])))
        op["consent"] = {}
        for prior in op["before"].values():
            prior["consented"] = []
    report = hosted.PullReport()
    hosted._recover_pull_ops(
        manager, HostedState(manager.store_root), "https://sfs.test|usr_me", ("user",),
        report=report,
    )
    assert HostedState(manager.store_root).read()["pull_ops"]  # pending
    [entry] = report.recovery
    assert entry["flags"] == ["--allow-scripts"]
    assert "could not be settled because consent for skill-scripts is missing" in entry["message"]
    assert "run `sfs skill pull --allow-scripts` to settle it" in entry["message"]
    # With it (the receipts of the pre-op installation), it is restored.
    with HostedState(manager.store_root).edit() as state:
        [op] = state["pull_ops"].values()
        op["consent"], op["before"] = saved[0], saved[1]
    hosted._recover_locally(manager, market.client(), ("user",))
    assert manager.list_installed("user")[0]["content_hash"] == v1["content_hash"]
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_a_migration_never_moves_candidate_authority_to_another_installation(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    from sessionfs.skills.state import SkillStateStore

    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.daemon_reconcile(manager, market.client())
    _strip_install_ids(manager)
    _crash_once(monkeypatch, SkillStateStore, "ensure_install_id")
    with pytest.raises(RuntimeError):
        hosted.daemon_reconcile(manager, market.client())
    [migration] = HostedState(manager.store_root).read()["install_id_migrations"].values()
    monkeypatch.undo()
    # The user re-adds it explicitly: a different installation now.
    manager.add("sessionfs:acme/review-loop", targets=["codex"], scope="user")
    hosted._recover_locally(manager, market.client(), ("user",))
    install_id = manager.list_installed("user")[0]["install_id"]
    record = _record_for(manager, row["id"])
    assert install_id != migration["candidate"]
    assert record["install_id"] == migration["candidate"]  # not transferred
    assert not hosted._has_authority(record, hosted._install_ids(manager, "user"))
    assert HostedState(manager.store_root).read()["install_id_migrations"] == {}


def _pending_scripted_recovery(tmp_path: Path, monkeypatch, market: FakeMarketplace):
    """An interrupted v1 -> v2 update (fresh optional, scripts) whose
    restore lacks every durable consent."""
    _no_project(monkeypatch)
    v1 = market.publish("scripted", "1.0.0", body="one", scripts=True)
    manager, _, _ = _manager(tmp_path, market)
    first = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes",
                flags={"allow_scripts": True})
    v2 = market.publish("scripted", "2.0.0", body="two", scripts=True,
                        package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes",
                    flags={"allow_scripts": True})
    with HostedState(manager.store_root).edit() as state:
        [op] = state["pull_ops"].values()
        op["consent"] = {}
        for prior in op["before"].values():
            prior["consented"] = []
    return v1, manager


def test_pull_and_daemon_report_a_pending_recovery_with_the_flag_that_settles_it(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, caplog
):
    import logging

    v1, manager = _pending_scripted_recovery(tmp_path, monkeypatch, market)
    report = hosted.pull(manager, market.client(), install_optional=lambda view: False)
    [entry] = report.recovery
    assert entry["flags"] == ["--allow-scripts"]
    assert report.as_dict()["recovery"] == report.recovery
    # The daemon logs it like the other notify-only items.
    from sessionfs.daemon.skill_reconcile import _log_report

    with caplog.at_level(logging.WARNING, logger="sessionfs.daemon.skill_reconcile"):
        _log_report(report)
    assert any("run `sfs skill pull --allow-scripts`" in r.getMessage() for r in caplog.records)
    # Pulling with the named flag settles it (and the entry is gone).
    report = hosted.pull(manager, market.client(), install_optional=lambda view: False,
                         flags={"allow_scripts": True})
    assert report.recovery == []
    assert HostedState(manager.store_root).read()["pull_ops"] == {}
    assert manager.list_installed("user")[0]["content_hash"] == v1["content_hash"]


def test_skill_pull_prints_a_pending_recovery(tmp_path: Path, monkeypatch, market: FakeMarketplace):
    _v1, manager = _pending_scripted_recovery(tmp_path, monkeypatch, market)
    _cli_env(tmp_path, monkeypatch, market.client())
    monkeypatch.setattr(SkillManager, "__init__", _bind_online_manager(manager, market))
    output = " ".join(runner.invoke(skill_app, ["pull"]).output.split())  # undo wrapping
    assert "could not be settled because consent for skill-scripts is missing" in output
    assert "sfs skill pull --allow-scripts" in output


def _bind_online_manager(manager: SkillManager, market: FakeMarketplace):
    real_init = SkillManager.__init__

    def _init(self, **kwargs):
        real_init(
            self, project_root=manager.project_root, home_dir=manager.home_dir,
            store_root=manager.store_root, hosted_client=market.client(),
        )

    return _init


# ---------------------------------------------------------------------------
# round 33: recovery acts only on stamped rows and persisted authorization
# ---------------------------------------------------------------------------


def test_recovery_restores_only_the_tools_each_origin_authorized(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch, "proj_1")
    item = market.publish("scripted", "1.0.0", scripts=True)
    market.requests = [
        _request(item, "a", tools=["codex"], caps=["instructions", "scripts"]),
        _request(item, "b", tools=["claude-code"], caps=["instructions"]),
    ]
    manager, _, home = _manager(tmp_path, market)
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), flags={"allow_scripts": True})
    [op] = HostedState(manager.store_root).read()["pull_ops"].values()
    assert op["authorized"] == {
        "request|sir_" + "a" * 24: ["codex"], "request|sir_" + "b" * 24: [],
    }
    hosted._recover_locally(
        manager, market.client(), ("user",), consent={"allow_scripts": True}
    )
    # Request b never approved scripts: claude-code is not installed by
    # recovery either, although b targets it and the op had --allow-scripts.
    assert _targets(manager) == ["codex"]
    assert not _claude_file(home, "scripted").exists()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}
    assert _record_for(manager, "sir_" + "b" * 24)["targets"] == []


def test_every_pull_mutation_stamps_the_row_and_a_user_compile_clears_it(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    required = market.assign(item, tools=["codex"])
    hosted.pull(manager, market.client())
    # A retarget by pull (an optional origin adds claude-code) is stamped too.
    market.assign(item, mode="optional", tools=["claude-code"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    [token] = HostedState(manager.store_root).read()["pull_ops"]
    [row] = manager.list_installed("user")
    assert row["pull_op"] == token and sorted(row["targets"]) == ["claude-code", "codex"]
    # The user then changes the installation themselves: it is no longer
    # the operation's change, so recovery must leave it exactly as it is.
    manager.compile(row["package_id"], scope="user", targets=["gemini"])
    assert "pull_op" not in manager.list_installed("user")[0]
    hosted._recover_locally(manager, market.client(), ("user",))
    assert _targets(manager) == ["claude-code", "codex", "gemini"]
    assert _claude_file(home).is_file()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}  # stale intent dropped
    assert _record_for(manager, required["id"]) is not None  # untouched


def test_a_change_after_the_intent_but_before_the_install_is_never_reverted(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, mode="optional", tools=["codex"])
    # Crash right after the intent is written, before anything is installed.
    _crash_once(monkeypatch, hosted, "_install_item")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert HostedState(manager.store_root).read()["pull_ops"]
    # Same install id as before the op, but the USER changed it.
    package_id = manager.list_installed("user")[0]["package_id"]
    manager.compile(package_id, scope="user", targets=["claude-code"])
    hosted._recover_locally(manager, market.client(), ("user",))
    assert _targets(manager) == ["claude-code", "codex"]
    assert _claude_file(home).is_file()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_cli_skill_add_rotates_the_install_id_in_its_own_commit(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    [token] = HostedState(manager.store_root).read()["pull_ops"]
    assert manager.list_installed("user")[0]["install_id"] == token

    def _crash(*args, **kwargs):
        raise RuntimeError("crash before the cleanup")

    monkeypatch.setattr(hosted, "retire_pull_authority", _crash)
    _cli_env(tmp_path, monkeypatch, market.client())
    result = runner.invoke(
        skill_app, ["add", "sessionfs:acme/review-loop", "--for", "codex", "--scope", "user",
                    "--yes"],
    )
    assert result.exit_code != 0  # the cleanup crashed ...
    [row] = manager.list_installed("user")
    assert row["install_id"] != token  # ... but the add already rotated the id
    monkeypatch.undo()
    hosted._recover_locally(manager, market.client(), ("user",))
    assert manager.list_installed("user")[0]["install_id"] == row["install_id"]
    assert _skill_file(home).is_file()  # the user's install is kept


def test_recovery_keeps_an_installation_another_identity_recorded(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    optional = market.assign(item, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    [token] = HostedState(manager.store_root).read()["pull_ops"]
    [row] = manager.list_installed("user")
    # Before recovery, another account records a required origin on it.
    with HostedState(manager.store_root).edit() as state:
        state["pulled"]["https://sfs.test|usr_other|user|~|assignment|osa_x"] = {
            "mode": "required", "namespace": "https://sfs.test|usr_other",
            "role": "reference", "install_id": token, "scope": "user", "project_root": None,
            "origin": "assignment", "origin_id": "osa_x", "version_id": item["version_id"],
            "content_hash": item["content_hash"], "targets": ["codex"],
            "package_id": item["package_id"], "local_package_id": row["package_id"],
            "installed_at": "2026-10-18T00:00:00+00:00",
        }
    hosted._recover_locally(manager, market.client(), ("user",))
    assert manager.list_installed("user")[0]["install_id"] == token  # kept
    assert _skill_file(home).is_file()
    state = HostedState(manager.store_root).read()
    assert state["pull_ops"] == {}  # only the intent was retired
    assert _record_for(manager, optional["id"]) is None  # the acceptance was never recorded


def test_project_recovery_state_is_settled_only_in_its_own_project(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch, "proj_1")
    item = market.publish("review-loop", "1.0.0")
    request = _request(item, "a", tools=["codex"], caps=["instructions"], scope="project")
    market.requests = [request]
    manager, project, _ = _manager(tmp_path, market)
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client())
    hosted_state = HostedState(manager.store_root)
    [op] = hosted_state.read()["pull_ops"].values()
    assert op["project_root"] == str(project)
    # Interrupted migrations of project A: a new entry, and a legacy one
    # whose root is only in its key.
    with hosted_state.edit() as state:
        state["install_id_migrations"][f"project|{project}|skp_new"] = {
            "scope": "project", "local_package_id": "skp_new", "candidate": "sfi_new",
            "project_root": str(project),
        }
        state["install_id_migrations"][f"project|{project}|skp_old"] = {
            "scope": "project", "local_package_id": "skp_old", "candidate": "sfi_old",
        }
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = SkillManager(
        project_root=other_root, home_dir=manager.home_dir, store_root=manager.store_root,
        hosted_client=market.client(),
    )
    hosted._recover_locally(other, market.client(), ("project", "user"))
    state = hosted_state.read()
    assert len(state["pull_ops"]) == 1 and len(state["install_id_migrations"]) == 2
    assert other.list_installed("project") == []
    # In project A they are settled.
    hosted._recover_locally(manager, market.client(), ("project", "user"))
    state = hosted_state.read()
    assert state["pull_ops"] == {} and state["install_id_migrations"] == {}
    assert _record_for(manager, request["id"]) is not None


def test_recovery_records_and_ends_the_intent_only_after_the_restore_succeeded(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    required = market.assign(item, tools=["codex"])
    market.assign(item, mode="optional", tools=["claude-code"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    # The corrective install crashes during recovery.
    _crash_once(monkeypatch, SkillManager, "install_prepared")
    with pytest.raises(RuntimeError):
        hosted._recover_locally(manager, market.client(), ("user",))
    state = HostedState(manager.store_root).read()
    assert state["pull_ops"]  # kept: nothing was recorded yet
    assert _record_for(manager, required["id"]) is None
    hosted._recover_locally(manager, market.client(), ("user",))
    assert _targets(manager) == ["codex"] and not _claude_file(home).exists()
    assert _record_for(manager, required["id"]) is not None
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_a_state_write_error_never_ends_an_install_id_migration(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(item)
    hosted.daemon_reconcile(manager, market.client())
    _strip_install_ids(manager)
    real_atomic = state_module.atomic_json

    def _disk_full(path, value):
        if Path(path).name == "skills.state.json":
            raise OSError(28, "No space left on device")
        return real_atomic(path, value)

    monkeypatch.setattr(state_module, "atomic_json", _disk_full)
    with pytest.raises(SkillError):
        manager.state_store("user").ensure_install_id(
            manager.list_installed("user")[0]["package_id"], "sfi_x"
        )
    hosted.daemon_reconcile(manager, market.client())  # the migration fails: deferred
    [migration] = HostedState(manager.store_root).read()["install_id_migrations"].values()
    monkeypatch.undo()
    hosted.daemon_reconcile(manager, market.client())
    assert manager.list_installed("user")[0]["install_id"] == migration["candidate"]
    assert _record_for(manager, row["id"])["install_id"] == migration["candidate"]
    assert HostedState(manager.store_root).read()["install_id_migrations"] == {}


def _feed_down(monkeypatch) -> None:
    def _down(*args, **kwargs):
        raise SkillError("Hosted marketplace unreachable")

    monkeypatch.setattr(hosted, "sync_revocations", _down)


@pytest.mark.parametrize("json_output", [False, True])
def test_skill_pull_shows_a_pending_recovery_even_when_the_network_fails(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, json_output: bool
):
    _v1, manager = _pending_scripted_recovery(tmp_path, monkeypatch, market)
    _feed_down(monkeypatch)
    _cli_env(tmp_path, monkeypatch, market.client())
    monkeypatch.setattr(SkillManager, "__init__", _bind_online_manager(manager, market))
    result = runner.invoke(skill_app, ["pull", *(["--json"] if json_output else [])])
    assert result.exit_code != 0
    output = " ".join(result.output.split())
    assert "unreachable" in output
    assert "run `sfs skill pull --allow-scripts` to settle it" in output


def test_the_daemon_logs_a_pending_recovery_even_when_the_network_fails(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, caplog
):
    import logging

    from sessionfs.daemon.skill_reconcile import SkillReconciler

    _v1, manager = _pending_scripted_recovery(tmp_path, monkeypatch, market)
    _feed_down(monkeypatch)
    reconciler = SkillReconciler(
        manager_factory=lambda: manager, client_factory=lambda url, key: market.client(),
    )
    with caplog.at_level(logging.WARNING, logger="sfsd.skills"):
        assert reconciler.maybe_run(
            enabled=True, api_url="https://sfs.test", api_key="sk_test", background=False
        ) is None
    messages = [record.getMessage() for record in caplog.records]
    assert any("run `sfs skill pull --allow-scripts`" in message for message in messages)
    assert reconciler.failures == 1


# ---------------------------------------------------------------------------
# round 34: full prior identity, user-owned tools, strict before-image
# ---------------------------------------------------------------------------


def test_a_rollback_restores_the_prior_version_identity_and_provenance(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    # Identical bytes republished as another version with other provenance.
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, _ = _manager(tmp_path, market)
    first = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    before = manager.list_installed("user")[0]
    v2 = market.publish("review-loop", "1.0.0", body="one", package_id=v1["package_id"],
                        visibility="org")
    assert v2["content_hash"] == v1["content_hash"] and v2["version_id"] != v1["version_id"]
    market.assignments.remove(first)
    market.assign(v2, mode="optional", tools=["codex"])
    monkeypatch.setattr(hosted, "_same_bytes_elsewhere", lambda rows, item, origin: None)
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    during = manager.list_installed("user")[0]
    assert during["source"]["commit"] == v2["version_id"]
    assert during["content_hash"] == before["content_hash"]  # "looks" restored
    [op] = HostedState(manager.store_root).read()["pull_ops"].values()
    [prior] = op["before"].values()
    assert prior["source"] == before["source"] and prior["provenance"] == before.get("provenance")
    hosted._recover_locally(manager, market.client(), ("user",))
    [row] = manager.list_installed("user")
    assert row["source"] == before["source"]  # v1 again, not v2 under v1's hash
    assert row.get("provenance") == before.get("provenance")
    assert HostedState(manager.store_root).read()["pull_ops"] == {}




def test_an_unreadable_state_aborts_before_any_intent_is_written(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, _ = _manager(tmp_path, market)
    first = market.assign(v1, tools=["codex"])
    hosted.pull(manager, market.client())
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, tools=["codex"])
    real_rows = hosted._installed_rows

    def _flaky(manager, scope, *, strict=False):
        if strict:
            raise SkillError("skills.state.json must contain a packages object")
        return real_rows(manager, scope, strict=strict)

    monkeypatch.setattr(hosted, "_installed_rows", _flaky)
    report = hosted.pull(manager, market.client())
    [item] = report.items
    assert item.status == "failed"
    assert HostedState(manager.store_root).read()["pull_ops"] == {}
    assert manager.list_installed("user")[0]["content_hash"] == v1["content_hash"]


def test_skill_pull_json_announces_a_pending_recovery_before_a_hard_stop(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _v1, manager = _pending_scripted_recovery(tmp_path, monkeypatch, market)

    def _killed(*args, **kwargs):
        raise SystemExit(3)  # not an Exception: no error handler sees it

    monkeypatch.setattr(hosted, "sync_revocations", _killed)
    _cli_env(tmp_path, monkeypatch, market.client())
    monkeypatch.setattr(SkillManager, "__init__", _bind_online_manager(manager, market))
    result = runner.invoke(skill_app, ["pull", "--json"])
    assert result.exit_code == 3
    assert "run `sfs skill pull --allow-scripts` to settle it" in " ".join(
        result.stderr.split()
    )
    assert "settle it" not in result.stdout


def test_the_daemon_logs_a_pending_recovery_at_once_and_only_once(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, caplog
):
    import logging

    from sessionfs.daemon.skill_reconcile import SkillReconciler

    _v1, manager = _pending_scripted_recovery(tmp_path, monkeypatch, market)
    reconciler = SkillReconciler(
        manager_factory=lambda: manager, client_factory=lambda url, key: market.client(),
    )

    def _hint_count() -> int:
        return sum(
            "run `sfs skill pull --allow-scripts`" in record.getMessage()
            for record in caplog.records
        )

    with caplog.at_level(logging.WARNING, logger="sfsd.skills"):
        real_sync = hosted.sync_revocations
        seen: list[int] = []

        def _sync(*args, **kwargs):
            seen.append(_hint_count())  # already logged before any network work
            return real_sync(*args, **kwargs)

        monkeypatch.setattr(hosted, "sync_revocations", _sync)
        report = reconciler.maybe_run(
            enabled=True, api_url="https://sfs.test", api_key="sk_test", background=False
        )
    assert report is not None and report.recovery  # still pending (no consent)
    assert seen == [1]
    assert _hint_count() == 1  # not logged again by the final report


# ---------------------------------------------------------------------------
# round 35: bound acceptance, ownership ledger, exact provenance, strict
# rows, fenced unassignment
# ---------------------------------------------------------------------------


def _edit_user_state(manager: SkillManager, change) -> None:
    store = manager.state_store("user")
    state = store.state()
    change(state["packages"])
    state_module.atomic_json(store.state_path, state)


@pytest.mark.parametrize("change", ["wider tools", "other content"])
def test_an_optional_acceptance_covers_only_what_was_accepted(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, change: str
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    record = _record_for(manager, row["id"])
    assert (record["accepted_version_id"], record["accepted_content_hash"],
            record["accepted_targets"]) == (v1["version_id"], v1["content_hash"], ["codex"])
    # The SAME assignment row changes after the acceptance.
    if change == "wider tools":
        row["target_tools"] = ["codex", "claude-code"]
    else:
        v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
        row["version_id"], row["content_hash"] = v2["version_id"], v2["content_hash"]
    asked: list[str] = []

    def _decline(view):
        asked.append(view.version_id)
        return False

    report = hosted.pull(manager, market.client(), install_optional=_decline)
    assert asked  # fresh consent was required
    assert report.items[0].status in {"offered", "up-to-date"}
    assert not _claude_file(home).exists()
    assert "one" in _skill_file(home).read_text()  # nothing changed without consent
    # Re-pulling without any change still honors the original acceptance.
    if change == "wider tools":
        row["target_tools"] = ["codex"]
        asked.clear()
        report = hosted.pull(manager, market.client(), install_optional=_decline)
        assert asked == [] and report.items[0].status == "up-to-date"




def test_a_rollback_omits_provenance_the_prior_row_did_not_have(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    # An older row without provenance.
    _edit_user_state(manager, lambda packages: [
        row.pop("provenance", None) for row in packages.values()
    ])
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert "provenance" in manager.list_installed("user")[0]  # v2's
    hosted._recover_locally(manager, market.client(), ("user",))
    [row] = manager.list_installed("user")
    assert row["content_hash"] == v1["content_hash"]
    assert "provenance" not in row  # absent again - not an empty record
    assert "one" in _skill_file(home).read_text()


@pytest.mark.parametrize("damage", ["not an object", "other package id", "bad install id"])
def test_a_malformed_row_aborts_before_any_intent_is_written(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, damage: str
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, _ = _manager(tmp_path, market)
    first = market.assign(v1, tools=["codex"])
    hosted.pull(manager, market.client())
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, tools=["codex"])

    def _damage(packages):
        if damage == "not an object":
            packages["skp_broken"] = ["not", "a", "row"]
        elif damage == "other package id":
            packages["skp_broken"] = {**next(iter(packages.values())), "package_id": "skp_x"}
        else:
            packages["skp_broken"] = {
                **next(iter(packages.values())), "package_id": "skp_broken", "install_id": 42,
            }

    _edit_user_state(manager, _damage)
    with pytest.raises(SkillError):
        hosted._installed_rows(manager, "user", strict=True)
    report = hosted.pull(manager, market.client())
    assert report.items[0].status == "failed"
    assert HostedState(manager.store_root).read()["pull_ops"] == {}
    assert "one" in _skill_file(manager.home_dir).read_text()


def _unassigned_setup(tmp_path: Path, market: FakeMarketplace):
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(item, tools=["codex"])
    hosted.daemon_reconcile(manager, market.client())
    market.assignments.remove(row)
    return manager, row


def test_a_stale_unassignment_pass_is_recomputed_not_written(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, row = _unassigned_setup(tmp_path, market)
    real = hosted._installed_rows
    raced: list[int] = []

    def _racing(manager_, scope, *, strict=False):
        if strict and not raced:
            # A concurrent bookkeeping write lands between the snapshot and
            # the fenced write (the scope/operation locks are held here).
            raced.append(1)
            with HostedState(manager_.store_root).edit() as state:
                state["pulled_generation"] = int(state.get("pulled_generation") or 0) + 1
                key, record = next(iter(state["pulled"].items()))
                # Another (also stale) origin of the same package is recorded.
                state["pulled"][key.replace(row["id"], "osa_late")] = {
                    **record, "origin_id": "osa_late",
                }
        return real(manager_, scope, strict=strict)

    monkeypatch.setattr(hosted, "_installed_rows", _racing)
    report = hosted.daemon_reconcile(manager, market.client())
    assert raced == [1]
    # The pass decided on the old snapshot was not written; the recomputed
    # one sees both stale origins and reports the package.
    assert [entry["package_id"] for entry in report.unassigned] == [
        manager.list_installed("user")[0]["package_id"]
    ]
    assert _record_for(manager, row["id"])["unassigned_since"]
    assert _record_for(manager, "osa_late")["unassigned_since"]


def test_unassignment_runs_under_the_scope_and_operation_locks(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, _row = _unassigned_setup(tmp_path, market)
    held: list[bool] = []
    real_edit = HostedState.edit

    def _edit(self):
        held.append(bool(getattr(hosted._SCOPE_LOCKS, "held", {}) and any(
            hosted._SCOPE_LOCKS.held.values()
        )))
        return real_edit(self)

    monkeypatch.setattr(hosted, "_report_unassigned_scope", hosted._report_unassigned_scope)
    real_scope = hosted._report_unassigned_scope

    def _spy(*args, **kwargs):
        monkeypatch.setattr(HostedState, "edit", _edit)
        try:
            return real_scope(*args, **kwargs)
        finally:
            monkeypatch.setattr(HostedState, "edit", real_edit)

    monkeypatch.setattr(hosted, "_report_unassigned_scope", _spy)
    report = hosted.daemon_reconcile(manager, market.client())
    assert report.unassigned and held and all(held)


def test_an_unreadable_state_never_drops_unassigned_records(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, row = _unassigned_setup(tmp_path, market)
    real = hosted._installed_rows

    def _unreadable(manager_, scope, *, strict=False):
        if strict:
            raise SkillError("skills.state.json must contain a packages object")
        return real(manager_, scope, strict=strict)

    monkeypatch.setattr(hosted, "_installed_rows", _unreadable)
    report = hosted.daemon_reconcile(manager, market.client())
    assert report.unassigned == []
    assert any("were not checked" in note for note in report.notes)
    assert _record_for(manager, row["id"]) is not None  # kept, not dropped


# ---------------------------------------------------------------------------
# round 36: recorded acceptance must be this op's, user tools survive a
# version update, ledgers need installer authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["new version", "wider tools"])
def test_an_older_acceptance_never_counts_as_the_interrupted_ops(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, change: str
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    if change == "new version":
        v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
        row["version_id"], row["content_hash"] = v2["version_id"], v2["content_hash"]
    else:
        row["target_tools"] = ["codex", "claude-code"]
    # The user accepts the change; the pull crashes before recording it.
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    hosted._recover_locally(manager, market.client(), ("user",))
    # The v1 / codex-only acceptance on the record is not the op's: the
    # unrecorded acceptance is rolled back.
    [installed] = manager.list_installed("user")
    assert installed["content_hash"] == v1["content_hash"]
    assert sorted(installed["targets"]) == ["codex"]
    assert "one" in _skill_file(home).read_text() and not _claude_file(home).exists()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}



# ---------------------------------------------------------------------------
# round 37: pull never removes a tool; version updates need approval for
# every installed tool; recovery evidence is the op's own stamp
# ---------------------------------------------------------------------------


def test_pull_reports_tools_no_longer_assigned_and_never_removes_them(
    tmp_path: Path, monkeypatch, market: FakeMarketplace, caplog
):
    import logging

    from sessionfs.daemon.skill_reconcile import _log_report

    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(item, tools=["codex", "claude-code"])
    hosted.pull(manager, market.client())
    row["target_tools"] = ["codex"]  # narrowed
    report = hosted.pull(manager, market.client())
    assert _targets(manager) == ["claude-code", "codex"]
    assert _claude_file(home).is_file()
    assert report.as_dict()["unassigned_tools"][0]["tools"] == ["claude-code"]
    with caplog.at_level(logging.INFO, logger="sfsd.skills"):
        _log_report(report)
    assert any(
        "claude-code no longer assigned; still installed" in record.getMessage()
        for record in caplog.records
    )
    # The user removes it themselves: no longer reported.
    package_id = manager.list_installed("user")[0]["package_id"]
    manager.compile(package_id, scope="user", drop=["claude-code"])
    assert hosted.pull(manager, market.client()).unassigned_tools == []
    # Assigned again: not reported either.
    row["target_tools"] = ["codex", "claude-code"]
    assert hosted.pull(manager, market.client()).unassigned_tools == []


def test_skill_pull_prints_tools_that_are_no_longer_assigned(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(item, tools=["codex", "claude-code"])
    hosted.pull(manager, market.client())
    row["target_tools"] = ["codex"]
    _cli_env(tmp_path, monkeypatch, market.client())
    monkeypatch.setattr(SkillManager, "__init__", _bind_online_manager(manager, market))
    output = " ".join(runner.invoke(skill_app, ["pull"]).output.split())
    package_id = manager.list_installed("user")[0]["package_id"]
    assert "claude-code no longer assigned - still installed" in output
    assert f"sfs skill compile {package_id} --scope user --drop claude-code" in output


def test_a_version_update_needs_approval_for_every_installed_tool(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(v1, tools=["codex", "claude-code"])
    hosted.pull(manager, market.client())
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, tools=["codex"])  # v2 approved only for codex
    report = hosted.pull(manager, market.client())
    [entry] = report.items
    package_id = manager.list_installed("user")[0]["package_id"]
    assert entry.status == "skipped"
    assert "2.0.0 is approved only for codex; claude-code still has 1.0.0" in entry.detail
    assert f"sfs skill compile {package_id} --scope user --drop claude-code" in entry.detail
    # Nothing got v2, nothing was removed.
    assert manager.list_installed("user")[0]["content_hash"] == v1["content_hash"]
    assert "one" in _claude_file(home).read_text() and "one" in _skill_file(home).read_text()
    # Once the user drops claude-code, the update goes ahead.
    manager.compile(package_id, scope="user", drop=["claude-code"])
    report = hosted.pull(manager, market.client())
    assert report.items[0].status == "installed"
    [row] = manager.list_installed("user")
    assert row["content_hash"] == v2["content_hash"] and row["targets"] == ["codex"]


def test_a_version_update_keeps_a_hand_added_tool_only_when_approved_for_it(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(v1, tools=["codex"])
    hosted.pull(manager, market.client())
    package_id = manager.list_installed("user")[0]["package_id"]
    manager.compile(package_id, scope="user", targets=["claude-code"])  # by hand
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    second = market.assign(v2, tools=["codex"])
    report = hosted.pull(manager, market.client())
    assert report.items[0].status == "skipped"  # claude-code is not approved for v2
    assert "one" in _claude_file(home).read_text()
    second["target_tools"] = ["codex", "claude-code"]  # approved now
    report = hosted.pull(manager, market.client())
    assert report.items[0].status == "installed"
    assert sorted(manager.list_installed("user")[0]["targets"]) == ["claude-code", "codex"]
    assert "two" in _claude_file(home).read_text()


def test_an_update_the_kept_origins_alone_would_not_allow_is_rolled_back(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(v1, tools=["codex", "claude-code"])
    hosted.pull(manager, market.client())
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, tools=["codex"])
    market.assign(v2, mode="optional", tools=["claude-code"])  # only it covers claude-code
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert "two" in _claude_file(home).read_text()
    hosted._recover_locally(manager, market.client(), ("user",))
    [row] = manager.list_installed("user")
    # Without the unrecorded acceptance claude-code had no approval for v2:
    # back to v1 for both tools (nothing removed).
    assert row["content_hash"] == v1["content_hash"]
    assert sorted(row["targets"]) == ["claude-code", "codex"]
    assert "one" in _claude_file(home).read_text()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_recovery_records_a_non_fresh_origin_unless_this_op_recorded_it(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, _ = _manager(tmp_path, market)
    row = market.assign(v1, tools=["codex"])
    hosted.pull(manager, market.client())
    # The SAME required assignment moves to v2; the pull crashes before
    # recording, leaving v1's record (same origin, same install id).
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    row["version_id"], row["content_hash"] = v2["version_id"], v2["content_hash"]
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client())
    [token] = HostedState(manager.store_root).read()["pull_ops"]
    assert _record_for(manager, row["id"])["version_id"] == v1["version_id"]  # stale
    hosted._recover_locally(manager, market.client(), ("user",))
    record = _record_for(manager, row["id"])
    assert record["version_id"] == v2["version_id"]  # recorded by the recovery
    assert record["pull_op"] == token
    assert manager.list_installed("user")[0]["content_hash"] == v2["content_hash"]
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


# ---------------------------------------------------------------------------
# round 38: another account's record claims only its own, current tools
# ---------------------------------------------------------------------------


def _foreign_record(manager: SkillManager, item: dict, origin: str, **fields) -> None:
    """Another account's record bound to the current user-scope install."""
    [row] = manager.list_installed("user")
    record = {
        "mode": "required", "namespace": "https://sfs.test|usr_other",
        "role": "reference", "install_id": row["install_id"], "scope": "user",
        "project_root": None, "origin": origin, "origin_id": f"x_{origin}",
        "version_id": item["version_id"], "content_hash": item["content_hash"],
        "targets": ["codex"], "package_id": item["package_id"],
        "local_package_id": row["package_id"], "installed_at": "2026-10-18T00:00:00+00:00",
        **fields,
    }
    with HostedState(manager.store_root).edit() as state:
        state["pulled"][f"https://sfs.test|usr_other|user|~|{origin}|x_{origin}"] = record


def _interrupted_update(tmp_path: Path, monkeypatch, market: FakeMarketplace, *, tools):
    """v1 (optional, accepted, codex) then an unrecorded fresh acceptance of
    v2 for `tools`, interrupted before recording."""
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, mode="optional", tools=tools)
    return v1, v2, manager, home


def test_a_stale_foreign_record_of_an_older_version_claims_nothing(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    v1, v2, manager, home = _interrupted_update(tmp_path, monkeypatch, market, tools=["codex"])
    _foreign_record(manager, v1, "assignment")  # another account, but for v1
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert "two" in _skill_file(home).read_text()
    hosted._recover_locally(manager, market.client(), ("user",))
    # The unrecorded v2 acceptance is rolled back despite the v1 reference.
    assert manager.list_installed("user")[0]["content_hash"] == v1["content_hash"]
    assert "one" in _skill_file(home).read_text()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_an_unaccepted_foreign_optional_record_claims_nothing(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    v1, v2, manager, home = _interrupted_update(tmp_path, monkeypatch, market, tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    _foreign_record(manager, v2, "assignment", mode="optional")  # never accepted
    hosted._recover_locally(manager, market.client(), ("user",))
    assert manager.list_installed("user")[0]["content_hash"] == v1["content_hash"]


def test_a_current_foreign_assignment_keeps_only_its_own_tools(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex", "claude-code"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert _claude_file(home).is_file()
    # Another account's required assignment of this version, for codex only.
    _foreign_record(manager, item, "assignment")
    hosted._recover_locally(manager, market.client(), ("user",))
    # The installation stays for codex; claude-code existed only for the
    # unrecorded acceptance and goes through the normal corrective restore.
    assert _targets(manager) == ["codex"]
    assert _skill_file(home).is_file() and not _claude_file(home).exists()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_a_foreign_request_record_keeps_the_intent_pending(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    _foreign_record(manager, item, "request")  # its approval is not checkable here
    report = hosted.PullReport()
    hosted._recover_locally(manager, market.client(), ("user",), report=report)
    assert HostedState(manager.store_root).read()["pull_ops"]  # kept, not retired
    [entry] = report.recovery
    assert "cannot be checked locally" in entry["message"]
    assert _skill_file(home).is_file()  # nothing removed meanwhile


# ---------------------------------------------------------------------------
# round 39: pending recovery is never re-stamped; defunct foreign records
# authorize nothing; a skipped narrower version still reports unassigned tools
# ---------------------------------------------------------------------------


def test_a_pending_recovery_is_deferred_not_re_stamped_by_a_later_pull(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    """An interrupted v1->v2 update (row stamped, intent live) whose recovery
    stays pending must not be touched by a later pull in the same run: its
    recompile would replace the pending operation's stamp, destroying the
    evidence the rollback of the unrecorded acceptance needs."""
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(v1, mode="optional", tools=["codex"])
    hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, mode="optional", tools=["codex", "claude-code"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    [token] = HostedState(manager.store_root).read()["pull_ops"]
    [row] = manager.list_installed("user")
    assert row["pull_op"] == token
    assert sorted(row["targets"]) == ["claude-code", "codex"]
    # Another account's install request of this version cannot be checked
    # locally, so the recovery stays pending.
    _foreign_record(manager, v2, "request")
    # A required origin now asks for another tool of the same package+version.
    market.assign(v2, tools=["codex", "gemini"])
    report = hosted.pull(manager, market.client(), install_optional=lambda view: False)
    [entry] = report.recovery
    assert "cannot be checked locally" in entry["message"]
    # The item was DEFERRED, not reconciled: the row kept the pending
    # operation's stamp, version and tools, and no junk intent was added.
    assert report.items[0].status == "deferred"
    [row] = manager.list_installed("user")
    assert row["pull_op"] == token
    assert row["content_hash"] == v2["content_hash"]
    assert sorted(row["targets"]) == ["claude-code", "codex"]
    assert list(HostedState(manager.store_root).read()["pull_ops"]) == [token]
    # Once the foreign record is gone the next pull settles first (back to
    # v1 with the recorded tools), then the update can go ahead.
    with HostedState(manager.store_root).edit() as state:
        state["pulled"].pop("https://sfs.test|usr_other|user|~|request|x_request")
    report = hosted.pull(manager, market.client(), install_optional=lambda view: False)
    assert HostedState(manager.store_root).read()["pull_ops"] == {}
    assert report.items[0].status == "installed"
    assert _targets(manager) == ["codex", "gemini"]
    assert "two" in _skill_file(home).read_text()
    assert not _claude_file(home).exists()


def test_begin_pull_op_refuses_a_row_stamped_by_a_live_intent(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    """A new pull operation never starts over a row still stamped by another
    live intent (its mutation would re-stamp the row); an unrelated package
    is not blocked."""
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, _ = _manager(tmp_path, market)
    market.assign(v1, tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client())
    hosted_state = HostedState(manager.store_root)
    [token] = hosted_state.read()["pull_ops"]
    [row] = manager.list_installed("user")
    assert row["pull_op"] == token
    origin = hosted.canonical_origin(market.client().api_url)
    before = hosted._before_image_rows(manager, "user")

    def _work(version_id: str, package_id: str) -> hosted.PullItem:
        return hosted.PullItem(
            mode="required", version_id=version_id, content_hash="sha256:x",
            scope="user", targets=["codex"],
            origins=[("assignment", "osa_another")],
            origin_targets={"assignment|osa_another": ["codex"]},
            origin_modes={"assignment|osa_another": "required"},
            package_id=package_id,
        )

    def _begin(work: hosted.PullItem) -> str:
        return hosted._begin_pull_op(
            manager, hosted_state, namespace="https://sfs.test|usr_me", server=origin,
            work=work, fresh_keys=[], rows=before, consent={},
        )

    # The stamped row IS this version: refused while the intent lives.
    with pytest.raises(hosted.RecordingDeferred):
        _begin(_work(v1["version_id"], ""))
    # A version update would change the same row (all-or-nothing): refused too.
    with pytest.raises(hosted.RecordingDeferred):
        _begin(_work("skv_000000000000000000000099", v1["package_id"]))
    # An unrelated package is not blocked by it.
    other = _begin(_work("skv_000000000000000000000099", "spk_000000000000000000000099"))
    assert other in hosted_state.read()["pull_ops"]
    hosted._finish_pull_op(hosted_state, other)
    assert list(hosted_state.read()["pull_ops"]) == [token]


def test_an_unassigned_foreign_assignment_authorizes_no_recovery_keep(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    """A foreign record marked unassigned is stale: it keeps no tools, so an
    installation created only for an unrecorded acceptance is removed."""
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex", "claude-code"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    assert _claude_file(home).is_file()
    _foreign_record(manager, item, "assignment", unassigned_since="2026-10-18T01:00:00+00:00")
    hosted._recover_locally(manager, market.client(), ("user",))
    assert manager.list_installed("user") == []
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_an_accepted_foreign_optional_record_still_claims_its_tools(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    """The bound-acceptance path keeps working: another account's accepted
    optional assignment of this exact version claims its own tools."""
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex", "claude-code"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    _foreign_record(
        manager, item, "assignment", mode="optional",
        accepted_at="2026-10-18T00:30:00+00:00", accepted_via="yes",
        accepted_version_id=item["version_id"],
        accepted_content_hash=item["content_hash"], accepted_targets=["codex"],
    )
    hosted._recover_locally(manager, market.client(), ("user",))
    assert _targets(manager) == ["codex"]
    assert _skill_file(home).is_file() and not _claude_file(home).exists()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_a_foreign_optional_acceptance_with_an_opt_out_claims_nothing(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    """Same record as above, but its namespace opted out of this
    (origin kind, version) afterwards: the acceptance is void."""
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, home = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex", "claude-code"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    [row] = manager.list_installed("user")
    _foreign_record(
        manager, item, "assignment", mode="optional",
        accepted_at="2026-10-18T00:30:00+00:00", accepted_via="yes",
        accepted_version_id=item["version_id"],
        accepted_content_hash=item["content_hash"], accepted_targets=["codex"],
    )
    with HostedState(manager.store_root).edit() as state:
        key = hosted._opt_out_key(
            manager, "https://sfs.test|usr_other", "user", "assignment", item["version_id"]
        )
        state["opt_outs"][key] = {
            "pending": True, "package_id": item["package_id"],
            "local_package_id": row["package_id"], "version_id": item["version_id"],
            "created_at": "2026-10-18T01:00:00+00:00", "generation": 1,
        }
    hosted._recover_locally(manager, market.client(), ("user",))
    assert manager.list_installed("user") == []
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_an_unassigned_foreign_request_record_does_not_hold_recovery_pending(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    """A defunct request record neither authorizes tools nor holds the
    intent pending: the unrecorded fresh install is settled right away."""
    _no_project(monkeypatch)
    item = market.publish("review-loop", "1.0.0")
    manager, _, _ = _manager(tmp_path, market)
    market.assign(item, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    _foreign_record(manager, item, "request", unassigned_since="2026-10-18T01:00:00+00:00")
    report = hosted.PullReport()
    hosted._recover_locally(manager, market.client(), ("user",), report=report)
    assert HostedState(manager.store_root).read()["pull_ops"] == {}
    assert report.recovery == []
    assert manager.list_installed("user") == []


def test_a_skipped_narrower_version_still_reports_and_persists_unassigned_tools(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    """v1 was assigned for codex+claude-code; the same assignment moves to v2
    for codex only. The all-or-nothing update is skipped (claude-code is not
    approved for v2), but claude-code - which no live origin asks for any
    more - is still reported AND persisted, so later pulls keep reporting it
    even though the installed version's record can no longer name it."""
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    row = market.assign(v1, tools=["codex", "claude-code"])
    hosted.pull(manager, market.client())
    v2 = market.publish("review-loop", "2.0.0", body="two", package_id=v1["package_id"])
    row["version_id"], row["content_hash"] = v2["version_id"], v2["content_hash"]
    row["target_tools"] = ["codex"]  # v2 assigned for codex only
    report = hosted.pull(manager, market.client())
    [entry] = report.items
    assert entry.status == "skipped"
    # claude-code keeps v1 and is reported (and remembered) as unassigned.
    assert entry.unassigned_tools == ["claude-code"]
    assert report.unassigned_tools[0]["tools"] == ["claude-code"]
    assert _record_for(manager, row["id"])["unassigned_targets"] == ["claude-code"]
    assert _targets(manager) == ["claude-code", "codex"]
    assert "one" in _claude_file(home).read_text()
    # Reported again by the next pull - not lost - until the user drops it.
    assert hosted.pull(manager, market.client()).unassigned_tools[0]["tools"] == ["claude-code"]
    package_id = manager.list_installed("user")[0]["package_id"]
    manager.compile(package_id, scope="user", drop=["claude-code"])
    report = hosted.pull(manager, market.client())
    assert report.unassigned_tools == []
    assert report.items[0].status == "installed"
    assert _targets(manager) == ["codex"]
    assert "two" in _skill_file(home).read_text()


# ---------------------------------------------------------------------------
# recovery of a renaming update and of a crashed corrective install
# ---------------------------------------------------------------------------


def _renaming_update_crashed_before_recording(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    """v1 `review-loop` on codex (required); v2 renames the skill and is
    assigned required for claude-code only plus optional for codex. The
    pull updates the row (moving it to the new local id) on both tools and
    crashes before recording anything."""
    _no_project(monkeypatch)
    v1 = market.publish("review-loop", "1.0.0", body="one")
    manager, _, home = _manager(tmp_path, market)
    first = market.assign(v1, tools=["codex"])
    hosted.pull(manager, market.client())
    [before] = manager.list_installed("user")
    v2 = market.publish("review-buddy", "2.0.0", body="two", package_id=v1["package_id"])
    market.assignments.remove(first)
    market.assign(v2, tools=["claude-code"])
    market.assign(v2, mode="optional", tools=["codex"])
    _crash_once(monkeypatch, hosted, "_record_origins")
    with pytest.raises(RuntimeError):
        hosted.pull(manager, market.client(), install_optional=lambda view: "yes")
    [row] = manager.list_installed("user")
    assert row["package_id"] != before["package_id"]  # moved to the new id
    assert row["install_id"] == before["install_id"]
    assert sorted(row["targets"]) == ["claude-code", "codex"]
    assert row["content_hash"] == v2["content_hash"]
    return manager, home, v1, before


def test_recovery_of_a_renaming_update_keeps_prior_tools_and_reverts(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, home, v1, before = _renaming_update_crashed_before_recording(
        tmp_path, monkeypatch, market
    )
    hosted._recover_locally(manager, market.client(), ("user",))
    # codex existed before the op: it is never dropped; the update is undone
    # (codex was approved for v2 only by the unrecorded acceptance).
    [row] = manager.list_installed("user")
    assert row["content_hash"] == v1["content_hash"]
    assert row["package_id"] == before["package_id"]
    assert row["install_id"] == before["install_id"]
    assert row["targets"] == ["codex"]
    assert "one" in _skill_file(home).read_text()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_a_crash_inside_a_revert_restore_re_applies_the_same_decision(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, home, v1, before = _renaming_update_crashed_before_recording(
        tmp_path, monkeypatch, market
    )
    real_install = SkillManager.install_prepared
    calls = {"n": 0}

    def _commit_then_crash(self, *args, **kwargs):
        result = real_install(self, *args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash right after the corrective install")
        return result

    monkeypatch.setattr(SkillManager, "install_prepared", _commit_then_crash)
    with pytest.raises(RuntimeError):
        hosted._recover_locally(manager, market.client(), ("user",))
    [half] = manager.list_installed("user")
    assert half["content_hash"] == v1["content_hash"] and half["targets"] == ["codex"]
    [op] = HostedState(manager.store_root).read()["pull_ops"].values()
    assert op["restore"]["to"] == "prior" and op["restore"]["targets"] == ["codex"]
    # The next pass re-applies exactly that decision: claude-code (approved
    # only for v2) never gets v1, and the end state is the same as an
    # uninterrupted recovery.
    hosted._recover_locally(manager, market.client(), ("user",))
    [row] = manager.list_installed("user")
    assert row["content_hash"] == v1["content_hash"]
    assert row["package_id"] == before["package_id"]
    assert row["targets"] == ["codex"]
    assert not _claude_file(home).exists() and not _claude_file(home, "review-buddy").exists()
    assert "one" in _skill_file(home).read_text()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}
    assert calls["n"] == 1  # nothing left to install on the second pass
    hosted._recover_locally(manager, market.client(), ("user",))  # idempotent
    assert manager.list_installed("user") == [row]


def test_a_recovery_pass_holding_a_stale_intent_replays_the_saved_decision(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    # Pass A saves a revert-to-prior decision, installs it and dies. Pass B
    # read the intent BEFORE A saved its decision and only now gets the
    # locks: it must act on the live intent (A's decision), never recompute
    # from its stale copy and hand v1 to a tool approved only for v2.
    import copy

    manager, home, v1, before = _renaming_update_crashed_before_recording(
        tmp_path, monkeypatch, market
    )
    stale = copy.deepcopy(HostedState(manager.store_root).read())
    real_install = SkillManager.install_prepared
    calls = {"n": 0}

    def _commit_then_crash(self, *args, **kwargs):
        result = real_install(self, *args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash right after the corrective install")
        return result

    monkeypatch.setattr(SkillManager, "install_prepared", _commit_then_crash)
    with pytest.raises(RuntimeError):
        hosted._recover_locally(manager, market.client(), ("user",))
    [op] = HostedState(manager.store_root).read()["pull_ops"].values()
    assert op["restore"]["to"] == "prior"

    real_read = HostedState.read
    reads = {"n": 0}

    def _first_read_is_stale(self):
        reads["n"] += 1
        return copy.deepcopy(stale) if reads["n"] == 1 else real_read(self)

    monkeypatch.setattr(HostedState, "read", _first_read_is_stale)
    hosted._recover_locally(manager, market.client(), ("user",))
    monkeypatch.setattr(HostedState, "read", real_read)
    [row] = manager.list_installed("user")
    assert row["content_hash"] == v1["content_hash"]
    assert row["targets"] == ["codex"]
    assert not _claude_file(home).exists() and not _claude_file(home, "review-buddy").exists()
    assert HostedState(manager.store_root).read()["pull_ops"] == {}


def test_a_saved_restore_decision_is_never_replaced_by_a_different_one(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, home, v1, before = _renaming_update_crashed_before_recording(
        tmp_path, monkeypatch, market
    )
    state = HostedState(manager.store_root)
    [token] = state.read()["pull_ops"]
    first = {"to": "prior", "targets": ["codex"]}
    hosted._save_restore_decision(state, {}, token, first)
    hosted._save_restore_decision(state, {}, token, dict(first))  # same: fine
    with pytest.raises(SkillError):
        hosted._save_restore_decision(state, {}, token, {"to": "prior", "targets": ["codex", "claude-code"]})
    assert state.read()["pull_ops"][token]["restore"] == first


def test_recovery_stays_pending_when_the_pre_op_state_cannot_be_matched(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, home, v1, before = _renaming_update_crashed_before_recording(
        tmp_path, monkeypatch, market
    )
    with HostedState(manager.store_root).edit() as state:
        [op] = state["pull_ops"].values()
        op["before"] = {}
    [row] = manager.list_installed("user")
    hosted._recover_locally(manager, market.client(), ("user",))
    # Never shrunk from a guess: left as it is, the intent kept.
    assert manager.list_installed("user") == [row]
    assert HostedState(manager.store_root).read()["pull_ops"]


def test_a_foreign_record_under_the_pre_rename_id_still_counts_in_recovery(
    tmp_path: Path, monkeypatch, market: FakeMarketplace
):
    manager, home, v1, before = _renaming_update_crashed_before_recording(
        tmp_path, monkeypatch, market
    )
    [row] = manager.list_installed("user")
    v2 = {
        "version_id": row["source"]["commit"], "content_hash": row["content_hash"],
        "package_id": v1["package_id"],
    }
    # Another account's assignment of v2 for codex, written under the id the
    # installation had before the rename.
    _foreign_record(manager, v2, "assignment", local_package_id=before["package_id"])
    hosted._recover_locally(manager, market.client(), ("user",))
    [after] = manager.list_installed("user")
    assert after["content_hash"] == v2["content_hash"]  # the update stays
    assert sorted(after["targets"]) == ["claude-code", "codex"]
    assert HostedState(manager.store_root).read()["pull_ops"] == {}
