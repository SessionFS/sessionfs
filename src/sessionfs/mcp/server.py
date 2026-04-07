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
    Tool(
        name="get_session_summary",
        description=(
            "Get a structured summary of a past session — files modified, "
            "commands run, tests executed, errors encountered, and packages "
            "installed. Useful for understanding what a session accomplished "
            "before resuming or reviewing it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID (ses_... format)",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="get_audit_report",
        description=(
            "Get the trust audit report for a session — verifiable claims, "
            "their verdicts (verified/unverified/hallucination), confidence "
            "scores, and severity classifications. Use when evaluating the "
            "trustworthiness of work done in a past session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID (ses_... format)",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="search_project_knowledge",
        description=(
            "Search the project knowledge base for specific information. "
            "Returns matching knowledge entries filtered by query and type."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "entry_type": {
                    "type": "string",
                    "description": "Filter: decision, pattern, discovery, convention, bug, dependency",
                },
                "limit": {"type": "number", "description": "Max results (default 10)"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="ask_project",
        description=(
            "Ask a question about the project. Researches the knowledge base "
            "and session history to provide an answer."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Question about the project",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["question"],
        },
    ),
    Tool(
        name="add_knowledge",
        description=(
            "Add a knowledge entry to the project knowledge base. "
            "Use this when you discover important patterns, decisions, "
            "conventions, bugs, or dependencies during a session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The knowledge entry content",
                },
                "entry_type": {
                    "type": "string",
                    "description": "Type: decision, pattern, discovery, convention, bug, dependency",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional session ID to link this entry to",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence score 0.0–1.0 (default: 1.0)",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["content", "entry_type"],
        },
    ),
    Tool(
        name="update_wiki_page",
        description=(
            "Create or update a wiki page in the project knowledge base. "
            "Use this to document architecture, conventions, or concepts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Page slug (URL-safe identifier, e.g. 'architecture')",
                },
                "content": {
                    "type": "string",
                    "description": "Page content in markdown",
                },
                "title": {
                    "type": "string",
                    "description": "Page title (optional, derived from slug if omitted)",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["slug", "content"],
        },
    ),
    Tool(
        name="list_wiki_pages",
        description=(
            "List all wiki pages in the project knowledge base. "
            "Returns page slugs, titles, and word counts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
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
        elif name == "get_session_summary":
            result = _handle_get_summary(arguments)
        elif name == "get_audit_report":
            result = _handle_get_audit(arguments)
        elif name == "search_project_knowledge":
            result = await _handle_search_knowledge(arguments)
            return [TextContent(type="text", text=result if isinstance(result, str) else json.dumps(result, indent=2, default=str))]
        elif name == "ask_project":
            result = await _handle_ask_project(arguments)
            return [TextContent(type="text", text=result if isinstance(result, str) else json.dumps(result, indent=2, default=str))]
        elif name == "add_knowledge":
            result = await _handle_add_knowledge(arguments)
            return [TextContent(type="text", text=result if isinstance(result, str) else json.dumps(result, indent=2, default=str))]
        elif name == "update_wiki_page":
            result = await _handle_update_wiki_page(arguments)
            return [TextContent(type="text", text=result if isinstance(result, str) else json.dumps(result, indent=2, default=str))]
        elif name == "list_wiki_pages":
            result = await _handle_list_wiki_pages(arguments)
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


async def _resolve_workspace_git_remote() -> str:
    """Try to detect the git remote from the MCP client's workspace roots."""
    import subprocess
    from urllib.parse import urlparse

    # 1. Try MCP roots (the proper way — asks the client for workspace dirs)
    try:
        ctx = app.request_context
        session = ctx.session
        roots_result = await session.list_roots()
        if roots_result and roots_result.roots:
            for root in roots_result.roots:
                root_uri = str(root.uri)
                # Convert file:// URI to path
                if root_uri.startswith("file://"):
                    root_path = urlparse(root_uri).path
                else:
                    root_path = root_uri
                try:
                    result = subprocess.run(
                        ["git", "remote", "get-url", "origin"],
                        capture_output=True, text=True, timeout=5,
                        cwd=root_path,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        return result.stdout.strip()
                except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                    continue
    except Exception:
        pass  # Roots not supported or no session context

    # 2. Fallback: try CWD (works if MCP server was started from the project dir)
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return ""


async def _handle_get_project_context(args: dict) -> str:
    """Get shared project context from the cloud API."""
    git_remote = args.get("git_remote", "")
    if not git_remote:
        git_remote = await _resolve_workspace_git_remote()

    if not git_remote:
        return "No git repository detected. Pass git_remote explicitly or ensure the MCP server can access workspace roots."

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
        context = (
            f"# Project Context: {data.get('name', normalized)}\n"
            f"_Last updated: {data.get('updated_at', '')[:10]}_\n\n"
            f"{doc}"
        )

        # Enrich with wiki pages
        try:
            project_id = data.get("id", "")
            if project_id:
                async with httpx.AsyncClient(timeout=10) as pages_client:
                    pages_resp = await pages_client.get(
                        f"{config.sync.api_url.rstrip('/')}/api/v1/projects/{project_id}/pages",
                        headers={"Authorization": f"Bearer {config.sync.api_key}"},
                    )
                if pages_resp.status_code == 200:
                    pages = pages_resp.json()
                    if pages:
                        wiki_section = "\n\n---\n## Wiki Pages\n"
                        for p in pages:
                            wiki_section += f"- [{p['title']}]({p['slug']}) ({p['word_count']} words)\n"
                        context += wiki_section
        except Exception:
            pass  # Don't fail if wiki unavailable

        # Enrich with recent knowledge entries
        try:
            project_id = data.get("id", "")
            if project_id:
                async with httpx.AsyncClient(timeout=10) as entries_client:
                    entries_resp = await entries_client.get(
                        f"{config.sync.api_url.rstrip('/')}/api/v1/projects/{project_id}/entries?limit=20&pending=true",
                        headers={"Authorization": f"Bearer {config.sync.api_key}"},
                    )
                if entries_resp.status_code == 200:
                    resp_data = entries_resp.json()
                    entries = resp_data if isinstance(resp_data, list) else resp_data.get("entries", [])
                    if entries:
                        activity = "\n\n---\n## Recent Session Activity\n"
                        activity += "*(Auto-extracted from recent sessions. Not yet compiled into the main document.)*\n\n"
                        for e in entries:
                            activity += f"- [{e['entry_type']}] {e['content']}\n"
                        context += activity
        except Exception:
            pass  # Don't fail if entries unavailable

        # Append contribution instructions so agents know how to write back
        context += """

---
## Contributing to This Knowledge Base
If you discover something important during this session, write it back:
- `add_knowledge("what you learned", "type")` — types: decision, pattern, discovery, convention, bug, dependency
- `update_wiki_page("slug", "full markdown content")` — create or update a wiki page
- `list_wiki_pages()` — see existing pages
- `search_project_knowledge("query")` — search the knowledge base

Your contributions are immediately available to the next AI agent in this repo.
"""

        return context
    except Exception as e:
        logger.warning("Failed to fetch project context: %s", e)
        return f"Could not fetch project context: {e}"


def _handle_get_summary(args: dict) -> dict[str, Any]:
    """Get deterministic session summary."""
    session_id = args.get("session_id", "")
    store = _get_store()
    session_dir = store.get_session_dir(session_id)
    if not session_dir:
        return {"error": f"Session {session_id} not found"}

    manifest_path = session_dir / "manifest.json"
    if not manifest_path.exists():
        return {"error": f"No manifest for session {session_id}"}

    manifest = json.loads(manifest_path.read_text())
    messages = read_sfs_messages(session_dir)

    workspace_path = session_dir / "workspace.json"
    workspace = {}
    if workspace_path.exists():
        try:
            workspace = json.loads(workspace_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    from sessionfs.server.services.summarizer import summarize_session
    from dataclasses import asdict

    summary = summarize_session(messages, manifest, workspace)
    data = asdict(summary)
    # Remove None narrative fields for cleaner output
    for key in ("what_happened", "key_decisions", "outcome", "open_issues", "narrative_model"):
        if data.get(key) is None:
            del data[key]
    return data


def _handle_get_audit(args: dict) -> dict[str, Any]:
    """Get audit report for a session."""
    session_id = args.get("session_id", "")
    store = _get_store()
    session_dir = store.get_session_dir(session_id)
    if not session_dir:
        return {"error": f"Session {session_id} not found"}

    from sessionfs.judge.report import load_report

    report = load_report(session_dir)
    if not report:
        return {"error": f"No audit report for session {session_id}. Run: sfs audit {session_id}"}

    from dataclasses import asdict
    data = asdict(report)

    # Add human-readable summary
    s = report.summary
    data["readable_summary"] = (
        f"Trust Score: {s.trust_score:.0%} | "
        f"Claims: {s.total_claims} | "
        f"Verified: {s.verified} | "
        f"Unverified: {s.unverified} | "
        f"Hallucinations: {s.hallucinations} | "
        f"Critical: {s.critical_count} | High: {s.high_count}"
    )

    return data


async def _handle_search_knowledge(args: dict) -> str:
    """Search project knowledge entries via cloud API."""
    query = args.get("query", "")
    entry_type = args.get("entry_type")
    limit = int(args.get("limit", 10))
    git_remote = args.get("git_remote", "")

    if not git_remote:
        git_remote = await _resolve_workspace_git_remote()

    if not git_remote:
        return "No git repository detected. Pass git_remote explicitly or ensure workspace roots are available."

    from sessionfs.server.github_app import normalize_git_remote
    normalized = normalize_git_remote(git_remote)
    if not normalized:
        return "Could not parse git remote URL."

    try:
        from sessionfs.daemon.config import load_config
        config = load_config()
        if not config.sync.api_key:
            return "Not authenticated with SessionFS cloud. Run 'sfs auth login' first."

        import httpx

        # First get project ID
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{config.sync.api_url.rstrip('/')}/api/v1/projects/{normalized}",
                headers={"Authorization": f"Bearer {config.sync.api_key}"},
            )
        if resp.status_code == 404:
            return f"No project found for {normalized}."
        if resp.status_code >= 400:
            return f"Error fetching project: {resp.status_code}"

        project_data = resp.json()
        project_id = project_data.get("id", "")

        # Search entries
        params = f"?search={query}&limit={limit}"
        if entry_type:
            params += f"&type={entry_type}"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{config.sync.api_url.rstrip('/')}/api/v1/projects/{project_id}/entries{params}",
                headers={"Authorization": f"Bearer {config.sync.api_key}"},
            )
        if resp.status_code >= 400:
            return f"Error searching entries: {resp.status_code}"

        entries = resp.json()
        if not entries:
            return f"No knowledge entries found matching '{query}'."

        # Format as readable markdown
        type_badges = {
            "decision": "\U0001f3af",
            "pattern": "\U0001f504",
            "discovery": "\U0001f50d",
            "convention": "\U0001f4cf",
            "bug": "\U0001f41b",
            "dependency": "\U0001f4e6",
        }

        lines = [f"# Knowledge Search: \"{query}\"", f"_{len(entries)} result(s)_\n"]
        for e in entries:
            etype = e.get("entry_type", "unknown")
            badge = type_badges.get(etype, "\u2022")
            confidence = e.get("confidence", 0)
            created = e.get("created_at", "")[:10]
            session_id = e.get("session_id", "unknown")

            lines.append(f"### {badge} [{etype.upper()}] (confidence: {confidence:.0%})")
            lines.append(f"{e.get('content', '')}")
            lines.append(f"_Source session: {session_id} | {created}_\n")

        return "\n".join(lines)

    except Exception as exc:
        logger.warning("Knowledge search failed: %s", exc)
        return f"Knowledge search failed: {exc}"


async def _resolve_project_id(git_remote: str = "") -> tuple[str, str, str]:
    """Detect git remote, authenticate, and return (api_url, api_key, project_id).

    Raises Exception with a user-friendly message on failure.
    """
    if not git_remote:
        git_remote = await _resolve_workspace_git_remote()

    if not git_remote:
        raise Exception("No git repository detected. Pass git_remote explicitly or ensure workspace roots are available.")

    from sessionfs.server.github_app import normalize_git_remote
    normalized = normalize_git_remote(git_remote)
    if not normalized:
        raise Exception("Could not parse git remote URL.")

    config = load_config()
    if not config.sync.api_key:
        raise Exception("Not authenticated. Run 'sfs auth login' first.")

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{config.sync.api_url.rstrip('/')}/api/v1/projects/{normalized}",
            headers={"Authorization": f"Bearer {config.sync.api_key}"},
        )
    if resp.status_code == 404:
        raise Exception(f"No project found for {normalized}. Create one with: sfs project init")
    if resp.status_code >= 400:
        raise Exception(f"Error fetching project: {resp.status_code}")

    project_id = resp.json().get("id", "")
    return config.sync.api_url.rstrip("/"), config.sync.api_key, project_id


async def _handle_add_knowledge(args: dict) -> str:
    """Add a knowledge entry via the cloud API."""
    content = args.get("content", "")
    entry_type = args.get("entry_type", "discovery")
    session_id = args.get("session_id")
    confidence = float(args.get("confidence", 1.0))
    git_remote = args.get("git_remote", "")

    if not content:
        return "Content is required."

    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)

        import httpx
        payload: dict = {
            "content": content,
            "entry_type": entry_type,
            "confidence": confidence,
        }
        if session_id:
            payload["session_id"] = session_id

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{api_url}/api/v1/projects/{project_id}/entries/add",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 201:
            data = resp.json()
            return f"Knowledge entry added (id: {data['id']}, type: {entry_type})."
        return f"Failed to add entry: {resp.status_code} — {resp.text}"

    except Exception as exc:
        return f"Failed: {exc}"


async def _handle_update_wiki_page(args: dict) -> str:
    """Create or update a wiki page via the cloud API."""
    slug = args.get("slug", "")
    content = args.get("content", "")
    title = args.get("title")
    git_remote = args.get("git_remote", "")

    if not slug or not content:
        return "slug and content are required."

    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)

        import httpx
        payload: dict = {"content": content}
        if title:
            payload["title"] = title

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(
                f"{api_url}/api/v1/projects/{project_id}/pages/{slug}",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 200:
            data = resp.json()
            return f"Page '{data['slug']}' updated ({data['word_count']} words)."
        return f"Failed to update page: {resp.status_code} — {resp.text}"

    except Exception as exc:
        return f"Failed: {exc}"


async def _handle_list_wiki_pages(args: dict) -> str:
    """List wiki pages via the cloud API."""
    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)

        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{api_url}/api/v1/projects/{project_id}/pages",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code != 200:
            return f"Failed to list pages: {resp.status_code}"

        pages = resp.json()
        if not pages:
            return "No wiki pages found for this project."

        lines = [f"# Wiki Pages ({len(pages)})\n"]
        for p in pages:
            auto = " [auto]" if p.get("auto_generated") else ""
            lines.append(
                f"- **{p['title']}** (`{p['slug']}`) — "
                f"{p['word_count']} words, {p['entry_count']} entries{auto}"
            )
        return "\n".join(lines)

    except Exception as exc:
        return f"Failed: {exc}"


async def _handle_ask_project(args: dict) -> str:
    """Research a question using project context and knowledge entries."""
    question = args.get("question", "")
    git_remote = args.get("git_remote", "")
    if not question:
        return "Please provide a question."

    # Get project context (pass through git_remote)
    context_result = await _handle_get_project_context({"git_remote": git_remote})
    if not isinstance(context_result, str):
        context_result = json.dumps(context_result, indent=2, default=str)

    # Search knowledge entries for the question
    search_result = await _handle_search_knowledge({"query": question, "limit": 15, "git_remote": git_remote})
    if not isinstance(search_result, str):
        search_result = json.dumps(search_result, indent=2, default=str)

    # Search local sessions for additional context
    local_results = ""
    try:
        search = _get_search()
        hits = search.search(question, limit=5)
        if hits:
            local_lines = ["\n## Related Local Sessions"]
            for hit in hits:
                sid = hit.get("session_id", "")
                title = hit.get("title", "Untitled")
                tool = hit.get("source_tool", "")
                local_lines.append(f"- **{title}** ({tool}) — `{sid}`")
            local_results = "\n".join(local_lines)
    except RuntimeError:
        pass  # Search index not available

    # Assemble research material
    lines = [
        f"# Research: {question}\n",
        "## Project Context",
        context_result,
        "\n## Knowledge Base Matches",
        search_result,
    ]

    if local_results:
        lines.append(local_results)

    lines.append(
        "\n---\n"
        "*This is research material gathered from the project knowledge base "
        "and session history. Check the referenced sessions for more detail.*\n\n"
        "**Important:** If your answer reveals something new about the codebase, "
        "save it back using `add_knowledge(\"what you learned\", \"discovery\")` "
        "so future sessions benefit from this research."
    )

    return "\n".join(lines)


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
