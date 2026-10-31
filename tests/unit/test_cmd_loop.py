"""Unit tests for `sfs loop start` and `sfs loop status` (Review Loop D1).

Both commands shell out to the same `_api_request` helper the other CLI
commands use, so the tests patch `_resolve_project` + `_api_request` and
assert on the URL, method and body the command sends (plus the rendered
output), without ever hitting the network.
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_loop import loop_app


class _FakeApi:
    """Records each call and returns the scripted (status, body)."""

    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        self.calls: list[dict] = []

    async def __call__(
        self,
        method,
        path,
        api_url,
        api_key,
        json_data=None,
        extra_headers=None,
        params=None,
        timeout=30,
    ):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "api_url": api_url,
                "api_key": api_key,
                "json_data": json_data,
                "params": params,
            }
        )
        return self.status, self.body, {}


LOOP_PAYLOAD = {
    "loop_id": "tk_loop1",
    "queue_id": "wq_1",
    "tickets": [{"id": "tk_a", "title": "Child 1", "brief_markdown": "do A"}],
    "reviewer": {
        "persona": "reviewer",
        "trusted": False,
        "setup_hint": "Register one.",
    },
    "env": {
        "implementer": {"SESSIONFS_PROFILE": "implementer"},
        "reviewer": {"SESSIONFS_PROFILE": "reviewer"},
    },
}


def _invoke(fake: _FakeApi, argv: list[str]):
    runner = CliRunner()
    with (
        patch(
            "sessionfs.cli.cmd_loop._resolve_project",
            return_value=("https://api.test", "key", "proj_x"),
        ),
        patch("sessionfs.cli.cmd_loop._api_request", new=fake),
    ):
        return runner.invoke(loop_app, argv)


def test_loop_start_builds_body_and_renders():
    fake = _FakeApi(201, LOOP_PAYLOAD)
    result = _invoke(
        fake,
        [
            "start",
            "My Loop",
            "--reviewer-persona",
            "reviewer",
            "--ticket",
            "Child 1::do A",
            "--token-budget",
            "5000",
            "--max-review-rounds",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/projects/proj_x/loops"
    assert call["api_url"] == "https://api.test"
    assert call["json_data"] == {
        "tickets": [
            {"title": "Child 1", "description": "do A", "acceptance_criteria": []}
        ],
        "reviewer_persona": "reviewer",
        "title": "My Loop",
        "token_budget": 5000,
        "max_review_rounds": 3,
    }
    assert "Loop started" in result.output
    assert "tk_loop1" in result.output
    assert "is not trusted yet" in result.output


def test_loop_start_adopts_existing_issue():
    fake = _FakeApi(201, LOOP_PAYLOAD)
    result = _invoke(
        fake,
        [
            "start",
            "tk_123",
            "--reviewer-persona",
            "reviewer",
            "--ticket",
            "Child 1",
            "--acceptance",
            "must build",
        ],
    )
    assert result.exit_code == 0, result.output
    body = fake.calls[0]["json_data"]
    assert body["from_ticket_id"] == "tk_123"
    assert "title" not in body
    assert body["tickets"] == [
        {"title": "Child 1", "description": "", "acceptance_criteria": ["must build"]}
    ]


def test_loop_start_reports_api_error():
    fake = _FakeApi(403, {"detail": "nope"})
    result = _invoke(
        fake,
        [
            "start",
            "My Loop",
            "--reviewer-persona",
            "reviewer",
            "--ticket",
            "Child 1",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "API error (403)" in result.output


def test_loop_start_rejects_empty_ticket_title():
    fake = _FakeApi(201, LOOP_PAYLOAD)
    result = _invoke(
        fake,
        [
            "start",
            "My Loop",
            "--reviewer-persona",
            "reviewer",
            "--ticket",
            "::no title",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "empty title" in result.output
    assert not fake.calls


def test_loop_status_single():
    fake = _FakeApi(200, {
        "id": "tk_42",
        "title": "Loop",
        "children": [
            {
                "title": "c",
                "status": "open",
                "branch": "feat/x",
                "head_sha": "abc",
                "round": 1,
                "harmful_open": 0,
                "verdict": None,
                "ready": False,
            }
        ],
    })
    result = _invoke(fake, ["status", "tk_42"])
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/v1/projects/proj_x/loops/tk_42"


def test_loop_status_list():
    fake = _FakeApi(200, [{"id": "tk_1", "title": "Loop", "children": []}])
    result = _invoke(fake, ["status"])
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/v1/projects/proj_x/loops"


def test_loop_status_404():
    fake = _FakeApi(404, {"detail": "Loop not found"})
    result = _invoke(fake, ["status", "tk_missing"])
    assert result.exit_code == 1, result.output
    assert "Loop not found" in result.output


def test_loop_update_sends_patch_and_renders():
    fake = _FakeApi(200, {
        "id": "tk_42",
        "title": "Loop",
        "children": [
            {
                "title": "c",
                "status": "open",
                "branch": "feat/x",
                "head_sha": "abc",
                "round": 1,
                "harmful_open": 0,
                "verdict": None,
                "ready": False,
            }
        ],
    })
    result = _invoke(
        fake,
        ["update", "tk_42", "--token-budget", "9000", "--max-review-rounds", "5"],
    )
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["method"] == "PATCH"
    assert call["path"] == "/api/v1/projects/proj_x/loops/tk_42"
    assert call["json_data"] == {"token_budget": 9000, "max_review_rounds": 5}


def test_loop_update_omits_unset_flags():
    fake = _FakeApi(200, {"id": "tk_42", "title": "Loop", "children": []})
    result = _invoke(fake, ["update", "tk_42", "--cost-budget-usd", "12.5"])
    assert result.exit_code == 0, result.output
    assert fake.calls[0]["json_data"] == {"cost_budget_usd": 12.5}


def test_loop_update_404():
    fake = _FakeApi(404, {"detail": "Loop not found"})
    result = _invoke(fake, ["update", "tk_missing", "--token-budget", "10"])
    assert result.exit_code == 1, result.output
    assert "Loop not found" in result.output


def test_loop_close_item_sends_post():
    fake = _FakeApi(200, {
        "ticket_id": "tk_child",
        "loop_id": "tk_loop",
        "item_status": "done",
        "closed_by": {"kind": "user", "user_id": "u1"},
    })
    result = _invoke(fake, ["close-item", "tk_loop", "tk_child"])
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert (
        call["path"]
        == "/api/v1/projects/proj_x/loops/tk_loop/children/tk_child/close"
    )
    assert "settled as done" in result.output


def test_loop_close_item_409():
    fake = _FakeApi(409, {"detail": "Only a needs_human loop item can be closed by hand"})
    result = _invoke(fake, ["close-item", "tk_loop", "tk_child"])
    assert result.exit_code == 1, result.output
    assert "needs_human" in result.output


# ── Worker runs and review rounds (Review Loop D3) ──


def test_loop_run_start_output_id_prints_only_the_id():
    fake = _FakeApi(201, {"run_id": "run_abc", "lease_epoch": 2, "loop_id": "tk_l"})
    result = _invoke(
        fake, ["run", "start", "tk_a", "--model", "m", "--provider", "p", "--branch", "b", "--output-id"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "run_abc"
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/projects/proj_x/agent-runs/start-worker"
    assert call["json_data"] == {
        "ticket_id": "tk_a", "model": "m", "provider": "p", "branch": "b",
    }


def test_loop_run_finish_sends_usage():
    fake = _FakeApi(200, {"run_id": "run_abc", "status": "passed", "tokens": 15})
    result = _invoke(
        fake,
        ["run", "finish", "run_abc", "--head-sha", "ABC1234", "--input-tokens", "10",
         "--output-tokens", "5", "--cost-usd", "0.5"],
    )
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["path"] == "/api/v1/projects/proj_x/agent-runs/run_abc/finish"
    assert call["json_data"] == {
        "head_sha": "abc1234",
        "exit_reason": "finished",
        "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5},
    }


def test_loop_review_reads_findings_file(tmp_path):
    f = tmp_path / "f.json"
    f.write_text('[{"severity":"high","class":"new","harm":"crash","title":"t"}]')
    fake = _FakeApi(
        201, {"round": 1, "stage": "design", "verdict_trusted": True, "item_status": None}
    )
    sha = "a" * 40
    result = _invoke(
        fake,
        ["review", "tk_l", "--ticket", "tk_l", "--round", "1", "--commit", sha,
         "--verdict", "changes-requested", "--findings-file", str(f), "--stage", "design"],
    )
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["path"] == "/api/v1/projects/proj_x/loops/tk_l/rounds"
    assert call["json_data"]["verdict"] == "CHANGES-REQUESTED"
    assert call["json_data"]["stage"] == "design"
    assert call["json_data"]["findings"][0]["class"] == "new"


def test_loop_run_start_prints_api_error():
    fake = _FakeApi(409, {"error": {"code": "409", "message": "parked", "details": {}}})
    result = _invoke(fake, ["run", "start", "tk_a"])
    assert result.exit_code == 1


def test_share_story_mints_link_and_prints_token_once():
    fake = _FakeApi(201, {
        "id": "lsl_1",
        "kind": "story",
        "ticket_id": "tk_1",
        "url": "https://app.example/share/loop/sekrit",
        "token": "sekrit",
        "expires_at": "2026-11-29T00:00:00+00:00",
    })
    result = _invoke(fake, ["share", "story", "tk_1", "--expires-days", "7"])
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/projects/proj_x/loop-share-links"
    assert call["json_data"] == {
        "kind": "story",
        "ticket_id": "tk_1",
        "expires_days": 7,
    }
    assert "sekrit" in result.output
    assert "not stored" in result.output


def test_share_ship_report_mints_link():
    fake = _FakeApi(201, {
        "id": "lsl_2",
        "kind": "ship_report",
        "url": "https://app.example/share/loop/sekrit2",
        "token": "sekrit2",
    })
    result = _invoke(fake, ["share", "ship-report", "0.30.0"])
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["json_data"] == {"kind": "ship_report", "release_version": "0.30.0"}
    assert "sekrit2" in result.output


def test_share_list_renders_without_tokens():
    fake = _FakeApi(200, [
        {
            "id": "lsl_1",
            "kind": "story",
            "ticket_id": "tk_1",
            "release_version": None,
            "month": None,
            "expires_at": "2026-11-29T00:00:00+00:00",
            "revoked_at": None,
        }
    ])
    result = _invoke(fake, ["share", "list"])
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/v1/projects/proj_x/loop-share-links"
    assert "lsl_1" in result.output
    assert "tk_1" in result.output
    assert "token" not in result.output


def test_share_revoke_deletes():
    fake = _FakeApi(204, None)
    result = _invoke(fake, ["share", "revoke", "lsl_1"])
    assert result.exit_code == 0, result.output
    call = fake.calls[0]
    assert call["method"] == "DELETE"
    assert call["path"] == "/api/v1/projects/proj_x/loop-share-links/lsl_1"
    assert "Revoked" in result.output


def test_share_revoke_404():
    fake = _FakeApi(404, {"detail": "Share link not found"})
    result = _invoke(fake, ["share", "revoke", "lsl_missing"])
    assert result.exit_code == 1, result.output
    assert "API error (404)" in result.output
