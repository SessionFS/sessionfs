# Adoption Roadmap — v0.15 (INTERNAL)

**Author:** Compass · **Date:** 2026-07-27 · **Status:** CEO-approved strategy, decomposed for execution
**Theme:** v0.15 is an ADOPTION release. No new platform features. Every item below maps to a funnel stage or removes a churn/reach blocker.

## Goal & North-Star Metric

**North star: Weekly Activated Installs (WAI)** — count of distinct `install_id`s that reached the *magic moment* (first resume OR first `ask_project`) within 7 days of install, measured weekly.

Guardrail metric: **D7 return rate** — % of activated installs with any daemon heartbeat or CLI event on day 7±2. WAI without D7 return means the magic moment isn't sticky; watch both, optimize WAI first.

Why WAI over raw installs: pip downloads are noise (CI, mirrors). Why not D7 alone: too lagging to steer a 6-week cycle.

## The Funnel (canonical definition)

| Stage | Event | Today's reality |
|---|---|---|
| 1. Install | `install` (first CLI invocation per install_id) | Not measured. pip-only path; fails in bad Python envs. |
| 2. Setup | `init_completed` (sfs init finishes with ≥1 tool enabled + daemon started) | Wizard works but ends with a passive "Next steps" text block. |
| 3. First capture | `first_capture` (daemon writes first .sfs) | Happens silently; user never sees proof it worked. |
| 4. **Magic moment** | `first_magic` (first `sfs resume` OR first MCP `ask_project`/`sfs project ask`) | Undemonstrated. Nothing pushes the user to it. |
| 5. Retention | `d7_return` (derived: any event day 7±2) + weekly `heartbeat` | Endpoint exists (`routes/telemetry.py`) but **there is no client emitter at all** — zero data today. |

**Biggest gap:** stages 3→4. The product does the magic invisibly and never shows it. Second biggest: we are flying blind (no client telemetry).

## Phases

### P0 — must ship in v0.15

**P0.1 — `sfs init` ends with a demonstrated magic moment** · Owner: **Atlas** · Size: **M**
- What: after daemon start in `cmd_init.py`, synchronously capture the user's most-recent existing session (reuse `sfs recapture`/import machinery — the native session files are already on disk, no need to wait for the watcher), print the captured session line (`✓ Captured: "Debug auth middleware" — 47 msgs, Claude Code`), then print the payoff prompt: `Now ask your agent: "what did we do last session?"` (MCP is already installed by step 4 of the wizard, so this works immediately).
- Why: converts stage 2→3→4 in one sitting. This is the single highest-leverage item in the release.
- AC: fresh init on a machine with ≥1 existing native session shows a real captured session + the ask prompt within the wizard; machine with zero sessions gets a graceful "use your AI tool, then run `sfs list`" fallback; capture failure never breaks the wizard (non-fatal, falls back to current next-steps text); covered by tests.
- Deps: none.

**P0.2 — Activation-funnel telemetry client + event vocabulary** · Owner: **Atlas** · Size: **M**
- What: (a) additive `event` + `event_ts` fields on `TelemetryPayload` (existing fields stay; no migration to existing rows needed beyond an additive column); (b) a client emitter (CLI + daemon) generating a random `install_id` at first run, firing exactly the funnel events above plus a daily daemon `heartbeat`; (c) fire-and-forget, ≤1s timeout, never blocks or errors user-facing commands; (d) `telemetry.enabled = false` opt-out in config.toml + `SFS_NO_TELEMETRY=1` env var, shown once at `sfs init` ("anonymous usage pings; disable with…") and documented; (e) no payload content beyond event name/version/os/tool-name — server PII gate stays.
- Why: WAI is unmeasurable without it; every other bet in this release is unverifiable.
- AC: all 5 funnel events observable in `telemetry_events` from a fresh install walkthrough; opt-out verified (zero requests); docs page exists; Shield-SR reviews the payload (privacy claim is public-facing).
- Deps: none. P0.1 should emit `first_capture`/`init_completed` through this emitter (build P0.2 first or land together).

**P0.3 — Python-env-independent install path** · Owner: **Forge** · Size: **M**
- What: (a) README/docs lead with `pipx install sessionfs` (works today, zero code); (b) Homebrew formula (own tap `sessionfs/tap` first; homebrew-core later); (c) `curl -fsSL get.sessionfs.dev | sh` installer script that detects pipx/uv/venv and does the right thing, served from the site; (d) a **timeboxed spike doc** (≤2 days) on static-binary feasibility (PyInstaller/PyOxidizer vs the fsevents/inotify + SQLite deps) — decision only, shipping a binary is P2.
- Why: stage-1 funnel; "pip broke my env" is a silent bounce we never see.
- AC: brew + pipx + curl paths each verified on a clean macOS and Linux box ending in a working `sfs init`; installer script is idempotent and checksummed; spike doc has a go/no-go recommendation.
- Deps: none. Coordinate with P0.4 for README ordering.

**P0.4 — Wedge-story rewrite: README + site hero + quickstart** · Owner: **Scribe** (site + docs) · Size: **M**
- What: one story everywhere above the fold: **"Your AI coding agent never starts from zero again."** Hero, README intro, and quickstart lead with memory + cross-tool resume ONLY (the current hero's "hand it to a teammate … rules, tickets, and audit" clause moves down-page). Handoff/governance/SSO become an "as your team grows" expansion section. Quickstart mirrors the new init flow (install → init → *see your session captured* → ask your agent).
- Why: stage 1–2 conversion; today's 4-pillar hero makes visitors do the positioning work themselves.
- AC: site hero + README + quickstart tell only the wedge story above the fold; expansion story present but below; install snippet matches P0.3's preferred path; Scribe-Site release gate passes.
- Deps: P0.3 (install command), P0.1 (quickstart must show the real init output).

**P0.5 — Capture-health self-monitoring (daemon + doctor)** · Owner: **Atlas** · Size: **M**
- What: daemon tracks per-watcher parse success/failure counters and last-successful-capture timestamp; a threshold (e.g. N consecutive parse failures, or tool activity detected but zero captures for X hours) flips the watcher to a `degraded` state persisted locally; `sfs doctor` gains a "capture health" check surfacing degraded watchers with the failing tool + suggested action; degraded state emits a telemetry event (`capture_degraded`, tool name + version only) so we learn about format breaks before churn.
- Why: retention/churn. A native tool updating its session format silently kills capture — the user notices weeks later and uninstalls. This is our early-warning system for all 9 watchers.
- AC: simulated malformed sessions flip a watcher to degraded; `sfs doctor` reports it; recovery (successful parse) clears it; telemetry event fires (respecting opt-out); no false positives from idle tools.
- Deps: P0.2 (event emitter). Dashboard surface is P1.4, deliberately split.

### P1 — fast-follow (start when P0 is merged; ship as v0.15.x)

**P1.1 — Handoff-recipient pre-signup landing** · Owner: **Prism** (UI) + **Atlas** (endpoint) · Size: **L**
- What: recipient of a handoff email lands on a page that shows the session (title, summary, message preview, sender, tool) BEFORE authentication; sign-up is the *claim* action, not the *view* gate. Read-only preview endpoint keyed by the handoff token; respects revoke/expiry; DLP-scanned content only.
- Why: this is the viral loop — every handoff becomes an acquisition surface. Today the recipient hits a signup wall with zero proof of value.
- AC: unauthenticated recipient sees the preview; revoked/expired handoffs show a safe state; claim flow unchanged post-signup; Sentinel reviews the token-gated preview surface.

**P1.2 — Public share links: read-only, no-auth session page** · Owner: **Atlas** + **Prism** · Size: **L**
- Product decision (Compass, settled here): scope is a **read-only, no-auth transcript view page** at a stable URL, owner-revocable, optional password (backend already supports PBKDF2 passwords), `noindex` OFF by default for public links (this is the SEO surface), transcript rendered with the v0.10.30 conversation components. NO forking, NO commenting, NO partial-message selection in v0.15.x.
- Why: shareable artifact + SEO surface; "look what my agent did" is the organic loop.
- AC: public link renders a full transcript logged-out; revoke kills it; DLP policy applies; OG meta tags for link unfurls; Shield-SR review (public content surface).

**P1.3 — Marketplace & listing sweep** · Owner: **Beacon** · Size: **M**
- What: submit/verify presence in MCP registries + directories, VS Code marketplace (extension listing refresh), relevant awesome-lists (awesome-mcp, awesome-claude-code, awesome-ai-agents), Claude Code plugin ecosystem, PyPI metadata/keywords polish. Deliverable: a tracked checklist with listing URLs + owners for upkeep.
- Why: stage-1 discovery; near-zero engineering cost.
- AC: ≥6 live listings with correct wedge-story copy (from P0.4); checklist doc committed internally.
- Deps: P0.4 (copy).

**P1.4 — Capture-health dashboard surface** · Owner: **Prism** · Size: **S**
- What: dashboard shows per-tool capture health for synced daemons (degraded badge + last-capture time), sourced from the P0.5 state pushed with sync metadata.
- AC: degraded watcher visible on dashboard within one sync cycle; healthy = quiet (no noise).
- Deps: P0.5.

**P1.5 — Coordinated launch moment** · Owner: **Reach** · Size: **M**
- What: Show HN + Product Hunt launch built on the wedge demo (terminal recording of install → init → magic moment → cross-tool resume), timed AFTER P0 ships and listings (P1.3) are live. Includes launch-day checklist, demo assets, and a follow-up plan for comment engagement. **External posting requires explicit CEO go — Reach prepares, human fires.**
- AC: launch kit ready (post draft, demo GIF/asciinema, FAQ answers); dry-run of the demo on a clean machine; CEO sign-off gate documented.
- Deps: P0.1–P0.4 shipped; P1.3 substantially done.

**P1.6 — Windows support: scoping spike + honest docs** · Owner: **Forge** · Size: **M** (spike; full support is P2)
- What: timeboxed spike: run capture on Windows (watchdog supports ReadDirectoryChangesW) — enumerate what breaks: native tool storage paths (`%APPDATA%`, `~/.claude` equivalents), daemon start/PID handling, `fcntl` usage in the exclusion-list locking, path handling in converters, `sfs init` detection (currently Darwin-else-Linux). Output: a gap list + effort estimate + CI-matrix plan. Meanwhile docs state platform support honestly (macOS/Linux now, Windows tracked).
- Why: reach blocker — a large share of developers; today we neither support nor disclaim it.
- AC: spike doc with per-subsystem gap list + estimate; README/docs platform statement updated; decision whether Windows GA is v0.16.

**P1.7 — Real-IdP SSO smoke test + SSO in Helm** · Owner: **Forge** (Helm) + **Atlas** (smoke) · Size: **M**
- What: (a) stand up a free Okta developer tenant + Google Workspace test and run the full v0.13/v0.14 SSO flow (CLI + browser login, JIT, enforcement, break-glass) against prod-shaped config, recording a runbook; (b) close the tracked SSO-in-Helm gap (chart values for provider config incl. `SFS_DASHBOARD_URL`, `client_secret_ref` env resolution documented).
- Why: enterprise credibility follow-through — we ship SSO but have never proven it against a real IdP; that's the first thing an enterprise evaluator does.
- AC: documented green runs against Okta AND Google Workspace; Helm chart deploys with SSO configured and the smoke test passes against it; gaps found become tickets.

### P2 — later (explicitly deferred)

- Static binary distribution (contingent on P0.3 spike go).
- Windows GA + CI matrix (contingent on P1.6 spike).
- SEO expansion on public share pages (sitemaps, canonical structure), share-page comments/forking.
- Funnel dashboard/reporting UI for ourselves (until then: SQL against `telemetry_events`).
- homebrew-core promotion (tap first).

## Explicitly OUT of v0.15

No new platform features: no new MCP tools, no new work-queue/resident capability, no new session-format features, no schema work beyond the additive telemetry column, no pricing/packaging changes. Anything arriving mid-cycle that isn't funnel-mapped goes to the v0.16 backlog.

## Measurement (exact event definitions)

All events: `{install_id, event, event_ts, version, os}` — nothing else identifying. Opt-out: config `telemetry.enabled=false` or `SFS_NO_TELEMETRY=1`; documented publicly.

| Event | Fired when | Fired by |
|---|---|---|
| `install` | first-ever CLI invocation for this install_id | CLI bootstrap |
| `init_completed` | `sfs init` exits with ≥1 tool enabled AND daemon started | cmd_init |
| `first_capture` | first .sfs written for this install | daemon (once, flag file) |
| `first_magic` | first `sfs resume` OR first `ask_project`/`sfs project ask` | CLI / MCP server (once) |
| `heartbeat` | daily while daemon runs | daemon |
| `capture_degraded` | watcher flips to degraded (includes tool name) | daemon |

Derived: **WAI** = weekly distinct install_ids with `first_magic` ≤ 7d after `install`; **D7 return** = activated installs with any event day 7±2. Review WAI weekly during the v0.15 cycle.

## Risks & open questions

- P0.1 synchronous capture must not hang the wizard on huge native session files — hard timeout, background fallback.
- Telemetry is brand-sensitive: default-on must be disclosed at init and trivially reversible; Shield-SR gates the release on the privacy wording.
- P1.2 public pages are a new abuse surface (secrets in transcripts) — DLP-on-share is a hard AC, not a nice-to-have.
- Launch timing (P1.5) compresses if P0 slips; launch on P0 quality, not on the calendar.

## Ticket manifest

| # | Title | Persona | Priority | Size | P0? |
|---|---|---|---|---|---|
| 1 | sfs init: demonstrated magic moment (capture latest session + ask prompt) | Atlas | high | M | YES |
| 2 | Activation-funnel telemetry: client emitter + event vocabulary + opt-out | Atlas | high | M | YES |
| 3 | Python-env-independent install: pipx docs, brew tap, curl installer, static-binary spike | Forge | high | M | YES |
| 4 | Wedge-story rewrite: README + site hero + quickstart | Scribe | high | M | YES |
| 5 | Capture-health self-monitoring: daemon degraded-state + sfs doctor check | Atlas | high | M | YES |
| 6 | Handoff-recipient pre-signup landing (view before signup) | Prism | medium | L | no |
| 7 | Public share links: read-only no-auth session page | Atlas | medium | L | no |
| 8 | Marketplace & listing sweep (MCP registries, VS Code, awesome-lists, plugins) | Beacon | medium | M | no |
| 9 | Capture-health dashboard surface | Prism | medium | S | no |
| 10 | Launch kit: Show HN / Product Hunt with wedge demo (CEO fires) | Reach | medium | M | no |
| 11 | Windows support scoping spike + honest platform docs | Forge | medium | M | no |
| 12 | Real-IdP SSO smoke test (Okta + Google Workspace) + SSO in Helm chart | Forge | medium | M | no |

**Ticket descriptions (paste-ready):**

1. **sfs init magic moment (Atlas, high, M, P0).** After the daemon starts in `cmd_init.py`, synchronously capture the user's most-recent existing native session (reuse recapture/import machinery; hard timeout with background fallback) and print the captured session line followed by: `Now ask your agent: "what did we do last session?"`. Zero-session machines get a graceful fallback; capture failure is non-fatal and falls back to the current next-steps block. Emits `init_completed` + `first_capture` via the ticket-2 emitter.

2. **Activation-funnel telemetry (Atlas, high, M, P0).** Add additive `event`/`event_ts` fields to `TelemetryPayload` + `TelemetryEvent`, and build the missing client emitter (CLI + daemon): random install_id at first run; events install / init_completed / first_capture / first_magic / heartbeat / capture_degraded; fire-and-forget ≤1s, never blocks commands. Opt-out via `telemetry.enabled=false` and `SFS_NO_TELEMETRY=1`, disclosed once at init and publicly documented. Payload carries only event/version/os(/tool for capture_degraded). Shield-SR reviews privacy wording.

3. **Env-independent install (Forge, high, M, P0).** Make pipx the documented default; publish a `sessionfs/tap` Homebrew formula; ship a checksummed `get.sessionfs.dev` curl installer that picks pipx/uv/venv; verify all three on clean macOS + Linux through a working `sfs init`. Include a ≤2-day static-binary feasibility spike (PyInstaller vs fsevents/inotify/SQLite deps) ending in a go/no-go doc — shipping a binary is out of scope.

4. **Wedge-story rewrite (Scribe, high, M, P0).** README, site hero (`site/src/pages/index.astro`), and quickstart tell only "your AI coding agent never starts from zero again" (memory + cross-tool resume) above the fold; handoff/governance/SSO move to a below-fold expansion section. Quickstart mirrors the new init flow ending in the demonstrated capture + ask prompt. Install snippet matches ticket 3's preferred path. Depends on tickets 1 and 3.

5. **Capture-health self-monitoring (Atlas, high, M, P0).** Daemon tracks per-watcher parse success/failure + last-capture time; consecutive-failure or activity-without-capture thresholds flip a persisted `degraded` state; `sfs doctor` gains a capture-health check naming the failing tool; recovery clears it; `capture_degraded` telemetry event (tool name only, opt-out respected). Guard against false positives from idle tools. Dashboard surface is ticket 9, deliberately excluded here.

6. **Handoff pre-signup landing (Prism, medium, L).** Recipient landing page shows session title, summary, sender, tool, and a message preview BEFORE authentication via a token-gated read-only preview endpoint (Atlas sub-task); signup is the claim action, not the view gate. Respects revoke/expiry; DLP-clean content only; Sentinel review required on the token surface.

7. **Public share links page (Atlas+Prism, medium, L).** Read-only, no-auth transcript page at a stable URL using the existing share-link backend (PBKDF2 password support kept): owner-revocable, DLP policy enforced, OG unfurl tags, rendered with the existing conversation components. Explicitly excluded: forking, comments, partial selection, auth-gated features. Shield-SR review as a new public content surface.

8. **Listing sweep (Beacon, medium, M).** Submit/verify SessionFS in MCP registries and directories, refresh VS Code marketplace presence, PR the relevant awesome-lists (MCP / Claude Code / AI agents), cover the Claude Code plugin ecosystem, and polish PyPI metadata. Use the ticket-4 wedge copy. Deliverable: internal checklist doc with live listing URLs and upkeep owners; target ≥6 live listings.

9. **Capture-health dashboard (Prism, medium, S).** Surface per-tool capture health (degraded badge + last-capture time) for synced daemons on the dashboard, sourced from ticket-5 state carried in sync metadata. Healthy state stays quiet. Depends on ticket 5.

10. **Launch kit (Reach, medium, M).** Prepare Show HN + Product Hunt launch around the wedge demo: post drafts, terminal recording (install → init magic moment → cross-tool resume), FAQ/comment plan, clean-machine dry run. Timed after P0 ships and listings are live. External posting is CEO-gated — Reach prepares, human fires.

11. **Windows spike (Forge, medium, M).** Timeboxed spike running capture on Windows: enumerate gaps across native storage paths, daemon/PID handling, `fcntl` locking, converter path handling, and the Darwin-else-Linux assumption in `sfs init` detection; produce a gap list, effort estimate, and CI-matrix plan; update docs to state platform support honestly. Decision output: is Windows GA a v0.16 headline.

12. **Real-IdP SSO smoke + Helm (Forge, medium, M).** Stand up free Okta developer + Google Workspace tenants and run the full SSO flow (CLI + browser login, JIT, enforcement, break-glass) end-to-end, producing a runbook; close the tracked SSO-in-Helm gap (provider config values incl. `SFS_DASHBOARD_URL`, documented `client_secret_ref` env resolution) and run the smoke test against a Helm deploy. Gaps found become follow-up tickets.
