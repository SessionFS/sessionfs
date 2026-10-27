"""`sfs helper …` — ask the session helper over sessions you can read.

*Enterprise feature `session_helper`. Requires a BYOK model key.*

Commands:
- sfs helper ask "..." [--project ID] [--session ID ...] [--conversation ID] [--no-stream] [--json]
- sfs helper conversations list
- sfs helper conversations show <id>
- sfs helper conversations delete <id> [--yes]
- sfs helper status

`ask` streams the answer to the terminal (Rich live markdown on a TTY,
plain text otherwise) and lists citations at the end. `--json` returns the
raw JSON document (non-streaming) for scripting; `--no-stream` fetches the
JSON document and renders it like the streaming output.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import typer
from rich.markdown import Markdown
from rich.markup import escape as markup_escape
from rich.table import Table

from sessionfs.cli.cmd_rules import _api_request, _get_api_config
from sessionfs.cli.common import (
    confirm_or_exit,
    console,
    err_console,
    format_api_error,
    handle_errors,
)

helper_app = typer.Typer(
    name="helper",
    help="Ask the session helper over sessions you can read (Enterprise).",
    no_args_is_help=True,
)
conversations_app = typer.Typer(
    name="conversations",
    help="List, show and delete helper conversations.",
    no_args_is_help=True,
)
helper_app.add_typer(conversations_app, name="conversations")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _fail(status: int, body: Any) -> None:
    err_console.print(
        f"[red]API error ({status}): {markup_escape(format_api_error(body, status))}[/red]"
    )
    raise typer.Exit(1)


def _parse_body(text: str, content_type: str) -> Any:
    """Best-effort JSON decode for an error body; fall back to the raw text."""
    if "application/json" not in (content_type or "").lower() and not text.lstrip().startswith("{"):
        return text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def _render_answer(answer: str, *, markdown: bool) -> None:
    if not answer:
        return
    if markdown:
        console.print(Markdown(answer))
    else:
        # Plain output must not go through Rich markup, or `[session:…]`-style
        # text inside the answer would be swallowed as an unknown style tag.
        console.print(answer, markup=False, highlight=False, soft_wrap=True)


def _citation_kind(c: dict) -> str:
    """The citation's kind; stored citations from before ``kind`` existed are
    a session (``session_id``) or a docs page (``slug`` + ``url``)."""
    kind = c.get("kind")
    if isinstance(kind, str) and kind:
        return kind
    if "ticket_id" in c:
        return "ticket"
    if "slug" in c and "url" not in c and "project_id" in c:
        return "wiki"
    if "slug" in c:
        return "doc"
    return "session"


def _citation_line(c: dict) -> tuple[str, str]:
    """``(title, location)`` for one citation of any kind."""
    kind = _citation_kind(c)
    if kind == "doc":
        # A docs citation: the page/section title and its public URL.
        return c.get("title") or c.get("slug") or "SessionFS docs", c.get("url") or ""
    if kind == "ticket":
        return c.get("title") or "Ticket", c.get("ticket_id") or ""
    if kind == "wiki":
        loc = f"wiki:{c.get('project_id', '')}/{c.get('slug', '')}"
        return c.get("title") or c.get("slug") or "Wiki page", loc
    sid = c.get("session_id", "")
    idx = c.get("message_index")
    return c.get("title") or "Untitled", sid + (f"#msg-{idx}" if idx is not None else "")


def _render_citations(citations: list[dict], *, markdown: bool) -> None:
    if not citations:
        return
    console.print()
    console.print("[bold]Citations:[/bold]")
    for c in citations:
        prefix = "  • " if markdown else "  - "
        title, loc = _citation_line(c)
        if markdown:
            console.print(f"{prefix}[cyan]{markup_escape(title)}[/cyan] — {markup_escape(loc)}")
        else:
            console.print(f"{prefix}{title} — {loc}", markup=False, highlight=False, soft_wrap=True)


def _render_usage(result: dict, *, conversation_id: str | None = None) -> None:
    usage = result.get("usage") or {}
    bits = []
    if conversation_id:
        bits.append(f"conversation {conversation_id}")
    stop_reason = result.get("stop_reason")
    if stop_reason and stop_reason != "stop":
        bits.append(f"stop: {stop_reason}")
    if "input_tokens" in usage or "output_tokens" in usage:
        bits.append(
            f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out tokens"
        )
    key_source = result.get("key_source")
    if key_source:
        bits.append(f"key: {key_source}")
    if bits:
        console.print()
        console.print(f"[dim]{markup_escape(' · '.join(bits))}[/dim]")


def _build_payload(
    question: str,
    project: str | None,
    sessions: list[str] | None,
    conversation: str | None,
) -> dict:
    payload: dict[str, Any] = {"message": question}
    if conversation:
        payload["conversation_id"] = conversation
    scope: dict[str, Any] = {}
    if project:
        scope["project_id"] = project
    if sessions:
        scope["session_ids"] = [s for s in sessions if s]
    if scope:
        payload["scope"] = scope
    return payload


async def _iter_sse_events(client: Any, url: str, headers: dict, payload: dict):
    """Yield (event_type, data_string) tuples from a streaming POST response.

    An HTTP error is surfaced as a single ("__http_error__", data) yield where
    data is JSON encoding {"status": <int>, "body": <text>}, so the caller can
    surface the error without a second request.
    """
    async with client.stream("POST", url, json=payload, headers=headers) as resp:
        if resp.status_code >= 400:
            text = (await resp.aread()).decode("utf-8", "replace")
            yield (
                "__http_error__",
                json.dumps({"status": resp.status_code, "body": text}),
            )
            return
        event_type: str | None = None
        data_lines: list[str] = []
        async for line in resp.aiter_lines():
            if line == "":
                if event_type is not None and data_lines:
                    yield (event_type, "\n".join(data_lines))
                event_type = None
                data_lines = []
                continue
            if line.startswith(":"):
                continue  # SSE comment (ping) — ignore
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if event_type is not None and data_lines:
            yield (event_type, "\n".join(data_lines))


# SSE events that carry one citation each.
_CITATION_EVENTS = ("citation", "doc_citation", "ticket_citation", "wiki_citation")


def _parse_event_data(event_type: str, data: str) -> dict:
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


async def _stream_ask(api_url: str, api_key: str, payload: dict) -> None:
    import httpx

    url = f"{api_url.rstrip('/')}/api/v1/helper/turns"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"}
    markdown = console.is_terminal

    answer_parts: list[str] = []
    citations: list[dict] = []
    conversation_id: str | None = None
    error_message: str | None = None
    done: dict = {}

    try:
        # The server sends a keep-alive ping every 15 s while a turn runs, so a
        # bounded read timeout never cuts a live stream but does end a stalled
        # connection instead of hanging forever.
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)
        )
    except Exception as exc:  # pragma: no cover - defensive
        err_console.print(f"[red]Unexpected error: {markup_escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc

    async with client:
        try:
            events = _iter_sse_events(client, url, headers, payload)
            if markdown:
                from rich.live import Live

                live = Live(Markdown(""), console=console, refresh_per_second=10)
                with live:
                    async for event_type, data in events:
                        if event_type == "__http_error__":
                            err = json.loads(data)
                            _fail(int(err["status"]), _parse_body(err["body"], ""))
                        if event_type == "conversation":
                            conversation_id = _parse_event_data(event_type, data).get(
                                "conversation_id"
                            )
                        elif event_type == "text":
                            delta = _parse_event_data(event_type, data).get("delta", "")
                            answer_parts.append(delta)
                            live.update(Markdown("".join(answer_parts)))
                        elif event_type in _CITATION_EVENTS:
                            citations.append(_parse_event_data(event_type, data))
                        elif event_type == "error":
                            error_message = _parse_event_data(event_type, data).get(
                                "message", "The helper failed."
                            )
                            break
                        elif event_type == "done":
                            done = _parse_event_data(event_type, data)
                            break
                _finish_stream(
                    "".join(answer_parts), citations, conversation_id, done, markdown, error_message
                )
            else:
                async for event_type, data in events:
                    if event_type == "__http_error__":
                        err = json.loads(data)
                        _fail(int(err["status"]), _parse_body(err["body"], ""))
                    elif event_type == "conversation":
                        conversation_id = _parse_event_data(event_type, data).get(
                            "conversation_id"
                        )
                    elif event_type == "text":
                        delta = _parse_event_data(event_type, data).get("delta", "")
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                        answer_parts.append(delta)
                    elif event_type in _CITATION_EVENTS:
                        citations.append(_parse_event_data(event_type, data))
                    elif event_type == "error":
                        error_message = _parse_event_data(event_type, data).get(
                            "message", "The helper failed."
                        )
                        break
                    elif event_type == "done":
                        done = _parse_event_data(event_type, data)
                        break
                if answer_parts:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                _finish_stream(
                    "".join(answer_parts), citations, conversation_id, done, False, error_message
                )
        except httpx.RequestError as exc:
            err_console.print(
                f"[red]Can't reach the SessionFS API at {markup_escape(api_url)}.[/red]\n"
                f"[dim]  ({type(exc).__name__}: {markup_escape(str(exc))})[/dim]"
            )
            raise typer.Exit(1) from exc


def _finish_stream(
    _answer: str,
    citations: list[dict],
    conversation_id: str | None,
    done: dict,
    markdown: bool,
    error_message: str | None,
) -> None:
    if error_message:
        err_console.print(f"[red]{markup_escape(error_message)}[/red]")
        # A streamed error is a failed request: exit non-zero. The partial text
        # has already been written as it streamed, so don't re-render it here.
        raise typer.Exit(1)
    _render_citations(citations, markdown=markdown)
    _render_usage(
        {
            "stop_reason": done.get("stop_reason"),
            "usage": {
                "input_tokens": done.get("input_tokens", 0),
                "output_tokens": done.get("output_tokens", 0),
            },
            "key_source": done.get("key_source"),
        },
        conversation_id=conversation_id,
    )


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


@helper_app.command("ask")
@handle_errors
def helper_ask(
    question: str = typer.Argument(..., help="Question to ask the helper"),
    project: str | None = typer.Option(
        None, "--project", "-P", help="Project ID (proj_...) to scope the question to."
    ),
    session: list[str] | None = typer.Option(
        None, "--session", "-s", help="Session ID (ses_...) to scope to. Repeatable."
    ),
    conversation: str | None = typer.Option(
        None, "--conversation", "-c", help="Conversation ID (hc_...) to continue."
    ),
    no_stream: bool = typer.Option(
        False, "--no-stream", help="Fetch the full JSON answer instead of streaming."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print the raw JSON document (implies --no-stream)."
    ),
) -> None:
    """Ask the session helper a question over the sessions you can read."""
    question = question.strip()
    if not question:
        err_console.print("[red]Please provide a non-empty question.[/red]")
        raise typer.Exit(1)

    api_url, api_key = _get_api_config()
    payload = _build_payload(question, project, session, conversation)
    stream = not (no_stream or json_output)

    if stream:
        asyncio.run(_stream_ask(api_url, api_key, payload))
        return

    payload["stream"] = False
    status, body, _ = asyncio.run(
        _api_request("POST", "/api/v1/helper/turns", api_url, api_key, payload)
    )
    if status >= 400:
        _fail(status, body)
    result = body if isinstance(body, dict) else {}
    if json_output:
        # Write the JSON document raw so Rich never reflows it or swallows
        # `[session:…]`-style tokens inside answer/citation text.
        sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
        return
    _render_answer(result.get("answer", ""), markdown=console.is_terminal)
    _render_citations(result.get("citations") or [], markdown=console.is_terminal)
    _render_usage(result, conversation_id=result.get("conversation_id"))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@helper_app.command("status")
@handle_errors
def helper_status() -> None:
    """Show whether the helper is enabled and which key/model it would use."""
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request("GET", "/api/v1/helper/status", api_url, api_key)
    )
    if status >= 400:
        _fail(status, body)
    if not isinstance(body, dict):
        err_console.print("[red]Unexpected API response.[/red]")
        raise typer.Exit(1)

    enabled = body.get("enabled")
    if enabled is False:
        console.print("[yellow]The session helper is not included in your plan.[/yellow]")
        console.print("[dim]It is an Enterprise feature.[/dim]")
        return
    key_source = body.get("key_source")
    if key_source is None:
        console.print("[yellow]The session helper is enabled, but no LLM key is set.[/yellow]")
        console.print("[dim]Add your key under Settings → Data protection → Security review model, or ask your org admin to add an org key.[/dim]")
        return
    console.print(f"[green]Session helper enabled[/green] (key: {markup_escape(key_source)})")
    if body.get("provider"):
        console.print(f"  Provider: {markup_escape(body['provider'])}")
    if body.get("model"):
        console.print(f"  Model:    {markup_escape(body['model'])}")
    effective = body.get("effective_settings")
    if isinstance(effective, dict):
        console.print("  Settings:")
        console.print(f"    max_output_tokens: {effective.get('max_output_tokens')}")
        console.print(f"    temperature:       {effective.get('temperature')}")
        console.print(f"    top_p:             {effective.get('top_p')}")
        console.print(f"    max_tool_steps:    {effective.get('max_tool_steps')}")
        console.print(f"    reasoning_effort:  {effective.get('reasoning_effort')}")
        console.print(f"    answer_style:      {effective.get('answer_style')}")
    if body.get("has_org_instructions"):
        console.print("  Org instructions: set")
    if body.get("has_personal_instructions"):
        console.print("  Personal instructions: set")


# ---------------------------------------------------------------------------
# conversations
# ---------------------------------------------------------------------------


@conversations_app.command("list")
@handle_errors
def conversations_list() -> None:
    """List your helper conversations (newest first)."""
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request("GET", "/api/v1/helper/conversations", api_url, api_key)
    )
    if status >= 400:
        _fail(status, body)
    if not isinstance(body, list):
        err_console.print("[red]Unexpected API response.[/red]")
        raise typer.Exit(1)
    if not body:
        console.print("[dim]No conversations yet.[/dim]")
        return
    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Updated", style="dim")
    for c in body:
        table.add_row(
            markup_escape(c.get("id", "")),
            markup_escape(c.get("title") or "Untitled conversation"),
            markup_escape(c.get("updated_at", "")),
        )
    console.print(table)


@conversations_app.command("show")
@handle_errors
def conversations_show(
    conversation_id: str = typer.Argument(..., help="Conversation ID (hc_...)"),
) -> None:
    """Show one helper conversation with its messages."""
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request(
            "GET",
            f"/api/v1/helper/conversations/{conversation_id}",
            api_url,
            api_key,
        )
    )
    if status >= 400:
        _fail(status, body)
    if not isinstance(body, dict):
        err_console.print("[red]Unexpected API response.[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]{markup_escape(body.get('title') or 'Untitled conversation')}[/bold]")
    console.print(f"[dim]{markup_escape(conversation_id)}[/dim]")
    messages = body.get("messages") or []
    if not messages:
        console.print("[dim]No messages.[/dim]")
        return
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "user":
            console.print(f"\n[bold cyan]You:[/bold cyan] {markup_escape(content)}")
        elif role == "assistant":
            console.print("\n[bold green]Helper:[/bold green]")
            _render_answer(content, markdown=console.is_terminal)
            citations = m.get("citations")
            if citations:
                _render_citations(citations, markdown=console.is_terminal)
        elif role == "note":
            # A write action's outcome (confirmed / cancelled / expired).
            console.print(f"\n[dim]  {markup_escape(content)}[/dim]")
        else:
            tool_name = m.get("tool_name") or m.get("role", "tool")
            console.print(f"\n[dim]  · {markup_escape(tool_name)}[/dim]")


@conversations_app.command("delete")
@handle_errors
def conversations_delete(
    conversation_id: str = typer.Argument(..., help="Conversation ID (hc_...)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete a helper conversation and its messages."""
    confirm_or_exit(
        f"Delete conversation {conversation_id}? This permanently deletes it.",
        yes=yes,
    )
    api_url, api_key = _get_api_config()
    status, body, _ = asyncio.run(
        _api_request(
            "DELETE",
            f"/api/v1/helper/conversations/{conversation_id}",
            api_url,
            api_key,
        )
    )
    if status >= 400:
        _fail(status, body)
    console.print("[green]Deleted.[/green]")
