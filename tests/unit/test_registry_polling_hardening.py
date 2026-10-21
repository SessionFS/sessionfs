"""Registry polling hardening (Sentinel review of GitHub #69): canonical
hosts only (no percent-encoding / backslash / unicode / userinfo in the
authority), the resolver pin keyed on the exact name libcurl uses, and git
transport failures typed apart from content refusals."""

from __future__ import annotations

import http.server
import socket
import threading
import time
from pathlib import Path

import pytest

from sessionfs.server.services import registry_polling as rp
from sessionfs.skills.models import SkillError
from sessionfs.skills.sources import GitTransportError, canonical_git_host, normalize_git_url
from tests.unit.test_registry_polling import needs_git


@pytest.mark.parametrize(
    "url",
    [
        "https://%6deta.evil.com/acme/skills",  # percent-encoded host
        "https://git.example.com%2f@169.254.169.254/x",
        "https://git.example.com%00.evil.com/x",
        "https://evil.com\\@git.example.com/x",  # backslash
        "https://git\\example.com/x",
        "https://bücher.example/x",  # unicode (IDN must be punycode)
        "https://user@git.example.com/x",  # userinfo
        "https://user:pw@git.example.com/x",
        "https://a@b@git.example.com/x",
        "https://git_example.com/x",
        "https://0x7f.1/x",  # numeric forms resolvers read differently
        "https://2130706433/x",
        "https://010.0.0.1/x",
        "https://[not-ip]/x",
    ],
)
def test_non_canonical_authorities_are_refused_everywhere(url: str) -> None:
    with pytest.raises(SkillError):
        normalize_git_url(url)
    with pytest.raises(rp.FetchBlocked):
        rp.check_poll_url(url)


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("Git.Example.COM", "git.example.com"),
        ("git.example.com.", "git.example.com"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ("[2606:4700:4700:0:0:0:0:1111]", "2606:4700:4700::1111"),
        ("140.82.112.3", "140.82.112.3"),
    ],
)
def test_canonical_host_spelling(raw: str, canonical: str) -> None:
    assert canonical_git_host(raw) == canonical


def test_trailing_dot_and_case_pin_one_identical_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []

    def resolve(host: str, port: int) -> list[str]:
        seen.append(host)
        return ["140.82.112.3"]

    monkeypatch.setattr(rp, "resolve_host", resolve)
    for spelling in ("https://Git.Example.com./acme/skills", "https://git.example.com/acme/skills"):
        transport = rp.prepare_transport(spelling, None, tmp_path)
        assert transport.url == "https://git.example.com/acme/skills"
        assert "http.curloptResolve=git.example.com:443:140.82.112.3" in transport.config
    assert seen == ["git.example.com", "git.example.com"]


@needs_git
def test_curl_uses_the_pinned_name_byte_for_byte(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """End to end through real git/libcurl: a name that does not exist in
    DNS (.test) is reachable ONLY via the pin, so a request arriving at the
    local server proves curl looked the URL host up in the pin we built."""
    hosts: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib API
            hosts.append(self.headers.get("Host", ""))
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(rp, "resolve_host", lambda host, p: ["127.0.0.1"])
        production = rp.prepare_transport(f"https://Registry.Example.TEST.:{port}/acme/skills", None, tmp_path)
        assert f"http.curloptResolve=registry.example.test:{port}:127.0.0.1" in production.config
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        env = rp._base_env(sandbox)
        env["GIT_ALLOW_PROTOCOL"] = "http"  # the local server speaks plain http
        transport = rp.GitTransport(url=production.url.replace("https://", "http://"), env=env, config=production.config)
        with pytest.raises(GitTransportError):
            rp.list_remote_refs(transport, sandbox)
        assert hosts and hosts[0] == f"registry.example.test:{port}"
    finally:
        server.shutdown()
        server.server_close()


@needs_git
def test_http_5xx_is_a_transport_error_not_a_content_refusal(tmp_path: Path) -> None:
    class Bad(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib API
            self.send_response(502)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bad)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        env = rp._base_env(sandbox)
        env["GIT_ALLOW_PROTOCOL"] = "http"
        transport = rp.GitTransport(url=f"http://127.0.0.1:{server.server_address[1]}/acme/skills", env=env, config=())
        with pytest.raises(GitTransportError):
            rp.list_remote_refs(transport, sandbox)
        ref = rp.RemoteRef("v1.0.0", "refs/tags/v1.0.0", "a" * 40)
        with pytest.raises(GitTransportError):
            rp.fetch_ref_packages(transport, ref, deadline=time.monotonic() + 60)
        with pytest.raises(GitTransportError):
            rp.fetch_ref_packages(transport, ref, deadline=time.monotonic() - 1)
    finally:
        server.shutdown()
        server.server_close()


def test_resolver_sees_only_the_canonical_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    asked: list[str] = []

    def getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.112.3", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    rp.prepare_transport("https://GIT.example.com./acme/x", None, tmp_path)
    assert asked == ["git.example.com"]
