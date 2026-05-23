# SessionFS Agent Team

Specialized agent personas for building and operating SessionFS with AI coding agents.

The server-side persona store is authoritative. Local `.agents/*.md` files are checkout conveniences for tools that read persona files directly. Use `sfs persona pull --all --force` to refresh local copies after server-side persona edits.

## Agents

| Agent | File | Use For |
|-------|------|---------|
| **Atlas** (Backend Architect) | `atlas-backend.md` | API server, daemon, sync, MCP/API/CLI parity, migrations, database |
| **Codex Reviewer** (Product Reviewer) | server-only | Release and implementation review for SessionFS itself |
| **Sentinel** (Security Engineer) | `sentinel-security.md` | Auth, threat modeling, tenant isolation, secrets, rate limits, API keys |
| **Forge** (DevOps and Platform Engineer) | `forge-devops.md` | CI/CD, Docker, Helm, GCP, release gates, observability, deployment safety |
| **Prism** (Product UI and Frontend Lead) | `prism-frontend.md` | Dashboard, site implementation, frontend API integration, product UX |
| **Scribe** (Documentation and Positioning Lead) | `scribe-docs.md` | Docs, API references, changelogs, release notes, source-backed claims |
| **Herald** (Marketing Strategy and Growth Lead) | `herald-marketing.md` | Positioning, launches, growth experiments, developer relations, copy strategy |
| **Relay** (Customer Success and Support Lead) | `relay-customer.md` | Onboarding, support triage, health checks, renewals, customer feedback |
| **Ledger** (Revenue and Entitlements Engineer) | `ledger-revenue.md` | Stripe billing, tiers, entitlements, metering, subscription lifecycle |
| **Steward** (Finance and Operations Lead) | `steward-finance.md` | Finance snapshots, runway, costs, vendor ops, fundraising prep, equity scenarios |
| **Shield** (Compliance and Governance Lead) | `shield-compliance.md` | DLP policy, audit evidence, compliance posture, retention, governance language |
| **Vault** (Licensing and IP Protection Lead) | `vault-licensing.md` | Open-core boundaries, licensing, packaging, IP protection, commercial artifacts |
| **Counsel** (Startup IP and Legal Strategy Lead) | `counsel-startup.md` | Legal research prep, IP evidence packets, contract review notes, attorney questions |
| **Scout** (Competitive Intelligence and Market Analyst) | `scout-competitive-intelligence.md` | Competitor monitoring, market signals, pricing intelligence, threat assessment, multi-source scouting workflows |

## How to Use

Preferred:

1. Create or pick up a SessionFS ticket assigned to the right persona.
2. Run `sfs ticket start <ticket_id>` or the MCP `start_ticket` tool.
3. Let SessionFS load the assigned persona, ticket context, KB claims, rules, dependencies, and recent comments.

Fallback for tools without SessionFS integration:

1. Pick the agent that matches your task.
2. Copy the agent's `.md` file content as the system prompt or prepend it to your task.
3. Add your specific task brief after the agent persona.

**Format for task briefs:**

```
[Paste contents of the agent .md file]

---

TASK: [What the agent should do]
CONTEXT: [Relevant background — paste PDD sections, prior spike results, etc.]
DELIVERABLES: [Concrete outputs with acceptance criteria]
```

## Assignment Matrix

| Task | Agent |
|------|-------|
| Backend routes, migrations, sync, MCP/API/CLI behavior | Atlas |
| Implementation or release review | Codex Reviewer |
| Security model, auth, API keys, tenant isolation | Sentinel |
| GCP, Cloud Run, Helm, CI/CD, release automation | Forge |
| Dashboard, site UI, product UX | Prism |
| Docs, changelog, API reference, release notes | Scribe |
| Positioning, launch, developer relations, campaigns | Herald |
| Customer onboarding, support, renewals, feedback | Relay |
| Billing, Stripe, tiers, entitlement enforcement | Ledger |
| Finance, runway, costs, fundraising prep | Steward |
| Compliance evidence, DLP policy, audit posture | Shield |
| Licensing, open-core boundary, packaging/IP | Vault |
| Legal research prep, contracts, trademark/patent evidence | Counsel |
| Competitor monitoring, market signals, pricing intelligence, scout workflows | Scout |

## Persona Hygiene

Keep reusable personas durable and low-risk:

- Personas should contain role behavior, ownership, invariants, handoff rules, and deliverable contracts.
- Volatile facts belong in KB entries, tickets, account dossiers, or finance reports.
- Do not embed customer hostnames, user names, license keys, private pricing exceptions, cash/runway numbers, legal strategy, or contract terms in reusable persona files.
- If a persona needs current facts, it should explicitly load scoped context through `get_ticket`, `search_project_knowledge`, or dedicated customer/finance/legal tickets.

## Adapted From

Agent personas adapted from [msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents) (MIT License), customized with SessionFS project context, architecture decisions, and domain-specific constraints.
