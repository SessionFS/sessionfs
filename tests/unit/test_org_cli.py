"""Unit tests for the v0.17.0 org member-management CLI surface.

Covers GitHub #59:
  - `sfs org set-role` by user ID and by email (case-insensitive)
  - `sfs org set-role` unknown email → friendly error, no HTTP call
  - `sfs org set-role owner` → rejected (ownership transfer is separate)
  - `sfs org remove` by email
  - `sfs org remove --bulk` with an unresolvable entry → aborts before any removal
  - `sfs org remove --bulk` partial failure → summary + non-zero exit
  - `sfs org remove --bulk` declined confirmation → nothing removed
  - `sfs org auto-join list|add|remove`

The HTTP boundary is patched so tests are deterministic and don't
require a running server (matches test_default_org_cli.py).
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_org import org_app

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


class FakeResp:
    """Minimal httpx.Response stand-in for the CLI's handle_api_response."""

    def __init__(self, status_code: int = 200, body=None):
        self.status_code = status_code
        self._body = body
        if body is None:
            self.text = ""
        elif isinstance(body, str):
            self.text = body
        else:
            self.text = json.dumps(body)

    def json(self):
        if self._body is None:
            return {}
        if isinstance(self._body, str):
            try:
                return json.loads(self._body)
            except Exception:
                return self._body
        return self._body


# ── sfs org set-role ────────────────────────────────────────────────────


def test_set_role_by_user_id():
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch("httpx.put", return_value=FakeResp(200)) as mock_put:
        result = runner.invoke(org_app, ["set-role", "u_alice", "admin"])
    assert result.exit_code == 0, result.output
    url = mock_put.call_args[0][0]
    assert "u_alice" in url
    assert url.endswith("/role")
    assert mock_put.call_args[1]["json"] == {"role": "admin"}


def test_set_role_by_email_case_insensitive():
    members = [{"email": "alice@example.com", "user_id": "u_alice", "role": "member"}]
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch(
        "sessionfs.cli.cmd_org._fetch_members", return_value=members
    ), patch("httpx.put", return_value=FakeResp(200)) as mock_put:
        result = runner.invoke(org_app, ["set-role", "ALICE@example.com", "member"])
    assert result.exit_code == 0, result.output
    assert "u_alice" in mock_put.call_args[0][0]
    assert mock_put.call_args[1]["json"] == {"role": "member"}


def test_set_role_unknown_email():
    members = [{"email": "bob@example.com", "user_id": "u_bob", "role": "member"}]
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch(
        "sessionfs.cli.cmd_org._fetch_members", return_value=members
    ), patch("httpx.put", return_value=FakeResp(200)) as mock_put:
        result = runner.invoke(org_app, ["set-role", "nobody@example.com", "admin"])
    assert result.exit_code == 1, result.output
    assert "no member with email" in _combined(result)
    mock_put.assert_not_called()


def test_set_role_rejects_owner():
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ):
        result = runner.invoke(org_app, ["set-role", "u_alice", "owner"])
    assert result.exit_code == 2, result.output
    assert "owner" in _combined(result).lower()


# ── sfs org remove ──────────────────────────────────────────────────────


def test_remove_by_email():
    members = [{"email": "alice@example.com", "user_id": "u_alice", "role": "member"}]
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch(
        "sessionfs.cli.cmd_org._fetch_members", return_value=members
    ), patch("httpx.delete", return_value=FakeResp(200)) as mock_delete:
        result = runner.invoke(org_app, ["remove", "alice@example.com"])
    assert result.exit_code == 0, result.output
    assert "u_alice" in mock_delete.call_args[0][0]
    assert "removed" in _combined(result).lower()


def test_remove_bulk_bad_entry_aborts_before_removal(tmp_path):
    f = tmp_path / "members.txt"
    f.write_text("alice@example.com\nnobody@example.com\n# a comment\n\n")
    members = [{"email": "alice@example.com", "user_id": "u_alice", "role": "member"}]
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch(
        "sessionfs.cli.cmd_org._fetch_members", return_value=members
    ), patch("httpx.delete", return_value=FakeResp(200)) as mock_delete:
        result = runner.invoke(org_app, ["remove", "--bulk", str(f), "--yes"])
    assert result.exit_code == 1, result.output
    assert "nothing removed" in _combined(result).lower()
    # Aborts BEFORE removing anything — no DELETE issued.
    mock_delete.assert_not_called()


def test_remove_bulk_partial_failure_summary(tmp_path):
    f = tmp_path / "members.txt"
    f.write_text("alice@example.com\nbob@example.com\n")
    members = [
        {"email": "alice@example.com", "user_id": "u_alice", "role": "member"},
        {"email": "bob@example.com", "user_id": "u_bob", "role": "member"},
    ]
    responses = [
        FakeResp(200),
        FakeResp(500, {"detail": {"message": "Cannot remove the last owner"}}),
    ]
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch(
        "sessionfs.cli.cmd_org._fetch_members", return_value=members
    ), patch("httpx.delete", side_effect=responses) as mock_delete:
        result = runner.invoke(org_app, ["remove", "--bulk", str(f), "--yes"])
    assert result.exit_code == 1, result.output
    assert "Removed 1 of 2" in _combined(result)
    assert "Cannot remove the last owner" in _combined(result)
    assert mock_delete.call_count == 2


def test_remove_bulk_declined_confirmation_removes_nothing(tmp_path):
    f = tmp_path / "members.txt"
    f.write_text("alice@example.com\n")
    members = [{"email": "alice@example.com", "user_id": "u_alice", "role": "member"}]
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch(
        "sessionfs.cli.cmd_org._fetch_members", return_value=members
    ), patch("rich.prompt.Confirm.ask", return_value=False), patch(
        "httpx.delete", return_value=FakeResp(200)
    ) as mock_delete:
        result = runner.invoke(org_app, ["remove", "--bulk", str(f)])
    assert result.exit_code == 0, result.output
    assert "aborted" in _combined(result).lower()
    mock_delete.assert_not_called()


def test_remove_bulk_unknown_user_id_aborts_before_removal(tmp_path):
    # A plain token is a user ID; it must be a current member too, or a valid
    # email listed before it would already be removed when the bad ID fails.
    f = tmp_path / "members.txt"
    f.write_text("alice@example.com\nbogus-id\n")
    members = [{"email": "alice@example.com", "user_id": "u_alice", "role": "member"}]
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch(
        "sessionfs.cli.cmd_org._fetch_members", return_value=members
    ), patch("httpx.delete", return_value=FakeResp(200)) as mock_delete:
        result = runner.invoke(org_app, ["remove", "--bulk", str(f), "--yes"])
    assert result.exit_code == 1, result.output
    assert "bogus-id" in _combined(result)
    mock_delete.assert_not_called()


def test_remove_bulk_duplicate_member_aborts_before_removal(tmp_path):
    f = tmp_path / "members.txt"
    f.write_text("alice@example.com\nu_alice\n")
    members = [{"email": "alice@example.com", "user_id": "u_alice", "role": "member"}]
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch(
        "sessionfs.cli.cmd_org._fetch_members", return_value=members
    ), patch("httpx.delete", return_value=FakeResp(200)) as mock_delete:
        result = runner.invoke(org_app, ["remove", "--bulk", str(f), "--yes"])
    assert result.exit_code == 1, result.output
    mock_delete.assert_not_called()


# ── sfs org auto-join ───────────────────────────────────────────────────


def test_auto_join_list_empty():
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch("sessionfs.cli.cmd_org._org_id", return_value="org_1"), patch(
        "sessionfs.cli.cmd_org._get_settings", return_value={"auto_join_domains": []}
    ):
        result = runner.invoke(org_app, ["auto-join", "list"])
    assert result.exit_code == 0, result.output
    assert "no auto-join domains" in _combined(result).lower()


def test_auto_join_add_normalizes_and_posts():
    settings = {
        "kb_retention_days": None,
        "kb_max_context_words": None,
        "kb_section_page_limit": None,
        "auto_join_domains": [],
    }
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch("sessionfs.cli.cmd_org._org_id", return_value="org_1"), patch(
        "sessionfs.cli.cmd_org._get_settings", return_value=settings
    ), patch("sessionfs.cli.cmd_org._put_settings") as mock_put:
        result = runner.invoke(org_app, ["auto-join", "add", "Example.com"])
    assert result.exit_code == 0, result.output
    payload = mock_put.call_args[0][3]
    assert payload["auto_join_domains"] == ["example.com"]


def test_auto_join_remove():
    settings = {
        "kb_retention_days": None,
        "kb_max_context_words": None,
        "kb_section_page_limit": None,
        "auto_join_domains": ["example.com", "other.com"],
    }
    with patch(
        "sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k")
    ), patch("sessionfs.cli.cmd_org._org_id", return_value="org_1"), patch(
        "sessionfs.cli.cmd_org._get_settings", return_value=settings
    ), patch("sessionfs.cli.cmd_org._put_settings") as mock_put:
        result = runner.invoke(org_app, ["auto-join", "remove", "EXAMPLE.com"])
    assert result.exit_code == 0, result.output
    assert mock_put.call_args[0][3]["auto_join_domains"] == ["other.com"]
