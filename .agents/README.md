# SessionFS Agent Team

Eight specialized agent personas for building SessionFS with AI coding agents.

## Agents

| Agent | File | Use For |
|-------|------|---------|
| **Atlas** (Backend Architect) | `atlas-backend.md` | API server, daemon, watchers, session spec, CLI, sync protocol, database |
| **Sentinel** (Security Engineer) | `sentinel-security.md` | Auth, threat modeling, encryption, access control, audit logging, data governance |
| **Forge** (DevOps Engineer) | `forge-devops.md` | CI/CD, Docker, Helm, GitHub Actions, package distribution, self-hosted deploy |
| **Prism** (Frontend Engineer) | `prism-frontend.md` | Web dashboard, VS Code extension, landing page |
| **Scribe** (Technical Writer) | `scribe-docs.md` | Documentation, quickstart guides, API reference, README, blog posts |
| **Ledger** (Revenue Engineer) | `ledger-revenue.md` | Stripe billing, pricing tiers, usage metering, subscription management |
| **Shield** (Compliance Officer) | `shield-compliance.md` | Data governance, compliance certifications, privacy, regulatory requirements |
| **Vault** (Licensing Architect) | `vault-licensing.md` | Open source licensing, IP protection, commercial licensing, contributor agreements |

## How to Use

1. Pick the agent that matches your task (see assignment matrix below)
2. Copy the agent's `.md` file content as the system prompt or prepend it to your task
3. Add your specific task brief after the agent persona

**Format for task briefs:**

```
[Paste contents of the agent .md file]

---

TASK: [What the agent should do]
CONTEXT: [Relevant background — paste PDD sections, prior spike results, etc.]
DELIVERABLES: [Concrete outputs with acceptance criteria]
```

## Phase 0 Assignment Matrix

| Task | Agent | Priority |
|------|-------|----------|
| Spike 1A: Claude Code session read | Atlas | P0 — Critical path |
| Spike 1B: Codex session read | Atlas | P0 — Critical path |
| Spike 2A: Claude Code session write-back | Atlas | P0 — Critical path |
| Spike 2B: Codex session write-back | Atlas | P0 — Critical path |
| Spike 3: Concurrent read during active session | Atlas | P0 — Critical path |
| Session spec JSON schema + validation | Atlas | P1 |
| FastAPI scaffold (auth + CRUD + PostgreSQL + S3) | Atlas | P1 |
| Threat model for daemon + sync architecture | Sentinel | P1 |
| GitHub repo setup + CI pipeline | Forge | P1 |
| Landing page | Prism | P2 |
| README.md + landing page copy | Scribe | P2 |

## Phase 1-3 Assignment Matrix

| Task | Agent |
|------|-------|
| Daemon with Claude Code + Codex watchers | Atlas |
| CLI (list, show, pull, resume, import, export) | Atlas |
| Auth implementation (API keys + OAuth) | Sentinel |
| Homebrew / PyPI distribution | Forge |
| Quickstart guide + spec reference + CLI reference | Scribe |
| Handoff workflow (CLI + email) | Atlas |
| Web dashboard v1 | Prism |
| Team workspaces + access control | Atlas + Sentinel |
| Stripe integration | Atlas |
| Audit logging | Sentinel |
| Docker Compose for self-hosted | Forge |
| Team handoff guide + dashboard docs | Scribe |
| Cursor + Gemini CLI watchers | Atlas |
| VS Code extension | Prism |
| Helm chart for Kubernetes | Forge |
| Watcher dev guide + self-hosting guide | Scribe |

## Adapted From

Agent personas adapted from [msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents) (MIT License), customized with SessionFS project context, architecture decisions, and domain-specific constraints.
