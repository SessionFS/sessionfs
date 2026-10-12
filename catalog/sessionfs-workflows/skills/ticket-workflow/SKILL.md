---
name: ticket-workflow
description: Work a SessionFS ticket from start to review. Use when the user gives you a ticket id (tk_...), asks what to work on next, or asks you to pick up, update, or finish a ticket.
---

# Ticket workflow

SessionFS tickets carry the description, acceptance criteria, file references,
and notes from finished dependencies, so the agent starts with full context.

## Steps

1. Load the ticket with the `start_ticket` MCP tool. It returns the assigned
   persona, the acceptance criteria, related knowledge, and a `lease_epoch`.
   Keep the epoch and pass it to later ticket calls. If you only need to read
   the ticket, use `get_ticket` instead; `start_ticket` marks it in progress.
2. Restate the acceptance criteria as a checklist before changing code. Ask
   the user if any criterion is ambiguous.
3. Do the work. Post progress, questions, and blockers with
   `add_ticket_comment` rather than leaving them only in the chat.
4. Verify every acceptance criterion, including tests, before finishing.
5. Finish with `complete_ticket`: summarise what changed, list
   `changed_files`, and note any follow-up. The ticket moves to review; it is
   not done until a reviewer resolves it.

## Rules

- Do not mark a ticket complete with failing tests or unmet criteria. Comment
  with what is left instead.
- If the ticket is blocked on someone else, say so in a comment and stop.
- Record anything a future agent should know with the project-knowledge skill.
