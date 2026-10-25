"""Tests for the derived session-changes cache single-flight guard."""

from __future__ import annotations

import threading
import time

from sessionfs.server.routes.sessions import (
    _changes_inflight,
    _get_cached_session_changes,
    clear_archive_cache,
)
from sessionfs.server.services import session_changes as sc_mod


def test_single_flight_computes_once(monkeypatch):
    """Concurrent requests for the same uncached key must share one derivation."""
    clear_archive_cache()
    calls: list[int] = []
    gate = threading.Event()

    def fake_derive(messages, workspace):
        calls.append(1)
        gate.wait(timeout=5)  # hold the computation open so both requests overlap
        return {"files": [], "git_ref": None, "truncated": False}

    monkeypatch.setattr(sc_mod, "derive_session_changes", fake_derive)

    content = {"messages": [], "workspace": None}
    results: list[dict] = []

    def worker() -> None:
        results.append(_get_cached_session_changes("s1", "etag1", content))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()

    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1  # exactly one computation started

    gate.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    assert len(calls) == 1  # the second request waited on the first, not re-derived
    assert len(results) == 2
    assert results[0] is results[1]
    assert _changes_inflight == {}  # the per-key guard is cleaned up


def test_single_flight_waiter_re_raises_owner_error(monkeypatch):
    """When the owner's derivation fails, waiters must re-raise the same error
    instead of KeyError on a cache entry that never got a result."""
    clear_archive_cache()
    started = threading.Event()
    release = threading.Event()

    class Boom(Exception):
        pass

    def fake_derive(messages, workspace):
        started.set()
        release.wait(timeout=5)  # hold the failing computation open
        raise Boom("derivation failed")

    monkeypatch.setattr(sc_mod, "derive_session_changes", fake_derive)

    content = {"messages": [], "workspace": None}
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            _get_cached_session_changes("s1", "etag1", content)
        except Boom as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()

    assert started.wait(timeout=5)  # the owner began deriving
    # Give the loser a moment to observe the inflight entry and block on it.
    time.sleep(0.05)
    release.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    assert len(errors) == 2
    assert all(isinstance(e, Boom) for e in errors)
    assert errors[0] is errors[1]  # waiters re-raise the very same exception object
    assert _changes_inflight == {}  # the per-key guard is cleaned up even on failure
