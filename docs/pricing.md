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
- Cloud sync with **14-day rolling retention**
- Dashboard access: browse your own sessions only
- Import from Claude Code and Codex
- 1 device syncing to cloud

**Limits:**

- Cloud sessions expire after 14 days (local capture is unlimited and never capped)
- No sharing or handoff
- No team features
- Single user only
- Community support (GitHub issues)

**Why 14 days?** It's enough to experience the full loop — capture, sync, pull on another machine, resume. It's not enough for daily use across months. When a developer's older sessions start expiring and they want permanent cloud storage, they upgrade. But the real conversion happens when they try to share a session with a teammate and can't.

---

### Pro — $12/month (billed annually: $10/month)

**For:** Individual developers who want unlimited sync and basic sharing.

**Includes everything in Free, plus:**

- **Unlimited cloud retention** — sessions never expire
- Up to **3 devices** syncing
- Share sessions via link (read-only, up to 5 active share links)
- Export to all formats (Claude Code, Codex, OpenAI, markdown)
- Full-text cloud search across all sessions
- Email support

**Why this tier exists:** Some developers will pay for personal convenience before their team adopts. This captures that revenue without requiring team buy-in. The share links also serve as a teaser for team features — when a developer shares a session link with a colleague who isn't on SessionFS, that colleague becomes a new user.

---

### Team — $20/user/month (billed annually: $16/user/month)

**Minimum 3 seats.**

**For:** Engineering teams (3–50 developers) who need session visibility, handoff, and collaboration.

**Includes everything in Pro, plus:**

- **Team workspace**: shared session library visible to all team members
- **Handoff**: transfer a session to a teammate with email notification — they pull + resume immediately
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
| Copilot CLI watcher | ✓ | ✓ | ✓ | ✓ |
| Amp watcher | ✓ | ✓ | ✓ | ✓ |
| Cline watcher | ✓ | ✓ | ✓ | ✓ |
| Roo Code watcher | ✓ | ✓ | ✓ | ✓ |
| **CLI** |
| list, show, resume | ✓ | ✓ | ✓ | ✓ |
| checkpoint, fork | ✓ | ✓ | ✓ | ✓ |
| export (markdown) | ✓ | ✓ | ✓ | ✓ |
| export (all formats) | — | ✓ | ✓ | ✓ |
| Full-text search | ✓ (local) | ✓ (local + cloud) | ✓ (local + cloud) | ✓ (local + cloud) |
| **Cloud Sync** |
| Cloud retention | 14 days | Unlimited | Unlimited | Unlimited |
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

## Ship Status

### Shipped

- Eight-tool daemon capture (Claude Code, Codex, Gemini CLI, Cursor, Copilot CLI, Amp, Cline, Roo Code)
- CLI (core commands: list, show, resume, export, fork, checkpoint, import, daemon, config, cloud sync, handoff, search)
- Cloud sync with push/pull and ETag conflict detection
- Email verification for cloud accounts
- Rolling 14-day retention enforcement for free tier
- Web dashboard with session management and full-text search
- MCP server for AI tool integration
- Cross-tool resume (Claude Code, Codex, Gemini CLI, Copilot CLI)
- Session search (local and cloud)
- Team handoff with email notification
- Self-hosted API server (PostgreSQL, S3/GCS)

### Planned

- Stripe billing integration (Pro and Team)
- Share link passwords
- SSO (Google Workspace, GitHub org)
- Audit log viewer in dashboard
- VS Code extension
- Device count enforcement
- Session similarity and duplicate detection
- Cost analytics dashboard

---

## Pricing Psychology

**Free -> Pro conversion trigger:** Developer's older sessions start expiring after 14 days, or they want to share a session link with a colleague.

**Pro -> Team conversion trigger:** Developer shares a session link, the recipient says "we should all be using this." Or the tech lead sees the developer using it and wants team-wide visibility.

**Team -> Enterprise conversion trigger:** InfoSec team says "we need this self-hosted" or "we need SAML SSO" or "we need data residency."

---

## Revenue Projections

### Conservative scenario (12 months post-launch)

**Free users:** 5,000

- Conversion to Pro: 3% = 150 Pro subscribers
- Pro revenue: 150 x $12/mo = $1,800/mo

**Pro -> Team conversion:** 10% of Pro users advocate for team adoption

- 15 teams x average 6 seats x $20/seat = $1,800/mo

**Direct Team signups** (from HN, word of mouth, blog posts):

- 30 teams x average 8 seats x $20/seat = $4,800/mo

**Enterprise:** 2 contracts x ~$2,500/mo average = $5,000/mo

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

**Visual design:** 4 columns (Free, Pro, Team, Enterprise). Team column highlighted as "Most Popular." Each column shows price, key features (5-7 bullets max), and a CTA button. Free = "Install Now." Pro = "Start Free Trial." Team = "Start Free Trial." Enterprise = "Contact Sales."

**Free trial:** 14 days of Team features for any new signup. After 14 days, downgrade to Free unless they enter payment. This lets developers experience the team features before committing.

---

## Related docs

- [Positioning](positioning.md)
- [Sync Guide](sync-guide.md)
- [Security spec](security/security-spec.md)
