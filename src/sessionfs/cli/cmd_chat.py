"""`sfs chat …` — chat threads (Slack, Discord, pasted) as conversation sources.

Commands:
- sfs chat upload <file> [--project proj_… | --group pg_…] --channel "#infra"
                         [--provider slack|discord|paste|teams_paste]
                         [--users users.json] [--attest] [--title T] [--extract]
- sfs chat list | search | show | delete | extract | participants |
  resolve-participant | action-items | promote | dismiss | restore | link | unlink

Everything after upload is the matching `sfs meeting` command run against the
same routes with ``?kind=chat``. Items cite message ranges (M<a>-<b>); message
N is line N of the stored thread. Every access rule (project edit / group
editor, DLP, the DM refusal, retention) is enforced server-side.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from sessionfs.cli import cmd_meeting as _m
from sessionfs.cli.common import console, err_console, handle_errors

chat_app = typer.Typer(
    name="chat",
    help="Store chat threads (Slack, Discord, pasted), extract action items and promote them to tickets.",
    no_args_is_help=True,
)

_KIND = "chat"
PROVIDERS = ("slack", "discord", "paste", "teams_paste")
# The server parses at most 2 MiB of raw export; refuse a bigger file here.
MAX_CHAT_FILE_BYTES = 2 * 1024 * 1024


def detect_provider(content: str) -> str:
    """Guess the export format: Slack JSON, DiscordChatExporter JSON, else paste."""
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        return "paste"

    def _slack_message(m: Any) -> bool:
        return isinstance(m, dict) and isinstance(m.get("ts"), str)

    if isinstance(data, list):
        if any(_slack_message(m) for m in data):
            return "slack"
        return "paste"
    if not isinstance(data, dict):
        return "paste"
    messages = data.get("messages")
    if not isinstance(messages, list):
        return "paste"
    # DiscordChatExporter: top-level guild/channel objects and messages whose
    # author is an object.
    if isinstance(data.get("guild"), dict) or any(
        isinstance(m, dict) and isinstance(m.get("author"), dict) for m in messages
    ):
        return "discord"
    if any(_slack_message(m) for m in messages):
        return "slack"
    return "paste"


def slack_lacks_channel_object(content: str) -> bool:
    """A Slack export with no ``channel``/``channel_info`` object (a bare
    message array) can't be told apart from a DM: the server needs
    ``attestation`` for it, as for a paste."""
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        return False
    if isinstance(data, list):
        return True
    if isinstance(data, dict):
        return not any(isinstance(data.get(k), dict) for k in ("channel", "channel_info"))
    return False


# Guild channel kinds a Discord export may carry without an attestation
# (same allowlist as the server: DiscordChatExporter names + numeric types).
_DISCORD_GUILD_TYPES = frozenset({
    "guildtextchat", "guildvoicechat", "guildcategory", "guildnews",
    "guildnewsthread", "guildpublicthread", "guildprivatethread",
    "guildstagevoice", "guilddirectory", "guildforum", "guildmedia",
    "0", "2", "4", "5", "10", "11", "12", "13", "14", "15", "16",
})


def discord_lacks_guild_channel(content: str) -> bool:
    """True when a Discord export doesn't prove it is a guild channel (guild +
    channel objects with a known guild channel type): the server then needs
    ``attestation``. Unparseable or non-export JSON is left to the server."""
    try:
        data = json.loads(content)
    except (ValueError, RecursionError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return False
    guild, channel = data.get("guild"), data.get("channel")
    if not isinstance(guild, dict) or not isinstance(channel, dict):
        return True
    ctype = channel.get("type")
    if isinstance(ctype, bool) or not isinstance(ctype, (str, int)):
        return True
    return str(ctype).strip().casefold() not in _DISCORD_GUILD_TYPES


def _read_capped(path: Path, what: str) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        err_console.print(f"[red]Cannot read {path}: {exc.strerror or exc}[/red]")
        raise typer.Exit(1) from exc
    if size > MAX_CHAT_FILE_BYTES:
        err_console.print(
            f"[red]{escape(str(path))} is {size} bytes; a {what} is limited to 2 MiB.[/red] "
            "Export a shorter range (one channel or one thread)."
        )
        raise typer.Exit(1)
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        err_console.print(f"[red]Cannot read {path}: {exc.strerror or exc}[/red]")
        raise typer.Exit(1) from exc


@chat_app.command("upload")
@handle_errors
def upload_chat(
    file: Path = typer.Argument(..., help="Slack export JSON, DiscordChatExporter JSON, or pasted text."),
    channel: str = typer.Option(..., "--channel", "-c", help='Channel label, e.g. "#infra" (max 80 chars).'),
    provider: str | None = typer.Option(
        None, "--provider",
        help="slack | discord | paste | teams_paste. Default: detected from the file.",
    ),
    users: Path | None = typer.Option(None, "--users", help="Slack only: the export's users.json."),
    attest: bool = typer.Option(
        False, "--attest",
        help=(
            "Confirm the thread is from a channel, not a direct message (required for "
            "a paste, a Slack export with no channel object, and a Discord export "
            "without guild channel details)."
        ),
    ),
    title: str | None = typer.Option(None, "--title", "-t", help="Optional title (max 200 chars)."),
    project: str | None = typer.Option(None, "--project", "-p", help="Project id (proj_…). Default: this repo's project."),
    group: str | None = typer.Option(None, "--group", "-g", help="Project group id (pg_…)."),
    extract: bool = typer.Option(False, "--extract", help="Also extract action items now (uses your own LLM key)."),
) -> None:
    """Upload a chat thread. DLP-redacted server-side before storage; DMs are refused."""
    if provider is not None and provider not in PROVIDERS:
        err_console.print(f"[red]--provider must be one of: {', '.join(PROVIDERS)}.[/red]")
        raise typer.Exit(1)
    channel = channel.strip()
    if not channel or len(channel) > 80:
        err_console.print("[red]--channel must be 1-80 characters.[/red]")
        raise typer.Exit(1)
    content = _read_capped(file, "chat thread upload")
    if not content.strip():
        err_console.print("[red]The file is empty.[/red]")
        raise typer.Exit(1)
    chosen = provider or detect_provider(content)
    if chosen in ("paste", "teams_paste") and not attest:
        err_console.print(
            f"[red]{'Detected a plain paste' if provider is None else 'A paste'}: pass --attest "
            "to confirm it is from a channel, not a direct message.[/red]"
            + (" (Use --provider slack|discord if it is an export.)" if provider is None else "")
        )
        raise typer.Exit(1)
    if chosen == "slack" and not attest and slack_lacks_channel_object(content):
        err_console.print(
            "[red]This Slack export has no channel object (a bare message array): pass "
            "--attest to confirm it is from a channel, not a direct message.[/red]"
        )
        raise typer.Exit(1)
    if chosen == "discord" and not attest and discord_lacks_guild_channel(content):
        err_console.print(
            "[red]This Discord export has no guild channel details (guild and channel "
            "objects with a known channel type): pass --attest to confirm it is from a "
            "channel, not a direct message.[/red]"
        )
        raise typer.Exit(1)
    users_text: str | None = None
    if users is not None:
        if chosen != "slack":
            err_console.print("[red]--users only applies to a Slack export.[/red]")
            raise typer.Exit(1)
        users_text = _read_capped(users, "users.json")

    api, base = _m._scope_base(project, group)
    payload: dict[str, Any] = {
        "provider": chosen,
        "channel_name": channel,
        "content": content,
        "attestation": attest,
    }
    if users_text:
        payload["users"] = users_text
    if title:
        payload["title"] = title
    if extract:
        payload["extract"] = True
    body = _m._call(
        "POST", f"{base}/chat-threads", json_data=payload, api=api,
        timeout=_m._LONG_TIMEOUT if extract else 30,
    )
    console.print(
        f"[green]Stored[/green] [cyan]{body['id']}[/cyan] ({chosen}) — "
        f"{body.get('line_count', '?')} messages"
        + (f", {body['dlp_findings']} redaction(s)" if body.get("dlp_findings") else "")
    )
    for warning in body.get("warnings") or []:
        console.print(f"[yellow]warning:[/yellow] {escape(str(warning))}")
    if extract:
        _m._print_extraction_summary(body)


@chat_app.command("list")
@handle_errors
def list_chats(
    project: str | None = typer.Option(None, "--project", "-p"),
    group: str | None = typer.Option(None, "--group", "-g"),
    query: str | None = typer.Option(None, "--query", "-q", help="Match title and message text."),
    limit: int = typer.Option(50, "--limit", "-n", min=1, max=200),
    before: str | None = typer.Option(None, "--before", help="Cursor: the id printed after the previous page."),
) -> None:
    """List chat threads, newest first."""
    _m.list_impl(project, group, query, limit, before, kind=_KIND)


@chat_app.command("search")
@handle_errors
def search_chats(
    q: str = typer.Argument(..., help="Text to find in titles and messages."),
    project: str | None = typer.Option(None, "--project", "-p"),
    group: str | None = typer.Option(None, "--group", "-g"),
    limit: int = typer.Option(25, "--limit", "-n", min=1, max=200),
) -> None:
    """Search chat threads by title and message text."""
    _m.search_impl(q, project, group, limit, kind=_KIND)


@chat_app.command("show")
@handle_errors
def show_chat(
    thread_id: str = typer.Argument(..., help="Chat thread id (chat_…)"),
    no_messages: bool = typer.Option(False, "--no-messages", help="Hide the messages."),
) -> None:
    """Print a chat thread: numbered messages, participants, action items."""
    _m.show_impl(thread_id, no_messages, kind=_KIND)


@chat_app.command("delete")
@handle_errors
def delete_chat(
    thread_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Delete a chat thread (soft-delete; the retention sweep removes it later)."""
    _m.delete_impl(thread_id, yes, kind=_KIND)


@chat_app.command("extract")
@handle_errors
def extract_chat(thread_id: str = typer.Argument(...)) -> None:
    """Extract participants, action items and decisions with your own LLM key."""
    _m.extract_impl(thread_id, kind=_KIND)


@chat_app.command("participants")
@handle_errors
def chat_participants(thread_id: str = typer.Argument(...)) -> None:
    """List a chat thread's participants and how each was resolved."""
    _m.participants_impl(thread_id, kind=_KIND)


@chat_app.command("resolve-participant")
@handle_errors
def chat_resolve_participant(
    thread_id: str = typer.Argument(...),
    participant_id: str | None = typer.Argument(None),
    user: str | None = typer.Option(None, "--user", help="User id to resolve to."),
    persona: str | None = typer.Option(None, "--persona", help="Persona name to resolve to."),
    contact: str | None = typer.Option(None, "--contact", help="External contact id (ec_...)."),
    file: str | None = typer.Option(None, "--file", "-f", help=_m.RESOLVE_FILE_HELP),
) -> None:
    """Resolve a participant to an org member, a persona or an external contact (exactly one)."""
    _m.resolve_participant_impl(
        thread_id, participant_id, user, persona, kind=_KIND, contact=contact, file=file
    )


@chat_app.command("action-items")
@handle_errors
def chat_action_items(thread_id: str = typer.Argument(...)) -> None:
    """List a chat thread's action items and decisions."""
    _m.action_items_impl(thread_id, kind=_KIND)


@chat_app.command("promote")
@handle_errors
def promote_chat_item(
    thread_id: str = typer.Argument(...),
    item_id: str = typer.Argument(...),
    title: str | None = typer.Option(None, "--title", help="Override the drafted title."),
    description: str | None = typer.Option(None, "--description", help="Override the drafted description."),
    assignee: str | None = typer.Option(None, "--assignee", "-a", help="Persona name to assign."),
    user: str | None = typer.Option(None, "--user", help="User id to assign."),
    due: str | None = typer.Option(None, "--due", help="Due date (YYYY-MM-DD)."),
    priority: str | None = typer.Option(None, "--priority", help="critical|high|medium|low"),
    target_project: str | None = typer.Option(None, "--target-project", help="Project id for the ticket (required for a group thread)."),
    jira: bool = typer.Option(False, "--jira", help="Also publish the ticket to Jira."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Create the ticket without asking."),
) -> None:
    """Turn an action item into a ticket. Shows the draft first and asks to confirm."""
    _m.promote_impl(
        thread_id, item_id, title=title, description=description, assignee=assignee,
        user=user, due=due, priority=priority, target_project=target_project, jira=jira,
        yes=yes, kind=_KIND,
    )


@chat_app.command("dismiss")
@handle_errors
def dismiss_chat_item(thread_id: str = typer.Argument(...), item_id: str = typer.Argument(...)) -> None:
    """Dismiss a pending action item."""
    _m.item_action_impl(thread_id, item_id, "dismiss", kind=_KIND)


@chat_app.command("restore")
@handle_errors
def restore_chat_item(thread_id: str = typer.Argument(...), item_id: str = typer.Argument(...)) -> None:
    """Bring a dismissed action item back to pending."""
    _m.item_action_impl(thread_id, item_id, "restore", kind=_KIND)


@chat_app.command("link")
@handle_errors
def link_chat_item(
    thread_id: str = typer.Argument(...),
    item_id: str = typer.Argument(...),
    ticket_id: str | None = typer.Argument(
        None, help="Existing ticket id (tk_...). Omit to use the item's suggestion."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Link without asking."),
) -> None:
    """Tie a pending action item to an EXISTING ticket. Shows the ticket and
    the comment it will add, then asks to confirm."""
    _m.link_impl(thread_id, item_id, ticket_id, yes=yes, kind=_KIND)


@chat_app.command("unlink")
@handle_errors
def unlink_chat_item(thread_id: str = typer.Argument(...), item_id: str = typer.Argument(...)) -> None:
    """Put a linked action item back to pending (the ticket comment stays)."""
    _m.unlink_impl(thread_id, item_id, kind=_KIND)
