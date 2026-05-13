"""Unit tests for `sfs project transfer` / `sfs project transfers`.

These tests patch the HTTP boundary (`_api_request`) and, where the
command resolves the cwd project, also patch `_resolve_project_id`.
The point of these tests is the argument parsing + branch routing
(initiate vs accept/reject/cancel vs list), not the HTTP details — the
server contract is covered by the Phase 2 integration tests.
"""

from __future__ import annotations

import re
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_project import project_app

runner = CliRunner()

# Rich emits ANSI escape codes when printing to its captured stream.
# We strip them before substring-matching so that styling changes don't
# break tests. The branch behavior is what we care about, not the dye.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _combined(result) -> str:
    """Rich's stderr console writes to a different stream than CliRunner
    captures for `result.stdout`. For tests that check err_console
    messages we read the test runner's captured stderr via
    `result.stderr` when available, otherwise just `result.output`.
    """
    parts = [getattr(result, "stdout", "") or "", getattr(result, "stderr", "") or "", getattr(result, "output", "") or ""]
    return _plain("\n".join(parts))


def _ok_transfer(state: str = "pending", **kw) -> dict:
    base = {
        "id": "xfer_abc123",
        "project_id": "proj_x",
        "project_git_remote_snapshot": "github.com/acme/x",
        "project_name_snapshot": "acme-x",
        "initiated_by": "u_me",
        "target_user_id": "u_other",
        "from_scope": "personal",
        "to_scope": "org_acme",
        "state": state,
        "accepted_by": None,
        "created_at": "2026-05-12T00:00:00Z",
        "accepted_at": None,
        "updated_at": "2026-05-12T00:00:00Z",
    }
    base.update(kw)
    return base


def test_transfer_requires_exactly_one_action():
    """No action → exit 2 with friendly message."""
    result = runner.invoke(project_app, ["transfer"])
    assert result.exit_code == 2, result.output


def test_transfer_rejects_multiple_actions():
    """--to AND --accept passed together → exit 2."""
    result = runner.invoke(
        project_app,
        ["transfer", "--to", "org_acme", "--accept", "xfer_x"],
    )
    assert result.exit_code == 2, result.output


def test_transfer_initiate_to_org_posts_and_prints_pending():
    """--to <org_id> resolves cwd project and POSTs initiate."""
    with patch(
        "sessionfs.cli.cmd_project._resolve_project_id",
        return_value=("proj_x", "https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value=_ok_transfer(state="pending"),
    ) as mock_req:
        result = runner.invoke(project_app, ["transfer", "--to", "org_acme"])

    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    assert "Transfer initiated" in out
    assert "xfer_abc123" in out
    args, kwargs = mock_req.call_args
    # _api_request(method, path, api_url, api_key, json_data=...)
    assert args[0] == "POST"
    assert args[1] == "/api/v1/projects/proj_x/transfer"
    assert kwargs["json_data"] == {"to": "org_acme"}


def test_transfer_initiate_personal_auto_accept_path():
    """When the server flips state to accepted, the CLI says 'auto-accepted'."""
    with patch(
        "sessionfs.cli.cmd_project._resolve_project_id",
        return_value=("proj_x", "https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value=_ok_transfer(state="accepted", to_scope="personal"),
    ):
        result = runner.invoke(project_app, ["transfer", "--to", "personal"])

    assert result.exit_code == 0, result.output
    assert "auto-accepted" in _plain(result.stdout)


def test_transfer_initiate_409_existing_pending():
    """409 from initiate → friendly 'cancel it first' message, exit 1."""
    with patch(
        "sessionfs.cli.cmd_project._resolve_project_id",
        return_value=("proj_x", "https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value={"_status": 409},
    ):
        result = runner.invoke(project_app, ["transfer", "--to", "org_acme"])

    assert result.exit_code == 1, result.output
    assert "pending transfer already exists" in _combined(result).lower()


def test_transfer_accept_posts_correct_url():
    """--accept <id> posts to /accept and prints 'accepted'."""
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value=_ok_transfer(state="accepted"),
    ) as mock_req:
        result = runner.invoke(project_app, ["transfer", "--accept", "xfer_abc123"])

    assert result.exit_code == 0, result.output
    assert "accepted" in _plain(result.stdout).lower()
    args, _ = mock_req.call_args
    assert args[1] == "/api/v1/transfers/xfer_abc123/accept"


def test_transfer_reject_posts_correct_url():
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value=_ok_transfer(state="rejected"),
    ) as mock_req:
        result = runner.invoke(project_app, ["transfer", "--reject", "xfer_abc"])

    assert result.exit_code == 0, result.output
    assert "rejected" in _plain(result.stdout).lower()
    args, _ = mock_req.call_args
    assert args[1] == "/api/v1/transfers/xfer_abc/reject"


def test_transfer_cancel_posts_correct_url():
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value=_ok_transfer(state="cancelled"),
    ) as mock_req:
        result = runner.invoke(project_app, ["transfer", "--cancel", "xfer_abc"])

    assert result.exit_code == 0, result.output
    assert "cancelled" in _plain(result.stdout).lower()
    args, _ = mock_req.call_args
    assert args[1] == "/api/v1/transfers/xfer_abc/cancel"


def test_transfer_state_action_409_friendly_message():
    """A 409 STALE_STATE on accept/reject/cancel surfaces a clear message."""
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value={"_status": 409},
    ):
        result = runner.invoke(project_app, ["transfer", "--accept", "xfer_abc"])

    assert result.exit_code == 1, result.output
    assert "no longer pending" in _combined(result).lower()


def test_transfers_list_incoming_calls_list_endpoint():
    """`sfs project transfers` hits GET /api/v1/transfers?direction=incoming."""
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value={"transfers": [_ok_transfer()]},
    ) as mock_req:
        result = runner.invoke(project_app, ["transfers"])

    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    assert "Incoming transfers (1)" in out
    assert "xfer_abc123" in out
    args, _ = mock_req.call_args
    assert args[0] == "GET"
    assert "direction=incoming" in args[1]


def test_transfers_list_outgoing_with_state_filter():
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value={"transfers": []},
    ) as mock_req:
        result = runner.invoke(
            project_app, ["transfers", "--direction", "outgoing", "--state", "pending"]
        )

    assert result.exit_code == 0, result.output
    assert "No outgoing transfers" in _plain(result.stdout)
    args, _ = mock_req.call_args
    assert "direction=outgoing" in args[1]
    assert "state=pending" in args[1]


def test_transfers_rejects_invalid_direction():
    """Unknown --direction → exit 2 with friendly message."""
    result = runner.invoke(project_app, ["transfers", "--direction", "sideways"])
    assert result.exit_code == 2, result.output


def test_transfer_state_action_404_friendly_message():
    """404 on accept/reject/cancel surfaces 'Transfer not found' (no KeyError).

    Regression for Phase 4 Round 1 Codex finding (entry 276): the state-
    change branch read `result['id']` without first checking 404, so a
    missing-transfer id crashed with a KeyError instead of a friendly
    message.
    """
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value={"_status": 404},
    ):
        result = runner.invoke(project_app, ["transfer", "--accept", "xfer_missing"])

    assert result.exit_code == 1, result.output
    assert "not found" in _combined(result).lower()


def test_transfer_reject_404_friendly_message():
    """Same 404 handling on --reject path."""
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value={"_status": 404},
    ):
        result = runner.invoke(project_app, ["transfer", "--reject", "xfer_missing"])
    assert result.exit_code == 1, result.output
    assert "not found" in _combined(result).lower()


def test_transfer_cancel_404_friendly_message():
    """Same 404 handling on --cancel path."""
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value={"_status": 404},
    ):
        result = runner.invoke(project_app, ["transfer", "--cancel", "xfer_missing"])
    assert result.exit_code == 1, result.output
    assert "not found" in _combined(result).lower()


def test_transfer_initiate_404_project_not_found():
    """404 on initiate (server-side project gone) → friendly 'project not found'."""
    with patch(
        "sessionfs.cli.cmd_project._resolve_project_id",
        return_value=("proj_x", "https://api.example.com", "k"),
    ), patch(
        "sessionfs.cli.cmd_project._api_request",
        return_value={"_status": 404},
    ):
        result = runner.invoke(project_app, ["transfer", "--to", "org_acme"])
    assert result.exit_code == 1, result.output
    assert "project not found" in _combined(result).lower()
