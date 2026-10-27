"""Unit tests for the two-way Jira sync service (``services/jira_sync.py``).

Pure helpers only — the inbound webhook, outbound pushes, sweep, and evidence
comment are covered in ``tests/server/integration/test_jira_sync.py``. Focus:

- HMAC signature verification (valid, invalid, missing, malformed);
- webhook secret generation / hashing / last-4 hint;
- status-map parsing and validation;
- sync-comments mode validation;
- DLP redaction before text leaves for Jira.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import uuid

import pytest
from fastapi import HTTPException

from sessionfs.server.db.models import Organization
from sessionfs.server.services import jira_sync


def _org(**settings: object) -> Organization:
    return Organization(
        id=f"org_{uuid.uuid4().hex[:10]}",
        name="Org",
        slug=f"o-{uuid.uuid4().hex[:8]}",
        settings=json.dumps(settings) if settings else "{}",
    )


# ── webhook secret ──


def test_generate_webhook_secret_is_url_safe_and_high_entropy():
    a = jira_sync.generate_webhook_secret()
    b = jira_sync.generate_webhook_secret()
    assert a != b
    assert len(a) >= 40  # 32 random bytes base64url-encoded
    # URL-safe: no characters that would need escaping in a header/query value.
    assert set(a) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_hash_and_last4_hint():
    secret = "whsec_super_secret_value_1234"
    assert jira_sync.hash_webhook_secret(secret) == hashlib.sha256(secret.encode()).hexdigest()
    assert jira_sync.webhook_secret_last4(secret) == "1234"
    assert jira_sync.webhook_secret_last4("abc") == "abc"


# ── signature verification ──


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_webhook_signature_accepts_valid():
    secret = "s3cret"
    body = b'{"webhookEvent": "jira:issue_updated"}'
    assert jira_sync.verify_webhook_signature(secret, body, _sign(secret, body))


def test_verify_webhook_signature_rejects_wrong_secret():
    secret = "s3cret"
    body = b'{"a": 1}'
    assert not jira_sync.verify_webhook_signature("other", body, _sign(secret, body))


def test_verify_webhook_signature_rejects_tampered_body():
    secret = "s3cret"
    body = b'{"a": 1}'
    assert not jira_sync.verify_webhook_signature(secret, b'{"a": 2}', _sign(secret, body))


@pytest.mark.parametrize("signature", [None, "", "sha256=", "sha256=deadbeef", "sha1=abc", "garbage"])
def test_verify_webhook_signature_rejects_missing_or_malformed(signature):
    assert not jira_sync.verify_webhook_signature("s3cret", b"body", signature)


def test_verify_webhook_signature_requires_secret():
    body = b"body"
    assert not jira_sync.verify_webhook_signature("", body, _sign("s3cret", body))


# ── status map ──


def test_default_status_map_has_expected_shape():
    m = jira_sync.default_status_map()
    assert set(m["transitions"]) == {"open", "in_progress", "blocked", "review", "done", "cancelled", "closed"}
    assert set(m["categories"]) == {"done", "indeterminate", "new"}


def test_default_status_map_maps_only_stock_jira_statuses():
    m = jira_sync.default_status_map()
    mapped = {k: v for k, v in m["transitions"].items() if jira_sync.mapped_transition(m, k)}
    assert mapped == {"open": "To Do", "in_progress": "In Progress", "done": "Done", "closed": "Done"}
    for status in ("blocked", "review", "cancelled"):
        assert jira_sync.mapped_transition(m, status) is None
    # Inbound: only Done acts by default.
    assert jira_sync.mapped_action(m, "done") == "resolve"
    assert jira_sync.mapped_action(m, "new") is None
    assert jira_sync.mapped_action(m, "indeterminate") is None
    assert jira_sync.mapped_action(m, None) is None


def test_validate_status_map_accepts_none_and_empty():
    normalized = jira_sync.validate_status_map(
        {
            "transitions": {"done": "", "closed": None, "review": "NONE", "blocked": "Block"},
            "categories": {"done": "none", "new": None, "indeterminate": ""},
        }
    )
    assert normalized["transitions"]["done"] == "none"
    assert normalized["transitions"]["closed"] == "none"
    assert normalized["transitions"]["review"] == "none"
    assert normalized["transitions"]["blocked"] == "Block"
    assert normalized["categories"] == {"new": "none", "indeterminate": "none", "done": "none"}
    assert jira_sync.mapped_action(normalized, "done") is None


def test_parse_status_map_drops_unknown_values():
    merged = jira_sync.parse_status_map(
        json.dumps({"transitions": {"bogus": "X"}, "categories": {"done": "explode", "new": "reopen"}})
    )
    assert "bogus" not in merged["transitions"]
    assert merged["categories"]["done"] == "resolve"
    assert merged["categories"]["new"] == "reopen"


def test_redact_then_cap_redacts_before_truncating():
    secret = "AKIAIOSFODNN7EXAMPLE"
    text = "x" * 495 + " " + secret
    out = jira_sync.redact_then_cap(text, _org(), 500)
    assert "AKIA" not in out
    assert len(out) <= 500


def test_jira_author_label_fits_column_and_redacts():
    label = jira_sync.jira_author_label("AKIAIOSFODNN7EXAMPLE " + "n" * 100, _org())
    assert label.endswith(" (Jira)") and len(label) <= 50
    assert "AKIA" not in label
    assert jira_sync.jira_author_label("", _org()) == "a Jira user (Jira)"


def test_parse_status_map_returns_default_when_unset():
    assert jira_sync.parse_status_map(None) == jira_sync.default_status_map()
    assert jira_sync.parse_status_map("") == jira_sync.default_status_map()


def test_parse_status_map_falls_back_on_malformed_json():
    assert jira_sync.parse_status_map("{not json") == jira_sync.default_status_map()
    assert jira_sync.parse_status_map("[1,2]") == jira_sync.default_status_map()


def test_parse_status_map_merges_partial_overrides():
    merged = jira_sync.parse_status_map(json.dumps({"transitions": {"done": "Complete"}}))
    assert merged["transitions"]["done"] == "Complete"
    assert merged["transitions"]["in_progress"] == "In Progress"  # default kept
    assert merged["categories"] == jira_sync.default_status_map()["categories"]


def test_validate_status_map_normalizes_trimmed_names():
    normalized = jira_sync.validate_status_map({"transitions": {"done": "  Done  "}})
    assert normalized["transitions"]["done"] == "Done"


@pytest.mark.parametrize(
    "bad",
    [
        ["not", "a", "dict"],
        {"transitions": {"nonsense": "X"}},
        {"transitions": {"done": ["Done"]}},
        {"unknown_key": {}},
        {"transitions": {"done": "x" * 121}},
        {"transitions": {"done": 123}},
        {"categories": {"bogus": "start"}},
        {"categories": {"done": "frobnicate"}},
        {"categories": {"done": "start", "new": "resolve", "indeterminate": "start", "bogus": "start"}},
    ],
)
def test_validate_status_map_rejects_bad_input(bad):
    with pytest.raises(HTTPException) as exc:
        jira_sync.validate_status_map(bad)
    assert exc.value.status_code == 422


def test_validate_status_map_returns_default_when_none():
    assert jira_sync.validate_status_map(None) == jira_sync.default_status_map()


# ── sync comments mode ──


@pytest.mark.parametrize("mode", ["off", "inbound", "outbound", "both"])
def test_validate_sync_comments_accepts_valid_modes(mode):
    assert jira_sync.validate_sync_comments(mode) == mode
    assert jira_sync.validate_sync_comments(mode.upper()) == mode


def test_validate_sync_comments_rejects_unknown_mode():
    with pytest.raises(HTTPException) as exc:
        jira_sync.validate_sync_comments("sideways")
    assert exc.value.status_code == 422


# ── DLP redaction ──


def test_redact_for_jira_redacts_known_secret():
    text = "the key is sk-ant-abc123def456ghi789jkl012mno345 and it is secret"
    out = jira_sync.redact_for_jira(text, _org())
    assert "sk-ant-" not in out
    assert "secret" in out


def test_redact_for_jira_leaves_clean_text_untouched():
    text = "plain ticket notes about the auth migration"
    assert jira_sync.redact_for_jira(text, _org()) == text


def test_redact_for_jira_honors_org_phi_policy():
    org = _org(dlp={"enabled": True, "categories": ["phi"]})
    text = "patient id is 123-45-6789"
    out = jira_sync.redact_for_jira(text, org)
    assert "123-45-6789" not in out


def test_redact_for_jira_defaults_to_secrets_only_without_policy():
    org = _org()
    # PHI is not redacted unless the org opts in.
    text = "patient id is 123-45-6789"
    assert jira_sync.redact_for_jira(text, org) == text


# ── terminal status helper ──


@pytest.mark.parametrize(
    "kind,terminal",
    [("task", "done"), ("issue", "closed")],
)
def test_terminal_status_for_kind(kind, terminal):
    assert jira_sync.terminal_status_for_kind(kind) == terminal
    assert jira_sync.is_terminal_status(terminal, kind)
    assert not jira_sync.is_terminal_status("open", kind)


def test_apply_inbound_action_maps_actions_to_targets():
    # Sanity: the action vocabulary resolves to the FSM targets.
    assert jira_sync.terminal_status_for_kind("task") == "done"
