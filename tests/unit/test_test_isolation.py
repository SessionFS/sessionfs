"""The test-suite safeguard itself: no real credentials, no outside network."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from tests.conftest import DUMMY_API_KEY, UNROUTABLE_API_URL, ExternalNetworkBlocked


def test_every_test_gets_a_throwaway_home():
    home = Path.home()
    assert home == Path(os.environ["HOME"])
    assert not (home / ".sessionfs" / "config.toml").exists()


def test_credentials_resolve_to_the_dummy_unroutable_server():
    from sessionfs.profiles import resolve_auth
    from sessionfs.skills.hosted import client_from_auth

    auth = resolve_auth()
    assert (auth.api_url, auth.api_key) == (UNROUTABLE_API_URL, DUMMY_API_KEY)
    client = client_from_auth()
    assert client is not None and client.api_url == UNROUTABLE_API_URL


@pytest.mark.parametrize("address", [("93.184.216.34", 443), ("api.sessionfs.dev", 443)])
def test_outbound_connections_to_non_local_hosts_are_refused(address):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(ExternalNetworkBlocked):
            sock.connect(address)


def test_loopback_stays_reachable():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=5):
            pass
