# SessionFS — Pricing & Tier Design

Internal product reference for tiers, feature matrix, build gaps, and pricing-page requirements. For how SessionFS compares to adjacent products, see [Positioning](positioning.md).

## The Business Model

SessionFS monetizes team collaboration features on top of a free individual capture tool.

**The free tier is the distribution channel.**  
Every individual developer who installs SessionFS is a potential vector into their team. The conversion event is the first time a developer hands off a session to a teammate and the teammate experiences the value.

**The Team tier is the product.**  
Tech leads and engineering managers pay because they get visibility into their team's AI-assisted work, the ability to take over stuck sessions, and an audit trail for compliance.

---

## Tier Structure

### Free — $0 forever

**For:** Individual developers who want to capture and manage their own sessions.

**Includes:**

- Daemon capture (unlimited local capture, always)
- CLI: list, show, resume, export (markdown, .sfs), checkpoint, fork
- Cloud sync: up to **25 sessions** in the cloud
- Dashboard access: browse your own sessions only
- Import from Claude Code and Codex
- 1 device syncing to cloud

**Limits:**

- 25 cloud-synced sessions (local capture is unlimited and never capped)
- No sharing or handoff
- No team features
- Single user only
- Community support (GitHub issues)

**Why 25?** It's enough to experience the full loop — capture, sync, pull on another machine, resume. It's not enough for daily use across months. When a developer hits 25 and wants to keep syncing, they either delete old sessions or upgrade. But the real conversion happens when they try to share a session with a teammate and can't.

---

### Pro — $12/month (billed annually: $10/month)

**For:** Individual developers who want unlimited sync and basic sharing.

**Includes everything in Free, plus:**

- Unlimited cloud-synced sessions
- Up to **3 devices** syncing
- Share sessions via link (read-only, up to 5 active share links)
- Export to all formats (Claude Code, Codex, OpenAI, markdown)
- Session search (full-text across all sessions)
- Email support

**Why this tier exists:** Some developers will pay for personal convenience before their team adopts. This captures that revenue without requiring team buy-in. The share links also serve as a teaser for team features — when a developer shares a session link with a colleague who isn't on SessionFS, that colleague becomes a new user.

---

### Team — $20/user/month (billed annually: $16/user/month)

**Minimum 3 seats.**

**For:** Engineering teams (3–50 developers) who need session visibility, handoff, and collaboration.

**Includes everything in Pro, plus:**

- **Team workspace**: shared session library visible to all team members
- **Handoff**: transfer a session to a teammate with a message — they get an email and can pull + resume immediately
- **Team dashboard**: see all team sessions, filter by member, tool, date
- **Role-based access**: Admin (manage team, billing), Member (capture, handoff, browse team sessions)
- **Session permissions**: owner controls who can see/resume/fork each session
- **Audit log**: who accessed which sessions, when, from where
- **SSO (Google Workspace / GitHub org)**: team members authenticate with existing accounts
- **Priority support**: 24-hour response time
- Unlimited devices per user
- Unlimited share links

**This is the core revenue tier.** The handoff workflow is the thing you can't get anywhere else and can't work around with copy-paste. The team dashboard gives the tech lead the visibility they need to justify the spend.

---

### Enterprise — Custom pricing (starting ~$35/user/month)

**Minimum 20 seats.**

**For:** Large engineering organizations with compliance and security requirements.

**Includes everything in Team, plus:**

- **Self-hosted deployment**: run the entire SessionFS stack in your own infrastructure
- **SAML/OIDC SSO**: integrate with your enterprise identity provider
- **Data residency**: choose cloud region (US, EU, APAC) for session storage
- **DLP integration**: webhook before sync for enterprise DLP tools to scan session content
- **Retention policies**: auto-delete sessions after N days, enforced at the org level
- **Session classification**: tag sessions with sensitivity levels, restrict handoff of "confidential" sessions
- **IP allowlisting**: restrict API access to corporate networks
- **Advanced audit**: SIEM-compatible log export (JSON, Splunk format)
- **Dedicated support**: Slack channel, named account manager
- **Custom onboarding**: help setting up daemon deployment across the org
- **SLA**: 99.9% uptime guarantee on managed cloud

---

## Feature Matrix

| Feature | Free | Pro | Team | Enterprise |
|---------|:----:|:---:|:----:|:----------:|
| **Capture** |
| Local daemon capture | Unlimited | Unlimited | Unlimited | Unlimited |
| Claude Code watcher | ✓ | ✓ | ✓ | ✓ |
| Codex watcher | ✓ | ✓ | ✓ | ✓ |
| Cursor watcher | ✓ | ✓ | ✓ | ✓ |
| Gemini CLI watcher | ✓ | ✓ | ✓ | ✓ |
| **CLI** |
| list, show, resume | ✓ | ✓ | ✓ | ✓ |
| checkpoint, fork | ✓ | ✓ | ✓ | ✓ |
| export (markdown) | ✓ | ✓ | ✓ | ✓ |
| export (all formats) | — | ✓ | ✓ | ✓ |
| Full-text search | — | ✓ | ✓ | ✓ |
| **Cloud Sync** |
| Cloud-synced sessions | 25 | Unlimited | Unlimited | Unlimited |
| Devices syncing | 1 | 3 | Unlimited | Unlimited |
| **Sharing** |
| Share via link (read-only) | — | 5 links | Unlimited | Unlimited |
| Handoff to teammate | — | — | ✓ | ✓ |
| **Dashboard** |
| Personal session browser | ✓ | ✓ | ✓ | ✓ |
| Team session browser | — | — | ✓ | ✓ |
| Team management | — | — | ✓ | ✓ |
| Handoff feed | — | — | ✓ | ✓ |
| **Admin & Security** |
| Audit log | — | — | ✓ | ✓ |
| SSO (Google/GitHub) | — | — | ✓ | ✓ |
| SAML/OIDC SSO | — | — | — | ✓ |
| Self-hosted | — | — | — | ✓ |
| Data residency | — | — | — | ✓ |
| DLP integration | — | — | — | ✓ |
| Retention policies | — | — | — | ✓ |
| IP allowlisting | — | — | — | ✓ |
| SIEM log export | — | — | — | ✓ |
| **Support** |
| Community (GitHub) | ✓ | ✓ | ✓ | ✓ |
| Email support | — | ✓ | ✓ | ✓ |
| Priority support (24h) | — | — | ✓ | ✓ |
| Dedicated Slack + AM | — | — | — | ✓ |
| SLA (99.9%) | — | — | — | ✓ |

---

## Pricing Psychology

**Free → Pro conversion trigger:** Developer hits 25 session cloud limit, or wants to share a session link with a colleague.

**Pro → Team conversion trigger:** Developer shares a session link, the recipient says "we should all be using this." Or the tech lead sees the developer using it and wants team-wide visibility.

**Team → Enterprise conversion trigger:** InfoSec team says "we need this self-hosted" or "we need SAML SSO" or "we need data residency."

---

## What We Need To Build For Each Tier

### Already built (Phase 1)

- Daemon capture ✓
- CLI (core commands: list, show, resume, export, fork, checkpoint, import, daemon, config, cloud sync) ✓
- Cloud sync ✓
- API auth (signup + API keys) ✓
- Export (markdown, .sfs, Claude Code format) ✓
- Checkpoint, fork ✓
- Self-hosted API server (PostgreSQL, object storage, session APIs) ✓

### Shipped (v0.1.0)

- **Web dashboard (personal)** — browser-based session management at localhost:8000.
- **Four-tool capture** — Claude Code, Codex, Gemini CLI, and Cursor watchers all shipping.
- **Cross-tool resume** — resume sessions across Claude Code, Codex, and Gemini CLI.

### Needs to be built for Pro

- Session count enforcement (25 limit on Free, unlimited on Pro)
- Device count tracking and enforcement
- Share link generation and rendering (read-only session viewer without login)
- Full-text session search
- Stripe integration for Pro billing

### Needs to be built for Team

- Team workspaces (create team, invite members, shared session library)
- Handoff workflow with email notifications
- Team dashboard view (all team members' sessions)
- Role-based access (Admin, Member)
- Session permissions (owner controls visibility)
- Audit log viewer in dashboard
- SSO (Google Workspace, GitHub org)
- Stripe integration for per-seat Team billing

### Needs to be built for Enterprise

- Self-hosted deployment (Docker Compose + Helm)
- SAML/OIDC integration
- Data residency configuration
- DLP webhook integration
- Retention policy engine
- Session classification
- IP allowlisting
- SIEM log export
- Everything from Team

---

## Revenue Projections

### Conservative scenario (12 months post-launch)

**Free users:** 5,000

- Conversion to Pro: 3% = 150 Pro subscribers
- Pro revenue: 150 × $12/mo = $1,800/mo

**Pro → Team conversion:** 10% of Pro users advocate for team adoption

- 15 teams × average 6 seats × $20/seat = $1,800/mo

**Direct Team signups** (from HN, word of mouth, blog posts):

- 30 teams × average 8 seats × $20/seat = $4,800/mo

**Enterprise:** 2 contracts × ~$2,500/mo average = $5,000/mo

**Total month 12:** ~$13,400 MRR = ~$161K ARR

### Growth scenario (18 months)

**Free:** 20,000 users  
**Pro:** 500 subscribers ($6,000/mo)  
**Team:** 200 teams, avg 8 seats ($32,000/mo)  
**Enterprise:** 10 contracts ($25,000/mo)

**Total month 18:** ~$63,000 MRR = ~$756K ARR

---

## Pricing Page Design Requirements

The pricing page needs to communicate three things in 5 seconds:

1. Free for individual developers (install and use today)
2. Teams pay for handoff and visibility
3. Enterprise gets self-hosted and compliance

**Visual design:** 4 columns (Free, Pro, Team, Enterprise). Team column highlighted as "Most Popular." Each column shows price, key features (5–7 bullets max), and a CTA button. Free = "Install Now." Pro = "Start Free Trial." Team = "Start Free Trial." Enterprise = "Contact Sales."

**Free trial:** 14 days of Team features for any new signup. After 14 days, downgrade to Free unless they enter payment. This lets developers experience the team features before committing.

---

## Related docs

- [Positioning](positioning.md)
- [Sync Guide](sync-guide.md)
- [Security spec](security/security-spec.md)
