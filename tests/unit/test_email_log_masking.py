"""Recipient addresses must not appear in full in email log lines."""

from __future__ import annotations

import asyncio
import logging

import pytest

from sessionfs.server.email import NullProvider, mask_address


def testmask_address_keeps_domain_only() -> None:
    assert mask_address("alice@example.com") == "a***@example.com"
    assert mask_address("Alice <alice@example.com>") == "A***@example.com>"
    assert mask_address("not-an-address") == "***"
    assert mask_address(None) == "***"


def test_null_provider_log_hides_address_and_subject(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="sessionfs.email"):
        assert asyncio.run(NullProvider().send("alice@example.com", "Secret plan", "<p>x</p>"))
    text = caplog.text
    assert "alice@example.com" not in text
    assert "Secret plan" not in text
    assert "a***@example.com" in text


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("send_transfer_proposed", {"org_name": "o", "proposer_name": "p", "reason": "", "expires_at_human": "soon"}),
        ("send_transfer_proposal_declined", {"org_name": "o", "owner_name": "w"}),
        ("send_transfer_proposal_expired", {}),
    ],
)
def test_transfer_subjects_strip_control_characters(method: str, kwargs: dict) -> None:
    """A project name with CR/LF can't inject headers through the subject."""
    sent: list[str] = []

    class _Capture(NullProvider):
        async def send(self, to, subject, html, text=None):  # type: ignore[override]
            sent.append(subject)
            return True

    import inspect

    fn = getattr(_Capture(), method)
    params = inspect.signature(fn).parameters
    call = {k: v for k, v in kwargs.items() if k in params}
    for name, p in params.items():
        if name not in call and name not in ("to_email", "project_name") and p.default is inspect.Parameter.empty:
            call[name] = "x"
    asyncio.run(fn(to_email="a@b.c", project_name="evil\r\nBcc: victim@x.com", **call))
    assert sent and "\r" not in sent[0] and "\n" not in sent[0]
