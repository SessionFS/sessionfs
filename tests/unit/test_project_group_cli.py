"""CLI tests for `sfs project group` (GitHub #101, phase 2).

Covers the org-scoped group CRUD/knowledge commands plus `sfs project list
--group`. The HTTP boundary is patched on ``sessionfs.cli.cmd_project_group``
(``_client`` for auth, ``_request`` for the transport) so the tests never
touch the network; the slug→id resolution and default-org fallback are
exercised through a small routing fake.
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_project import project_app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _combined(result) -> str:
    parts = [
        getattr(result, "stdout", "") or "",
        getattr(result, "stderr", "") or "",
        getattr(result, "output", "") or "",
    ]
    return _plain("\n".join(parts))


class _Router:
    """Routes the group CLI's ``_request`` calls by (method, path).

    ``routes`` maps a (method, path) tuple to a return value; ``default`` is
    used for anything not matched. Records every call for assertions.
    """

    def __init__(self, routes: dict, default=None):
        self.routes = routes
        self.default = default
        self.calls: list[dict] = []

    async def __call__(self, method, path, api_url, api_key, json_data=None):
        self.calls.append({"method": method, "path": path, "body": json_data})
        return self.routes.get((method, path), self.default)


def _groups_summary(*slugs):
    return [
        {
            "id": f"pg_{s}",
            "org_id": "org_1",
            "slug": s,
            "name": s.capitalize(),
            "description": None,
            "member_count": 2,
            "project_count": 1,
            "my_role": "admin",
        }
        for s in slugs
    ]


# ── list ──


def test_group_list_renders_table():
    router = _Router({("GET", "/api/v1/orgs/org_1/groups"): _groups_summary("core", "apps")})
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ), patch("sessionfs.cli.cmd_project_group._request", new=router):
        result = runner.invoke(project_app, ["group", "list", "--org", "org_1"])

    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "core" in out and "Core" in out
    assert "apps" in out and "Apps" in out
    assert router.calls[0]["path"] == "/api/v1/orgs/org_1/groups"


def test_group_list_json():
    groups = _groups_summary("core")
    router = _Router({("GET", "/api/v1/orgs/org_1/groups"): groups})
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ), patch("sessionfs.cli.cmd_project_group._request", new=router):
        result = runner.invoke(project_app, ["group", "list", "--org", "org_1", "--json"])

    assert result.exit_code == 0, _combined(result)
    assert json.loads(result.stdout) == groups


# ── default-org fallback ──


def test_group_list_resolves_default_org_from_me():
    router = _Router(
        {
            ("GET", "/api/v1/auth/me"): {"default_org_id": "org_default"},
            ("GET", "/api/v1/orgs/org_default/groups"): _groups_summary("core"),
        }
    )
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ), patch("sessionfs.cli.cmd_project_group._request", new=router):
        result = runner.invoke(project_app, ["group", "list"])

    assert result.exit_code == 0, _combined(result)
    assert any(c["path"] == "/api/v1/auth/me" for c in router.calls)
    assert any(c["path"] == "/api/v1/orgs/org_default/groups" for c in router.calls)


# ── show (slug → id resolution) ──


def test_group_show_resolves_slug_and_renders_members_projects():
    detail = {
        "id": "pg_core",
        "org_id": "org_1",
        "slug": "core",
        "name": "Core",
        "description": "Core services",
        "my_role": "admin",
        "members": [
            {"user_id": "u_1", "email": "a@x.io", "role": "admin", "granted_by_user_id": None, "created_at": None},
            {"user_id": "u_2", "email": "b@x.io", "role": "reader", "granted_by_user_id": "u_1", "created_at": None},
        ],
        "projects": ["proj_alpha", "proj_beta"],
    }
    router = _Router(
        {
            ("GET", "/api/v1/orgs/org_1/groups"): _groups_summary("core"),
            ("GET", "/api/v1/orgs/org_1/groups/pg_core"): detail,
        }
    )
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ), patch("sessionfs.cli.cmd_project_group._request", new=router):
        result = runner.invoke(project_app, ["group", "show", "core", "--org", "org_1"])

    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "Core" in out and "Core services" in out
    assert "a@x.io" in out and "b@x.io" in out
    assert "proj_alpha" in out and "proj_beta" in out
    assert ("GET", "/api/v1/orgs/org_1/groups/pg_core") in {
        (c["method"], c["path"]) for c in router.calls
    }


# ── create ──


def test_group_create_posts_and_handles_conflict():
    created = {
        "id": "pg_new",
        "org_id": "org_1",
        "slug": "new",
        "name": "New Group",
        "description": None,
        "my_role": "admin",
        "members": [],
        "projects": [],
    }
    router = _Router({("POST", "/api/v1/orgs/org_1/groups"): created})
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ), patch("sessionfs.cli.cmd_project_group._request", new=router):
        result = runner.invoke(
            project_app, ["group", "create", "New Group", "--org", "org_1", "--slug", "new"]
        )

    assert result.exit_code == 0, _combined(result)
    assert "New Group" in _combined(result)
    assert router.calls[-1]["body"] == {"name": "New Group", "slug": "new"}


def test_group_create_reports_slug_conflict():
    router = _Router({("POST", "/api/v1/orgs/org_1/groups"): {"_status": 409}})
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ), patch("sessionfs.cli.cmd_project_group._request", new=router):
        result = runner.invoke(project_app, ["group", "create", "Core", "--org", "org_1"])

    assert result.exit_code == 1
    assert "already exists" in _combined(result)


# ── grant role validation ──


def test_group_grant_rejects_unknown_role():
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ):
        result = runner.invoke(
            project_app,
            ["group", "grant", "core", "u_2", "--role", "superuser", "--org", "org_1"],
        )

    assert result.exit_code == 2
    assert "reader" in _combined(result)


# ── search ──


def test_group_search_renders_hits_and_json():
    hits = [
        {
            "project_id": "proj_alpha",
            "project_name": "Alpha",
            "entry_id": 7,
            "entry_type": "decision",
            "confidence": 0.95,
            "freshness": "current",
            "created_at": None,
            "content": "Use Postgres for persistence",
        }
    ]
    router = _Router({("GET", "/api/v1/orgs/org_1/groups/core/knowledge/search?limit=100"): hits})
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ), patch("sessionfs.cli.cmd_project_group._request", new=router):
        result = runner.invoke(project_app, ["group", "search", "core", "--org", "org_1", "--json"])

    assert result.exit_code == 0, _combined(result)
    assert json.loads(result.stdout) == hits


def test_group_search_validates_entry_type():
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ):
        result = runner.invoke(
            project_app, ["group", "search", "core", "--org", "org_1", "--entry-type", "bogus"]
        )

    assert result.exit_code == 2
    assert "decision" in _combined(result)


# ── context / compile ──


def test_group_context_prints_document():
    doc = "# Core — Group Knowledge Context\n\nShip v1"
    router = _Router(
        {
            (
                "GET",
                "/api/v1/orgs/org_1/groups/core/context",
            ): {"group_id": "pg_core", "slug": "core", "document": doc, "compiled_at": None, "compiled_by_user_id": None, "sources": []}
        }
    )
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ), patch("sessionfs.cli.cmd_project_group._request", new=router):
        result = runner.invoke(project_app, ["group", "context", "core", "--org", "org_1"])

    assert result.exit_code == 0, _combined(result)
    assert doc in _combined(result)


def test_group_compile_reports_disabled():
    router = _Router({("POST", "/api/v1/orgs/org_1/groups/core/compile"): {"_status": 409}})
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ), patch("sessionfs.cli.cmd_project_group._request", new=router):
        result = runner.invoke(project_app, ["group", "compile", "core", "--org", "org_1"])

    assert result.exit_code == 1
    assert "not enabled" in _combined(result)


# ── sfs project list --group ──


def test_project_list_filters_by_group():
    projects = [
        {"id": "proj_alpha", "name": "Alpha", "git_remote_normalized": "github.com/a/a", "org_id": "org_1"},
        {"id": "proj_beta", "name": "Beta", "git_remote_normalized": "github.com/a/b", "org_id": "org_1"},
    ]
    detail = {
        "id": "pg_core",
        "org_id": "org_1",
        "slug": "core",
        "name": "Core",
        "description": None,
        "my_role": "admin",
        "members": [],
        "projects": ["proj_alpha"],
    }

    # `project_list` uses cmd_project._api_request for the project list and the
    # group detail, but the slug→id helper in cmd_project_group uses its own
    # `_request` transport — patch both.
    api_router = _Router(
        {
            ("GET", "/api/v1/projects/"): projects,
            ("GET", "/api/v1/orgs/org_1/groups/pg_core"): detail,
        }
    )
    slug_router = _Router({("GET", "/api/v1/orgs/org_1/groups"): _groups_summary("core")})
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api", "key"),
    ), patch("sessionfs.cli.cmd_project._api_request", new=api_router), patch(
        "sessionfs.cli.cmd_project_group._request", new=slug_router
    ):
        result = runner.invoke(
            project_app, ["list", "--group", "core", "--org", "org_1"]
        )

    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "Alpha" in out
    assert "Beta" not in out


# ── helpers for the review follow-ups ──


def _invoke(router, args):
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ), patch("sessionfs.cli.cmd_project_group._request", new=router):
        return runner.invoke(project_app, ["group", *args])


_LIST = ("GET", "/api/v1/orgs/org_1/groups")
_FORBIDDEN = {
    "_status": 403,
    "_body": {"error": {"code": "forbidden", "message": "Group admin role required"}},
}


# ── grant / revoke: email resolution, --role, error paths ──


def test_group_grant_resolves_email_and_uses_role_option():
    router = _Router(
        {
            _LIST: _groups_summary("core"),
            ("GET", "/api/v1/orgs/org_1/members"): {
                "members": [{"user_id": "u_bob", "email": "Bob@X.io", "role": "member"}]
            },
            ("PUT", "/api/v1/orgs/org_1/groups/pg_core/members/u_bob"): {"id": "pg_core"},
        }
    )
    result = _invoke(router, ["grant", "core", "bob@x.io", "--role", "editor", "--org", "org_1"])
    assert result.exit_code == 0, _combined(result)
    assert router.calls[-1]["body"] == {"role": "editor"}
    assert "Granted" in _combined(result)


def test_group_grant_unknown_email_fails_clearly():
    router = _Router(
        {
            _LIST: _groups_summary("core"),
            ("GET", "/api/v1/orgs/org_1/members"): {"members": []},
        }
    )
    result = _invoke(router, ["grant", "core", "nobody@x.io", "--role", "reader", "--org", "org_1"])
    assert result.exit_code == 1
    assert "No member of org org_1 has the email nobody@x.io" in _combined(result)
    assert not any(c["method"] == "PUT" for c in router.calls)


def test_group_grant_404_and_other_errors():
    router = _Router({_LIST: _groups_summary("core")}, default={"_status": 404})
    result = _invoke(router, ["grant", "core", "u_2", "--role", "reader", "--org", "org_1"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "Project group not found" in out
    assert "Granted" not in out and "Unexpected error" not in out

    router = _Router({_LIST: _groups_summary("core")}, default=_FORBIDDEN)
    result = _invoke(router, ["grant", "core", "u_2", "--role", "reader", "--org", "org_1"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "API error (403)" in out and "Group admin role required" in out
    assert "Granted" not in out


def test_group_revoke_404_is_not_a_false_success():
    router = _Router({_LIST: _groups_summary("core")}, default={"_status": 404})
    result = _invoke(router, ["revoke", "core", "u_2", "--org", "org_1"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "holds no grant" in out
    assert "Revoked" not in out


def test_group_remove_404_and_403():
    router = _Router({_LIST: _groups_summary("core")}, default={"_status": 404})
    result = _invoke(router, ["remove", "core", "proj_x", "--org", "org_1"])
    assert result.exit_code == 1
    assert "is not in group core" in _combined(result)
    assert "Removed" not in _combined(result)

    router = _Router({_LIST: _groups_summary("core")}, default=_FORBIDDEN)
    result = _invoke(router, ["remove", "core", "proj_x", "--org", "org_1"])
    assert result.exit_code == 1
    assert "Group admin role required" in _combined(result)
    assert "Removed" not in _combined(result)


def test_group_add_multiple_projects_and_stops_on_error():
    router = _Router(
        {
            _LIST: _groups_summary("core"),
            ("PUT", "/api/v1/orgs/org_1/groups/pg_core/projects/proj_a"): {"id": "pg_core"},
            ("PUT", "/api/v1/orgs/org_1/groups/pg_core/projects/proj_b"): {"_status": 404},
        }
    )
    result = _invoke(router, ["add", "core", "proj_a", "proj_b", "--org", "org_1"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "Added project proj_a" in out
    assert "Project proj_b not found" in out


def test_group_create_404_and_403():
    router = _Router({}, default={"_status": 404})
    result = _invoke(router, ["create", "Core", "--org", "org_x"])
    assert result.exit_code == 1
    assert "Org org_x not found" in _combined(result)

    router = _Router({}, default=_FORBIDDEN)
    result = _invoke(router, ["create", "Core", "--org", "org_1"])
    assert result.exit_code == 1
    assert "API error (403)" in _combined(result)
    assert "Created" not in _combined(result)


def test_group_compile_404_and_disabled_hint():
    router = _Router({}, default={"_status": 404})
    result = _invoke(router, ["compile", "core", "--org", "org_1"])
    assert result.exit_code == 1
    assert "No project group 'core'" in _combined(result)

    router = _Router(
        {},
        default={
            "_status": 409,
            "_body": {"error": {"code": "group_compile_disabled", "message": "x"}},
        },
    )
    result = _invoke(router, ["compile", "core", "--org", "org_1"])
    assert result.exit_code == 1
    assert "sfs project group set core --compile" in " ".join(_combined(result).split())


def test_group_delete_errors_are_not_successes():
    router = _Router({_LIST: _groups_summary("core")}, default=_FORBIDDEN)
    result = _invoke(router, ["delete", "core", "--org", "org_1"])
    assert result.exit_code == 1
    assert "Deleted" not in _combined(result)


def test_unknown_slug_and_org_404():
    router = _Router({_LIST: _groups_summary("other")})
    result = _invoke(router, ["show", "core", "--org", "org_1"])
    assert result.exit_code == 1
    assert "No project group with slug 'core'" in _combined(result)

    router = _Router({}, default={"_status": 404})
    result = _invoke(router, ["show", "core", "--org", "org_1"])
    assert result.exit_code == 1
    assert "Org org_1 not found" in _combined(result)


# ── set --compile/--no-compile ──


def test_group_set_compile_on_and_off():
    router = _Router(
        {
            _LIST: _groups_summary("core"),
            ("PATCH", "/api/v1/orgs/org_1/groups/pg_core"): {
                "slug": "core",
                "compile_enabled": True,
            },
        }
    )
    result = _invoke(router, ["set", "core", "--compile", "--org", "org_1"])
    assert result.exit_code == 0, _combined(result)
    assert router.calls[-1]["method"] == "PATCH"
    assert router.calls[-1]["body"] == {"compile_enabled": True}
    assert "knowledge compile on" in _combined(result)

    result = _invoke(router, ["set", "core", "--no-compile", "--org", "org_1"])
    assert router.calls[-1]["body"] == {"compile_enabled": False}


def test_group_set_requires_a_change():
    result = _invoke(_Router({}), ["set", "core", "--org", "org_1"])
    assert result.exit_code == 2
    assert "Nothing to update" in _combined(result)


def test_group_set_feature_gate_error():
    router = _Router(
        {_LIST: _groups_summary("core")},
        default={
            "_status": 403,
            "_body": {"error": {"code": "upgrade_required", "message": "Project groups require the Team plan or above."}},
        },
    )
    result = _invoke(router, ["set", "core", "--compile", "--org", "org_1"])
    assert result.exit_code == 1
    assert "Team plan" in _combined(result)


# ── search positional query + path quoting ──


def test_group_search_positional_query_is_encoded():
    router = _Router({}, default=[])
    result = _invoke(router, ["search", "core", "100% done", "--org", "org_1"])
    assert result.exit_code == 0, _combined(result)
    assert router.calls[-1]["path"] == (
        "/api/v1/orgs/org_1/groups/core/knowledge/search?limit=100&q=100%25+done"
    )


def test_group_search_rejects_short_query():
    result = _invoke(_Router({}), ["search", "core", "ab", "--org", "org_1"])
    assert result.exit_code == 2
    assert "3–200" in _combined(result)


def test_group_ids_are_path_quoted():
    router = _Router({}, default={"document": "", "slug": "x"})
    result = _invoke(router, ["context", "a b?x", "--org", "org_1"])
    assert result.exit_code == 0, _combined(result)
    assert router.calls[-1]["path"] == "/api/v1/orgs/org_1/groups/a%20b%3Fx/context"

    result = _invoke(_Router({}), ["context", "core", "--org", "../admin"])
    assert result.exit_code == 2
    assert "Invalid org id" in _combined(result)

    result = _invoke(_Router({}), ["context", "..", "--org", "org_1"])
    assert result.exit_code == 2
    assert "Invalid group slug" in _combined(result)


def test_group_network_error_is_clear(monkeypatch):
    import httpx

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **k):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    with patch(
        "sessionfs.cli.cmd_project_group._client", return_value=("http://api", "key")
    ):
        result = runner.invoke(project_app, ["group", "list", "--org", "org_1"])
    assert result.exit_code == 1
    out = _combined(result)
    assert "Could not reach the SessionFS API" in out
    assert "Unexpected error" not in out


def test_group_grant_prefers_exact_email_and_refuses_ambiguous():
    members = {
        "members": [
            {"user_id": "u_lower", "email": "bob@x.io", "role": "member"},
            {"user_id": "u_upper", "email": "Bob@X.io", "role": "member"},
        ]
    }
    routes = {
        _LIST: _groups_summary("core"),
        ("GET", "/api/v1/orgs/org_1/members"): members,
        ("PUT", "/api/v1/orgs/org_1/groups/pg_core/members/u_upper"): {"id": "pg_core"},
    }
    router = _Router(routes)
    result = _invoke(router, ["grant", "core", "Bob@X.io", "--role", "reader", "--org", "org_1"])
    assert result.exit_code == 0, _combined(result)
    assert router.calls[-1]["path"].endswith("/members/u_upper")

    router = _Router(routes)
    result = _invoke(router, ["grant", "core", "BOB@x.io", "--role", "reader", "--org", "org_1"])
    assert result.exit_code == 1
    out = " ".join(_combined(result).split())
    assert "matches more than one member" in out
    assert not any(c["method"] == "PUT" for c in router.calls)
