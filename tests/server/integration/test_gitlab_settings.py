"""GitLab integration settings never hand a stored secret back."""

from __future__ import annotations

import socket

import pytest

URL = "/api/v1/settings/gitlab"

_HOSTS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """Deterministic DNS: unknown hosts resolve to a public address."""
    _HOSTS.clear()

    def fake_getaddrinfo(host, port, *args, **kwargs):
        ip = _HOSTS.get(host, host if host[0].isdigit() or ":" in host else "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.delenv("SFS_GITLAB_ALLOW_PRIVATE_HOSTS", raising=False)


async def test_not_configured(client, auth_headers):
    resp = await client.get(URL, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "instance_url": "https://gitlab.com",
        "has_token": False,
        "has_webhook_secret": False,
        "webhook_secret": None,
    }


async def test_generated_secret_is_shown_once(client, auth_headers):
    resp = await client.put(URL, headers=auth_headers, json={"access_token": "glpat-test"})
    assert resp.status_code == 200
    generated = resp.json()["webhook_secret"]
    assert generated and len(generated) == 32
    assert resp.json()["has_webhook_secret"] is True

    got = (await client.get(URL, headers=auth_headers)).json()
    assert got["webhook_secret"] is None
    assert got["has_webhook_secret"] is True
    assert got["has_token"] is True
    assert generated not in str(got)


async def test_supplied_secret_is_never_echoed(client, auth_headers):
    resp = await client.put(
        URL,
        headers=auth_headers,
        json={"access_token": "glpat-test", "webhook_secret": "my-own-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["webhook_secret"] is None
    assert "my-own-secret" not in resp.text
    assert "my-own-secret" not in (await client.get(URL, headers=auth_headers)).text


async def test_update_without_secret_keeps_it_and_shows_nothing(client, auth_headers):
    await client.put(URL, headers=auth_headers, json={"access_token": "a", "webhook_secret": "keep-me"})
    resp = await client.put(
        URL,
        headers=auth_headers,
        json={"access_token": "b", "instance_url": "https://gitlab.example.com"},
    )
    body = resp.json()
    assert body["webhook_secret"] is None
    assert body["has_webhook_secret"] is True
    assert body["instance_url"] == "https://gitlab.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "http://gitlab.example.com",
        "ftp://gitlab.example.com",
        "https://127.0.0.1",
        "https://localhost",
        "https://10.1.2.3",
        "https://192.168.1.5",
        "https://169.254.169.254",
        "https://[::1]",
        "https://user:pw@gitlab.example.com",
    ],
)
async def test_cloud_rejects_unsafe_urls(client, auth_headers, url):
    _HOSTS["localhost"] = "127.0.0.1"
    resp = await client.put(
        URL, headers=auth_headers, json={"access_token": "t", "instance_url": url}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_gitlab_instance_url"
    assert (await client.get(URL, headers=auth_headers)).json()["has_token"] is False


async def test_cloud_rejects_host_resolving_private(client, auth_headers):
    _HOSTS["gitlab.evil.test"] = "10.0.0.7"
    resp = await client.put(
        URL,
        headers=auth_headers,
        json={"access_token": "t", "instance_url": "https://gitlab.evil.test"},
    )
    assert resp.status_code == 422


async def test_cloud_accepts_public_https_and_strips_slash(client, auth_headers):
    resp = await client.put(
        URL,
        headers=auth_headers,
        json={"access_token": "t", "instance_url": "https://gitlab.example.com/"},
    )
    assert resp.status_code == 200
    assert resp.json()["instance_url"] == "https://gitlab.example.com"


async def test_self_hosted_allows_internal_hosts(client, auth_headers, monkeypatch):
    monkeypatch.setenv("SFS_GITLAB_ALLOW_PRIVATE_HOSTS", "true")
    _HOSTS["gitlab.corp.internal"] = "10.4.4.4"
    for url in ("http://gitlab.corp.internal", "https://192.168.1.5", "http://127.0.0.1:8080"):
        resp = await client.put(
            URL, headers=auth_headers, json={"access_token": "t", "instance_url": url}
        )
        assert resp.status_code == 200, (url, resp.text)


async def test_self_hosted_still_blocks_metadata(client, auth_headers, monkeypatch):
    monkeypatch.setenv("SFS_GITLAB_ALLOW_PRIVATE_HOSTS", "true")
    _HOSTS["sneaky.test"] = "169.254.169.254"
    for url in ("http://169.254.169.254", "https://sneaky.test", "ftp://x.test"):
        resp = await client.put(
            URL, headers=auth_headers, json={"access_token": "t", "instance_url": url}
        )
        assert resp.status_code == 422, url
