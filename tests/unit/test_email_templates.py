"""Unit tests for HTML email templates.

The template functions must HTML-escape every user-supplied value exactly
once. These tests guard the GH #96 review round 1 (L3) double-escape fix: the
proposer's display name used to be escaped once before being interpolated into
the quote-block label, then escaped a second time by ``_quote_block`` itself,
turning ``O'Brien & Co`` into ``O&amp;#x27;Brien &amp;amp; Co``.
"""

from __future__ import annotations

from sessionfs.server.email_templates import transfer_proposed_email


def test_transfer_proposed_email_escapes_proposer_name_once() -> None:
    html = transfer_proposed_email(
        project_name="acme",
        org_name="Test Org",
        proposer_name="O'Brien & Co",
        reason="consolidate",
        expires_at_human="2026-06-01",
    )

    # The apostrophe and ampersand are escaped exactly once, in both the
    # body and the quote-block label.
    assert "O&#x27;Brien &amp; Co" in html
    assert "Reason from O&#x27;Brien &amp; Co:" in html

    # No double-escaping: the escaped sequences must not themselves be escaped.
    assert "&amp;#x27;" not in html
    assert "&amp;amp;" not in html
