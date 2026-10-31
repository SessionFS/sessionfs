"""Unit tests for `sfs org delivery-webhook rotate|status`.

Covers track F2b's CLI surface:
  - rotate POSTs the org's per-provider rotate endpoint and prints the
    one-time secret plus the inbound URL
  - status renders the configured / not-configured state
  - an unknown provider is refused locally (no request)
  - a non-2xx response surfaces a friendly error and exits non-zero

The HTTP boundary is patched on `sessionfs.cli.cmd_org._api_request` so the
tests are deterministic and need no running server.
"""

from __future__ import annotations

from typer.testing import CliRunner

from sessionfs.cli.cmd_org import org_app

runner = CliRunner()


def _invoke(monkeypatch, args: list[str], status: int, body, headers=None):
    monkeypatch.setattr(
        "sessionfs.cli.cmd_org._load_api_config",
        lambda: ("http://api.test", "k"),
    )
    monkeypatch.setattr("sessionfs.cli.cmd_org._org_id", lambda api_url, api_key: "org_x")

    calls: list[tuple] = []

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None, params=None):
        calls.append((method, path))
        return (status, body, headers or {})

    monkeypatch.setattr("sessionfs.cli.cmd_org._api_request", _fake_api)
    result = runner.invoke(org_app, ["delivery-webhook", *args])
    return result, calls


def test_rotate_prints_one_time_secret_and_url(monkeypatch):
    result, calls = _invoke(
        monkeypatch,
        ["rotate", "--provider", "gitlab"],
        200,
        {
            "webhook_secret": "whsec_abc",
            "webhook_secret_last4": "abc",
            "webhook_url": "http://api.test/api/v1/webhooks/gitlab/org_x",
        },
    )
    assert result.exit_code == 0, result.output
    assert calls == [("POST", "/api/v1/orgs/org_x/delivery-webhooks/gitlab/rotate")]
    assert "Rotated gitlab delivery-webhook secret" in result.output
    assert "whsec_abc" in result.output
    assert "http://api.test/api/v1/webhooks/gitlab/org_x" in result.output
    assert "shown only once" in result.output


def test_rotate_normalizes_provider_case(monkeypatch):
    result, calls = _invoke(
        monkeypatch,
        ["rotate", "--provider", "GitHub"],
        200,
        {
            "webhook_secret": "whsec_abc",
            "webhook_secret_last4": "abc",
            "webhook_url": "http://api.test/api/v1/webhooks/github/org_x",
        },
    )
    assert result.exit_code == 0, result.output
    assert calls == [("POST", "/api/v1/orgs/org_x/delivery-webhooks/github/rotate")]


def test_rotate_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr(
        "sessionfs.cli.cmd_org._load_api_config",
        lambda: ("http://api.test", "k"),
    )
    monkeypatch.setattr("sessionfs.cli.cmd_org._org_id", lambda api_url, api_key: "org_x")
    result = runner.invoke(
        org_app, ["delivery-webhook", "rotate", "--provider", "bitbucket"]
    )
    assert result.exit_code != 0
    assert "Unsupported provider" in result.output


def test_rotate_surfaces_api_error(monkeypatch):
    result, _ = _invoke(
        monkeypatch,
        ["rotate", "--provider", "github"],
        403,
        {"error": {"code": "403", "message": "Forbidden"}},
    )
    assert result.exit_code != 0
    assert "API error (403)" in result.output


def test_status_configured(monkeypatch):
    result, calls = _invoke(
        monkeypatch,
        ["status", "--provider", "gitlab"],
        200,
        {
            "has_secret": True,
            "secret_last4": "xyz9",
            "webhook_url": "http://api.test/api/v1/webhooks/gitlab/org_x",
        },
    )
    assert result.exit_code == 0, result.output
    assert calls == [("GET", "/api/v1/orgs/org_x/delivery-webhooks/gitlab/status")]
    assert "is configured" in result.output
    assert "xyz9" in result.output
    assert "http://api.test/api/v1/webhooks/gitlab/org_x" in result.output


def test_status_not_configured(monkeypatch):
    result, _ = _invoke(
        monkeypatch,
        ["status", "--provider", "github"],
        200,
        {"has_secret": False, "secret_last4": None, "webhook_url": "http://api.test/api/v1/webhooks/github/org_x"},
    )
    assert result.exit_code == 0, result.output
    assert "is not configured" in result.output


def test_status_surfaces_api_error(monkeypatch):
    result, _ = _invoke(
        monkeypatch,
        ["status", "--provider", "gitlab"],
        403,
        {"error": {"code": "403", "message": "Forbidden"}},
    )
    assert result.exit_code != 0
    assert "API error (403)" in result.output
