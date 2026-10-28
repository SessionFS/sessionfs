"""Unit coverage for the helper's ticket-timeline rendering (GitHub #113).

The timeline line builder in ``helper/tools.py`` renders each event's actor;
these tests pin the email-masking rule that a captured-session reader (no
read standing) must never learn a teammate's address from the timeline, even
when that teammate has no display name.
"""

from __future__ import annotations

from sessionfs.server.db.models import TicketEvent, User
from sessionfs.server.email import mask_address
from sessionfs.server.helper.tools import _event_actor_name


def _user(email: str, display_name: str | None = None) -> User:
    return User(id="u_actor", email=email, display_name=display_name)


def test_event_actor_name_masks_email_without_read_standing() -> None:
    actor = _user("alice@example.com")  # no display name
    event = TicketEvent(actor_type="user", actor_user_id="u_actor")

    # Read standing: full address.
    assert _event_actor_name(event, {"u_actor": actor}, show_email=True) == "alice@example.com"

    # Captured-session reader: masked, exactly like the creator/resolver labels.
    masked = _event_actor_name(event, {"u_actor": actor}, show_email=False)
    assert masked == mask_address("alice@example.com")
    assert masked != "alice@example.com"
    assert masked.startswith("a***@")


def test_event_actor_name_prefers_display_name() -> None:
    actor = _user("alice@example.com", display_name="Alice")
    event = TicketEvent(actor_type="user", actor_user_id="u_actor")
    # A display name is user-controlled but not private, so it wins in both modes.
    assert _event_actor_name(event, {"u_actor": actor}, show_email=True) == "Alice"
    assert _event_actor_name(event, {"u_actor": actor}, show_email=False) == "Alice"


def test_event_actor_name_non_user_types() -> None:
    assert _event_actor_name(TicketEvent(actor_type="jira"), {}) == "Jira"
    assert _event_actor_name(TicketEvent(actor_type="system"), {}) == "system"
    assert (
        _event_actor_name(TicketEvent(actor_type="service", actor_label="timeline-bot"), {})
        == "timeline-bot"
    )
