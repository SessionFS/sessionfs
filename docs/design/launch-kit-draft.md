# Launch kit draft — Show HN / Product Hunt (tk_88dd3b01e5894dd1) — INTERNAL

**Status: DRAFT for CEO review. Nothing here is posted — the CEO fires.**
Blocked on: P0 quality bar (done), listings live (Beacon ticket), a clean-
machine dry run of the public instructions.

## Show HN draft
Title: Show HN: SessionFS — your AI coding agent never starts from zero again
Body (skeleton):
- The itch: every Claude Code/Codex/Cursor session starts blank; context
  dies with the window. We capture sessions from 9 tools into one portable
  format and feed them back — resume in a different tool, ask "what did we
  do last session?", hand a session to a teammate.
- Demo: `pipx install sessionfs && sfs init` — the wizard captures your most
  recent session and tells you what to ask your agent.
- Honest notes for HN: local-first by default; cloud sync opt-in; anonymous
  telemetry with prominent opt-out (link the disclosure doc); macOS/Linux
  today, Windows planned; MIT core + FSL for the enterprise bits.
- First comment (pre-written): architecture summary (fsevents/inotify daemon,
  canonical .sfs format, MCP server) + known limitations.

## Product Hunt draft
Tagline: Memory for your AI coding agents
Gallery: terminal recording (install → init magic moment → cross-tool
resume) + dashboard shots. Maker comment: the wedge story, 2 paragraphs.

## FAQ/comment prep (both venues)
- "How is this different from Claude Code's own resume?" → cross-TOOL + team
  + queryable memory; native resume is single-tool, single-machine.
- Privacy/telemetry pushback → link docs/telemetry.md; opt-outs; self-hosted
  telemetry goes to YOUR server.
- "Does it read my code?" → captures the session transcripts your tools
  already store locally; DLP scrubbing on anything shared/public.
- Security posture → reviews, DLP gates, no server-side LLM keys.

## Dry run checklist (before firing)
- [ ] Clean macOS VM: follow the public quickstart verbatim → magic moment
- [ ] Clean Ubuntu container: same
- [ ] All README/site links resolve; pricing page current
- [ ] Terminal recording ≤90s, no real keys on screen
- [ ] CEO sign-off recorded in the ticket
