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
                "audit_session_id": {
                    "type": "string",
                    "description": "Optional current session id to append this retrieval to its audit log",
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
                "audit_session_id": {
                    "type": "string",
                    "description": "Optional current session id to append this retrieval to its audit log",
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
            "Returns matching knowledge entries filtered by query and type. "
            "By default returns only active claims; set include_stale=true for all. "
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project search` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling out."
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
                "include_stale": {
                    "type": "boolean",
                    "description": "Include stale and superseded entries (default: false)",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
                "audit_session_id": {
                    "type": "string",
                    "description": "Optional current session id to append this retrieval to its audit log",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="ask_project",
        description=(
            "Ask a question about the project. Researches the knowledge base "
            "and session history to provide an answer."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project ask` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling out."
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
            "conventions, bugs, or dependencies during a session. "
            "Entries default to 'note' class and auto-promote to 'claim' "
            "when quality gates pass (confidence >= 0.8, content >= 50 chars)."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project add-entry` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling "
            "out — the CLI hits rate limits and auth edge cases that this "
            "tool avoids."
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
                "entity_ref": {
                    "type": "string",
                    "description": "Optional entity reference (e.g., 'src/foo.py', 'KnowledgeEntry')",
                },
                "entity_type": {
                    "type": "string",
                    "description": "Optional entity type (e.g., 'file', 'class', 'function', 'module')",
                },
                "force_claim": {
                    "type": "boolean",
                    "description": "Attempt claim classification (still enforces quality gates)",
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
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project page` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling out."
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
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project pages` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
        },
    ),
    Tool(
        name="get_rules",
        description=(
            "Get canonical project rules and compilation config for this repo. "
            "Read-only — agents never self-modify project rules."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
        },
    ),
    Tool(
        name="get_compiled_rules",
        description=(
            "Get the compiled rule file content for a specific tool. "
            "If `tool` is omitted, the active tool is inferred from the "
            "caller's environment when possible."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "description": "Tool slug: claude-code, codex, cursor, copilot, gemini",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
                "audit_session_id": {
                    "type": "string",
                    "description": "Optional current session id to append this retrieval to its audit log",
                },
            },
        },
    ),
    Tool(
        name="get_knowledge_entry",
        description=(
            "Get a single knowledge entry's full record by integer ID. "
            "Includes `last_relevant_at` so you can see when the entry "
            "was last referenced as authoritative."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project entries get` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Knowledge entry ID",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="list_knowledge_entries",
        description=(
            "List knowledge entries for the project with rich filters, "
            "sort, and pagination. Filters: entry_type, claim_class "
            "(evidence|claim|note), freshness_class (current|aging|stale|"
            "superseded), dismissed, session_id. Sort: created_at_desc "
            "(default), last_relevant_at_desc, confidence_desc."
            "\n\nPagination: pass `page` for OFFSET-style fetching "
            "(simple but may skip/duplicate rows under concurrent "
            "writes), or pass `cursor` (the `id` of the last entry from "
            "the previous response) for snapshot-stable keyset "
            "pagination. Cursor is only valid with the default sort. "
            "Response includes `next_cursor` when more results are "
            "available via cursor pagination."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project entries` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entry_type": {
                    "type": "string",
                    "description": "Filter by type: decision, pattern, discovery, convention, bug, dependency",
                },
                "claim_class": {
                    "type": "string",
                    "description": "Filter by claim_class: evidence, claim, note",
                },
                "freshness_class": {
                    "type": "string",
                    "description": "Filter by freshness_class: current, aging, stale, superseded",
                },
                "dismissed": {
                    "type": "boolean",
                    "description": "Filter by dismissed status",
                },
                "session_id": {
                    "type": "string",
                    "description": "Filter to entries created in a specific session",
                },
                "sort": {
                    "type": "string",
                    "description": "Sort order: created_at_desc (default), last_relevant_at_desc, confidence_desc",
                },
                "page": {
                    "type": "integer",
                    "description": "Page number, 1-indexed (default 1). Ignored when `cursor` is set.",
                },
                "cursor": {
                    "type": "integer",
                    "description": "Keyset pagination cursor — pass the `id` of the last entry from the previous response. Snapshot-stable. Default sort only.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Page size (default 50, max 200)",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
        },
    ),
    Tool(
        name="get_wiki_page",
        description=(
            "Get a single wiki page by slug, including its content and "
            "backlinks. Use after `list_wiki_pages` to read a page in full."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project page get` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Page slug (e.g. 'architecture', 'concept/auth-flow')",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
                "audit_session_id": {
                    "type": "string",
                    "description": "Optional current session id to append this retrieval to its audit log",
                },
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="get_knowledge_health",
        description=(
            "Get the project's knowledge base health record: pending, "
            "compiled, dismissed counts; word count; stale and "
            "low-confidence counts; and prioritised recommendations."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project health` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
        },
    ),
    Tool(
        name="get_context_section",
        description=(
            "Return one section of the project context document by slug "
            "instead of fetching the full document. Slugs match the "
            "lowercase, non-alphanumeric-collapsed form of `## Heading` "
            "titles. On miss, the error includes available_slugs."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project context section` or any other sfs CLI command. This "
            "tool connects directly to the API and is more reliable than "
            "shelling out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Section slug (e.g. 'architecture', 'team_workflow')",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
                "audit_session_id": {
                    "type": "string",
                    "description": "Optional current session id to append this retrieval to its audit log",
                },
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="get_session_retrieval_log",
        description=(
            "Return the retrieval audit log for a session. Server-side logs are "
            "recorded automatically when start_ticket created a retrieval_audit_id; "
            "offline MCP clients can still append local fallback rows with "
            "`audit_session_id` or SESSIONFS_SESSION_ID/SFS_SESSION_ID."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session id whose retrieval log should be returned",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="get_session_provenance",
        description=(
            "Return the instruction provenance for a session: which rules "
            "version governed it (rules_version, rules_hash, rules_source) "
            "and which artifacts were injected into prompt context "
            "(instruction_artifacts). Useful for debugging stale-rule "
            "regressions or replaying a session under the same governance "
            "state."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs session provenance` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling out."
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
        name="compile_knowledge_base",
        description=(
            "Trigger a compile pass for the project's knowledge base. "
            "HEAVY + MUTATING: this promotes pending claims into the "
            "project context document, runs decay + retention + "
            "auto-supersession passes, regenerates section pages, "
            "refreshes concept pages, and may invoke an LLM if one is "
            "configured. Concurrent calls are serialized with a "
            "row-level lock on the project. Returns a compact summary: "
            "entries_compiled, context_words_before, context_words_after, "
            "section_pages_updated, concept_pages_updated, compiled_at. "
            "Full context_before/context_after diff is omitted from the "
            "MCP response to keep agent context small — fetch via "
            "GET /api/v1/projects/{id}/compilations (the most recent "
            "entry carries the full before/after) if you need it."
            "\n\nCall sparingly. The dashboard's compile button is the "
            "primary trigger; an MCP-side compile is for after a "
            "deliberate writeback when a human isn't watching the UI. "
            "There is no automatic background scheduler — pending "
            "claims wait until somebody compiles."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project compile` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than shelling out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
        },
    ),
    # ── v0.10.1 Phase 4 — Agent Personas + Ticketing ──
    Tool(
        name="list_personas",
        description=(
            "List active agent personas for this project. Returns each "
            "persona's id, name, role, and specializations.\n\n"
            "Use this to discover which agents exist in this project — "
            "personas are portable AI roles (atlas/prism/scribe/etc.) "
            "shared by humans and AI agents.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs persona list` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
        },
    ),
    Tool(
        name="get_persona",
        description=(
            "Load a persona's full context. Returns the persona's role, "
            "specializations, and full markdown content. Use this when you "
            "want to work as a specific agent but aren't starting from a "
            "ticket — for ticket work, use `start_ticket` which loads the "
            "persona automatically.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs persona show` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Persona name (e.g. 'atlas')"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
                "audit_session_id": {
                    "type": "string",
                    "description": "Optional current session id to append this retrieval to its audit log",
                },
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="list_tickets",
        description=(
            "List tickets for this project. Filter by `assigned_to` "
            "(persona name), `status`, or `priority`. Returns each ticket's "
            "id, title, assigned persona, status, and priority.\n\n"
            "Status values: suggested, open, in_progress, blocked, review, "
            "done, cancelled.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket list` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "assigned_to": {"type": "string", "description": "Filter by persona name"},
                "status": {"type": "string", "description": "Filter by status"},
                "priority": {"type": "string", "description": "Filter by priority"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
        },
    ),
    Tool(
        name="get_ticket",
        description=(
            "Get full ticket details including description, acceptance "
            "criteria, context references, file references, dependency "
            "status, and comments.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket show` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "Ticket id (e.g. 'tk_...')"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["ticket_id"],
        },
    ),
    Tool(
        name="start_ticket",
        description=(
            "Start working on a ticket. Returns the compiled persona + "
            "ticket context (markdown) the agent should consume.\n\n"
            "Automatically loads the assigned persona, ticket description, "
            "acceptance criteria, file refs, explicit KB claims, recent "
            "comments, and completion notes from already-done dependencies. "
            "The persona is loaded automatically — you don't need to call "
            "get_persona separately.\n\n"
            "Also writes ~/.sessionfs/active_ticket.json so the daemon "
            "tags every session captured during this work with the persona "
            "+ ticket provenance.\n\n"
            "Returns 409 if the ticket is already in_progress (concurrent "
            "start). Pass `force=true` to recover a stuck `blocked` "
            "ticket. Successful starts return `ticket.lease_epoch`; pass that "
            "epoch to complete_ticket/add_ticket_comment/resolve_ticket to "
            "fence stale daemons.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket start` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "Ticket id"},
                "force": {"type": "boolean", "description": "Recover a blocked ticket", "default": False},
                "tool": {"type": "string", "description": "Target tool for token budget (claude-code/codex/gemini/copilot/cursor/...)", "default": "generic"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["ticket_id"],
        },
    ),
    Tool(
        name="create_ticket",
        description=(
            "Create a new ticket. Can be created by a human or an agent "
            "working on another ticket.\n\n"
            "Agent-created tickets (source='agent') default to 'suggested' "
            "status and require:\n"
            "- acceptance_criteria (at least one)\n"
            "- description >= 20 characters\n"
            "- max 3 per session_id\n\n"
            "Human-created tickets (source='human', default) land as "
            "'open' immediately.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket create` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string", "default": ""},
                "assigned_to": {"type": "string", "description": "Persona name"},
                "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"], "default": "medium"},
                "context_refs": {"type": "array", "items": {"type": "string"}, "default": []},
                "file_refs": {"type": "array", "items": {"type": "string"}, "default": []},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "default": []},
                "depends_on": {"type": "array", "items": {"type": "string"}, "default": []},
                "source": {"type": "string", "enum": ["human", "agent"], "default": "human"},
                "created_by_session_id": {"type": "string"},
                "created_by_persona": {"type": "string"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="complete_ticket",
        description=(
            "Mark a ticket as complete. Provide `notes` on what was done "
            "and `changed_files` (list of paths). The ticket moves to "
            "'review' status; the reporter sees it in their next session.\n\n"
            "Knowledge entries extracted from the session are tagged with "
            "the ticket_id for traceability. Removes "
            "~/.sessionfs/active_ticket.json so subsequent sessions are no "
            "longer attributed to this ticket.\n\n"
            "`lease_epoch` is optional for backward compatibility. Passing "
            "the epoch returned by start_ticket enables coordinated stale-worker "
            "fencing; omitting it is unfenced and should only be used by legacy "
            "or single-worker callers.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket complete` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "notes": {"type": "string", "description": "Completion notes — what was done, key decisions, follow-ups"},
                "changed_files": {"type": "array", "items": {"type": "string"}, "default": []},
                "knowledge_entry_ids": {"type": "array", "items": {"type": "string"}, "default": []},
                "lease_epoch": {"type": "integer", "description": "Optional stale-writer fence from start_ticket"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["ticket_id", "notes"],
        },
    ),
    Tool(
        name="add_ticket_comment",
        description=(
            "Add a comment to a ticket. Use for progress updates, "
            "questions, blockers, or findings during work. Optionally pass "
            "`author_persona` to attribute the comment to a specific "
            "persona role. `lease_epoch` is optional for backward compatibility; "
            "when supplied, the comment insert is atomically rejected if the "
            "ticket lease changed.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket comment` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "content": {"type": "string"},
                "author_persona": {"type": "string"},
                "session_id": {"type": "string"},
                "lease_epoch": {"type": "integer", "description": "Optional stale-writer fence from start_ticket"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["ticket_id", "content"],
        },
    ),
    # ── v0.10.1 Phase 8 — Agent workflow MCP tools ──
    Tool(
        name="create_persona",
        description=(
            "Create a new agent persona in this project. Persona names "
            "must be ASCII (1-50 chars: letters, digits, dash, underscore). "
            "Use this when an agent decides a new role is needed (e.g. "
            "after recognizing a gap in the team's expertise).\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs persona create` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Persona name (ASCII, 1-50 chars)"},
                "role": {"type": "string", "description": "Short role description (≤100 chars)"},
                "content": {"type": "string", "description": "Full persona content (markdown)", "default": ""},
                "specializations": {"type": "array", "items": {"type": "string"}, "default": []},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["name", "role"],
        },
    ),
    Tool(
        name="assign_persona",
        description=(
            "Assign a persona to a ticket (sets `ticket.assigned_to`). "
            "Use when an agent triages an unassigned ticket or wants to "
            "hand work off to a different persona. The ticket FSM is "
            "unaffected — this is purely a routing update.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket assign` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "persona_name": {"type": "string", "description": "Persona to assign"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["ticket_id", "persona_name"],
        },
    ),
    Tool(
        name="assume_persona",
        description=(
            "Declare that you are working AS a persona without starting a "
            "ticket. Writes the local provenance bundle so the daemon tags "
            "every captured session with the persona name. Useful for "
            "ad-hoc agent work that isn't tied to a specific ticket "
            "(exploration, code review, etc).\n\n"
            "Pairs with `forget_persona` which clears the bundle so "
            "subsequent sessions aren't attributed.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs persona assume` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Persona name to assume"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="resolve_ticket",
        description=(
            "Mark a ticket as resolved — moves from `review` to `done`. "
            "Triggers the dependency-enrichment pass that propagates "
            "completion notes + KB refs to every dependent ticket and "
            "auto-unblocks any that were waiting on this one.\n\n"
            "Atomic state transition with rowcount-1 guard — concurrent "
            "resolves cannot duplicate enrichment.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket resolve` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "lease_epoch": {"type": "integer", "description": "Optional stale-writer fence from start_ticket"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["ticket_id"],
        },
    ),
    Tool(
        name="escalate_ticket",
        description=(
            "Bump a ticket's priority one level (low → medium → high → "
            "critical). No-op if already critical. Optionally posts an "
            "escalation comment so the audit trail captures who/why.\n\n"
            "Use when work needs more urgency than originally rated. The "
            "ticket FSM state is unaffected.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket escalate` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "reason": {"type": "string", "description": "Optional rationale — recorded as a comment on the ticket"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["ticket_id"],
        },
    ),
    Tool(
        name="forget_persona",
        description=(
            "Clear the local persona-only provenance bundle written by "
            "`assume_persona`. Subsequent sessions will no longer be "
            "tagged with the persona name.\n\n"
            "Safe to call when no bundle exists — returns gracefully. "
            "Does NOT clear ticket-tagged bundles (use `complete_ticket` "
            "for that, which checks ticket ownership).\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs persona forget` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    # ── v0.10.2 — AgentRun tracking tools ──
    Tool(
        name="create_agent_run",
        description=(
            "Create a tracked execution record for an agent run. This is "
            "a TRACKING + ENFORCEMENT tool — it records that a persona "
            "ran (manually or via CI), captures findings + severity, and "
            "evaluates a fail_on policy at completion. It does NOT spawn "
            "the model runtime; the caller is responsible for executing "
            "the actual agent work and submitting results via "
            "`complete_agent_run`.\n\n"
            "Pass `start_now=true` to chain create + start as a single "
            "MCP call (two HTTP requests under the hood — POST /create "
            "then POST /start). The response includes compiled context "
            "from the start call. If the start step fails after create "
            "succeeded, the queued run is returned with `start_error`; "
            "callers can retry `start_agent_run` separately.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs agent run` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "persona_name": {"type": "string"},
                "tool": {"type": "string", "default": "generic"},
                "trigger_source": {
                    "type": "string",
                    "enum": ["manual", "ci", "webhook", "scheduled", "mcp", "api"],
                    "default": "mcp",
                },
                "ticket_id": {"type": "string"},
                "trigger_ref": {"type": "string"},
                "ci_provider": {"type": "string"},
                "ci_run_url": {"type": "string"},
                "fail_on": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high", "critical"],
                },
                "triggered_by_persona": {"type": "string"},
                "start_now": {"type": "boolean", "default": False},
                "git_remote": {"type": "string"},
            },
            "required": ["persona_name"],
        },
    ),
    Tool(
        name="complete_agent_run",
        description=(
            "Record the result of an agent run. Submit `severity` of "
            "findings and a list of structured findings; the server "
            "evaluates the configured `fail_on` policy and stores "
            "`policy_result` (pass/fail) + `exit_code` (0/1).\n\n"
            "Severity hierarchy (low → critical). `severity=none` never "
            "trips a threshold. Caller-submitted `status=errored` is "
            "preserved regardless of policy.\n\n"
            "Atomic transition — only running/queued runs can be "
            "completed. Returns the full updated row.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs agent complete` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["passed", "failed", "errored"],
                    "default": "passed",
                },
                "result_summary": {"type": "string"},
                "severity": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high", "critical"],
                    "default": "none",
                },
                "findings": {
                    "type": "array",
                    "items": {"type": "object"},
                    "default": [],
                },
                "session_id": {"type": "string"},
                "git_remote": {"type": "string"},
            },
            "required": ["run_id"],
        },
    ),
    Tool(
        name="list_agent_runs",
        description=(
            "List recent agent runs in the project. Filter by persona, "
            "status, trigger_source, or ticket_id. Sorted by created_at "
            "descending; default limit 50, max 200.\n\n"
            "Useful for: 'has atlas already reviewed this PR?', 'show me "
            "all failed sentinel runs from last week', 'what runs touched "
            "this ticket?'.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs agent list` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "persona_name": {"type": "string"},
                "status": {"type": "string"},
                "trigger_source": {"type": "string"},
                "ticket_id": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
                "git_remote": {"type": "string"},
            },
        },
    ),
    Tool(
        name="dismiss_knowledge_entry",
        description=(
            "Dismiss a knowledge entry that's wrong, stale, or no longer "
            "useful. WRITE + AUDITED: records who dismissed (user_id), "
            "when (timestamp), and the reason on the entry. Dismissed "
            "entries are excluded from compile and don't reach the "
            "project context document. Idempotent — re-dismissing is a "
            "200 no-op; supplying a new reason on re-dismiss updates the "
            "reason but preserves the original timestamp + dismisser."
            "\n\nSet `undismiss=true` to reverse a dismissal (clears the "
            "audit row so the entry re-enters compile)."
            "\n\nThe response includes the persisted audit triple — "
            "`dismissed_at`, `dismissed_by`, `dismissed_reason` — so the "
            "agent can confirm what was recorded and surface it to the "
            "user (\"dismissed by X on Y because Z\")."
            "\n\nWhen to use: an agent reads an entry via "
            "`get_knowledge_entry` or `search_project_knowledge`, "
            "discovers it's wrong (e.g. references a removed file or "
            "decision that's been reversed), and the user confirms it "
            "should be retired."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project entries dismiss` or any other sfs CLI command. "
            "This tool connects directly to the API and is more reliable "
            "than shelling out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Knowledge entry ID",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional rationale (max 500 chars). Why was this dismissed? Helps reviewers later.",
                },
                "undismiss": {
                    "type": "boolean",
                    "description": "Set true to reverse a dismissal. Default: false (= dismiss).",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["id"],
        },
    ),
    # ── v0.10.2 — Ticket approval + session ops ──
    Tool(
        name="approve_ticket",
        description=(
            "Approve an agent-suggested ticket: moves status from "
            "`suggested` → `open` so it can be assigned and started. Use "
            "this on tickets created via `create_ticket` by a non-trusted "
            "agent. The ticket must currently be in `suggested` status — "
            "any other state returns a 409 conflict.\n\n"
            "Use `dismiss_knowledge_entry` for KB entries, NOT this tool — "
            "this is for tickets only.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket approve` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["ticket_id"],
        },
    ),
    Tool(
        name="checkpoint_session",
        description=(
            "Create a named checkpoint snapshot of a local session's "
            "current state (manifest + messages). Lives on disk under "
            "`~/.sessionfs/sessions/<id>.sfs/checkpoints/<name>/` and lets "
            "you later `fork_session(from_checkpoint=<name>)` to branch "
            "from that point.\n\n"
            "Names: 1-100 chars, must start with alphanumeric; allowed "
            "chars are letters/digits/'.'/'_'/'-'. Re-using an existing "
            "name returns an error.\n\n"
            "LOCAL-ONLY: operates on `~/.sessionfs`. Does not upload "
            "anything to the cloud.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs checkpoint` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Full session id or unique prefix"},
                "name": {"type": "string", "description": "Checkpoint name (see naming rules)"},
            },
            "required": ["session_id", "name"],
        },
    ),
    Tool(
        name="list_checkpoints",
        description=(
            "List checkpoints stored for a session, oldest first. Each "
            "entry returns `name`, `created_at`, `message_count`, and the "
            "absolute path on disk. Use this before `fork_session` to "
            "discover what branch points exist.\n\n"
            "Returns an empty list if the session has no checkpoints.\n\n"
            "LOCAL-ONLY: operates on `~/.sessionfs`."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Full session id or unique prefix"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="fork_session",
        description=(
            "Fork a session (or a named checkpoint of it) into a new "
            "independent session. The new session inherits the source's "
            "messages + workspace + tools but gets a fresh session id and "
            "the `name` you supply as its title. The new manifest records "
            "`parent_session_id` (and `forked_from_checkpoint` when "
            "applicable) so lineage stays introspectable.\n\n"
            "Pass `from_checkpoint` to fork from a snapshot created by "
            "`checkpoint_session`; omit it to fork from the live session "
            "head. The source session is unmodified.\n\n"
            "LOCAL-ONLY: operates on `~/.sessionfs`. The fork is not "
            "automatically pushed to the cloud — run `sfs push <new_id>` "
            "explicitly if you want that.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs fork` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Full session id or unique prefix"},
                "name": {"type": "string", "description": "Title for the forked session (non-empty)"},
                "from_checkpoint": {
                    "type": "string",
                    "description": "Optional checkpoint name to fork from (see list_checkpoints)",
                },
            },
            "required": ["session_id", "name"],
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
            await _record_retrieval_for_tool(name, arguments, result)
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
        elif name == "get_rules":
            result = await _handle_get_rules(arguments)
            return [TextContent(type="text", text=result if isinstance(result, str) else json.dumps(result, indent=2, default=str))]
        elif name == "get_compiled_rules":
            result = await _handle_get_compiled_rules(arguments)
            return [TextContent(type="text", text=result if isinstance(result, str) else json.dumps(result, indent=2, default=str))]
        elif name == "get_knowledge_entry":
            result = await _handle_get_knowledge_entry(arguments)
        elif name == "list_knowledge_entries":
            result = await _handle_list_knowledge_entries(arguments)
        elif name == "get_wiki_page":
            result = await _handle_get_wiki_page(arguments)
        elif name == "get_knowledge_health":
            result = await _handle_get_knowledge_health(arguments)
        elif name == "get_context_section":
            result = await _handle_get_context_section(arguments)
        elif name == "get_session_provenance":
            result = await _handle_get_session_provenance(arguments)
        elif name == "get_session_retrieval_log":
            result = await _handle_get_session_retrieval_log(arguments)
        elif name == "compile_knowledge_base":
            result = await _handle_compile_knowledge_base(arguments)
        elif name == "list_personas":
            result = await _handle_list_personas(arguments)
        elif name == "get_persona":
            result = await _handle_get_persona(arguments)
        elif name == "list_tickets":
            result = await _handle_list_tickets(arguments)
        elif name == "get_ticket":
            result = await _handle_get_ticket(arguments)
        elif name == "start_ticket":
            result = await _handle_start_ticket(arguments)
        elif name == "create_ticket":
            result = await _handle_create_ticket(arguments)
        elif name == "complete_ticket":
            result = await _handle_complete_ticket(arguments)
        elif name == "add_ticket_comment":
            result = await _handle_add_ticket_comment(arguments)
        elif name == "create_persona":
            result = await _handle_create_persona(arguments)
        elif name == "assign_persona":
            result = await _handle_assign_persona(arguments)
        elif name == "assume_persona":
            result = await _handle_assume_persona(arguments)
        elif name == "forget_persona":
            result = _handle_forget_persona(arguments)
        elif name == "resolve_ticket":
            result = await _handle_resolve_ticket(arguments)
        elif name == "escalate_ticket":
            result = await _handle_escalate_ticket(arguments)
        elif name == "create_agent_run":
            result = await _handle_create_agent_run(arguments)
        elif name == "complete_agent_run":
            result = await _handle_complete_agent_run(arguments)
        elif name == "list_agent_runs":
            result = await _handle_list_agent_runs(arguments)
        elif name == "dismiss_knowledge_entry":
            result = await _handle_dismiss_knowledge_entry(arguments)
        elif name == "approve_ticket":
            result = await _handle_approve_ticket(arguments)
        elif name == "checkpoint_session":
            result = _handle_checkpoint_session(arguments)
        elif name == "list_checkpoints":
            result = _handle_list_checkpoints(arguments)
        elif name == "fork_session":
            result = _handle_fork_session(arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        logger.error("Tool %s failed: %s", name, exc, exc_info=True)
        result = {"error": str(exc)}

    await _record_retrieval_for_tool(name, arguments, result)
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def _record_retrieval_for_tool(name: str, arguments: dict, result: Any) -> None:
    from sessionfs.retrieval_audit import (
        RETRIEVAL_TOOLS,
        audit_context_id,
        audit_session_id,
        collect_returned_refs,
        record_retrieval,
        sanitize_arguments,
    )

    if name not in RETRIEVAL_TOOLS:
        return
    context_id = audit_context_id(arguments)
    if context_id:
        try:
            api_url, api_key, project_id = await _resolve_project_id(
                arguments.get("git_remote", "")
            )
            import httpx

            payload = {
                "context_id": context_id,
                "session_id": audit_session_id(arguments),
                "tool_name": name,
                "arguments": sanitize_arguments(arguments),
                "returned_refs": collect_returned_refs(result),
                "source": "mcp",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{api_url}/api/v1/projects/{project_id}/retrieval-audit-events",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            if resp.status_code < 400:
                return
            logger.warning(
                "Server retrieval audit failed for %s: %s %s",
                name,
                resp.status_code,
                resp.text[:200],
            )
        except Exception as exc:
            logger.warning("Server retrieval audit failed for %s: %s", name, exc)
    try:
        record_retrieval(tool_name=name, args=arguments, result=result)
    except OSError as exc:
        logger.warning("Failed to record retrieval audit for %s: %s", name, exc)


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

        # Enrich with recent knowledge entries (active claims only)
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
                    # Filter to active claims only
                    entries = [
                        e for e in entries
                        if e.get("claim_class", "claim") == "claim"
                        and e.get("freshness_class", "current") in ("current", "aging")
                        and not e.get("superseded_by")
                        and not e.get("dismissed", False)
                    ]
                    if entries:
                        activity = "\n\n---\n## Recent Session Activity\n"
                        activity += "*(Active claims from recent sessions. Not yet compiled into the main document.)*\n\n"
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
    include_stale = args.get("include_stale", False)
    git_remote = args.get("git_remote", "")

    # Match the route-side 3-char floor (v0.9.9.10) so the pg_trgm
    # index on knowledge_entries.content can serve every accepted
    # query without falling back to a sequential scan. Reject early
    # so agents get a clean message instead of an HTTP 422 round-trip.
    if isinstance(query, str) and len(query.strip()) < 3:
        return (
            "search_project_knowledge requires a query of 3+ characters "
            "(strip whitespace). Shorter queries fall back to a "
            "sequential scan in production — narrow your search."
        )

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
        if args.get("_used_in_answer"):
            params += "&used_in_answer=true"

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

        # Filter to active claims by default unless include_stale
        if not include_stale:
            entries = [
                e for e in entries
                if e.get("claim_class", "claim") == "claim"
                and e.get("freshness_class", "current") in ("current", "aging")
                and not e.get("superseded_by")
                and not e.get("dismissed", False)
            ]

        lines = [f"# Knowledge Search: \"{query}\"", f"_{len(entries)} result(s)_\n"]
        for e in entries:
            etype = e.get("entry_type", "unknown")
            badge = type_badges.get(etype, "\u2022")
            confidence = e.get("confidence", 0)
            entry_id = e.get("id")
            created = e.get("created_at", "")[:10]
            session_id = e.get("session_id", "unknown")
            freshness = e.get("freshness_class", "current")
            claim_class = e.get("claim_class", "claim")

            freshness_tag = f" [{freshness}]" if freshness != "current" else ""
            class_tag = f" ({claim_class})" if claim_class != "claim" else ""

            id_tag = f" KB #{entry_id}" if entry_id is not None else ""
            lines.append(f"### {badge} [{etype.upper()}]{id_tag} (confidence: {confidence:.0%}){freshness_tag}{class_tag}")
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
    entity_ref = args.get("entity_ref")
    entity_type = args.get("entity_type")
    force_claim = args.get("force_claim", False)
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
        if entity_ref:
            payload["entity_ref"] = entity_ref
        if entity_type:
            payload["entity_type"] = entity_type
        if force_claim:
            payload["force_claim"] = True

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{api_url}/api/v1/projects/{project_id}/entries/add",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code == 201:
            data = resp.json()
            claim_class = data.get("claim_class", "note")
            tip = data.get("tip")
            msg = f"Knowledge entry added (id: {data['id']}, type: {entry_type}, class: {claim_class})."
            if tip:
                msg += f"\nTip: {tip}"
            return msg
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

    # Search knowledge entries for the question. Pass used_in_answer=true
    # so the server increments used_in_answer_count + updates last_relevant_at
    # on matched entries (strong relevance signal).
    search_result = await _handle_search_knowledge({"query": question, "limit": 15, "git_remote": git_remote, "_used_in_answer": True})
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


async def _handle_get_rules(args: dict) -> dict:
    """Return canonical rules + compilation config for the repo."""
    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/rules",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    data = resp.json()
    # Strip the ETag header echo — not useful for agents.
    data.pop("etag", None)
    return data


async def _handle_get_compiled_rules(args: dict) -> dict:
    """Return the compiled output for a requested tool (or all tools)."""
    git_remote = args.get("git_remote", "")
    tool = args.get("tool")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    payload: dict = {}
    if tool:
        payload["tools"] = [tool]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/rules/compile",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    data = resp.json()
    # Filter by tool if asked — compile may return all enabled outputs.
    if tool:
        filtered = [o for o in data.get("outputs", []) if o.get("tool") == tool]
        return {
            "version": data.get("version"),
            "aggregate_hash": data.get("aggregate_hash"),
            "outputs": filtered,
        }
    return data


# ---------------------------------------------------------------------------
# Tier A read-side handlers (v0.9.9.6)
# ---------------------------------------------------------------------------


async def _handle_get_knowledge_entry(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/entries/{entry_id}."""
    entry_id = args.get("id")
    if entry_id is None:
        return {"error": "id is required"}
    try:
        entry_id = int(entry_id)
    except (TypeError, ValueError):
        return {"error": "id must be an integer"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/entries/{entry_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Entry {entry_id} not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_list_knowledge_entries(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/entries with rich filters."""
    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    # Build query params — only forward what the caller actually set so we
    # don't pin defaults the API would otherwise pick.
    params: dict[str, str] = {}
    if args.get("entry_type"):
        params["type"] = str(args["entry_type"])
    if args.get("claim_class"):
        params["claim_class"] = str(args["claim_class"])
    if args.get("freshness_class"):
        params["freshness_class"] = str(args["freshness_class"])
    if args.get("dismissed") is not None:
        params["dismissed"] = "true" if args["dismissed"] else "false"
    if args.get("session_id"):
        params["session_id"] = str(args["session_id"])
    if args.get("sort"):
        params["sort"] = str(args["sort"])
    if args.get("page") is not None:
        params["page"] = str(int(args["page"]))
    if args.get("cursor") is not None:
        params["cursor"] = str(int(args["cursor"]))
    if args.get("limit") is not None:
        params["limit"] = str(int(args["limit"]))

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/entries",
            params=params,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    entries = resp.json()
    # Surface the keyset cursor so callers don't have to read response
    # headers themselves. Empty when no more results are available or
    # when OFFSET pagination was used.
    next_cursor_hdr = resp.headers.get("X-Next-Cursor")
    payload: dict = {
        "entries": entries,
        "count": len(entries) if isinstance(entries, list) else 0,
        "filters": {
            k: v
            for k, v in params.items()
            if k not in ("page", "limit", "sort", "cursor")
        },
        "page": int(args.get("page", 1)),
        "limit": int(args.get("limit", 50)),
        "sort": args.get("sort", "created_at_desc"),
    }
    if next_cursor_hdr:
        try:
            payload["next_cursor"] = int(next_cursor_hdr)
        except ValueError:
            pass
    return payload


async def _handle_get_wiki_page(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/pages/{slug}."""
    slug = args.get("slug", "")
    if not slug:
        return {"error": "slug is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/pages/{slug}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Page '{slug}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_get_knowledge_health(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/health."""
    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/health",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_get_context_section(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/context/sections/{slug}."""
    slug = args.get("slug", "")
    if not slug:
        return {"error": "slug is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/context/sections/{slug}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        # Surface the available slugs so the agent can recover without a
        # second round-trip to list_wiki_pages or get_project_context.
        # The server wraps HTTPException detail dicts in a global error
        # envelope: {"error": {"code", "message", "details": {...}}}.
        # Older deployments (or direct 404s) use plain {"detail": {...}}.
        try:
            body = resp.json()
        except ValueError:
            body = {}
        detail: dict = {}
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and isinstance(err.get("details"), dict):
                detail = err["details"]
            elif isinstance(body.get("detail"), dict):
                detail = body["detail"]
        return {
            "error": detail.get("error", f"Section '{slug}' not found"),
            "available_slugs": detail.get("available_slugs", []),
        }
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_get_session_provenance(args: dict) -> dict:
    """Wrap GET /api/v1/sessions/{session_id}/provenance."""
    session_id = args.get("session_id", "")
    if not session_id:
        return {"error": "session_id is required"}

    config = load_config()
    if not config.sync.api_key:
        return {"error": "Not authenticated. Run 'sfs auth login' first."}

    import httpx
    api_url = config.sync.api_url.rstrip("/")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/sessions/{session_id}/provenance",
            headers={"Authorization": f"Bearer {config.sync.api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Session {session_id} not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_get_session_retrieval_log(args: dict) -> dict:
    session_id = args.get("session_id", "")
    if not session_id:
        return {"error": "session_id is required"}
    from sessionfs.retrieval_audit import is_safe_audit_id, read_retrieval_log

    if not is_safe_audit_id(session_id):
        return {"error": "Invalid session_id"}
    config = load_config()
    if config.sync.api_key:
        import httpx

        api_url = config.sync.api_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{api_url}/api/v1/sessions/{session_id}/retrieval-log",
                    headers={"Authorization": f"Bearer {config.sync.api_key}"},
                )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code not in {404, 405}:
                logger.warning(
                    "Server retrieval log lookup failed: %s %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as exc:
            logger.warning("Server retrieval log lookup failed: %s", exc)
    # Local-fallback shape MUST match the server's RetrievalAuditLog
    # Response so agents see one stable schema regardless of which path
    # fires. Local rows are JSONL records from retrieval_audit.record_
    # retrieval — they carry timestamp / tool_name / arguments /
    # returned_refs. We lift them into the same shape as the server's
    # RetrievalAuditEventResponse with id=null / context_id="" /
    # project_id="" / session_id=session_id / source="local" /
    # caller_user_id=None so the response key set is identical.
    # (Codex KB #395 Finding B fix.)
    entries = read_retrieval_log(session_id)
    events = [
        {
            "id": None,
            "context_id": "",
            "project_id": "",
            "session_id": session_id,
            "tool_name": e.get("tool_name", ""),
            "arguments": e.get("arguments") or {},
            "returned_refs": e.get("returned_refs") or {},
            "source": "local",
            "caller_user_id": None,
            "created_at": e.get("timestamp"),
        }
        for e in entries
    ]
    return {
        "session_id": session_id,
        "retrieval_audit_id": "",
        "events": events,
        "count": len(events),
    }


async def _handle_compile_knowledge_base(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/compile.

    Triggers a compile pass — promotes pending claims into the project
    context document and refreshes section + concept pages. Returns the
    structured CompilationResponse so the agent can surface a "what
    changed" diff to the user.
    """
    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/compile",
            json={},
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    # Strip the full context_before/context_after diff from the MCP
    # response. The structured counters are sufficient for agent
    # decision-making and the full doc bodies (often thousands of
    # words) bloat agent context. Dashboard callers hit the route
    # directly and still receive the full payload.
    payload = resp.json()
    if isinstance(payload, dict):
        payload.pop("context_before", None)
        payload.pop("context_after", None)
    return payload


async def _handle_list_personas(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/personas."""
    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/personas",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return {"personas": resp.json()}


async def _handle_get_persona(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/personas/{name}."""
    name = args.get("name", "")
    if not name:
        return {"error": "name is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/personas/{name}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Persona '{name}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_list_tickets(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/tickets."""
    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    params: dict[str, str] = {}
    for key in ("assigned_to", "status", "priority"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            params[key] = val.strip()

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/tickets",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return {"tickets": resp.json()}


async def _handle_get_ticket(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/tickets/{ticket_id}."""
    ticket_id = args.get("ticket_id", "")
    if not ticket_id:
        return {"error": "ticket_id is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Ticket '{ticket_id}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_start_ticket(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/tickets/{ticket_id}/start.

    Returns the compiled persona + ticket context and writes the local
    provenance bundle so the daemon tags subsequent sessions with the
    persona + ticket.
    """
    ticket_id = args.get("ticket_id", "")
    if not ticket_id:
        return {"error": "ticket_id is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    params: dict[str, str] = {}
    if args.get("force"):
        params["force"] = "true"
    tool = args.get("tool")
    if isinstance(tool, str) and tool.strip():
        params["tool"] = tool.strip()

    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}/start",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
    if resp.status_code == 404:
        return {"error": f"Ticket '{ticket_id}' not found"}
    if resp.status_code == 409:
        return {"error": "Ticket already started — pass force=true to recover a blocked ticket"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}

    payload = resp.json()
    ticket = payload.get("ticket", {}) if isinstance(payload, dict) else {}
    persona_name = ticket.get("assigned_to")
    lease_epoch = ticket.get("lease_epoch")
    retrieval_audit_id = payload.get("retrieval_audit_id") if isinstance(payload, dict) else None

    from sessionfs.active_ticket import bundle_path, write_bundle
    bundle_ok = write_bundle(
        ticket_id=ticket_id,
        persona_name=persona_name,
        project_id=project_id,
        lease_epoch=lease_epoch if isinstance(lease_epoch, int) else None,
        retrieval_audit_id=(
            retrieval_audit_id if isinstance(retrieval_audit_id, str) else None
        ),
    )
    # KB 339 LOW — surface a structured warning when the bundle write
    # failed so the agent doesn't keep working under the assumption
    # that subsequent sessions are tagged with this ticket/persona.
    if not bundle_ok and isinstance(payload, dict):
        payload["provenance_warning"] = (
            f"Could not write {bundle_path()}. Subsequent sessions will "
            "NOT be tagged with this ticket until the bundle can be "
            f"written. Check permissions on {bundle_path().parent}."
        )

    return payload


async def _handle_create_ticket(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/tickets."""
    title = args.get("title", "")
    if not title:
        return {"error": "title is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict[str, Any] = {"title": title}
    for key in (
        "description", "assigned_to", "priority", "source",
        "created_by_session_id", "created_by_persona",
    ):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            body[key] = val.strip()
    for key in ("context_refs", "file_refs", "acceptance_criteria", "depends_on"):
        val = args.get(key)
        if isinstance(val, list):
            body[key] = val

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/tickets",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_complete_ticket(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/tickets/{ticket_id}/complete.

    Removes the local provenance bundle so subsequent sessions are no
    longer attributed to this ticket.
    """
    ticket_id = args.get("ticket_id", "")
    if not ticket_id:
        return {"error": "ticket_id is required"}
    notes = args.get("notes", "")
    if not notes:
        return {"error": "notes is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict[str, Any] = {"notes": notes}
    for key in ("changed_files", "knowledge_entry_ids"):
        val = args.get(key)
        if isinstance(val, list):
            body[key] = val
    if args.get("lease_epoch") is not None:
        body["lease_epoch"] = int(args["lease_epoch"])

    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}/complete",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code == 404:
        return {"error": f"Ticket '{ticket_id}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}

    # Only remove the bundle if it points at the ticket we just completed
    # (KB 332 LOW fix). If another tool/session started a different ticket
    # since we started this one, leave its bundle in place so the daemon
    # keeps tagging that ticket's sessions.
    from sessionfs.active_ticket import clear_bundle_if_owned
    clear_bundle_if_owned(ticket_id=ticket_id, project_id=project_id)

    return resp.json()


async def _handle_add_ticket_comment(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/tickets/{ticket_id}/comments."""
    ticket_id = args.get("ticket_id", "")
    if not ticket_id:
        return {"error": "ticket_id is required"}
    content = args.get("content", "")
    if not content:
        return {"error": "content is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict[str, Any] = {"content": content}
    for key in ("author_persona", "session_id"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            body[key] = val.strip()
    if args.get("lease_epoch") is not None:
        body["lease_epoch"] = int(args["lease_epoch"])

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}/comments",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code == 404:
        return {"error": f"Ticket '{ticket_id}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


# ── v0.10.1 Phase 8 — Agent workflow handlers ──


async def _handle_create_persona(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/personas."""
    name = (args.get("name") or "").strip()
    role = (args.get("role") or "").strip()
    if not name:
        return {"error": "name is required"}
    if not role:
        return {"error": "role is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict[str, Any] = {
        "name": name,
        "role": role,
        "content": args.get("content") or "",
        "specializations": list(args.get("specializations") or []),
    }

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/personas",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code == 409:
        return {"error": f"Persona '{name}' already exists"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_assign_persona(args: dict) -> dict:
    """Wrap PUT /api/v1/projects/{project_id}/tickets/{ticket_id} with
    {"assigned_to": persona_name}."""
    ticket_id = (args.get("ticket_id") or "").strip()
    persona_name = (args.get("persona_name") or "").strip()
    if not ticket_id:
        return {"error": "ticket_id is required"}
    if not persona_name:
        return {"error": "persona_name is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"assigned_to": persona_name},
        )
    if resp.status_code == 404:
        return {"error": f"Ticket '{ticket_id}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_assume_persona(args: dict) -> dict:
    """Write a persona-only bundle so the daemon tags subsequent
    sessions with the persona name (without a ticket).
    """
    persona_name = (args.get("name") or "").strip()
    if not persona_name:
        return {"error": "name is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    # Verify the persona exists in this project before pretending to be it.
    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/personas/{persona_name}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Persona '{persona_name}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}

    from sessionfs.active_ticket import bundle_path, write_bundle
    bundle_ok = write_bundle(
        ticket_id=None,
        persona_name=persona_name,
        project_id=project_id,
    )
    result: dict[str, Any] = {
        "persona_name": persona_name,
        "project_id": project_id,
        "bundle_path": str(bundle_path()),
    }
    if not bundle_ok:
        result["provenance_warning"] = (
            f"Could not write {bundle_path()}. Subsequent sessions will "
            f"NOT be tagged with persona '{persona_name}' until the bundle "
            f"can be written. Check permissions on {bundle_path().parent}."
        )
    return result


def _handle_forget_persona(args: dict) -> dict:
    """Clear a persona-only active bundle. Refuses (no-op + error) when
    the bundle is ticket-tagged — that path goes through
    `complete_ticket` so the ownership check fires (KB 332 LOW + KB 352
    MEDIUM). The tool description is the contract; this guards it.
    """
    from sessionfs.active_ticket import bundle_path, clear_bundle, read_bundle
    bundle = read_bundle()
    if isinstance(bundle, dict) and bundle.get("ticket_id"):
        return {
            "cleared": False,
            "bundle_path": str(bundle_path()),
            "error": (
                f"Bundle is tagged to ticket "
                f"{bundle.get('ticket_id')!r}. Use `complete_ticket` "
                "(which enforces the ownership check) instead of "
                "`forget_persona` to retire a ticket attribution."
            ),
        }
    cleared = clear_bundle()
    return {
        "cleared": cleared,
        "bundle_path": str(bundle_path()),
    }


async def _handle_resolve_ticket(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/tickets/{ticket_id}/accept
    (the existing review → done lifecycle endpoint)."""
    ticket_id = (args.get("ticket_id") or "").strip()
    if not ticket_id:
        return {"error": "ticket_id is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    params: dict[str, str] = {}
    if args.get("lease_epoch") is not None:
        params["lease_epoch"] = str(int(args["lease_epoch"]))

    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}/accept"
        if params:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                params=params,
            )
        else:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    if resp.status_code == 404:
        return {"error": f"Ticket '{ticket_id}' not found"}
    if resp.status_code == 409:
        return {"error": f"Ticket '{ticket_id}' could not be resolved: {resp.text}"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


_PRIORITY_ESCALATION = {"low": "medium", "medium": "high", "high": "critical"}


async def _handle_escalate_ticket(args: dict) -> dict:
    """Bump a ticket's priority one level. Optionally post a comment
    capturing the escalation rationale.
    """
    ticket_id = (args.get("ticket_id") or "").strip()
    if not ticket_id:
        return {"error": "ticket_id is required"}
    reason = args.get("reason")
    if isinstance(reason, str):
        reason = reason.strip()

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        # Read current priority.
        get_resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if get_resp.status_code == 404:
            return {"error": f"Ticket '{ticket_id}' not found"}
        if get_resp.status_code >= 400:
            return {"error": f"API error {get_resp.status_code}: {get_resp.text}"}
        current = get_resp.json().get("priority", "medium")
        new_priority = _PRIORITY_ESCALATION.get(current)
        if new_priority is None:
            # KB 352 LOW — match the tool description ("No-op if already
            # critical") and the CLI's exit-0 semantics. Return a
            # structured no-op payload instead of an error envelope.
            return {
                "ticket_id": ticket_id,
                "priority": current,
                "escalated": False,
                "reason": "already at maximum priority",
            }

        # Bump priority via PUT.
        put_resp = await client.put(
            f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"priority": new_priority},
        )
        if put_resp.status_code >= 400:
            return {"error": f"API error {put_resp.status_code}: {put_resp.text}"}

        # Optionally post the rationale as an audit-trail comment. This
        # is non-fatal — if the comment fails the priority bump stands —
        # but capture HTTP failures into `comment_warning` so the caller
        # knows the rationale wasn't recorded (KB 352 LOW).
        comment_warning: str | None = None
        if reason:
            try:
                cresp = await client.post(
                    f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}/comments",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"content": f"Escalated {current} → {new_priority}: {reason}"},
                )
                if cresp.status_code >= 400:
                    comment_warning = (
                        f"Priority bumped, but audit comment failed: "
                        f"HTTP {cresp.status_code}: {cresp.text}"
                    )
                    logger.warning("Escalation comment post returned %s: %s", cresp.status_code, cresp.text)
            except Exception as exc:
                comment_warning = (
                    f"Priority bumped, but audit comment failed: {exc}"
                )
                logger.warning("Escalation comment post failed: %s", exc)

    payload = put_resp.json() if isinstance(put_resp.json(), dict) else {}
    payload["escalated_from"] = current
    payload["escalated_to"] = new_priority
    payload["escalated"] = True
    if comment_warning:
        payload["comment_warning"] = comment_warning
    return payload


# ── v0.10.2 — AgentRun MCP handlers ──


async def _handle_create_agent_run(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/agent-runs.

    When `start_now=True` is passed, also calls /start on the newly
    created run and returns the StartAgentRunResponse with compiled
    context. Otherwise returns the queued run record.
    """
    persona_name = (args.get("persona_name") or "").strip()
    if not persona_name:
        return {"error": "persona_name is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict[str, Any] = {"persona_name": persona_name}
    for key in (
        "tool", "trigger_source", "ticket_id", "trigger_ref",
        "ci_provider", "ci_run_url", "fail_on", "triggered_by_persona",
    ):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            body[key] = val.strip()

    # MCP-only convenience flag — server REST does not honor a body
    # field; we chain a follow-up /start call below.
    start_now = bool(args.get("start_now", False))

    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/agent-runs",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    payload = resp.json()
    run_id = payload.get("id")

    if start_now and run_id:
        async with httpx.AsyncClient(timeout=30) as client:
            start_resp = await client.post(
                f"{api_url}/api/v1/projects/{project_id}/agent-runs/{run_id}/start",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if start_resp.status_code >= 400:
            # Creation succeeded but start failed — return the create
            # payload plus an error so the caller knows it can still
            # call start_agent_run separately.
            return {
                **payload,
                "start_error": f"API error {start_resp.status_code}: {start_resp.text}",
            }
        return start_resp.json()
    return payload


async def _handle_complete_agent_run(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/agent-runs/{run_id}/complete."""
    run_id = (args.get("run_id") or "").strip()
    if not run_id:
        return {"error": "run_id is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict[str, Any] = {
        "status": args.get("status", "passed"),
        "severity": args.get("severity", "none"),
    }
    if args.get("result_summary"):
        body["result_summary"] = args["result_summary"]
    if args.get("session_id"):
        body["session_id"] = args["session_id"]
    findings = args.get("findings")
    if isinstance(findings, list):
        body["findings"] = findings

    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/agent-runs/{run_id}/complete",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code == 404:
        return {"error": f"Agent run '{run_id}' not found"}
    if resp.status_code == 409:
        return {"error": f"Agent run '{run_id}' is already in a terminal state"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_list_agent_runs(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/agent-runs."""
    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    params: dict[str, str] = {}
    for key in ("persona_name", "status", "trigger_source", "ticket_id"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            params[key] = val.strip()
    limit = args.get("limit")
    if isinstance(limit, int) and limit > 0:
        params["limit"] = str(limit)

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/agent-runs",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return {"agent_runs": resp.json()}


async def _handle_dismiss_knowledge_entry(args: dict) -> dict:
    """Wrap PUT /api/v1/projects/{project_id}/entries/{entry_id}.

    Dismisses (or un-dismisses) a knowledge entry and records the audit
    triple (dismissed_at, dismissed_by, dismissed_reason). The reason
    field is length-capped at 500 chars server-side.
    """
    entry_id = args.get("id")
    if not isinstance(entry_id, int) or entry_id <= 0:
        return {"error": "id must be a positive integer"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict = {"dismissed": not bool(args.get("undismiss", False))}
    reason = args.get("reason")
    if isinstance(reason, str) and reason.strip():
        body["reason"] = reason.strip()

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{api_url}/api/v1/projects/{project_id}/entries/{entry_id}",
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_approve_ticket(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/tickets/{ticket_id}/approve."""
    ticket_id = args.get("ticket_id", "")
    if not ticket_id:
        return {"error": "ticket_id is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}/approve",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Ticket '{ticket_id}' not found"}
    if resp.status_code == 409:
        return {"error": f"Ticket '{ticket_id}' is not in `suggested` status: {resp.text}"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


def _handle_checkpoint_session(args: dict) -> dict:
    """Create a named checkpoint of a local session via the shared helper."""
    from sessionfs.session_ops import SessionOpError, create_checkpoint

    session_id = args.get("session_id", "")
    name = args.get("name", "")
    if not session_id:
        return {"error": "session_id is required"}
    if not name:
        return {"error": "name is required"}

    store = _get_store()
    full_id = _resolve_session_id_or_error(store, session_id)
    if isinstance(full_id, dict):
        return full_id
    try:
        return create_checkpoint(store, full_id, name)
    except SessionOpError as exc:
        return {"error": str(exc)}


def _handle_list_checkpoints(args: dict) -> dict:
    from sessionfs.session_ops import SessionOpError, list_checkpoints

    session_id = args.get("session_id", "")
    if not session_id:
        return {"error": "session_id is required"}

    store = _get_store()
    full_id = _resolve_session_id_or_error(store, session_id)
    if isinstance(full_id, dict):
        return full_id
    try:
        return {"session_id": full_id, "checkpoints": list_checkpoints(store, full_id)}
    except SessionOpError as exc:
        return {"error": str(exc)}


def _handle_fork_session(args: dict) -> dict:
    from sessionfs.session_ops import SessionOpError, fork_session

    session_id = args.get("session_id", "")
    name = args.get("name", "")
    from_checkpoint = args.get("from_checkpoint")
    if not session_id:
        return {"error": "session_id is required"}
    if not name:
        return {"error": "name is required"}

    store = _get_store()
    full_id = _resolve_session_id_or_error(store, session_id)
    if isinstance(full_id, dict):
        return full_id
    try:
        return fork_session(store, full_id, name, from_checkpoint=from_checkpoint)
    except SessionOpError as exc:
        return {"error": str(exc)}


def _resolve_session_id_or_error(store, session_id: str):
    """Return the full session id, or an `{"error": ...}` dict.

    Accepts a unique prefix; ambiguous matches → error. Mirrors the
    CLI's `resolve_session_id` semantics without raising SystemExit.
    """
    if store.get_session_dir(session_id):
        return session_id
    matches = store.find_sessions_by_prefix(session_id)
    if not matches:
        return {"error": f"Session '{session_id}' not found"}
    if len(matches) > 1:
        ids = ", ".join(m["session_id"][:12] for m in matches[:5])
        return {"error": f"Session prefix '{session_id}' is ambiguous: {ids}..."}
    return matches[0]["session_id"]


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
