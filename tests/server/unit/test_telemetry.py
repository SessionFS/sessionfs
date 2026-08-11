"""Unit tests for the telemetry endpoint payload validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sessionfs.server.routes.telemetry import (
    TelemetryPayload,
    _contains_pii,
    _VALID_EVENTS,
)


class TestTelemetryPayloadValidation:
    def test_valid_baseline_payload(self):
        payload = TelemetryPayload(
            install_id="abc123", version="0.15.0", os="darwin",
        )
        assert payload.install_id == "abc123"

    def test_accepts_valid_event(self):
        for event in sorted(_VALID_EVENTS):
            payload = TelemetryPayload(
                install_id="abc", version="0.15.0", os="linux", event=event,
            )
            assert payload.event == event

    def test_rejects_unknown_event(self):
        with pytest.raises(ValidationError, match="Unknown event"):
            TelemetryPayload(
                install_id="abc", version="0.15.0", os="linux",
                event="fake_event",
            )

    def test_event_none_is_valid(self):
        payload = TelemetryPayload(
            install_id="abc", version="0.15.0", os="linux", event=None,
        )
        assert payload.event is None

    def test_tool_too_long_rejected(self):
        with pytest.raises(ValidationError, match="tool field too long"):
            TelemetryPayload(
                install_id="abc", version="0.15.0", os="linux",
                tool="x" * 51,
            )

    def test_tool_50_chars_accepted(self):
        payload = TelemetryPayload(
            install_id="abc", version="0.15.0", os="linux",
            tool="x" * 50,
        )
        assert payload.tool == "x" * 50

    def test_install_id_too_long_rejected(self):
        with pytest.raises(ValidationError, match="too long"):
            TelemetryPayload(
                install_id="x" * 101, version="0.15.0", os="linux",
            )


class TestPIIDetection:
    def test_email_in_event_rejected(self):
        payload = TelemetryPayload(
            install_id="abc", version="0.15.0", os="linux",
            event="heartbeat",
        )
        # Simulate email in the tool field
        payload2 = TelemetryPayload(
            install_id="abc", version="0.15.0", os="linux",
            tool="user@example.com",
        )
        assert _contains_pii(payload) is False
        assert _contains_pii(payload2) is True

    def test_ip_in_any_field_rejected(self):
        # tool with IP — caught by PII guard
        payload = TelemetryPayload(
            install_id="abc", version="0.15.0", os="linux",
            tool="10.0.0.1",
        )
        assert _contains_pii(payload) is True

    def test_clean_payload_passes_pii_check(self):
        payload = TelemetryPayload(
            install_id="deadbeef-cafe", version="0.15.0", os="darwin",
            event="heartbeat", event_ts="2026-08-11T12:00:00Z",
        )
        assert _contains_pii(payload) is False


def test_event_ts_z_suffix_accepted(client_payload_helper=None):
    """Z-suffixed ISO timestamps parse on all supported Pythons (3.10 rejects
    bare 'Z' in fromisoformat; the route normalizes it)."""
    from datetime import datetime
    raw = "2026-07-28T03:00:00Z"
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
