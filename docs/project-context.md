# Shared Project Context

Share a single source-of-truth document with every AI agent working on your codebase. Any teammate, on any device, using any AI tool with the SessionFS MCP server, gets the same project context.

## Quick Start

```bash
# From inside your git repo
sfs project init
sfs project edit          # Opens in $EDITOR

# Other teammates can now see it
sfs project show
```

## How It Works

1. **One document per repository** — matched by git remote URL (SSH and HTTPS both work)
2. **Shared across the team** — anyone with sessions in the repo can read it and add to it; the owner and the project's org members can change it (see [Who can change a project](#who-can-change-a-project))
3. **Accessible via MCP** — AI agents call `get_project_context` to read the document
4. **Free-form markdown** — write whatever your team needs

## Who can change a project

Anyone who can see a project can read it and add to it. Changing it is limited to its owner and its organization.

| Who | Read | Add (knowledge entries, tickets, comments, agent runs, feedback) | Change (settings, rules, wiki pages, context document, knowledge curation, personas, work queues) |
|---|---|---|---|
| Project owner | Yes | Yes | Yes |
| Member of the project's organization (any role) | Yes | Yes | Yes |
| Anyone else who synced a session from the same repository | Yes | Yes | No |

What people in the last row add waits for the owner or an org member:

- Their knowledge entries stay notes (confidence at most 0.7), are never promoted or allowed to supersede other entries automatically at compile time, and so don't reach the context document or compiled rules files until an owner or org member promotes them. Bulk promote leaves their notes out unless `include_reader_notes` is set. An `add_knowledge` call with `upsert=true` (which retires the other entries in its slot) needs change rights.
- Their tickets start as *suggested*. The assignee isn't notified and autonomous work queues don't pick the ticket up until an owner or org member approves it.
- They can keep editing tickets they created. Editing one after it was approved sends it back to *suggested* for approval again. Once work has started, or a work queue has picked the ticket up, they can only change its reference lists (context refs, file refs, related sessions). A work queue doesn't hand out a ticket that is back in *suggested* (or cancelled) until an owner or org member approves it again.
- Their comments are visible to people in the dashboard, API and MCP tools, but aren't passed to automated agents: work queues leave them out of the comments they send to agents, and they don't start a reviewer's turn.
- On tickets they can dismiss, reopen or accept tickets they created (but not accept work they resolved themselves), and start, complete, block or unblock tickets they created or resolved. Approving a suggested ticket needs change rights.
- They can cancel or complete agent runs they triggered, but not anyone else's.
- A rules compile they run returns the compiled files without adding a version to the rules history.
- Project-scope skill install requests need change rights; personal (user or device) requests don't. A change they try gets `403 project_edit_forbidden`, and the dashboard hides the controls they can't use (the project response carries `can_edit`). Renaming, linking repositories, merging, transferring and deleting still need project-admin standing (the owner, or an org owner or admin). Service keys keep their own boundary: the key's organization and project allowlist.

## CLI Commands

### `sfs project init`

Create a project context for the current repository. Detects the git remote automatically.

```bash
cd ~/code/my-project
sfs project init
# Project created for myorg/my-project
# Run 'sfs project edit' to add context.
```

### `sfs project edit`

Open the context document in your editor. Changes are uploaded on save.

```bash
sfs project edit
# Opens in $EDITOR (default: nano)
```

Set your preferred editor:

```bash
export EDITOR=vim
# or
export EDITOR="code --wait"
```

### `sfs project show`

Display the current project context with metadata.

```bash
sfs project show
# Project: my-project
# Remote:  myorg/my-project
# Updated: 2026-03-26
#
# # Architecture
# ...
```

### `sfs project set`

Rename the project (and/or toggle auto-narrative). Renaming requires
project-admin standing (project owner or org admin).

```bash
sfs project set --name "Acme Platform"
# name = Acme Platform
```

The new name appears in `sfs project show` and the dashboard. This
renames the **project**, not a session (see `sfs session rename` for
that).

### `sfs project compile-schedule`

Choose when the project's knowledge is compiled into the context document
without anyone clicking **Compile**: `nightly`, `weekly` or `off`, optionally
with a pending-claim threshold that triggers a compile on its own. The same
setting is on the dashboard under the project's **Settings → Scheduled KB
compile**. Scheduled compiles use the deterministic compiler (no LLM).

```bash
sfs project compile-schedule weekly --after-claims 20
sfs project compile-schedule off --no-after-claims
```

Projects created from v0.23.0 on start at `nightly` when the owner's plan
includes project context; existing projects start at `off`. The project
Overview shows when the context was last compiled and how many claims are
waiting, and offers a one-click compile when it is out of date. On a
self-hosted server the schedule only takes effect once the operator runs the
[compile sweep](self-hosted.md#scheduled-kb-compile-sweep).

### `sfs project set-context`

Set the context from a file (useful for CI or scripting).

```bash
sfs project set-context ./PROJECT_INSTRUCTIONS.md
# Project context updated (2.4 KB).
```

### `sfs project get-context`

Output raw markdown to stdout (useful for piping).

```bash
sfs project get-context > local-copy.md
```

## MCP Integration

When connected to the SessionFS MCP server, AI agents can call `get_project_context` to read the shared document. This works with any MCP-aware tool (Claude Code, Cursor, etc.).

The tool auto-detects the git remote from the current working directory; you can also pass it explicitly as an argument.

## What to Include

The default template provides a starting structure:

```markdown
# Project Context

## Overview
What is this project? One paragraph.

## Architecture
Tech stack, infrastructure, key services.

## Conventions
Coding standards, branch strategy, PR process.

## API Contracts
Key endpoints, request/response formats.

## Key Decisions
Important decisions that are locked and shouldn't be revisited.

## Team
Who works on what.
```

Write whatever is useful for your team. Some ideas:

- Database schema overview
- Deployment process
- Testing conventions
- Security requirements
- Third-party service credentials (names only, not values)
- Links to external documentation

## Access Control

- **Read:** Any authenticated user with sessions in the repository
- **Write:** Same as read (team trust model)
- **Owner:** The user who ran `sfs project init`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/projects/` | Create a project context |
| GET | `/api/v1/projects/{remote}` | Get project context by git remote |
| PATCH | `/api/v1/projects/{id}` | Rename the project (`{"name": "…"}`, project-admin only) |
| PUT | `/api/v1/projects/{remote}/context` | Update the context document |
| GET | `/api/v1/projects/{id}/compile-status` | Last compile, pending claims and whether a compile is recommended |

## Related: Project Rules

Project context and project rules are adjacent features. Context is the *descriptive* knowledge about your codebase (what the architecture is, how the data flows, what the team conventions are). Rules are the *prescriptive* instructions compiled into `CLAUDE.md`, `codex.md`, `.cursorrules`, and the other tool-specific files each AI agent reads.

Selected sections of this project context document are injected into compiled rules by default (see `sfs rules compile`). See [Rules Portability](rules.md) for the full reference.
