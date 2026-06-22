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
            "and session history and returns the assembled research material."
            "\n\nReturns a JSON object with two fields: `markdown` (the "
            "assembled research material) and `sources_cited` (a list of "
            "typed entities returned by the research step that shaped the "
            "assembled material — `{type: 'kb', id: <int>}` for knowledge "
            "entries, `{type: 'session', id: '<str>'}` for local sessions "
            "matched on the question). ask_project does not call an LLM "
            "answer step today; `sources_cited` tracks the inputs to the "
            "research material, not an LLM answer. Use it for SoD / audit "
            "/ cited-evidence rendering."
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
            "\n\nConfidence handling: omit `confidence` to let the server "
            "default it by source — manual/cli-ask entries (no `session_id` "
            "or `session_id='manual'`) default to 0.7, session-derived "
            "entries default to 1.0. If you want a claim-eligible score, "
            "pass `confidence: 0.9` (or 1.0) explicitly. The default does "
            "NOT clear the 0.8 promotion gate, so a manual entry stays a "
            "note until you raise its confidence."
            "\n\nFull MCP workflow when starting from low confidence: "
            "`add_knowledge` (creates note) → verify the claim against "
            "stronger evidence → `update_entry_confidence` (raise above "
            "0.8) → `promote_entry` (note → claim transition) → "
            "`compile_knowledge_base` (refresh project context)."
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
                    "description": (
                        "Confidence score 0.0–1.0. Optional — when omitted "
                        "the server defaults by source: manual/cli-ask "
                        "entries → 0.7 (note, below the 0.8 promotion gate), "
                        "session-derived entries → 1.0 (auto-promotable). "
                        "Pass an explicit value (e.g. 0.9) to override."
                    ),
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
                "upsert": {
                    "type": "boolean",
                    "description": (
                        "Treat this write as a state-cache roll-forward. "
                        "Requires entity_ref. When True, the server skips "
                        "similarity dedup and auto-dismisses any prior active "
                        "entry sharing the same entity_ref in this project, "
                        "surfacing the dismissed IDs in the response. Use for "
                        "durable append-only agent state (e.g. an LRU map of "
                        "already-classified signals). Default False preserves "
                        "the normal multi-claim entity_ref semantics that "
                        "compile-time _auto_supersede relies on."
                    ),
                },
                "persona_name": {
                    "type": "string",
                    "maxLength": 64,
                    "description": (
                        "Optional persona attribution (e.g. 'scout', 'atlas'). "
                        "When omitted, the handler auto-threads the active "
                        "ticket bundle's persona if its project_id matches the "
                        "resolved project (mirrors update_wiki_page). The "
                        "server validates the name against agent_personas for "
                        "the project — unknown personas return 422. Use this "
                        "so autonomous agents can retrieve their own prior "
                        "findings via GET /entries?persona_name=<name>."
                    ),
                },
                "author_class": {
                    "type": "string",
                    "enum": ["human", "agent"],
                    "description": (
                        "Optional author class. User keys may set 'human' or "
                        "'agent'. Service-key callers cannot spoof 'human' — "
                        "the server forces author_class='agent' for service "
                        "keys regardless of this value."
                    ),
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
            "\n\nProvenance: when an active-ticket bundle exists for the "
            "current project, `persona_name` and `ticket_id` are "
            "automatically threaded into the page revision history. "
            "Pass them explicitly to override."
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
                "persona_name": {
                    "type": "string",
                    "description": "Optional persona attribution. Defaults to active-ticket bundle when bundle.project_id matches the current project.",
                },
                "ticket_id": {
                    "type": "string",
                    "description": "Optional ticket attribution (must be owned by the writing user). Defaults to active-ticket bundle when bundle.project_id matches.",
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
        name="get_wiki_page_history",
        description=(
            "Get a wiki page's full revision history (multi-author "
            "attribution). Each revision carries revision_number, "
            "revised_at, title, word_count, user_id, persona_name, "
            "and ticket_id. Use to render edit history, surface who "
            "shaped a page, or filter for SoD checks."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project page history` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Page slug (e.g. 'architecture', 'concept/auth-flow')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum revisions per page (default 50, max 200)",
                },
                "cursor": {
                    "type": "integer",
                    "description": "Optional keyset pagination cursor — pass the `next_cursor` value returned by the previous response (or the `id` of the last revision in that page)",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="get_knowledge_health",
        description=(
            "Get the project's knowledge base health record. Returns "
            "structured counts that mirror the compile pipeline:\n"
            "- pending_entries: claims compile will process THIS RUN — "
            "claim_class='claim' AND compiled_at IS NULL AND not dismissed "
            "AND freshness_class IN ('current', 'aging') AND superseded_by "
            "IS NULL. Mirror of the compiler's Phase 2b select.\n"
            "- auto_promotable_evidence: evidence rows compile WILL "
            "auto-promote then process — claim_class='evidence' AND "
            "confidence >= 0.5 AND len(content) >= 30 AND not dismissed. "
            "Mirror of the compiler's Phase 2a UPDATE.\n"
            "- uncompiled_notes: notes that need bulk_promote first — "
            "claim_class='note' AND compiled_at IS NULL AND not dismissed. "
            "Compile does NOT touch these; call bulk_promote.\n"
            "- compiled_entries, dismissed_entries, total_entries: bucket "
            "counts (may overlap — a dismissed-after-compile row counts in "
            "both compiled and dismissed).\n"
            "- word_count, section_count, last_compiled, total_compilations.\n"
            "- potentially_stale: true if compile-eligible claim content is "
            "missing from the project context document.\n"
            "- stale_entry_count, low_confidence_count, decayed_count: "
            "actionable counts for housekeeping passes.\n"
            "- recommendations: ordered list of operator hints (run compile, "
            "call bulk_promote, etc.)."
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
        name="rename_session",
        description=(
            "Rename a captured session's title and/or alias. Owner-only — "
            "the caller's user account must own the session. At least one "
            "of `new_title` or `new_alias` is required. Pass `new_alias` "
            "as an empty string to clear the alias.\n\n"
            "Tier gating: setting an alias requires Starter+ "
            "(`aliases_cloud` feature). Title edits are available to all "
            "tiers. Returns the updated session record.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs session rename` or any other sfs CLI command. This tool "
            "connects directly to the API and is more reliable than "
            "shelling out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "The session ID (ses_... format) or alias",
                },
                "new_title": {
                    "type": "string",
                    "description": "New title (≤500 chars, must be non-empty after HTML strip + trim)",
                },
                "new_alias": {
                    "type": "string",
                    "description": "New alias (3-100 chars, alphanumeric + hyphens/underscores, must start alphanumeric). Empty string clears the alias.",
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
            "(persona name), `status`, `priority`, or `kind`. Returns each "
            "ticket's id, title, assigned persona, status, priority, kind, "
            "and parent_ticket_id when set.\n\n"
            "Status values for tasks: suggested, open, in_progress, "
            "blocked, review, done, cancelled. Status values for issues: "
            "open, in_progress, closed, cancelled (no suggested/review/"
            "blocked — Issues are PM-triaged containers, not executor "
            "work units).\n\n"
            "Kind values: 'task' (default — the existing executor unit) "
            "or 'issue' (a PM-triaged container that rolls up child "
            "tasks via parent_ticket_id).\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket list` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "assigned_to": {"type": "string", "description": "Filter by persona name"},
                "status": {"type": "string", "description": "Filter by status"},
                "priority": {"type": "string", "description": "Filter by priority"},
                "kind": {
                    "type": "string",
                    "enum": ["issue", "task"],
                    "description": "Filter by kind. Omit to return both.",
                },
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
        name="list_ticket_comments",
        description=(
            "List comments on a ticket in chronological (oldest-first) "
            "order. Use this to poll review threads — pass the "
            "`since` timestamp AND `since_id` of the last comment you've "
            "seen, and only strictly newer comments are returned. Each "
            "comment includes id, author_user_id, author_persona, "
            "content, session_id, created_at, and ticket_id. Useful for "
            "Codex/Claude review loops where one agent posts and another "
            "reacts.\n\n"
            "Always pass `since` + `since_id` together when polling — "
            "two comments can share a millisecond and `since` alone "
            "would skip one. Order is stable on (created_at, id).\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket comments` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "Ticket id (tk_...)"},
                "since": {
                    "type": "string",
                    "description": (
                        "Optional ISO-8601 timestamp. Only comments created "
                        "strictly after this are returned (for incremental polling)."
                    ),
                },
                "since_id": {
                    "type": "string",
                    "description": (
                        "Cursor tiebreaker. Pass with `since` — when two "
                        "comments share a created_at, the one with id > "
                        "since_id is returned. Prevents same-timestamp skip."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max comments to return (1-500, default 200).",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["ticket_id"],
        },
    ),
    Tool(
        name="get_ticket_review_state",
        description=(
            "Compact summary of a review ticket's open findings, closed "
            "findings, last verdict, and severity counts — derived from "
            "the comment thread without re-reading every comment.\n\n"
            "Use this on Codex review tickets when picking up an "
            "in-progress review loop, or polling for the latest verdict. "
            "Far cheaper context than `list_ticket_comments` on a long "
            "thread.\n\n"
            "Returns `review_state: null` for non-review tickets (no "
            "codex-reviewer comments). When present, the shape is:\n"
            "  - open_findings: [{severity, text, round, raised_comment_id}]\n"
            "  - closed_findings: [{severity, text, round, closed_round, ...}]\n"
            "  - last_verdict: 'VERIFIED-CLEAN' or 'CHANGES_REQUESTED'\n"
            "  - severity_counts: {CRITICAL, HIGH, MEDIUM, LOW} over OPEN findings\n"
            "  - last_review_comment_id / last_implementer_comment_id\n"
            "  - rounds: [{round, verdict, timestamp, findings_raised}]\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket review-state` or scraping comments yourself."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "Ticket id"},
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
            "**Kind: Issue vs Task** (v0.10.24).\n"
            "- `kind='task'` (default) — the existing executor work unit. "
            "Owned by Atlas/Sentinel/Prism/Forge/etc. Tasks finish via "
            "complete + accept. File a Task for concrete code work.\n"
            "- `kind='issue'` — a PM-triaged container that rolls up one "
            "or more child Tasks via `parent_ticket_id`. Owned by "
            "Compass. Issues finish via /close (manual close by Compass "
            "— NOT auto-derived from child status). File an Issue when "
            "a user-reported problem will spawn multiple workstreams "
            "that need PM-level rollup. Authorization: only Compass-"
            "assigned or project owner may file Issues.\n\n"
            "When `parent_ticket_id` is set: parent must exist in this "
            "project AND be kind='issue'. Single-level nesting only — "
            "Issues cannot be nested under other Issues.\n\n"
            "Agent-created Tasks (source='agent') default to 'suggested' "
            "status and require:\n"
            "- acceptance_criteria (at least one)\n"
            "- description >= 20 characters\n"
            "- max 3 per session_id\n\n"
            "Agent-created Issues bypass the suggested-quality gate "
            "(Issues are PM-triaged containers, not executor work).\n\n"
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
                "kind": {
                    "type": "string",
                    "enum": ["issue", "task"],
                    "default": "task",
                    "description": "'task' = executor work (default). 'issue' = PM-triaged rollup container.",
                },
                "parent_ticket_id": {
                    "type": "string",
                    "description": "Optional parent Issue (only valid when kind='task'). Parent must be kind='issue' in this same project.",
                },
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
            "Names are case-insensitive-unique per project (v0.10.23 "
            "tk_884b2321fdb74170) — creating 'Atlas' when 'atlas' exists "
            "returns 409. Use this when an agent decides a new role is "
            "needed (e.g. after recognizing a gap in the team's expertise).\n\n"
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
        name="update_persona",
        description=(
            "Update fields on an existing persona — role, "
            "specializations, and/or content. Bumps `version` only on "
            "actual mutations (no-op PUT does not invalidate caches). "
            "Preserves `created_at` and `created_by`. Errors 404 if no "
            "persona by that name exists in this project.\n\n"
            "v0.10.24 (GH #50): closes the 'agent built a persona during "
            "onboarding, deeper research disproved some claims, now "
            "needs to edit it via MCP' gap. Previously agents had to "
            "drop out of MCP and ask a human to run `sfs persona edit`. "
            "Names are case-insensitive-unique per project (v0.10.23 "
            "tk_884b2321fdb74170) — lookup uses the exact stored case.\n\n"
            "Authorization: same as create_persona (project owner / org "
            "admin / case-insensitive name match).\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs persona edit` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Persona name to update (exact case as stored)"},
                "role": {"type": "string", "description": "New role description (omit to leave unchanged)"},
                "content": {"type": "string", "description": "New persona body markdown (omit to leave unchanged)"},
                "specializations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replace the specializations list (omit to leave unchanged)",
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="delete_persona",
        description=(
            "Soft-delete a persona — sets `is_active=false`. The row "
            "stays in the database so historical tickets and sessions "
            "still resolve their persona name. The name remains "
            "RESERVED at the case-insensitive uniqueness layer "
            "(v0.10.23 tk_884b2321fdb74170), so you cannot create a "
            "new persona with the same name (case-insensitive) while "
            "the soft-deleted row exists. Reactivation is NOT exposed "
            "via MCP today — to bring a soft-deleted persona back, an "
            "operator must use the HTTP API directly "
            "(`PUT /api/v1/projects/{pid}/personas/{name}` with "
            "`is_active=true`). Plan accordingly before calling this tool.\n\n"
            "Refuses with 409 when non-terminal tickets (suggested / "
            "open / in_progress / blocked / review) reference this "
            "persona, unless `force=true` is passed. Without the "
            "guard, those tickets would be stranded — start_ticket "
            "later refuses to load an inactive persona.\n\n"
            "v0.10.24 (GH #50): closes the 'retire a persona that was "
            "filed by mistake or has been replaced' gap that previously "
            "required dropping out of MCP for `sfs persona delete`.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs persona delete` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Persona name to soft-delete"},
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Override the non-terminal-ticket guard. Use "
                        "only when the operator has reassigned or "
                        "cancelled stranded tickets manually."
                    ),
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["name"],
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
        name="update_ticket",
        description=(
            "Patch mutable ticket fields (title, description, "
            "acceptance_criteria, context_refs, file_refs, depends_on, "
            "priority). Only fields the caller passes are mutated — "
            "omitted fields are left untouched.\n\n"
            "SIDE EFFECT: every successful update auto-posts a "
            "`system`-authored diff comment on the ticket AND writes one "
            "audit row per mutated field to the `ticket_edits` table. "
            "Do NOT add a separate comment to record the change — the "
            "side-effect comment already captures it.\n\n"
            "Status transitions are NOT mutable via this verb. Use the "
            "lifecycle tools (start_ticket / complete_ticket / "
            "resolve_ticket / approve_ticket / escalate_ticket) for "
            "status moves. Persona reassignment is NOT exposed here "
            "either — use `assign_persona` for that. This verb is "
            "specifically for corrections + refinements to the spec "
            "(description, acceptance criteria, file/context refs, "
            "dependency edges, priority).\n\n"
            "Optional `lease_epoch` for optimistic concurrency: when "
            "passed, the update is rejected with 409 if the ticket's "
            "epoch has advanced since the caller read it. When omitted, "
            "last-write-wins is allowed (the auto-posted diff comment "
            "marks it as unfenced). lease_epoch alone is a fence, NOT "
            "a mutation — at least one mutable field is required.\n\n"
            "Authorization: caller must be the ticket creator or a "
            "project admin. Persona name does not grant edit rights.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs ticket update` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                },
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "context_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "file_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "lease_epoch": {
                    "type": "integer",
                    "description": "Optional fence — read from get_ticket; 409 if stale. Not a mutation by itself.",
                },
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
    Tool(
        name="update_entry_confidence",
        description=(
            "Update a knowledge entry's confidence score in [0.0, 1.0]. "
            "Wraps PUT /api/v1/projects/{pid}/entries/{id}/confidence — "
            "the v0.10.10 repair endpoint added for tk_483cede83deb443b. "
            "Does NOT auto-promote: keeps confidence orthogonal to the "
            "claim_class transition. After raising confidence above the "
            "0.8 promotion gate, call `promote_entry` to attempt the "
            "note → claim transition."
            "\n\nWhen to use: an agent added an entry via `add_knowledge` "
            "with an initial confidence and now has stronger evidence "
            "(e.g. the entry was verified against multiple sessions, or "
            "the user explicitly confirmed it). Raising the score above "
            "0.8 makes the entry eligible for promotion to a claim, which "
            "is what reaches the compiled project context."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project entries confidence` or any other sfs CLI "
            "command. This tool connects directly to the API and is more "
            "reliable than shelling out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "Knowledge entry ID",
                },
                "confidence": {
                    "type": "number",
                    "description": "New confidence score, 0.0–1.0 inclusive",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["id", "confidence"],
        },
    ),
    Tool(
        name="promote_entry",
        description=(
            "Promote a knowledge entry from `note` to `claim` if quality "
            "gates pass. Wraps PUT /api/v1/projects/{pid}/entries/{id}/"
            "promote. Quality gates enforced server-side: confidence ≥ "
            "0.8, content length ≥ 50 chars, no near-duplicate (>85% word "
            "overlap) of an existing active claim. Failures return a 422 "
            "listing the specific gates that didn't pass — fix the entry "
            "(e.g. raise confidence via `update_entry_confidence`, "
            "lengthen the content) and retry."
            "\n\nOnly claims reach the compiled project context — notes "
            "live in the KB but are excluded from compile until promoted. "
            "Re-promoting an already-claim entry returns 409."
            "\n\nFull MCP workflow: `add_knowledge` (creates note) → "
            "`update_entry_confidence` (raise above 0.8 once verified) → "
            "`promote_entry` (transition note → claim) → "
            "`compile_knowledge_base` (refresh project context)."
            "\n\nIMPORTANT: Always use this MCP tool instead of running "
            "`sfs project entries promote` or any other sfs CLI command. "
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
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="promote_eligible_entries",
        description=(
            "Bulk-promote every eligible KB note in the project to "
            "claim in one operation. Use this to repair a stuck KB "
            "(e.g. after upgrading from a version with the v0.10.10 "
            "confidence-clamp bug). The per-entry "
            "`update_entry_confidence` + `promote_entry` path is "
            "impractical past ~5 entries."
            "\n\nDefault is **dry-run** — the tool computes which "
            "entries would be promoted and returns the structured "
            "result without writing. Inspect the `reasons` breakdown "
            "(`too_short`, `low_confidence`, `duplicate`, `dismissed`, "
            "`superseded`, `wrong_type`) to understand what's "
            "ineligible before calling again with `dry_run=false` to "
            "actually mutate."
            "\n\nEligibility (in order): class=note, not dismissed, "
            "not superseded, matches `entry_type` if filter set, "
            "content length ≥ `min_length`, "
            "confidence ≥ `min_confidence` (unless `set_confidence` "
            "is provided to override), no near-duplicate (>85% word "
            "overlap) of an existing active claim."
            "\n\nReturns `{promoted, skipped, reasons, promoted_ids, "
            "dry_run}`. Call `compile_knowledge_base` afterwards to "
            "fold the newly promoted claims into the project context."
            "\n\nIMPORTANT: Always use this MCP tool instead of "
            "running `sfs project promote-eligible` or scripting the "
            "single-entry endpoints in a loop."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "min_length": {
                    "type": "integer",
                    "description": "Skip entries shorter than this. Matches the single-entry gate (50) by default.",
                    "minimum": 1,
                    "maximum": 10000,
                    "default": 50,
                },
                "min_confidence": {
                    "type": "number",
                    "description": "Only honored when `set_confidence` is omitted. Skip entries below this confidence. Default 0.8 (parity with the single-entry promote gate).",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 0.8,
                },
                "set_confidence": {
                    "type": "number",
                    "description": "Override each candidate's confidence to this value BEFORE the near-duplicate check. Use when bulk-asserting that stuck notes should clear the promotion gate.",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "entry_type": {
                    "type": "string",
                    "description": "Optional filter — only consider this entry_type (e.g. 'decision', 'pattern').",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Default true: compute decision without writing. Pass false to actually promote.",
                    "default": True,
                },
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": [],
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
    # v0.10.9 — handoff MCP surface (8 tools).
    Tool(
        name="create_handoff",
        description=(
            "Hand off a session to another collaborator. Recipient is exactly one of: "
            "recipient_email (any user), recipient_user_id (direct account), or "
            "recipient_team_id (team — Team+ tier). Optional v0.10.9 provenance: "
            "ticket_id + persona_name (carried to recipient's active-ticket bundle on claim). "
            "Optional attachments: list of {kind: 'kb_entry'|'wiki_page'|'ticket', ref_id}. "
            "Pro+ tier required."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session to hand off"},
                "recipient_email": {"type": "string", "description": "Recipient email"},
                "recipient_user_id": {"type": "string", "description": "Recipient user id (direct account match)"},
                "recipient_team_id": {"type": "string", "description": "Team id (Team+ tier)"},
                "message": {"type": "string", "description": "Optional message to recipient"},
                "ticket_id": {"type": "string", "description": "Active ticket id to carry through"},
                "persona_name": {"type": "string", "description": "Persona to carry through"},
                "expires_in_hours": {
                    "type": "integer",
                    "description": "Expiry in hours, default 168 (7d); clamped to tier max",
                },
                "attachments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["kb_entry", "wiki_page", "ticket"]},
                            "ref_id": {"type": "string"},
                        },
                        "required": ["kind", "ref_id"],
                    },
                    "description": "Curated refs to project KB/wiki/ticket entities",
                },
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="claim_handoff",
        description=(
            "Claim a handoff sent to you. Copies the session into your account and "
            "(if the handoff carried ticket_id+persona_name) returns an active_ticket_payload "
            "you can persist to ~/.sessionfs/active_ticket.json. Inaccessible attachment refs "
            "are dropped (returned in dropped_attachments)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handoff_id": {"type": "string", "description": "Handoff id (hnd_...)"},
            },
            "required": ["handoff_id"],
        },
    ),
    Tool(
        name="get_handoff",
        description=(
            "Get full details of a handoff including events, comments, attachments. "
            "Caller must be the sender or a valid recipient (individual or team). "
            "Recipient's first call records a `viewed` audit event."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handoff_id": {"type": "string", "description": "Handoff id"},
            },
            "required": ["handoff_id"],
        },
    ),
    Tool(
        name="list_inbox_handoffs",
        description=(
            "List handoffs sent to you (matched by email, user_id, or team membership). "
            "Pass include_team=false to drop team-handoff dimension."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "include_team": {"type": "boolean", "description": "Include team handoffs (default: true)"},
            },
        },
    ),
    Tool(
        name="list_sent_handoffs",
        description="List handoffs sent BY you.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="revoke_handoff",
        description=(
            "Sender revokes a pending handoff with a required reason. The recipient is "
            "notified by email (individual handoffs). Already-claimed handoffs cannot be revoked."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handoff_id": {"type": "string", "description": "Handoff id"},
                "reason": {"type": "string", "description": "Required reason (1-500 chars)"},
            },
            "required": ["handoff_id", "reason"],
        },
    ),
    Tool(
        name="decline_handoff",
        description=(
            "Recipient declines a pending handoff with optional reason. Sender is notified by email."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handoff_id": {"type": "string", "description": "Handoff id"},
                "reason": {"type": "string", "description": "Optional reason (max 500 chars)"},
            },
            "required": ["handoff_id"],
        },
    ),
    Tool(
        name="add_handoff_comment",
        description=(
            "Post a comment on a handoff thread (author must be sender or a valid recipient). "
            "The other party is notified by email (individual handoffs)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "handoff_id": {"type": "string", "description": "Handoff id"},
                "content": {"type": "string", "description": "Comment body (1-10000 chars)"},
            },
            "required": ["handoff_id", "content"],
        },
    ),
    Tool(
        name="create_work_queue",
        description=(
            "Create an agent work queue — a durable, project-scoped plan "
            "for an agent to repeatedly service a set of tickets without a "
            "human dispatcher.\n\n"
            "`mode` is one of 'review_until_clean', 'implement_until_done', "
            "or 'triage'. `selector` is a JSON filter dict "
            "(status/kind/assigned_to/priority) AND/OR an explicit "
            "{\"ticket_ids\": [...]} list — ticket_ids are seeded as queue "
            "items immediately; filter-only selectors materialize lazily at "
            "run-step time (a later phase).\n\n"
            "Budget caps (rejected if violated): cadence_seconds floor 120 "
            "(default 300); max_tickets_per_run default 1, hard cap 5; "
            "max_attempts_per_item default 3.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs queue create` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human label (1-100 chars; unique per project)"},
                "mode": {
                    "type": "string",
                    "enum": ["review_until_clean", "implement_until_done", "triage"],
                    "description": "What 'service' means for this queue",
                },
                "selector": {
                    "type": "object",
                    "description": "JSON filter dict and/or {ticket_ids:[...]}",
                },
                "assigned_persona": {"type": "string", "description": "Persona name that acts (optional)"},
                "auto_adopt": {"type": "boolean", "description": "Re-run selector each wake to adopt new tickets (default false)"},
                "max_adopt_per_wake": {"type": "integer", "description": "Cap on adopted tickets per wake (default 5)"},
                "stop_condition": {
                    "type": "string",
                    "enum": ["queue_empty", "all_clean", "max_tickets", "manual"],
                    "description": "When to stop (default queue_empty)",
                },
                "cadence_seconds": {"type": "integer", "description": "Advisory wake interval (>=120, default 300)"},
                "max_tickets_per_run": {"type": "integer", "description": "Budget per wake (1-5, default 1)"},
                "max_attempts_per_item": {"type": "integer", "description": "Emitted-directive attempts before failing an item (default 3)"},
                "created_by_session_id": {"type": "string", "description": "Provenance — session id (optional)"},
                "created_by_persona": {"type": "string", "description": "Provenance — persona name (optional)"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["name", "mode"],
        },
    ),
    Tool(
        name="get_work_queue",
        description=(
            "Inspect one work queue: its config, a progress rollup "
            "{pending, active, waiting, done, failed}, and (by default) its "
            "per-item cursor summary. Safe to poll — does no work.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs queue show` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "work_queue_id": {"type": "string", "description": "Work queue id (wq_...)"},
                "include_items": {"type": "boolean", "description": "Include per-item summary (default true)"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["work_queue_id"],
        },
    ),
    Tool(
        name="list_work_queues",
        description=(
            "List work queues for this project, each with a progress "
            "rollup. Filter by `status` (active/paused/completed/"
            "cancelled).\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs queue list` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by queue status"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
        },
    ),
    Tool(
        name="set_work_queue_status",
        description=(
            "Change a work queue's lifecycle status. The server enforces "
            "the allowed-transition table:\n"
            "  active    -> paused | completed | cancelled\n"
            "  paused    -> active | completed | cancelled\n"
            "  completed -> active (reopen) | cancelled\n"
            "  cancelled -> (terminal)\n\n"
            "An illegal transition returns 409 invalid_status_transition. "
            "Optionally pass `lease_epoch` (from a prior get/list) to fence "
            "concurrent edits — a stale epoch returns 409 stale_lease_epoch. "
            "The epoch bumps on success.\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs queue set-status` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "work_queue_id": {"type": "string", "description": "Work queue id (wq_...)"},
                "status": {
                    "type": "string",
                    "enum": ["active", "paused", "completed", "cancelled"],
                    "description": "Target status",
                },
                "lease_epoch": {"type": "integer", "description": "Optional fence — current queue lease_epoch"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["work_queue_id", "status"],
        },
    ),
    Tool(
        name="run_work_queue_step",
        description=(
            "Drive ONE wake of a work queue — the heartbeat. Claims up to "
            "max_tickets_per_run eligible items, emits a BOUNDED directive "
            "for each (intent + ticket_id + ticket_lease_epoch + a small "
            "comment_delta + expand_hints + directive_id + "
            "work_queue_run_id), and returns it. Do the directive's one "
            "intent, write back per writeback_contract (fence ticket writes "
            "with ticket_lease_epoch — the queue posts comments; you NEVER "
            "send author_persona, the server stamps it), then call "
            "complete_work_queue_step.\n\n"
            "Responses: {status:'ok', directives:[...]} (work to do); "
            "{status:'idle', reason:'cadence'} (woke too soon — respect "
            "cadence_seconds); {status:'stopped', reason:'queue_empty'|"
            "'paused'} (done / paused). review_until_clean queues return "
            "an error (the trusted-verdict stop oracle ships in a later "
            "phase) — use implement_until_done or triage for now.\n\n"
            "Crash-safe: if you crash before complete, the SAME directive "
            "re-emits next wake (same directive_id) — no review lost, none "
            "double-counted.\n\n"
            "Requires the work_queues:write AND tickets:write scopes (plus "
            "agent_runs:write if a directive opens an execution audit).\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs queue step` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "work_queue_id": {"type": "string", "description": "Work queue id (wq_...)"},
                "wake_source": {"type": "string", "description": "loop|cron|ci|manual|event (default manual)"},
                "wake_ref": {"type": "string", "description": "CI run URL / cron id / loop tag (optional)"},
                "max_tickets": {"type": "integer", "description": "Cap items this wake (1-5; <= queue's max_tickets_per_run)"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["work_queue_id"],
        },
    ),
    Tool(
        name="complete_work_queue_step",
        description=(
            "Settle a directive after acting on it — the SINGLE commit point "
            "of the loop, the ONLY place the durable (ACKED) cursor "
            "advances. Pass the directive_id + item_id + ticket_id from the "
            "directive, the comment_id you posted, and the outcome.\n\n"
            "On success the server VALIDATES your comment landed before "
            "advancing the cursor, settles the directive lease, and sets the "
            "item to 'waiting' (more rounds expected) or 'done' (outcome "
            "completed_ticket/resolved). Set failed=true if you made no "
            "progress — the item backs off (2m→5m→15m→60m) and parks as "
            "'failed' after max_attempts_per_item (human reset needed).\n\n"
            "Idempotent: settling the same directive_id twice returns the "
            "prior outcome without re-applying.\n\n"
            "Requires the work_queues:write AND tickets:write scopes (plus "
            "agent_runs:write when linking an agent_run_id).\n\n"
            "IMPORTANT: Always use this MCP tool instead of running "
            "`sfs queue ...` or any other sfs CLI command."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "work_queue_id": {"type": "string", "description": "Work queue id (wq_...)"},
                "item_id": {"type": "string", "description": "Item id from the directive (wqi_...)"},
                "directive_id": {"type": "string", "description": "Directive id being settled (dir_...)"},
                "ticket_id": {"type": "string", "description": "Ticket id from the directive (tk_...)"},
                "outcome": {"type": "string", "description": "posted_review|posted_progress|completed_ticket|resolved|waited"},
                "comment_id": {"type": "string", "description": "The comment you just posted (validated server-side)"},
                "agent_run_id": {"type": "string", "description": "AgentRun you opened, if any (optional)"},
                "ticket_lease_epoch": {"type": "integer", "description": "Ticket lease epoch from the directive (fencing)"},
                "failed": {"type": "boolean", "description": "true if no progress was made (triggers backoff)"},
                "summary": {"type": "string", "description": "Short human summary of the outcome (optional)"},
                "git_remote": {"type": "string", "description": "Git remote URL (auto-detected if empty)"},
            },
            "required": ["work_queue_id", "item_id", "directive_id", "ticket_id", "outcome"],
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
        elif name == "get_wiki_page_history":
            result = await _handle_get_wiki_page_history(arguments)
        elif name == "get_knowledge_health":
            result = await _handle_get_knowledge_health(arguments)
        elif name == "get_context_section":
            result = await _handle_get_context_section(arguments)
        elif name == "rename_session":
            result = await _handle_rename_session(arguments)
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
        elif name == "list_ticket_comments":
            result = await _handle_list_ticket_comments(arguments)
        elif name == "get_ticket_review_state":
            result = await _handle_get_ticket_review_state(arguments)
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
        elif name == "update_persona":
            result = await _handle_update_persona(arguments)
        elif name == "delete_persona":
            result = await _handle_delete_persona(arguments)
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
        elif name == "update_ticket":
            result = await _handle_update_ticket(arguments)
        elif name == "create_agent_run":
            result = await _handle_create_agent_run(arguments)
        elif name == "complete_agent_run":
            result = await _handle_complete_agent_run(arguments)
        elif name == "list_agent_runs":
            result = await _handle_list_agent_runs(arguments)
        elif name == "dismiss_knowledge_entry":
            result = await _handle_dismiss_knowledge_entry(arguments)
        elif name == "update_entry_confidence":
            result = await _handle_update_entry_confidence(arguments)
        elif name == "promote_entry":
            result = await _handle_promote_entry(arguments)
        elif name == "promote_eligible_entries":
            result = await _handle_promote_eligible_entries(arguments)
        elif name == "approve_ticket":
            result = await _handle_approve_ticket(arguments)
        elif name == "checkpoint_session":
            result = _handle_checkpoint_session(arguments)
        elif name == "list_checkpoints":
            result = _handle_list_checkpoints(arguments)
        elif name == "fork_session":
            result = _handle_fork_session(arguments)
        elif name == "create_handoff":
            result = await _handle_create_handoff(arguments)
        elif name == "claim_handoff":
            result = await _handle_claim_handoff(arguments)
        elif name == "get_handoff":
            result = await _handle_get_handoff(arguments)
        elif name == "list_inbox_handoffs":
            result = await _handle_list_inbox_handoffs(arguments)
        elif name == "list_sent_handoffs":
            result = await _handle_list_sent_handoffs(arguments)
        elif name == "revoke_handoff":
            result = await _handle_revoke_handoff(arguments)
        elif name == "decline_handoff":
            result = await _handle_decline_handoff(arguments)
        elif name == "add_handoff_comment":
            result = await _handle_add_handoff_comment(arguments)
        elif name == "create_work_queue":
            result = await _handle_create_work_queue(arguments)
        elif name == "get_work_queue":
            result = await _handle_get_work_queue(arguments)
        elif name == "list_work_queues":
            result = await _handle_list_work_queues(arguments)
        elif name == "set_work_queue_status":
            result = await _handle_set_work_queue_status(arguments)
        elif name == "run_work_queue_step":
            result = await _handle_run_work_queue_step(arguments)
        elif name == "complete_work_queue_step":
            result = await _handle_complete_work_queue_step(arguments)
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
        raise Exception(
            f"No project found for {normalized}. "
            f"Create one with: sfs project init, or if this repo is part of "
            f"a multi-repo project, link it with: sfs project link-repo"
        )
    if resp.status_code >= 400:
        raise Exception(f"Error fetching project: {resp.status_code}")

    project_id = resp.json().get("id", "")
    return config.sync.api_url.rstrip("/"), config.sync.api_key, project_id


async def _handle_add_knowledge(args: dict) -> str:
    """Add a knowledge entry via the cloud API.

    v0.10.21 Phase 4a (tk_8028c79963fe4dc7) — forwards `persona_name` and
    `author_class` so MCP-authored entries are first-class citizens in the
    KB just like direct API writes. When `persona_name` is not explicitly
    supplied, auto-threads from `~/.sessionfs/active_ticket.json` if the
    bundle's project_id matches the resolved project (mirrors
    `update_wiki_page`). Explicit args win over the bundle.

    Anti-spoof note: `author_class` is forwarded as-is to the server,
    which is authoritative — for service-key callers, the server forces
    `author_class='agent'` regardless of payload. The MCP layer just
    plumbs the field through; it does not (and cannot) enforce auth.
    """
    content = args.get("content", "")
    entry_type = args.get("entry_type", "discovery")
    session_id = args.get("session_id")
    # v0.10.10 — only forward confidence when caller explicitly passed
    # it. Pre-fix, the handler defaulted to 1.0 and always sent it,
    # which prevented the server from distinguishing 'caller said 1.0'
    # from 'caller didn't say'. Combined with the server's old
    # manual-source clamp at 0.7, this silently lowered legitimate
    # caller-supplied confidence. Now both layers agree: explicit
    # confidence is honored end-to-end.
    raw_conf = args.get("confidence")
    confidence: float | None = float(raw_conf) if raw_conf is not None else None
    entity_ref = args.get("entity_ref")
    entity_type = args.get("entity_type")
    force_claim = args.get("force_claim", False)
    persona_name = args.get("persona_name")
    author_class = args.get("author_class")
    # v0.10.23 tk_49db8d2b6c424d35 — explicit opt-in for state-cache
    # upsert semantics. Requires entity_ref. When True, the server
    # skips similarity dedup and auto-dismisses any prior active entry
    # with the same entity_ref in the project, surfacing the
    # dismissed IDs in the response.
    upsert = bool(args.get("upsert", False))
    git_remote = args.get("git_remote", "")

    if not content:
        return "Content is required."

    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)

        # v0.10.21 — when persona_name isn't explicit, auto-thread from
        # the active-ticket bundle if it matches this project. Mirrors
        # the v0.10.7 update_wiki_page pattern. Explicit args win.
        bundle_threaded_persona = False
        if not persona_name:
            try:
                from sessionfs.active_ticket import read_bundle

                bundle = read_bundle()
                if (
                    isinstance(bundle, dict)
                    and bundle.get("project_id") == project_id
                ):
                    bundle_persona = bundle.get("persona_name")
                    if bundle_persona:
                        persona_name = bundle_persona
                        bundle_threaded_persona = True
            except Exception:
                # Bundle read is best-effort — don't fail KB write
                # because the local provenance file is corrupt.
                pass

        # Codex R1 MEDIUM (tk_8028c79963fe4dc7): when the persona came
        # from the active-ticket bundle (i.e. the caller is operating
        # as that persona) and the caller did not explicitly set
        # author_class, default to 'agent'. Without this, the bundle
        # path produces `persona_name=scout, author_class=human` which
        # the GET /entries?persona_name=scout&author_class=agent
        # retrieval channel — the whole point of Phase 4a — misses.
        # Explicit `author_class` always wins; the no-bundle / no-
        # persona legacy path continues to omit the field so the
        # server applies its own default.
        if bundle_threaded_persona and not author_class:
            author_class = "agent"

        import httpx
        payload: dict = {
            "content": content,
            "entry_type": entry_type,
        }
        if confidence is not None:
            payload["confidence"] = confidence
        if session_id:
            payload["session_id"] = session_id
        if entity_ref:
            payload["entity_ref"] = entity_ref
        if entity_type:
            payload["entity_type"] = entity_type
        if force_claim:
            payload["force_claim"] = True
        if persona_name:
            payload["persona_name"] = persona_name
        if author_class:
            payload["author_class"] = author_class
        if upsert:
            payload["upsert"] = True

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
            msg = (
                f"Knowledge entry added (id: {data['id']}, "
                f"type: {entry_type}, class: {claim_class})."
            )
            # v0.10.21 — surface the attribution that landed so callers
            # can confirm persona/author_class made it through and the
            # server didn't reject or override (e.g. service-key
            # author_class=human → server forces 'agent').
            persisted_persona = data.get("persona_name")
            persisted_author = data.get("author_class")
            attribution_bits = []
            if persisted_persona:
                attribution_bits.append(f"persona: {persisted_persona}")
            if persisted_author:
                attribution_bits.append(f"author_class: {persisted_author}")
            if attribution_bits:
                msg += "\nAttribution: " + ", ".join(attribution_bits)
            # v0.10.23 tk_49db8d2b6c424d35 — when entity_ref upsert
            # superseded a prior, surface the dismissed IDs so the
            # caller knows their state cache was rolled forward (and
            # didn't silently win a similarity-dedup race against an
            # unrelated entry).
            superseded = data.get("upserted_from") or []
            if superseded:
                msg += (
                    f"\nUpsert: superseded prior entries "
                    f"{superseded} via entity_ref={entity_ref!r}"
                )
            if tip:
                msg += f"\nTip: {tip}"
            return msg
        return f"Failed to add entry: {resp.status_code} — {resp.text}"

    except Exception as exc:
        return f"Failed: {exc}"


async def _handle_update_wiki_page(args: dict) -> str:
    """Create or update a wiki page via the cloud API.

    v0.10.7 R2 — when an active-ticket bundle exists for this project,
    automatically thread `persona_name` and `ticket_id` into the PUT
    request so wiki revisions are attributed to the persona / ticket
    that produced them. Explicit args (`persona_name`, `ticket_id`)
    override the bundle.
    """
    slug = args.get("slug", "")
    content = args.get("content", "")
    title = args.get("title")
    git_remote = args.get("git_remote", "")

    if not slug or not content:
        return "slug and content are required."

    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)

        # v0.10.7 R2 — pick up persona_name + ticket_id from the active
        # bundle when its project matches our project. Explicit args win.
        persona_name = args.get("persona_name")
        ticket_id = args.get("ticket_id")
        if not persona_name or not ticket_id:
            try:
                from sessionfs.active_ticket import read_bundle

                bundle = read_bundle()
                if (
                    isinstance(bundle, dict)
                    and bundle.get("project_id") == project_id
                ):
                    if not persona_name:
                        persona_name = bundle.get("persona_name")
                    if not ticket_id:
                        ticket_id = bundle.get("ticket_id")
            except Exception:
                # Bundle read is best-effort — don't fail page write
                # because the local provenance file is corrupt.
                pass

        import httpx
        payload: dict = {"content": content}
        if title:
            payload["title"] = title
        if persona_name:
            payload["persona_name"] = persona_name
        if ticket_id:
            payload["ticket_id"] = ticket_id

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


async def _handle_ask_project(args: dict) -> dict:
    """Research a question using project context and knowledge entries.

    Returns a structured dict so callers can trace which entities shaped
    the assembled research material (SoD / audit / Agent Runner).

    `sources_cited` is a typed list of `{type, id}` entries:
      - `{"type": "kb", "id": <int>}` for KB entries returned by the search step
      - `{"type": "session", "id": "<str>"}` for local sessions matched on the question

    Compiled-context sections are NOT consulted by ask_project today (no
    section-retrieval step). When that lands, sources_cited gains a
    `{"type": "section", "slug": "<str>"}` variant without changing the
    field name.
    """
    question = args.get("question", "")
    git_remote = args.get("git_remote", "")
    if not question:
        return {"markdown": "Please provide a question.", "sources_cited": []}

    # Get project context (pass through git_remote)
    context_result = await _handle_get_project_context({"git_remote": git_remote})
    if not isinstance(context_result, str):
        context_result = json.dumps(context_result, indent=2, default=str)

    # Search knowledge entries for the question. Pass used_in_answer=true
    # so the server increments used_in_answer_count + updates last_relevant_at
    # on matched entries (strong relevance signal).
    search_args = {"query": question, "limit": 15, "git_remote": git_remote, "_used_in_answer": True}
    search_result = await _handle_search_knowledge(search_args)
    if not isinstance(search_result, str):
        search_result = json.dumps(search_result, indent=2, default=str)

    # Fetch the same KB entries as a structured list so we can record
    # their IDs in sources_cited. Re-uses the same project-scoped search
    # path the markdown render hit; cheap second call against pg_trgm.
    kb_entries = await _fetch_kb_entries_raw(search_args)
    sources_cited: list[dict] = []
    for e in kb_entries:
        kb_id = e.get("id")
        if isinstance(kb_id, int):
            sources_cited.append({"type": "kb", "id": kb_id})

    # Search local sessions for additional context
    local_results = ""
    local_session_ids: list[str] = []
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
                if isinstance(sid, str) and sid:
                    local_session_ids.append(sid)
            local_results = "\n".join(local_lines)
    except RuntimeError:
        pass  # Search index not available

    for sid in local_session_ids:
        sources_cited.append({"type": "session", "id": sid})

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

    return {
        "markdown": "\n".join(lines),
        "sources_cited": sources_cited,
    }


async def _fetch_kb_entries_raw(args: dict) -> list[dict]:
    """Fetch KB entries for a query as a structured list (no markdown).

    Mirrors `_handle_search_knowledge`'s lookup path so callers
    (`_handle_ask_project`) can capture KB IDs without parsing the
    formatted markdown — avoids the regex-extraction-from-prose
    antipattern Codex flagged in v0.10.4's collect_returned_refs.

    Returns [] on any failure (auth missing, project not found, etc.)
    rather than raising — ask_project should degrade gracefully.
    """
    query = args.get("query", "")
    entry_type = args.get("entry_type")
    limit = int(args.get("limit", 10))
    include_stale = args.get("include_stale", False)
    git_remote = args.get("git_remote", "")

    if isinstance(query, str) and len(query.strip()) < 3:
        return []

    if not git_remote:
        git_remote = await _resolve_workspace_git_remote()
    if not git_remote:
        return []

    from sessionfs.server.github_app import normalize_git_remote

    normalized = normalize_git_remote(git_remote)
    if not normalized:
        return []

    try:
        from sessionfs.daemon.config import load_config

        config = load_config()
        if not config.sync.api_key:
            return []

        import httpx

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{config.sync.api_url.rstrip('/')}/api/v1/projects/{normalized}",
                headers={"Authorization": f"Bearer {config.sync.api_key}"},
            )
        if resp.status_code >= 400:
            return []
        project_data = resp.json()
        project_id = project_data.get("id", "")
        if not project_id:
            return []

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
            return []
        entries = resp.json()
        if not isinstance(entries, list):
            return []

        if not include_stale:
            entries = [
                e for e in entries
                if e.get("claim_class", "claim") == "claim"
                and e.get("freshness_class", "current") in ("current", "aging")
                and not e.get("superseded_by")
                and not e.get("dismissed", False)
            ]
        return entries
    except Exception as exc:
        logger.warning("ask_project KB structured fetch failed: %s", exc)
        return []


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


async def _handle_get_wiki_page_history(args: dict) -> dict:
    """v0.10.7 — wrap GET /api/v1/projects/{project_id}/pages/{slug}/history."""
    slug = args.get("slug", "")
    if not slug:
        return {"error": "slug is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    params: dict = {}
    if "limit" in args:
        try:
            params["limit"] = int(args["limit"])
        except (TypeError, ValueError):
            pass
    if "cursor" in args and args["cursor"] is not None:
        try:
            params["cursor"] = int(args["cursor"])
        except (TypeError, ValueError):
            pass

    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/pages/{slug}/history",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
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


async def _handle_rename_session(args: dict) -> dict:
    """Wrap PATCH /api/v1/sessions/{session_id} to rename title and/or alias.

    Sessions are user-scoped, not project-scoped, so this does not call
    `_resolve_project_id`. Auth is the standard bearer-token user gate;
    the server enforces owner-only via `_get_user_session`. Tier gating
    on alias is enforced server-side via `check_feature(aliases_cloud)`.
    """
    session_id = args.get("session_id", "")
    if not session_id:
        return {"error": "session_id is required"}

    new_title = args.get("new_title")
    new_alias = args.get("new_alias")
    if new_title is None and new_alias is None:
        return {"error": "At least one of new_title or new_alias is required"}

    payload: dict = {}
    if new_title is not None:
        payload["title"] = new_title
    if new_alias is not None:
        # Empty string means "clear the alias" — route the clear path
        # through DELETE /alias rather than PATCH alias=null, since the
        # PATCH endpoint validates non-empty against _ALIAS_RE.
        if new_alias == "":
            # Handled below as a separate request.
            pass
        else:
            payload["alias"] = new_alias

    config = load_config()
    if not config.sync.api_key:
        return {"error": "Not authenticated. Run 'sfs auth login' first."}

    import httpx
    api_url = config.sync.api_url.rstrip("/")
    headers = {"Authorization": f"Bearer {config.sync.api_key}"}

    async with httpx.AsyncClient(timeout=15) as client:
        # Resolve the caller-supplied identifier (which may be an alias)
        # to the canonical session id BEFORE any mutation. Codex R1
        # MEDIUM: if the caller passes the alias they're about to clear,
        # the post-DELETE GET/PATCH would 404 since the alias is no
        # longer resolvable. Capture the canonical id once up front.
        canonical_id = session_id
        if new_alias == "":
            del_resp = await client.delete(
                f"{api_url}/api/v1/sessions/{session_id}/alias",
                headers=headers,
            )
            if del_resp.status_code == 404:
                return {"error": f"Session {session_id} not found"}
            if del_resp.status_code >= 400:
                return {"error": f"API error {del_resp.status_code}: {del_resp.text}"}
            try:
                del_body = del_resp.json()
                if isinstance(del_body, dict) and del_body.get("id"):
                    canonical_id = del_body["id"]
            except Exception:
                # If the response isn't JSON, fall back to the caller's
                # identifier. PATCH/GET below will surface a 404 if that
                # was the just-cleared alias.
                pass
            if not payload:
                # Alias-clear-only path: DELETE response already carries
                # the updated SessionDetail. No follow-up call needed.
                return del_body if isinstance(del_body, dict) else {"id": canonical_id}
        if payload:
            resp = await client.patch(
                f"{api_url}/api/v1/sessions/{canonical_id}",
                json=payload,
                headers=headers,
            )
            if resp.status_code == 404:
                return {"error": f"Session {session_id} not found"}
            if resp.status_code >= 400:
                return {"error": f"API error {resp.status_code}: {resp.text}"}
            return resp.json()
        # No mutation requested but caller didn't trigger alias-clear
        # (defensive — this branch is unreachable given the earlier
        # at-least-one-of check, kept for safety).
        get_resp = await client.get(
            f"{api_url}/api/v1/sessions/{canonical_id}",
            headers=headers,
        )
        if get_resp.status_code >= 400:
            return {"error": f"API error {get_resp.status_code}: {get_resp.text}"}
        return get_resp.json()


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
    for key in ("assigned_to", "status", "priority", "kind"):
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


async def _handle_get_ticket_review_state(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/tickets/{ticket_id}/review-state.

    v0.10.11 tk_e025375272b84a95 — compact derived review state for
    long review threads. Returns `{"ticket_id": ..., "review_state": ...}`
    where review_state is null for non-review tickets.
    """
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
            f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}/review-state",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Ticket '{ticket_id}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_list_ticket_comments(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/tickets/{ticket_id}/comments.

    tk_32f3dacf1c9749bc — unblocks Codex/Claude review polling loops.
    Supports `since` (ISO timestamp) + `since_id` (tiebreaker) + `limit`
    for incremental polling. Pass since AND since_id together to handle
    same-timestamp comment ties safely (Codex review #1).
    """
    ticket_id = args.get("ticket_id", "")
    if not ticket_id:
        return {"error": "ticket_id is required"}

    # Codex review #2 — validate args BEFORE the network call so
    # bad-input errors surface as local validation, not as opaque
    # DNS/config failures from _resolve_project_id.
    params: dict[str, Any] = {}
    since = args.get("since")
    if isinstance(since, str) and since.strip():
        params["since"] = since.strip()
    since_id = args.get("since_id")
    if isinstance(since_id, str) and since_id.strip():
        params["since_id"] = since_id.strip()
    limit = args.get("limit")
    if limit is not None:
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            return {"error": "limit must be an integer"}
        if limit_int < 1 or limit_int > 500:
            return {"error": "limit must be between 1 and 500"}
        params["limit"] = limit_int

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/tickets/{ticket_id}/comments",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params or None,
        )
    if resp.status_code == 404:
        return {"error": f"Ticket '{ticket_id}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    # Wrap the list response in a dict so callers can extend later
    # without breaking clients (e.g. add next_cursor when we move to
    # opaque cursors). MCP responses are JSON-serialized to text anyway.
    return {"comments": resp.json()}


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
        "kind", "parent_ticket_id",
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


async def _handle_create_work_queue(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/work-queues.

    WQ-P2 (tk_3481237f3b0847d6). Validates required args + budget caps
    locally before the network call so bad input surfaces here, not as an
    opaque 422.
    """
    name = args.get("name", "")
    if isinstance(name, str):
        name = name.strip()
    if not name:
        return {"error": "name is required"}

    mode = args.get("mode", "")
    valid_modes = ("review_until_clean", "implement_until_done", "triage")
    if mode not in valid_modes:
        return {"error": f"mode must be one of: {list(valid_modes)}"}

    selector = args.get("selector")
    if selector is not None and not isinstance(selector, dict):
        return {"error": "selector must be a JSON object"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict[str, Any] = {"name": name, "mode": mode}
    if isinstance(selector, dict):
        body["selector"] = selector
    for key in (
        "assigned_persona", "stop_condition",
        "created_by_session_id", "created_by_persona",
    ):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            body[key] = val.strip()
    if isinstance(args.get("auto_adopt"), bool):
        body["auto_adopt"] = args["auto_adopt"]
    for key in (
        "max_adopt_per_wake", "cadence_seconds",
        "max_tickets_per_run", "max_attempts_per_item",
    ):
        val = args.get(key)
        if isinstance(val, int) and not isinstance(val, bool):
            body[key] = val

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/work-queues",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_get_work_queue(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/work-queues/{queue_id}."""
    queue_id = args.get("work_queue_id", "")
    if not queue_id:
        return {"error": "work_queue_id is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    params: dict[str, str] = {}
    if isinstance(args.get("include_items"), bool):
        params["include_items"] = str(args["include_items"]).lower()

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/work-queues/{queue_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
    if resp.status_code == 404:
        return {"error": f"Work queue '{queue_id}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_list_work_queues(args: dict) -> dict:
    """Wrap GET /api/v1/projects/{project_id}/work-queues."""
    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    params: dict[str, str] = {}
    status = args.get("status")
    if isinstance(status, str) and status.strip():
        params["status"] = status.strip()

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/projects/{project_id}/work-queues",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return {"work_queues": resp.json()}


async def _handle_set_work_queue_status(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/work-queues/{queue_id}/status."""
    queue_id = args.get("work_queue_id", "")
    if not queue_id:
        return {"error": "work_queue_id is required"}
    status = args.get("status", "")
    valid_statuses = ("active", "paused", "completed", "cancelled")
    if status not in valid_statuses:
        return {"error": f"status must be one of: {list(valid_statuses)}"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict[str, Any] = {"status": status}
    if args.get("lease_epoch") is not None:
        try:
            body["lease_epoch"] = int(args["lease_epoch"])
        except (TypeError, ValueError):
            return {"error": "lease_epoch must be an integer"}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/work-queues/{queue_id}/status",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code == 404:
        return {"error": f"Work queue '{queue_id}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_run_work_queue_step(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/work-queues/{queue_id}/step.

    WQ-P3 (tk_3de50bf7bb73418b). The heartbeat — claims work, emits a bounded
    directive, advances the SEEN cursor. Validates args locally before the
    network call so bad input surfaces here, not as an opaque 422.
    """
    queue_id = args.get("work_queue_id", "")
    if not queue_id:
        return {"error": "work_queue_id is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict[str, Any] = {}
    wake_source = args.get("wake_source")
    if isinstance(wake_source, str) and wake_source.strip():
        body["wake_source"] = wake_source.strip()
    wake_ref = args.get("wake_ref")
    if isinstance(wake_ref, str) and wake_ref.strip():
        body["wake_ref"] = wake_ref.strip()
    max_tickets = args.get("max_tickets")
    if isinstance(max_tickets, int) and not isinstance(max_tickets, bool):
        body["max_tickets"] = max_tickets

    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/work-queues/{queue_id}/step",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code == 404:
        return {"error": f"Work queue '{queue_id}' not found"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_complete_work_queue_step(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{pid}/work-queues/{queue_id}/step/complete.

    WQ-P3 (tk_3de50bf7bb73418b). The single commit point — idempotent settle,
    validate-writeback-landed, advance the ACKED cursor / apply backoff.
    """
    queue_id = args.get("work_queue_id", "")
    if not queue_id:
        return {"error": "work_queue_id is required"}
    for req in ("item_id", "directive_id", "ticket_id", "outcome"):
        val = args.get(req)
        if not isinstance(val, str) or not val.strip():
            return {"error": f"{req} is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    body: dict[str, Any] = {
        "item_id": args["item_id"].strip(),
        "directive_id": args["directive_id"].strip(),
        "ticket_id": args["ticket_id"].strip(),
        "outcome": args["outcome"].strip(),
    }
    for key in ("comment_id", "agent_run_id", "summary"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            body[key] = val.strip()
    if isinstance(args.get("failed"), bool):
        body["failed"] = args["failed"]
    if args.get("ticket_lease_epoch") is not None:
        try:
            body["ticket_lease_epoch"] = int(args["ticket_lease_epoch"])
        except (TypeError, ValueError):
            return {"error": "ticket_lease_epoch must be an integer"}

    import httpx
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/work-queues/{queue_id}/step/complete",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code == 404:
        return {"error": f"Work queue '{queue_id}' not found"}
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


async def _handle_update_persona(args: dict) -> dict:
    """Wrap PUT /api/v1/projects/{project_id}/personas/{name}.

    v0.10.24 tk_32abb6d0d4744c5d / GH #50. Mirror the CLI `sfs persona
    edit` flow for agents working through MCP. Only fields the caller
    passes are mutated; the server bumps `version` only on actual
    mutations so a no-op call doesn't invalidate caches.
    """
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    # Build the PUT body with only the fields the caller passed. Server
    # treats omitted fields as no-op (preserves existing value), so we
    # must not send role/content/specializations with their defaults —
    # that would silently overwrite existing values.
    body: dict[str, Any] = {}
    for key in ("role", "content"):
        val = args.get(key)
        if isinstance(val, str):
            body[key] = val
    specs = args.get("specializations")
    if isinstance(specs, list):
        body["specializations"] = list(specs)

    if not body:
        return {
            "error": (
                "At least one of role, content, or specializations is "
                "required for update_persona"
            )
        }

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{api_url}/api/v1/projects/{project_id}/personas/{name}",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code == 404:
        return {"error": f"Persona '{name}' not found in this project"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_delete_persona(args: dict) -> dict:
    """Wrap DELETE /api/v1/projects/{project_id}/personas/{name}?force=...

    v0.10.24 tk_32abb6d0d4744c5d / GH #50. Soft-delete only — the server
    sets is_active=false. Refuses with 409 when non-terminal tickets
    reference the persona unless `force=true` is passed (server-side
    guard from KB 339).
    """
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    # Codex R1 MEDIUM (tk_32abb6d0d4744c5d) — `bool("false")` is True
    # in Python. A destructive path should never accept truthy-string
    # values as "force". Accept literal True OR a narrow set of
    # case-insensitive truthy strings ("true", "1", "yes"); anything
    # else (False, "false", "no", "0", omitted) stays unforced.
    raw_force = args.get("force", False)
    if isinstance(raw_force, bool):
        force = raw_force
    elif isinstance(raw_force, str):
        force = raw_force.strip().lower() in {"true", "1", "yes"}
    else:
        force = False

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    params: dict[str, str] = {}
    if force:
        params["force"] = "true"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.delete(
            f"{api_url}/api/v1/projects/{project_id}/personas/{name}",
            headers={"Authorization": f"Bearer {api_key}"},
            params=params,
        )
    if resp.status_code == 204:
        return {"ok": True, "name": name, "soft_deleted": True}
    if resp.status_code == 404:
        return {"error": f"Persona '{name}' not found in this project"}
    if resp.status_code == 409:
        # Surface the structured envelope so the caller can decide
        # whether to retry with force=true.
        try:
            return {"error": resp.json(), "status": 409}
        except Exception:
            return {"error": resp.text, "status": 409}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return {"ok": True, "name": name, "soft_deleted": True}


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


# Codex R1 LOW — assignment + related_sessions are NOT exposed via
# update_ticket. `assigned_to` belongs to `assign_persona` (the
# routing-only verb); `related_sessions` is an internal write-path
# field. Keeping them in the wrapped surface here would undercut
# the FSM/verb boundary even though the underlying PUT route still
# accepts them for `assign_persona` back-compat.
_UPDATE_TICKET_MUTABLE_FIELDS = (
    "title",
    "description",
    "priority",
    "acceptance_criteria",
    "context_refs",
    "file_refs",
    "depends_on",
)
_UPDATE_TICKET_FIELDS = _UPDATE_TICKET_MUTABLE_FIELDS + ("lease_epoch",)


async def _handle_update_ticket(args: dict) -> dict:
    """Wrap PUT /api/v1/projects/{project_id}/tickets/{ticket_id}.

    tk_835a876529de4551 — partial-update for mutable ticket fields.
    Build the PUT body from only the fields the caller passed so
    server-side omitted-fields-are-untouched semantics work; do not
    inject defaults that would silently overwrite existing values.
    """
    ticket_id = (args.get("ticket_id") or "").strip()
    if not ticket_id:
        return {"error": "ticket_id is required"}

    body: dict[str, Any] = {}
    for key in _UPDATE_TICKET_FIELDS:
        if key not in args:
            continue
        val = args[key]
        if val is None:
            continue
        body[key] = val

    # Codex R1 MED #2 — lease_epoch is a fence parameter, not a
    # mutation. Reject lease_epoch-only payloads locally so callers
    # don't hit the server's 400 (and don't waste the round-trip).
    has_mutable = any(k in body for k in _UPDATE_TICKET_MUTABLE_FIELDS)
    if not has_mutable:
        return {
            "error": (
                "At least one mutable field is required for update_ticket "
                "(title, description, priority, acceptance_criteria, "
                "context_refs, file_refs, depends_on). "
                "lease_epoch alone is a fence, not a mutation; use "
                "assign_persona to change assignment."
            )
        }

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
            json=body,
        )
    if resp.status_code == 404:
        return {"error": f"Ticket '{ticket_id}' not found"}
    if resp.status_code == 409:
        # Stale lease_epoch — surface the structured envelope so the
        # caller can re-fetch and retry.
        try:
            return {"error": "stale_lease_epoch", "detail": resp.json()}
        except Exception:
            return {"error": f"stale_lease_epoch: {resp.text}"}
    if resp.status_code == 403:
        return {"error": "Not authorized to update this ticket (need creator or project admin)"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


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


async def _handle_update_entry_confidence(args: dict) -> dict:
    """Wrap PUT /api/v1/projects/{project_id}/entries/{entry_id}/confidence.

    Updates a KB entry's confidence in [0.0, 1.0]. Validates args locally
    BEFORE resolving the project so bad input surfaces as a clear error
    rather than a DNS / network failure.
    """
    entry_id = args.get("id")
    if not isinstance(entry_id, int) or entry_id <= 0:
        return {"error": "id must be a positive integer"}

    confidence = args.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        return {"error": "confidence must be a number in [0.0, 1.0]"}
    if not (0.0 <= float(confidence) <= 1.0):
        return {"error": "confidence must be in [0.0, 1.0]"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{api_url}/api/v1/projects/{project_id}/entries/{entry_id}/confidence",
            json={"confidence": float(confidence)},
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Entry {entry_id} not found in project {project_id}"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_promote_entry(args: dict) -> dict:
    """Wrap PUT /api/v1/projects/{project_id}/entries/{entry_id}/promote.

    Server enforces quality gates (confidence ≥ 0.8, length ≥ 50, no
    near-duplicate). 422 surfaces the failing gates verbatim so callers
    can fix the entry and retry. 409 means already a claim.
    """
    entry_id = args.get("id")
    if not isinstance(entry_id, int) or entry_id <= 0:
        return {"error": "id must be a positive integer"}

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{api_url}/api/v1/projects/{project_id}/entries/{entry_id}/promote",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Entry {entry_id} not found in project {project_id}"}
    if resp.status_code == 409:
        return {"error": f"Entry {entry_id} is already a claim"}
    if resp.status_code == 422:
        return {"error": f"Promotion gates failed: {resp.text}"}
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_promote_eligible_entries(args: dict) -> dict:
    """Wrap POST /api/v1/projects/{project_id}/entries/bulk-promote.

    v0.10.12 tk_c64915570f4d4042 — bulk repair for stuck KB notes.
    Args validated locally BEFORE _resolve_project_id so bad input
    surfaces as a clear error, not a network failure.
    """
    body: dict = {}

    min_length = args.get("min_length")
    if min_length is not None:
        if not isinstance(min_length, int) or isinstance(min_length, bool):
            return {"error": "min_length must be an integer"}
        if min_length < 1 or min_length > 10_000:
            return {"error": "min_length must be in [1, 10000]"}
        body["min_length"] = min_length

    min_confidence = args.get("min_confidence")
    if min_confidence is not None:
        if isinstance(min_confidence, bool) or not isinstance(
            min_confidence, (int, float)
        ):
            return {"error": "min_confidence must be a number in [0.0, 1.0]"}
        if not (0.0 <= float(min_confidence) <= 1.0):
            return {"error": "min_confidence must be in [0.0, 1.0]"}
        body["min_confidence"] = float(min_confidence)

    set_confidence = args.get("set_confidence")
    if set_confidence is not None:
        if isinstance(set_confidence, bool) or not isinstance(
            set_confidence, (int, float)
        ):
            return {"error": "set_confidence must be a number in [0.0, 1.0]"}
        if not (0.0 <= float(set_confidence) <= 1.0):
            return {"error": "set_confidence must be in [0.0, 1.0]"}
        body["set_confidence"] = float(set_confidence)

    entry_type = args.get("entry_type")
    if entry_type is not None:
        if not isinstance(entry_type, str) or not entry_type.strip():
            return {"error": "entry_type must be a non-empty string"}
        body["entry_type"] = entry_type.strip()

    dry_run = args.get("dry_run", True)
    if not isinstance(dry_run, bool):
        return {"error": "dry_run must be a boolean"}
    body["dry_run"] = dry_run

    git_remote = args.get("git_remote", "")
    try:
        api_url, api_key, project_id = await _resolve_project_id(git_remote)
    except Exception as exc:
        return {"error": str(exc)}

    import httpx
    # Bulk operation — use a longer timeout. 270 entries × ~50 claims
    # near-dup compare is still trivial CPU work but allow headroom.
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{api_url}/api/v1/projects/{project_id}/entries/bulk-promote",
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code == 404:
        return {"error": f"Project {project_id} not found"}
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


# ---------------------------------------------------------------------------
# v0.10.9 handoff MCP handlers (8 tools)
# ---------------------------------------------------------------------------


def _handoff_api_config() -> tuple[str, str] | dict:
    """Return (api_url, api_key) or an error dict. Handoffs are not
    project-scoped, so unlike _resolve_project_id this doesn't need a
    git remote."""
    config = load_config()
    if not config.sync.api_key:
        return {"error": "Not authenticated. Run 'sfs auth login' first."}
    return config.sync.api_url.rstrip("/"), config.sync.api_key


async def _handle_create_handoff(args: dict) -> dict:
    session_id = args.get("session_id", "")
    if not session_id:
        return {"error": "session_id is required"}
    conf = _handoff_api_config()
    if isinstance(conf, dict):
        return conf
    api_url, api_key = conf

    body: dict[str, Any] = {"session_id": session_id}
    for key in (
        "recipient_email",
        "recipient_user_id",
        "recipient_team_id",
        "message",
        "ticket_id",
        "persona_name",
    ):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            body[key] = val.strip()
    if args.get("expires_in_hours") is not None:
        try:
            body["expires_in_hours"] = int(args["expires_in_hours"])
        except (TypeError, ValueError):
            return {"error": "expires_in_hours must be an integer"}
    if isinstance(args.get("attachments"), list):
        body["attachments"] = args["attachments"]

    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{api_url}/api/v1/handoffs",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_claim_handoff(args: dict) -> dict:
    handoff_id = args.get("handoff_id", "")
    if not handoff_id:
        return {"error": "handoff_id is required"}
    conf = _handoff_api_config()
    if isinstance(conf, dict):
        return conf
    api_url, api_key = conf

    import httpx

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{api_url}/api/v1/handoffs/{handoff_id}/claim",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_get_handoff(args: dict) -> dict:
    handoff_id = args.get("handoff_id", "")
    if not handoff_id:
        return {"error": "handoff_id is required"}
    conf = _handoff_api_config()
    if isinstance(conf, dict):
        return conf
    api_url, api_key = conf

    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/handoffs/{handoff_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_list_inbox_handoffs(args: dict) -> dict:
    include_team = args.get("include_team", True)
    conf = _handoff_api_config()
    if isinstance(conf, dict):
        return conf
    api_url, api_key = conf

    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/handoffs/inbox",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"include_team": str(bool(include_team)).lower()},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_list_sent_handoffs(args: dict) -> dict:
    conf = _handoff_api_config()
    if isinstance(conf, dict):
        return conf
    api_url, api_key = conf

    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{api_url}/api/v1/handoffs/sent",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_revoke_handoff(args: dict) -> dict:
    handoff_id = args.get("handoff_id", "")
    reason = args.get("reason", "")
    if not handoff_id:
        return {"error": "handoff_id is required"}
    if not reason:
        return {"error": "reason is required"}
    conf = _handoff_api_config()
    if isinstance(conf, dict):
        return conf
    api_url, api_key = conf

    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{api_url}/api/v1/handoffs/{handoff_id}/revoke",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"reason": reason},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_decline_handoff(args: dict) -> dict:
    handoff_id = args.get("handoff_id", "")
    if not handoff_id:
        return {"error": "handoff_id is required"}
    conf = _handoff_api_config()
    if isinstance(conf, dict):
        return conf
    api_url, api_key = conf

    body: dict[str, Any] = {}
    reason = args.get("reason")
    if isinstance(reason, str) and reason.strip():
        body["reason"] = reason.strip()

    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{api_url}/api/v1/handoffs/{handoff_id}/decline",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


async def _handle_add_handoff_comment(args: dict) -> dict:
    handoff_id = args.get("handoff_id", "")
    content = args.get("content", "")
    if not handoff_id:
        return {"error": "handoff_id is required"}
    if not content:
        return {"error": "content is required"}
    conf = _handoff_api_config()
    if isinstance(conf, dict):
        return conf
    api_url, api_key = conf

    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{api_url}/api/v1/handoffs/{handoff_id}/comments",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"content": content},
        )
    if resp.status_code >= 400:
        return {"error": f"API error {resp.status_code}: {resp.text}"}
    return resp.json()


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
