"""CLI tests for `sfs project repos` and `sfs project set --name`.

Covers tk_e1bd970236bc42fa (#1): GET /repos returns a bare JSON array;
the CLI helper `_repos_api_request` must tolerate a list success body
(it previously called `.get("_status")` on a list → AttributeError crash)
while still handling the dict error envelopes (404/409/422).

Also covers tk_9b5fd8c3e2604254 (#2): `sfs project set --name` issues a
PATCH /projects/{id} rename.

The HTTP boundary is patched on `sessionfs.cli.cmd_project._api_request`
and `_get_project_client` so the tests are deterministic and never touch
the network.
"""

from __future__ import annotations

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


class _FakeApiRequest:
    """Async-callable replacement for `_api_request`.

    Records the last call and returns a fixed body verbatim (mirroring
    the real helper, which returns `resp.json()` — a dict OR a list).
    """

    def __init__(self, body):
        self.body = body
        self.last: dict = {}

    async def __call__(self, method, path, api_url, api_key, json_data=None):
        self.last = {"method": method, "path": path, "body": json_data}
        return self.body


# ── #1: `sfs project repos` tolerates the bare list body ──────────────


def test_project_repos_renders_list_body_without_crashing():
    """GET /repos returns a JSON array — the CLI must render it, not crash.

    Regression for tk_e1bd970236bc42fa: `_repos_api_request` called
    `result.get("_status")` on the list and raised AttributeError.
    """
    repos = [
        {
            "id": "repo_aaa",
            "project_id": "proj_x",
            "git_remote_normalized": "github.com/acme/backend",
            "is_primary": True,
            "verified": True,
            "verification_method": "github_app",
        },
        {
            "id": "repo_bbb",
            "project_id": "proj_x",
            "git_remote_normalized": "github.com/acme/api",
            "is_primary": False,
            "verified": False,
            "verification_method": "owner_attested",
        },
    ]
    fake = _FakeApiRequest(repos)
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api", "key"),
    ), patch("sessionfs.cli.cmd_project._api_request", new=fake):
        result = runner.invoke(project_app, ["repos", "--project-id", "proj_x"])

    assert result.exit_code == 0, _combined(result)
    out = _combined(result)
    assert "github.com/acme/backend" in out
    assert "github.com/acme/api" in out
    assert "(primary)" in out
    # Confirm it hit the list endpoint.
    assert fake.last["method"] == "GET"
    assert fake.last["path"] == "/api/v1/projects/proj_x/repos"


def test_project_repos_empty_list_renders_no_repos_message():
    """An empty array (no repos linked) renders the empty-state, no crash."""
    fake = _FakeApiRequest([])
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api", "key"),
    ), patch("sessionfs.cli.cmd_project._api_request", new=fake):
        result = runner.invoke(project_app, ["repos", "--project-id", "proj_x"])

    assert result.exit_code == 0, _combined(result)
    assert "No repos linked" in _combined(result)


def test_repos_api_request_handles_404_dict_envelope():
    """A 404 dict envelope still surfaces as a 'Project not found' error."""
    fake = _FakeApiRequest({"_status": 404})
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api", "key"),
    ), patch("sessionfs.cli.cmd_project._api_request", new=fake):
        result = runner.invoke(project_app, ["repos", "--project-id", "proj_missing"])

    assert result.exit_code == 1
    assert "Project not found" in _combined(result)


def test_repos_api_request_handles_409_dict_envelope():
    """A 409 conflict dict envelope renders its message (not a crash)."""
    fake = _FakeApiRequest({
        "_status": 409,
        "detail": {
            "code": "repo_already_linked",
            "message": "This repo is already linked to another project.",
            "existing_project_id": "proj_other",
        },
    })
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api", "key"),
    ), patch("sessionfs.cli.cmd_project._api_request", new=fake):
        result = runner.invoke(
            project_app,
            ["link-repo", "github.com/acme/x", "--project-id", "proj_x"],
        )

    assert result.exit_code == 1
    out = _combined(result)
    assert "already linked" in out
    assert "proj_other" in out


# ── #2: `sfs project set --name` issues a PATCH rename ────────────────


def test_project_set_name_issues_patch_rename():
    """`sfs project set --name` resolves the project then PATCHes it."""

    class _Router:
        """Routes the GET (resolve) and PATCH (rename) calls."""

        def __init__(self):
            self.calls: list[dict] = []

        async def __call__(self, method, path, api_url, api_key, json_data=None):
            self.calls.append({"method": method, "path": path, "body": json_data})
            if method == "GET":
                return {"id": "proj_set", "name": "old-name"}
            if method == "PATCH":
                return {"id": "proj_set", "name": json_data["name"]}
            return {}

    router = _Router()
    with patch(
        "sessionfs.cli.cmd_project._get_project_client",
        return_value=("http://api", "key"),
    ), patch(
        "sessionfs.cli.cmd_project._get_git_remote",
        return_value="git@github.com:acme/repo.git",
    ), patch(
        "sessionfs.cli.cmd_project._normalize_remote",
        return_value="github.com/acme/repo",
    ), patch("sessionfs.cli.cmd_project._api_request", new=router):
        result = runner.invoke(project_app, ["set", "--name", "new-name"])

    assert result.exit_code == 0, _combined(result)
    patch_calls = [c for c in router.calls if c["method"] == "PATCH"]
    assert len(patch_calls) == 1
    assert patch_calls[0]["path"] == "/api/v1/projects/proj_set"
    assert patch_calls[0]["body"] == {"name": "new-name"}
    assert "new-name" in _combined(result)
