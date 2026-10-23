"""GitLab integration settings never hand a stored secret back."""

from __future__ import annotations

URL = "/api/v1/settings/gitlab"


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
