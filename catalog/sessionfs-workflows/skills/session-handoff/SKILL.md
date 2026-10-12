---
name: session-handoff
description: Hand the current AI coding session to a teammate or another tool with SessionFS, or pick up a session someone handed to you. Use when the user says hand off, pass this to, continue in another tool, or shares a handoff id (hnd_...).
---

# Session handoff

SessionFS captures the conversation, tool calls, and workspace context of an
AI coding session so someone else, or another tool, can continue it.

## Handing a session off

1. Write a short message for the recipient: what is done, what is next, and
   anything that is blocked or risky. Do not include secrets.
2. Run `sfs handoff <session_id> --to <email> -m "<message>"`. It uploads the
   session as it is now and then creates the handoff, so finish or pause the
   current step first.
3. If the work belongs to a ticket, add `--ticket <ticket_id>` so the
   recipient inherits it. Attach the knowledge entries or wiki pages they need
   with `--attach kb_entry:<id>` or `--attach wiki_page:<slug>`.
4. Report the handoff id and link back to the user.

The `create_handoff` MCP tool takes the same fields but does not upload
anything. Use it only for a session that is already synced, or run
`sfs push <session_id>` first; otherwise it fails with "Session not found".

## Picking a session up

1. List what is waiting with the `list_inbox_handoffs` MCP tool, or read one
   with `get_handoff` when the user gives you an id.
2. Run `sfs pull-handoff <hnd_id> --in <tool>` (claude-code, codex, copilot,
   gemini). It claims the handoff and downloads the session; then run the
   `sfs resume <session_id> --in <tool>` command it prints to continue. The
   `claim_handoff` MCP tool only claims it in the cloud; it does not download
   anything.
3. Read the sender's message and any attachments before acting. Confirm the
   next step with the user if the message leaves it unclear.

## Rules

- Only hand a session to someone the user named. Never guess a recipient.
- Handoffs expire (7 days by default). Mention the expiry if the recipient is
  not expected to start soon.
