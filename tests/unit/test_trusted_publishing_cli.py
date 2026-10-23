"""CLI side of trusted publishing and publisher targets."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from sessionfs.skills import publishing

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    # CI forces colour, and usage errors come in a box that wraps lines.
    return " ".join(_ANSI_RE.sub("", s).replace("│", " ").split())


def test_personal_public_target_sends_visibility() -> None:
    target = publishing.resolve_target(org_id=None, visibility="public", namespace="acme", collection=None)
    assert target.path == "/api/v1/users/me/skills/packages"
    assert target.form() == {"namespace": "acme", "collection": "personal", "visibility": "public"}
    assert "personal publisher" in target.describe()
    with pytest.raises(ValueError):
        publishing.resolve_target(org_id=None, visibility="org", namespace=None, collection=None)


def test_github_oidc_token_requests_audience_without_leaking() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"value": "jwt-value"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    env = {
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://pipelines.example/token?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runner-secret",
    }
    assert publishing.github_oidc_token("sessionfs", env=env, client=client) == "jwt-value"
    assert seen["url"].endswith("api-version=2.0&audience=sessionfs")
    assert seen["auth"] == "Bearer runner-secret"
    with pytest.raises(publishing.TrustedPublishingError, match="id-token: write"):
        publishing.github_oidc_token("sessionfs", env={}, client=client)


def test_exchange_and_upload() -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            calls.append(("token", json.loads(request.content)))
            return httpx.Response(200, json={"token": "sfs_tp_x", "namespace": "acme"})
        calls.append(("publish", {"auth": request.headers["authorization"]}))
        return httpx.Response(201, json={"version_id": "skv_1"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    body = publishing.exchange_trusted_publishing_token(
        api_url="https://api.example", oidc_token="jwt", namespace="acme", package="pkg", client=client
    )
    assert body["token"] == "sfs_tp_x"
    status, result, _ = publishing.upload_trusted(
        api_url="https://api.example", publish_token="sfs_tp_x", filename="p.tar.gz", data=b"x",
        visibility=None, collection=None, client=client,
    )
    assert status == 201 and result == {"version_id": "skv_1"}
    assert calls == [
        ("token", {"token": "jwt", "namespace": "acme", "package": "pkg"}),
        ("publish", {"auth": "Bearer sfs_tp_x"}),
    ]


def test_audience_is_read_from_the_server() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"enabled": True, "audience": "https://api.acme.example"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    audience = publishing.trusted_publishing_audience(api_url="https://api.acme.example", client=client)
    assert audience == "https://api.acme.example"
    assert seen == ["/api/v1/marketplace/trusted-publishing/config"]

    def off(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"enabled": False, "audience": None})

    with pytest.raises(publishing.TrustedPublishingError, match="not configured"):
        publishing.trusted_publishing_audience(
            api_url="https://x", client=httpx.Client(transport=httpx.MockTransport(off))
        )


def test_exchange_failure_is_explained() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "invalid_oidc_token", "message": "rejected", "details": {"reason": "invalid_aud"}}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(publishing.TrustedPublishingError, match="invalid_aud"):
        publishing.exchange_trusted_publishing_token(
            api_url="https://api.example", oidc_token="jwt", namespace="acme", package=None, client=client
        )


def test_trusted_validation_target_follows_the_publisher() -> None:
    assert publishing.trusted_validation_visibility(None, {"default_visibility": "user"}) == "personal"
    assert publishing.trusted_validation_visibility(None, {"default_visibility": "org"}) == "org"
    assert publishing.trusted_validation_visibility(None, {}) == "org"
    assert publishing.trusted_validation_visibility("public", {"default_visibility": "user"}) == "public"


# ---------------------------------------------------------------------------
# GitLab CI
# ---------------------------------------------------------------------------


def _fake_jwt(payload: dict) -> str:
    import base64

    def segment(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'RS256'})}.{segment(payload)}.sig"


def test_gitlab_id_token_is_read_from_the_named_variable() -> None:
    token = _fake_jwt({"aud": "https://api.acme.example"})
    assert publishing.gitlab_id_token("SFS_ID_TOKEN", env={"SFS_ID_TOKEN": token}) == token
    assert (
        publishing.gitlab_id_token(
            "MY_TOKEN", audience="https://api.acme.example", env={"MY_TOKEN": token}
        )
        == token
    )
    with pytest.raises(publishing.TrustedPublishingError, match="id_tokens") as missing:
        publishing.gitlab_id_token("SFS_ID_TOKEN", audience="https://api.acme.example", env={})
    assert "aud: https://api.acme.example" in str(missing.value)
    with pytest.raises(publishing.TrustedPublishingError, match="minted for audience") as wrong:
        publishing.gitlab_id_token(
            "SFS_ID_TOKEN", audience="https://api.other.example", env={"SFS_ID_TOKEN": token}
        )
    # The token itself never appears in an error.
    assert token not in str(wrong.value)
    with pytest.raises(publishing.TrustedPublishingError, match="Invalid environment variable"):
        publishing.gitlab_id_token("BAD-NAME", env={})


def test_gitlab_exchange_names_the_provider_and_github_stays_unchanged() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"token": "sfs_tp_x"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    publishing.exchange_trusted_publishing_token(
        api_url="https://api.example", oidc_token="jwt", namespace="acme", package=None,
        provider="gitlab", client=client,
    )
    publishing.exchange_trusted_publishing_token(
        api_url="https://api.example", oidc_token="jwt", namespace="acme", package=None, client=client
    )
    assert bodies == [
        {"token": "jwt", "provider": "gitlab", "namespace": "acme"},
        {"token": "jwt", "namespace": "acme"},
    ]


def test_audience_origin_must_match_the_api_url() -> None:
    publishing.check_audience_matches_api("https://api.acme.example", "https://api.acme.example/")
    publishing.check_audience_matches_api("https://API.acme.example:443", "https://api.acme.example")
    for audience in ("https://api.other.example", "http://api.acme.example", "sessionfs-prod-1",
                     "https://api.acme.example:8443"):
        with pytest.raises(publishing.TrustedPublishingError, match="--audience"):
            publishing.check_audience_matches_api(audience, "https://api.acme.example")


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_publish_refuses_a_foreign_audience_unless_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path, provider: str
) -> None:
    from types import SimpleNamespace

    import typer

    from sessionfs.cli import cmd_skill

    calls: list[str] = []
    token = _fake_jwt({"aud": "https://api.other.example"})
    monkeypatch.setenv("SFS_ID_TOKEN", token)
    monkeypatch.setattr(
        publishing, "validate_source",
        lambda _path, visibility: SimpleNamespace(ok=True, package=SimpleNamespace(name="pkg"), data=b"x", problems=[]),
    )
    monkeypatch.setattr("sessionfs.profiles.resolve_auth", lambda: SimpleNamespace(api_url="https://api.acme.example"))
    monkeypatch.setattr(publishing, "trusted_publishing_audience", lambda api_url: "https://api.other.example")

    def github_token(audience: str) -> str:
        calls.append(f"github:{audience}")
        return token

    def exchange(**kwargs) -> dict:
        calls.append(f"exchange:{kwargs['provider']}")
        return {"token": "sfs_tp_x", "default_visibility": "user"}

    monkeypatch.setattr(publishing, "github_oidc_token", github_token)
    monkeypatch.setattr(publishing, "exchange_trusted_publishing_token", exchange)
    monkeypatch.setattr(
        publishing, "upload_trusted",
        lambda **_kwargs: (201, {"name": "pkg", "signature": {}}, {}),
    )
    common = dict(source=str(tmp_path), visibility=None, namespace="acme", collection=None,
                  package_name=None, json_output=True, ci_provider=provider)
    with pytest.raises(typer.Exit):
        cmd_skill._publish_trusted(audience=None, **common)
    # Refused before any token was requested or exchanged.
    assert calls == []
    cmd_skill._publish_trusted(audience="https://api.other.example", **common)
    assert calls[-1] == f"exchange:{provider}"


def test_publisher_trust_gitlab_payload_and_exclusivity(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from sessionfs.cli import cmd_skill_publisher
    from sessionfs.cli.cmd_skill import skill_app

    calls: list[tuple[str, str, dict | None]] = []

    def fake(method: str, path: str, payload: dict | None = None) -> dict:
        calls.append((method, path, payload))
        return {"id": "stp_1", "provider": "gitlab", "gitlab_project_id": 4242,
                "gitlab_host": "https://gitlab.com", "repository": "acme/skills"}

    monkeypatch.setattr(cmd_skill_publisher, "_request", fake)
    runner = CliRunner()
    result = runner.invoke(skill_app, [
        "publisher", "trust", "pub_1", "--gitlab-project", "4242", "--gitlab-project-path", "acme/skills",
        "--ref-pattern", "refs/tags/v*", "--environment", "production", "--no-namespace-pin",
    ])
    assert result.exit_code == 0, result.output
    assert calls == [("POST", "/publishers/pub_1/trusted-publishers", {
        "provider": "gitlab", "gitlab_project_id": 4242, "repository": "acme/skills",
        "environment": "production", "ref_pattern": "refs/tags/v*",
    })]
    assert "4242" in result.output
    mixed = runner.invoke(skill_app, [
        "publisher", "trust", "pub_1", "--gitlab-project", "1", "--repo", "o/r", "--workflow", "r.yml",
    ])
    assert mixed.exit_code != 0 and len(calls) == 1
    neither = runner.invoke(skill_app, ["publisher", "trust", "pub_1"])
    assert neither.exit_code != 0 and len(calls) == 1
    host_only = runner.invoke(skill_app, ["publisher", "trust", "pub_1", "--gitlab-host", "https://git.acme.example"])
    assert host_only.exit_code != 0 and len(calls) == 1
    no_env = runner.invoke(skill_app, [
        "publisher", "trust", "pub_1", "--gitlab-project", "1", "--require-protected-environment",
    ])
    assert no_env.exit_code != 0 and len(calls) == 1
    github_flag = runner.invoke(skill_app, [
        "publisher", "trust", "pub_1", "--repo", "o/r", "--workflow", "r.yml", "--environment", "prod",
        "--require-protected-environment",
    ])
    assert github_flag.exit_code != 0 and len(calls) == 1
    protected = runner.invoke(skill_app, [
        "publisher", "trust", "pub_1", "--gitlab-project", "1", "--environment", "production",
        "--require-protected-environment", "--no-namespace-pin",
    ])
    assert protected.exit_code == 0, protected.output
    assert calls[-1][2] == {
        "provider": "gitlab", "gitlab_project_id": 1, "require_protected_environment": True,
        "environment": "production",
    }


def _trust_harness(monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from sessionfs.cli import cmd_skill_publisher
    from sessionfs.cli.cmd_skill import skill_app

    calls: list[dict | None] = []

    def fake(method: str, path: str, payload: dict | None = None) -> dict:
        calls.append(payload)
        return {"id": "stp_1", "provider": "gitlab", "gitlab_project_id": 4242,
                "gitlab_host": "https://gitlab.com", "repository": "project 4242"}

    monkeypatch.setattr(cmd_skill_publisher, "_request", fake)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("CI_JOB_TOKEN", raising=False)
    return CliRunner(), skill_app, calls


def _mock_gitlab(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def get(url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(lambda req: (seen.append(req), handler(req))[1])) as client:
            return client.get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", get)
    return seen


def test_gitlab_trust_pins_the_looked_up_namespace_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, app, calls = _trust_harness(monkeypatch)
    seen = _mock_gitlab(
        monkeypatch, lambda req: httpx.Response(200, json={"id": 4242, "namespace": {"id": 77}})
    )
    result = runner.invoke(app, ["publisher", "trust", "pub_1", "--gitlab-project", "4242"])
    assert result.exit_code == 0, result.output
    assert str(seen[0].url) == "https://gitlab.com/api/v4/projects/4242"
    assert "private-token" not in seen[0].headers and "job-token" not in seen[0].headers
    assert calls[-1]["gitlab_namespace_id"] == 77


def test_gitlab_lookup_uses_a_token_from_the_environment_without_printing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, app, calls = _trust_harness(monkeypatch)
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-secret-value")
    seen = _mock_gitlab(monkeypatch, lambda req: httpx.Response(200, json={"namespace": {"id": 5}}))
    result = runner.invoke(
        app, ["publisher", "trust", "pub_1", "--gitlab-project", "4242", "--gitlab-host", "https://git.acme.example"]
    )
    assert result.exit_code == 0, result.output
    assert str(seen[0].url) == "https://git.acme.example/api/v4/projects/4242"
    assert seen[0].headers["private-token"] == "glpat-secret-value"
    assert "glpat-secret-value" not in result.output
    assert calls[-1]["gitlab_namespace_id"] == 5


def test_gitlab_explicit_namespace_skips_the_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, app, calls = _trust_harness(monkeypatch)
    seen = _mock_gitlab(monkeypatch, lambda req: httpx.Response(500))
    result = runner.invoke(app, ["publisher", "trust", "pub_1", "--gitlab-project", "4242", "--gitlab-namespace", "9"])
    assert result.exit_code == 0, result.output
    assert seen == [] and calls[-1]["gitlab_namespace_id"] == 9


def test_gitlab_no_namespace_pin_warns_and_sends_no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, app, calls = _trust_harness(monkeypatch)
    seen = _mock_gitlab(monkeypatch, lambda req: httpx.Response(500))
    result = runner.invoke(app, ["publisher", "trust", "pub_1", "--gitlab-project", "4242", "--no-namespace-pin"])
    assert result.exit_code == 0, result.output
    assert "not pinned" in result.output
    assert seen == [] and "gitlab_namespace_id" not in calls[-1]
    both = runner.invoke(
        app, ["publisher", "trust", "pub_1", "--gitlab-project", "4242", "--no-namespace-pin", "--gitlab-namespace", "9"]
    )
    assert both.exit_code != 0 and len(calls) == 1
    mixed = runner.invoke(
        app, ["publisher", "trust", "pub_1", "--repo", "o/r", "--workflow", "r.yml", "--no-namespace-pin"]
    )
    assert mixed.exit_code != 0 and len(calls) == 1


@pytest.mark.parametrize(
    "handler",
    [
        lambda req: httpx.Response(404, json={"message": "404 Project Not Found"}),
        lambda req: httpx.Response(200, json={"id": 4242}),
        lambda req: httpx.Response(200, text="not json"),
        lambda req: (_ for _ in ()).throw(httpx.ConnectError("down")),
    ],
)
def test_gitlab_lookup_failure_refuses_and_names_both_flags(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    runner, app, calls = _trust_harness(monkeypatch)
    _mock_gitlab(monkeypatch, handler)
    result = runner.invoke(app, ["publisher", "trust", "pub_1", "--gitlab-project", "4242"])
    assert result.exit_code != 0 and calls == []
    out = _plain(result.output)
    assert "--gitlab-namespace" in out and "--no-namespace-pin" in out


def test_gitlab_server_warnings_are_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.cli import cmd_skill_publisher

    runner, app, _ = _trust_harness(monkeypatch)
    monkeypatch.setattr(
        cmd_skill_publisher, "_request",
        lambda *a, **k: {"id": "stp_1", "provider": "gitlab", "gitlab_project_id": 1, "repository": "r",
                         "gitlab_host": "h", "warnings": ["namespace is not pinned"]},
    )
    result = runner.invoke(app, ["publisher", "trust", "pub_1", "--gitlab-project", "1", "--gitlab-namespace", "3"])
    assert result.exit_code == 0 and "namespace is not pinned" in result.output
