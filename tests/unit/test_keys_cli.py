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
