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
