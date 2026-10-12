---
name: project-knowledge
description: Read and contribute to the SessionFS project knowledge base. Use at the start of work on a repository to load context, and whenever you learn a decision, convention, pattern, bug, or dependency a future agent would need.
---

# Project knowledge

The SessionFS knowledge base is shared by every agent and teammate on the
project. Reading it first avoids repeating past mistakes; writing to it saves
the next agent from rediscovering what you learned.

## Before starting

- Load the compiled project context with `get_project_context`.
- Search for the area you are about to touch with `search_project_knowledge`.
  Follow recorded conventions unless the user tells you otherwise.

## While working

Add an entry with `add_knowledge` as soon as you learn something durable, not
at the end of the session. Pick the type that fits:

| Type | Example |
| --- | --- |
| decision | "Auth resolves the effective tier from the org, not the user record." |
| convention | "All commits use the project bot as author." |
| pattern | "Converters parse native format, then write canonical .sfs." |
| bug | "SQLite WAL can corrupt on power loss; the index self-heals." |
| dependency | "Markdown is rendered with react-markdown." |
| discovery | Anything else a future session would want to know. |

Write one fact per entry, specific enough to act on, with the file or
component it concerns. Search first so you do not add a duplicate.

For a longer explanation, such as an architecture overview or a runbook,
write a wiki page with `update_wiki_page`.

## Rules

- Never record secrets, credentials, or customer data.
- Record what is true now. If an entry is wrong, say so in a new entry rather
  than repeating the mistake.
