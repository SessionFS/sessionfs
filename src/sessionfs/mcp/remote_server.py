"""Remote MCP server for SessionFS — HTTP/SSE transport.

Runs as a standalone service at mcp.sessionfs.dev. Claude.ai and other
remote MCP clients connect here to search the user's cloud sessions.

Authentication: Bearer token (SessionFS API key) in the Authorization header.
Transport: MCP over HTTP with Server-Sent Events.

Usage:
    uvicorn sessionfs.mcp.remote_server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

from mcp.server import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

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
    Tool(
        name="get_project_context",
        description=(
            "Get the shared project context document for a repository. "
            "Returns architecture decisions, conventions, API contracts, "
            "and team information that all agents should know. "
            "Call this early in a session to understand the project."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_remote": {
                    "type": "string",
                    "description": "Git remote URL or normalized owner/repo format",
                },
            },
            "required": ["git_remote"],
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

        elif name == "get_project_context":
            git_remote = arguments.get("git_remote", "")
            if not git_remote:
                return [TextContent(type="text", text="Provide a git_remote argument (e.g. 'github.com/owner/repo' or SSH URL).")]

            from sessionfs.server.github_app import normalize_git_remote
            normalized = normalize_git_remote(git_remote)
            if not normalized:
                return [TextContent(type="text", text="Could not parse git remote URL.")]

            data = await cloud.get_project_context(api_key, normalized)
            if data is None:
                return [TextContent(type="text", text=(
                    f"No project context found for {normalized}. "
                    f"Create one with: sfs project init && sfs project edit"
                ))]

            doc = data.get("context_document", "")
            if not doc.strip():
                return [TextContent(type="text", text=(
                    f"Project context for {normalized} exists but is empty. "
                    f"Edit it with: sfs project edit"
                ))]

            return [TextContent(type="text", text=(
                f"# Project Context: {data.get('name', normalized)}\n"
                f"_Last updated: {data.get('updated_at', '')[:10]}_\n\n"
                f"{doc}"
            ))]

        else:
            return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    except Exception as e:
        logger.exception("Tool call failed: %s", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# ---------------------------------------------------------------------------
# HTTP/SSE App
# ---------------------------------------------------------------------------

import asyncio as _asyncio

# Persistent MCP sessions: session_id -> transport
_mcp_sessions: dict[str, StreamableHTTPServerTransport] = {}
# Track background tasks to prevent GC
_session_tasks: dict[str, _asyncio.Task] = {}


async def _run_mcp_session(transport: StreamableHTTPServerTransport) -> None:
    """Background task that keeps the MCP server running for a session."""
    async with transport.connect() as (read_stream, write_stream):
        await mcp.run(read_stream, write_stream, mcp.create_initialization_options())


async def handle_mcp(request: Request):
    """Handle MCP requests — persistent sessions across requests."""
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

    # Check for existing session
    session_id = request.headers.get("mcp-session-id")

    if session_id and session_id in _mcp_sessions:
        transport = _mcp_sessions[session_id]
        await transport.handle_request(request.scope, request.receive, request._send)
        return

    # New session — create persistent transport
    transport = StreamableHTTPServerTransport(
        mcp_session_id=None,
        is_json_response_enabled=True,
    )

    # Start MCP server as a background task (persists across requests)
    task = _asyncio.create_task(_run_mcp_session(transport))

    # Handle the initialize request
    await transport.handle_request(request.scope, request.receive, request._send)

    # Store session for subsequent requests
    if transport.mcp_session_id:
        _mcp_sessions[transport.mcp_session_id] = transport
        _session_tasks[transport.mcp_session_id] = task


async def handle_health(request: Request):
    """Health check."""
    return JSONResponse({"status": "healthy", "service": "sessionfs-mcp"})


# ---------------------------------------------------------------------------
# OAuth 2.0 Authorization Code Flow with PKCE
# ---------------------------------------------------------------------------
# Claude.ai sends users here to authorize. The user enters their SessionFS
# API key, we validate it, then redirect back with an auth code. Claude.ai
# exchanges the code for an access token (which is just the API key).

# In-memory store: auth_code -> {api_key, code_challenge, redirect_uri, expires}
_auth_codes: dict[str, dict[str, Any]] = {}

_AUTHORIZE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Authorize SessionFS</title>
<style>
  body {{ background: #0d1117; color: #e6edf3; font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 32px; max-width: 400px; width: 100%; }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }}
  p {{ color: #8b949e; font-size: 14px; margin: 0 0 24px; }}
  label {{ display: block; font-size: 13px; color: #8b949e; margin-bottom: 6px; }}
  input {{ width: 100%; padding: 10px 12px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; color: #e6edf3; font-size: 14px; font-family: monospace; box-sizing: border-box; }}
  input:focus {{ outline: none; border-color: #58a6ff; }}
  button {{ width: 100%; padding: 10px; background: #238636; color: white; border: none; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer; margin-top: 16px; }}
  button:hover {{ background: #2ea043; }}
  .error {{ color: #f85149; font-size: 13px; margin-top: 8px; display: none; }}
  .hint {{ color: #8b949e; font-size: 12px; margin-top: 12px; }}
</style>
</head>
<body>
<div class="card">
  <h1>Connect SessionFS</h1>
  <p>Enter your API key to let Claude search your past coding sessions.</p>
  <form method="POST" action="/authorize">
    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
    <input type="hidden" name="state" value="{state}">
    <input type="hidden" name="code_challenge" value="{code_challenge}">
    <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
    <label for="api_key">SessionFS API Key</label>
    <input type="password" id="api_key" name="api_key" placeholder="sk_sfs_..." required>
    {error_html}
    <button type="submit">Authorize</button>
    <p class="hint">Find your key: <code>sfs config show</code></p>
  </form>
</div>
</body>
</html>"""


async def handle_authorize_get(request: Request):
    """GET /authorize — show the API key form."""
    return HTMLResponse(_AUTHORIZE_HTML.format(
        redirect_uri=request.query_params.get("redirect_uri", ""),
        state=request.query_params.get("state", ""),
        code_challenge=request.query_params.get("code_challenge", ""),
        code_challenge_method=request.query_params.get("code_challenge_method", ""),
        error_html="",
    ))


async def handle_authorize_post(request: Request):
    """POST /authorize — validate key and redirect with auth code."""
    form = await request.form()
    api_key = str(form.get("api_key", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    state = str(form.get("state", ""))
    code_challenge = str(form.get("code_challenge", ""))
    code_challenge_method = str(form.get("code_challenge_method", ""))

    cloud = _get_cloud()
    if not await cloud.validate_key(api_key):
        return HTMLResponse(_AUTHORIZE_HTML.format(
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            error_html='<p class="error" style="display:block">Invalid API key. Check with: sfs config show</p>',
        ), status_code=400)

    # Generate auth code
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "api_key": api_key,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "redirect_uri": redirect_uri,
        "expires": time.time() + 300,  # 5 min
    }

    # Redirect back to Claude.ai
    params = {"code": code, "state": state}
    return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=302)


async def handle_token(request: Request):
    """POST /token — exchange auth code for access token."""
    # Clean expired codes
    now = time.time()
    expired = [k for k, v in _auth_codes.items() if v["expires"] < now]
    for k in expired:
        del _auth_codes[k]

    body = await request.form()
    grant_type = str(body.get("grant_type", ""))
    code = str(body.get("code", ""))
    code_verifier = str(body.get("code_verifier", ""))

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    stored = _auth_codes.pop(code, None)
    if not stored:
        return JSONResponse({"error": "invalid_grant", "error_description": "Invalid or expired code"}, status_code=400)

    # Verify PKCE challenge
    if stored["code_challenge_method"] == "S256":
        expected = hashlib.sha256(code_verifier.encode()).digest()
        import base64
        expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
        if expected_b64 != stored["code_challenge"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)

    # Return the API key as the access token
    return JSONResponse({
        "access_token": stored["api_key"],
        "token_type": "Bearer",
        "expires_in": 86400 * 365,  # effectively no expiry
    })


# ---------------------------------------------------------------------------
# OAuth metadata (well-known)
# ---------------------------------------------------------------------------

def _get_base_url(request: Request) -> str:
    """Get the public base URL, respecting X-Forwarded-Proto from Cloud Run."""
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host", request.base_url.hostname or "mcp.sessionfs.dev")
    return f"{proto}://{host}"


async def handle_oauth_metadata(request: Request):
    """GET /.well-known/oauth-authorization-server"""
    base = _get_base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def handle_protected_resource_metadata(request: Request):
    """GET /.well-known/oauth-protected-resource (RFC 9728)"""
    base = _get_base_url(request)
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": [],
        "bearer_methods_supported": ["header"],
    })


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------
# Claude.ai registers as a public client. We accept any registration and
# return a client_id. Since we authenticate via the user's API key (not
# client credentials), the client_id is just for protocol compliance.

_registered_clients: dict[str, dict[str, Any]] = {}


async def handle_register(request: Request):
    """POST /register — Dynamic Client Registration."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    client_id = secrets.token_urlsafe(16)
    redirect_uris = body.get("redirect_uris", [])

    _registered_clients[client_id] = {
        "client_id": client_id,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "client_name": body.get("client_name", "MCP Client"),
    }

    return JSONResponse(
        {
            "client_id": client_id,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        status_code=201,
    )


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
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["https://claude.ai", "https://claude.com"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["Mcp-Session-Id"],
        ),
    ],
    routes=[
        Route("/health", handle_health),
        Route("/.well-known/oauth-authorization-server", handle_oauth_metadata),
        Route("/.well-known/oauth-protected-resource", handle_protected_resource_metadata),
        Route("/register", handle_register, methods=["POST"]),
        Route("/authorize", handle_authorize_get, methods=["GET"]),
        Route("/authorize", handle_authorize_post, methods=["POST"]),
        Route("/token", handle_token, methods=["POST"]),
        Route("/sse", handle_mcp, methods=["GET", "POST"]),
        Route("/mcp", handle_mcp, methods=["GET", "POST"]),
        Route("/", handle_mcp, methods=["GET", "POST", "DELETE"]),
    ],
)
