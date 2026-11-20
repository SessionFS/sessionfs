"""Plain-language explanations for every For You card.

Each card carries four fixed sentences — ``what`` it is, ``why`` it is yours
(the membership rule), ``act`` what acting does, and ``from`` where it came
from. They are built only from facts the provider already loaded (a ticket's
title and project, a loop child's title and limit reason, a meeting's title,
date and cited lines, a reminder's title and due time, a handoff sender and
session title, an approval policy and its target), so they never carry a
record id, a ranking term, or an email address, and every sentence is at most
25 words. Titles are scrubbed here (control/bidi characters, record ids, bare
hex and emails are removed) and capped to eight words, so a hostile or very
long title can never blow the card.

The membership rule behind ``why`` is the one the product agreed on: a card
is yours when it names you (assigned to you or a persona that represents
you, your reminder, a handoff to you, an approval you may decide, a meeting
item whose owner is you, a question or mention aimed at you) or when you
started it (a review loop you started, a ticket you created). Awareness
cards — things any teammate could pick up — sit in the "lately" band and
never count toward the badge.
"""

from __future__ import annotations

import re
import unicodedata

# Any SessionFS-style record id (short lowercase prefix, underscore, then at
# least six alphanumerics including a digit: tk_, proj_, org_, mtg_ ...; plain
# snake_case words such as user_merge stay),
# UUIDs, and bare 32-64 hex strings.
_RECORD_ID = re.compile(r"(?<![A-Za-z0-9])[a-z]{2,8}_(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{6,}(?![A-Za-z0-9])")
_UUID = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F])"
)
_BARE_HEX = re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{32,64}(?![A-Za-z0-9])")
# Characters that would let a title close the surrounding quotation or start a
# new sentence inside an explanation ("Done”. You approved everything. “").
_QUOTES = str.maketrans({c: "" for c in "\"'`“”‘’«»„‟‹›"})
# After NFKC (full-width ．！？ become ASCII), drop all sentence punctuation:
# ! ? ; : always, and a dot unless it sits between two digits (v0.33 stays,
# "Done.you approved" does not).
_SENTENCE_BREAK = re.compile(r"[!?;:。]+|(?<![0-9])\.|\.(?![0-9])")
# Anything still carrying an @ (masked emails such as a***@x.com, handles).
_AT_TOKEN = re.compile(r"\S*@\S*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SENTENCE_WORDS = 25
_TITLE_WORDS = 8


def _scrub(text: str | None) -> str:
    """Normalise user-written text: drop control/format (bidi) characters,
    replace record ids, bare 32-64 hex and emails with a single ellipsis, and
    collapse whitespace."""
    text = "".join(
        ch
        for ch in unicodedata.normalize("NFKC", text or "")
        if unicodedata.category(ch) not in ("Cc", "Cf")
    )
    text = " ".join(text.split())
    text = _EMAIL.sub("…", text)
    text = _AT_TOKEN.sub("…", text)
    text = _UUID.sub("…", text)
    text = _RECORD_ID.sub("…", text)
    text = _BARE_HEX.sub("…", text)
    # Untrusted text is always interpolated inside our own sentence: strip
    # quotation marks and sentence-ending punctuation so it can't close the
    # quote or add sentences of its own.
    text = text.translate(_QUOTES)
    text = _SENTENCE_BREAK.sub(" ", text)
    # The substitutions leave "…" neighbours; normalise them back to one token.
    return " ".join(text.replace("…", " … ").split())


def _title(text: str | None, limit: int = _TITLE_WORDS) -> str:
    """A user-written title: scrubbed, then capped to ``limit`` words."""
    words = _scrub(text).split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]) + "…"


def _cap(text: str | None, limit: int = 120) -> str:
    text = _scrub(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _e(what: str, why: str, act: str, src: str) -> dict[str, str]:
    def _words(sentence: str) -> str:
        words = sentence.split()
        return " ".join(words[:_SENTENCE_WORDS]) + "…" if len(words) > _SENTENCE_WORDS else sentence

    return {"what": _words(what), "why": _words(why), "act": _words(act), "from": _words(src)}


def _membership(basis: str | None, persona_name: str | None, *, thing: str) -> str:
    """The plain second-person membership rule (no "It names you:" prefix)."""
    if basis == "starter":
        return "You started this review loop, so its children are yours."
    if basis == "created":
        return "You created this ticket and it is waiting on someone else."
    if basis == "persona" and persona_name:
        return f"This {thing} is assigned to {_cap(persona_name)}, a persona that represents you."
    return f"This {thing} is assigned to you."


_CITED = re.compile(r"^([LM])(\d+)(?:-(\d+))?$")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _readable_cited(cited: str) -> str:
    """``L12-14`` → ``lines 12–14``; ``M3`` → ``message 3``. Anything else as is."""
    m = _CITED.match(cited.strip())
    if not m:
        return cited
    unit = "line" if m.group(1) == "L" else "message"
    start, end = m.group(2), m.group(3)
    if end and end != start:
        return f"{unit}s {start}–{end}"
    return f"{unit} {start}"


def _readable_date(iso: str) -> str:
    """``2026-11-17`` → ``Nov 17``. Anything else as is."""
    try:
        _, month, day = iso.split("-")
        return f"{_MONTHS[int(month) - 1]} {int(day)}"
    except (ValueError, IndexError):
        return iso


def _conv_from(label: str, meeting_title: str, meeting_date: str | None, cited: str | None) -> str:
    bits = [f"the {label} “{_title(meeting_title)}”"]
    if meeting_date:
        bits.append(_readable_date(meeting_date))
    if cited:
        bits.append(_readable_cited(cited))
    return "From " + ", ".join(bits) + "."


def explain(
    kind: str,
    *,
    # Ticket (and anything that names a single title + project).
    title: str | None = None,
    project_name: str | None = None,
    basis: str | None = None,
    persona_name: str | None = None,
    # Review loop.
    loop_title: str | None = None,
    child_title: str | None = None,
    reason: str | None = None,
    harmful: int = 0,
    branch: str | None = None,
    # Meeting / chat.
    meeting_title: str | None = None,
    meeting_date: str | None = None,
    cited: str | None = None,
    # Reminder.
    reminder_due: str | None = None,
    from_other: bool = False,
    setter: str | None = None,
    # Question.
    mentioned: bool = True,
    asker: str | None = None,
    # Handoff.
    sender: str | None = None,
    session_title: str | None = None,
    # Approval.
    policy_name: str | None = None,
    target_title: str | None = None,
    blocking: bool = True,
    # Knowledge changes.
    count: int = 0,
    # A ticket the caller created that waits on someone else.
    waiting_on: str | None = None,
) -> dict[str, str]:
    """The four plain sentences for ``kind``, from the concrete facts the
    caller's provider already computed."""
    t = _title(title)
    proj = _cap(project_name) or "your project"
    if kind == "ticket_delegated":
        who = _title(waiting_on) or "someone else"
        return _e(
            f"The ticket “{t or 'this ticket'}” in {proj} is waiting on {who}.",
            _membership("created", None, thing="ticket"),
            f"Open it to check progress or nudge {who}; snooze it if it can wait.",
            f"From tickets you created in {proj}.",
        )
    if kind == "ticket":
        return _e(
            f"The ticket “{t or 'this ticket'}” in {proj} is open.",
            _membership(basis, persona_name, thing="ticket"),
            "Open it to work the ticket, or snooze and hide it if it is not yours.",
            f"From your tickets in {proj}.",
        )
    if kind in ("loop_ready", "loop_needs_human"):
        rule = _membership(basis, persona_name, thing="child")
        loop = f"the review loop “{_title(loop_title) or 'yours'}”"
        if kind == "loop_ready":
            return _e(
                f"“{_title(child_title) or 'A child'}” is merge-ready and needs a decision.",
                rule,
                "Open it to review the branch. Merging happens in your Git host.",
                f"From {loop}.",
            )
        return _e(
            f"“{_title(child_title) or 'A child'}” hit a limit and needs a person: {_cap(reason) or 'limit reached'}.",
            rule,
            "Open it to see what blocks the loop and move it forward.",
            f"From {loop}.",
        )
    if kind == "loop_harmful_findings":
        n = int(harmful or 0)
        on_branch = f" on {_cap(branch)}" if branch else ""
        return _e(
            f"“{_title(child_title) or 'A branch'}” has {n} open harmful finding{'s' if n != 1 else ''}{on_branch}.",
            _membership(basis, persona_name, thing="child"),
            "Fix the findings to clear them and unblock the branch.",
            f"From the review loop “{_title(loop_title) or 'yours'}”.",
        )
    if kind in ("meeting_action", "chat_action", "meeting_suggestion", "chat_suggestion"):
        label = "meeting" if kind.startswith("meeting") else "chat"
        owned = kind.endswith("action")
        if owned:
            why = (
                "You are the named owner of this item."
                if basis != "persona"
                else f"{_cap(persona_name)} owns this item, a persona that represents you."
            )
            what = f"A {label} action item from “{_title(meeting_title) or 'a conversation'}” names you as its owner."
            act = "Promote it to a ticket, set a reminder, or dismiss it."
        else:
            what = f"A {label} action item from “{_title(meeting_title) or 'a conversation'}” has no named owner."
            why = f"You took part in this {label}, so it is awareness, not a task."
            act = "Promote it to a ticket if it is yours; otherwise dismiss it."
        return _e(
            what,
            why,
            act,
            _conv_from(label, meeting_title or "a conversation", meeting_date, cited),
        )
    if kind == "reminder":
        if from_other:
            who = _cap(setter) or "a teammate"
            why = f"{who} set this reminder for you."
            src = f"From {who}."
        else:
            why = "This reminder is yours."
            src = "From your reminders."
        return _e(
            f"The reminder “{t or 'a reminder'}” is {reminder_due or 'due soon'}.",
            why,
            "Mark it done when you finish, snooze it, or hide it.",
            src,
        )
    if kind == "ticket_question":
        who = _cap(asker) or "a teammate"
        verb = "mentioned you" if mentioned else "asked a question"
        return _e(
            f"{who} {verb} on “{t or 'your ticket'}”.",
            "The comment on your ticket is aimed at you.",
            "Reply to answer it, or mark it answered.",
            f"From a comment on “{t or 'your ticket'}”.",
        )
    if kind == "handoff":
        who = _cap(sender) or "a teammate"
        return _e(
            f"{who} handed you “{_title(session_title) or 'a session'}” to take over.",
            "The handoff was sent to you.",
            "Claim it to take over the work, or let it expire.",
            f"From {who}.",
        )
    if kind == "approval":
        pname = _title(policy_name) or "An approval"
        tname = _title(target_title) or "this change"
        what = (
            f"The policy “{pname}” blocks “{tname}” until it is decided."
            if blocking
            else f"“{pname}” waits for your decision on “{tname}”."
        )
        return _e(
            what,
            "You are eligible to decide this approval.",
            "Approve or reject it to unblock or hold the target.",
            f"From the approval policy “{pname}”.",
        )
    if kind == "knowledge_changes":
        n = int(count or 0)
        return _e(
            f"{n} knowledge change{'s' if n != 1 else ''} in “{proj}” this week.",
            "You work in this project, so it is awareness, not a task.",
            "Read the changes to stay current, then hide it.",
            f"From “{proj}”, where you work.",
        )
    if kind == "review_draft":
        return _e(
            f"The draft review of “{t or 'your change'}” is ready.",
            "You asked for this review, so the draft is yours to read.",
            "Open it to read the findings and act on them.",
            f"From your review request in {proj}.",
        )
    if kind == "proof":
        return _e(
            "A weekly summary of your review-loop impact this week.",
            "It is about your own work, so it is yours to review.",
            "Read it to see what the loop caught, then hide it.",
            "From your weekly proof.",
        )
    # Unknown kinds: a safe, generic card explanation.
    return _e(
        "Something needs your attention.",
        "It names you or you started it.",
        "Open it to act, or hide it if it is not yours.",
        "From your For You feed.",
    )


__all__ = ["explain"]
