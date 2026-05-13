"""Unit tests for v0.10.0 Phase 5 default-org CLI surfaces.

Covers:
  - `sfs config default-org` (show / set / clear) — talks to the server.
  - `sfs project init --org <id>` — passes org_id in POST body.
  - `sfs project init --personal` — explicitly NULL even if /me has a default.
  - `sfs project init` fallback path — reads /me.default_org_id.
  - `sfs project init --org` + `--personal` together → exit 2.

The HTTP boundary is patched so tests are deterministic and don't
require a running server.
"""

from __future__ import annotations

import re
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_config import config_app
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


# ── sfs config default-org ─────────────────────────────────────────────


def test_config_default_org_show_when_unset():
    with patch(
        "sessionfs.cli.cmd_config._auth_client",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_config._http_json",
        return_value={"default_org_id": None, "email": "u@e", "user_id": "u_1"},
    ):
        result = runner.invoke(config_app, ["default-org"])
    assert result.exit_code == 0, result.output
    assert "no default org" in _combined(result).lower()


def test_config_default_org_show_when_set():
    with patch(
        "sessionfs.cli.cmd_config._auth_client",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_config._http_json",
        return_value={"default_org_id": "org_acme", "email": "u@e", "user_id": "u_1"},
    ):
        result = runner.invoke(config_app, ["default-org"])
    assert result.exit_code == 0, result.output
    assert "org_acme" in _combined(result)


def test_config_default_org_set_posts_put():
    with patch(
        "sessionfs.cli.cmd_config._auth_client",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_config._http_json",
        return_value={"default_org_id": "org_acme"},
    ) as mock_http:
        result = runner.invoke(config_app, ["default-org", "org_acme"])
    assert result.exit_code == 0, result.output
    assert "set to org_acme" in _combined(result).lower()
    # Last call should be PUT to /api/v1/auth/me/default-org with the org id.
    args, kwargs = mock_http.call_args
    assert args[0] == "PUT"
    assert args[1] == "/api/v1/auth/me/default-org"
    assert kwargs["body"] == {"org_id": "org_acme"}


def test_config_default_org_clear_posts_null():
    with patch(
        "sessionfs.cli.cmd_config._auth_client",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_config._http_json",
        return_value={"default_org_id": None},
    ) as mock_http:
        result = runner.invoke(config_app, ["default-org", "--clear"])
    assert result.exit_code == 0, result.output
    assert "cleared" in _combined(result).lower()
    args, kwargs = mock_http.call_args
    assert args[0] == "PUT"
    assert kwargs["body"] == {"org_id": None}


def test_config_default_org_rejects_id_and_clear_together():
    result = runner.invoke(config_app, ["default-org", "org_acme", "--clear"])
    assert result.exit_code == 2, result.output


def test_config_default_org_set_403_friendly_message():
    with patch(
        "sessionfs.cli.cmd_config._auth_client",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_config._http_json",
        return_value={"_status": 403, "_detail": "You are not a member"},
    ):
        result = runner.invoke(config_app, ["default-org", "org_other"])
    assert result.exit_code == 1, result.output
    assert "not a member" in _combined(result).lower()


# ── sfs project init --org / --personal ────────────────────────────────


def test_project_init_with_org_flag_posts_org_id():
    with patch(
        "sessionfs.cli.cmd_project._get_git_remote",
        return_value="git@github.com:acme/repo.git",
    ), patch(
        "sessionfs.cli.cmd_project._normalize_remote",
        return_value="acme/repo",
    ), patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        side_effect=[
            {"_status": 404},  # GET /projects/{remote} → not found
            {"id": "proj_x"},  # POST /projects/ → success
        ],
    ) as mock_req:
        result = runner.invoke(project_app, ["init", "--org", "org_acme"])

    assert result.exit_code == 0, result.output
    # Second call (POST) carries org_id=org_acme.
    post_call = mock_req.call_args_list[1]
    assert post_call.kwargs["json_data"]["org_id"] == "org_acme"
    assert "(org org_acme)" in _plain(result.stdout)


def test_project_init_with_personal_flag_omits_org_id():
    with patch(
        "sessionfs.cli.cmd_project._get_git_remote",
        return_value="git@github.com:acme/repo.git",
    ), patch(
        "sessionfs.cli.cmd_project._normalize_remote",
        return_value="acme/repo",
    ), patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        side_effect=[
            {"_status": 404},
            {"id": "proj_x"},
        ],
    ) as mock_req:
        result = runner.invoke(project_app, ["init", "--personal"])

    assert result.exit_code == 0, result.output
    post_call = mock_req.call_args_list[1]
    assert "org_id" not in post_call.kwargs["json_data"]
    assert "(personal)" in _plain(result.stdout)


def test_project_init_fallback_uses_me_default_org():
    """No --org / --personal → CLI reads /me and uses default_org_id."""
    with patch(
        "sessionfs.cli.cmd_project._get_git_remote",
        return_value="git@github.com:acme/repo.git",
    ), patch(
        "sessionfs.cli.cmd_project._normalize_remote",
        return_value="acme/repo",
    ), patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        side_effect=[
            {"_status": 404},  # GET /projects/{remote}
            {"default_org_id": "org_default"},  # GET /auth/me
            {"id": "proj_x"},  # POST /projects/
        ],
    ) as mock_req:
        result = runner.invoke(project_app, ["init"])

    assert result.exit_code == 0, result.output
    # Third call (POST) carries the default_org_id from /me.
    post_call = mock_req.call_args_list[2]
    assert post_call.kwargs["json_data"]["org_id"] == "org_default"


def test_project_init_fallback_no_default_is_personal():
    """No --org / --personal AND /me has null default → personal scope."""
    with patch(
        "sessionfs.cli.cmd_project._get_git_remote",
        return_value="git@github.com:acme/repo.git",
    ), patch(
        "sessionfs.cli.cmd_project._normalize_remote",
        return_value="acme/repo",
    ), patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        side_effect=[
            {"_status": 404},
            {"default_org_id": None},
            {"id": "proj_x"},
        ],
    ) as mock_req:
        result = runner.invoke(project_app, ["init"])

    assert result.exit_code == 0, result.output
    post_call = mock_req.call_args_list[2]
    assert "org_id" not in post_call.kwargs["json_data"]


def test_project_init_rejects_org_and_personal_together():
    result = runner.invoke(project_app, ["init", "--org", "org_x", "--personal"])
    assert result.exit_code == 2, result.output


def test_project_init_org_403_friendly_message():
    """Server rejects org membership → friendly message + exit 1."""
    with patch(
        "sessionfs.cli.cmd_project._get_git_remote",
        return_value="git@github.com:acme/repo.git",
    ), patch(
        "sessionfs.cli.cmd_project._normalize_remote",
        return_value="acme/repo",
    ), patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api.test", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        side_effect=[
            {"_status": 404},
            {"_status": 403},
        ],
    ):
        result = runner.invoke(project_app, ["init", "--org", "org_other"])
    assert result.exit_code == 1, result.output
    assert "not a member" in _combined(result).lower()
