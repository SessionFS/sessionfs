"""SessionFS MCP server.

Exposes session search and retrieval as MCP tools that AI coding agents
can call during conversations. Runs on stdio transport.

Usage:
    sfs mcp serve
    # Or in Claude Code config: {"command": "sfs", "args": ["mcp", "serve"]}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.types import TextContent, Tool

from sessionfs.cli.common import read_sfs_messages
from sessionfs.daemon.config import load_config
from sessionfs.mcp.search import SessionSearchIndex
from sessionfs.store.local import LocalStore

logger = logging.getLogger("sessionfs.mcp")

app = Server("sessionfs")

# Module-level state (initialized in serve())
_store: LocalStore | None = None
_search: SessionSearchIndex | None = None


def _get_store() -> LocalStore:
    if _store is None:
        raise RuntimeError("MCP server not initialized")
    return _store


def _get_search() -> SessionSearchIndex:
    if _search is None:
        raise RuntimeError("Search index not initialized")
    return _search


# ---------------------------------------------------------------------------
# Tool definitions
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
                    "description": "Filter by tool (claude-code, codex, gemini, cursor, copilot, amp, cline, roo-code)",
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
                    "description": "Return just metadata summary instead of full messages (default: false)",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="list_recent_sessions",
        description=(
            "List recent AI coding sessions. Use when the user asks about "
            "recent work or when you need to understand what was done recently."
        ),
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
                "project_filter": {
                    "type": "string",
                    "description": "Filter by project/workspace path substring",
                },
            },
        },
    ),
    Tool(
        name="find_related_sessions",
        description=(
            "Find past sessions related to specific files or errors. "
            "Use when working on a file that was modified in past sessions, "
            "or encountering an error that may have been seen before."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Find sessions that touched this file",
                },
                "error_text": {
                    "type": "string",
                    "description": "Find sessions that encountered similar errors",
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
                    "description": "Git remote URL (auto-detected from CWD if empty)",
                },
            },
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "search_sessions":
            result = _handle_search(arguments)
        elif name == "get_session_context":
            result = _handle_get_context(arguments)
        elif name == "list_recent_sessions":
            result = _handle_list_recent(arguments)
        elif name == "find_related_sessions":
            result = _handle_find_related(arguments)
        elif name == "get_project_context":
            result = await _handle_get_project_context(arguments)
            return [TextContent(type="text", text=result if isinstance(result, str) else json.dumps(result, indent=2, default=str))]
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        logger.error("Tool %s failed: %s", name, exc, exc_info=True)
        result = {"error": str(exc)}

    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _handle_search(args: dict) -> dict[str, Any]:
    query = args.get("query", "")
    tool_filter = args.get("tool_filter")
    max_results = int(args.get("max_results", 5))

    search = _get_search()
    results = search.search(query, tool_filter=tool_filter, limit=max_results)

    return {
        "query": query,
        "results": results,
        "count": len(results),
    }


def _handle_get_context(args: dict) -> dict[str, Any]:
    session_id = args.get("session_id", "")
    max_messages = int(args.get("max_messages", 50))
    summary_only = args.get("summary_only", False)

    store = _get_store()
    session_dir = store.get_session_dir(session_id)
    if not session_dir:
        return {"error": f"Session {session_id} not found"}

    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return {"error": f"No manifest for session {session_id}"}

    manifest = json.loads(manifest_path.read_text())

    result: dict[str, Any] = {
        "session_id": session_id,
        "title": manifest.get("title"),
        "source_tool": manifest.get("source", {}).get("tool"),
        "model": manifest.get("model", {}).get("model_id"),
        "created_at": manifest.get("created_at"),
        "stats": manifest.get("stats", {}),
    }

    if summary_only:
        return result

    # Read messages
    messages = read_sfs_messages(session_dir)
    main_messages = [m for m in messages if not m.get("is_sidechain")]

    # Format messages for readability
    formatted = []
    for msg in main_messages[:max_messages]:
        role = msg.get("role", "unknown")
        content_blocks = msg.get("content", [])
        text_parts = []

        if isinstance(content_blocks, str):
            text_parts.append(content_blocks)
        else:
            for block in content_blocks:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype == "tool_use":
                        text_parts.append(f"[tool: {block.get('name', '')}]")
                    elif btype == "tool_result":
                        text_parts.append(f"[result: {str(block.get('content', ''))[:200]}]")
                    elif btype == "thinking":
                        text_parts.append("[thinking...]")

        formatted.append({
            "role": role,
            "text": "\n".join(text_parts),
            "timestamp": msg.get("timestamp"),
        })

    result["messages"] = formatted
    result["messages_returned"] = len(formatted)
    result["messages_total"] = len(main_messages)
    return result


def _handle_list_recent(args: dict) -> dict[str, Any]:
    limit = int(args.get("limit", 10))
    tool_filter = args.get("tool_filter")
    project_filter = args.get("project_filter")

    store = _get_store()
    sessions = store.list_sessions()

    # Filter
    if tool_filter:
        sessions = [s for s in sessions if s.get("source_tool") == tool_filter]
    if project_filter:
        sessions = [
            s for s in sessions
            if project_filter in (s.get("project_path") or "")
        ]

    # Already sorted by created_at DESC from the index
    sessions = sessions[:limit]

    return {
        "sessions": [
            {
                "session_id": s["session_id"],
                "title": s.get("title"),
                "source_tool": s.get("source_tool"),
                "model_id": s.get("model_id"),
                "message_count": s.get("message_count", 0),
                "created_at": s.get("created_at"),
            }
            for s in sessions
        ],
        "count": len(sessions),
    }


def _handle_find_related(args: dict) -> dict[str, Any]:
    file_path = args.get("file_path")
    error_text = args.get("error_text")
    limit = int(args.get("limit", 5))

    search = _get_search()
    results = []

    if file_path:
        results = search.find_by_file(file_path, limit=limit)
    elif error_text:
        results = search.find_by_error(error_text, limit=limit)
    else:
        return {"error": "Provide file_path or error_text"}

    return {
        "file_path": file_path,
        "error_text": error_text,
        "results": results,
        "count": len(results),
    }


async def _handle_get_project_context(args: dict) -> str:
    """Get shared project context from the cloud API."""
    import subprocess

    git_remote = args.get("git_remote", "")
    if not git_remote:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                git_remote = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if not git_remote:
        return "No git repository detected. Cannot look up project context."

    from sessionfs.server.github_app import normalize_git_remote
    normalized = normalize_git_remote(git_remote)
    if not normalized:
        return "Could not parse git remote URL."

    # Use the cloud API to fetch project context
    try:
        from sessionfs.daemon.config import load_config
        config = load_config()
        if not config.sync.api_key:
            return (
                "Not authenticated with SessionFS cloud. Run 'sfs auth login' first.\n"
                "To create project context: sfs project init && sfs project edit"
            )

        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{config.sync.api_url.rstrip('/')}/api/v1/projects/{normalized}",
                headers={"Authorization": f"Bearer {config.sync.api_key}"},
            )
        if resp.status_code == 404:
            return (
                f"No project context found for {normalized}. "
                f"Create one with: sfs project init && sfs project edit"
            )
        if resp.status_code >= 400:
            return f"Error fetching project context: {resp.status_code}"

        data = resp.json()
        doc = data.get("context_document", "")
        if not doc or doc.strip() == "" or doc.strip().startswith("# Project Context\n\n## Overview\n<!-- "):
            return (
                f"Project context for {normalized} exists but is empty. "
                f"Edit it with: sfs project edit"
            )
        return (
            f"# Project Context: {data.get('name', normalized)}\n"
            f"_Last updated: {data.get('updated_at', '')[:10]}_\n\n"
            f"{doc}"
        )
    except Exception as e:
        logger.warning("Failed to fetch project context: %s", e)
        return f"Could not fetch project context: {e}"


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def init_server(store_dir: Path | None = None) -> None:
    """Initialize the MCP server's store and search index."""
    global _store, _search

    if store_dir is None:
        config = load_config()
        store_dir = config.store_dir

    _store = LocalStore(store_dir)
    _store.initialize()

    search_db = store_dir / "search.db"
    _search = SessionSearchIndex(search_db)
    _search.initialize()

    # Ensure all sessions are indexed
    indexed = _search.reindex_all(store_dir)
    if indexed:
        logger.info("Search index: %d sessions indexed", indexed)


async def serve() -> None:
    """Run the MCP server on stdio transport."""
    from mcp.server.stdio import stdio_server

    init_server()

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())
