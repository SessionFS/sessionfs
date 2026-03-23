"""Remote MCP server for SessionFS — HTTP/SSE transport.

Runs as a standalone service at mcp.sessionfs.dev. Claude.ai and other
remote MCP clients connect here to search the user's cloud sessions.

Authentication: Bearer token (SessionFS API key) in the Authorization header.
Transport: MCP over HTTP with Server-Sent Events.

Usage:
    uvicorn sessionfs.mcp.remote_server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from sessionfs.mcp.cloud_client import CloudAPIClient

logger = logging.getLogger("sessionfs.mcp.remote")

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = Server("sessionfs-remote")
_cloud: CloudAPIClient | None = None

# Per-request API key stored in context (set by SSE transport auth)
_current_api_key: str | None = None


def _get_cloud() -> CloudAPIClient:
    if _cloud is None:
        raise RuntimeError("Cloud client not initialized")
    return _cloud


# ---------------------------------------------------------------------------
# Tool definitions (same 4 tools as local server)
# ---------------------------------------------------------------------------

_TOOLS = [
    Tool(
        name="search_sessions",
        description=(
            "Search your past AI coding sessions for relevant context. "
            "Use this when debugging a problem that may have been solved before, "
            "or when looking for past architectural decisions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — keywords, error messages, file paths",
                },
                "tool_filter": {
                    "type": "string",
                    "description": "Filter by tool (claude-code, codex, gemini, copilot, cursor, amp, cline, roo-code)",
                },
                "max_results": {
                    "type": "number",
                    "description": "Max results to return (default: 5)",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_session_context",
        description=(
            "Get full conversation context from a specific past session. "
            "Use after search_sessions finds a relevant session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID (ses_... format)",
                },
                "max_messages": {
                    "type": "number",
                    "description": "Limit messages returned (default: 50)",
                },
                "summary_only": {
                    "type": "boolean",
                    "description": "Return just metadata summary (default: false)",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="list_recent_sessions",
        description="List recent AI coding sessions.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "number",
                    "description": "Max sessions to return (default: 10)",
                },
                "tool_filter": {
                    "type": "string",
                    "description": "Filter by tool",
                },
            },
        },
    ),
    Tool(
        name="find_related_sessions",
        description="Find past sessions related to specific files or errors.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Find sessions that touched this file",
                },
                "error_text": {
                    "type": "string",
                    "description": "Find sessions with similar errors",
                },
                "limit": {
                    "type": "number",
                    "description": "Max results (default: 5)",
                },
            },
        },
    ),
]


@mcp.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS


@mcp.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    global _current_api_key
    api_key = _current_api_key
    if not api_key:
        return [TextContent(type="text", text=json.dumps({"error": "Not authenticated"}))]

    cloud = _get_cloud()

    try:
        if name == "search_sessions":
            data = await cloud.search(
                api_key=api_key,
                query=arguments["query"],
                tool_filter=arguments.get("tool_filter"),
                days=arguments.get("days"),
                max_results=int(arguments.get("max_results", 5)),
            )
            results = data.get("results", [])
            if not results:
                return [TextContent(type="text", text="No matching sessions found.")]

            lines = [f"Found {len(results)} matching session(s):\n"]
            for r in results:
                lines.append(f"**{r.get('title', 'Untitled')}** (`{r['session_id']}`)")
                lines.append(f"  Tool: {r.get('source_tool', '?')} | Messages: {r.get('message_count', 0)} | {r.get('updated_at', '')[:10]}")
                for m in r.get("matches", []):
                    lines.append(f"  > {m.get('snippet', '')}")
                lines.append("")
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "get_session_context":
            session_id = arguments["session_id"]
            max_msgs = int(arguments.get("max_messages", 50))
            summary_only = arguments.get("summary_only", False)

            session = await cloud.get_session(api_key, session_id)

            if summary_only:
                return [TextContent(type="text", text=json.dumps(session, indent=2))]

            msgs_data = await cloud.get_messages(api_key, session_id, page_size=max_msgs)
            messages = msgs_data.get("messages", [])

            lines = [
                f"**{session.get('title', 'Untitled')}** ({session.get('source_tool', '?')})",
                f"Messages: {msgs_data.get('total', 0)} | Showing: {len(messages)}\n",
            ]
            for msg in messages:
                role = msg.get("role", "?")
                content = msg.get("content", [])
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_use":
                                parts.append(f"[tool: {block.get('name', '?')}]")
                            elif block.get("type") == "tool_result":
                                r = block.get("content", "")
                                parts.append(f"[result: {str(r)[:200]}]")
                    text = "\n".join(parts)
                lines.append(f"**{role}:** {text[:500]}")
                lines.append("")

            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "list_recent_sessions":
            limit = int(arguments.get("limit", 10))
            tool_filter = arguments.get("tool_filter")
            data = await cloud.list_sessions(
                api_key, page_size=limit, source_tool=tool_filter,
            )
            sessions = data.get("sessions", [])
            if not sessions:
                return [TextContent(type="text", text="No sessions found.")]

            lines = [f"Recent sessions ({len(sessions)}):\n"]
            for s in sessions:
                lines.append(
                    f"- **{s.get('title', 'Untitled')}** (`{s['id']}`) "
                    f"— {s.get('source_tool', '?')}, {s.get('message_count', 0)} msgs, "
                    f"{s.get('updated_at', '')[:10]}"
                )
            return [TextContent(type="text", text="\n".join(lines))]

        elif name == "find_related_sessions":
            file_path = arguments.get("file_path")
            error_text = arguments.get("error_text")
            if not file_path and not error_text:
                return [TextContent(type="text", text=json.dumps({"error": "Provide file_path or error_text"}))]

            query = file_path or error_text or ""
            data = await cloud.search(
                api_key=api_key,
                query=query,
                max_results=int(arguments.get("limit", 5)),
            )
            results = data.get("results", [])
            if not results:
                return [TextContent(type="text", text="No related sessions found.")]

            lines = [f"Found {len(results)} related session(s):\n"]
            for r in results:
                lines.append(f"- **{r.get('title', 'Untitled')}** (`{r['session_id']}`) — {r.get('source_tool', '?')}")
            return [TextContent(type="text", text="\n".join(lines))]

        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    except Exception as e:
        logger.exception("Tool call failed: %s", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# ---------------------------------------------------------------------------
# HTTP/SSE App
# ---------------------------------------------------------------------------

sse_transport = SseServerTransport("/messages/")


async def handle_sse(request: Request):
    """SSE endpoint — client connects here for MCP communication."""
    global _current_api_key

    # Authenticate
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "Missing Authorization: Bearer <api_key>"}, status_code=401)

    api_key = auth_header[7:]
    cloud = _get_cloud()

    if not await cloud.validate_key(api_key):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)

    _current_api_key = api_key

    async with sse_transport.connect_sse(request.scope, request.receive, request._send) as streams:
        await mcp.run(streams[0], streams[1], mcp.create_initialization_options())


async def handle_messages(request: Request):
    """POST endpoint for MCP messages over SSE."""
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)


async def handle_health(request: Request):
    """Health check."""
    return JSONResponse({"status": "healthy", "service": "sessionfs-mcp"})


@asynccontextmanager
async def lifespan(app):
    global _cloud
    api_url = os.environ.get("SFS_API_URL", "https://api.sessionfs.dev")
    _cloud = CloudAPIClient(api_url)
    logger.info("Remote MCP server started, API: %s", api_url)
    yield
    _cloud = None


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/health", handle_health),
        Route("/sse", handle_sse),
        Mount("/messages/", routes=[Route("/", handle_messages, methods=["POST"])]),
    ],
)
