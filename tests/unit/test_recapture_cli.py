"""Unit tests for ``sfs recapture`` (explicit user action)."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_recapture_uses_skip_excluded_false(monkeypatch, tmp_path: Path):
    """An explicit recapture must ignore the exclusion list, like ``sfs pull``.

    ``recapture`` is a deliberate user action ("re-capture this session from
    its native source"), so it must not silently skip a session a delete marked
    excluded — the user asked for the current source state, exactly as pull
    overrides an exclusion.
    """
    from unittest.mock import MagicMock

    from sessionfs.cli import cmd_recapture
    from sessionfs.store.local import CommitResult
    from sessionfs.watchers.base import NativeSessionRef

    monkeypatch.setattr(
        "sessionfs.watchers.codex.parse_codex_session",
        lambda _p: object(),
    )
    monkeypatch.setattr(
        "sessionfs.watchers.codex.convert_codex_to_sfs",
        lambda _session, _staging, session_id: None,
    )

    store = MagicMock()
    store.commit_session.return_value = CommitResult(True, None, None)

    ref = NativeSessionRef(
        tool="codex",
        native_session_id="n1",
        native_path=str(tmp_path),
        sfs_session_id="ses_aaaa1111bbbb2222",
    )

    cmd_recapture._recapture_codex(store, "ses_aaaa1111bbbb2222", ref, tmp_path)

    store.commit_session.assert_called_once()
    assert store.commit_session.call_args.kwargs.get("skip_excluded") is False
