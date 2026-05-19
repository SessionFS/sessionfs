"""v0.10.11 — CLI tests for sfs admin service-keys + sfs auth keys.

HTTP boundary is patched on `sessionfs.cli.cmd_keys._api_request` so
tests are deterministic. We don't exercise the server here — the
v0.10.10 backend has its own integration coverage at
tests/server/integration/test_scoped_service_keys.py.
"""

from __future__ import annotations

import re
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_keys import auth_keys_app, service_keys_app

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


class _FakeRequest:
    """Async-callable replacement for `_api_request` that records the
    last call. Pass via `patch(..., new=fake)` (NOT `side_effect=...`)
    so the CLI's `asyncio.run(_api_request(...))` awaits the actual
    coroutine that __call__ returns. `side_effect` doesn't preserve
    the async return value cleanly across `asyncio.run`."""

    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        self.last: dict = {}

    async def __call__(
        self, method, path, api_url, api_key, json_data=None, extra_headers=None
    ):
        self.last = {"method": method, "path": path, "body": json_data}
        return self.status, self.body, {}


def _fake_request(status: int, body) -> _FakeRequest:
    return _FakeRequest(status, body)


# ── sfs admin service-keys ───────────────────────────────────────────


def test_service_keys_scopes_lists_the_full_vocabulary():
    """`scopes` is a pure local printout; must include every entry from
    the server's VALID_SCOPES so it stays the source of truth."""
    from sessionfs.server.schemas.api_keys import VALID_SCOPES

    result = runner.invoke(service_keys_app, ["scopes"])
    assert result.exit_code == 0, result.output
    out = _combined(result)
    for scope in VALID_SCOPES:
        assert scope in out, f"missing scope in output: {scope}"


def test_service_keys_create_rejects_wildcard_locally():
    """`*` must be rejected before the network call — service keys
    cannot use the wildcard scope (v0.10.10 Codex R1 finding C).
    Verify we never hit _api_request and exit code is 2 (typer-style
    user error)."""
    with patch("sessionfs.cli.cmd_keys._api_request") as mock_req:
        result = runner.invoke(
            service_keys_app,
            ["create", "--org", "org_x", "--name", "ci", "--scope", "*"],
        )
    assert result.exit_code == 2, result.output
    assert "wildcard" in _combined(result).lower()
    mock_req.assert_not_called()


def test_service_keys_create_rejects_unknown_scope_locally():
    """Unknown scopes are rejected pre-network. The valid-scopes list is
    surfaced so the user sees the full vocab on a typo."""
    with patch("sessionfs.cli.cmd_keys._api_request") as mock_req:
        result = runner.invoke(
            service_keys_app,
            [
                "create",
                "--org",
                "org_x",
                "--name",
                "ci",
                "--scope",
                "bogus:scope",
            ],
        )
    assert result.exit_code == 2, result.output
    assert "unknown scopes" in _combined(result).lower()
    assert "handoffs:write" in _combined(result)  # vocab surfaced
    mock_req.assert_not_called()


def test_service_keys_create_happy_path_shows_raw_key_once():
    """Default mode: raw key in pretty stdout + 'save this' warning."""
    body = {
        "id": "key_abc",
        "name": "ci",
        "key_prefix": "sk_sfs_xxxxx",
        "scopes": ["handoffs:write"],
        "project_ids": None,
        "key": "sk_sfs_FULL_RAW_KEY_HERE",
    }
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=_fake_request(201, body),
    ):
        result = runner.invoke(
            service_keys_app,
            [
                "create",
                "--org",
                "org_x",
                "--name",
                "ci",
                "--scope",
                "handoffs:write",
                "--expires-days",
                "30",
            ],
        )
    assert result.exit_code == 0, result.output
    combined = _combined(result)
    assert "sk_sfs_FULL_RAW_KEY_HERE" in combined
    assert "save" in combined.lower() and "not be shown again" in combined.lower()


def test_service_keys_create_output_key_mode_emits_only_raw_key_on_stdout():
    """--output-key prints raw key alone (no rich formatting, no
    'created' line, no warning on stdout). Used by CI runners as
    `KEY=$(sfs admin service-keys create ... --output-key)`."""
    body = {
        "id": "key_abc",
        "name": "ci",
        "key_prefix": "sk_sfs_xxxxx",
        "scopes": ["handoffs:write"],
        "project_ids": None,
        "key": "sk_sfs_FULL_RAW_KEY_HERE",
    }
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=_fake_request(201, body),
    ):
        result = runner.invoke(
            service_keys_app,
            [
                "create",
                "--org",
                "org_x",
                "--name",
                "ci",
                "--scope",
                "handoffs:write",
                "--output-key",
            ],
        )
    assert result.exit_code == 0, result.output
    # stdout MUST contain exactly the raw key + newline. We strip ANSI
    # and trim — anything else would break `KEY=$(...)`.
    stripped = _plain(result.stdout).strip()
    assert stripped == "sk_sfs_FULL_RAW_KEY_HERE", (
        f"--output-key stdout was {stripped!r}, expected just the raw key"
    )


def test_service_keys_create_sends_project_ids_when_supplied():
    body = {
        "id": "key_abc",
        "name": "ci",
        "key_prefix": "sk_sfs_x",
        "scopes": ["tickets:read"],
        "project_ids": ["proj_a", "proj_b"],
        "key": "sk_sfs_RAW",
    }
    fake = _fake_request(201, body)
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=fake,
    ):
        result = runner.invoke(
            service_keys_app,
            [
                "create",
                "--org",
                "org_x",
                "--name",
                "ci",
                "--scope",
                "tickets:read",
                "--project",
                "proj_a",
                "--project",
                "proj_b",
            ],
        )
    assert result.exit_code == 0, result.output
    assert fake.last["body"]["project_ids"] == ["proj_a", "proj_b"]


def test_service_keys_revoke_rejects_blank_reason_locally():
    with patch("sessionfs.cli.cmd_keys._api_request") as mock_req:
        result = runner.invoke(
            service_keys_app,
            ["revoke", "key_abc", "--org", "org_x", "--reason", "   "],
        )
    assert result.exit_code == 2, result.output
    assert "cannot be blank" in _combined(result).lower()
    mock_req.assert_not_called()


def test_service_keys_revoke_sends_trimmed_reason_in_body():
    fake = _fake_request(204, "")
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=fake,
    ):
        result = runner.invoke(
            service_keys_app,
            [
                "revoke",
                "key_abc",
                "--org",
                "org_x",
                "--reason",
                "  superseded by new key  ",
            ],
        )
    assert result.exit_code == 0, result.output
    assert fake.last["method"] == "DELETE"
    assert fake.last["path"] == "/api/v1/orgs/org_x/service-keys/key_abc"
    assert fake.last["body"] == {"reason": "superseded by new key"}


def test_service_keys_revoke_404_friendly_error():
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=_fake_request(404, {"detail": "not found"}),
    ):
        result = runner.invoke(
            service_keys_app,
            ["revoke", "missing", "--org", "org_x", "--reason", "x"],
        )
    assert result.exit_code == 1, result.output
    assert "not found" in _combined(result).lower()


def test_service_keys_rotate_happy_path_returns_new_raw():
    body = {
        "id": "key_new",
        "name": "ci",
        "key_prefix": "sk_sfs_new",
        "scopes": ["handoffs:write"],
        "project_ids": None,
        "key": "sk_sfs_NEW_RAW",
    }
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=_fake_request(200, body),
    ):
        result = runner.invoke(
            service_keys_app, ["rotate", "key_old", "--org", "org_x"]
        )
    assert result.exit_code == 0, result.output
    assert "sk_sfs_NEW_RAW" in _combined(result)


def test_service_keys_rotate_409_on_already_revoked():
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=_fake_request(409, {"detail": "revoked"}),
    ):
        result = runner.invoke(
            service_keys_app, ["rotate", "key_old", "--org", "org_x"]
        )
    assert result.exit_code == 1, result.output
    out = _combined(result).lower()
    assert "revoked" in out
    assert "create a new" in out


def test_service_keys_list_renders_active_revoked_states():
    body = [
        {
            "id": "k1",
            "name": "ci",
            "key_prefix": "sk_sfs_aaa",
            "scopes": ["handoffs:write"],
            "project_ids": None,
            "expires_at": None,
            "is_active": True,
        },
        {
            "id": "k2",
            "name": "retired",
            "key_prefix": "sk_sfs_bbb",
            "scopes": ["tickets:read"],
            "project_ids": ["proj_a"],
            "expires_at": "2026-12-01T00:00:00Z",
            "is_active": False,
        },
    ]
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=_fake_request(200, body),
    ):
        result = runner.invoke(service_keys_app, ["list", "--org", "org_x"])
    assert result.exit_code == 0, result.output
    out = _combined(result)
    assert "k1" in out and "k2" in out
    assert "ci" in out and "retired" in out
    # Active flag rendering
    assert "yes" in out
    assert "revoked" in out


def test_service_keys_list_empty_message():
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=_fake_request(200, []),
    ):
        result = runner.invoke(service_keys_app, ["list", "--org", "org_x"])
    assert result.exit_code == 0, result.output
    assert "no service keys" in _combined(result).lower()


# ── sfs auth keys (personal) ─────────────────────────────────────────


def test_auth_keys_create_happy_path():
    body = {
        "id": "key_personal",
        "name": "laptop",
        "key_prefix": "sk_sfs_per",
        "expires_at": None,
        "is_active": True,
        "key": "sk_sfs_PERSONAL_RAW",
    }
    fake = _fake_request(201, body)
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=fake,
    ):
        result = runner.invoke(
            auth_keys_app,
            ["create", "--name", "laptop", "--expires-days", "90"],
        )
    assert result.exit_code == 0, result.output
    assert "sk_sfs_PERSONAL_RAW" in _combined(result)
    assert fake.last["method"] == "POST"
    assert fake.last["path"] == "/api/v1/auth/me/api-keys"
    assert fake.last["body"]["name"] == "laptop"
    assert fake.last["body"]["expires_in_days"] == 90


def test_auth_keys_create_output_key_emits_only_raw_on_stdout():
    body = {
        "id": "k",
        "name": "n",
        "key_prefix": "sk_sfs_x",
        "expires_at": None,
        "is_active": True,
        "key": "sk_sfs_PERSONAL_RAW",
    }
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=_fake_request(201, body),
    ):
        result = runner.invoke(
            auth_keys_app,
            ["create", "--name", "n", "--output-key"],
        )
    assert result.exit_code == 0, result.output
    assert _plain(result.stdout).strip() == "sk_sfs_PERSONAL_RAW"


def test_auth_keys_revoke_sends_reason():
    fake = _fake_request(204, "")
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=fake,
    ):
        result = runner.invoke(
            auth_keys_app, ["revoke", "key_personal", "--reason", "rotated"]
        )
    assert result.exit_code == 0, result.output
    assert fake.last["method"] == "DELETE"
    assert fake.last["path"] == "/api/v1/auth/me/api-keys/key_personal"
    assert fake.last["body"] == {"reason": "rotated"}


def test_auth_keys_list_empty_message():
    with patch(
        "sessionfs.cli.cmd_keys._get_api_config",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_keys._api_request",
        new=_fake_request(200, []),
    ):
        result = runner.invoke(auth_keys_app, ["list"])
    assert result.exit_code == 0, result.output
    assert "no personal api keys" in _combined(result).lower()


# ── _parse_error coverage (Codex R1 MEDIUM on tk_53e042ecee7e43ff) ───
#
# All four FastAPI / v0.10.10 error envelope shapes must render a
# readable message. The previous implementation collapsed every shape
# but the structured envelope to the literal string "None".


def test_parse_error_top_level_structured_envelope():
    from sessionfs.cli.cmd_keys import _parse_error

    msg = _parse_error(
        403,
        {"error": {"code": "service_key_not_allowed", "message": "not allowed"}},
    )
    assert msg == "service_key_not_allowed: not allowed"


def test_parse_error_detail_wrapped_envelope_uses_error_as_code():
    """tier_gate.py raises HTTPException(detail={'error': 'upgrade_required',
    'message': '...'}). FastAPI wraps that as {"detail": {...}}. The
    `error` field acts as the code; this is the most common shape an
    org-admin user will hit when they aren't on Team+ tier."""
    from sessionfs.cli.cmd_keys import _parse_error

    msg = _parse_error(
        403,
        {
            "detail": {
                "error": "upgrade_required",
                "message": "This feature requires Team or above.",
            }
        },
    )
    assert msg == "upgrade_required: This feature requires Team or above."


def test_parse_error_detail_plain_string():
    """Most FastAPI HTTPExceptions: {"detail": "Organization not found"}."""
    from sessionfs.cli.cmd_keys import _parse_error

    assert _parse_error(404, {"detail": "Organization not found"}) == (
        "Organization not found"
    )


def test_parse_error_pydantic_422_validation_list():
    """Pydantic 422 errors arrive as {"detail": [{"loc": [...], "msg": "..."}]}.
    Show the first error with a dotted loc path."""
    from sessionfs.cli.cmd_keys import _parse_error

    msg = _parse_error(
        422,
        {
            "detail": [
                {
                    "loc": ["body", "reason"],
                    "msg": "field required",
                    "type": "value_error.missing",
                }
            ]
        },
    )
    assert msg == "validation error at body.reason: field required"


def test_parse_error_non_dict_body():
    from sessionfs.cli.cmd_keys import _parse_error

    assert "plain text body" in _parse_error(500, "plain text body")


def test_parse_error_does_not_return_literal_none():
    """Regression for Codex R1 MEDIUM: every shape must produce a
    user-readable message, never the literal string 'None'."""
    from sessionfs.cli.cmd_keys import _parse_error

    for status, body in [
        (403, {"detail": {"error": "x", "message": "y"}}),
        (404, {"detail": "not found"}),
        (422, {"detail": [{"loc": ["a"], "msg": "bad"}]}),
        (500, {"error": {"code": "boom", "message": "explosion"}}),
        (500, {}),
    ]:
        rendered = _parse_error(status, body)
        assert rendered != "None", (
            f"_parse_error({status}, {body}) returned literal 'None'"
        )
        assert rendered, f"_parse_error returned empty string for {body}"


# ── Shared helper regressions (Codex R1 LOW on tk_53e042ecee7e43ff) ──
#
# cmd_rules._api_request gained two behaviors during v0.10.11 that
# need explicit coverage:
#   1. DELETE forwards json_data via client.request("DELETE", ...).
#   2. Empty response body returns "" even when content-type says JSON
#      (FastAPI 204 No Content with the header still attached).


def test_api_request_delete_forwards_json_body():
    """v0.10.10 revoke endpoints require RevokeKeyRequest in the body
    on DELETE. The earlier `client.delete(...)` call did not accept a
    json kwarg, so reasons were dropped and the server 422'd."""
    import asyncio
    from unittest.mock import patch

    from sessionfs.cli.cmd_rules import _api_request

    captured: dict = {}

    class _FakeResponse:
        status_code = 204
        content = b""
        headers: dict = {}

        def json(self):
            return {}

        @property
        def text(self):
            return ""

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def request(self, method, url, *, headers=None, json=None):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _FakeResponse()

        async def get(self, *a, **kw):  # unused — DELETE path only
            raise AssertionError("DELETE should not route through .get()")

        async def delete(self, *a, **kw):
            raise AssertionError(
                "DELETE-with-body must NOT use .delete() — httpx ignores "
                "the json kwarg on that helper. v0.10.11 service-keys "
                "revoke regressed when the implementation called .delete()."
            )

    with patch("httpx.AsyncClient", return_value=_FakeClient()):
        status, body, _ = asyncio.run(
            _api_request(
                "DELETE",
                "/api/v1/orgs/o/service-keys/k",
                "http://api.test",
                "secret",
                json_data={"reason": "rotated"},
            )
        )

    assert status == 204
    assert captured["method"] == "DELETE"
    assert captured["json"] == {"reason": "rotated"}
    assert captured["headers"]["Authorization"] == "Bearer secret"


def test_api_request_empty_body_with_json_content_type_returns_empty_string():
    """FastAPI 204 No Content responses still carry
    `content-type: application/json` for some routes. resp.json() on
    b"" raises JSONDecodeError — the helper now short-circuits to ""
    when content is empty."""
    import asyncio
    from unittest.mock import patch

    from sessionfs.cli.cmd_rules import _api_request

    class _FakeResponse:
        status_code = 204
        content = b""
        headers = {"content-type": "application/json"}

        def json(self):
            raise AssertionError(
                "resp.json() must not be called on empty body — "
                "the helper short-circuits before calling .json()"
            )

        @property
        def text(self):
            return ""

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def request(self, method, url, *, headers=None, json=None):
            return _FakeResponse()

    with patch("httpx.AsyncClient", return_value=_FakeClient()):
        status, body, _ = asyncio.run(
            _api_request(
                "DELETE",
                "/foo",
                "http://api.test",
                "k",
                json_data={"reason": "x"},
            )
        )

    assert status == 204
    assert body == ""
