# Positioning

How SessionFS fits next to native AI tools, adjacent products, and common substitutes. For product behavior and commands, see the [README](../README.md), [Quickstart](quickstart.md), and [CLI Reference](cli-reference.md).

## Category

SessionFS is a **portable session layer for coding agents**: it watches native tools, normalizes conversations into a canonical `.sfs` format, indexes them locally, and optionally syncs via HTTP. It does **not** replace your chat UI—you keep using your editor and agent; SessionFS captures and moves **session data** (history, workspace context, tool metadata) for browse, resume, fork, export, and handoff.

## One-line positioning

**We don’t replace your AI coding tool; we make agent sessions portable, syncable, and handoff-ready across tools and teammates.**

## Native tools (Cursor, Claude Code, Codex, Copilot, etc.)

Vendors optimize for the best experience **inside** their product. History and storage are typically tied to that stack.

SessionFS optimizes for **ownership and portability**: a stable, documented representation you can move across machines and (with sync) share with a defined conflict model. Multi-tool and multi-machine workflows are the core story—not a better inline chat.

**Platform risk:** If a single vendor ships excellent first-party export, sync, and sharing, solo users who never leave that stack may feel less urgency. SessionFS’s durable angle is **cross-tool reality** and **explicit teammate handoff**, not winning the default chat UX.

## Export and documentation tools

Many tools turn conversations into markdown, docs, or ad hoc files. That overlaps on “get content out of the UI,” but the focus is often **narrative or publishing**.

SessionFS treats the session as **infrastructure**: append-only `messages.jsonl`, manifest and workspace metadata, CLI and optional API—not primarily a prettier document. Markdown export is one output on top of a canonical session object.

## Agent memory and RAG systems

Memory products (retrieval stacks, vector stores, “remember this for the model”) optimize for **what the model should recall next**.

SessionFS optimizes for **what happened**, in order, with workspace and tool context—so humans and tools can **resume, audit, fork, or hand off** a thread. The two are often **complementary**: memory answers retrieval; SessionFS answers reproducibility and transfer of a full session artifact.

## Enterprise governance and observability

Gateways and observability stacks focus on **policy, routing, cost, and request-level audit**.

SessionFS is **session-centric data**: git-relative paths, tool traces, and handoff-oriented operations. A natural narrative is governance on the **control plane**, SessionFS on the **session artifact**—with security and key handling documented under [security/](security/).

## Full-desktop or lifelogging products

Broad capture tools optimize for “never lose anything” across the whole machine.

SessionFS is **narrower**: agent sessions and workspace-relative state, with a clearer scope for consent and team policy than whole-desktop recording.

## Git, tickets, and written specs

Commits and design docs remain the system of record for **intent and code**. They rarely preserve **turn-level conversation, tool calls, and compaction boundaries**—the layer people mean when they say “pick up where I left off” in an agent thread.

SessionFS does not replace Git; it holds what repositories and tickets were never meant to store as structured, replayable session data.

## Summary

| Dimension | Native tools | Export / docs | Memory / RAG | Governance | SessionFS |
|-----------|--------------|---------------|--------------|------------|-----------|
| Primary unit | Vendor session blob | Narrative / file | Facts / chunks | Requests / policy | Canonical `.sfs` session |
| Cross-tool | Weak | Varies | App-specific | Stack-specific | **Core** |
| Team handoff | Often awkward | Usually manual | Rarely the goal | Compliance-first | **Primary team wedge** |
| Network default | Vendor cloud | Often local | Varies | Often cloud | **Local-first; sync opt-in** |

## Risks and trust

- **Category clarity:** Buyers may not search for “session sync”; they feel pain as lost context, slow onboarding, or “I can’t continue on another machine.” Messaging should lead with outcomes.
- **Trust:** Full sessions may contain secrets and sensitive code. See [security/security-spec.md](security/security-spec.md) and [security/threat-model.md](security/threat-model.md); operational posture matters as much as feature lists.

## Related docs

- [Pricing & tiers](pricing.md) — internal tier design and feature matrix
- [Sync Guide](sync-guide.md) — opt-in cloud sync and self-hosting
- [Security spec](security/security-spec.md) — keys, scopes, and server expectations
