# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.29.0] - 2026-10-29

**Meetings: store transcripts, pull out action items and decisions, and turn them into tickets. The session helper knows who is asking.** Server migrations 104 and 105.

### Added
- **Meeting transcripts** (GitHub #116), for projects on every plan and for project groups on Team and Enterprise.
  - Add a meeting by pasting text or uploading `.txt`, `.md`, `.vtt` or `.srt` files (caption timing is removed) from the dashboard, `sfs meeting upload`, the API or MCP.
  - Secrets and personal data are redacted under your org's DLP policy before anything is stored; a blocking policy refuses the upload.
  - **Extract** action items, decisions and participants with your own AI key or your org's. Every item cites the transcript lines it came from, and items that can't be checked against the text are dropped and counted.
  - Participants are matched to org members and personas only when their name appears in the transcript; anyone unmatched can be matched by hand.
  - **Create ticket** from an action item through an editable preview that quotes the meeting lines, with the assignee and due date filled in and optional Jira publishing.
  - Meetings are kept for 180 days by default (`general.meeting_retention_days`, 1–3650), with an opt-in Helm CronJob for the cleanup sweep.
  - New API key scopes `meetings:read` and `meetings:write`; promoting with a service key also needs `tickets:write`.
- **Meetings in the dashboard**: a Meetings view under a project's Work section and on project group pages, with search, upload, a numbered transcript, and action items, decisions and participants beside it.
- **`sfs meeting`** commands: `upload`, `list`, `search`, `show`, `delete`, `extract`, `participants`, `resolve-participant`, `action-items`, `promote`, `dismiss`, `restore`.
- **Six MCP tools** (106 in total): `upload_meeting_transcript`, `list_meeting_transcripts`, `get_meeting_transcript`, `extract_meeting_action_items`, `promote_meeting_action_item`, `search_meetings`.
- **The session helper reads meetings**: it can search and read meetings in your scope, list your open action items, and cite meeting lines as links that open the transcript at those lines.
- **The session helper knows who is asking** (GitHub #117): "my tickets" works, a name finds tickets assigned to a persona or to a person, and project groups can be named by name, slug or abbreviation.
- AI usage is recorded by source, so meeting extraction and helper chats show separately and both count against the org budget.

### Changed
- Tabs across the dashboard use a quieter underline style instead of boxed tabs.
- The project **Personas** tab is now called **Agents**.
- Dialogs keep keyboard focus inside the topmost dialog while it is busy.

## [0.28.0] - 2026-10-28

**Ask the session helper about a whole project group, due dates, estimates and a full history on tickets, Jira sprints and parent epics, and sweep keys that explain themselves.** Server migrations 102 and 103.

### Added
- **Scope the session helper to a project group.** Pick a group in the scope menu, use `sfs helper ask --group <slug|id>`, or pass `group_id` to the MCP `ask_session_helper` tool.
  - Answers stay inside the group's projects that you can read, checked again on every step.
  - The group's compiled context is part of the helper's instructions, DLP-redacted and protected against injected tags.
- **Due dates and estimates on tickets.** Set them in the dashboard, with `sfs ticket create|update --due YYYY-MM-DD --estimate 2d4h`, or through MCP `due_date` / `estimated_effort`.
  - Ticket lists accept `?overdue=true`, and the dashboard has an **Overdue** filter.
  - Estimates use Jira's duration syntax.
- **Ticket history.** Every change to a ticket is recorded: status, assignment, field edits, due dates and Jira links.
  - Read it with `GET /api/v1/projects/{id}/tickets/{tid}/events` or `sfs ticket history`, or in the ticket's Activity timeline.
  - Tickets now show who created and who resolved them. Emails follow the existing masking rules.
- **Jira sprints and parents.**
  - Sprint and epic-link fields are detected automatically. Org admins can review or override them under Org → Integrations → Jira → Agile fields without re-entering the API token.
  - Publishing can put the issue in an active or future sprint.
  - A ticket's parent is published as the Jira parent or epic.
  - Sprint and parent are shown on the ticket.
- **Due dates and estimates in Jira.** They are published as `duedate` and `originalEstimate` and pulled back. Inbound due-date changes apply when sync is on. If a project's screens hide a field, the publish still succeeds and says which field was skipped.
- **The helper's create-and-publish can choose the Jira project** (one mapped to a project you can edit), **the template** (version pinned when proposed) **and the sprint**.
- **Fetch models on the org AI-key form**, with a manual fallback, like the personal form. Model lookups are rate-limited per user.
- **Opt-in Helm CronJobs for every scheduled sweep:**
  - ticket notifications, every 10 minutes;
  - org digest, daily;
  - knowledge base compile, every 15 minutes;
  - Jira sync, every minute;
  - Confluence publish, every 15 minutes.

  The CronJob pods can reach only DNS and the API, and they mount no service account token.

### Changed
- **Jira publishing never creates a duplicate issue.**
  - Only a rejected field (HTTP 400 naming it) is retried, and only that field is dropped.
  - After a timeout, a server error or a crashed publish, the next publish looks for the existing issue by label in the original project and adopts it.
  - Warnings explain in plain language anything that couldn't be set.
- **Sweep service keys explain their admin rule.**
  - **Creating or rotating a key:** a `platform_sweeps:run` key gets a warning that it only works while the person who created it stays a platform admin.
  - **When it's refused:** the 403 names the cause and the fix.
  - **CLI:** `sfs admin service-keys list` marks keys that no longer work, and `scopes` flags the scopes with this rule.
- **The helper remembers your last scope** per signed-in account and tab, and restores a conversation's own scope.
- **The Jira template editor** has explanations for each mode and source field, a preview beside the sections, **Copy from…**, and a searchable project picker.

### Fixed
- Ticket and wiki citations in a helper answer link correctly as soon as the answer finishes, instead of showing "source not available" until the conversation was reopened.

### Upgrading
- Run migrations 102 and 103 (both additive).
- **Existing Jira connections:** an org admin should open Org → Integrations → Jira → Agile fields, click **Detect fields** and save once. Until then, sprints and epic links aren't available on connections made before 0.28.0.
- **Self-hosted:** check your sweep keys with `sfs admin service-keys list`, and consider the new opt-in CronJobs in the Helm chart.

## [0.27.0] - 2026-10-27

**Assign tickets to people, link tickets to existing Jira issues and choose which side wins, a Jira-aware session helper, and a review loop that works with any reviewer.** Server migration 101.

### Added
- **Assign a ticket to a person.** A ticket can now go straight to a teammate instead of a persona. They get a "Your turn" notification and follow the ticket automatically.
  - **Rules:** a ticket has a person or a persona, never both. Only people who can read the project can be assigned.
  - **Finding people:** `GET /api/v1/projects/{id}/assignable-users` lists them. Ticket lists accept `?assigned_user=me` and the dashboard has an **Assigned to me** filter.
  - **CLI, MCP and helper:** `sfs ticket create|update --user/-u <email|id>`, MCP `assigned_user`. The helper accepts a person by name or email.
  - **Warning:** the dashboard assignee picker warns when the chosen persona represents nobody, because nobody would be notified.
- **Link a ticket to an existing Jira issue.** Use **Link existing issue** on a ticket, `POST /api/v1/projects/{id}/tickets/{tid}/jira/link`, or ask the helper. An issue already linked to another ticket is refused.
- **Choose which side wins on a linked issue.** Publishing never silently overwrites an issue that was linked from Jira. You pick one of two actions:
  - **Overwrite the issue in Jira:** publish with confirmation (`overwrite_existing`, CLI `sfs ticket jira publish --overwrite-existing`).
  - **Update the ticket from Jira:** `POST .../jira/pull`, CLI `sfs ticket jira pull`. Pulls are recorded as normal ticket edits. Empty Jira fields leave the ticket unchanged, and your DLP policy applies.
- **The session helper understands Jira.** It can:
  - read a linked Jira issue;
  - send a comment to Jira as well;
  - link a ticket to an issue;
  - overwrite either side;
  - create a ticket and publish it to Jira in one confirm.
- **Review loop guide.** A new docs page and a `worker-review-loop` skill in the `sessionfs-workflows` catalog describe the implement → review → fix loop and its strict merge gate.

### Changed
- **Any reviewer name.** Review verdict headers accept any reviewer name, such as "Claude R1 review", not only Codex.
- **Unreadable verdicts are refused.** A work-queue completion whose verdict header can't be parsed is rejected with 422 `verdict_header_unparseable`.
- **Strict check in the CLI.** `sfs ticket review-state` prints whether the ticket is strictly VERIFIED-CLEAN.
- **Merged projects stay out of lists.** They no longer appear in project lists, the helper or Jira health, and can't be added to a project group (410 `project_merged`). Direct links still answer 410 with the merge target.
- **Sweep schedules documented.** The Jira sync and Confluence publish sweeps now have documented schedules.

## [0.26.0] - 2026-10-27

**Project groups, two-way Jira sync, Confluence publishing, and a session helper that can draft tickets and find sessions by model.** Server migrations 096–100. Adds 15 MCP tools (85 → 100).

### Added
- **Project groups.** Org admins can group projects (for example "Payments platform") and grant people reader, editor or admin access to the whole group. An org can turn on **restricted project visibility** so members see only projects they own, created, or reach through a group. Groups offer knowledge search and a list of wiki pages across their projects, and an optional deterministic overview document compiled from them. Everything is filtered to the projects each person can actually read. Team and Enterprise. API: `/api/v1/orgs/{org_id}/groups…`, `GET /api/v1/me/groups`, `GET /api/v1/projects/{id}/groups`. CLI: `sfs project group list|create|show|set|delete|add|remove|members|grant|revoke|access|search|context|compile`, `sfs project list --group`. MCP: 15 new group tools.
- **Two-way Jira sync.**
  - **Status:** a linked ticket's status changes move the Jira issue through your status map. Moving the issue to Done in Jira resolves the ticket.
  - **Comments:** comments can be mirrored in either direction. A **Send to Jira** checkbox posts a comment to the issue, and trusted VERIFIED-CLEAN review verdicts are forwarded automatically.
  - **Completion evidence:** when a ticket completes, one evidence comment goes to the issue: the verdict, changed files, and promoted knowledge.
  - **Webhook:** inbound changes arrive at `POST /api/v1/webhooks/jira/{org_id}`, signed with a per-org secret (`POST /api/v1/orgs/{org_id}/integrations/jira/webhook-secret`).
  - **Reliability:** changes going out are queued and retried with backoff (`POST /api/v1/admin/sweeps/jira-sync`), and our own changes echoing back are ignored.
  - **Settings:** `PATCH /api/v1/orgs/{org_id}/integrations/jira/sync` changes sync settings without re-entering the token.
- **Custom Jira story templates.** Org admins can define story templates with verbatim and rewritten sections and pick one per mapping or per ticket. Every published issue is pinned to the template version it used, and a footer links back to SessionFS. CLI: `sfs org jira templates list|show`.
- **Searchable Jira project mapping.** The mapping dialog searches your Jira projects instead of loading them all.
- **Confluence publishing (Enterprise).** Publish wiki pages and the compiled project context to a Confluence Cloud space chosen by an org admin, with a preview showing citations and anything dropped.
  - **Edited in Confluence:** a page that someone edits there is never overwritten. Publishing reports the drift with a diff and lets a project admin overwrite or unlink.
  - **Scheduled publishing:** an optional publish sweep (`POST /api/v1/admin/sweeps/confluence-publish`) keeps opted-in pages current and skips unchanged ones.
  - **Endpoints and commands:** API `/api/v1/orgs/{org_id}/integrations/confluence…` and `/api/v1/projects/{id}/confluence/preview|publish|links`. CLI: `sfs org confluence status|connect|map|unmap`, `sfs project confluence publish`.
- **The session helper reads more of your project.** It can look up tickets, wiki pages, agent runs and personas, with the date each answer is as of, and cite them.
- **The helper can draft tickets, assign them and comment.** You review and edit every field before pressing **Confirm**, and nothing changes until you do. It follows the same permissions as the ticket pages, never posts a trusted review verdict, and records which conversation the change came from.
- **The helper finds sessions by model.** Ask "which sessions used DeepSeek?" and it lists the models you've used with counts, filters by model or provider family, pages through results, and can propose moving every match to Trash (100 per proposal).

### Changed
- **Calmer marketplace.**
  - **Browse:** a compact header, with most filters moved behind a **Filters** button that shows removable chips.
  - **Install:** install commands now live in an **Install in your tool** dialog.
  - **Cards:** simpler cards.
  - **Package detail:** tabs, with scan results and safety alerts always on the first tab.
- **Organization → Integrations** starts with one summary row per integration that names only real problems. Each integration's settings sit in their own sections: overview, connection, mappings, templates, sync and activity.
- **Ticket detail** has a clear header and one timeline of comments, review verdicts, Jira activity and edits.
- **Project settings** are grouped into General, Knowledge, Repositories, Access & ownership and Danger zone.
- The top bar no longer overlaps its icons at tablet widths, and headings, landmarks and scrollable tables are labelled for screen readers.

### Fixed
- Merging two projects that both had knowledge entries no longer fails with a server error.
- The project Transfer panel only appears for people allowed to start a transfer.
- Session search with a date range no longer fails on PostgreSQL.
- Text from consecutive helper steps no longer runs together.

### Security
- Two-way Jira sync stops for a project that leaves the linked org.
- The Confluence Markdown converter can't be made to stall the server.
- Confluence pages and new-project knowledge backfill only use sessions that belong to the project or its creator.

### Upgrade notes
- **Self-hosted servers: run migrations 096–100.** They add project groups, Jira custom templates, group context, Confluence and Jira sync. All are additive.
- **Optional schedules:** to keep Jira sync retries and Confluence auto-publishing running, schedule `POST /api/v1/admin/sweeps/jira-sync` and `POST /api/v1/admin/sweeps/confluence-publish` with a `platform_sweeps:run` key.

## [0.25.0] - 2026-10-26

**The session helper can clean up for you, answers from the SessionFS docs, and is configurable per org; filter sessions by model; test an LLM key before saving it.** Server migrations 094–095. MCP tools unchanged (85).

### Added
- **Helper actions with confirmation.** The session helper can propose two changes: moving sessions to Trash ("clean up my scratch sessions from last month") and turning Auto Narrative on or off for projects. Each proposal shows a card listing every target and what will happen to it; nothing changes until you press **Confirm**, and access is checked again for each target at that moment. Sessions go to Trash, never deleted, so cleanup can be undone for 30 days. Proposals expire after 15 minutes, and every decision is recorded in the retrieval audit log. API: `POST /api/v1/helper/actions/{id}/confirm|cancel`, `GET /api/v1/helper/actions/{id}`.
- **Clickable references in helper answers.** Ticket ids open a preview over the conversation (status, priority, assignee, description, latest comments, **Open ticket**) without leaving the page; project and session ids open their pages, and PR numbers and commit SHAs link to GitHub when the project has one verified repository. API: `GET /api/v1/tickets/{ticket_id}`.
- **The helper answers from the SessionFS docs.** Questions about how SessionFS works, setup, commands, settings and plans are answered from the official documentation, with links to the exact page and section.
- **Configure the assistant.** Org owners and admins, and each user for their own key, can set the maximum answer length, temperature, top-p, tool steps, reasoning effort and answer style; orgs can cap output tokens and tool steps. Add custom instructions (up to 4000 characters for the org, 2000 personal) that cannot override the helper's access and safety rules. API: `GET/PUT /api/v1/settings/helper`, `GET/PUT /api/v1/orgs/{org_id}/helper-settings`. `sfs helper status` shows the effective settings.
- **Token usage and a monthly budget.** Every helper turn records its token usage. Org admins see usage per member for the current month and can set a monthly token budget for turns that use the org's key; once it is reached, helper turns return `429 helper_budget_exceeded` until the next month. Deleting a conversation doesn't reset usage.
- **Filter and sort sessions by model.** The Sessions page can filter by the LLM model a session used and group sessions by model. API: `?model=` on the session list and `GET /api/v1/sessions/models`.
- **Test connection for LLM keys.** Personal and org key settings have a **Test connection** button that sends a tiny request and shows the endpoint, model and latency, or a readable reason it failed (wrong address, invalid key, model not found, rate limited, out of balance). API: `POST /api/v1/settings/judge/test`, `POST /api/v1/orgs/{org_id}/llm-settings/test`.
- **Help page refresh.** The dashboard Help page covers the session helper, ticket notifications, model settings and the current MCP tools.

### Changed
- **Saving a Base URL checks it first.** A base URL that points at a provider's website or console instead of its API (for example `https://platform.deepseek.com`, which answers `405 Method Not Allowed`) or a key the provider rejects is refused with a clear message. Outages and rate limits never block a save; they are shown as warnings. When the helper hits a wrong address at runtime, its error names the API address to use.
- **Only a project's owner and its organization's members can change it.** Anyone else who synced a session from the same repository can still read the project and add knowledge entries, tickets, comments and agent runs, but changing settings, rules, wiki pages, the context document, personas or work queues, or curating the knowledge base, now returns `403 project_edit_forbidden`. Project responses carry `can_edit`, and the dashboard hides the controls such a collaborator can't use.
- **Collaborators' knowledge entries stay notes.** Entries from people who can view but not edit a project are never promoted or allowed to supersede other entries automatically, so they don't reach the context document or compiled rules until an owner or org member promotes them. Bulk promote leaves them out unless you pass `include_reader_notes` (`--include-reader-notes`).
- **Collaborators' tickets need approval.** Tickets they create start as *suggested*, and editing one after approval sends it back to *suggested*. Open tickets created by non-members before this change are no longer picked up by work queues with auto-adopt until an owner or org member approves them. Once a work queue has adopted a ticket, they can't change what it asks for, and a queue stops handing out a ticket that goes back to *suggested* or is cancelled.
- **Collaborators' comments don't reach automated agents.** People still see them everywhere, but work-queue directives leave them out and they don't start a reviewer's turn.

### Security
- **People who only synced a session can no longer change a project.** Before this release, anyone with a captured session from a project's repository could edit its settings, rules and context, which could change the instructions compiled into other members' tool rules files. Editing is now limited to the project's owner and its organization's members (see Changed above).

### Upgrade notes
- **Self-hosted servers: run migrations 094 and 095** (helper actions; helper settings and usage ledger; both additive).
- **Review collaborator contributions.** Tickets and knowledge notes created by people outside your org now wait for an owner or org member to approve or promote them.

## [0.24.0] - 2026-10-25

**Follow tickets and get told when it's your turn, bring a key from 14 LLM providers, and a local session store that survives crashes.** Server migration 093. Adds 2 MCP tools (83 → 85).

### Added
- **Follow tickets.** Follow a ticket to hear when it changes state, changes hands or gets a comment; creating a ticket, being assigned, commenting or posting a review verdict follows it for you, and you can mute a ticket without unfollowing. A bell in the dashboard opens your inbox, where "your turn" notifications (a ticket assigned to you, a review waiting on you) stand out and open the ticket directly. Bursts of changes are combined into one notification, and notifications are written by the server and never copy comment bodies. CLI: `sfs ticket follow|unfollow|inbox`. API: `/api/v1/me/ticket-notifications`, `PUT /api/v1/me/notification-settings`.
- **Ticket updates for agents.** Agents poll what changed on the tickets they follow with `sfs ticket updates`, the MCP tools `watch_ticket` and `get_ticket_updates`, or `GET /api/v1/me/ticket-updates`. Each persona keeps its own cursor, so several agents on one account each see every change once; service keys get their own cursors and only see projects they are allowed to.
- **Ticket email digest.** Choose `off`, `your_turn` (the default) or `all` under **Settings → Preferences**. One email per run lists everything pending instead of one email per change, and access is re-checked before anything is sent. It is sent by the sweep `POST /api/v1/admin/sweeps/ticket-notifications`.
- **14 LLM providers.** The security review, session helper, summaries and Jira drafting accept a key from Anthropic, OpenAI, Google Gemini, OpenRouter, DeepSeek, Kimi (Moonshot), xAI, Mistral, Z.ai (GLM), Qwen (Alibaba Cloud), MiniMax, Groq, Together AI or Fireworks AI, or any OpenAI-compatible endpoint (LiteLLM, vLLM, Ollama). **Fetch models** lists the models your key can use, and any model id can be typed in. `sfs audit --provider` reads that provider's own key variable (`DEEPSEEK_API_KEY`, `MOONSHOT_API_KEY`, and so on). The default model is now `claude-sonnet-5-5`.
- **Helper citations open the exact message.** A citation in a helper answer opens the session at the cited message and highlights it (`/sessions/<id>?msg=<n>`, backed by `GET /api/v1/sessions/{id}/messages?around=<n>`). The audit tab's **Jump to message** uses the same link.
- **Helper message retention in org settings.** Org admins can set how long helper messages are kept from the Organization page.

### Changed
- **The session helper drawer is redesigned**, with a pinned composer, readable citations, conversation history and New always at hand, and clear states while access is being checked or a scope isn't available. Switching between project, session and org scope keeps the drawer usable.
- **Reasoning models work behind any gateway (GitHub #102).** When an endpoint rejects `temperature`, `top_p` or reasoning settings, SessionFS retries that request once without them and remembers it for that endpoint and model. Reasoning effort is sent in each API's own shape. When a narrative summary fails, the dashboard shows why, without echoing network error details.
- **Retrying a failed helper answer doesn't save the question twice** (`retry: true` on `POST /api/v1/helper/turns`).
- **`sfs pull` makes the local copy match the server.** Files the server version no longer has are removed, while local-only reports such as the audit report and checkpoints are kept.
- **Enterprise features follow the project's plan.** Promote to Jira, the session helper and other project features are shown based on the plan of the project's organization, not your primary org's. Project responses include `effective_tier`.
- **The MCP server has 85 tools** (`watch_ticket` and `get_ticket_updates` added).
- The install docs use `curl -fsSL https://sessionfs.dev/install.sh | sh`.

### Fixed
- **Local session store is crash-safe.** Every capture, pull, recapture and delete now writes through one locked commit: a session you deleted can no longer come back from a capture already in progress, files are written atomically and flushed to disk so a power cut can't leave a half-written session, symlinked session folders are refused, and a session that changed during an upload is no longer marked as uploaded (it uploads again). Captures are no slower.
- **Pushing a session with an unusual character no longer fails.** A broken Unicode character in a branch name, title or message made the upload fail with a server error; it is now stored with a replacement character.
- **Codex resume no longer produces duplicate tool calls.** A message captured twice made Codex reject the resumed session.
- **The helper status follows the question's scope**, so a project in an Enterprise org shows the helper as available even when your primary org isn't Enterprise.

### Security
- **LLM keys and integration tokens use a derived encryption key.** Stored keys are encrypted with a key derived by HKDF (existing values still decrypt). The server refuses to store or read encrypted secrets with an unset or placeholder `SFS_VERIFICATION_SECRET` (503 `encryption_unavailable`), except in development.
- **A saved LLM key stays with its provider.** Changing a saved key's provider or endpoint requires entering the key again, so it can't be redirected to another host. Security reviews and narrative summaries that name a different endpoint or provider no longer fall back to your saved key; they must supply their own.
- **Upload limits.** Concurrent upload parts per user are capped and abandoned part data is cleaned up.
- **Jira hardening.** Bounded connection fields, a two-minute clock-skew allowance when adopting an issue after a timeout, and Jira error bodies are no longer passed through.
- Helper knowledge search escapes `%` and `_` in queries.

### Upgrade notes
- **Self-hosted servers: run migration 093** (ticket watchers, notifications and cursors; additive).
- **Helm: generated secrets now persist across upgrades.** Charts before 0.24.0 generated a new `verification-secret` and `encryption-key` on every `helm upgrade`, which broke stored LLM keys and GitLab/Jira tokens. From 0.24.0 the chart reuses the existing values. If stored keys stopped working after an earlier upgrade, re-enter them once. GitOps (`helm template`) flows can't read the cluster: set both values or use `security.existingSecret`.
- **Set `SFS_VERIFICATION_SECRET`** to a real secret on production servers; placeholders now make encrypted-secret routes return 503.
- **Schedule the ticket digest** if you want ticket emails: call `POST /api/v1/admin/sweeps/ticket-notifications` from a CronJob with a `platform_sweeps:run` service key.
- **CLI: `sfs pull` now replaces the local session folder** with the server's version (local audit reports and checkpoints are kept).

## [0.23.0] - 2026-10-24

**Ask questions across your sessions with the Enterprise session helper, promote tickets to Jira, and resume large uploads where they stopped.** Server migrations 084–092. Adds 2 MCP tools (81 → 83).

### Added
- **Session helper (Enterprise).** Ask a question about the sessions and project knowledge you can already read ("find the session where we fixed the SSO redirect", "what did we decide about Redis and why?") and get an answer that cites the sessions and messages it used. The helper searches sessions, reads summaries and excerpts, searches project knowledge, and finds sessions that touched a file; it never sees more than you can. Answers stream into a drawer in the dashboard (from the sidebar, the Cmd+K palette, or a project or session page, scoped to it), into the terminal with `sfs helper ask`, or come back whole from the MCP tool `ask_session_helper`. Threads are kept so you can continue them (`sfs helper conversations list|show|delete`), and `sfs helper status` shows whether it is available and which key it would use. The model call always uses your own key (your security review model settings), or else your org's helper key; SessionFS never supplies one. Everything sent to the model is redacted with your org's DLP policy, and each tool call is written to the retrieval audit log. Service keys can't use it, and each user can run 2 questions at a time per server process. API: `POST /api/v1/helper/turns` (Server-Sent Events by default, or one JSON document with `stream: false`). See `docs/session-helper.md`.
- **Org AI key for the session helper.** Enterprise org owners and admins can set one provider, model, API key and optional base URL for members without a key of their own (**Organization → Policies → AI key for the session helper**, or `GET/PUT/DELETE /api/v1/orgs/{org_id}/llm-settings`). The key is encrypted at rest, never shown again after saving (only its last four characters), can be switched off without deleting it, and a custom base URL passes the same address checks as other user-supplied LLM endpoints.
- **Helper conversation retention.** Helper messages are deleted 90 days after they were written by default; orgs can set 1–3650 days with `helper_retention_days` on `PUT /api/v1/orgs/{org_id}/settings`. The deletion runs from the sweep `POST /api/v1/admin/sweeps/helper-retention` (platform admin or `platform_sweeps:run` key), and the Helm chart can schedule it (`api.helperRetention`).
- **Jira Cloud integration (Enterprise).** Promote a ticket to a Jira issue. Org admins connect a Jira Cloud site with an Atlassian API token from the Organization page's new **Integrations** tab or `sfs org jira connect`, map projects to Jira projects, and check health (unmapped projects, failed updates, stale links). On a ticket, **Promote to Jira** shows an editable preview built from one of four templates (Agile, Compliance, SRE, Bug report) before anything is sent, then creates the issue, or updates its summary and description on later publishes. Acceptance criteria, completion notes and linked sessions are copied word for word; other sections can be reworded by your LLM key, and any reworded paragraph that doesn't cite a real part of the ticket is dropped. The link is one way: changes in Jira never come back, and status, assignee and sprint are never touched. CLI: `sfs ticket jira preview|publish|status|unlink`. See `docs/integrations/jira.md`.
- **See what a session changed (GitHub #94).** A session's new **Changes** tab lists every file the agent created, edited or deleted, with line counts and a diff per file, rebuilt from the edit, write and patch tool calls captured for Claude Code, Codex, Cursor, Gemini CLI, Cline, Roo Code, Kilo Code, Amp, Copilot CLI and Antigravity. Diffs come from the stored, already-redacted session, so a redacted secret never reappears. API: `GET /api/v1/sessions/{id}/changes`. See `docs/session-changes-capture.md`.
- **Replay an agent run with a different prompt or context (GitHub #95).** `sfs agent replay RUN_ID` (or **Replay** on a run in the dashboard, or the MCP tool `replay_agent_run`) creates a queued copy of a run with an edited prompt, ticket, persona, context references, fail-on level or trigger ref, records which run it came from, and prints the `sfs agent run --run-id` command and a CI snippet to run it. `sfs agent compare A B` (and **Compare** in the dashboard) shows which findings were added, removed or unchanged between two runs. `sfs agent run` gains `--run-id`, `--prompt` and `--context-ref`.
- **Scheduled knowledge-base compile (GitHub #93).** Each project can compile its context on a schedule (`nightly`, `weekly` or `off`) and, optionally, once a number of claims are waiting. Set it under the project's **Settings → Scheduled KB compile** or with `sfs project compile-schedule`. The project Overview shows when the context was last compiled and how many claims are waiting, with a one-click compile when it is out of date; the compile history marks manual and scheduled compiles. Scheduled compiles use the deterministic compiler, never an LLM, and run from the sweep `POST /api/v1/admin/sweeps/kb-compile`. API: `GET /api/v1/projects/{id}/compile-status`.
- **Org admins can propose moving a project into their org (GitHub #96).** An owner or admin of an organization can ask the owner of a personal project to move it into that org, with a reason. The owner sees the proposal in their transfer inbox and by email, and nothing moves until they accept. Proposals expire after 7 days, a declined proposal can't be repeated for 7 days (24 hours after a cancel), and an org can have at most 10 pending. API: `POST /api/v1/orgs/{org_id}/projects/{project_id}/propose-transfer`; CLI: `sfs admin override project-transfer --propose`.
- **Weekly org activity digest (GitHub #97).** Owners and admins of Team and Enterprise orgs get a weekly email with the past week's sessions, tickets, agent runs, knowledge claims, handoffs and admin actions, each linking to the matching view in the dashboard. Each of them can turn it off for themselves under **Organization → Policies → Notifications**. It is sent by the sweep `POST /api/v1/admin/sweeps/org-digest`, once per org per week however often the sweep runs.
- **Persona activity and history (GitHub #92).** Clicking an org persona opens a panel with its recent sessions, open tickets, latest knowledge entry and latest agent run, limited to the projects you can see; org admins also see who has represented the persona over time. Your account page lists the org personas you represent, each linking to its own organization. API: `GET /api/v1/projects/{id}/personas/{name}/rollup`, `GET /api/v1/orgs/{org_id}/personas/{name}/rollup`, `GET /api/v1/orgs/{org_id}/personas/{name}/represent-history`.
- **Operator metrics (GitHub #90).** `GET /api/v1/admin/metrics` serves Prometheus metrics: request latency by route, push outcomes and sizes, pulls, handoffs, DLP findings, authentication failures, active users, session count and database pool use. It needs a platform admin key or a service key with the new `admin_metrics:read` scope; labels never contain ids or raw paths. A ready-made Grafana dashboard ships in `docs/observability/sessionfs-ops.json`, and Helm `api.metrics.scrapeAnnotations` adds scrape annotations. See `docs/observability.md`.

### Changed
- **Large sessions upload in resumable parts (GitHub #91).** Sessions over 8 MiB are pushed in parts (8 MiB by default) that are retried individually, and an interrupted push resumes with only the missing parts instead of starting again. Unfinished uploads expire after 24 hours. Older servers get the single-request push automatically; nothing needs configuring.
- **Knowledge confidence decays with time, not with each compile.** Claims nobody has referenced for 90 days lose confidence (×0.8) at most once per 30 days. Before, the reduction was applied on every compile, so projects compiled often aged their claims much faster.
- **Org settings saves merge.** `PUT /api/v1/orgs/{org_id}/settings` keeps general settings you leave out, and an explicit `null` returns a setting to the default.
- **The MCP server has 83 tools** (`ask_session_helper` and `replay_agent_run` added).
- The Helm chart's dashboard nginx passes `/api/v1/helper/turns` through unbuffered with a 600-second timeout, so helper answers stream as they are written.

### Fixed
- **Codex sessions resume with their tool results attached.** Resuming into Codex gave every tool call a new id, so no tool output matched its call; ids are now kept, and Codex custom tool calls (such as `apply_patch`) and their outputs come back as the same item types they were captured as.
- **DLP policies saved before stricter checks stay editable.** A stored custom pattern without a category or severity, or with a regex that no longer compiles, made every later policy save fail, even one that only turned DLP off. Missing fields now default to `secret` / `high`, only patterns in the current request must compile, a broken stored pattern is skipped during scans instead of failing them, and `GET /api/v1/dlp/policy` lists it under `invalid_custom_patterns`.
- A `409` from a push is treated as a version conflict only when the server says so (`etag_mismatch`). Other 409s are retried with backoff instead of dropping the local change as a conflict.

### Security
- **Remote images in rendered Markdown aren't loaded.** Project context, wiki pages and helper answers only display images from the dashboard itself or inline image data; any other image appears as a link, so a document can't make your browser send data to another site.
- **Jira credentials and requests are confined.** The Atlassian API token is encrypted at rest and never returned or logged. Site URLs must be `https://<name>.atlassian.net`, redirects aren't followed, responses are capped at 2 MiB, and ticket text is redacted for secrets before any of it reaches a model, even when the org has no DLP policy.
- **The session helper sees only what you can.** Its tools check your access on every call, a thread bound to an org re-checks your membership on every turn, it applies the strictest DLP policy of the orgs involved, and messages are stored redacted. Logs record ids, token counts and timings, never prompts, excerpts or keys.
- **Org keys and Jira connections stay where they were set.** Changing the org AI key's provider or endpoint URL requires entering the key again, so a saved key can't be pointed at another host; the helper re-checks a custom endpoint's address before every request in a turn; and only members of the project's organization can preview or publish through its Jira connection.
- Site dependencies: undici 8.12.3 and zod 4.6.7 (advisories).

### Upgrade notes
- **Self-hosted servers: run migrations 084–092.** 084 org helper keys; 085 replay columns on `agent_runs`; 086 digest opt-out and send ledger; 087 transfer proposal columns; 088 resumable upload tables; 089 compile schedule columns (existing projects get `off`); 090 lets retrieval-audit rows have no project (helper turns across projects); 091 Jira tables; 092 helper conversations. All are additive except 090, whose downgrade deletes helper audit rows without a project.
- **Existing projects are not compiled on a schedule** until someone turns it on. New projects start at `nightly` when their plan includes project context.
- **The scheduled features need a scheduler.** The KB compile, org digest and helper retention sweeps run only when something calls them; use a `platform_sweeps:run` service key from a CronJob (see Self-Hosted). The Helm chart can create the helper retention CronJob (`api.helperRetention.enabled`, with `cronJob.existingSecret`).
- **Proxies in front of the chart:** turn off response buffering for `/api/v1/helper/turns` so helper answers stream.

## [0.22.0] - 2026-10-24

**One project per repository, operator merge tools, and a tighter GitLab integration.** Migration 083 (stored git remotes rewritten to one canonical form). No new MCP tools (81).

### Added
- **Merge duplicate accounts.** Platform admins can fold a duplicate signup into the account to keep with `sfs admin override user-merge SOURCE TARGET --reason "..."` (or `POST /api/v1/admin/users/{source_id}/merge-into/{target_id}`). Dry run by default; `--apply` moves sessions, projects, handoffs, tickets, knowledge and the rest in one transaction, deduplicates org memberships (the higher role wins), revokes the duplicate's API keys, deactivates it and writes one audit entry. It refuses rather than guess when both accounts hold an active plan, or the duplicate has billing or marketplace assets.
- **Merge duplicate projects as an operator.** `sfs admin override project-merge SOURCE TARGET --reason "..."` (or `POST /api/v1/admin/projects/{target_id}/merge-from/{source_id}`) runs the existing project merge for a platform admin who owns neither project, including two users' personal projects. Dry run by default; the audit entry commits together with the merge (GitHub #89).

### Changed
- **Project pages use the project id in the URL** (`/projects/proj_...`). Old links by remote redirect, and a merged project's link opens the project it was merged into.
- The session transcript renderer is split into smaller modules (no visible change).

### Fixed
- **The same repository no longer becomes two projects.** Remotes written as `ssh://`, `git://`, `git+ssh://`, scp style with a user other than `git` (or none), with an uppercase scheme or a trailing slash were stored as written, so each spelling made a separate project. All of them now map to the repository path, project creation normalizes the value it's given, and migration 083 rewrites stored remotes. Where two projects already exist for one repository, the migration leaves the pair alone and prints both ids so an operator can merge them. Repositories in a home directory or at an absolute path on a personal server keep their host, so different machines don't collide (GitHub #88).
- Merging accounts clears a session alias the target already uses and keeps one sync-watchlist row per session.

### Security
- **GitLab instance URLs are validated.** Saving a GitLab instance URL that points at a private, loopback or cloud-metadata address is refused (`422 invalid_gitlab_instance_url`), the host is re-checked before every request so a DNS change after saving can't redirect the token, and redirects aren't followed. Self-hosted deployments with GitLab on a private network can allow private addresses with `SFS_GITLAB_ALLOW_PRIVATE_HOSTS=true` (Helm `api.gitlab.allowPrivateHosts`); metadata addresses stay blocked.
- The SSRF guards now judge IPv4-mapped IPv6 addresses (`::ffff:169.254.169.254`) by their IPv4 part, and also block `0.0.0.0/8` and `100.64.0.0/10`.
- **Custom LLM endpoints are checked the same way.** A user-supplied `base_url` (judge settings, audit and narrative-summary requests, model discovery) was fetched by the server without any address check, so a signed-in user could make the API send requests to internal or metadata addresses. The URL is now checked when it is saved or submitted and again before every request, redirects are not followed, and on the hosted service it must be a public https endpoint. Self-hosted deployments whose model gateway runs on a private network keep working: the Helm chart sets `SFS_LLM_ALLOW_PRIVATE_HOSTS=true` (`api.llm.allowPrivateHosts`); metadata addresses stay blocked.
- Model discovery takes the API key in the request body (`POST /api/v1/settings/judge/models`); `GET` with an `api_key` query parameter now returns 400, because query strings end up in access logs. `GET` without one still uses the saved key.
- Dashboard dependencies: zod 4.6.7 and undici (memory-exhaustion and denial-of-service advisories).

## [0.21.1] - 2026-10-23

**Project context you can actually read, a GitLab card in the dashboard, and steadier releases.** No server migrations. No new MCP tools (81).

### Added
- **GitLab in the dashboard.** Settings → Integrations → GitLab connects, updates or disconnects your GitLab instance (gitlab.com or self-managed) for merge-request context comments. Tokens and webhook secrets are write-only: the form never shows them back.
- **Section list for project context.** The Overview tab shows an "On this page" list of the context's sections beside the text, highlighting the one you're reading; on phones it's a collapsible list above it.

### Changed
- **Project context and wiki pages render as documents.** Bullets, numbered lists, quotes, tables, task lists and code blocks now display properly, each line an agent writes stays on its own line, long ids and URLs wrap, and the text uses the page width at a comfortable reading length. Raw HTML in a document is never rendered, and only web, mail and in-page links are clickable.
- **GitLab trusted publishing pins the namespace by default.** `sfs skill publisher trust --gitlab-project ID` looks up the project's current namespace and pins it, so a project moved to another namespace can't keep publishing. Pass `--gitlab-namespace ID` to set it yourself or `--no-namespace-pin` to opt out (with a warning). The server warns on unpinned GitLab trusts and the dashboard marks them.
- **Session list.** Rows no longer nest buttons inside a clickable row, so screen readers and keyboards handle them properly. In a group of follow-up sessions, the title expands the group and **View** opens the session.
- Dashboard buttons and colours use the shared components and theme colours throughout; a CI check now rejects hard-coded colours.
- React 19.2.7, Tailwind CSS 4.3.3.

### Fixed
- Platform administrator accounts were treated as Free in plan checks when they also had the plan record created by migration 050. They now always count as Enterprise.
- Checkpoints created within the same filesystem timestamp are listed in a stable order.

### Security
- `GET /api/v1/settings/gitlab` returned the stored GitLab webhook secret on every read. It now reports only whether one is set (`has_webhook_secret`); `PUT` returns the secret once, and only when the server generated it.

## [0.21.0] - 2026-10-22

**A reorganized dashboard that works on phones, GitLab CI trusted publishing for the marketplace, and an audit trail for operator overrides.** Server migrations 081–082. No new MCP tools (81).

### Added
- **GitLab CI trusted publishing (GitHub #82).** GitLab CI jobs can exchange a GitLab ID token (`id_tokens:`) for the same single-use, 15-minute publish token GitHub Actions gets, so no stored `skills:admin` key is needed in CI. A trust binds to the numeric GitLab project (optionally the namespace), requires a protected ref, matches the ref pattern and environment, and only accepts push, web and API pipelines; merge request, scheduled, trigger and downstream pipelines never publish. A trust can also require a protected GitLab environment. Accepted GitLab instances come from `SFS_GITLAB_OIDC_ISSUERS` (default `https://gitlab.com`, `none` disables). Set it up with `sfs skill publisher trust --gitlab-project ID [--gitlab-host HOST]` and publish with `sfs skill publish --trusted-publishing --ci-provider gitlab`. Example pipeline: `docs/integrations/gitlab-ci-skill-publish.yml`.
- **Audit trail for operator overrides (GitHub #84).** Admin actions now record a reason, the before and after state, and whether they went through the API or were recorded by hand. `POST /api/v1/admin/projects/{id}/force-transfer` moves a project into an organization without the owner's consent, under the same safety checks as a normal transfer; the new owner must be an active member of the destination org, and `expected_org_id` stops a retried request from moving a project twice. `POST /api/v1/admin/audit-log` records a change made by hand, and `GET /api/v1/admin/audit-log` filters by target, action, source and admin. User and org tier changes and forced owner transfers accept a reason. CLI: `sfs admin audit-log`, `sfs admin override project-transfer|record`.
- **`sfs --version`, `sfs -V` and `sfs version` (GitHub #85)** print the installed version with the Python version and platform.

### Changed
- **Dashboard reorganized.** The project page's 12 tabs are now 5 sections (Overview, Knowledge, Work, Rules & Skills, Settings), kept in the URL so links and the back button work; old links still open the right place. Project rename, repos, merge, transfer and delete live under **Settings**. Account settings are split into Account, Preferences, Integrations, Data protection and Sync, each with its own address. The Organization page has tabs (Members, Personas, Policies, Security). The sidebar is grouped into Library, Inbox and Manage, and the marketplace pages share one sub-navigation. The project-level Marketplace tab is now called **Skills**.
- **Dashboard works on phones.** Every page fits a 375 px screen without sideways scrolling, and tab bars scroll instead of spilling out of their card (GitHub #86, #87).
- **Dashboard colours and accessibility.** Status colours in the light theme meet WCAG AA contrast; the light-theme brand blue is slightly darker, and filled brand-coloured buttons in the dark theme use dark text. Tabs follow the standard keyboard pattern (arrow keys, Home, End), notifications are announced reliably to screen readers, and error messages use one wording.

### Fixed
- **`/health/ready` through the Helm chart's ingress (GitHub #83).** The dashboard's nginx answered `/health/ready` with its own `{"status":"ok"}`, so readiness probes through the ingress always saw 200 even when the database schema was behind. `/health/ready` is now passed to the API, and other paths under `/health/` return 404.
- Verifying an SSO domain no longer reports an error when the domain is deleted at the same moment.

### Upgrade notes
- **Self-hosted servers: run migrations 081 and 082.** 081 adds reason, before/after state and source columns to `admin_actions`, plus indexes. 082 lets `skill_trusted_publishers` hold GitLab trusts (GitHub-only columns become nullable, with a check that keeps each provider's row consistent); downgrading it removes GitLab trusts and their tokens.
- Self-hosted GitLab users who want trusted publishing: add your instance URL to `SFS_GITLAB_OIDC_ISSUERS`, exactly as GitLab's `external_url`.

## [0.20.0] - 2026-10-22

**An org persona can stand for a person, who can then keep it up to date without being an org admin. Plus project tools that accept the folder your agent is working in.** Server migration 080. No new MCP tools (81).

### Added
- **Org personas that represent a member (GitHub #81).** An org owner or admin can link an org persona to the member it stands for. That member can then update the persona's role, content and specializations without org admin rights; turning the persona on or off, changing or removing the link, creating and deleting stay with admins. Set the link on the dashboard (**Organization → Personas → Represents**), with `sfs persona create --org ORG --represents EMAIL` or `sfs persona represent NAME --org ORG --user EMAIL|--clear`, or through the MCP `create_persona` / `update_persona` tools (`represented_user`). Members see their own persona under **Your org persona**. Linking a project persona is refused.

### Changed
- **Project MCP tools accept a folder as `git_remote`.** Agents often pass the directory they work in. A value starting with `/`, `~`, `./` or `../` is now resolved through that repository's `origin` remote (git worktrees included), instead of being looked up as a remote and returning "No project found". A folder that isn't a git repository with an `origin` gets a clear error instead of acting on another project; leaving `git_remote` out still uses the current workspace. The "No project found" message now says `git_remote` takes a remote URL such as `github.com/org/repo`.

### Security
- Removing a member from an organization, or deactivating a user, clears any persona link to them, with an entry in the organization's audit log. A member who rejoins does not get edit rights back until an admin links them again.

### Upgrade notes
- **Self-hosted servers: run migration 080** (adds a nullable `agent_personas.represented_user_id`; on PostgreSQL also a foreign key that clears the link when the user is deleted, and a check that only org personas can have one). Existing personas represent nobody.

## [0.19.0] - 2026-10-21

**Find the right skill, install it with your tool's own plugin command, and see where it really works. Plus an org-wide skills policy for Enterprise, Git registries that publish themselves, and much faster pushes of large sessions.** Adds 1 MCP tool (80 → 81) and server migrations 076–079.

### Added
- **Marketplace discovery.** Browse by category and filter by publisher, verified publisher, trusted-publishing provenance, license, worst open scan finding, capability (instruction-only, scripts, MCP servers, hooks, executable extensions, pre-approved tools), the tools a package is verified to work in, and how recently it was updated. The filters are the same on the dashboard, `sfs skill search`, `sfs skill categories`, the API and the MCP `search_skill_marketplace` tool. Ranking is fixed and documented (no stars or reviews). Listings show the package README, rendered safely: no raw HTML, no remote images, and images inside the package are checked by content.
- **Install counts and freshness.** Listings show 30-day installs per tool (counts under 10 show as `<10`), last updated and last verified per tool, and **stale**, **deprecated** (with an optional successor) and **unmaintained** flags. Publishers get an analytics view (dashboard **Publishers → Analytics**, `sfs skill publisher stats`), and Team organization admins can see what their own members use (`sfs skill usage --org`). Install counts are sent only after a one-time notice, only while signed in, and stop with `SFS_SKILL_USAGE_REPORTING=0` or either telemetry opt-out (see `docs/telemetry.md`).
- **Native tool catalogs.** Approved packages are published as read-only git catalogs that each tool's own plugin command can add: Claude Code `/plugin`, Copilot CLI, the Codex Plugins Directory, Cursor team marketplaces and Gemini CLI extensions. `sfs catalog list|add|doctor` sets them up, with your key sent only through the `sfs catalog credential` helper and never in a URL. There is a public catalog for every signed-in user, a personal catalog (Starter and above) and an organization catalog (Team and above). Blocks, yanks and quarantines remove entries on the next fetch. `sfs catalog export --to <remote>` mirrors a catalog to a repository you own, and can run unattended with `--ssh-key-file`.
- **"Works in" matrix.** Every listing shows, per tool, whether the tool's own installer accepted the package (`verified`, with tool version and date), whether the generated entry matches the tool's documented format (`format-valid`), whether it `regressed` on the newest tool version, or only what SessionFS `expected`. `sfs skill works-in <id>` and MCP `get_marketplace_package` show it too. Verification runs in an isolated sandbox that never runs a skill; checks on private packages are a Team feature.
- **Org skills policy (Enterprise).** One policy, applied on the server (install requests, approvals, assignments and MCP loads are refused when the policy forbids a version) and compiled into each tool's own managed settings: Claude Code and Copilot `managed-settings.json`, a VS Code device policy, Gemini CLI system settings, Codex `requirements.toml`, and a Cursor dashboard checklist. Every field says whether the tool enforces it or only SessionFS checks it. New `sfs policy show|compile|status|set|disable`, the MCP tool `get_skill_policy`, and a `devices:report` scope for compliance reports. Delivery starts in advisory mode; deploy the compiled files through MDM for tool-level enforcement. An organization that leaves Enterprise keeps its last policy enforced, and owners and admins can relax or disable it but not tighten it.
- **Git registries publish themselves (GitHub #69).** An organization registry on `https://` can be polled by the server: every new tag matching the registry's pattern becomes a new, immutable version, through exactly the checks of a manual publish (validation, DLP, scanning, disclosure, quotas, blocks). The dashboard shows each registry's poll status with a **Poll now** button. Private repositories use a credential reference (`env:` or `k8s:`), never a token. The fetch refuses private and metadata addresses, follows no redirects, runs no hooks and checks nothing out.
- **`platform_sweeps:run` service-key scope** so scheduled jobs can run the platform sweeps without a personal admin key. The key stops working if its owner or the admin who created it loses platform admin; report-only and forced rescans still need an admin's own key.

### Changed
- **Pushing large sessions is much faster (GitHub #71).** The server's secret scan skips lines and positions a pattern cannot match and redacts large sessions in one pass instead of value by value, and the work no longer blocks other requests. A push whose `If-Match` is already out of date is refused before any scanning. Pushes report how long each phase took in a `Server-Timing` header and the server log, and `SFS_LOG_LEVEL` now takes effect.
- **The MCP server has 81 tools** (`get_skill_policy` added).
- The Helm chart's API image now includes git and an SSH client (registry polling and catalog export), with new `api.marketplace.publicCatalogMirror` and `api.registryPolling` values.

### Security
- Email delivery logs no longer include full recipient addresses or, for the logging-only provider, message subjects.
- Marketplace text shown in the terminal has control characters stripped.
- A native catalog whose rebuild fails stops serving (503) rather than keep shipping a version that was since blocked, yanked or quarantined.
- Skills-policy source pins must be full 40-character commit SHAs and match exactly.
- `certifi` 2026.10.19 or later is required, dropping revoked certificate intermediates.

### Upgrade notes
- **Self-hosted servers: run migrations 076–079** (discovery, native catalogs, org skills policy, registry polling). All are additive.
- **Registry polling is off for existing registries** until an owner or admin turns it on. Polling needs a scheduler calling the dedicated poll endpoint with a `skills_registry:poll` key (see Self-Hosted).

## [0.18.0] - 2026-10-20

**A marketplace you can trust: see exactly what a skill can do before you install it, get asked again when an update wants more access, and have dangerous packages scanned, quarantined or taken down on every device. Plus verified publishers with trusted publishing from GitHub Actions, org-level personas, Antigravity session capture, and safer GitHub and billing integrations.** Adds 1 MCP tool (79 → 80) and server migrations 065–075 (067 and 068 are not used).

### Added
- **Install disclosure for every skill package.** `sfs skill inspect` and `sfs skill add`, the dashboard and the MCP tool `get_marketplace_package` show, before you consent, what a package will do in each tool: the files it writes, scripts and their interpreter, hooks, tool pre-approvals, MCP servers, network endpoints, environment variables, shell commands its instructions tell the agent to run, scanner findings, and hidden or bidirectional Unicode characters (shown with file, line and column). Local packages carrying the hidden characters the publish gate refuses need `--allow-hidden-unicode`.
- **Updates are classified and re-consented.** Every update is `text` (instructions or assets only: one confirmation, your earlier consent carries forward), `code` (script, hook or MCP bytes changed: the capability's flag again) or `widening` (any new access: always stops until you pass `--approve-widening` or an org admin approves that exact version). `sfs skill pull` and the daemon apply text updates and hold the rest, listing them as `held: <name> needs approval`. `sfs skill diff` and the new MCP tool `diff_marketplace_versions` show what changed between two versions. The same rules apply to `rollback` and `sync --locked`.
- **Held updates in the dashboard.** "Updates to review" in an organization's managed library lists held updates with the diff and the new version's disclosure; an admin acknowledges the disclosure and advances the assignment. Approving or assigning a newer version that widens access or changes code needs an explicit re-approval bound to what was shown.
- **Hosted marketplace installs from the CLI.** `sfs skill add sessionfs:<namespace>/<name>[@version]` checks the content hash three ways before installing, and the lockfile pins the exact version and server. `sfs skill search` includes hosted results (`--tool` filters by compatibility), and `sfs skill pull` installs your organization's required assignments and your approved install requests (optional assignments are offered, never forced). With cloud sync on, the daemon does the same for required assignments every 15 minutes.
- **Interrupted installs recover.** An install or update that a crash or kill interrupted is settled at the start of the next `sfs skill pull` or daemon run, before any network work. If settling it would need a capability you never consented to, it is left alone and reported with the flag that settles it.
- **Publish from the CLI and dashboard.** `sfs skill validate` runs the server's own publish checks locally, with each problem's file, line and fix; `sfs skill publish` validates the same bytes and uploads them to your personal library or an organization. The dashboard marketplace page has a publish dialog for pre-built archives.
- **Reports, takedown and revocations.** Anyone can report a listing. Platform moderators, and an organization's owners and admins for their own listings, can yank (keep installed copies, refuse new installs) or block (remove from every device) a version or a whole package, with a required reason and an audit record, from the new dashboard moderation queue. Blocked content is refused by content hash, so deleting and re-publishing the same bytes does not bring it back. `sfs skill pull` and the daemon follow the revocations feed: blocked packages are moved into `~/.sessionfs/skills/quarantine/` (your edits are kept there), yanked ones show a warning in `sfs skill list` and `sfs skill doctor`.
- **Instruction scanning.** Every published version is scanned for dangerous instructions and scripts: fetch-and-run, encoded execution, credential harvesting, exfiltration endpoints, permission bypass and hook auto-approval, persistence, concealment, defense evasion and hidden Unicode. Critical findings refuse the publish; others are shown on the listing and in the disclosure. Live versions are rescanned weekly and whenever the ruleset changes; a new critical finding quarantines the version (no new installs, downloads, approvals or MCP loads; existing installs are warned, never removed) until a moderator blocks or restores it. This release ships ruleset `2026.10.6`.
- **Verified publishers.** `sfs skill publisher claim|list|show|verify|check` claims a globally unique namespace and verifies it with a DNS TXT record or a GitHub organization or account. Listings show a `verified:` badge; names that imitate a reserved brand or a verified publisher (lookalike letters from any script, digit swaps, typos, `-official` suffixes) are refused. A DNS record missing for seven days drops the badge until it is back. Public publishing is open to verified publishers that SessionFS has enabled, and a publisher's first public version, or an update that widens access, waits for review.
- **Trusted publishing from GitHub Actions.** `sfs skill publisher trust` allows a specific repository and workflow (optionally a protected environment and ref pattern) to publish without storing an API key; `sfs skill publish --trusted-publishing` exchanges the job's OIDC token for a single-use, 15-minute publish token. Pull-request events never publish. Listings show where each version was published from. Example workflow: `docs/integrations/github-actions-skill-publish.yml`.
- **Org-level personas (GitHub #70).** Define a persona once for an organization and use it in every project in that org. Owners and admins manage them in the dashboard, with `sfs persona … --org`, the org persona API, or MCP `create_persona` / `update_persona` / `delete_persona` with `org_id`. A project persona with the same name overrides the org one for that project. `sfs persona list` and `pull` include org personas unless `--no-org`. Moving a project to another org is refused while open tickets are assigned to org personas the destination doesn't have (`--force` overrides).
- **Antigravity session capture.** A new watcher captures Google Antigravity conversations from the desktop app, the `agy` CLI and the IDE build, including tool calls and results, with subagent conversations folded into their parent. Conversations that exist only in encrypted form are skipped and counted in `sfs daemon status` and `sfs doctor` instead of being half-captured. `sfs init`, `sfs watcher enable antigravity`, `sfs recapture` and `sfs mcp install|uninstall --for antigravity` support it. Capture-only, like Cursor.
- **Verified GitHub sign-in when linking the GitHub App.** Linking an installation now requires signing in to GitHub, and SessionFS links it only after GitHub confirms that account can access the installation. Repository verification additionally needs a proof, refreshed at least every 7 days, that you are the installation's account or an owner of its organization; the dashboard shows **Re-verify with GitHub** when it lapses. Organization installations need the App's **Members: Read** permission for this.

### Changed
- Project features (project context and wiki writes, personas, tickets, agent runs and agent feedback) are now checked against the plan of the project's owner — the owning organization, or the owner's own plan for personal projects — instead of the plan of whichever organization the caller belongs to. Members of a paid organization now get its features in its projects even when their own default organization is on the free plan. Service keys are checked against their organization's plan.
- Existing usage is kept: a project that already used a feature before this release keeps it for anyone whose own plan includes it (recorded once by migration 065). New usage comes from the project's plan.
- An upgrade prompt raised inside a project now says it is the project's plan (`plan_scope: "project"`, `current_tier` = the project's plan), and the plan is only checked after access to the project is confirmed.
- **Organization creators are owners.** Creating an organization with `sfs org create`, the dashboard or platform provisioning now makes the creator its owner (it used to make them an admin, leaving the org without an owner for ownership transfer, SSO break-glass and the owner's SSO exemption). Owners now also have admin standing everywhere admins do, including Team checkout, the billing portal and project transfers.
- **Billing is reconciled more carefully.** A checkout can't open a second subscription for the same account, events that can't be matched to exactly one owner are set aside for review instead of being guessed at, and a paying account is never downgraded by an out-of-order event. If the billing provider can't be reached to confirm a subscription, checkout and organization creation answer `503 billing_unavailable` (try again shortly) rather than proceeding unconfirmed.
- **Settings → Account shows your effective plan**, including a plan you get through your organization, instead of the personal plan on your user record.
- **Persona names resolve case-insensitively in both project and org scope**, preferring an exact-case match, everywhere a persona is looked up (ticket assignment and start, agent runs, knowledge, wiki and handoff provenance).
- **The MCP server has 80 tools** (`diff_marketplace_versions` added).
- **`sfs mcp install` and `uninstall` edit config files atomically**, checking every target file first, keeping a timestamped backup of each, and leaving files that aren't valid JSON untouched.

### Fixed
- **Large sessions couldn't be pushed** (GitHub #68). Uploads gave up after a fixed 30 seconds, before a self-hosted server with DLP scanning could finish storing a multi-megabyte session. Uploads and downloads now wait up to 5 minutes by default. You can change that with `sfs push --timeout`, the `SFS_UPLOAD_TIMEOUT` environment variable, or `upload_timeout` under `[sync]` in `config.toml`. A push that still runs out of time now stops at once with a message saying so and how to raise the limit, instead of resending the same upload three times and ending with a blank error. The same applies when a proxy in front of the server gives up first (HTTP 504). Self-hosted Helm installs: the built-in nginx now waits up to `dashboard.apiProxyTimeout` (default 300s) for the API, and the Ingress passes matching timeout and body-size limits to ingress-nginx unless you set your own. Logging in again (`sfs auth login`) now keeps other `[sync]` settings such as `upload_timeout` and `auto` instead of resetting them.
- Merging projects now moves agent feedback records along with the runs and tickets they refer to, and carries over the source project's kept features.
- **Sessions with a detected slash command failed format validation** because `tools.json` didn't allow the fields the skill detector writes. The schema now describes them.
- **`sfs delete` and pruning could leave a session in the local index** when the daemon was tracking it.
- **Kilo Code got the generic context budget** instead of the Cline-family budget the docs promised; Antigravity now has its own.

### Security
- **GitHub webhooks fail closed.** A server without `SFS_GITHUB_WEBHOOK_SECRET` used to skip signature checks, so forged deliveries could remove installation records or trigger PR comments. Every delivery is now refused with 503 until a secret is set (local development can opt in to unsigned deliveries with `SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED=1`). Deliveries are de-duplicated against replays.
- **GitHub installation claims could be hijacked.** Linking an installation by its numeric ID proved nothing about who controlled it, so another user could claim someone else's installation and have repositories recorded as verified. Linking now requires the GitHub OAuth proof described above, and migration 073 resets every repository verification that came from an old installation claim to owner-attested. Links are kept; owners re-verify by linking the repository again after signing in to GitHub. A GitHub publisher badge needs the same recent owner or admin proof.
- **Repo-reclaimed projects can no longer be squatted** through the placeholder remote they used to carry.
- **SSO and publisher domains share one canonical spelling** (lowercase, no trailing dot), so the same domain can't be verified twice under different spellings.

### Upgrade notes
- **Self-hosted servers: run migrations 065–075.** Migration 073 resets GitHub-verified repositories to owner-attested (see Security). Migration 071 canonicalizes and de-duplicates SSO domain verifications: if two organizations had verified the same domain under different spellings, the later one is marked failed and that organization needs to verify its SSO domain again. Migration 074 gives every ownerless organization an owner from the strongest evidence available and logs any it can't resolve for manual repair; migration 075 likewise logs subscriptions with ambiguous ownership for manual billing review instead of guessing.
- **Skills installed before this release ask for their capability flags once on the next update.** Consent recorded earlier never carries forward; give the flag once (for example `sfs skill compile <name> --allow-scripts`) to record current consent. Hosted packages installed before their server was recorded need `sfs skill update <id> --rebind-origin`.
- **Rescans may quarantine more packages.** Ruleset `2026.10.6` closes several scanner bypasses, so the first rescan after upgrading can quarantine versions that passed before. Existing installs stay installed and are warned.
- **GitHub App:** set `SFS_GITHUB_WEBHOOK_SECRET`, or every webhook is refused with 503. Linking installations needs `SFS_GITHUB_APP_CLIENT_ID`, `SFS_GITHUB_APP_CLIENT_SECRET`, `SFS_GITHUB_OAUTH_CALLBACK_URL` and `SFS_DASHBOARD_URL`; without them linking is disabled. Configure the App's two callback URLs in order, keep "Request user authorization during installation" off, and approve the **Members: Read** permission on organization installations (see `docs/github-app.md`). Existing links keep PR comments working but must be re-verified before they verify repositories.
- **Trusted publishing is disabled unless `SFS_PUBLIC_API_URL` or `SFS_TRUSTED_PUBLISHING_AUDIENCE` is set**, and a server configured with a generic audience such as `sessionfs` refuses to start.

## [0.17.1] - 2026-10-14

**The daemon stops hammering the server on failing pushes, and server logs stop recording session content.**

### Added
- **`sfs daemon retry-sync [SESSION_ID]`** asks the running daemon to retry sessions whose sync is backing off or paused (a unique prefix of the ID works; `--config` targets a daemon started with a custom config).

### Changed
- **Sync backs off on repeated failures.** When pushes for a session keep failing with server errors, rate limits or network problems, the daemon now retries after 1 minute, 5 minutes, 15 minutes, then hourly, instead of every push interval. After 12 failures over at least six hours it pauses that session until its content changes, the daemon restarts, or you run `sfs daemon retry-sync`. `sfs daemon status` and `sfs doctor` list these sessions with their last error.
- **Local cleanup never removes changes that haven't been uploaded.** A session with newer local changes than the server holds is no longer treated as synced, and sessions the daemon still intends to upload are skipped by automatic pruning.

### Fixed
- **One session was retried 15,670 times.** Its pushes failed because of a NUL byte (fixed in v0.17.0); the daemon now backs off instead.
- **Server error logs could include session content.** Failed database statements no longer log their values, and integrity errors log only the error type and constraint, never the row data the database quotes.
- **CI type check on Python 3.12** for the agent feedback summary.

## [0.17.0] - 2026-10-14

**Agent feedback records, self-service org membership for enterprises, and a round of fixes for self-hosted teams.** Adds 3 MCP tools (76 → 79) and server migration 064.

### Added
- **Agent feedback records (GitHub #54).** Record whether an agent's work was useful once a human has reviewed it: routing, summaries, actionability, duplicates and outcomes, each with a verdict and an optional label from a fixed vocabulary. Records attach to an agent run, a ticket or a ticket comment and are append-only. A summary endpoint counts them by type, verdict and label, optionally per persona. When a ticket created by an agent is moved from one persona to another, a `wrong_route` record is captured automatically. Available on Team and Enterprise through the API, the new MCP tools `record_agent_feedback`, `list_agent_feedback` and `get_agent_feedback_summary`, and `sfs agent feedback add|list|summary`. New service-key scopes `agent_feedback:read` and `agent_feedback:write`.
- **Auto-join by verified email domain (GitHub #59).** Organizations can list domains they have verified; when someone verifies an email address on one of those domains and isn't in an organization yet, they join as a member. Seat limits apply, people who were removed or already invited are never added this way, and nobody becomes an admin automatically. Requires a paid plan with SSO and real email verification. Manage it in the dashboard or with `sfs org auto-join list|add|remove`.
- **`sfs org set-role`** changes a member's role by user ID or email.
- **`sfs org remove` accepts an email, and `--bulk <file>` removes many members.** Every entry is checked against the member list before anyone is removed.
- **Readiness endpoint `/health/ready`** reports whether the database schema is migrated for the running version. The Helm chart's readiness probe uses it.

### Changed
- **DLP redaction works on decoded JSON values.** Secrets are now also found in JSON field names, in `"field": "value"` pairs, inside quoted strings and in malformed lines. Content that DLP can't fully inspect (for example, nested too deeply or with repeated keys) is refused rather than stored as clean. Redaction keeps every other value exactly as it was, including numbers. The public share check uses the same scanning.
- **Helm under ArgoCD runs in order:** database first, then migrations, then (in local license mode) the license seed, then the API and MCP servers. Fresh Helm installs create the migration job alongside the database instead of as a pre-install hook.
- **Self-hosted: domain auto-join requires `SFS_VERIFICATION_SECRET`** to be set to a random value of at least 32 characters (the Helm chart sets one). Email verification links must also match the account's current address.

### Fixed
- **DLP redaction could corrupt session files (GitHub #65).** Redacting a quoted value could leave unbalanced escaping, so `sfs resume` and handoff pickup failed. `sfs resume` now also skips lines already damaged this way and says how many it skipped.
- **A NUL byte in session content caused a 500 on push (GitHub #67)** and the daemon retried forever. The push is now accepted and the NUL is stripped from the copies stored in the database; the session archive itself is unchanged.
- **The Codex watcher skipped sessions containing `"info": null` (GitHub #64).**
- **Database migrations didn't run before the API under ArgoCD (GitHub #66).** The API also logs a clear error at startup if the database schema doesn't match the code.
- **Fresh installs in local license mode never started**, because the license check waited on the API it was guarding. The check now runs in cloud license mode only.
- **Concurrent membership changes.** Accepting an invite, creating an org, activating a license, auto-join, and verifying or deleting a domain now take their locks in one consistent order, so they can't exceed the seat limit, add a user to two orgs or deadlock.
- **Saving org settings could undo a concurrent change**, and saving only KB settings cleared auto-join domains.
- **Member removals are now recorded in the organization audit log.**

## [0.16.1] - 2026-10-12

**Starter gets the storage it pays for, the Marketplace gets a launch catalog, and hosted publishing is hardened.**

### Added
- **Launch catalog: `sessionfs-workflows`.** An MIT-licensed, SessionFS-authored package with three instruction-only skills (no scripts, hooks, or MCP servers): `session-handoff`, `ticket-workflow`, and `project-knowledge`. Source lives in `catalog/sessionfs-workflows`; a test keeps it publishable through the hosted validator.

### Changed
- **Pro plan storage is 2 GB**, matching the pricing page (was 500 MB in the plan definition).
- **`/sync/status` reports each paid plan's storage** (Starter 500 MB, Pro 2 GB, Team 1 GB per user, Enterprise unlimited) instead of the per-session upload cap. Free still reports its 50 MB sync cap.
- **Hosted skill packages are capped at 48 MiB unpacked** (about 58 MiB compressed), down from 128 MiB. Local `sfs skill` imports keep the 128 MiB limit.

### Fixed
- **The oversized-session error suggested `sfs compact`, which does not exist.** It now points to upgrading for a larger per-session limit.
- **Starter customers were held to Free-tier sync limits.** Starter now gets the paid per-session cap (300 MB) and per-file cap (50 MB), like every other paid plan.
- **Publishing a skill package could stall the API.** Oversized uploads are rejected from `Content-Length` (or a running byte count for chunked bodies) before the form is read. Callers are authenticated before a publish slot is taken; each API process admits at most two publishes at a time and one per user, answering `503 publish_busy` with `Retry-After` beyond that; a body that stalls or takes longer than two minutes gets `408 publish_timeout`; validation runs off the event loop, and a slot is not freed until its validation thread has really finished.
- **Organization publishes had no storage quota.** Org and org-visible publishes are charged against the organization's entitlement storage, counting members' synced sessions and the org's packages live. Every package is limited to 100 stored versions. Curated public listings by platform administrators are never charged to the org, but do count toward the version cap. An identical re-publish that races another reuses the existing version instead of hitting the cap.
- **The personal publishing quota used the 300 MB per-session cap as total storage.** It now uses the plan's storage (Starter 500 MB, Pro 2 GB, Team 1 GB, Enterprise and platform administrators unlimited), matching `sfs sync status`.
- **A malicious Git source could exhaust memory during `sfs skill add`.** `git ls-tree` output is streamed and git and any helper processes it started are stopped as soon as the file-count, depth, record-size, or time limit is exceeded.
- **Skill portability tests failed on Linux CI** because of forced colour output and case-insensitive filename assumptions; the v0.16.0 API deploy was blocked by this and ships with this release.

## [0.16.0] - 2026-10-11

**Skills and plugins across every tool, plus the SessionFS Marketplace.** Install a reusable Agent Skill or Agent Plugin once, pinned to an exact version, and use it in all supported coding agents — with a clear report of anything a tool can't support. Teams can browse a hosted catalog, share packages inside their organization, and approve what agents may load. Adds 8 MCP tools (68 → 76) and server migration 063.

### Added

- **`sfs skill` (alias `sfs plugin`).** Inspect, install, update, remove and roll back Agent Skills and Agent Plugins from a local path, a Git repository pinned to an exact commit, or a registered marketplace (including Claude Code marketplace repositories). One command installs for Claude Code, Codex, Gemini CLI, Cursor, GitHub Copilot, Amp, Cline, Kilo Code and Roo Code (legacy, since Roo's upstream repository was archived). Standard skills are kept byte-for-byte; anything a target tool can't express is listed as omitted rather than silently dropped.
- **Safe installs.** Every install is planned first, checked for path safety and collisions, and applied as one transaction that rolls back cleanly on failure. Files you edited are never overwritten or deleted. `sfs skill doctor` reports drift and missing files.
- **Reproducible setups.** A committed project lockfile plus `sfs skill sync --locked` reproduces exactly the same packages on another machine or in CI.
- **Explicit consent for risky content.** Packages with scripts, hooks or tool grants need a specific flag (`--allow-scripts`, `--allow-hooks`, `--allow-executable-extensions`), bound to that exact package version; `--yes` alone is refused. Any change to a package's contents asks again.
- **Multiple marketplaces.** Register several named marketplace sources per user or project with `sfs skill marketplace add|list|refresh|remove`, and search them with `sfs skill search`. Packages with the same name in two marketplaces never silently replace each other.
- **SessionFS Marketplace (hosted).** Browse packages in the dashboard with their source, exact content hash, license, risk flags and a per-tool compatibility view. Every version is immutable and scanned for secrets on upload. Browsing and downloading are open to every signed-in user, including the Free tier.
- **Publishing.** Starter and above can publish private personal packages. Team and Enterprise organizations can publish to their organization, and the public catalog is curated. Organization and public packages must declare a license.
- **Team distribution.** Team and Enterprise organizations can manage registries, assign exact package versions to the organization, a member or a device, and run an install-approval queue per project. Enterprise organizations can attach policy to assignments.
- **MCP marketplace tools.** Agents can search and preview marketplace packages, load an approved instruction-only skill into the current session (always marked as untrusted third-party text), and request an install. They can't write files, run scripts, enable hooks or start MCP servers; approval happens only in the dashboard or API, and approving your own request requires an emailed confirmation code.
- **Package provenance in captured sessions.** Sessions record which skill packages were active, on every tier.

### Security

- Skill package uploads have a dedicated Cloud Armor rate limit.
- Skill imports enforce per-file, per-package and file-count limits, refuse unsafe paths and symlinks, and never execute imported code while fetching, scanning or planning.

## [0.15.6] - 2026-10-09

**Site and docs.** Upgrades the documentation site to Astro 7 and Starlight 0.42, which clears every npm advisory in the site build and fixes tables in the docs. No CLI, server, schema or MCP tool changes; migrations remain at 062.

### Fixed

- **Tables in the documentation render again.** The docs site was showing markdown tables (CLI flags, environment variables, API fields and others) as raw pipe-separated text. They now render as tables across all docs pages.
- **The docs no longer point to an unavailable hosted MCP endpoint.** The remote-MCP pages told users to connect web clients to `https://mcp.sessionfs.dev`, which is not currently offered. They now say remote MCP is not currently supported (hosted or self-hosted) and point to the local MCP server, which works with every supported tool. The self-hosting docs mark the chart's MCP component as experimental.

### Security

- **Site build dependencies have no known vulnerabilities.** Upgrading Astro (6 → 7) and Starlight (0.38 → 0.42) and refreshing transitive packages clears the 19 advisories reported against the site build (1 critical, 7 high, 11 moderate), including in development dependencies. The site is static, so these affected the build toolchain rather than visitors.

## [0.15.5] - 2026-10-08

**MCP and autosync fixes.** The local MCP server (`sfs mcp serve`) now connects immediately, no matter how many sessions you have, and autosync now syncs sessions as they are captured instead of only when the daemon starts. No hosted server or MCP tool changes; server database migrations remain at 062. The local search index (`search.db`) gains a column and is upgraded in place automatically.

### Fixed

- **The local MCP server no longer times out on startup for heavy users.** On every start, `sfs mcp serve` rebuilt its full-text search index over every captured session before answering the AI tool's first request. With a few hundred sessions this took over 30 seconds, longer than MCP clients such as Claude Code wait, so the SessionFS MCP server failed to connect, and it got worse as sessions accumulated. The server now answers immediately (well under a second on a 378-session store that previously took 35 seconds) and updates the search index in the background.
- **Search indexing is now incremental and continuous.** Sessions that haven't changed since they were last indexed are skipped, so after the first start, keeping the index current takes a fraction of a second. Changes to sync bookkeeping alone don't count as changes. The server re-checks every minute, so sessions captured while it is running now appear in search within about a minute (previously not until the next restart). `sfs mcp index` still rebuilds the whole index on demand.
- **Deleted sessions no longer appear in MCP search results.** Search entries for sessions that were removed from disk are now cleaned up.
- **One malformed session no longer breaks local search.** A session with an unexpected manifest, workspace or message shape (for example, one pulled from a teammate) is now skipped instead of stopping indexing for every session after it.

- **Autosync now syncs sessions while the daemon is running.** With autosync on, sessions were only queued for upload when the daemon started, so sessions captured during a working day weren't synced until the next restart, and each restart then uploaded the whole backlog at once. Captured sessions are now queued as soon as they are captured (after the usual quiet period while a session is still being written), but only when something that would be uploaded actually changed — re-captures that change nothing, such as Cursor re-reading every chat when its database changes, don't trigger uploads. Turning autosync on now picks up sessions captured while it was off, and turning it off (or switching to selective) now cancels uploads that were already queued.
- **Re-captured sessions keep their sync state.** Every re-capture rewrote the session's manifest without its sync record, so the next upload went out without the etag that lets the server detect a conflicting copy from another device, and the session looked never-synced. The sync record is now carried across re-captures, and the session is marked as changed only if its content changed.

### Upgrading

The first start after upgrading re-indexes every session once, in the background; search results fill in as it completes. With autosync on, the first daemon start after upgrading still uploads any sessions captured since your last restart.

## [0.15.4] - 2026-10-08

**Container and deploy-pipeline security.** Extends the v0.15.3 hardening to the hosted remote MCP server image, and isolates the vulnerability scanner from deploy credentials in both server pipelines. No application, schema or MCP tool changes; migrations remain at 062.

### Changed

- **Remote MCP server deploys are now gated on fixable vulnerabilities.** The MCP server deploy previously ran no image scan at all. It now fails on any critical or high finding with a fix available, before the image is pushed.
- **The scanner can no longer obtain deploy credentials.** In both the API and MCP deploys, the image is now built and scanned in a job that has no cloud credentials and no permission to request them. The scanned image is handed to a separate push job, so what is deployed is exactly what was scanned. Previously the scanner ran in the same job as the cloud login, and the whole workflow could request a deploy token.
- **Third-party scanning actions are pinned to verified commits.** Every use of the Trivy GitHub Actions was tracking a moving reference. They are now pinned to specific commits released after the March 2026 Trivy supply-chain incident. SessionFS pipelines did not run during that incident's exposure window.

### Security

- **Remote MCP server image no longer ships outdated packaging tools.** The image had 2 fixable high findings in the base image's setuptools (its bundled jaraco.context and wheel). pip, setuptools and wheel are now upgraded before installing, and pip is removed from the image afterwards. The image now has zero fixable critical or high findings.

## [0.15.3] - 2026-10-08

**Container security.** Hardens the published API server image and adds a vulnerability gate to the API deploy. No application, schema or MCP changes; migrations remain at 062.

### Changed

- **API deploys are now gated on fixable vulnerabilities.** The image scan previously only produced a report. The deploy now fails on any critical or high finding in the server image that has a fix available. Findings in the base operating-system packages with no published fix remain visible in the scan report. Uploading the report to code scanning is now best-effort and can no longer block a deploy.

### Security

- **Server image no longer ships outdated packaging tools.** pip, setuptools and wheel are upgraded to releases that clear their published advisories, and pip is removed from the runtime image entirely, since the server never uses it and its bundled copies of urllib3 and msgpack lag the fixed releases. The image now has zero fixable critical or high findings. Self-hosted operators building from this Dockerfile get the same result; the CLI and the Python package are unchanged.

## [0.15.2] - 2026-10-06

**CLI fix.** Restores correct exit codes for every `sfs` command when installed alongside typer 0.27 or newer. No server, schema or MCP changes; migrations remain at 062.

### Fixed

- **`sfs` commands no longer report deliberate exits as crashes.** typer 0.27 made `typer.Exit` and `typer.Abort` standalone exception classes that no longer inherit from click's, and the CLI's error handler only recognised click's. Any command that exits with a specific code therefore printed "Unexpected error: N" and exited with 1 instead of N, and a few commands that finish successfully through `typer.Exit(0)` reported failure. Scripts and CI jobs that rely on `sfs` exit codes (for example `sfs agent complete --enforce`) were affected. Declining a confirmation prompt also printed an unexpected-error message instead of a clean cancel. The handler now recognises typer's own classes on every typer release. Affects installations of 0.15.0 and 0.15.1 that resolved typer 0.27.

## [0.15.1] - 2026-10-05

**Security patch.** Fixes an authenticated request-forgery flaw in the MCP server that affects every release through 0.15.0. No schema changes; migrations remain at 062.

### Fixed

- **MCP server: caller-supplied git remotes are validated and percent-encoded before use.** Every project-scoped MCP tool interpolates its `git_remote` argument into the `/api/v1/projects/{...}` path, and the fallback in `normalize_git_remote` returns unrecognized input unchanged. A remote such as `../admin/users` was therefore normalized away by the HTTP client and the request — carrying the user's own API key — was issued against a different endpoint, turning any project-scoped tool into an authenticated proxy. This matters because MCP tools accept arguments chosen by the model, so prompt-injected content could reach API endpoints the MCP surface deliberately does not expose. Remotes are now rejected for traversal or injection (empty, `.` or `..` segments including percent-encoded forms, `?`, `#`, whitespace, control characters, backslashes) and each path segment is percent-encoded. The check is reject-only and never narrows the syntax the server accepts: `+`, `~`, percent-encoded characters, IPv6 hosts, GitLab subgroups, scheme-less `host/owner/repo` and user-less scp (`github.com:owner/repo.git`) remotes all continue to resolve.
- **MCP server: every other caller-supplied identifier in an API path is validated and percent-encoded too.** The same flaw applied to ticket ids, persona names, wiki slugs, handoff ids, work-queue ids, agent-run ids, knowledge-entry ids and session ids (e.g. `get_ticket` with `ticket_id="../../../admin/users"`). All ~40 interpolation sites now go through a shared helper (`sessionfs.mcp.path_safety`) that rejects dot segments (raw or percent-encoded), empty segments, backslashes and control characters and encodes `/`, `?` and `#`. The hosted remote MCP server's `get_project_context` and session lookups get the same treatment, and knowledge-search query strings are now URL-encoded.
- **Remote validation also covers non-HTTP schemes.** `git://`, `git+ssh://` and upper-case scheme remotes (which the server stores verbatim) resolve again, and a dot segment smuggled in via the host of such a URL (`ssh://../admin`) is rejected.
- **Dependencies:** PyJWT ≥ 2.15.1 (JWKS-set parsing, algorithm-confusion and signature-encoding advisories; the OIDC SSO path parses IdP key sets with `PyJWKSet`), urllib3 ≥ 2.8.0 and anyio ≥ 4.14.2 floors; dashboard dev-dependency lockfile refreshed (vitest, undici, js-yaml and others).

### Known limitation

- Repositories whose git remote is a *relative* local path (for example `../sibling/repo`) can no longer be used with project-scoped MCP tools. Such remotes are stored with `.`/`..` segments in the project key, which is exactly the traversal shape this release blocks, so they cannot be requested safely. Absolute local paths, `file://` remotes and every hosted form are unaffected.

## [0.15.0] - 2026-08-12

**The adoption release.** No new platform features by design — this release removes the friction between installing SessionFS and feeling what it does, measures that journey, and protects it. Migrations advance to **062**, all proven on PostgreSQL 16.

### Added

- **A demonstrated magic moment.** `sfs init` now ends by capturing your most recent AI session and telling you what to ask next — *ask your agent: "what did we do last session?"*. Race-free with the daemon by construction (init waits read-only when a daemon owns the store), bounded at every step, and respectful of deletions and the compaction guard.
- **Friction-free installs.** pipx is the documented default; a Homebrew formula and a checksummed curl installer (`install/install.sh`, mirrored at the site) join it. A static-binary feasibility assessment recommends GO for v0.16.
- **Anonymous activation telemetry** (default-on, prominently disclosed, dual opt-out: `SFS_NO_TELEMETRY=1` or `[telemetry] enabled=false`). Six funnel events; the payload is a random install id, version, OS, and the event name — never paths, session content, or personal data. Self-hosted deployments send telemetry to their own server. Full disclosure in `docs/telemetry.md`.
- **Capture-health self-monitoring.** The daemon detects when a tool's format change breaks capture (distinct-session thresholds, never flags an idle tool), persists it, surfaces it in `sfs doctor` (check #9) and a dashboard Settings card.
- **Handoff pre-signup landing.** Recipients see what they were handed — sender, title, and a bounded, redacted preview — before creating an account; signing up becomes the claim action. The preview is precomputed at handoff time, token-gated (hashed at rest, carried in a URL fragment and sent as a header), with constant 404s for every failure mode.
- **Public share pages.** A shared session renders as a read-only page at `/s/{token}` — owner-revocable, password-capable, `no-store`/`noindex`, and hard-gated by a DLP scan of exactly the content served (tool inputs included), re-run whenever the underlying session changes.
- **SSO in the Helm chart** (`api.sso` values block) plus a real-IdP smoke-test runbook; a Windows support scoping assessment (planned for v0.16 — docs now state platform support honestly).

### Changed

- README, site, and quickstart lead with one story: *your AI coding agent never starts from zero again.* Platform breadth moved below the fold.

### Fixed

- **SessionFS's own API keys (`sk_sfs_…`) were excluded from every DLP gate** by a deliberate allowlist entry — a user's real key could pass session-push scans, handoff previews, and share pages unredacted. The exclusion is removed and a dedicated detection pattern added; documentation placeholders remain covered by dummy-value filters.

## [0.14.0] - 2026-07-06

**Resident agents + the SSO dashboard.** Two large feature bodies: a resident reviewer/implementer runner (autonomous, propose-only agents with a durable "mind"), and the dashboard surface for Organization SSO (which shipped backend-only in v0.13.x). Database migrations advance to **058** (057 resident foundation; 058 SSO browser-login). Both proven on PostgreSQL 16.

### Added

- **Resident runner (R0–R5).** An operator-hosted, always-on process that drives a work queue by calling *your own* LLM (no server-side LLM key). Two kinds: a **reviewer** that posts trusted verdicts, and an **implementer** that writes code in an isolated worktree and can only *propose* — it never merges, never self-closes (the server's self-review prohibition closes items only on an independent trusted `VERIFIED-CLEAN`), and never sends code to the server (only branch/SHA/changed-path diff-refs). Includes a first-class server-side resident memory primitive (migration 057: `residents` + `resident_memory_entries`, `resident_memory:read`/`resident_memory:write` scopes), living-context hydrate/writeback/compaction, fail-closed LLM cost bounding (per-wake + daily token ceilings) with a `--cold` mind rebuild, a `sfs resident run`/`sfs resident health` CLI, a hardened sandbox profile (`deploy/resident-sandbox/`), an operator guide, and a mandatory non-rubber-stamp high-risk-merge checklist (the human merge is the load-bearing backstop). Free for the local runner.
- **SSO admin dashboard.** A new SSO section on the org admin page: OIDC provider configuration (client secret handled strictly as a *reference*, never a pasteable secret), DNS-TXT domain verification, an enforcement toggle (owner is never locked out), and break-glass grants (owner-only). Server authz remains authoritative.
- **SSO browser login.** "Sign in with SSO" on the login page (organization slug → identity provider) plus a secure one-time-code exchange so the browser lands back in the dashboard logged in. The OIDC callback (previously CLI-only, returning the key as JSON) now, for a browser flow, mints no key inline — it issues a single-use, 120-second, hashed-at-rest one-time code, redirects to the dashboard, and a new `POST /api/v1/auth/sso/exchange` re-mints the key. The raw key is never stored or placed in a URL. The code is bound to the authenticating browser (an HttpOnly cookie nonce verified at exchange) to defeat login-CSRF / session-fixation. New server setting `SFS_DASHBOARD_URL` (browser-login redirect origin).
- CI gate that runs `alembic upgrade head` against real PostgreSQL 16 (catches the dialect-divergence + object-already-exists migration classes that required the v0.13.1/.2 fast-follows).

### Changed

- The SSO `/callback` success paths were refactored behind a single credential-issuance seam (mint-or-code + one commit); all identity-linking, JIT, seat-cap, and audit logic is unchanged.
- The admin OIDC retention sweeper also reaps expired browser-login exchange codes.

## [0.13.2] - 2026-06-29

**Patch: migration 056 no longer recreates an index that already exists.** Second fast-follow on the SSO migration — the v0.13.1 prod migrate-job got past the (now-fixed) PostgreSQL constraint drop but then failed on `CREATE ... uq_org_members_org_user` with `DuplicateTableError`. The deploy was again correctly gated (prod stayed on 0.12.1, DB cleanly at 054).

### Fixed

- Migration 056 created `uq_org_members_org_user` on `org_members`, but that constraint has existed since **migration 016** — on any real database at migration 054 it is already present, so the `CREATE` raised `DuplicateTableError` on PostgreSQL. (The design's premise that the constraint was missing was incorrect.) 056 no longer creates, dedupes against, or drops `uq_org_members_org_user` — migration 016 owns it, and because it has always enforced one-membership-per-`(org_id, user_id)`, no duplicate rows can exist. The JIT `ON CONFLICT (org_id, user_id) DO NOTHING` continues to target the 016 constraint. Verified end-to-end on PostgreSQL 14 against a prod-accurate schema (org_members carrying the 016 constraint): upgrade 054→055→056 and the downgrade both succeed and leave the 016 constraint intact.

## [0.13.1] - 2026-06-29

**Patch: fix the SSO migration (056) so `alembic upgrade head` succeeds on PostgreSQL.** Same-day fast-follow — v0.13.0's prod migrate-job failed and the deploy was correctly gated (prod stayed on 0.12.1, DB cleanly at migration 054, no partial state).

### Fixed

- Migration 056 dropped `uq_external_identity_issuer_sub` with `op.drop_index`. Migration 055 declares that key as an inline `UniqueConstraint`, which PostgreSQL materializes as a real CONSTRAINT (backed by an index) — `DROP INDEX` raises `DependentObjectsStillExistError`; the constraint must be dropped instead. On SQLite (where the test suite runs) Alembic renders it as a plain unique index, so `DROP INDEX` worked and the failure was invisible to the SQLite-only migration test. The drop (and the downgrade's recreate) are now dialect-aware: `DROP CONSTRAINT` / `CREATE UNIQUE CONSTRAINT` on PostgreSQL, `DROP INDEX` / `CREATE UNIQUE INDEX` on SQLite. Verified end-to-end on PostgreSQL 14 (upgrade → downgrade → re-upgrade) and the SQLite migration test remains green. No schema-shape change versus what 0.13.0 intended.

## [0.13.0] - 2026-06-29

**Organization SSO (OIDC) — enterprise single sign-on with anti-takeover account linking, JIT provisioning, and org-wide enforcement.** New runtime dependency `dnspython` (for DNS-TXT domain verification). DB migrations 055 + 056.

### Added

- **OIDC SSO login (authorization-code + PKCE).** `POST /api/v1/auth/sso/start` + `GET /api/v1/auth/sso/callback`. Login attempts are durable and single-use (atomic rowcount-1 consume — replay/CSRF safe), with state/nonce/PKCE bound to an HttpOnly+Secure+SameSite=Lax cookie. A successful login mints a normal user API key so the CLI and dashboard work unchanged.
- **Anti-takeover account linking.** An SSO identity auto-links to an existing account ONLY when the IdP email is verified, exactly matches an already-verified SessionFS account, and that account is already a member of the IdP's org — every other case is denied. Identity is keyed on `(org_idp_id, subject)`, never the mutable email.
- **JIT provisioning.** First SSO login from a verified org domain creates the user (member role only) and reconciles any pending invite, respecting the org's seat cap.
- **Provider config + domain verification.** Owner/admin endpoints to configure an OIDC provider (the client secret is stored only as a reference — never plaintext) and verify org domains via DNS-TXT, with a consumer-email denylist and one-org-per-domain enforcement.
- **Org SSO enforcement + break-glass.** Owners/admins can require SSO for their org; service keys (CI / automation) are categorically exempt, the owner is never locked out, and an owner can issue a time-boxed (1-hour) break-glass grant for one admin during an IdP outage.
- **Hardened `id_token` validation** (asymmetric-alg allowlist, `alg:none`/`HS*` rejected, issuer matched against the login attempt, `azp` on multi-aud, strict `email_verified`) and an **SSRF-guarded fetch layer** for all issuer-derived requests (HTTPS-only, private/loopback/link-local/metadata IPs rejected per-host).
- **Deprovisioning** now revokes a removed member's SSO-minted keys and deactivates their external identities in the same transaction, and an admin retention sweeper reaps stale SSO login attempts.

### Changed

- `oidc_sso` is available on all paid tiers (free excluded).

## [0.12.1] - 2026-06-23

**Multi-repo project fixes (from a heavy user's report) + the autonomous work-queue reviewer path completed.** Additive only — no schema change (migrations 001–054 unchanged).

### Fixed

- **`sfs project repos` no longer crashes.** The command errored on every project because the CLI mishandled the endpoint's JSON-array response; it now lists a project's linked repos correctly.
- **Projects can be renamed.** New `PATCH /api/v1/projects/{id}` (project-admin only) plus `sfs project set --name <new>` — the project name was previously immutable after creation, so a consolidated/umbrella project could be stuck showing a stale name.
- **`link-repo --primary` keeps the previous primary linked.** Promoting a new primary repo demotes the old one to a regular linked repo (it stays resolvable) instead of dropping it — now enforced by a one-primary constraint at the model level and a regression test.
- **`project merge` repo linking is now surfaced and proven.** Merging projects already moved the source repos into the target; the dry-run plan now reports how many repos will be linked, and it's covered end-to-end.

### Added

- **Work-queue reviewer verdicts via the settle path.** A `review_until_clean` reviewer now delivers its verdict through `complete_work_queue_step` (the server stamps the reviewer identity and trust from the authenticated caller — never from client-supplied input), closing a contract gap found while dogfooding the queues.
- **Trusted-reviewer administration.** Org admins can register / list / revoke trusted reviewers (`/api/v1/orgs/{org_id}/trusted-reviewers` + `sfs admin trusted-reviewers`), so a dedicated automated reviewer key can be authorized to post verdicts the work-queue stop oracle will trust. Tightly gated and fully audited.

## [0.12.0] - 2026-06-22

**Agent work queues — autonomous, supervised ticket-closing loops — plus a hardening of review-verdict trust.** An agent (Claude / Codex / Gemini) can be pointed at a queue of tickets and repeatedly woken (via `/loop`, cron, or CI) to service, review, and close them using SessionFS as the source of truth — no human dispatcher. The server holds all the loop state, so it resumes with zero chat memory. Backend + MCP (6 new tools, 62 → 68). DB migrations 053–054. Reviewed end-to-end: design (Compass) + Atlas/Sentinel design review, then four implementation phases each Codex-reviewed clean, and a full Shield-SR pre-release pass.

### Added

- **Agent work queues (`work_queues` / `work_queue_items` / `work_queue_runs`, migration 054).** Create a queue over a filter or explicit ticket list with a mode — `review_until_clean`, `implement_until_done`, or `triage` — a cadence, and per-run/per-item budgets. The heartbeat tool `run_work_queue_step` returns one bounded directive at a time (a single intent + a small comment delta, not the whole thread); the agent acts and calls `complete_work_queue_step`, which is the only point that durably advances the queue. A crash between the two re-emits the same directive rather than losing or repeating work. Six MCP tools: `create_work_queue`, `get_work_queue`, `list_work_queues`, `set_work_queue_status`, `run_work_queue_step`, `complete_work_queue_step`. Two new capability scopes (`work_queues:read` / `work_queues:write`); acting also requires the downstream `tickets` / `agent_runs` write scopes.
- **`review_until_clean` stop oracle.** A review queue auto-finishes an item only when the server itself re-derives a **trusted, literal `VERIFIED-CLEAN` with no open findings** — it never trusts an agent's self-reported outcome. Available to all tiers.
- **Built-in safety envelope** (load-bearing, since queues are available on every tier): a per-item attempt cap with exponential backoff (2m → 5m → 15m → 60m) that parks an item as `failed` for human reset, a per-wake ticket cap, a cadence floor, and a dedicated rate-limit class on the step endpoint.

### Changed / Security

- **Review verdicts now require server-verified provenance (migration 053).** Previously a review verdict was recognized by the comment's `author_persona` string — which a caller could set freely, allowing a forged `VERIFIED-CLEAN`. Verdict authority is now a server-stamped `verdict_trusted` flag, set from the authenticated identity against a trusted-reviewer registry, never from the request body. Human-facing review display is unchanged; the strict gate applies to the autonomous stop oracle. This closes the spoofing weakness and is the trust foundation the work-queue oracle stands on.

## [0.11.2] - 2026-06-20

**Self-service licensing is now usable in the dashboard.** v0.11.0 shipped the licensing + org-management backend; this release adds the web UI for it, plus hardening of the activation/profile paths. No schema change (migrations 001–052 unchanged).

### Added

- **License activation in the dashboard** — a guided flow for users not yet in an organization: enter a license key → confirm the previewed org/tier → submit → enter the single-use code emailed for verification → land as the new organization's owner. Email verification is required; invalid-key and bad/expired-code states are handled, and the exact-match-email path completes without a code round-trip.
- **Ownership transfer in the dashboard** — an organization owner can transfer ownership to an existing admin; the target sees an Accept/Decline banner and the initiator a pending banner with Cancel, each showing the transfer's expiry. The owner role is shown in the members list and the owner row is protected from demotion/removal. (Distinct from project transfer.)

### Fixed

- **Profile and activation no longer 500 on a transient duplicate organization membership.** `/me` now resolves the caller's membership deterministically (earliest-joined) instead of failing when more than one row is briefly present, and the activation verify step rejects an already-member caller with a clean 409 (rolling back so no partial organization is created) even when duplicate membership rows exist.

### Security

- `msgpack >= 1.2.1` (dev/audit tooling only; GHSA-6v7p-g79w-8964) — defense-in-depth; not a runtime dependency.

## [0.11.1] - 2026-06-20

**Patch — fixes a latent failure in the v0.11.0 entitlements backfill migration and restores a green CI.** No schema change, and no behavior change for already-migrated databases (the v0.11.0 migration had already applied cleanly to environments where the backfill recorded no diagnostics).

### Fixed

- **Migration 050 diagnostic statement** — the entitlements backfill emitted a diagnostic via `op.execute(text, params_dict)`, but alembic's `op.execute` accepts only `execution_options` (not a positional bind-parameter dict), so on any upgrade where the backfill recorded diagnostics (e.g. a tier coercion or an unmatched license) the migration would raise an unbound-parameter error. The discarded `SELECT` produced no durable record anyway; diagnostics are now written to the migration log (captured by the alembic migrate job). This also clears the mypy error that turned the v0.11.0 CI run red. The release process now runs `mypy` locally before every release so type errors are caught before tagging.

## [0.11.0] - 2026-06-20

**Licensing + organization-management redesign — entitlements are now the single source of truth, so enterprise admins can fully manage their organizations.** Plan, seats, and storage for an org are resolved from one authoritative record instead of being scattered across user rows and license tables, which is what previously left enterprise admins unable to see or administer their own orgs. On top of that foundation: self-service license activation with required email verification, two-step organization ownership transfer, an explicit owner role, and last-owner safety guards. Backend + dashboard. Migration 001–**052** (050 additive entitlements foundation; 051 ownership-transfer table; 052 role/status integrity constraints). 2415 backend tests + 388 dashboard tests. Reviewed end-to-end across four phases (Codex + Sentinel design review, Codex code review, Shield-SR pre-release — all clean).

### Added

- **Entitlements model (migration 050)** — a single `entitlements` record per organization is the authoritative source for plan tier, seats, and storage, with a clean lifecycle (`active` / `canceled` / `expired` / `revoked`) tracked separately from billing health (`current` / `past_due`). Existing orgs are backfilled deterministically from their strongest existing signal (license → subscription → manual grant), so no plan changes for anyone. A partial unique index guarantees exactly one active entitlement per owner.
- **Self-service license activation** — an organization admin can activate a license key against their org directly: `GET/POST /api/v1/org/activate` plus an email-verification step (`POST /api/v1/org/activate/verify`). **Email verification is required** — activation completes only after the admin confirms a single-use, time-limited code sent to their address. The activation lookup is non-oracular (it never confirms whether an arbitrary key exists) and the whole flow is rate-limited at the edge.
- **Organization ownership transfer** — a two-step, owner-initiated transfer (`initiate` → target `accept`, with `cancel`) backed by a new `org_owner_transfer` record (migration 051) and an explicit `OWNER` role. Transfers re-validate both parties' standing at accept time and expire if not accepted.
- **Last-owner safety** — an org can never be left without an owner: deactivating or transferring away the last owner is blocked (or auto-promotes the longest-tenured admin), and admins get a force-transfer path with a full audit trail.
- **`/me` enrichment** — the authenticated profile endpoint now returns the caller's effective tier, organization id/name, and org role, so the dashboard can gate org-management surfaces correctly.

### Changed

- **Effective tier is resolved entitlement-first** across the whole server, with a safe fall-back to the legacy columns for any record not yet migrated. All write paths (admin grants, org changes, billing webhooks) now write through the entitlement so the authoritative record and the legacy caches stay in agreement.
- **Database integrity constraints (migration 052)** — organization role and transfer-status values are constrained at the schema level, and the reserved internal `admin` tier can never be assigned as a customer plan.
- **CI:** API deploys are now health-checked against the public endpoint (`https://api.sessionfs.dev`) behind the load balancer rather than the internal Cloud Run URL.
- **Security dependency:** `pydantic-settings >= 2.14.2` (GHSA-4xgf-cpjx-pc3j, defense-in-depth — the affected feature is not used).

## [0.10.32] - 2026-06-18

**Hotfix — Stripe `past_due` no longer locks out paying customers.** The `customer.subscription.updated` webhook handler previously treated transient billing-health statuses (`past_due` / `unpaid` / `paused` / `incomplete_expired`) the same as a cancellation: it downgraded the customer to the free tier, zeroed seats/storage, and cleared `stripe_subscription_id` immediately. Because Stripe sets `past_due` on the *first* failed invoice while smart retries run for days, a paying customer could be cut off on a transient card decline — and clearing the subscription pointer orphaned the account from its Stripe customer, so the eventual recovery (`subscription.updated → active`) could no longer be matched. Backend only; no schema change. Migration 001–049 (unchanged). 2279 backend tests.

### Fixed

- **Stripe `past_due` downgrade regression** — transient billing-health statuses are now non-destructive: the customer keeps their tier, seats, storage, and `stripe_subscription_id` for the duration of Stripe's retry window. Only the terminal `customer.subscription.deleted` event downgrades to free. This restores the recovery path (a successful retry re-establishes the tier) and prevents lockouts on transient declines. Three regression tests cover past-due-keeps-access (user + org) and the past-due → active recovery flow.

## [0.10.31] - 2026-06-17

**Multi-repo projects — a project can now own more than one git repo.** Personas, knowledge, tickets, and rules are shared across all of a product's repos instead of being duplicated across separate single-repo projects. Backend + CLI + dashboard. Migration 001–049 (049 additive). 2278 backend tests (+99) + 388 dashboard tests. Reviewed end-to-end: design Codex-CLEAN + Sentinel-approved; implementation Codex code-review CLEAN + Shield-SR approved. **Free for all tiers.**

### Added

- **`project_repos` model + migration 049** — a project owns N repos (each repo belongs to exactly one project, globally unique normalized remote). Existing projects are backfilled with a single primary repo, so single-repo behavior is unchanged. Repos carry an ownership-verification stamp (`github_app` / `owner_attested` / `legacy_backfill`).
- **Repo linking** — `POST/DELETE/GET /api/v1/projects/{id}/repos` + `sfs project link-repo` / `unlink-repo` / `repos`. Linking requires project-admin standing plus a verification stamp: a GitHub repo is verified against the linker's own App installation; otherwise it's owner-attested. A *verified* claim can reclaim a repo squatted by an *unverified* one; a project whose last repo is reclaimed enters a `repo_reclaimed` state and is revived by linking a new repo. `provider_repo_id` is server-derived, never trusted from the caller.
- **Project merge** — `POST /api/v1/projects/{id}/merge` + `sfs project merge` to consolidate already-split projects into one. Dry-run by default (zero writes); `--confirm` executes as a single atomic transaction with a full audit trail (started → completed/failed) that survives rollback. Persona/rules/wiki collisions resolve by keeping the target and safely renaming/archiving the source; the source becomes a tombstone that transparently redirects.
- **Dashboard** — a Repos tab on each project (link/unlink, primary + verification badges) and a Merge surface (dry-run plan → explicit confirm → audit outcome). Inline project rename; repo-count badge on the projects list.

### Changed

- **Security dependency upgrades:** `starlette >=1.3.1` (CVE-2026-54283/54282/48817/48818), `python-multipart >=0.0.31` (CVE-2026-53538/53539/53540), `cryptography >=48.0.1` (GHSA-537c-gmf6-5ccf). Site build chain: vite/astro/js-yaml advisories cleared.

## [0.10.30] - 2026-06-14

**Dashboard visual redesign — top-tier, dark-first "mission control for AI sessions."** A multi-phase craft pass over the entire web dashboard, reviewed across multiple consolidated Codex rounds to VERIFIED-CLEAN. Frontend + product-site only: no backend changes, no DB migrations (still 001–048), no MCP tool changes (still 62), no auth changes. Backend remains 2179 tests; dashboard 369 UI tests (was 198).

### Added

- **Design-token foundation** — dark-first OKLCH color ladder, a canonical named type scale (`text-2xs` → `text-lg`), and a radius scale, all in Tailwind v4 `@theme`. Semantic utilities (`bg-surface`, `text-text-primary`, `border-border`, `--bg-sunken` wells, hairline borders) replace ad-hoc inline styles and arbitrary `[var(--…)]` classes across the app.
- **`ui/` primitive library** — Button, Card, Input/Textarea/Select, Dialog, Tabs, Table, Dropdown, Tooltip, Kbd. The Select/Dropdown is a custom anchored, keyboard-accessible (`aria-activedescendant`) listbox that fixes the detached far-right native-popup bug.
- **Grouped left sidebar** replacing the old top nav (shared config with the mobile drawer), with hero activation cards for empty states and a compact resume bar.
- **List/grid view toggle** on both Sessions and Projects (persisted per-view to `localStorage`).
- **Captured-session conversation redesign** — tool-aware rendering replaces raw `JSON.stringify` dumps: Bash commands in terminal-style wells, Edit/MultiEdit as real added/removed diffs, file-path/pattern chips, `tool_result` in sunken wells with error detection + line-clamp + copy; a real transcript with speaker hierarchy (role accent rails + uppercase role chips), assistant markdown on our type scale with copy-able fenced-code wells, and de-emphasized collapsible thinking blocks.
- **Account/avatar menu** with Settings · Help · Logout (primary navigation stays in the sidebar, not duplicated).

### Changed

- **Brand tagline → "Memory Layer For AI Agents"** across the dashboard (header, footer, wordmark) and the product site (meta/OG, layout, landing).
- **Security hardening:** `react-router-dom` / `@tanstack/react-query` / `react-markdown` moved from `devDependencies` to `dependencies` so the release `npm audit --omit=dev` gate covers them (react-router resolves to 7.17.0, clearing 2 HIGH advisories); a `site/` esbuild override (`>=0.28.1`) clears 6 HIGH transitive build-time advisories.

### Fixed

- **SessionDetail white-screen crash** — a `useMemo` and derived values were computed after the loading/error early-return guards, so the loaded render invoked more hooks than the loading render ("Rendered more hooks than during the previous render"). Hoisted them above the guards; added a loading→loaded transition regression test that fails on any hook-after-early-return.
- **Avatar menu missing Settings** — the earlier account-menu slim removed all items including Settings; restored Settings + Help (the conventional account-menu items) while keeping primary nav sidebar-only.
- Header overflow that clipped the account menu; dead handoffs docs links; dark-theme text contrast on raised surfaces.

## [0.10.29] - 2026-06-11

**Named auth profiles (multi-account on one device) + cloud dashboard 428 fix.** Two workstreams, both Codex-reviewed to CLEAN (profiles R1 1 HIGH + 2 MED + 1 LOW → R2 1 MED → R3 VERIFIED-CLEAN; CORS R1 CLEAN). No DB migrations (still 001–048). MCP tool count unchanged (62).

### Added

**Named auth profiles** (`tk_457d060822bc48c0`). Two SessionFS accounts could not cleanly coexist on one device: identity resolution was split across `_get_api_config()` (env var > config.toml — used by ticket/persona/agent/rules/keys/admin commands) and `_load_sync_config()` (config.toml ONLY — used by push/pull/sync/delete/project and the daemon), so `SESSIONFS_API_KEY` flipped half the CLI to one account while sync kept acting as the other. This release makes ONE switch change every command consistently:

- **New shared resolver `sessionfs/profiles.py:resolve_auth()`** — the single source of truth for api_url/api_key/store_dir. Both legacy helpers and the daemon now resolve through it.
- **Precedence chain:** `SESSIONFS_API_KEY` (explicit, wins) > `SESSIONFS_PROFILE` (per-shell profile override) > persisted active profile (`~/.sessionfs/active_profile`) > `default` (existing `config.toml` — zero migration for single-account installs).
- **Named profiles** at `~/.sessionfs/profiles/<name>.toml` with isolated per-profile `store_dir` (`~/.sessionfs/profiles/<name>/store`) so session captures, the SQLite index, and the `deleted.json` exclusion list never bleed across accounts. Profile names are validated (no path traversal); profile files and the active_profile pointer are written `chmod 600`.
- **CLI verbs:** `sfs auth login --profile <name>`, `sfs auth use <name>`, `sfs auth profiles`, `sfs auth whoami` (resolves email via one `/auth/me` call).
- **Daemon pins its startup profile** — `_reload_config` refuses a mid-run identity switch; restart the daemon to change accounts. The watcher's capture guard checks the active profile's `deleted.json` (via the new `LocalStore.store_dir` property), so a deletion under one profile is not resurrected from the native source.

### Fixed

**Dashboard 428 "If-Match header required" on Settings → Context/Knowledge Injection — cloud only** (`tk_1a73a7bf359f41c3`). The dashboard stores the ETag from `GET /rules` and replays it as `If-Match` on the next PUT (optimistic concurrency). Per the Fetch spec, a cross-origin response header is hidden from JS unless listed in `Access-Control-Expose-Headers` — and the CORS middleware never exposed `ETag`. On cloud (app.sessionfs.dev → api.sessionfs.dev, cross-origin) the browser hid the header, the dashboard read `etag=''`, sent `If-Match: ''`, and the rules route returned 428. Helm deployments serve dashboard + API same-origin, so the bug was invisible there. Fix: `expose_headers=["ETag"]` on the CORS middleware — completing the round-trip that v0.10.24 started when it added `If-Match` to `allow_headers` (send side). Two new drift-guard tests pin the exposure (live cross-origin response header + source literal). No client change needed; the fix lands with the API deploy.

## [0.10.28] - 2026-06-03

**Coordination + recovery surface: ticket mutation, org invite resend, admin key recovery, compile latency cut.** Bundles four R2-CLEAN Codex-reviewed tickets. Migration 048 adds the `ticket_edits` audit table (strictly additive, cross-DB safe). MCP tool count 61 → 62.

### Added

**`update_ticket` MCP/API/CLI verb** (`tk_835a876529de4551`). Until now, every ticket correction had to land in the comment stream — descriptions drifted permanently out of sync with reality and `start_ticket` loaded stale framing into the compiled persona context. This release adds the missing mutation verb across all three surfaces:
- **PUT `/api/v1/projects/{pid}/tickets/{tid}`** — partial update for mutable fields (`title`, `description`, `priority`, `acceptance_criteria`, `context_refs`, `file_refs`, `depends_on`). Status transitions remain FSM-only. Authz: ticket creator OR project admin (persona name does NOT grant edit rights). Optional `lease_epoch` for optimistic concurrency (409 + structured envelope on stale; last-write-wins when omitted; lease_epoch alone is rejected with 400 — it's a fence, not a mutation).
- **Auto-posted system diff comment** — every successful update writes ONE `TicketComment` with `author_persona='system'` summarizing the per-field diff. No-op writes do NOT post the comment (empty audit pollution is the anti-pattern).
- **Per-field audit rows** in the new `ticket_edits` table (migration 048): id, ticket_id, edited_by_user_id, edited_by_persona, field_name, JSON-encoded old/new values, edited_at, lease_epoch at edit time. Indexed on `(ticket_id, edited_at)` for history queries. `edited_by_user_id` is plain String (not FK) — audit rows survive user-row deletes per `AdminAction` / `KnowledgeEntry` convention.
- **`depends_on` updates** run `_validate_dependencies_same_project` + `_check_dependency_cycle` + dedup BEFORE the wipe-and-reinsert (no cross-project leaks, no cycles, no `IntegrityError` envelopes for duplicates — full-list replacement semantics).
- **MCP `update_ticket` tool** — schema explicitly excludes `assigned_to` and `related_sessions` (those belong to `assign_persona` and the internal write-path respectively). Description names the auto-comment side effect so agents don't double-post.
- **CLI `sfs ticket update <id>`** — flags `--title`, `--description`, `--description-file` (file-based for long markdown without shell-quoting hazards), `--priority`, `--acceptance-criteria-file`, `--lease-epoch` (opportunistically pulled from active-ticket bundle).

**Admin `POST /api/v1/admin/users/{user_id}/api-keys`** (`tk_4afbae8ed3a442e9`). SessionFS has no magic-link/OAuth/password-reset flow, so a user who loses their key has no self-service recovery — the chicken-and-egg blocks `POST /auth/me/api-keys`. This admin-gated endpoint mints a user-kind `ApiKey` on behalf of any active user; raw key returned exactly once; audit row via `_log_action(action="mint_api_key_on_behalf", details={key_id, name})`. `key_kind="user"`, `scopes='["*"]'`, `key_prefix=raw_key[:12]`, `created_by_user_id=admin.id` all set explicitly at the call site (Codex R3 MEDIUM 2 precedent — refactor-safe). Guards: 404 unknown user, 403 inactive user (no backdoor for disabled accounts), 403 non-admin caller. Service keys remain out of scope — they live at `/api/v1/orgs/{org_id}/service-keys` per v0.10.10.

### Fixed

**Org invite UPSERT over stale rows** (`tk_d88678e6fe384de6`). Migration 016 enforces `uq_org_invites_org_email` on `(org_id, email)`. v0.10.22 added a route-level predicate filtering accepted/declined/expired rows so the admin surface "looked like" a resend was allowed, but the DB constraint blocked the INSERT with `IntegrityError` — re-rendered as a generic `409 duplicate_resource` with no actionable detail. After expiry/decline, the CEO had no API path to re-invite (production incident 2026-05-31 with `richardmcmullen1992@gmail.com` and `sammykezz@gmail.com`).

Fix: both `invite_member` endpoints now lookup ANY existing row for `(org_id, email)` with `.with_for_update()` (PG row-lock serializes concurrent re-invites; SQLite single-writer is harmless), classify by state, and route accordingly:
- No row → INSERT fresh.
- Active row → 409 "active invite exists".
- Already a member via live `OrgMember` → 409 "already a member".
- Stale (declined / expired / orphan-accepted) → UPDATE in place: regenerate `id` (invalidates stale email-acceptance links), refresh `expires_at`, clear `accepted_at` / `declined_at` / `decline_reason` / `last_emailed_at`, update `role` + `invited_by`. **Preserves `created_at` as the audit signal of when the email was first onboarded.**

**`/compile` single-pass `auto_generate_concepts`** (`tk_5c124c496ea14ecc`). The route invoked `auto_generate_concepts()` twice on real-compile requests — once before the noop classification (intended for both branches) and again after the real-compile path (flagged `(Legacy) ... for compatibility` in the prior inline comment). Doubled LLM call latency and increased timeout/OOM risk on large projects. Removed the second call; the first now covers both branches; `concept_pages` counted once and reused as `concept_pages_updated` in both response paths.

### Security

- **`cryptography` ≥46.0.7** already pinned (since v0.10.12) — past all GH #16 reported CVE fix-versions (44.0.1 / 46.0.5 / 46.0.6). Issue closed as resolved.
- **`PyJWT >=2.13.0,<3.0`** bumped this release (was `>=2.8`) — Shield-SR pre-release scan flagged PYSEC-2026-175/177/178/179 against 2.12.x. Test suite re-run after the upgrade: no regressions.

### Migration

- **048** — `ticket_edits` audit table. Strictly additive: `CREATE TABLE` + one index on `(ticket_id, edited_at)`. Downgrade drops in reverse order. JSON-encoded `old_value` + `new_value` as `Text` (not native JSONB) for SQLite Helm compatibility; PG can `json_extract` via cast if cross-DB indexing is ever needed.

### Documentation

- **CLI reference** picks up `sfs ticket update` flags and the FSM-only-vs-mutation boundary callout (use `assign_persona` for reassignment, escalate_ticket for one-step priority bumps).
- **MCP tool description** for `update_ticket` explicitly names the auto-posted system comment side effect so agents don't double-post a manual comment.

### Verification

- pytest tests/ -x -q → **2153 passed (+34 from v0.10.27 baseline 2119), 2 xfailed** (pre-existing PG-only migration smoke tests tracked `tk_7dc9e8764a5a4297`)
- ruff check src/ → clean
- helm lint charts/sessionfs → clean
- Shield-SR independent pre-release security review → **CLEAN** (0 CRITICAL / 0 HIGH / 0 MEDIUM new findings; 4 PyJWT CVEs fixed by version bump)
- Codex review threads: tk_d88678e6fe384de6 R1 (1 MED + 1 LOW) → R2 CLEAN; tk_4afbae8ed3a442e9 R1 (1 MED + 1 LOW) → R2 CLEAN → R3 docstring CLEAN; tk_5c124c496ea14ecc R1 CLEAN (non-blocking test tightening) → R2 CLEAN; tk_835a876529de4551 R1 (2 MED + 1 LOW) → R2 CLEAN.

### Issues closed
- GH #50 — MCP `update_persona` / `delete_persona` parity (shipped v0.10.24, re-confirmed in this cycle's audit)
- GH #51 — `sfs org create` 500 + IntegrityError envelope (shipped v0.10.24)
- GH #16 — Dependency CVEs in cryptography 43.0.3 (pinned `>=46.0.7` since v0.10.12)

## [0.10.27] - 2026-05-30

**Daemon liveness hotfix + tier-aware per-file cap discovery.** Bundles three R2-CLEAN Codex-reviewed tickets under one PATCH release. All changes additive — no schema migrations, no breaking changes, no auth changes. Older CLIs talking to a v0.10.27 server ignore the new field harmlessly; v0.10.27 CLIs talking to an older server fall through to the 50 MB literal with a logged warning.

### Fixed

**Daemon: skip excluded sessions in `_sync_sessions()` loop** (`tk_4abfa69b38d54bc0`, parent Issue `tk_714456298d424202`). Without this guard, `_sync_sessions()` retried sessions already in the local exclusion list (`deleted.json`) every cycle. A 75 MB excluded session on a paying customer's daemon caused 862+ retries / 4 days, starving the async event loop on decompression + DLP scanning, timing out `/health`, and tripping 13 liveness probe kills. Fix: at the top of the per-session loop body, `is_excluded(session_id)` short-circuits with `continue` + `_pending_sessions.discard(session_id)` so the next cycle does not re-queue. Debug-level skip log mirrors the watcher convention. Bug B (persistent `sync_failures` counter across daemon restart) deferred per Compass — the one-line guard is the minimum viable hotfix.

**`sfs sync` upload path now runs the resolver-aware oversized-member preflight** (`tk_d5945c4bce3245ce` R1 MEDIUM). `sync_all._push_one` was the only `push_session()` caller still skipping the preflight; `sfs push` and `sfs handoff` already had it. After `pack_session()` and before `acquire_for_retry()` / `push_session()`, `_find_oversized_member(archive_data, max_size=max_member_size)` runs and over-cap archives are marked as push errors with the same friendly skip message shape — no wasted upload of a guaranteed-413 archive.

### Added

**Server: `max_member_bytes` in `GET/PUT /api/v1/sync/settings` response** (`tk_bb56f6a56da34b05`, parent Issue `tk_714456298d424202`). `SyncSettingsResponse` gains a tier-aware `max_member_bytes: int` field sourced from `_member_size_limit_for_tier(await get_effective_tier(user, db))`, so the value tracks the same server-side enforcement as `_check_member_sizes` and follows any `SFS_MAX_SYNC_MEMBER_BYTES_{FREE,PAID}` operator overrides automatically. Purely additive response shape; no migration. `get_sync_settings` gains `db: AsyncSession = Depends(get_db)` so it can resolve the effective tier.

**CLI: tier-aware per-file cap discovery via `_resolve_max_member_size()`** (`tk_d5945c4bce3245ce`, depends on `tk_bb56f6a56da34b05`). New resolver in `src/sessionfs/cli/cmd_cloud.py` with explicit precedence:
1. `SFS_MAX_SYNC_MEMBER_BYTES_PAID` env-var override — always wins; lets customers experiment ahead of server upgrades.
2. Server-supplied `max_member_bytes` from `GET /sync/settings` — tier-aware value the server enforces.
3. 50 MB literal final fallback — logged warning so operators see the misconfiguration; only used when (1) is unset AND server discovery fails.

Cached per CLI invocation via module-level `_max_member_size_cache` — one `/sync/settings` call per `sfs push` / `sfs pull` / `sfs sync` / `sfs handoff` / `sfs pull-handoff` run. Older servers without the field fall through cleanly without crashing. `_find_oversized_member(archive_data, max_size=None)` takes an optional override; entry points (push, pull, sync_all, handoff, pull_handoff) resolve the cap once at command entry and thread it into oversize checks + `unpack_session(member_limit_bytes=...)`.

### Documentation

- `docs/cli-reference.md` adds "Per-file size cap discovery (v0.10.27+)" section covering the precedence chain across all five sync commands.
- `site/src/content/docs/api.mdx` GET + PUT `/sync/settings` rows describe the new field + older-client compatibility.

### Verification

- pytest tests/ -x -q → 2119 passed (+11 from v0.10.26 baseline 2108), 2 xfailed (pre-existing PG-only migration smoke tests tracked `tk_7dc9e8764a5a4297`)
- ruff check src/ → clean
- helm lint charts/sessionfs → clean
- Shield-SR independent pre-release security review → CLEAN (0 CRITICAL / 0 HIGH / 0 MEDIUM)
- Codex review threads: tk_4abfa69b38d54bc0 R1 (1 LOW ruff) → R2 CLEAN; tk_bb56f6a56da34b05 R1 CLEAN; tk_d5945c4bce3245ce R1 (1 MED missed sync preflight) → R2 CLEAN

## [0.10.26] - 2026-05-30

**Hotfix release — fix typer 0.26 BadParameter class hierarchy regression so Deploy API ships.** v0.10.25's `click<8.4` pin installed click 8.3.3 in CI but tests still failed: typer 0.26.3 vendors its own click module (`typer._click`), so `typer.BadParameter` is `typer._click.exceptions.BadParameter`, NOT `click.exceptions.ClickException`. `handle_errors` decorator's `except click.exceptions.ClickException` no longer catches typer-raised parameter errors → they fall through to the generic `except Exception` → render as "Unexpected error: ..." instead of the standard typer parser-error format. Same root cause as the Codex round-2 fix on tk_e025375272b84a95 / v0.10.11, surfaced again by typer's 0.26 refactor.

### Fixed

**`handle_errors` catches both `click.exceptions.ClickException` AND `typer._click.exceptions.ClickException`** (`src/sessionfs/cli/common.py`). Builds a tuple of base classes at import time — best-effort tries `from typer._click import exceptions` and adds typer's `ClickException`/`Exit`/`Abort` classes to the tuples used in `except` clauses. Falls back to standard click on typer <0.26 (or future renames). Inline comment cites the typer 0.26 class-hierarchy split + v0.10.11 Codex precedent.

Click pin removed (`click>=8.0,<8.4` from v0.10.25 is gone; click resolves freely again).

### Verification

- pytest tests/ -x -q with CI's typer 0.26.3 + click 8.3.3 installed locally → 2108 passed (same as v0.10.24), 2 xfailed, 0 regressions
- `tests/unit/test_autosync.py::TestHandoffIdMisuseRedirect::test_typer_invocation_with_session_id_still_requires_to` → PASS under both typer 0.24 (local default) and 0.26 (CI)
- `tests/unit/test_cmd_agent.py::test_agent_complete_findings_rejects_non_object_elements` → PASS under both versions
- ruff check src/ → clean
- helm lint → clean

## [0.10.25] - 2026-05-30

**Hotfix release — pin click<8.4 so Deploy API succeeds.** v0.10.24 tagged and Release/MCP/Container-Images pipelines all succeeded, but CI + Deploy API failed on 2 CLI tests that assert click `BadParameter`-derived exit code and output format. Tests pass on local `.venv` (click 8.3.1) but CI installed click 8.4.1 which changed both the exit-code emitted by `ClickException` and the class hierarchy that `handle_errors` decorator passes through. Result: `sfs org create` and other CLI flows reach the generic `except Exception` "Unexpected error" branch instead of click's standard parser-error format. Pinning `click>=8.0,<8.4` restores the v0.10.24 behavior; follow-up ticket will fix the brittle assertions and lift the pin.

### Fixed

**Pin `click<8.4` in `pyproject.toml`** so CI installs the version `handle_errors` was tested against. Specifically affects `tests/unit/test_autosync.py::TestHandoffIdMisuseRedirect::test_typer_invocation_with_session_id_still_requires_to` (asserts absence of "unexpected error" in CLI output) and `tests/unit/test_cmd_agent.py::test_agent_complete_findings_rejects_non_object_elements` (asserts `exit_code == 2`). Both pass under click 8.3.x; click 8.4.x bumps both expectations. Without this pin, `Deploy API` workflow's pre-deploy test gate fails and the API stays on v0.10.23.

### Verification

- pytest tests/ -x -q → 2108 passed, 2 xfailed (unchanged from v0.10.24)
- ruff check src/ → clean
- helm lint → clean

### Out of scope

- Lift the click pin + make the 2 brittle CLI tests resilient to click `BadParameter` exit-code / output-format drift (follow-up ticket).

## [0.10.24] - 2026-05-30

**Bundle release — Issue/Task rollup + 2 enterprise unblocks (GH #50 + GH #51) + dashboard surface.** Migration 047 adds `kind` + `parent_ticket_id` to tickets so a single user-reported problem can roll up multiple executor workstreams. Two enterprise customer bugs filed by `najitestech` close: `sfs org create` 500 (latent FK ordering since v0.10.0, blocked every manual-license enterprise) and missing MCP `update_persona` / `delete_persona` parity. Plus structured `IntegrityError` envelopes across CLI + dashboard so future opaque 500s become actionable, Rules-page max-tokens UX, session rename, CORS If-Match preflight unblock, and the Prism-side dashboard surface for the rollup. 2108 backend tests + 198 dashboard tests passing (+85 backend / +11 dashboard from v0.10.23 baseline). 1 strictly additive migration.

### Added

**Issue/Task rollup** (`tk_dbccde26ed604b3c` + `tk_5d14f10e489d4361` Prism, commits `0cae114` + `09d0735` + `98d3154`). Compass-scoped Option A from `tk_23f523c1bdd94fc5`: extend `tickets` with `kind` String(10) NOT NULL default 'task' + `parent_ticket_id` String(64) FK ON DELETE SET NULL + composite index on `(project_id, parent_ticket_id)`. NOT a reuse of `TicketDependency` (DAG ordering ≠ container relationship). Forked FSM: Tasks keep the executor lifecycle (suggested → open → in_progress → blocked → review → done); Issues use a simpler manager FSM (open → in_progress → closed + cancelled escape). Issue status is **manually closed by Compass** — NOT auto-derived from child Task status, per CEO direction.

- New `POST /tickets/{tid}/close` Issue terminator. Rejects Tasks with 400 + clear error.
- `POST /tickets` accepts `kind` + `parent_ticket_id` with full validation: parent must exist + be same-project + be `kind='issue'`; no Issue-under-Issue (single-level only in v1); cross-project parent → 422.
- `GET /tickets` accepts `kind` query filter (`issue` | `task` | omitted); detail returns `child_ticket_ids: list[str]` for Issues (single SELECT, indexed, project-scoped).
- **Actor-based authorization** via new `user_is_project_admin(db, user_id, project)` helper in `src/sessionfs/server/auth/project_access.py`. Returns True for project owner OR (org-scoped) `OrgMember.role='admin'`. Drops the `assigned_to=='compass'` user-input-trusting branch entirely (Codex R1 MEDIUM #1 — anyone could pass it). Issue creation AND `/close` both gated on this trusted-actor signal.
- Compiled persona context now emits `Kind: {kind}`, `Parent Issue: {id} — {title} (status: {status})` when set, and `## Child Tasks (N)` section listing every child with id + status + title (Codex R1 MEDIUM #3 — agents reading the markdown can now see the rollup).
- MCP `create_ticket` + `list_tickets` tools gain `kind` + `parent_ticket_id` params with docstrings explaining the distinction. `get_ticket` + `start_ticket` compiled_context surface the rollup automatically.
- CLI: `sfs ticket create --kind issue|task --parent <tk_>`; `sfs ticket list --kind issue|task`; `sfs ticket show` displays kind + parent Issue + child task list; `sfs ticket close <tk_>` Issue terminator. `sfs ticket watch --until-closed` terminal set extended with `closed`.

**Dashboard Issue/Task surface** (Prism, `98d3154`). Single-surface design — no separate IssuesTab. `TicketsTab.tsx` extended with: `KIND_FILTERS` (All / Issues / Tasks) alongside existing status filter; indigo left-edge bar + `KindBadge` on each row for at-a-glance distinction; Issue expand shows a Children section listing child Tasks (id/title/status/assigned_to) as clickable buttons that re-target `selected` in place; Task expand with non-null `parent_ticket_id` shows a Back-to-parent Issue breadcrumb; New Ticket modal gains a kind selector + conditional Parent Issue autocomplete (Task only); Close Issue button on `in_progress` Issues calling the new `closeTicket()` route; STATUS_FILTERS gains `closed` row with emerald STATUS_TONE. `useTickets` hook gains `kind` filter; new `useTicketChildren` parallel-fetches children via `useQueries` with shared `['ticket', projectId, id]` cache keys so direct expand reuses the entry; new `useCloseTicket` mutation. KB entry **#611** documents the UI pattern.

**Session rename** (`tk_cf9f1691091d4e8e`, commits `2ef0335` + `7bb93cd`). Click-to-edit title + alias inline on session detail view, side-by-side per CEO. Title + alias persist via the existing PATCH /sessions/{id} endpoint (hardened: empty-title 422 + `aliases_cloud` tier gate + all-None body 400). New MCP tool `rename_session(session_id, new_title?, new_alias?)` — empty `new_alias=""` routes through DELETE /alias and captures canonical id from the DELETE response so a caller passing the about-to-be-cleared alias as the identifier doesn't 404 on the follow-up (Codex R1 MEDIUM). Three-layer plumbing: server (route hardening), MCP (new tool), dashboard (click-to-edit affordance with side-by-side form + `MoreMenu` "Rename (Title & Alias)" entry, replacing the old alias-only edit).

**Generic `IntegrityError` envelope + structured 5xx hardening** (`tk_e7da4c4508d94bac` / GH #51 ask #2, commits `8a2cb85` + `3576245`). Closes the customer-facing UX failure mode shared by every multi-INSERT bug: bare "Internal Server Error" with no actionable body. New FastAPI exception handler for `sqlalchemy.exc.IntegrityError` classifies by PostgreSQL SQLSTATE (`pgcode`) first, falls back to SQLite message string-matching. Unique violation → 409 `duplicate_resource`; FK violation → 500 `foreign_key_violation` (server-bug class, structured body so triage doesn't lose three releases like 2026-05-20); NOT NULL → 422 `missing_required_field`; CHECK → 422 `check_constraint_violation`; catch-all → 500 `integrity_error`. Handler logs raw DBAPI text at ERROR with request method + path; client envelope intentionally strips column names and row values to avoid PII leak. New CLI helper `format_api_error(body, status)` parses v0.10.x envelope first, falls back to legacy `detail` string + dict shapes, then `str(body)` — wired into `handle_api_response` (the `sfs org create` triggering path), `_api_request` helpers in `cmd_project.py` + `cmd_rules.py`, and all 12 inline "API error" prints in `cmd_ticket.py`. Dashboard `ApiError` class now extracts `code` + `details` + clean `message` from the envelope at construction time, with `raw` preserved for diagnostics; new `parseApiErrorBody` helper exports the same parse for non-throw paths.

**MCP `update_persona` + `delete_persona`** (`tk_32abb6d0d4744c5d` / GH #50, commits `f55fc85` + `030230b` + `fde0f4e`). Closes the 6-of-8 persona-tool gap najitestech surfaced. MCP tool count 59 → 61. `update_persona` wraps PUT /personas/{name} — body sends ONLY fields the caller passed (omitted fields stay unchanged; no silent overwrite); rejects locally if no mutable field provided. `delete_persona` wraps DELETE /personas/{name}?force=... — soft-delete with strict force-parsing (literal `True` OR case-insensitive `{"true", "1", "yes"}` only; `bool("false") == True` Python footgun closed per Codex R1 MEDIUM). 409 surfaces the structured envelope so callers can route on `error.code` + `error.details.open_ticket_count` to decide whether to retry with force=true. 404 surfaces the persona name in a clean error. Tool descriptions reference the v0.10.23 case-insensitive-unique contract and the honest reactivation guidance (R2 fix): "Reactivation is NOT exposed via MCP today — operator must use the HTTP API directly with `is_active=true`."

**Rules page max-tokens UX** (`tk_d4a13a68b6724ba6`, commits `c4c8d02` + `0d9ccf9`). Companion to the v0.10.23 CORS If-Match unblock on the same surface. Dashboard `DebouncedTokenInput` gains `max={20000}` (browser arrows respect the cap) + `n > 20000` debounce gate (defends against paste-bypass since `<input type="number" max=>` accepts pasted out-of-range by spec). Server's both `knowledge_max_tokens` + `context_max_tokens` over-cap branches now raise structured envelope `code: 'max_tokens_exceeded'` with `field`, `min`, `max`, `current` — replaces bare "knowledge_max_tokens out of range".

### Fixed

**`sfs org create` 500 for manual-license enterprise customers** (`tk_17b39010f9a64cba` / GH #51, commit `31cec52`). One-line `await db.flush()` between `db.add(org)` and `db.add(member)` in `routes/org.py:139-160`. Latent since v0.10.0 because SQLAlchemy's unit-of-work doesn't reliably topologically sort two pending INSERTs by FK dependency without an intervening flush. For Stripe-paying users the implicit autoflush triggered by `update(User)` masked the bug; users with no Stripe fields (every manual-license enterprise customer, every user upgraded via admin action without going through Stripe checkout) hit the FK violation → 500. Inline comment cites GH #51 + the autoflush mechanics so a future reader doesn't strip the flush as "unnecessary". New regression `test_create_org_enterprise_user_no_stripe` uses `PRAGMA foreign_keys=ON` on the test session so SQLite enforces FKs like PostgreSQL (otherwise the bug doesn't reproduce in tests).

**CORS preflight `If-Match` allowed** (`febd732`, no parent ticket — direct enterprise repro). Dashboard's `PUT /rules` sends `If-Match: <etag>` for ETag-based optimistic concurrency. Browser preflight requests `Access-Control-Request-Headers: ...if-match` but `CORSMiddleware.allow_headers` was `["Content-Type", "Authorization"]` — `If-Match` not listed → preflight 400 → PUT never fires → dashboard toggle reverts with no error surface. Live-verified via curl on `api.sessionfs.dev`. Added `If-Match` + `If-None-Match` to allow_headers + drift-guard regression test pinning the audited list. Affects every ETag-protected write surfaced via the dashboard.

### Verification

- pytest tests/ -x -q → **2108 passed**, 2 xfailed (pre-existing migration-003 chain)
- ruff check src/ → clean
- helm lint charts/sessionfs → clean
- dashboard npm test -- --run → **198 passed** (was 186)
- dashboard npm run build → tsc + vite clean
- pip-audit → 0 vulnerabilities
- npm audit (dashboard + site) → 0 vulnerabilities
- bandit → 0 HIGH; 17 MEDIUM pre-existing baseline (none in v0.10.24 files)
- Shield-SR independent pre-release review → APPROVED 0 CRITICAL / 0 HIGH / 0 NEW MEDIUM

### Codex review cycles

All 8 Atlas+Prism tickets implemented directly by Claude per CEO's standing rule (no Codex CLI spawn). Polling Codex (`author_persona="codex"`) reviewed:
- tk_cf9f1691091d4e8e: R1 1 MED → R2 CLEAN
- tk_dbccde26ed604b3c: R1 3 MED + 2 LOW → R2 CLEAN
- tk_17b39010f9a64cba: R1 CLEAN first round
- tk_e7da4c4508d94bac: R1 2 MED + 1 LOW → R2 CLEAN
- tk_d4a13a68b6724ba6: R1 1 LOW → R2 CLEAN
- tk_32abb6d0d4744c5d: R1 1 MED + 2 LOW → R2 1 LOW → R3 CLEAN
- tk_5d14f10e489d4361 (Prism): not Codex-reviewed (Compass-scoped, CEO-shipped)

## [0.10.23] - 2026-05-26

**Bundle release — Scout state-cache fix + ChatGPT MCP hotfix + persona case-sensitivity + release-skill hardening.** Five commits across two operational fixes (entity_ref upsert closing Scout's silent cache loss, MCP `_ConsumedResponse` sentinel restoring tool discovery on ChatGPT/Claude.ai) and three release-hygiene improvements (persona case-insensitive lookup, Scribe-Site gate, merge-conflict docs). No migrations. No new endpoints. 2023 backend tests + 187 dashboard tests passing (+16 backend from v0.10.22 baseline).

### Added

**Opt-in entity_ref upsert on `/entries/add`** (`tk_49db8d2b6c424d35`, commits `7a98f27` + `393b25f` + `a78728d`). New optional `upsert: bool = False` field on `AddEntryRequest`. When `True` AND `entity_ref` is provided, the route looks up the prior active entry by `(project_id, entity_ref, dismissed_at IS NULL, superseded_by_id IS NULL)` and supersedes-in-place rather than running the similarity-based dedup that returns 409 on cache rotations. Closes Scout's state-cache silent loss path (3 duplicate tickets filed in 18 hours on a 6%-delta cache body). Existing callers unaffected — feature is fully opt-in. Cloud agent dispatchers (`bedrock-action-group.yaml`, `vertex_tools.py`) updated to expose `upsert` in their tool schemas. Scout n8n docs (`scout-n8n.md`) updated with the dismiss-then-create workaround pattern for clients pinned to v0.10.22 and the upsert recipe for v0.10.23+. Codex R1 MEDIUM (similarity gate must check intent before bypass) + R1 LOW (test coverage) → R2 VERIFIED-CLEAN.

**ChatGPT / Claude.ai MCP tool-discovery hotfix** (commit `f7fdcc1`). `_ConsumedResponse(Response)` sentinel class in `mcp/remote_server.py` so `handle_mcp` returns a no-op ASGI callable after `transport.handle_request()` consumes the underlying response. Starlette 1.x `Route` wrapper rejects `None` return with `TypeError: 'NoneType' object is not callable`, which truncated the wire response and caused remote MCP clients to see "no tools" after OAuth connect. Auth ordering preserved — Bearer-token validate runs before transport delegation. 4 new unit tests covering the sentinel shape + ASGI no-op semantics + auth gate ordering.

**Persona name case-insensitive lookup + normalize-at-write** (`tk_884b2321fdb74170`, commits `b39f9c9` + `df501b6`). New `_resolve_persona_name(db, project_id, name) -> str | None` helper in `routes/tickets.py` using `func.lower(AgentPersona.name) == name.lower()` with `.order_by(id).first()` (deterministic tiebreaker, won't raise `MultipleResultsFound`). `start_ticket` resolver, `create_ticket`, and `update_ticket` PUT all normalize `assigned_to` through it; unknown names pass through as free-text for back-compat. `list_tickets` filter switched to `func.lower(Ticket.assigned_to) == assigned_to.lower()` so the discovery path also surfaces legacy `Atlas`-cased rows when an agent queries `?assigned_to=atlas`. `create_persona` endpoint rejects case-insensitive duplicates with 409 + explicit "case-insensitive-unique" error message — resolves ambiguity at the source so the resolver tiebreaker stays defensive. Closes the v0.10.18 onboarding pain where `start_ticket` 400'd because `.agents/atlas-backend.md` capitalizes the persona name but `agent_personas.name` stores it lowercase. 6 regression tests. Restored gutted assertion block at the end of `test_review_state_cap_preserves_earliest_rounds` (Codex R1 LOW1 — accidentally removed during the initial edit). Codex R1 1 MED + 2 LOW → R2 VERIFIED-CLEAN.

**Release-skill Scribe-Site gate** (`tk_2cc5bcca97284a91`, commit `a1071de`). Release script step 6f now reads `git diff $(git describe --abbrev=0 --tags)..HEAD -- site/` and skips Scribe-Site invocation + site deploy when `wc -l` returns 0. Saves ~5-10 minutes on backend-only releases without weakening the "no stale site content" invariant (still mandatory when `site/` is touched).

**`.release/README.md` documenting expected develop→main merge conflicts** (`tk_7d3b6b1ac1714c60`, commit `12a58c1`). 71-line pure-docs addition explaining what `.release/` contains, which paths conflict at merge time (`.claude/commands/release.md`, `CLAUDE.md`, `.agents/**`), why we don't use a broad `merge=ours` `.gitattributes` rule (would silently drop legitimate develop-side changes), and the escape valve if conflict-then-sanitize pain returns (narrow per-path `.gitattributes`, never broad).

### Operational closes (no code)

- `tk_c72ad4f99dae404b` — housekeeping close (`sfs ticket watch` shipped earlier in v0.10.7)
- `tk_12e6d8775eb045a2` — v0.10.5 compile source manifest review (Codex R2 was clean on 2026-05-15; just never got the `resolve_ticket` call)
- `tk_a1144426a013413c` — v0.10.7 customer-ask provenance fields review (Codex R8 was VERIFIED-CLEAN on 2026-05-17; same)

### Verification

- pytest tests/ -x -q → **2023 passed**, 2 xfailed (pre-existing migration-003 chain)
- ruff check src/ → clean
- helm lint charts/sessionfs → clean
- pip-audit → 0 vulnerabilities
- npm audit (dashboard + site) → 0 vulnerabilities
- bandit → 0 HIGH; 11 MEDIUM pre-existing baseline (all in files NOT touched by v0.10.23)
- Shield-SR independent pre-release review → APPROVED 0 CRITICAL / 0 HIGH / 0 NEW MEDIUM

### Authoring + reviews

All 5 commits implemented directly by Claude (no Codex CLI spawn), per CEO's standing rule. Polling Codex (`author_persona="codex"`) reviewed:
- tk_49db8d2b6c424d35: R1 MED + R1 LOW → R2 VERIFIED-CLEAN
- tk_884b2321fdb74170: R1 MED + 2 LOW → R2 VERIFIED-CLEAN
- f7fdcc1, a1071de, 12a58c1: small targeted changes shipped without review (release-process tooling + ChatGPT outage hotfix)

## [0.10.22] - 2026-05-24

**Org-collaboration fixes.** Three tickets that close long-standing operational gaps on the org surface: new org members can finally read org-scoped artifacts without first cloning the repo; invite emails actually send; recipients have a dashboard `/invites` page to accept or decline; and Scout's n8n workflow gets the multi-source signal-shape contract that was deferred from v0.10.21. No platform breakage — one strictly-additive migration (046).

### Added

**Scout multi-source signal-shape contract** (`tk_918073e8aa4c4478`, commits `59812de` + `0589563` + `cb66312`). New `docs/integrations/scout-signal-shape.md` (~600 lines) defines the 9-field canonical envelope (`source`, `source_id`, `title`, `url`, `content`, `posted_at`, `author`, `signal_strength`, `raw`) that every upstream feeds through before Scout's reasoning loop runs. `source_id` is the dedup key and threads into v4's `source_context` format unchanged. Four reference Code-node templates ship in `docs/integrations/n8n-source-adapters/` (HN Algolia, GitHub Releases, Reddit, generic RSS) each with an inline FIXTURE block. PII scrubbing hard rules in §5 + §5.1 with the Reddit adapter as the reference implementation: drops `selftext` from `raw` entirely and rewrites `raw.author` to the `u/`-prefixed form before emit so even a misconfigured workflow can't leak. Mandatory `Code: strip raw` node in the n8n workflow diagram for token-budget discipline. Updates `scout-n8n.md` §8 to point at the new spec and adds `.agents/scout-competitive-intelligence.md` for persona ownership. Adding source N+1 = one normalizer + one Merge-node pin; reasoning, dedup, and write steps unchanged. Three rounds of polling-Codex review → VERIFIED-CLEAN.

**Org-member project access fix** (`tk_7a457574c5624e12`, commit `b469d8d`). New shared helper `src/sessionfs/server/auth/project_access.py` with `user_can_access_project(db, user_id, project) -> bool` returning True for the 3-branch user-key predicate: owner OR member of `project.org_id` OR captured-session-on-git-remote (legacy fallback). Closes the v0.10.0 Phase 5 design intent documented in `db/models.py:187` but never enforced — new org members were getting 403 on every org-scoped artifact (personas, KB, wiki, tickets, agent runs) until they cloned the repo and synced a session. Both `_get_project_or_404` copies (knowledge.py + wiki.py) collapsed to thin wrappers delegating to `load_project_for_user`; 30+ existing importers across tickets, agent_runs, retrieval_audit, personas pick up the fix without call-site changes. `routes/projects.py:list_projects` SQL gains the OrgMember subquery branch so org-scoped projects appear in `GET /api/v1/projects` for the right users. `get_project` and `update_project_context` route through the same helper. `delete_project` intentionally unchanged (owner-or-admin only). Service-key path untouched — `service_key.org_id` remains the only org boundary there. 9 regression tests covering all 6 affected route families + cross-org isolation + personal-project unchanged. Codex R1 first-round VERIFIED-CLEAN.

**Org invite email + dashboard `/invites` page** (`tk_6afbcfefe5804c1d`, commits `4888043` + `91d3522` + `ca30263`). Closes the 2026-05-23 onboarding incident where 4 new SessionFS signups had to be hand-messaged accept URLs because the invite endpoints never fired email.

- **Migration 046** strictly additive: `declined_at`, `decline_reason`, `last_emailed_at` on `org_invites`. No backfill.
- **`services/invite_helpers.dispatch_invite_email`** shared best-effort helper. Both invite endpoints (`routes/org.py` legacy + `routes/org_members.py` multi-org) + the new resend endpoint route through it. SMTP/Resend failure logs but does not 500 the route.
- **`email.EmailProvider.send_org_invite` + `email_templates.org_invite_email`** mirror the v0.10.9 handoff lifecycle shapes. Every user-controlled field is `_html.escape`'d.
- **`app.state.config`** wired in `server/app.py` lifespan so services can read `config.app_url` for dashboard URL building without re-importing `ServerConfig`.
- **New `POST /api/v1/orgs/{org_id}/invites/{invite_id}/resend`** — admin-only re-fire without creating a new invite row. 409 on already-accepted/declined/expired.
- **New `POST /api/v1/org/invite/{invite_id}/decline`** — recipient marks the invite refused with optional reason (1000-char cap). Atomic rowcount-1 guarded transition; subsequent `/accept` rejects 400.
- **New `GET /api/v1/org/invites/me`** — pending invites for the logged-in user, server-filtered to exclude accepted/declined/expired.
- **Atomic accept + decline FSM transitions** via rowcount-1 guarded conditional UPDATE on `accepted_at IS NULL AND declined_at IS NULL AND expires_at > now()` with `synchronize_session=False` to skip the ORM's client-side WHERE eval (SQLite vs PG datetime safety). On rowcount=0 the session rolls back (dropping any pending OrgMember add for the accept path) and returns 409. Codex R1 MEDIUM fix.
- **Active-invite predicate** (`accepted_at IS NULL AND declined_at IS NULL AND expires_at > now()`) now consistent across all 5 sites (duplicate-invite check at both creation paths, /invites/me, admin /invites, resend pre-flight). Closes Codex R1 MEDIUM + R2 LOW (predicate drift would otherwise block admin recovery after a decline + surface stale "Pending Invites" rows on the dashboard).
- **Dashboard `/invites` page** + nav link with pending-count badge in both desktop nav and mobile drawer (`dashboard/src/invites/InvitesPage.tsx` + `useInvites.ts`). Reads `?highlight=<invite_id>` from the email accept link to outline the matching row. Inline reason textarea on decline. Accept invalidates `['my-orgs']` so the org settings page refreshes.

13 regression tests covering both invite endpoints firing email, best-effort behavior under provider failure, resend without new row, resend admin-only + 409 on accepted, decline blocks subsequent accept, decline wrong-email denied, /invites/me filters to current user + hides accepted/declined, atomic accept-after-concurrent-decline returns 409 with no OrgMember leak, admin-can-reinvite-after-decline, admin pending list hides declined + expired. Three Codex review rounds → R3 VERIFIED-CLEAN.

### Verification

- pytest tests/ -x -q → **2007 passed**, 2 xfailed (pre-existing migration-003 chain)
- ruff check src/ → clean
- helm lint charts/sessionfs → clean
- dashboard tsc -b → clean
- dashboard npm test -- --run → **187 passed**
- dashboard npm run build → no warnings
- pip-audit → 0 vulnerabilities (105 deps)
- npm audit (dashboard + site) → 0 vulnerabilities
- bandit → 0 HIGH, 17 MEDIUM (all pre-existing baseline; 0 in v0.10.22 files)
- Shield-SR independent pre-release review → APPROVED 0 CRITICAL / 0 HIGH / 0 NEW MEDIUM

### Authoring + reviews

All 3 tickets implemented directly by Claude (no Codex CLI spawn), per CEO's standing rule. Polling Codex (`author_persona="codex"`) reviewed every commit:
- tk_918073e8aa4c4478: R1 MEDIUM (raw PII leak) + R1 LOW (GH prerelease policy) → R2 LOW (stale §5.1 prose) → R3 VERIFIED-CLEAN
- tk_7a457574c5624e12: R1 VERIFIED-CLEAN first round
- tk_6afbcfefe5804c1d: R1 HIGH (test assertion) + 2 MEDIUMs (reinvite-after-decline + accept/decline race) → R2 LOW (admin list filter) → R3 VERIFIED-CLEAN

### Operational unlock

After this deploys, every future org invite from either endpoint sends an email automatically; recipients see pending invites in `/invites` with a nav badge; admins can resend without re-inviting and recover from a decline by re-inviting; new org members can pull personas + read KB + see project listing without needing to clone the repo first as a workaround. The 2026-05-23 onboarding pain doesn't recur.

## [0.10.21] - 2026-05-22

**Phase 4a + the continuous-agent stack.** This release lands the agent-authored KB attribution model (Phase 4a), the MCP write-path equivalent, the full Scout v4 n8n integration contract, AgentRun provenance exposure, and an `agent_runs:read` service-key opt-in — five tickets that together unblock continuous autonomous agents on SessionFS for the first time. Plus a transitive starlette CVE pin caught by Shield-SR.

### Added

**`persona_name` + `author_class` on KnowledgeEntry** (`tk_f5ae3eea92934add`, commits `dfbd215` + `e8b9297`). Migration 045 strictly additive:
- `persona_name VARCHAR(64) NULL` — validated against `agent_personas` for the same project on write (mirrors v0.10.7 wiki page-write policy); unknown personas return 422.
- `author_class VARCHAR(16) NOT NULL DEFAULT 'human'` — every existing row backfills to `'human'` via the column default; new writes default to `'human'` for user keys, `'agent'` for service keys.
- Composite index `idx_knowledge_persona_recent (project_id, persona_name, created_at DESC)` for Scout v4's per-persona retrieval.

Anti-spoof invariant: `POST /entries/add` forces `author_class = "agent"` whenever `auth.actor_type == "service_key"`, regardless of request body. The DB-level regression `test_service_key_cannot_spoof_author_class_human` asserts at row level after `db_session.refresh(entry)`.

`GET /entries` gains three new query params with AND-composition against the existing filters:
- `persona_name` — exact match (max 64 chars)
- `author_class` — `Literal["human", "agent"]`
- `source_filter` — literal substring match via `.contains(value, autoescape=True)`; `%` and `_` in user input escaped so they match as data characters, not LIKE wildcards (Codex R1 LOW fix, commit `e8b9297`).

`KnowledgeEntryResponse` exposes both fields on every read path (list, get, dismiss, refresh, promote, supersede).

**MCP `add_knowledge` persona attribution** (`tk_8028c79963fe4dc7`, commits `73ee0ab` + `9c479c8`). MCP input schema gains `persona_name` (max 64) and `author_class` (`human|agent`). When `persona_name` is omitted AND the active-ticket bundle's `project_id` matches the resolved project (mirroring `update_wiki_page` v0.10.7), the handler auto-threads `bundle.persona_name` AND defaults `author_class` to `"agent"` — without this default the bundle path would land rows as `human` and miss the agent retrieval channel (Codex R1 MEDIUM). Explicit args win in both directions. The tool response surfaces the attribution that actually landed so callers can detect server-side overrides (anti-spoof). 8 regression tests.

**Scout v4 n8n workflow contract** (`tk_d8e02fb02d874b3f`, commits `9544c99` + `4f96e23` + `cbaf702` + `99e1fcd` + `20615b1`). New `docs/integrations/scout-n8n.md` (~620 lines) is the full contract for running Scout as a continuous analyst from n8n via the direct HTTP API. Covers the scope matrix, durable `trigger_ref` via a `Build trigger_ref` Set node using documented n8n primitives (`$exec.id` + `String.hash('sha256')`), persona preflight failure path (no AgentRun to complete on 404), POST /agent-runs flow with `/start` explicitly skipped (user-key only), KB writes with stable `source_context` format `scout:n8n:<workflow_id>:<exec_id>:<signal_id>`, dedupe + retry caps (`MAX_KB_WRITES_PER_RUN=20`, `MAX_TICKET_CREATES_PER_RUN=5`, `MAX_RETRY_PER_SIGNAL=1`), failure-branch severity matrix, and a run-N + run-N+1 smoke-test procedure that verifies the agent-memory loop end-to-end. No platform changes — uses existing v0.10.21 endpoints only. Three rounds of polling-Codex review + one n8n-engineer human review.

**`AgentRunResponse` exposes service-key audit triple** (`tk_a77b671fd86a42fb`, commit `e61d38b`). `actor_type`, `service_key_id`, `service_key_name` (v0.10.10 migration 042 columns) now serialize on every AgentRun read path (create, list, get, start, complete, cancel) via the shared `_row_to_response` helper. No DB migration. Scout n8n smoke-test (§6.1) now verifies provenance directly via `GET /agent-runs` instead of routing through the KB attribution fallback. 3 regression tests including the backward-compat invariant (user-key rows return `actor_type='user'` + null service-key fields).

**`agent_runs:read` service-key opt-in** (`tk_31b87575d5534d00`, commits `f4cded5` + `20615b1`). `GET /agent-runs` and `GET /agent-runs/{run_id}` converted from `get_current_user` to `require_scope("agent_runs:read")`, routed through `_get_project_for_auth` (v0.10.19 helper that branches on `auth.key_kind`) followed by `assert_service_key_can_access_project`. Write routes untouched — `agent_runs:write` remains required for create + complete; `/start` and `/cancel` stay user-key only by design. The scope catalog in `docs/api-keys.md` flips `agent_runs:read` from "reserved" to **✅ live**. 5 regression tests covering allow, deny, missing-scope, cross-project (`project_not_in_allowlist`), cross-org (`cross_org_denied`), and user-key backward compatibility.

### Security

**`starlette>=1.0.1` pin** (commit `3775ce0`) to close GHSA-86qp-5c8j-p5mr (host-header URL reconstruction → potential auth bypass). Shield-SR caught this during the initial v0.10.21 review; FastAPI 0.135+ requires only `starlette>=0.46.0` so the floor pin prevents the resolver from landing on a vulnerable transitive version on the next deploy.

### Verification

- `pytest tests/ -x -q` → **1985 passed + 2 xfailed** (was 1959 + 2 baseline at v0.10.20; +26 net new across the five tickets).
- `pytest tests/server/integration/test_scoped_service_keys.py tests/server/integration/test_agent_runs_api.py -q` → **78 passed** (was 42 + 18 = 60 at v0.10.20).
- `pytest tests/unit/test_mcp_server.py -q` → 104 passed (was 96; +8 for MCP add_knowledge attribution).
- Dashboard `npm test` → **187 passed** (unchanged — no frontend changes).
- `ruff check src/` → clean.
- `mypy src/sessionfs/server/routes/{knowledge,agent_runs,personas,tickets}.py src/sessionfs/mcp/server.py` → clean.
- `helm lint charts/sessionfs` → clean.
- `pip-audit` → **0 vulnerabilities** (after starlette pin).
- `npm audit` (dashboard + site) → **0 vulnerabilities**.
- bandit → 0 HIGH / 0 new MEDIUM (pre-existing MEDIUM not in v0.10.21 files).
- Migration smoke: isolated 044 → 045 → 044 SQLite upgrade/downgrade with legacy-row backfill regression → clean.
- Codex review threads — all VERIFIED-CLEAN after fixes:
  - `tk_f5ae3eea92934add` R1 LOW (source_filter wildcards) → resolved in `e8b9297`.
  - `tk_8028c79963fe4dc7` R1 MEDIUM (bundle author_class default) → resolved in `9c479c8`, R2 VERIFIED-CLEAN.
  - `tk_d8e02fb02d874b3f` R1 → R2 → R3 cycle (2 MEDIUM + 2 LOW, then 1 MEDIUM on n8n expression primitives) → R3 VERIFIED-CLEAN, plus a separate R1 LOW caught after `agent_runs:read` landed fixed in `20615b1`.
  - `tk_a77b671fd86a42fb` R1 VERIFIED-CLEAN first round.
  - `tk_31b87575d5534d00` R1 LOW (scope-coverage doc) → R2 VERIFIED-CLEAN.
- Shield-SR independent pre-release security review on the initial Phase 4a slice — APPROVED 0/0/0/0 (caught + fixed the starlette CVE).

### Scout v4 unblock

After v0.10.21 deploys, Scout v4 can run as a continuous autonomous analyst from n8n with a least-privilege service key. Each execution loads its own prior findings via `GET /entries?persona_name=scout&author_class=agent&limit=30`, writes new findings with `persona_name=scout` + a stable `source_context`, wraps everything in an `AgentRun` that surfaces service-key provenance in read responses, and runs without needing a CEO personal key for the runtime persona load. The same loop unblocks every future autonomous agent on the SessionFS fleet (Sentinel-watch, Ledger-monitor, Relay-listener).

## [0.10.20] - 2026-05-22

Phase 3.6 service-key opt-in: persona CRUD routes. Unblocks the n8n Scout agent's runtime persona load and every future autonomous agent that needs to fetch the actual persona doc as its system prompt at runtime.

### Added

**Service-key access to persona routes** (`tk_4d932478298b4e27`). 5 routes converted to `require_scope`, mirroring the v0.10.18 + v0.10.19 conversion pattern exactly:

READ (`require_scope("personas:read")`):
- `GET /projects/{pid}/personas` (list)
- `GET /projects/{pid}/personas/{name}` (detail — the route the n8n Scout's "Get Scout Persona" node calls)

WRITE (`require_scope("personas:write")`):
- `POST /projects/{pid}/personas` (create)
- `PUT /projects/{pid}/personas/{name}` (update)
- `DELETE /projects/{pid}/personas/{name}` (soft-delete via is_active=False)

Tier C (`assume_persona`, `forget_persona` — session-state mutators) intentionally NOT touched. Those mutate the caller's local provenance bundle, not a project row, and remain user-key only.

**AgentPersona audit-row columns (migration 044)**. Migration 042 (v0.10.10) added `actor_type` / `service_key_id` / `service_key_name` to 5 audit tables; v0.10.19 migration 043 added them to Ticket. AgentPersona was excluded. v0.10.20 closes that gap with a strictly-additive migration mirroring 043 exactly (nullable cols, no defaults, no constraints, no indexes, no FKs). create/update/delete persona writes now stamp the triple from `AuthContext` so the audit row records the service-key principal.

**Cross-route helper reuse**: persona routes import `_get_project_for_auth` from `knowledge.py` (mirrors `wiki._get_project_or_404` cross-route import). Service keys load by id only; user keys keep the legacy owner/session gate. `assert_service_key_can_access_project(db, auth, project)` enforces the org+allowlist boundary AFTER the helper returns at all 5 sites.

### Changed

**`docs/api-keys.md`**: `personas:read` and `personas:write` moved from "reserved" to **✅ live** with full endpoint lists. Scope-vocabulary count corrected from 14 → 15 (Codex R1 LOW fix). Scout pattern example extended to include the runtime persona-load step. New explicit note that `assume_persona`/`forget_persona` remain user-key only.

### Verification

- pytest tests/ -x -q → **1959 passed + 2 xfailed** (was 1952 + 2; +7 new regression tests)
- pytest tests/server/integration/test_scoped_service_keys.py -q → **42 passed** (was 35; +7 new)
- dashboard `npm test` → **187 passed** (unchanged)
- ruff check src/ → clean
- mypy src/sessionfs/server/routes/personas.py → clean
- helm lint charts/sessionfs → clean
- pip-audit → **0 vulnerabilities**
- npm audit (dashboard + site) → **0 vulnerabilities**
- bandit → 0 HIGH / 0 new MEDIUM (all existing MEDIUM are pre-existing)
- Migration smoke: isolated 043 → 044 → 043 SQLite upgrade/downgrade → clean
- Polling Codex review thread on `tk_4d932478298b4e27` — R1 1 LOW (scope-count typo) → **resolved** in commit `cbcd0c6`. No behavioral findings.
- Shield-SR independent pre-release security review — **APPROVED, 0 CRITICAL / 0 HIGH / 0 MEDIUM / 0 LOW**

### Scout agent unblock

After v0.10.20 deploys, the stopgap "CEO Personal Key" credential in the n8n Scout workflow can be removed and the "Get Scout Persona" node rewired back to the org-bound service key (now scoped `[personas:read, knowledge:read, knowledge:write, tickets:write]`). Every future n8n agent — Sentinel-watch, Ledger-monitor, Relay-listener — can now operate as a least-privilege service key with runtime persona load.

## [0.10.19] - 2026-05-21

Phase 3.5 service-key opt-in: ticket CREATE + knowledge routes. Unblocks the n8n Scout agent and every future autonomous discovery/research agent that gathers external signals and writes them back as KB entries + new tickets.

### Added

**Service-key access to ticket CREATE + knowledge routes** (`tk_65a096acf57946eb`). 8 new routes converted to `require_scope`, mirroring the v0.10.18 ticket-route conversion exactly:

WRITE (`require_scope("tickets:write")`):
- `POST /projects/{pid}/tickets` (ticket create — the entrypoint Scout uses to open follow-up tickets)

READ (`require_scope("knowledge:read")`):
- `GET /projects/{pid}/entries` (list/search)
- `GET /projects/{pid}/entries/{eid}` (detail)

WRITE (`require_scope("knowledge:write")`):
- `POST /projects/{pid}/entries/add`
- `PUT /projects/{pid}/entries/{eid}` (dismiss/update)
- `PUT /projects/{pid}/entries/{eid}/refresh`
- `PUT /projects/{pid}/entries/{eid}/promote`
- `PUT /projects/{pid}/entries/{eid}/supersede`

Tier C routes (`compile`, `rebuild`, `dismiss-stale`, `health`, `compilations`) deliberately NOT touched — they remain user-key only. A regression test (`test_service_key_still_denied_on_compile_rebuild_dismiss_stale`) pins the deny-by-default contract so a future Phase 4 must opt them in explicitly.

**Ticket audit-row columns (migration 043)**. Migration 042 (v0.10.10) added `actor_type` / `service_key_id` / `service_key_name` to 5 tables (TicketComment, KnowledgeEntry, AgentRun, RetrievalAuditEvent, HandoffEvent) but Ticket itself was excluded. v0.10.19 closes that audit gap with a strictly-additive migration mirroring 042 exactly (nullable cols, no defaults, no constraints, no indexes). `create_ticket` populates the columns from `AuthContext`. KnowledgeEntry mutators (dismiss/update/refresh/promote/supersede) now stamp the same triple on every write path so the audit row records the service-key principal.

**`_get_project_for_auth` helper** (Codex R1 MEDIUM 1). Service keys minted by an org admin against a project owned by a different org member previously 403'd on the legacy `_get_project_or_404(project_id, db, user.id)` user-owner / captured-session gate before the org/allowlist boundary could evaluate. The new helper branches on `auth.key_kind`: service keys load the project by id (404 only); user keys keep the legacy gate. Replaced across all 7 converted knowledge.py call sites + 8 converted tickets.py call sites. `assert_service_key_can_access_project(db, auth, project)` continues to enforce the org+allowlist boundary AFTER the helper returns.

**`knowledge:read` is now truly read-only for service keys** (Codex R1 HIGH 1). The `GET /entries` search side-effect path previously mutated `used_in_answer_count` / `last_relevant_at` / `retrieved_count` whenever the search matched. A service key with only `knowledge:read` could therefore alter freshness/decay state. v0.10.19 gates the telemetry UPDATE on `auth.key_kind == "service" and "knowledge:write" not in auth.scopes`: service keys with read-only scope no longer mutate counters; service keys with read+write keep the existing telemetry; user keys are unaffected (key_kind="user" short-circuits the AND).

### Changed

**`docs/api-keys.md`** — `knowledge:read` and `knowledge:write` moved from the "reserved for Phase 3" list to **✅ live** with full endpoint lists. New Scout-pattern example added (`search_project_knowledge` → `add_knowledge` → `create_ticket` curl flow). Audit-row narrative updated to include Ticket alongside the other 5 audit tables. Tier C remains explicitly called out as user-key only.

### Verification

- pytest tests/ -x -q → **1952 passed + 2 xfailed** (was 1950 + 2 xfailed; +2 new regression tests in this release)
- pytest tests/server/integration/test_scoped_service_keys.py -q → **35 passed** (was 33; +2 new — knowledge:read freshness gate + cross-owner project access)
- dashboard `npm test` → **187 passed**
- ruff check src/ → clean
- mypy src/sessionfs/server/routes/tickets.py src/sessionfs/server/routes/knowledge.py → clean
- helm lint charts/sessionfs → clean
- pip-audit → **0 vulnerabilities**
- npm audit (dashboard + site) → **0 vulnerabilities**
- Migration smoke: isolated 042 → 043 → 042 SQLite upgrade/downgrade → clean
- Codex independent review thread on `tk_65a096acf57946eb` — R1 NEEDS-FIXES (HIGH + MEDIUM) → R2 **VERIFIED-CLEAN** on commit `adf4955`
- Shield-SR independent pre-release security review — **APPROVED, 0 CRITICAL / 0 HIGH / 0 MEDIUM / 0 LOW**

### Scout agent unblock

After v0.10.19 deploys, the n8n Scout service key on `proj_c0242b0fccbd48b4` will be reminted with scopes `[tickets:read, tickets:write, knowledge:read, knowledge:write, agent_runs:read, agent_runs:write]` and Scout will start operating with a least-privilege org-bound key (no more CEO personal key for autonomous discovery agents).

## [0.10.18] - 2026-05-21

Opts ticket routes into service-key auth (v0.10.10 Phase 3). Unblocks the n8n triage agent and every future CI/cloud-agent integration that touches tickets.

### Added

**Service-key access to ticket routes** (`tk_1ea90b1d210d40a8`). 7 ticket routes converted to `require_scope` dependencies, mirroring the v0.10.10 agent_runs/handoffs pattern exactly:

READ (`require_scope("tickets:read")`):
- `GET /projects/{pid}/tickets` (list)
- `GET /projects/{pid}/tickets/{tid}`
- `GET /projects/{pid}/tickets/{tid}/comments`
- `GET /projects/{pid}/tickets/{tid}/review-state`

WRITE (`require_scope("tickets:write")`):
- `POST /projects/{pid}/tickets/{tid}/comments`
- `POST /projects/{pid}/tickets/{tid}/start`
- `POST /projects/{pid}/tickets/{tid}/complete`

For every converted route: `assert_service_key_can_access_project(db, auth, project)` runs before any DB work — enforces both the org boundary (`service_key.org_id == project.org_id`) and the optional per-key project allowlist. For write routes: `TicketComment` audit rows now populate `actor_type` / `service_key_id` / `service_key_name` from `AuthContext` so service keys never silently impersonate humans in provenance.

Lease-epoch fencing on `/comments` and `/complete` preserved under service-key callers. `_assert_lease_required_mode` accepts the new `AuthContext` and sources `require_lease_epoch_on_ticket_writes` from `auth.org_id` (the service key's bound org), falling back to `project.org_id` for legacy user-key paths. Stale-lease fence still uses the row-count atomic-UPDATE pattern (unchanged).

Resolve/escalate/approve/dismiss/create routes intentionally NOT converted — out of scope per the ticket. User keys with the `*` wildcard scope continue to work on all converted routes.

`docs/api-keys.md` updated: `tickets:read` + `tickets:write` moved from "reserved for Phase 3 route opt-in" to "✅ live" with full endpoint lists. New triage-bot persona example added to "When to mint one".

### Tests

7 new regression tests in `tests/server/integration/test_scoped_service_keys.py`:
- `test_service_key_can_list_tickets_with_read_scope`
- `test_service_key_can_read_ticket_detail_and_comments_and_review_state`
- `test_service_key_can_start_complete_and_comment_with_write_scope` (asserts `actor_type='service_key'` + key id/name populated on the comment row)
- `test_service_key_tickets_insufficient_scope_denied` (asserts `insufficient_scope` 403 with `required` + `current` arrays)
- `test_service_key_tickets_project_allowlist_denied_before_write` (asserts `project_not_in_allowlist` and pre-write denial with `COUNT(comments)` unchanged)
- `test_service_key_still_denied_on_unconverted_ticket_route` (POST /accept still rejects with `service_key_not_allowed`)
- `test_service_key_ticket_lease_required_mode_and_stale_fence` (422 on missing lease, 409 on stale lease, 200/201 on valid lease)
- `test_user_key_regression_on_converted_ticket_routes` (user keys still 200, audit row `actor_type='user'`)

### Notes

- **Tests:** 1942 backend + 186 dashboard passing (+7 net new). 2 xfail-strict pre-existing.
- **MCP tools:** 58 (unchanged).
- **Migrations:** 001–042 (no new — migration 042 already added the `actor_type` / `service_key_id` / `service_key_name` columns on `TicketComment`).
- **No new endpoints.** Pure auth-boundary widening on existing routes.
- **Compatibility:** Additive. User keys continue to authorize identically. Service keys gain access to the converted ticket routes; all other ticket routes (`resolve`, `escalate`, `approve`, `dismiss`, `accept`, `POST /tickets`) still reject service keys with `service_key_not_allowed`.

### Process notes

- **AgentRun pattern miss documented for follow-up.** The work shape (orchestrator spawns Codex CLI as Atlas with a ticket-scoped prompt → Codex edits + tests + reports) is exactly what v0.10.2's AgentRun + CI Integration was designed to capture. This release bypassed it (no `create_agent_run` record exists for the Codex session that did the implementation). Atlas/Forge follow-up: document the orchestrator pattern in `docs/integrations/cloud-agents.mdx` so the next external-agent spawn uses AgentRun for provenance.
- **Persona name case-sensitivity bug surfaced.** The ticket was assigned to `Atlas` (capitalized) but the active persona record is `atlas`. MCP `start_ticket` rejects with 400 because the match is case-sensitive. Worked around via `assign_persona` to `atlas`. Atlas to file a follow-up ticket: either match case-insensitively in the persona resolver or validate against the persona registry at ticket-create / assign-time.

## [0.10.17] - 2026-05-21

`knowledge_health` API + MCP fix. The `pending_entries` count was a generalized "uncompiled non-dismissed claims" — it counted superseded/stale claims (compile skips) and missed auto-promotable evidence (compile processes). Operators got "Run compile to process N pending entries" advice that lied in both directions: stayed flat after a successful compile, and reported 0 for projects with only eligible evidence.

### Fixed

**`knowledge_health.pending_entries` overcount + counterpart undercount** (`tk_935a4eb62be94676`):

- `pending_entries` now mirrors the compile pipeline's Phase 2b select EXACTLY: `claim_class='claim'` AND `compiled_at IS NULL` AND not dismissed AND `freshness_class IN ('current', 'aging')` AND `superseded_by IS NULL`.
- New `auto_promotable_evidence` field surfaces evidence rows the compiler's Phase 2a will auto-promote AND that survive Phase 2b post-promotion (`claim_class='evidence'` AND `confidence >= 0.5` AND `length(content) >= 30` AND not dismissed AND `compiled_at IS NULL` AND current/aging AND no superseder).
- New `uncompiled_notes` field counts notes that need `bulk_promote` first.
- `potentially_stale` flag uses the same compile-eligible predicate as `pending_entries` so notes / superseded claims with novel terms can't drive a false-positive "Context may be stale" warning.
- "Run compile" recommendation now drives off `compile_work_total = pending_entries + auto_promotable_evidence`. Structured breakdown below threshold: "3 pending claims + 1 auto-promotable evidence row — run compile". Single-line above threshold: "Run compile to process N entries (P pending claims, Q auto-promotable evidence)".
- "No compilations yet — run compile to build context" recommendation now gated on `compile_work_total > 0`. Notes-only fresh projects get "No compile-eligible entries yet — add claims (or promote evidence) before running compile".
- New "N uncompiled notes — call bulk_promote..." recommendation fires whenever notes exist.

MCP `get_knowledge_health` tool description rewritten to document each field with its exact filter clause so MCP agents read the same contract the route enforces.

5 new regression tests pin each surface:
- `test_health_pending_entries_matches_compile_filter`
- `test_health_counts_auto_promotable_evidence`
- `test_health_potentially_stale_ignores_notes_and_superseded`
- `test_health_auto_promotable_excludes_stale_and_superseded_evidence`
- `test_health_no_compile_advice_when_only_notes_exist`

Toggle-tested: temporarily disabling the new filter clauses produces the original overcount shape and the regression test fails with the exact pre-fix count.

### Notes

- **Tests**: 1935 backend + 186 dashboard passing (+5 net new). 2 xfail-strict pre-existing.
- **MCP tools**: 58 (unchanged — `get_knowledge_health` description rewritten, schema unchanged).
- **Migrations**: 001–042 (no new migrations).
- **API contract**: backward-compatible additive change. `uncompiled_notes` and `auto_promotable_evidence` are optional on the dashboard `ProjectHealthResponse` interface so older self-hosted servers still parse.
- **No site changes** — site already cloud-first as of yesterday's pivot.

## [0.10.16] - 2026-05-20

Follow-on hotfix. v0.10.15 closed one `uq_kl_link` violation site (`_auto_supersede`) but production exposed a SECOND site at `auto_generate_concepts`'s existing-page branch where `KnowledgeLink` rows are deleted-and-re-inserted with the same composite key in a single transaction. SQLAlchemy's UnitOfWork orders INSERTs ahead of DELETEs by default, so the INSERT fires while the old row is still present, violating `uq_kl_link`. The IntegrityError bubbles past the route's try/except, the session enters PendingRollback, and the next `_count_pages` call surfaces a Starlette text/plain 500.

### Fixed

**`/compile` 500 from delete+insert race in `auto_generate_concepts`** (`tk_09d8bdf4f6374a13`, R2 follow-up) — added explicit `await db.flush()` after the delete loop at `src/sessionfs/server/services/compiler.py:1346` and before the new-link add loop. This serialises the DELETEs to the open transaction (still rollbackable) so the INSERTs that follow see a clean slate. Plus belt-and-suspenders `await db.rollback()` in the `/compile` route's exception handler at `src/sessionfs/server/routes/knowledge.py:976-987` so a future commit failure in `auto_generate_concepts` no longer poisons the session for downstream calls like `_count_pages`.

New regression test `test_auto_generate_concepts_flushes_delete_before_insert` seeds a concept page + entry-link for the same pair, monkeypatches `check_concept_candidates` + `generate_concept_article` to force the regenerate branch, and asserts exactly one (entry, page) link survives the delete+re-add cycle.

### Notes

- **Tests:** 1930 backend + 186 dashboard passing (+1 net new regression test). 2 xfail-strict pre-existing.
- **MCP tools:** 58 (unchanged).
- **Migrations:** 001–042 (no new migrations).
- **v0.10.15 stays merged.** The pair-level dedup in `_auto_supersede` is correct and necessary. v0.10.16 closes the second site.
- **Cloud Run log diagnosis FTW.** The first attempt at v0.10.15 (commit `4d7f1f7` before the R1 fix) was based on a misdiagnosis. Reading the actual asyncpg traceback from `gcloud logging read` revealed the precise table, constraint, and stack — turning a "the worker is being killed somehow" theory into a deterministic fix at the right line.

## [0.10.15] - 2026-05-20

Hotfix release. Fixes the ACTUAL `/compile` 500 on proj_c0242b0fccbd48b4 — the v0.10.14 fix closed an audited crash class but turned out not to be the bug crashing prod.

### Fixed

**`/compile` 500 on projects with stable `contradicts` KnowledgeLinks** (`tk_09d8bdf4f6374a13`) — `_auto_supersede` in `src/sessionfs/server/services/compiler.py` was creating `KnowledgeLink` rows with `link_type='contradicts'` on every `/compile` pass without checking whether the same `(project_id, source_type, source_id, target_type, target_id)` tuple already existed. The `supersedes` path was self-gating via `older.superseded_by is not None: continue`, but `contradicts` had no such gate. Every subsequent `/compile` against a project with stable contradicts links queued a duplicate insert that SQLAlchemy autoflush surfaced on the next `db.execute()` (the decay UPDATE), raising `asyncpg.exceptions.UniqueViolationError` on `uq_kl_link`. The IntegrityError bubbled up uncaught past FastAPI's middleware, and Starlette returned its default `PlainTextResponse("Internal Server Error", status_code=500)` — exactly 21 bytes of text/plain. The v0.10.14 release notes misdiagnosed this response shape as a Cloud Run worker kill; the worker stays alive, only the request fails.

Root cause confirmed from Cloud Run logs (revision `sessionfs-api-00132-f2p`):

```
sqlalchemy.dialects.postgresql.asyncpg.AsyncAdapt_asyncpg_dbapi.IntegrityError:
<class 'asyncpg.exceptions.UniqueViolationError'>:
duplicate key value violates unique constraint "uq_kl_link"
```

Fix: prefetch all existing entry→entry link tuples `(source_id, target_id, link_type)` for the project ONCE at the top of `_auto_supersede`, then check the in-memory set before `db.add(link)` for both the supersedes and contradicts branches. In-run additions also join the set so two compiles in the same transaction can't double-add. New regression test `test_auto_supersede_idempotent_on_existing_contradicts_link` seeds the failure shape (two entries with same entity_ref + overlap in the contradicts band), calls `_auto_supersede` twice on fresh sessions, asserts no exception + flat link count across passes.

### Notes

- **Tests:** 1928 backend + 186 dashboard passing (+1 net regression test). 2 xfail-strict pre-existing.
- **MCP tools:** 58 (unchanged).
- **Migrations:** 001–042 (no new migrations).
- **v0.10.14 stays merged.** The `_safe_entry_link_ids` helper closes a real audited crash class for the malformed-link path. The two regression tests added for it remain valid.
- **My v0.10.14 root-cause writeup was wrong about "Cloud Run worker kill / GFE 21-byte text/plain 500".** The 21-byte body is Starlette's default `PlainTextResponse` for uncaught exceptions with `debug=False`. The worker stays alive between requests; only the failing request bubbles. Updated knowledge entry 408 in proj_c0242b0fccbd48b4 to reflect this.

## [0.10.14] - 2026-05-20

Hotfix release. Fixes the `/compile` availability bug that hit proj_c0242b0fccbd48b4 immediately after the v0.10.13 R5 metadata restore.

### Fixed

**`/compile` 500 on projects with malformed `KnowledgeLink` source_ids** (`tk_d92434fe63564c06`) — `_prune_dead_concept_pages` in `src/sessionfs/server/services/compiler.py` did `int(lk.source_id)` without a try/except when `source_type=='entry'`. `KnowledgeLink.source_id` is a `String(64)` column; the convention is `str(KnowledgeEntry.id)` but legacy rows can have non-numeric values (slugs, UUIDs, malformed migration data). A single bad row triggers `ValueError`. The `/compile` route's `try/except Exception` around `auto_generate_concepts` catches the raise — but earlier links in the same page's link list already had partial `await db.delete(lk)` calls. The SQLAlchemy session is left in a poisoned state. Subsequent `db.execute()` calls in the route then die with un-handled SQLAlchemy errors that bypass FastAPI's exception middleware → Cloud Run worker kill → 21-byte plain-text "Internal Server Error" from Google Frontend.

Diagnosed via: `bulk-promote` on the same project returned 200 while `/compile` returned 500 in 0.5s. The fast-fail signature ruled out timeout / OOM / LLM. Diffing `_prune_dead_concept_pages` against its sibling code in `auto_generate_concepts` (line ~1198) showed the latter has the exact `try/except (TypeError, ValueError) → continue` pattern this site was missing.

Fix shipped in two rounds:

- **R0** (`tk_d92434fe63564c06`) — mirror the sibling guard at `_prune_dead_concept_pages`. Malformed rows skip with a warning log instead of crashing.
- **R1** (Codex review `tk_e5185f5d432243f2`, HIGH) — Codex widened the audit and surfaced a remaining unguarded `int(lk.source_id)` at the per-existing-page deletion check in `auto_generate_concepts` (compiler.py:1247-1251 pre-fix), and a 4th site outside compiler.py at `src/sessionfs/server/routes/wiki.py:602` (page regenerate route). Refactored to extract `_safe_entry_link_ids(links, *, page_slug=None) -> list[int]` helper at module scope and route all 3 compiler sites through it; wiki.py:602 gets the matching inline guard. Final grep confirms only the helper's own (guarded) cast remains.

Two regression tests pin the failure shapes: `test_prune_dead_concept_pages_skips_malformed_links` (direct call path) and `test_auto_generate_concepts_existing_page_skips_malformed_links` (monkeypatches `check_concept_candidates` to force the existing-page branch where R1 HIGH lived).

This bug was masked before v0.10.13 by the destructive `/rebuild` data-loss bug (`tk_bc3c02a63e994717`): both ran together and the destructive resets ate the projection before the prune crash could be observed independently. v0.10.13's fail-closed contract isolated this one to "availability only" — zero DB drift on each failed `/compile`, confirmed in production.

### Notes

- **Tests:** 1927 backend + 186 dashboard passing (+2 net regression tests). 2 xfail-strict pre-existing.
- **MCP tools:** 58 (unchanged).
- **Migrations:** 001–042 (no new migrations).
- **No new endpoints.** Single-file backend fix.

## [0.10.13] - 2026-05-20

Incident-driven safety release. No new features. Forced by the 2026-05-20 incident on the SessionFS dev project where `/rebuild` wiped `project.context_document` and every active claim's `compiled_at` when the recompile crashed mid-flight. Two related fixes ship together.

### Fixed

**Fail-closed `/rebuild` and `/compile`** (`tk_bc3c02a63e994717`, CRITICAL) — `compile_project_context` previously committed twice inside the function (lines 419 + 522) and the `/rebuild` route added two more commits BEFORE calling it for the destructive resets. Four transaction boundaries through one logical compile, with the destructive resets in the first two — any crash after them was data-destructive. Refactored to a single atomic commit at the end of `compile_project_context`:

- Phase 1 (reads only): SELECT project FOR UPDATE + pending claims + active claims for source_manifest + sessions for persona lookup. No writes.
- Phase 2 (compute in memory, no DB writes during LLM call): build LLM prompt, call LLM (or `_simple_compile` fallback), enforce word budget, group entries, compute section page contents.
- Phase 3 (single atomic transaction): apply freshness / decay / retention / auto-promote UPDATEs; apply destructive reset if `force_rebuild=True`; update `project.context_document`; mark pending entries compiled; INSERT `ContextCompilation` (flush only — committed at end); upsert section pages; **commit once.** Any exception before the final commit rolls back everything.

New `force_rebuild: bool = False` parameter on `compile_project_context`. The `/rebuild` route is now a thin wrapper calling `compile_project_context(force_rebuild=True)`. The destructive reset (NULL `compiled_at` on every active claim, clear `context_document`) now runs INSIDE the same atomic transaction.

Codex R1 caught a related correctness bug: `force_rebuild=True` was nulling `compiled_at` but the compile still read `context_before = project.context_document` and merged new claims INTO that stale text — so dismissed/superseded/aging entries' content could survive a "rebuild" forever. Fixed by splitting the variable: `previous_context` preserves the prior document for the audit trail (`ContextCompilation.context_before` is unchanged), `compile_base_context` becomes `""` when `force_rebuild=True` so the merge genuinely starts from scratch.

3 new rollback regression tests in `tests/server/integration/test_knowledge.py`, including the headline `test_rebuild_rollback_on_compile_crash_preserves_prior_state` — the test that would have caught the 2026-05-20 incident if it had existed.

### Added

**Admin repair endpoint** (`tk_dd3ba7082ef0432e`, HIGH) — `POST /api/v1/admin/projects/{project_id}/restore-from-compilation` recovers a project's `context_document` AND the `compiled_at` metadata on participating entries from a chosen `ContextCompilation` snapshot. Body `{compilation_id: int, dry_run: bool = true}`. Reads the row, parses `source_manifest` for participating entry ids, restores both the document text and the per-entry `compiled_at` in a single atomic transaction. Admin-gated (`require_admin`); audit-logged via `AdminAction`. Codex R1 caught Python's bool-is-int trap (a body `{"compilation_id": true}` would coerce to `compilation_id=1`); fixed with explicit `isinstance(x, bool)` rejection on both `compilation_id` and `dry_run`.

Closes the metadata gap left by the v0.10.12 R1 emergency restore (which only repaired the document text via the public `PUT /context` endpoint). Provides a safe operator tool for future incidents.

9 integration tests in `tests/server/integration/test_admin.py` cover dry-run/apply, 404 on unknown compilation_id, 404 on cross-project compilation_id, non-admin 401/403, empty `context_after` 422, non-integer compilation_id 422, bool/string `dry_run` 422.

### Reviews

Codex single review thread `tk_879dbd5a5a034d0e`: R1 1 HIGH (force_rebuild empty-base) + 1 MEDIUM (strict bool rejection) → **R2 VERIFIED-CLEAN**.

Shield-SR pre-release security review: 0 critical / 0 high / 0 medium new findings. Release approved.

### Notes

- **Tests:** 1925 backend + 186 dashboard passing (+13 net over v0.10.12). 2 xfail-strict pre-existing migration-003 PG-syntax (`tk_7dc9e8764a5a4297`).
- **MCP tools:** 58 (unchanged from v0.10.12).
- **Migrations:** 001–042 (no new migrations).
- **New endpoint:** 1 (admin restore-from-compilation).
- **Non-blocking follow-ups deferred:** Duplicate freshness pass in `/rebuild` (cosmetic, can return count from `compile_project_context` later) and pre-existing duplicate concept generation in `/compile` route — both flagged by Codex as out of scope for the safety release.

## [0.10.12] - 2026-05-19

### Added

**Bulk-promote eligible KB notes to claims** (parent `tk_c64915570f4d4042`, Codex R1+R2 VERIFIED-CLEAN on `tk_03263e280f4b4732`) — the v0.10.10 confidence-clamp bug (fixed in v0.10.10) left many production KBs with hundreds of stuck note-class entries. The per-entry repair path (`PUT /entries/{id}/confidence` + `PUT /entries/{id}/promote` × N) doesn't scale past ~5 entries. SessionFS dev had 270 stuck notes; Baptist Health has the same problem. This release ships the practical bulk repair surface.

- New service `src/sessionfs/server/services/bulk_promote.py:promote_eligible_notes()` — pure-Python eligibility filter (class=note, not dismissed, not superseded, optional entry_type filter, ≥ `min_length` chars, ≥ `min_confidence` unless caller overrides via `set_confidence`, no near-duplicate against active claims). Pre-fetches active claims once; in-run promotions join the compare set so two near-duplicate notes can't both promote. Dry-run is the default and makes zero writes.
- New endpoint `POST /api/v1/projects/{pid}/entries/bulk-promote` — same auth as the single-entry promote (project membership via `_get_project_or_404`). `BulkPromoteRequest` validates `min_length∈[1, 10000]`, `min_confidence`/`set_confidence∈[0.0, 1.0]`, optional `entry_type` ≤ 64 chars. Returns `{promoted, skipped, reasons, promoted_ids, dry_run}` with a stable per-reason breakdown (`too_short`, `low_confidence`, `duplicate`, `dismissed`, `superseded`, `wrong_type`, `already_claim`).
- New MCP tool `promote_eligible_entries` (57 → 58 tools) — wraps the endpoint with local arg validation BEFORE the network call (boolean rejection on numeric fields, blank-string rejection on `entry_type`, range bounds on every numeric). 60s httpx timeout for the bulk operation.
- New CLI `sfs project promote-eligible --min-length N --min-confidence N --confidence N --entry-type X --confirm` — dry-run is the default; `--confirm` is required to mutate. Renders a verdict header + Skipped-by-reason rich table.

**Per-ticket review-state endpoint row cap** (`tk_33a25a12a5cf4dc3`, Shield-SR LOW follow-up from v0.10.11) — `GET /api/v1/projects/{pid}/tickets/{tid}/review-state` now caps `TicketComment` fetch at 500 rows (parity with `list_ticket_comments`). `ORDER BY (created_at, id) ASC` ensures the earliest review rounds always survive the cap on long threads — callers care about "what was raised first and is it closed", not the latest 500 noisy comments.

**Release-process hardening** — three improvements informed by the v0.10.11 post-mortem:

- **Pinned mypy >= 1.20** (`tk_cd196cf0421b4a6e`) in `pyproject.toml` dev extras. v0.10.11 CI on main failed because local mypy 1.19.1 missed 11 union-attr errors that CI's 1.20.2 caught. The pin is the narrowest fix for the entire class of "green local / red on main" surprises. Local now reports 1.20.2 on `pip install -e .[dev]`.
- **`.release/sanitize_main.py`** (`tk_b9b6eb47685e4916`) — deterministic Python helper for stripping private files during the develop → main merge. Reads `.release/private-files.txt`; supports `--dry-run` (default) and `--apply`; exits non-zero on residual leaks. Replaces the prior 14-chained-shell-command sweep. 8 unit tests in `tests/unit/test_sanitize_main.py` (parser + leak-finder); tests skip cleanly when `.release/` is stripped on main.
- **Post-PyPI smoke step** (`tk_4698db39fb0248b3`) — new release skill step 12b runs a fresh-venv `pip install sessionfs==X.Y.Z` + `sfs --help` shape check + new-command help check with 6×30s retry for PyPI index lag. Catches "wheel published but ships wrong contents" before users do.

**MCP handler dispatch tests** for `promote_eligible_entries` (`tk_97b693793c814f4d`, Codex R2 residual-risk follow-up) — 8 new tests in `tests/unit/test_mcp_server.py` mirroring the `update_entry_confidence` pattern: URL routing, body forwarding for all 5 args, range validation, boolean rejection on numeric fields, blank-string and non-string `entry_type` rejection, `dry_run`-must-be-bool, entry_type whitespace stripping.

### Fixed

**Codex R1 fixes on bulk-promote** (in `5a41085` on develop):
- **MEDIUM** — Dry-run/confirm asymmetry on in-run duplicates. The original implementation only appended eligible content to the compare set inside `if not dry_run`, so a dry-run with two near-duplicate notes reported both as promoted while the confirmed run correctly skipped one. Fix: append unconditionally; only the field writes stay gated on `not dry_run`. The "inspect-then-mutate" safety contract now holds. New regression test pins the parity.
- **LOW** — Boundary defaults disagreed with the service constant. The API/CLI/MCP defaulted to `min_confidence=0.85` but the single-entry promote gate and the service constant are 0.8 — entries at [0.80, 0.85) would promote one-by-one but skip in bulk. Lowered all three boundary defaults to 0.8 for parity.

### Security

- Auto-upgraded dependencies during Shield-SR audit: `cryptography` 46.0.5 → 48.0.0 (pin widened to `>=46.0.7`), `idna` 3.11 → 3.15, `mako` 1.3.10 → 1.3.12, `pygments` 2.19.2 → 2.20.0, `urllib3` 2.6.3 → 2.7.0, `pip` 26.0 → 26.1.1. Closes 9 HIGH/MEDIUM CVEs.

### Reviews

- Bulk-promote: `tk_03263e280f4b4732` R1 (1 MEDIUM + 1 LOW) → R2 **VERIFIED-CLEAN**
- v0.10.12 follow-ups (row cap + MCP dispatch tests): `tk_bf4c08059e1e42c1` — administratively closed per CEO (pure test-hardening + 1-line LIMIT guard; Codex itself pre-flagged as non-blocking residual-risk on the parent reviews)

Shield-SR pre-release security review: 0 critical / 0 high / 0 medium new findings. Release approved.

### Notes

- **Tests:** 1912 backend + 186 dashboard passing (+41 net over v0.10.11). 2 xfail-strict pre-existing migration-003 PG-syntax (`tk_7dc9e8764a5a4297`).
- **MCP tools:** 57 → 58 (+1: `promote_eligible_entries`).
- **Migrations:** 001–042 (no new migrations).

## [0.10.11] - 2026-05-19

### Added

**CLI for v0.10.10 scoped service keys** (parent tk_e0d7db15ff814c0a, Codex R1+R2 VERIFIED-CLEAN on tk_53e042ecee7e43ff) — closes the human-facing surface gap so issuing, listing, rotating, and revoking keys no longer requires curl.

- `sfs admin service-keys list|create|revoke|rotate|scopes` — org admin + Team+ tier required for mutations. Raw key prints to stdout exactly once on create + rotate with a `Save this — you won't see it again.` warning on stderr.
- `sfs auth keys list|create|revoke` — any logged-in user, personal-key surface.
- `--output-key` flag on create + rotate emits ONLY the raw key for `KEY=$(sfs admin service-keys create ... --output-key)` CI capture. Mirrors `--output-id` on `ticket create`.
- Local scope validation against `VALID_SCOPES` (imported from server schemas, single source of truth). Unknown scopes and `*` wildcard for service keys fail with exit 2 before any network call.
- Structured error codes from the server (insufficient_scope, cross_org_denied, etc.) are surfaced verbatim via `_parse_error` covering all 4 FastAPI envelope shapes ({"error": {...}}, {"detail": {...}}, {"detail": "string"}, {"detail": [validation_list]}).
- All commands inherit `SESSIONFS_API_KEY` / `SESSIONFS_API_URL` env-var auth via the shared `_get_api_config` helper.

**`docs/api-keys.md`** (tk_522991717c6446c9, Codex R1+R2 VERIFIED-CLEAN on tk_ee765a03b69045eb) — public reference covering both kinds of keys, the 14-scope vocabulary with current opt-in status (handoffs:write and agent_runs:write live today; remaining 12 scopes reserved for Phase 3 route opt-in), service-key lifecycle, cloud-agent integration recipes (Bedrock, Vertex, GitHub Actions, GitLab MR), rotation policy guidance, and complete structured error code reference. `docs/cli-reference.md` gains full option tables for `sfs auth keys` and `sfs admin service-keys`.

**Per-ticket review-state derivation** (tk_e025375272b84a95, Codex R1 VERIFIED-CLEAN on first round on tk_d7354a8032e8443b) — compact summary of open findings, closed findings, last verdict, and severity counts for long review threads. No new schema or stored state — computed at read time from existing TicketComment rows.

- `GET /api/v1/projects/{pid}/tickets/{tid}/review-state` — same auth as get_ticket (project membership + agent_tickets feature). Cross-project access denied by `_get_project_or_404`. Returns `review_state: null` for tickets without any codex-reviewer comments.
- `get_ticket_review_state` MCP tool (56 → 57 total tools) — wraps the endpoint. Description steers agents away from scraping comments themselves.
- `sfs ticket review-state <id>` CLI command — renders verdict + severity counts header, Rounds table, Open + Closed finding lists.
- Parser (`src/sessionfs/server/services/review_state.py`) extracts findings from Codex's consistent comment shape (`Codex R{N} review on tk_X: VERDICT` header + `• SEVERITY — text` bullets). Closure rule: findings raised in round N close when any subsequent round has VERIFIED-CLEAN verdict. Reopens not modeled (rare and fragile to text-match). 14 unit tests + 4 integration tests.

### Fixed

**`sfs ticket watch` DNS error** (tk_aeb8580706d84e2e, Codex R1 VERIFIED-CLEAN on first round on tk_d611718c24944110) — caught by Codex during v0.10.9 review when its sandbox got a raw `[Errno 8] nodename nor servname provided, or not known` traceback. The shared `_api_request` helper now catches `httpx.RequestError` (parent of ConnectError, TimeoutException, NetworkError, SSL errors — but NOT HTTPStatusError) and exits 1 with an actionable `Configure cloud auth first: sfs auth login --url ... or set SESSIONFS_API_URL / SESSIONFS_API_KEY` message. Every CLI command using `_api_request` benefits — not just ticket watch/comments.

**Shared `_api_request` helper** — two regression fixes from the service-keys CLI work:
- DELETE method now forwards `json_data` via `client.request("DELETE", ..., json=...)`. Httpx's `client.delete()` doesn't accept a `json` kwarg, so v0.10.10 revoke endpoints (which require a `RevokeKeyRequest` body) were dropping the reason and 422-ing.
- 204 No Content responses no longer crash `resp.json()` even when content-type is `application/json` — short-circuits to empty string when `resp.content` is empty.

**Manifest schema `retrieval_audit_id`** (tk_c19f7694009e4723) — `src/sessionfs/spec/schemas/manifest.schema.json` now allows the `retrieval_audit_id` field added in v0.10.4 (migration 039). `test_full_capture_pipeline` was failing because `additionalProperties: false` at root rejected valid manifests. Schema fixed inline before this release.

**Dashboard footer version** (commit e87953d, posted earlier) — Layout.tsx no longer hardcodes a version string. Vite reads from a new `dashboard/VERSION` file (or SFS_VERSION env / pyproject.toml fallbacks) at build time. `/release` skill now bumps `dashboard/VERSION` alongside pyproject and Chart.yaml.

### Reviews

Five separate Codex review threads, all VERIFIED-CLEAN:
- Service-keys CLI: tk_53e042ecee7e43ff (R1 1 MEDIUM + 1 LOW → R2 CLEAN)
- docs/api-keys.md: tk_ee765a03b69045eb (R1 1 MEDIUM + 3 LOW → R2 CLEAN)
- DNS error catch: tk_d611718c24944110 (R1 CLEAN first round)
- Review-state: tk_d7354a8032e8443b (R1 CLEAN first round)
- Update-entry-confidence + promote-entry MCP tools (tk_44bc8c8862304051) bundled from earlier in same cycle (R2 CLEAN)

Shield-SR pre-release security review: 0 critical / 0 high / 0 medium new findings — release approved. One LOW noted as Atlas follow-up (`tk_33a25a12a5cf4dc3`: cap row count on review-state endpoint).

### Notes

- **Tests:** 1871 backend + 186 dashboard passing (+52 net over v0.10.10). 2 xfail-strict pre-existing migration-003 PG-syntax (`tk_7dc9e8764a5a4297`).
- **MCP tools:** 54 → 57 (+3: `update_entry_confidence`, `promote_entry`, `get_ticket_review_state`).
- **Migrations:** 001–042 (no new migrations this release).
- **Phase 3 route opt-in for service keys still deferred** to v0.10.12+ — TicketComment / KnowledgeEntry / RetrievalAuditEvent write routes remain on plain `get_current_user` and reject service keys. Scopes are defined but reserved.

## [0.10.10] - 2026-05-18

### Added

**Scoped service API keys** (parent tk_2e030a85253143df, 7 Codex review rounds across two phase reviews) — replaces broad static user bearer tokens for cloud agents, CI runners, and integration partners with expirable, scope-restricted service keys. Static user tokens no longer required for Bedrock, Vertex, GitHub Actions, GitLab MR pipelines.

- **Migration 042** — additive. `ApiKey` gains `key_kind` ('user' | 'service'), `org_id` (FK organizations CASCADE, required for service keys), `scopes` (JSON list), `expires_at`, `revoked_at`, `revoke_reason`, `created_by_user_id` (FK users SET NULL), `last_used_ip` (45 chars, IPv6-safe), `service_key_name`, `project_ids` (optional project allowlist within org), `key_prefix` (real raw-key prefix captured at create/rotate for incident response). Existing user keys back-fill to `scopes='["*"]'` so every legacy token continues to authorize unchanged.
- **5 audit-row tables** (TicketComment, KnowledgeEntry, AgentRun, RetrievalAuditEvent, HandoffEvent) gain `actor_type` ('user' | 'service_key') + `service_key_id` + `service_key_name`. Service keys never silently impersonate humans in provenance.
- **Deny-by-default for service keys** (Codex Phase 2 R2 HIGH) — `get_current_user` rejects service keys with 403 `service_key_not_allowed`. The ONLY way a service key reaches a route handler is via `require_scope(*scopes)` or `require_any_scope(*scopes)` dependencies. Pre-route enforcement, not post-route middleware — side effects are impossible from unauthorized service keys.
- **Scope vocabulary** — 14 capability scopes (sessions:read, handoffs:read/write, tickets:read/write, personas:read/write, knowledge:read/write, rules:read/write, agent_runs:read/write, retrieval_audit:read, admin:*). `*` wildcard reserved for legacy user/admin keys; service keys must enumerate explicitly (rejected with 422 at create otherwise).
- **Cross-org boundary** — `assert_service_key_can_access_project` enforces that a service key's `org_id` matches the target project's `org_id` before any project-scoped state change. Optional per-key `project_ids` allowlist gives finer control. Routes that handle handoffs resolve the source session's project via `sessions.project_id` (authoritative since migration 036) — Codex Phase 2 R6 caught and fixed a `git_remote_normalized` fallback that originally "preferred the key's org" on ambiguity (bypass vector).
- **Org-scoped admin surface** — `POST/GET /api/v1/orgs/{org_id}/service-keys`, `DELETE /api/v1/orgs/{org_id}/service-keys/{id}`, `POST /api/v1/orgs/{org_id}/service-keys/{id}/rotate`. Org admin role + Team+ tier required for mutations. Cross-org 404 (not 403) for existence hiding. Personal user keys under `/api/v1/auth/me/api-keys`.
- **Structured error codes** — `api_key_revoked`, `api_key_expired`, `service_key_not_allowed`, `insufficient_scope` (with `required` + `current` arrays), `cross_org_denied` (with `key_org_id` + `project_org_id`), `service_key_project_required`, `service_key_project_not_registered`, `service_key_project_ambiguous`. Agents can distinguish rotate-needed from permission-needed without log-scraping.
- **Secret handling** — raw key returned exactly once on POST create + POST rotate. List/detail endpoints return only `key_prefix`. Tests assert raw key absent from log output on both paths.
- **Phase 2 route opt-in** — 5 handoff write routes (POST `/handoffs`, claim, revoke, decline, comments POST) + agent_runs create/complete now use `require_scope("handoffs:write" | "agent_runs:write")`. Service-key callers populate `actor_type='service_key'` on the resulting HandoffEvent and AgentRun audit rows.
- **18 new integration tests** covering deny-by-default, scope allow/deny, expiry, revocation, cross-org boundary, project-id authoritative resolution, orphan-handoff denial, raw-key-not-in-list, last_used_at/_ip updates, wildcard rejection on service keys, unknown-scope rejection.

**Knowledge base confidence fix** (tk_483cede83deb443b, Codex review tk_328006e4c6024dd8 R1-R3 VERIFIED-CLEAN) — restores the CEO-driven workflow for marking strategy/decision entries as high-confidence and promoting them into compiled context.

- **Root cause** — `POST /entries/add` silently clamped `confidence = min(confidence, 0.7)` for `session_id in ("cli-ask", "manual")`. Combined with MCP's default `session_id="manual"`, this lowered EVERY caller-supplied confidence on manual/MCP-sourced entries to 0.7, blocking promotion (gate is 0.8). Entries 403/404/405 stayed at 0.7 regardless of what the caller passed.
- **Fix** — `AddEntryRequest.confidence` is now `Optional[float]` with `Field(None, ge=0.0, le=1.0)`. When caller passes confidence explicitly, server honors it. When omitted, legacy 0.7-for-manual / 1.0-for-session-derived defaults still apply. MCP `_handle_add_knowledge` only forwards confidence when caller explicitly supplied it.
- **NEW endpoint** — `PUT /api/v1/projects/{pid}/entries/{id}/confidence` lets agents/dashboards update confidence on existing entries that got clamped before the fix landed. Repair path for legacy data.
- **Compile no-op consistency** — `POST /compile` no-op response now derives `context_words_before/after` from `project.context_document` (same source as `/health.word_count`) instead of returning 0 unconditionally. New `noop_reason` field surfaces actionable diagnosis: "No claims eligible to compile. N note(s) are uncompiled — notes do not auto-promote. Update confidence via PUT /entries/{id}/confidence then call PUT /entries/{id}/promote, which returns the specific gate failures...".
- **Shared serializer** — `_entry_to_response(entry)` helper centralizes the full `KnowledgeEntryResponse` shape so new routes can't omit fields (retrieved_count, used_in_answer_count, compiled_count, last_relevant_at, supersession_reason).

**`list_ticket_comments` MCP tool** (tk_32f3dacf1c9749bc, bundled follow-up from v0.10.9) — wraps `GET /api/v1/projects/{pid}/tickets/{id}/comments` with `since` (ISO timestamp) + `since_id` (cursor tiebreaker for same-millisecond ties) + `limit` (1-500, default 200). Unblocks autonomous Codex/Claude review polling loops over MCP.

### Reviews

- Scoped service keys foundation (Phase 1): Codex R1 scope → R2 schema → R3 implementation → R4 re-review VERIFIED-CLEAN (tk_9fefcc3832ac49da)
- Scoped service keys route opt-in (Phase 2): Codex R5 → R6 → R7 VERIFIED-CLEAN (tk_f664b480140f40c4)
- list_ticket_comments: Codex R2 CLEAN (tk_32f3dacf1c9749bc)
- KB confidence fix: Codex R1 → R2 → R3 VERIFIED-CLEAN (tk_328006e4c6024dd8). HIGH finding (MCP add_knowledge confidence clamp) caught by Codex during this review — Atlas had originally fixed only the symptom (/confidence repair endpoint) without addressing the write-path bug.
- Shield-SR independent pre-release security review: 0 CRITICAL / 0 HIGH / 0 MEDIUM. Release approved.

### Deferred to v0.10.11+

- TicketComment / KnowledgeEntry / RetrievalAuditEvent writer provenance (their routes still on plain `get_current_user` — service keys correctly rejected; full route opt-in is Phase 3)
- CLI surface for scoped service keys (`sfs admin service-keys list/create/revoke/rotate`, `sfs auth keys list/create/revoke`)
- Bedrock + Vertex + GitHub Actions + GitLab MR doc updates to use scoped service keys (`docs/integrations/*` examples)
- New `docs/api-keys.md`
- Optional `ApiKey` model_validator rejecting service keys with empty `scopes`

## [0.10.9] - 2026-05-17

### Added

**Comprehensive handoff redesign** — single-feature release that elevates handoffs from "copy a session blob to an email recipient" to a first-class coordination primitive on the same plane as tickets, personas, and agent runs.

- **Migration 041** — additive schema, zero existing data loss. 12 new columns on `handoffs` (recipient_user_id, recipient_team_id, ticket_id, persona_name, revoked_at, revoked_by_user_id, revoke_reason, handoff_kind, snapshot_persona_name, snapshot_ticket_title, sender_tier_snapshot, viewed_at) and 5 new tables: `teams`, `team_members`, `handoff_attachments` (with `project_id` for unambiguous wiki slug lookups), `handoff_comments`, `handoff_events`.
- **Exactly-one-recipient invariant** — every handoff specifies exactly one of `recipient_email` (any user by email), `recipient_user_id` (direct account match), or `recipient_team_id` (team handoff, Team+ tier). Enforced server-side via Pydantic `@model_validator(exactly_one_recipient)` with `strip_blank` field validators so whitespace-only IDs collapse to None before the count check.
- **Provenance carry-through** — sender attaches `ticket_id` + `persona_name`; on claim, the response includes an `active_ticket_payload` (ticket_id, persona_name, project_id, lease_epoch) that the recipient's CLI writes to `~/.sessionfs/active_ticket.json`. The next captured session is automatically tagged with the handed-off context. Persona-only handoffs derive `project_id` from the source session's git_remote so write_bundle has what it needs.
- **Sender curates context** — `attachments[]` list of `{kind: kb_entry|wiki_page|ticket, ref_id}` validated against the session's project at create time. At claim, attachments are re-validated against recipient's accessible projects; inaccessible refs are silently dropped and surfaced in `dropped_attachments` with structured reason (`not_accessible` | `deleted` | `invalid_id` | `unknown_kind`).
- **Lifecycle endpoints** — `POST /handoffs/{id}/revoke` (sender-only, atomic, required reason), `POST /handoffs/{id}/decline` (recipient-only, atomic, optional reason), `POST/GET /handoffs/{id}/comments` (sender + valid recipients, paged 200), `GET /handoffs/{id}/events` (audit log, paged 200).
- **Team management surface** — `routes/teams.py`: `POST/GET /teams`, `GET/DELETE /teams/{id}`, `POST/GET/DELETE /teams/{id}/members/...`. Org admin required for mutations; any org member can list. `team_handoff` feature added to Team + Enterprise tiers.
- **Existence-hiding (404-not-403)** — non-parties get 404 on `GET /handoffs/{id}`, `/claim`, `/decline`, `/comments`, `/events`, `/summary` (rewritten from 403 + email-only check). Eligibility is checked BEFORE any lazy-expire writes or status-specific responses so non-recipients can't distinguish pending vs claimed vs revoked via response codes.
- **Atomic claim race** — `UPDATE Handoff WHERE id=X AND status='pending'` runs FIRST. Race losers never write blobs or insert Session rows — eliminates orphan blob storage from concurrent team-member claims. `new_session_id` is pre-allocated and included in the atomic UPDATE.
- **Lazy expiry** — pending handoffs past `expires_at` flip to `expired` on read with audit event. Per-tier `expires_in_hours` clamping: 720h (30d) on Free/Pro/Team, 2160h (90d) on Enterprise.
- **viewed_at tracking** — first GET by a non-sender stamps `viewed_at` + emits a `viewed` event. Sender's later reads do not overwrite.
- **4 lifecycle email notifications** — `send_handoff_claimed` (to sender), `send_handoff_revoked` (to recipient), `send_handoff_declined` (to sender), `send_handoff_comment` (to other party). All templates `html.escape()` user-supplied fields. All sends are best-effort try/except — never fail the route.
- **8 new handoff MCP tools** — `create_handoff`, `claim_handoff`, `get_handoff`, `list_inbox_handoffs`, `list_sent_handoffs`, `revoke_handoff`, `decline_handoff`, `add_handoff_comment`. Alongside the `list_ticket_comments` follow-up, v0.10.9 exposes 54 tools total (was 45). All handoff tools use the shared `_handoff_api_config` helper (handoffs aren't project-scoped).
- **CLI extensions** — `sfs handoff` accepts `--to-user-id`, `--to-team-id`, `--ticket`, `--persona`, `--expires-hours`, `--attach kind:ref_id` (repeatable). New subcommands: `sfs handoffs get | revoke | decline | comment | comments | events`. `sfs pull-handoff` now persists `active_ticket_payload` to `~/.sessionfs/active_ticket.json` (the core v0.10.9 promise — without this the recipient session loses ticket+persona context).

### Review history

Full receipts across 5 rounds of Codex review on parent ticket `tk_89e90060e6314311`:
- R1 (scope): clean with 2 corrections (file paths `email.py`/`email_templates.py` not `email_service.py`; tier gating preserved at existing Pro+, not "Free=email")
- R2 (schema): 3 MEDIUM (recipient_team_id missing FK, _accessible_project_ids missed org-member projects, wiki slug ambiguity in attachment validation) + 1 LOW (blank-strip whitespace) — all fixed pre-implementation
- R3 (implementation): 2 HIGH (claim/decline existence leak via status-before-auth + pull_handoff dropping active_ticket_payload) + 4 MEDIUM (persona-only project_id, blob copy before atomic claim, /summary using old 403, attachment response missing project_id) + 2 LOW (MCP not dispatch-tested, no real team E2E test) — all fixed
- R4 (re-review): VERIFIED-CLEAN. Independent Shield-SR security review: zero critical/high findings. Release approved.

### Deferred to v0.10.10+
- Broadcast claim_policy + per-team-member fan-out
- `parent_handoff_id` relay chain (A→B→C)
- `Session.blob_refcount` for storage efficiency on unchanged claims
- Background expiry sweeper (currently lazy-on-read per Codex G)
- Team email fan-out for revoke/comment notifications

## [0.10.8] - 2026-05-16

### Fixed
- **CI test regression on v0.10.7.** `test_returns_kb_and_session_sources` passed locally but failed on the public CI runner. Root cause: `_fetch_kb_entries_raw` re-imports `load_config` from `sessionfs.daemon.config` inside the function body. The monkeypatch on `mcp_server.load_config` doesn't intercept that re-import; in CI without a real `~/.sessionfs/config.toml` on disk, `load_config` returned defaults (empty api_key) and the function early-returned `[]`, leaving `sources_cited` empty. Fix patches `sessionfs.daemon.config.load_config` directly alongside the existing `mcp_server.load_config` patch. Test now passes under CI-like conditions (no config file on disk).

No product code changes from v0.10.7; this release exists to unblock the Deploy API and Deploy MCP Server pipelines that failed on the v0.10.7 push because of the test failure. Cloud Run picks up v0.10.7's customer-ask provenance fields + `sfs ticket watch` CLI + migration 040 SQLite-compat fix here.

Version skipped 0.10.7.1 because Helm chart versions require strict SemVer (X.Y.Z) — 4-segment versions aren't valid.

## [0.10.7] - 2026-05-16

### Added
- **Customer-ask provenance fields.** Three read-side extensions to existing endpoints, all additive (no schema breaks). Same pattern as v0.10.5 `source_entries` work, extending the evidence trail to three more read endpoints.
  - **`sources_cited` on `ask_project`** — typed `list[{type: "kb"|"session", id}]` returned alongside the assembled research markdown. KB IDs come from a structured re-fetch (`_fetch_kb_entries_raw`) — no regex extraction from prose. Session IDs come from the local search index. Open for `{type: "section", slug}` once ask_project grows a compiled-section retrieval step.
  - **Wiki page revision history.** New `wiki_page_revisions` table (migration 040) stores every page edit with `revision_number`, `revised_at`, `user_id`, `persona_name`, `ticket_id`, full content snapshot. New `GET /api/v1/projects/{id}/pages/{slug}/history` endpoint with cursor pagination + `next_cursor` envelope. New `get_wiki_page_history` MCP tool (45 → 46 tools). Wiki PUT now accepts optional `persona_name` + `ticket_id`; MCP `update_wiki_page` auto-threads them from the active-ticket bundle when project matches.
  - **`personas_active` on session summaries.** New `session_summaries.personas_active` JSON list collected from manifest + per-message persona annotations. Refreshed on both deterministic and narrative regeneration paths. Documented as session-level overblocking (per-decision authorship requires summarizer prompt rework — separate ticket).
  - **Lease required-mode org setting.** Org admins can flip `Organization.settings.require_lease_epoch_on_ticket_writes = true`; complete/comment/accept ticket writes then return 422 if `lease_epoch` is omitted. Existing supplied-lease behavior unchanged. Personal projects (no org) bypass.
- **`sfs ticket watch <id>` CLI.** Polls the `GET /comments` endpoint and renders new comments live (Panel + Markdown). Flags: `--interval N` (clamped to [5, 300] seconds, default 30), `--from-author NAME` filter, `--exit-on-new` for CI scripting, `--notify` for macOS terminal-notifier. Pairs with `sfs ticket comments` (v0.10.5).

### Fixed
- **Migration 040 SQLite-incompat.** First implementation called `op.create_unique_constraint` after `op.create_table` — fails on SQLite (ALTER TABLE can't add constraints). Codex flagged across R2-R7. Fix: `sa.UniqueConstraint(...)` moved inside `op.create_table()` as a column-level argument; downgrade simplified to drop the table (constraint goes with it). An interim follow-up migration 041 was tried first but Codex correctly identified that a linear repair migration can't heal a chain that halts at the failed 040 — pivoted to in-place edit since 040 had never shipped.
- **Wiki revision provenance ownership check.** `_validate_revision_provenance` allows `ticket_id` only when the user owns the ticket through one of three roles: creator (`Ticket.created_by_user_id`), current resolver (`Ticket.resolver_user_id`), or active executor (open `RetrievalAuditContext` for this ticket created by the user via `start_ticket`, with matching `lease_epoch` AND ticket still `in_progress`). Without the executor path, agents executing colleagues' tickets couldn't attribute wiki revisions to them — defeating the agent-execution provenance use case. Lease+status gate replaces the original `closed_at IS NULL` check (which never expired because nothing in the codebase sets `closed_at`).
- **Test isolation flake.** `test_unknown_block_type_logged` was failing under full-suite runs because `tests/server/integration/test_migrations_sqlite.py` triggered alembic's `fileConfig()` which defaults to `disable_existing_loggers=True`, killing the `sfs.writeback` logger's propagation. Fixture now constructs alembic `Config` without the ini path so `fileConfig` is skipped.

### Tests
- 1727 → 1745 backend (+18: ask_project sources_cited, wiki history + revision provenance + executor-association + stale-lease + post-status, lease required-mode + personal-project bypass, `sfs ticket watch` clamping/filter/exit-on-new/404, migration smoke xfail). 186 dashboard unchanged.
- 2 xfail-strict: `test_migrations_sqlite.py` SQLite chain blocked by migration 003's `USING GIN` (broader fix tracked in `tk_7dc9e8764a5a4297`).

### Security
- Shield-SR independent pre-release review CLEAN — 0 CRITICAL / 0 HIGH / 0 MEDIUM. Codex review across 8 rounds (R1 scope + R2-R8 implementation): final R8 verdict VERIFIED-CLEAN.

## [0.10.6] - 2026-05-15

### Added
- **Kilo Code watcher (9th supported tool).** New capture-only watcher for the Kilo Code VS Code extension (`kilocode.Kilo-Code` on the marketplace, on-disk path `globalStorage/kilocode.kilo-code/`). Kilo Code is a fork of Roo Code (which is a fork of Cline) and uses the same per-task UUID storage layout with `api_conversation_history.json` and `cline_messages.json` files, so capture reuses the already-reviewed Cline parsing path. New `KiloCodeWatcher` is a thin subclass of `ClineWatcher` setting `tool="kilo-code"`. Wired into `sfs daemon`, `sfs init` auto-detect, `sfs watcher enable/disable/list`, `sfs mcp install/uninstall --for kilo-code`, `sfs recapture`, and the `sfs resume --in kilo-code` rejection path (capture-only contract matches Cline/Roo).

### Tests
- 1720 → 1727 backend (+7: parsing, UUID task ID, history_item metadata, fallback discovery, manifest tool name, watcher config default, platform-aware default storage path).

### Security
- Shield-SR independent pre-release review CLEAN — 0 CRITICAL / 0 HIGH / 0 MEDIUM. No new attack surface (storage_dir is admin-controlled TOML, same trust boundary as Cline/Roo; tool name added to closed allowlist enums on every routing site).

## [0.10.5] - 2026-05-15

### Added
- **Compile source manifest: `created_by_persona` + `compile_id`.** Each entry in `get_context_section.source_entries` now carries the resolving persona (from the source session's `persona_name`, set by the daemon's active-ticket annotation pipeline) and the parent `ContextCompilation.id`. Agent Runner SoD callers can disqualify by persona/tool/whole-compile cohort, not just by `created_by_user_id`. Persona attribution lookup is one batched SELECT per compile (bounded by distinct session_ids, not entry count); compile_id is denormalized at response time so the compile + manifest stay one atomic write.
- **Tier-aware archive unpack cap.** `sync/archive.py:validate_tar_archive` and `unpack_session` now accept `member_limit_bytes`. CLI `pull` / `sync` / `pull_handoff` thread `MAX_MEMBER_SIZE` (already reads `SFS_MAX_SYNC_MEMBER_BYTES_PAID`) into unpack. The old hardcoded 50 MB silently nullified paid-tier overrides above 50 MB — same class of bug DLP carried before v0.9.9.8. Default fallback is 100 MB, matching the server abuse cap in `_validate_tar_gz`. Self-hosted operators raising the paid-tier cap must set the same env var on every CLI host.
- **`sfs ticket comments <id>` CLI** — read-only client of the existing `GET /api/v1/projects/{id}/tickets/{id}/comments` endpoint. Closes the gap where cross-agent review threads could only be read through the dashboard. Renders each comment in a titled Panel with author + created_at; Markdown body content so code fences render legibly.

### Fixed
- **Cross-project persona leak on `source_entries`.** Persona attribution SELECT now constrains `Session.project_id == project_id AND Session.is_deleted == False`. A project-scoped KB entry pointing at a session from another project (KnowledgeEntry.session_id is plain text, not a project-validated FK) no longer leaks that session's `persona_name` into this project's `source_entries`. Deleted-session attribution degrades to `created_by_persona=null` without error. 2 regression tests added.
- **Nondeterministic `compile_id` in same-timestamp bucket.** `get_context_section` latest-compile lookup now orders by `(compiled_at DESC, id DESC)`. Since `compile_id` is part of the SoD evidence contract, identical-timestamp tiebreaks need to be deterministic. 1 regression test added.
- **Site `devalue` HIGH (GHSA-77vg-94rm-hx3p, CWE-770 DoS, CVSS 7.5).** `site/node_modules/devalue` 5.6.4 → 5.8.1 via `npm audit fix`. Astro stays on 6.3.1. No app code change.

### Tests
- 1711 → 1720 backend (+9: archive tier-aware regressions, cross-project persona leak regressions, same-timestamp compile tiebreak regression). 186 dashboard unchanged.

### Security
- Shield-SR independent pre-release review: 0 CRITICAL / 0 HIGH / 0 MEDIUM after `devalue` fix. Codex R2 on `tk_12e6d8775eb045a2` (compile source manifest): no findings. Codex review on archive tier-aware change (KB 396): no blocking issues, one informational note on env-parity for self-hosted operators (documented in `docs/environment-variables.md`).

## [0.10.4] - 2026-05-15

### Added
- **Migration 039 — ticket lease epochs + context source manifest + retrieval audit tables.** Adds `tickets.lease_epoch` (NOT NULL DEFAULT 0), `context_compilations.source_manifest` (TEXT NOT NULL DEFAULT '{}'), `sessions.retrieval_audit_id` (nullable, indexed), and two new tables: `retrieval_audit_contexts` (one row per ticket start, links to project + ticket + persona + lease_epoch + created_by_user) and `retrieval_audit_events` (events per context, with serialized arguments/returned_refs, source flag, caller_user_id).
- **Ticket lease fencing.** `start_ticket` now atomically increments `lease_epoch` via `UPDATE ... WHERE status IN (...) SET lease_epoch = lease_epoch + 1`. `complete_ticket`, `add_ticket_comment`, and `accept_ticket` accept optional `lease_epoch` in the request body and use it as an inline WHERE-clause predicate. Stale daemons get 409 with a clear current-vs-supplied message. Coordinated audit, not strict mutex — opt-in semantics documented on the MCP tool descriptions.
- **Compile source manifest.** `compile_project_context` snapshots the active KB claims feeding each section before LLM compilation and stores them on the compilation row. `get_context_section` returns `source_entries` so SoD/audit callers can trace rendered prose back to the claims that shaped it.
- **Retrieval audit log (server primary + local fallback).** Server: 4 new routes — `POST /api/v1/projects/{id}/retrieval-audit-contexts`, `POST /api/v1/projects/{id}/retrieval-audit-events`, `GET /api/v1/retrieval-audit-contexts/{ctx}/events`, `GET /api/v1/sessions/{id}/retrieval-log`. Local fallback: `~/.sessionfs/retrieval_logs/<id>.jsonl` for offline MCP clients. MCP records context-shaping retrievals (search_project_knowledge, get_wiki_page, get_persona, get_compiled_rules, get_context_section, find_related_sessions, get_session_context) when an active ticket bundle has `retrieval_audit_id`; sessions persist the id from `active_ticket` manifests so the audit chain survives capture.
- **MCP `get_session_retrieval_log` tool** (44 total). Server-success path and local-fallback path return identical top-level keys `{session_id, retrieval_audit_id, events, count}`; local rows are lifted into the server's `RetrievalAuditEventResponse` shape with `source="local"` so consumers parse one schema.
- **`sfs persona pull [<name>|--all]` CLI** — pulls server-side personas to local `.agents/*.md` files. Preserves the existing kebab-naming convention on re-pull (`<name>-<role-fragment>.md`); HTML-comment preamble only (no double H1 against the persona's own identity header). Release-only personas (`shield-security-review.md`, `scribe-site-sync.md`) untouched — no server equivalent.
- **Site messaging rewrite for v1.0 positioning.** Landing / features / enterprise / pricing reframed around Memory / Identity / Coordination / Governance pillars. "Your team today. AI agents tomorrow." hybrid-operator section on enterprise. Cloud-agent-ready strip (Bedrock, Vertex, custom API) phrased precisely as "via integration docs" — not a hosted gateway.

### Fixed
- **Site `/dlp/` 404 + stale secret-pattern count.** Landing DLP card was linking to a non-existent route; repointed to `/enterprise/` where the Compliance Built In pillar describes DLP. Secret-pattern count corrected from stale 22 → 19 to match `sessionfs.security.secrets.SECRET_PATTERNS`. Historical changelog entries untouched.
- **Retrieval-audit defenses (10 sentinel findings + 2 Codex follow-up findings closed).** Path traversal: `SAFE_AUDIT_ID_RE` regex validates audit/session IDs at every entry point (audit_session_id, audit_context_id, record_retrieval, read_retrieval_log, MCP `get_session_retrieval_log`). Session upload validates claimed `retrieval_audit_id` exists in `retrieval_audit_contexts` for the same project AND was created by the uploading user — silently drops the field with a warning otherwise. Context create validates supplied `ticket_id` and `persona_name` belong to the same project; rejects forged provenance with 422. Event creator must own the context (`_assert_context_owner`). Audit event payloads capped at 16 KiB with `_truncated` marker. Cross-project SELECT leak closed via `Project.id IN (accessible_projects_subquery)` filter in initial WHERE. Comment lease check is now atomic (`INSERT … SELECT WHERE lease_epoch = ?`). Local JSONL caps at 10 MiB. `sanitize_arguments` strips any key containing api_key/token/secret/password/auth/credential. `collect_returned_refs` walker has 50-level depth limit and no longer regex-extracts `KB N` / `Entry N` from arbitrary string fields.

### Tests
- 1626 → 1711 backend (+85: retrieval-audit API regression suite, ticket lease tests, compile source-manifest assertions, sentinel hardening regressions). 186 dashboard unchanged.

### Security
- Shield-SR independent pre-release review: 0 CRITICAL / 0 HIGH / 0 MEDIUM. One LOW noted: `_get_project_or_404` vs `_accessible_project_ids_subquery` give different visibility for context-create POST vs context-events GET (GET stricter). Direction is defensive, not permissive. Tracked as Atlas follow-up; not release-blocking.

## [0.10.3] - 2026-05-14

### Added
- **Dashboard UI: Personas, Tickets, AgentRuns tabs.** Three new ProjectDetail tabs surface the v0.10.1 personas + tickets and v0.10.2 AgentRun backends. Read-focused MVP — the moderation transitions a human reviewer wants ship; the FSM transitions tied to the local active-ticket bundle stay in CLI/MCP. Reviewed across 2 rounds of Codex review on ticket `tk_530dfba7f14446dd`; full receipts there.
  - **Personas tab** — list with role + specializations + last-updated; create modal with name-immutable + ASCII regex pattern + markdown content; edit modal; delete with `--force` toggle for the server's non-terminal-ticket guard.
  - **Tickets tab** — list filtered by status (all 7 FSM states + All); expand-in-place detail panel (description, acceptance criteria as ☐ boxes, dependency list, completion notes, comments); approve (suggested→open) + dismiss (suggested/open→cancelled) buttons gated by current status; inline comment composer; new-ticket modal.
  - **AgentRuns tab** — audit-trail read-only view (CI-driven on the backend); status + persona + trigger filters; 30s refetchInterval; expand to view tool / ticket / trigger ref / fail-on / duration / structured findings JSON. Clickable CI run URL goes through a `safeHttpUrl()` allowlist (http/https only) so a crafted `javascript:` or `data:` URL falls back to plain text.
- **API client + react-query hooks** for all three surfaces. 14 new methods on `ApiClient` (list/get/create/update/delete personas; list/get/create/approve/dismiss/comment tickets; list/get agent-runs). New typed interfaces mirror the server Pydantic responses field-for-field.

### Fixed
- **CLI `sfs agent complete --findings-file` now rejects non-object list elements locally.** The API model is `list[dict[str, Any]]` and rejects `[1]` or `["bad"]` with a 422 that leaves the run stuck in `running` for non-CI users. The CLI now enumerates elements and reports the index + type of the first non-object before the HTTP call.
- **`handle_errors` decorator preserves `typer.Exit(N)` exit codes.** Latent bug: `typer.Exit` is a `RuntimeError`, not a `SystemExit`, so the generic `except Exception` was silently downgrading every `typer.Exit(N)` (N != 0) to `SystemExit(1)` with `"Unexpected error: N"`. Affected 7 sites across cmd_agent / cmd_persona / cmd_config / cmd_project. The decorator now catches `click.exceptions.Exit` explicitly and re-raises as `SystemExit(exit_code)`.
- **Mypy union-attr error in `sfs agent run` timeout polling.** `r3.get('status')` was called inside an `'r3' in dir()` guard, but mypy correctly rejected `.get()` on the union return type. Replaced with `isinstance(r3, dict)` narrowing + pre-initialised `r3: dict[str, Any] | list[Any] | str = {}`. CI hotfix already on main; bundled here for the release commit.
- **Dashboard `addToast` signature.** `PersonasTab` + `TicketsTab` had 10 call sites using `addToast({ kind, message })`; the hook exports `addToast(type, message)`. `npm run build` was failing TS2554 at every site even though the vitest mocks were permissive enough to mask it. All 10 sites rewritten; test mocks now assert against the positional shape.
- **Dashboard `ci_run_url` rendered without scheme guard.** Operator-supplied URL was rendered as `<a href={url}>` with `rel="noreferrer"`. Now goes through `safeHttpUrl(raw)` which parses via `new URL()` and only allows `http:` / `https:`; everything else (`javascript:`, `data:`, malformed) falls back to plain text. `rel` upgraded to `"noopener noreferrer"`.
- **DeleteConfirmModal Esc close.** The persona-delete modal trapped focus via `useFocusTrap` but missed the `keydown` Esc handler that the create/edit and new-ticket modals already used. All three modals now share the same close-on-Escape behaviour.

### Tests
- +2 backend (1686 → 1688): findings-file non-object rejection + `handle_errors` Exit-preservation regression.
- +21 dashboard (165 → 186): three new tab test files covering happy + filter + expand + action-gating + toast surfacing + URL safety + Esc close.

### Security
- Shield-SR independent pre-release review CLEAN — zero CRITICAL/HIGH findings, zero pip-audit / npm audit vulnerabilities (104 Python + dashboard deps). Bandit clean (no new findings; pre-existing MEDIUMs in unchanged files). `dangerouslySetInnerHTML`: 0 occurrences in new dashboard code. Codex R1 HIGH on `ci_run_url` confirmed closed.

## [0.10.2] - 2026-05-14

### Added
- **AgentRun + CI Integration.** New layer on top of v0.10.1 personas + tickets: an AgentRun is one auditable execution of one persona, optionally against one ticket, with trigger metadata, severity, findings, and CI-friendly policy enforcement. Tracking + enforcement, not model auto-spawning — the CI script picks the LLM, SessionFS records the result. Cross-agent reviewed across 10 rounds (Codex R1–R10); full receipts on ticket `tk_f5381e113f144be5`.
- **Migration 038** — `agent_runs` table. Project-scoped FK; `persona_name` / `ticket_id` / `session_id` intentionally plain strings (no FK) for audit-row survival across persona-renames and session-deletes. Columns: status, severity, trigger_source/ref, ci_provider/run_url, findings JSON-as-text (`NOT NULL DEFAULT '[]'`), fail_on threshold, policy_result, exit_code, duration_seconds, started_at/completed_at. Five indexes: `idx_agent_run_project_status`, `idx_agent_run_ticket`, `idx_agent_run_project_persona`, `idx_agent_run_project_trigger`, `idx_agent_run_project_created`.
- **AgentRun REST API** — `POST/GET/POST start/POST complete/POST cancel` at `/api/v1/projects/{id}/agent-runs`. Atomic `UPDATE ... WHERE status IN (...)` rowcount-1 guards on every transition. Same-project ticket validation (cross-project ticket-id rejected with 422). Errored/failed final statuses force `exit_code=1` so `--enforce` always fails CI on crash. **Tier: Team+ (`agent_runs`).** Router registered before `projects.router` catch-all.
- **CLI `sfs agent`** — 4 subcommands: `run` (create + start + emit compiled context to `--context-file`), `complete` (record findings + severity + policy result with `--enforce` for CI exit-code propagation), `status` (text / json / markdown formats; markdown is GitHub/GitLab step-summary compatible), `list` (filter by persona/status/trigger_source/limit). Machine-safe `--output-id` flag prints only the run id; polling status output is routed to stderr in output-id mode. Defense-in-depth: `--enforce` exits non-zero for any `failed`/`errored` status even when stored `exit_code` is 0.
- **`sfs ticket create --output-id`** — companion machine-safe flag for CI scripting. Stdout is exactly the ticket id; the human "Created ticket …" confirmation goes to stderr.
- **`approve_ticket` MCP tool** — mirrors the `sfs ticket approve` CLI; moves a `suggested` ticket → `open`. 409 surfaces as a readable status-conflict error so agents can recover.
- **Session-ops MCP tools** — three new local-only tools that operate on `~/.sessionfs`: `checkpoint_session` (named snapshot of manifest + messages, regex-validated name `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$` blocks path traversal), `list_checkpoints` (oldest first, returns name + created_at + message_count + on-disk path), `fork_session` (forks the live head or a named checkpoint into a new session with `parent_session_id` + optional `forked_from_checkpoint` lineage in the manifest). Shared pure helpers in `src/sessionfs/session_ops.py`; CLI `sfs checkpoint` / `sfs fork` refactored to call the same helpers (DRY).
- **MCP tools (7 new)** — `create_agent_run`, `complete_agent_run`, `list_agent_runs`, `approve_ticket`, `checkpoint_session`, `list_checkpoints`, `fork_session`. Total MCP tool count: 36 → 43.
- **Cloud Agent Control Plane** — Bedrock + Vertex AI integration. `docs/integrations/bedrock-action-group.yaml` + `docs/integrations/bedrock_lambda.py` (Bedrock action-group dispatcher with closed `OPERATIONS` table + `parse.quote(safe="")` on path params blocking traversal); `docs/integrations/vertex_tools.py` (function-calling schema + dispatcher). Public docs at `site/src/content/docs/integrations/cloud-agents.mdx`.
- **CI Integration docs page** — `site/src/content/docs/integrations/ci-integration.mdx` with policy matrix, crash-safety patterns, and the PR-injection hardening playbook (token scoping, `${{ }}` template-substitution avoidance, `persist-credentials: false`, separate `comment-on-pr` job) developed across the 10-round Codex review.
- **GitHub Actions + GitLab CI example workflows** — `docs/integrations/github-actions-agent-run.yml` + `docs/integrations/gitlab-agent-run.yml`. Defense layers: per-step `SESSIONFS_API_KEY` env scoping (off the PR-controlled review step), `persist-credentials: false` on `actions/checkout`, `permissions: contents: read` only, PR title/body via `env:` (never `${{ }}` template), `jq -e 'type == "array" and all(.[]; type == "object")'` findings-shape guard routing crash / missing / malformed / non-object-element to the `errored` complete path so runs never stick in `running`, workspace-relative `.sessionfs/` paths so `hashFiles()` works AND artifacts upload, commented-out separate `comment-on-pr` job demonstrating the correct GitHub-Actions pattern (workflow- or job-level `permissions:`, never step-level).
- **Env-var auth for CI** — `_get_api_config` now honors `SESSIONFS_API_KEY` / `SESSIONFS_API_URL` env vars before falling back to `~/.sessionfs` sync config, so a fresh CI runner with only the documented secret authenticates without `sfs auth login` first.
- **Manifest schema additions** — `persona_name`, `ticket_id`, `instruction_provenance`, `_resume_parent_id` properties added to `src/sessionfs/spec/schemas/manifest.schema.json`. Fixes a Phase-6 bug where active-ticket annotation collided with `additionalProperties: false`.
- **Tool-aware token budgets** — `_compile_persona_context` now recognises `bedrock` (16000 tokens) and `vertex` (8000 tokens) alongside the existing tool aliases.

### Tests
- +60 tests over v0.10.1 (1626 → 1686 backend tests). 27 AgentRun coverage (lifecycle, policy matrix, atomic concurrent-start race, cross-project guards, tier gate, errored/failed exit-code forcing); 9 `sfs agent` CLI tests (`--format markdown` step-summary, `--format json`, `--output-id` polling stream-routing, env-var auth precedence, `--enforce` defense-in-depth); 14 MCP coverage for the new tools (approve dispatch + 409 path, checkpoint/list/fork happy paths, name regex rejection, duplicate-name rejection, missing-session, missing-checkpoint, prefix resolution); plus Cloud Agent integration smoke tests.

### Security
- Independent Shield-SR pre-release review: zero CRITICAL/HIGH findings, zero pip-audit / npm audit vulnerabilities (104 Python + 390 npm deps), zero bandit HIGH, no hardcoded secrets. v0.10.2 surface follows v0.10.1 patterns: project-scoped FKs, atomic-UPDATE FSM guards, tier-gated routes, project-scoped lookups failing closed to 404. The CI YAML examples carry the hardening from 10 rounds of Codex review.

## [0.10.1] - 2026-05-13

### Added
- **Agent Personas + Ticketing System.** v0.10.1 is the first SessionFS release with first-class persona + ticket management. Personas are portable AI roles per project; tickets are self-contained units of work with a server-enforced FSM, dependency graph, comments, and an active-ticket provenance bundle that tags every captured session with `persona_name` + `ticket_id`. Built across 6 phases under the cross-agent review pattern; full receipts under KB `entity_ref=agent-personas-tickets-v0.10.1-phase-{1..6}`.
- **Migration 037** — 4 new tables: `agent_personas` (project-scoped, ASCII-name UNIQUE, soft-delete via `is_active`), `tickets` (FSM: suggested → open → in_progress → blocked → review → done | cancelled; reporter provenance split into `created_by_user_id`/`session_id`/`persona`; JSON-as-text columns for context_refs/file_refs/related_sessions/acceptance_criteria/changed_files/knowledge_entry_ids, all `NOT NULL DEFAULT '[]'`), `ticket_dependencies` (composite-PK edge table with `idx_ticket_deps_depends_on` for reverse lookup), `ticket_comments` (slack-like, non-idempotent). Also two new columns on `sessions`: `persona_name VARCHAR(50)` and `ticket_id VARCHAR(64)` with `idx_sessions_ticket_id`.
- **Persona CRUD API** — `GET/POST /api/v1/projects/{id}/personas`, `GET/PUT/DELETE /api/v1/projects/{id}/personas/{name}`. ASCII regex `^[A-Za-z0-9_-]{1,50}$` (no Unicode leak). Pre-check duplicate before insert with narrow IntegrityError catch on `uq_persona_project_name`. **Tier: Pro+ (`agent_personas`).**
- **Persona-delete guard** — `DELETE /api/v1/projects/{id}/personas/{name}` refuses 409 when non-terminal tickets (suggested/open/in_progress/blocked/review) reference the persona. `?force=true` overrides; terminal `done`/`cancelled` tickets never block. Protects MCP, CLI, and any future client equally.
- **Ticket CRUD + lifecycle API** — 13 routes under `/api/v1/projects/{id}/tickets`. Atomic `UPDATE ... WHERE status='X'` rowcount-1 guards on `start_ticket` and `accept` (no double-accept duplication). Cross-project dependency validation in `_validate_dependencies_same_project` + JOIN-filtered `_enrich_dependents` (belt + suspenders against legacy bad edges). Dependency cycle detection via BFS. `list(dict.fromkeys(...))` dedup on `depends_on` before validation/cycle-check/insert. Agent-created tickets (source='agent') require ≥1 acceptance criterion + ≥20-char description, max 3 per session_id. **Tier: Team+ (`agent_tickets`).**
- **Ticket comments API** — `GET/POST /api/v1/projects/{id}/tickets/{ticket_id}/comments`. Slack-like non-idempotent (each call creates a row). Project-gated via `_get_project_or_404` + `_get_ticket_or_404`. Inherit `agent_tickets` tier gate.
- **Compiled persona + ticket context** — `start_ticket` now returns `{ticket, compiled_context}`. Markdown assembled from persona content + ticket section (description, criteria as checkboxes, file refs, KB claims, completed-dep notes) + recent comments. Tool-aware truncation via `?tool=` query param (`claude-code`=16k, `codex/gemini/copilot/amp/generic`=8k, `cursor/windsurf/cline/roo-code`=4k tokens; char budget = tokens × 4 with explicit `[...truncated to fit tool token limit]` marker).
- **Cross-project leak defense** — `_compile_persona_context` filters every data-exposure SELECT by `project_id`: KB claims filtered by `KnowledgeEntry.project_id == ticket.project_id` AND `claim_class='claim'` AND `superseded_by IS NULL` AND `dismissed=False` AND `freshness_class IN (current, aging)`; dependency completion notes filtered by `Ticket.project_id == ticket.project_id` AND `status='done'`. `context_refs` cast to int with non-numeric drops (prevents driver-dependent coercion).
- **MCP tools (8 new)** — `list_personas`, `get_persona`, `list_tickets`, `get_ticket`, `start_ticket` (writes local provenance bundle), `create_ticket`, `complete_ticket` (clears owned bundle, attaches `provenance_warning` on write failure), `add_ticket_comment`. Total MCP tool count: 22 → 30.
- **Shared active-ticket bundle** — `src/sessionfs/active_ticket.py` exposes `bundle_path()`, `read_bundle()`, `write_bundle()` (returns bool, surfaces `provenance_warning` to MCP and yellow warning to CLI on OSError), `clear_bundle_if_owned()` (reads-before-unlink; only removes when both `ticket_id` AND `project_id` match the completing ticket — never deletes another tool's bundle).
- **CLI `sfs persona`** — 5 commands: `list`, `show` (`--raw` to skip markdown render), `create` (`--content` / `--file` / `$EDITOR`), `edit` (opens content in `$EDITOR`), `delete` (`--force` flag passes through to server guard; on 409 surfaces server message via `body.get("detail") or body.get("error", {}).get("message")` envelope-fallback).
- **CLI `sfs ticket`** — 12 commands: `list`, `show`, `create`, `start` (writes bundle, prints compiled context, `--tool` / `--force` / `--no-print-context`), `complete` (clears bundle iff owned), `comment` (`--as PERSONA`), `status` (Rich panel from bundle), `block`, `unblock`, `reopen`, `approve`, `dismiss`.
- **Daemon integration** — all 7 watchers (claude_code, codex, copilot, cursor, gemini, amp, cline) call `annotate_manifest_with_active_ticket(session_dir)` after capture, which reads the local bundle and adds `ticket_id` + `persona_name` to the session's `manifest.json`. The server-side `_extract_manifest_metadata` sanitizes (str.strip, 50/64-char truncation matching column widths) and the 4 Session-write sites populate the columns. Manifest is the source of truth, so re-syncing the same `.sfs` from a different machine preserves the original tagging.
- **Router precedence** — `personas.router` and `tickets.router` registered BEFORE `projects.router` in `app.py` so their `/{project_id}/(personas|tickets)/...` paths beat the catch-all `/{git_remote_normalized:path}` (same trick used by rules + project_transfers).

### Tests
- +124 tests over v0.10.0 (1502 → 1626 backend tests). 11 schema canaries (incl. AST-based migration source nullability check), 24 persona-CRUD tests, 28 ticket FSM + dependency + atomicity tests, 13 ticket comments + compiled context + cross-project leak regressions, 14 bundle module tests, 9 CLI smoke + error-envelope tests, 7 watcher annotation tests, 4 session-upload provenance roundtrip tests, plus updated MCP server tool-count + dispatcher tests for Phase 8 agent-workflow tools (16 new tests including +2 Round 2 regressions for forget_persona ticket-bundle refusal and escalate audit-comment warnings).

### Cross-agent review history
- **Phase 1** (schema): 3 rounds. MEDIUM nullable=False missing on 6 JSON columns; LOW missing reverse-lookup index; LOW nullability canary only ORM-shaped (added AST-based migration source assertion).
- **Phase 2** (persona CRUD): 2 rounds. LOW Unicode `str.isalnum()` leak (replaced with explicit ASCII regex); LOW broad `IntegrityError` masked FK failures (pre-check + narrow constraint-name catch).
- **Phase 3** (ticket CRUD + FSM): 3 rounds. MEDIUM cross-project dep validation; MEDIUM non-atomic accept (UPDATE...WHERE status='review' rowcount guard); LOW duplicate-deps composite-PK collision (dedup with `list(dict.fromkeys(...))`).
- **Phase 4** (comments + compiled context + MCP): 3 rounds. HIGH cross-project KB claim leak in compiled_context (project_id + dismissed + freshness filter); LOW provenance bundle ownership check (read-before-unlink); MEDIUM cross-project dep completion_notes leak (same project_id filter on done_deps).
- **Phase 5** (CLI + shared bundle): 3 rounds. MEDIUM persona-delete strands tickets (server-side guard + `?force=true`); LOW bundle write failures swallowed (bool return + CLI yellow warning + MCP `provenance_warning`); LOW CLI 409 envelope mismatch (read both `body["detail"]` and `body["error"]["message"]`).
- **Phase 6** (daemon integration): bundle propagation across all 7 watchers + manifest extractor + 4 server write-sites + sanitization at column widths.
- **Phase 7** (docs): docs/CLI-reference + README features + CHANGELOG narrative.
- **Phase 8** (agent workflow tools): 6 new MCP tools (`create_persona`, `assign_persona`, `assume_persona`, `forget_persona`, `resolve_ticket`, `escalate_ticket`) at customer request. Total MCP tool count 30 → 36. New `clear_bundle()` helper and persona-only bundles (`ticket_id=null` + `persona_name` set) so the daemon tags ad-hoc agent work that isn't tied to a ticket. CLI additions: `sfs persona assume|forget`, `sfs ticket assign|resolve|escalate`.

## [0.10.0] - 2026-05-13

### Added
- **Org Admin Console.** v0.10.0 is the first SessionFS release with full org-level administration. Org admins manage members, transfer projects between scopes, edit org defaults, and link captured sessions to org-scoped projects from a single Organization page and a parallel CLI surface. Built across 7 phases over ~5 hours of cross-agent review; full receipts under KB entry_ref `org-admin-console-v0.10.0-phase-{1..7}`.
- **Migration 035** — `projects.org_id` (nullable FK → organizations.id, ON DELETE SET NULL) gives projects an explicit org scope. `users.default_org_id` (same shape) stores each user's preferred org for multi-org routing. New `project_transfers` table provides a durable audit + state machine for cross-scope project moves. ON DELETE SET NULL on `project_id` so an audit row survives the project being hard-deleted; the `project_git_remote_snapshot` column (stable identity — git_remote_normalized is unique server-wide) keeps historical transfers identifiable.
- **Migration 036** — `sessions.project_id` (nullable FK → projects.id, ON DELETE SET NULL, indexed). Server resolves the linkage on every sync from the workspace's git remote so the org-scope of any captured session is recoverable.
- **Project transfer API.** `POST /api/v1/projects/{id}/transfer` initiates, `POST /api/v1/transfers/{xfer_id}/{accept,reject,cancel}` mutates state. Atomic `UPDATE ... WHERE state='pending'` with rowcount check prevents double-accept. Partial-unique index `idx_project_transfers_pending_unique` is the DB-level backstop for concurrent-initiate races. `GET /api/v1/transfers?direction=&state=` for inbox listing. Auto-accept when initiator == target (personal → own org). Standing is re-validated on accept/reject — a member demoted between initiate and accept loses the right to act on the transfer.
- **Multi-org member management API.** `GET /api/v1/orgs` lists the caller's memberships with role. `GET /api/v1/orgs/{org_id}/members` lists, `POST /api/v1/orgs/{org_id}/members/invite` invites, `PUT /api/v1/orgs/{org_id}/members/{user_id}/role` promotes/demotes, `DELETE /api/v1/orgs/{org_id}/members/{user_id}` removes. Removal preserves all member-authored data (CEO-mandated "data stays, access revoked" invariant) — sessions stay user-owned, org-scoped projects auto-transfer to the removing admin with a ProjectTransfer audit row, KB entries stay with authorship preserved, default_org_id cleared if it pointed at this org, and pending transfers tied to the removed user's standing here are cancelled. `SELECT ... FOR UPDATE` row-locks the admin rows before any last-admin guard so concurrent cross-demotion/cross-removal can't leave the org with zero admins.
- **Org settings API.** `GET/PUT /api/v1/orgs/{org_id}/settings` for the three KB creation defaults (`kb_retention_days`, `kb_max_context_words`, `kb_section_page_limit`). Stored in `Organization.settings` JSON under the `general` key (parallel to the existing DLP block under `dlp`). Admin-only PUT with range validation; any member can GET. The DLP block survives a general-settings PUT via structural merge. New org-scoped projects inherit the three kb_* defaults from the org at project-create time.
- **Default-org API.** `GET /api/v1/auth/me` now includes `default_org_id`. `PUT /api/v1/auth/me/default-org` sets it (membership validated; non-member 403); passing null clears.
- **Session project resolution.** Both upload surfaces (`POST /api/v1/sessions` and `PUT /api/v1/sessions/{id}/sync`) now resolve `session.project_id` from the workspace git remote via a shared `_resolve_project_id_for_session` helper. Resolution runs inside the write transaction with `SELECT ... FOR UPDATE` on both the Project and OrgMember rows so concurrent project deletes / membership removals can't leave a session row pointing at a project the user no longer has access to. Re-sync re-evaluates: pre-Phase-5 sessions retroactively pick up project_id when the project is created later; stale linkages clear when access is revoked.
- **Dashboard surfaces.** New `MembersTab` (org members management with CEO data-stays modal copy), `TransferInbox` (`/transfers` route, lists incoming + outgoing pending with Accept/Reject/Cancel), `TransferPanel` (per-project Transfer tab on the Project page with destination dropdown and pending-cancel branch), `OrgSettingsTab` (KB creation defaults form mounted on the Organization page). New nav link `/transfers` with a pending-incoming badge mirroring the Handoffs badge.
- **CLI.** `sfs project init --org <id>` and `--personal` for explicit project scope. `sfs project transfer --to|--accept|--reject|--cancel <id>` and `sfs project transfers --direction --state` for transfer ops. `sfs config default-org [<id>|--clear]` for default-org preference (server-canonical).

### Changed
- **`/api/v1/auth/me` response** now includes `default_org_id` (nullable string). All other fields unchanged.
- **`POST /api/v1/projects/`** request body gains optional `org_id`. Caller must be a member; non-members get 403. Personal scope (omitted) preserves the pre-v0.10.0 behavior.
- **`/api/v1/org/members/...` (legacy single-org)** routes delegate to the same `perform_member_removal` / `perform_role_change` services as the new multi-org routes, so both URL surfaces enforce the same data-stays invariants.

### Notes
- 1502 backend tests + 165 dashboard tests passing (was 1384 + 117 at v0.9.9.12). +118 backend (project transfers, org members, default-org routing, org general settings, creation-time inheritance). +48 dashboard (MembersTab, TransferInbox, TransferPanel, OrgSettingsTab, useTransfers hook-level cache invalidation).
- 5 new MCP tools? No — MCP unchanged. v0.10.0 is API + dashboard + CLI surface only.
- Migrations 035 and 036 both apply cleanly on a fresh PG and on upgrade from migration 034 with existing session data. Downgrade present and reverse-tested. All new columns are nullable; zero-downtime DDL.
- Codex review chain: 7 phases of cross-agent review across 30+ rounds. Each round's findings, fixes, and verification logged in the KB under `entity_ref=org-admin-console-v0.10.0-phase-{1..7}`.

## [0.9.9.12] - 2026-05-12

### Fixed
- **Daemon-reindex data loss when the SQLite index self-heals against any malformed manifest.** A user-reported symptom — "7-8 Codex sessions disappeared from `sfs list` after daemon restart following an Index-was-corrupted recovery" — traced to two distinct bugs in the rebuild loop:
  1. `src/sessionfs/store/index.py:upsert_session` used `manifest.get("source", {})` (and the same shape for `model` / `stats` / `tags`), which returns `None` (not the default) when the manifest has `"source": null`. The next `source.get(...)` raised `AttributeError`. `created_at` and `source.tool` (both NOT NULL columns) hit `sqlite3.IntegrityError` on the same kind of null-or-wrong-type input. Replaced with `_as_dict` / `_as_list` helpers that use `isinstance` checks — so any falsy OR truthy non-matching type (e.g. `"source": "codex"`, `"source": ["codex"]`, `"tags": "not-a-list"`) is normalized to an empty container instead of crashing.
  2. `_rebuild_index_from_disk` only caught `(json.JSONDecodeError, OSError)`, so an `AttributeError` from bug #1 — or any `sqlite3.IntegrityError` / `TypeError` — aborted the entire reindex loop. Every session sorted alphabetically AFTER the bad one was silently dropped. Broadened to `except Exception` with WARNING-level skip logging and a "N indexed, K skipped" summary line.
- **`sqlite3.IntegrityError` misinterpreted as index corruption.** `upsert_session_metadata`'s `except sqlite3.DatabaseError` branch caught IntegrityError (parent class) and ran the destructive recreate-and-retry recovery path per bad session — wasteful + noisy. Now catches `sqlite3.IntegrityError` first and re-raises so the per-session skip in the outer loop handles it cleanly. Same fix applied to `upsert_tracked_session`.
- **`sfs daemon rebuild-index` CLI command had its own copy of the same two bugs.** `cli/cmd_daemon.py:rebuild_index` duplicated the reindex loop with the same null-unsafe defaults and the same too-narrow except. Applied the matching `isinstance` guards and broadened the per-session except. Backfill block now repairs `"source": null` / `"source": "codex"` shapes in-place when a `tracked_sessions` row exists for the ID. Cross-reference comment added so future maintainers audit both loops together; the DRY refactor is queued for a v0.10.x cleanup.

### Notes
- No new database migrations (still 034). No new MCP tools.
- Helm chart version 0.9.14 → 0.9.15 (chart evolves independent of app). appVersion 0.9.9.12.
- 1384 backend tests + 117 dashboard tests passing (was 1376 + 117 at v0.9.9.11; +8 backend from new regression suite in `tests/unit/test_resilience.py`). Four Codex review rounds (KB entries 222 → 224 → 226 → 228 → 229 CLEAN) resolved before tag.

## [0.9.9.11] - 2026-05-12

### Fixed
- **Dashboard freeze when editing project rules → knowledge / context max tokens.** Both `<input type="number">` fields fired `patchRules()` on every keystroke, so typing `8000` issued four sequential `PUT /api/v1/projects/{id}/rules` calls — the first succeeded with a new ETag, the next three 409'd against the stale one, and the cascade of "refresh and try again" toasts plus react-query refetch storm rendered as a freeze. `RulesTab.tsx` now routes both inputs through a new `DebouncedTokenInput` helper: 600 ms local-draft debounce, flush-on-blur, flush-on-unmount (via empty-dep `useEffect` cleanup + `onCommitRef`), and a `flush(): Promise<boolean>` handle exposed via `forwardRef` + `useImperativeHandle` so the Compile button can drain pending edits before running. `patchRules` switched to `mutateAsync` and returns `Promise<boolean>` (never rejects — signals failure in-band so the existing 8 fire-and-forget checkbox callers don't generate unhandled-rejection warnings). `handleCompile` is now `async`, awaits both flushes, and short-circuits with a "Compile skipped — pending rules update failed" toast when any flush returned `false`. Compile button additionally disabled while `updateRules.isPending` as defense-in-depth against HTTP/2 multiplexing race. Six new regression tests in `RulesTab.test.tsx` cover: keystroke coalescing per pause, independent debounce per input, blur-flush, unmount-flush, compile-drains-pending (deferred-promise pattern proves compile is held until the patch promise settles), compile-skipped-on-409, compile-skipped-on-network/500.

### Notes
- No new database migrations (still 034). No server-side code changes.
- Helm chart version 0.9.13 → 0.9.14 (chart evolves independent of app). appVersion 0.9.9.11.
- 1376 backend tests + 117 dashboard tests passing (was 109 dashboard in v0.9.9.10; +8 from the RulesTab regression suite). Four Codex review rounds (entries 214 → 216 → 218 → 220 CLEAN) resolved before tag.

## [0.9.9.10] - 2026-05-12

### Added
- **`pg_trgm` GIN index on `knowledge_entries.content`** (migration 034). PostgreSQL only; SQLite is a no-op. Accelerates `ILIKE '%query%'` substring search used by `GET /api/v1/projects/{id}/entries?search=` and MCP `search_project_knowledge`. The route and the MCP wrapper now both enforce a 3-character minimum on `search` so every accepted query benefits from the trigram index — 1-2 char queries previously fell back to a sequential scan.
- **Shared ANSI-strip test helper** (`tests/utils/ansi.py`) with `strip_ansi()`, `assert_in_ansi()`, `assert_not_in_ansi()`. Replaces four inline `re.sub(r"\x1b\[...", ...)` sites in `test_autosync.py` and `test_hooks_installer.py` that previously protected those tests from the v0.9.9.8 CI-color flake class. New helper has 10 unit tests in `tests/unit/test_ansi_helper.py`; default case-insensitive matching is opt-out for tests where casing is part of the contract.
- **`confirm_or_exit()` CLI helper** (`src/sessionfs/cli/common.py`) — single source of truth for interactive confirmation prompts. Adds a non-TTY guard before `typer.confirm` so piped EOF returns a structured "stdin not a tty; pass --yes" exit instead of leaking an "Unexpected error" through `handle_errors`. Wired into the DLP scan-on-push prompt and the push-after-delete prompt in `cli/cmd_cloud.py`.

### Changed
- **`/api/v1/sync/status` collapsed from 5 queries to 2.** The watchlist counts (`watched`/`queued`/`failed`) now come from a single `GROUP BY status` SELECT against `tracked_sessions`; `watched` is the row sum across statuses, with `queued` and `failed` projected from the grouped result. The aggregate session counters remain on a single multi-aggregate SELECT. Behavior unchanged.
- **Admin `/api/v1/admin/orgs` no longer N+1s.** Member counts for the paginated org list are now loaded via a single `WHERE org_id IN (...) GROUP BY org_id` SELECT against `org_members`, then merged into the response. Empty-page guard prevents `IN ()`.
- **`sfs project ask` keyword extractor** rebuilt around `_extract_search_keywords()` in `cli/cmd_project.py`. The helper strips trailing punctuation, lowercases, drops stop words, enforces a 3-character minimum (mirrors the server gate), dedupes, and caps at 5 keywords. Previous behavior tokenized with `len(w) > 1` and would 422-abort on common 2-char tokens like `db`, `ai`, `ui` once the server-side floor landed. 8 regression tests in `tests/unit/test_ask_keywords.py` including a canary that fails if the CLI floor and server gate drift apart.
- **`/release` skill step 12 (post-deploy verification)** hardened against the v0.9.9.x detection-blind-spot class. Cache-busted probes (`?nocache=$(date +%s)`); strict version match via `grep -F "v${VERSION}"` against the changelog endpoint; `vercel inspect <live-alias>` checks the deployment that actually serves traffic; output captured to a variable before `grep` so `set -o pipefail` doesn't mask `inspect` failures.

### Fixed
- **`urllib3` bumped past CVE-2026-44431 / CVE-2026-44432** (fix in 2.7.0). Transitive dependency — no direct pin in `pyproject.toml`.
- **`click.exceptions.Abort` backstop in `handle_errors`** (`cli/common.py`). `Abort` does not inherit from `ClickException`; without an explicit catch it was bubbling up as "Unexpected error: aborted." for any cancelled `typer.confirm`.

### Notes
- Migration 034 added: `idx_ke_content_trgm` (`CREATE EXTENSION IF NOT EXISTS pg_trgm` + GIN with `gin_trgm_ops`). Idempotent on re-run; downgrade drops the index but leaves the extension installed (other tables / future migrations may depend on it).
- Helm chart version 0.9.12 → 0.9.13 (chart evolves independent of app). appVersion 0.9.9.10.
- 1376 backend tests + 109 dashboard tests passing (was 1367 in v0.9.9.9; +9 from B4/B6 + ask-keyword regression suites). Three Codex review rounds (entry 207 → 209 → 211 CLEAN) resolved before tag.

## [0.9.9.9] - 2026-05-11

### Fixed
- **CI / Deploy API gate.** Three tests asserted substrings (`--to`, `messages.jsonl`, `claude-code (user)`) directly against captured stdout/stderr. Rich's console renderer splits styled text across ANSI escape sequences when color is enabled (CI's wider terminal triggered it; local pytest had color disabled). v0.9.9.8's Deploy API workflow runs `pytest tests/ -v` as a deploy gate, so this blocked the release deploy. Fix: strip ANSI codes with a `\x1b\[[0-9;]*[a-zA-Z]` regex before substring assertions in `tests/unit/test_autosync.py` and `tests/unit/test_hooks_installer.py`. Verified by running the full backend suite under `FORCE_COLOR=1`.

### Changed
- **Dashboard audit polling consolidated.** `dashboard/src/hooks/useAudit.ts` previously exposed a `useRunAudit` hook that owned a parallel 5-second poll loop competing with `BackgroundTasksProvider` for the same audit lifecycle. `useRunAudit` had zero callers — pure dead code. Removed; `BackgroundTasksProvider` (driven by `AuditModal`) is now the single owner of run + poll.
- **Onboarding page stops polling on completion.** `dashboard/src/onboarding/GettingStartedPage.tsx` used `refetchInterval: 10_000` unconditionally on the `sessions` and `projects` queries, so the page kept hitting `/api/v1/sessions` and `/api/v1/projects` every 10 s indefinitely. Now each query stops polling once its own completion condition is met (`sessions.total > 0`, `projects.length > 0`) via the function-form `refetchInterval`.
- **Folder + inbox-handoff stale windows extended on the hottest screen.** `useFolders` staleTime 30 s → 300 s; `SessionList`'s inbox-handoff query 60 s → 300 s. Folder mutations (`create`/`update`/`delete`/`bookmark`) still invalidate the cache, so stale data isn't a correctness risk. The 5-min window cuts redundant refetches on every remount and tab focus — `SessionList` is the dashboard's hottest screen and these queries fired on every mount.

### Notes
- No new database migrations. No server-side code changes.
- Helm chart version 0.9.11 → 0.9.12 (chart evolves independent of app). appVersion 0.9.9.9.
- 1344 backend tests + 109 dashboard tests passing. Test fix verified under `FORCE_COLOR=1` to match CI behaviour.

## [0.9.9.8] - 2026-05-11

### Fixed
- **DLP per-member size cap is now tier-aware.** `redact_and_repack` accepts a `member_limit_bytes` parameter sourced from the same `_member_size_limit_for_tier` helper used by `_check_member_sizes`. Pre-fix, a hardcoded `50 * 1024 * 1024` in `server/dlp.py` silently nullified any `SFS_MAX_SYNC_MEMBER_BYTES_PAID` env override above 50 MB for orgs with DLP=REDACT mode enabled. New typed `DlpMemberTooLargeError` lets the route return the same structured 413 envelope as the non-DLP path. Post-redaction size validation added — replacement markers like `[REDACTED:openai_api_key]` (24 chars) can expand a payload past the cap.
- **`sfs handoff <handoff_id>` now redirects to `sfs pull-handoff` instead of "Missing option --to".** Recipients who pasted a handoff ID into the (sender) command hit a wall; redirect is gated on `not to` so session aliases shaped like `hnd_…` aren't hijacked.
- **`handle_errors` decorator no longer swallows Typer/Click validation errors.** `click.exceptions.ClickException` re-raises through the standard Typer error-box rendering instead of being caught as generic Exception and printed as "Unexpected error: ...".
- **`sfs sync` no longer silently skips sessions the daemon auto-excluded for transient reasons.** Hard deletes still respected. Per-session atomic clearing under `fcntl.flock` (new `acquire_for_retry()` helper) gates immediately before the network call so partial-failure paths preserve the exclusion. TOCTOU window between snapshot read and clear is closed: any concurrent writer that installs a hard delete during the sync run wins. All exclusion-list helpers (`is_excluded`, `get_entry`, `list_deleted`, `is_transient_exclusion`) now defensively filter non-dict entries from a hand-edited or corrupted `deleted.json`.
- **`sfs doctor` install-consistency check** detects "pip-installed sessionfs to user-site but PATH points at an older binary tied to a different Python interpreter" drift. Parses the on-PATH `sfs` binary's shebang and subprocesses that interpreter to read its `sessionfs.__version__`. Reports the divergence and points at the `python -m sessionfs.cli.main` fallback (made reachable via a new `__main__` guard in `cli/main.py`).
- **Helm chart version** bumped 0.9.10 → 0.9.11 (chart evolves independently of app version; appVersion 0.9.9.8).

### Notes
- No new database migrations. 031, 032, 033 from v0.9.9.7 remain authoritative.
- 1344 backend tests + 109 dashboard tests passing. Ten Codex review rounds resolved.

## [0.9.9.7] - 2026-05-10

### Added
- **Tier-aware per-member sync size cap** — `messages.jsonl` and other archive members can now reach **50 MB** for Pro/Team/Enterprise (was 10 MB across the board). Free/Starter stay at 10 MB. Override via `SFS_MAX_SYNC_MEMBER_BYTES_FREE` / `SFS_MAX_SYNC_MEMBER_BYTES_PAID`. Hard 100 MB abuse cap unchanged. The CLI pre-flight check uses the larger paid default; the server enforces the actual tier-resolved cap and returns a structured 413 with tier-aware suggestion text.
- **`dismiss_knowledge_entry` MCP tool (audited)** — write tool that records `dismissed_at`, `dismissed_by`, `dismissed_reason` on the entry. Idempotent (re-dismiss preserves the original timestamp + dismisser). Set `undismiss=true` to reverse a dismissal — clears the audit row. Length-capped at 500 chars; whitespace-only reasons normalize to NULL on the server. Audit triple is exposed on every entry-returning endpoint via `KnowledgeEntryResponse` so agents can confirm what was recorded.
- **Migration 031** — `dismissed_at`, `dismissed_by`, `dismissed_reason` columns on `knowledge_entries`.
- **Migration 032** — `recipient_email_normalized` column on `handoffs` plus a dedicated index for inbox lookups. Backfilled from existing rows via SQLAlchemy Core (cross-DB).
- **Migration 033** — composite indexes on `knowledge_entries` for the Tier A list paths: `(project_id, dismissed, claim_class, freshness_class)`, `(project_id, compiled_at, dismissed)`, and `(project_id, created_at)` for keyset cursor pagination.

### Changed
- **Concept compiler query collapse** — `auto_generate_concepts` now prefetches the active-claim set, all candidate concept pages, and their links once before the candidate loop, then resolves dismissed-entry membership in one bulk query. Previously did `candidates × active_claims` plus per-slug page + link lookups; now O(candidates) in-memory.
- **Handoff list batching** — `/api/v1/handoffs/inbox` and `/sent` batch-load referenced senders + sessions in two queries instead of N+1 per handoff. Inbox filters on `recipient_email_normalized` directly (raw-column index miss is fixed); a fallback OR clause keeps pre-migration rows reachable until backfill catches them.
- **Server-side dismiss reason normalization** — `DismissRequest.reason` strips whitespace and collapses empty/whitespace-only to `None` via Pydantic `field_validator`. Direct API callers can no longer persist `"   "` as the audit reason or clobber a real prior reason with whitespace.
- **`KnowledgeEntry` row lock on dismiss** — `PUT /entries/{id}` now `SELECT ... FOR UPDATE` the entry before branching on `entry.dismissed`, eliminating the audit-row race where two concurrent dismissals could both take the first-dismiss path. SQLite no-ops the lock; PostgreSQL serializes overlapping callers.

### Known Performance Items
The following items were identified during the v0.9.9.7 perf audit but
deferred — they are not blocking and can land in a later patch:
- Dashboard polling consolidation: `BackgroundTasks.tsx` and `useAudit.ts`
  run independent `setInterval` loops against overlapping endpoints. Move
  to a single query-driven path with backoff and visibility awareness.
- `GettingStartedPage.tsx` polls every 10 s indefinitely; should stop
  once `hasSession && hasProject`.
- `SessionList.tsx` always loads folders + inbox handoffs; lazy-load
  behind the folder/handoff UI or extend the stale window.
- `sync status` does five separate aggregates and `admin.py` org listing
  does per-org member-count queries; collapse into grouped aggregates.
- KB content search still uses `ILIKE %…%`; trigram / FTS index is the
  next scalability step once the composite indexes above bed in.

### Fixed
- **Sync error message uses server detail** — `SyncTooLargeError` prefers the structured detail body from the server (which carries tier-aware `suggestion` text) over the hardcoded message. Pre-v0.9.9.7 fallback text retained for older deployments.
- **Pip-audit MEDIUM bumps** — `python-multipart>=0.0.27`, mako and pip refreshed in the dev environment.
- **postcss XSS bump** — dashboard `postcss` 8.5.8 → 8.5.14 (dev-time tooling, not a production runtime exposure).

## [0.9.9.6] - 2026-05-10

### Added
- **MCP Tier A read surface (7 new tools)** — `get_knowledge_entry`, `list_knowledge_entries`, `get_wiki_page`, `get_knowledge_health`, `get_context_section`, `get_session_provenance`, `compile_knowledge_base`. Total MCP tool count goes 14 → 21. All tools enforce existing project membership / session ownership checks.
- **`list_knowledge_entries` rich filters** — `claim_class`, `freshness_class`, `dismissed`, `session_id` query params on `GET /api/v1/projects/{id}/entries`, plus three sort modes (`created_at_desc`, `last_relevant_at_desc`, `confidence_desc`) with stable `id` tiebreak so identical sort-key values can't reorder.
- **Keyset cursor pagination** — opt-in `?cursor=<id>` query param on `list_entries` (default sort only). Snapshot-stable across concurrent inserts/deletes — no skipped or duplicated rows. Server emits `X-Next-Cursor` response header on every default-sort page when more rows exist (works in both OFFSET and cursor modes), so callers can bootstrap keyset iteration from page 1 without inventing a sentinel.
- **Per-user tier-aware knowledge rate limits** — `KNOWLEDGE_RATE_LIMITS = {free:20, starter:50, pro:100, team:100, enterprise:200, admin:500}` requests/hour on `POST /entries/add`. Bucket key is `user_id` (not session_id) so MCP `manual` callers don't share buckets. `SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR` env override. Admin tier promoted before effective-tier resolution.
- **413 graceful failure** — `sfs push`/`sfs handoff`/`sfs sync` pre-scan archives for oversized members (10MB limit) and surface `SyncTooLargeError` with actionable guidance instead of an opaque server 413.
- **Compression-safe capture guard** — shared `should_recapture()` helper consulted by all 7 watchers before re-write. Checks the deleted-sessions exclusion list FIRST, then compares source JSONL message count against existing `.sfs` to prevent re-capturing a compressed session as empty.
- **`sfs recapture` command** — manual re-capture flow with `CursorComposerPurgedError` guard for purged-composer cases.
- **Cross-tool MCP-over-CLI nudge** — three-layer fix so agents prefer MCP tools over `sfs` shell commands: (1) MCP tool descriptions say "Always use this MCP tool instead of running `sfs ...`"; (2) `_SESSIONFS_MCP_GUIDANCE` block injected into every compiled rules file (CLAUDE.md / codex.md / .cursorrules / GEMINI.md / copilot-instructions.md) lists all 21 tools; (3) `sfs hooks install` writes a SessionStart hook for Claude Code that emits the cached compiled output before the first message.
- **Dashboard "Compile now" CTA** — workflow hint banner now carries an inline button so users don't navigate to the Entries tab to act. Server-side health recommendations fire at any pending count > 0 (was > 20).
- **Compile workflow guidance in compiled rules** — agents are instructed to check `get_knowledge_health` after contributing knowledge, prompt the user, and only call `compile_knowledge_base` on explicit consent.
- **Pre-upgrade Helm migration Job** — runs Alembic upgrade head before the API rolls out, so a self-hosted upgrade that drops a new migration cannot serve traffic against an unmigrated DB.

### Changed
- **Project-row lock on compile + concept generation** — `compile_project_context` and `auto_generate_concepts` both `SELECT … FOR UPDATE` the project row before reading pending claims / creating concept pages. Two compile callers serialize end-to-end on PostgreSQL; SQLite no-ops the lock harmlessly. Eliminates the duplicate-context-document and duplicate-concept-page races.
- **Compile MCP response trimmed** — `compile_knowledge_base` strips `context_before` / `context_after` (often thousands of words) from the MCP JSON payload to keep agent context small. Dashboard direct-API calls still receive the full payload.
- **Dashboard manualChunks** — explicit `vite.config.ts` chunk naming (`react-vendor`, `markdown`, `zod`, `react-router`, `react-query`). Main bundle drops from 212 KB to 30 KB (8× smaller); vendors cache separately across deploys.
- **`/api/v1/health` alias** — added alongside `/health` for self-hosted ingress paths that scope readiness to `/api/v1/*`.
- **DELETE `/sessions/{id}` backward-compat** — accepts `scope=` query param for older CLI clients.
- **Settings.json fcntl locking** — `sfs hooks install/uninstall` and rules emitter use `fcntl.flock` on a `.sfs-lock` file so concurrent invocations can't corrupt the JSON. Hook entry detection now requires command prefix `sfs rules emit ` (not just sentinel match) before treating an entry as managed.

### Fixed
- **Stable pagination ordering** — `KnowledgeEntry.id.desc()` is the absolute final tiebreak in every sort mode on `list_entries`. Without it, identical sort-key values reordered arbitrarily across pages.
- **Daemon transient errors no longer exclude sessions** — only `SyncTooLargeError` (413) counts toward the exclusion threshold; transient `SyncError` failures are retried instead of permanently excluded.
- **Admin tier reaches its 500/hr knowledge bucket** — admin is checked from raw `user.tier` before effective-tier resolution (which collapses admin → enterprise). Previously admins were capped at 200/hr.
- **`sfs handoff` 413 graceful path** — handoff now pre-checks oversized members and catches `SyncTooLargeError`, exiting with actionable guidance instead of an opaque server error.
- **Compile description honesty** — `compile_knowledge_base` MCP description now flags "HEAVY + MUTATING", explains the project lock, and points to the real `GET /api/v1/projects/{id}/compilations` collection (not a non-existent per-id route). Removes the misleading "cron-driven pass" claim — there is no scheduler.
- **Rules emit unmanaged-CLAUDE.md guard** — `sfs rules emit` no longer falls back to reading an unmanaged CLAUDE.md as if it were managed content.
- **KB health pending count** — only counts claims (excludes notes). Notes don't compile, so they shouldn't drive the pending banner.

## [0.9.9.5] - 2026-04-17

### Added
- **First-run onboarding** — signup auto-authenticates and navigates to `/getting-started`. Three-step onboarding page (install tool, capture session, create project) with live completion indicators. State-based redirect gate: 0 sessions + 0 projects → onboarding. API key shown in dismissible banner after signup. User-scoped dismissal via djb2 hash (not global to browser). Legacy key migration for upgrade path.
- **Sort direction toggle** — ascending/descending on all sort modes in the dashboard. CLI adds `--sort messages-asc` and `--sort tokens-asc` for finding small sessions.
- **Tool sort mode** — real "Sort: Tool" in the sessions list groups by tool label.
- **Tool filter alias normalization** — `gemini` / `gemini-cli` and `copilot` / `copilot-cli` treated as the same family across list, search, and admin endpoints.

### Changed
- **Cloud Run min-instances** — API deployment now sets `--min-instances 1` to eliminate cold-start latency.
- **OnboardingGate loading** — shows lightweight placeholder instead of blank screen or mounting SessionList prematurely (avoids extra folder/handoff queries for first-time users).
- **Publish Container Images workflow** — now supports `workflow_dispatch` for manual retrigger with configurable ref.

### Fixed
- **Unified 410 delete propagation** — structured `SyncDeletedError` replaces string-based detection. Shared `cleanup_deleted_session()` helper wired into all three sync paths (bulk `sfs sync`, explicit `sfs push`, daemon autosync). Dashboard-deleted sessions are auto-cleaned locally on next sync instead of showing red 410 errors.
- **Full local cleanup on server 410** — removes `.sfs` directory + SQLite index entry (both `sessions` and `tracked_sessions` tables) + adds to exclusion list. No more orphaned local copies.
- **Migration 030 cross-DB backfill** — replaced raw PostgreSQL-only `INTERVAL` syntax + `is_deleted = 1` with SQLAlchemy Core queries (works on both PostgreSQL and SQLite). Handles NULL `deleted_at` with sensible defaults.
- **`sfs delete` / `sfs restore` prefix resolution** — now resolves session ID prefixes via local store for all scopes (was only resolving for local/everywhere, not cloud).
- **`sfs rules init` TTY guard** — fails fast with clear message when stdin is not a TTY and `--yes` is not passed.
- **Sessions empty state** — now links to `/getting-started` and `/help` instead of bare `sfs push` command.

## [0.9.9.4] - 2026-04-16

### Added
- **Three-scope session delete** — `sfs delete <id>` with `--cloud` (server only, keep local), `--local` (device only, keep cloud), or `--everywhere` (both). No default — explicit choice required. Confirmation prompt with `--force` bypass for automation.
- **Sync-aware deletes** — autosync respects intentional deletes via `~/.sessionfs/deleted.json` exclusion list. Push and pull skip excluded sessions. `sync_push` un-delete path is gated behind `X-SessionFS-Undelete: true` header with ETag conflict check — autosync can never reverse a delete.
- **`sfs trash`** — lists soft-deleted sessions in the 30-day retention window with scope badges and purge dates.
- **`sfs restore <id>`** — reverses a soft-delete on the server, clears local tombstone, prints `sfs pull` guidance when local copy was removed.
- **Dashboard delete dialog** — replaces the single `confirm()` with a two-choice dialog: "Remove from cloud" or "Delete everywhere". One-line explanation per option.
- **Dashboard Trash view** — filter toggle on the session list showing soft-deleted sessions with scope badges, purge dates, restore buttons, and scope-aware restore guidance toast.
- **Admin purge endpoint** — `POST /api/v1/admin/purge-deleted` hard-deletes expired soft-deleted sessions and their blobs. Single-session or bulk. Returns purge count and bytes reclaimed.
- **Restore response guidance** — `POST /sessions/{id}/restore` returns `restored_from_scope` and `local_copy_may_be_missing` so clients can show accurate recovery guidance.
- **Migration 030** — `deleted_by`, `delete_scope`, `purge_after` columns on sessions table.

### Changed
- **DELETE endpoint** — now requires `?scope=cloud|everywhere` query parameter (was parameterless). Returns 200 with session record including `purge_after` (was 204). Old clients without `?scope=` get 400.
- **Storage quota** — soft-deleted sessions excluded from used-bytes calculation.
- **Share links** — return 410 Gone for deleted sessions (was 404).

### Fixed
- **Autosync un-delete bug** — the original soft-delete was immediately reversed by autosync pushing the local copy. Now gated by explicit intent header + ETag check.
- **Purge audit atomicity** — purge and audit log now committed in a single transaction (was two separate commits).

### Security
- **CVE-2026-40347** — python-multipart bumped from `>=0.0.9` to `>=0.0.26` (DoS via crafted multipart preamble/epilogue).
- **Purge endpoint hardening** — session_id format validation + atomic audit logging.

## [0.9.9.3] - 2026-04-16

### Added
- **End-to-end resume smoke tests** — 3 tests exercising the actual `resume()` command wiring with real file writes, mocked API boundary, and stubbed tool launch. Proves: missing target file gets created before launch, unmanaged file is skipped without `--force-rules`, preflight failure does not abort resume.
- **Recursive nested skills/agents provenance** — `instruction_provenance` capture now discovers files recursively under `.agents/`, `.claude/commands/`, `.claude/skills/`, etc. (was flat-only). Uses `os.walk` with deterministic sorted traversal, symlink skipping, and a 30-file-per-root bound.

### Fixed
- **Provenance docs misstatements** — docs said the source-provenance line is "omitted" when absent (it actually prints a fallback message), and said "meaningfully initialized" checks `rules_versions` rows (it only checks `enabled_tools` / `static_rules` / `tool_overrides`). Both corrected in `docs/rules.md` and `site/.../rules.mdx`.
- **Provenance capture summary table** — added to docs: 4-row table (managed / unmanaged / global / nested) showing what's captured and whether it's reconstructable.

### Documentation
- **Provenance reconstruction semantics** — docs now explicitly state that unmanaged/global artifacts are hash-only (not reconstructable), and that resume uses current canonical rules by default (not historical session rules). Historical replay deferred to v0.9.10.

## [0.9.9.2] - 2026-04-16

### Fixed
- **Session push 500 with instruction provenance** — `rules_hash` column was `String(64)` but captured hashes are `sha256:` + 64 hex = 71 chars. PostgreSQL enforced the length and raised on INSERT. Fix: strip the `sha256:` prefix before storing (hex digest alone is sufficient for identity matching) + migration 029 widens the column to `String(80)` for defense-in-depth.
- **`sfs push --yes`** — new flag to skip the interactive DLP confirmation prompt. Findings are still displayed but don't block the push. Required for non-interactive environments (CI, scripts, daemon autosync).

## [0.9.9.1] - 2026-04-15

### Fixed
- **Migration 028 boolean defaults** — `server_default=sa.text("1")` on `project_rules.include_knowledge` and `include_context` worked on SQLite but PostgreSQL rejects `1` as a boolean literal. Changed to `sa.text("true")` to match the project's Postgres-compatible migration pattern. v0.9.9 prod API deploy failed the Alembic migration step; v0.9.9.1 clears it. No data impact — the broken migration never committed a transaction.

## [0.9.9] - 2026-04-14

### Added

#### Rules Portability — canonical project rules compiled to five tool formats
- **Canonical rules per project** — new `project_rules` + `rules_versions` tables (migration 028) with 4 new columns on `sessions` for instruction provenance. Managed via `GET/PUT /api/v1/projects/{id}/rules`, compiled via `POST /api/v1/projects/{id}/rules/compile`.
- **Five tool compilers** — deterministic partial-compile-aware compilers for Claude Code (`CLAUDE.md`), Codex (`codex.md`), Cursor (`.cursorrules`), Copilot (`.github/copilot-instructions.md`), and Gemini (`GEMINI.md`). Each embeds a SessionFS managed marker at the top of the file.
- **Knowledge + context injection** — compilers pull active claims (default `convention` + `decision` types) and project context sections (default `overview` + `architecture`) with per-tool token ceilings and progressive condensation.
- **`sfs rules` CLI** — `init` (auto-detects existing rule files + recent session-history tool usage, preselects from the 5 supported tools), `edit`, `show` (in-sync state), `compile` (with `--tool X`, `--dry-run`, `--force`), `push`, `pull`. `--local-only` at init adds compiled files to `.gitignore`; the default is shared-in-repo.
- **Managed-file safety** — `sfs rules compile` refuses to overwrite a user-maintained rule file unless `--force` is set. Detection reads only the first 512 bytes so markers buried in hand-written content don't trigger false positives.
- **Session instruction provenance** — session manifests now carry `rules_version`, `rules_hash`, `rules_source` (`sessionfs` / `manual` / `mixed` / `none`), and `instruction_artifacts[]`. Managed files record version + hash only (content lives in `rules_versions`); unmanaged / global artifacts store path + hash + source + scope. `SFS_CAPTURE_GLOBAL_RULES=off` suppresses global hashing for privacy-sensitive environments.
- **MCP tools** — read-only `get_rules` and `get_compiled_rules` for agents. No agent self-modification of rules.
- **Dashboard Rules tab** — new `RulesTab` under ProjectDetail with version badge, static preferences editor, enabled tools checklist, knowledge/context injection settings, per-tool compiled output viewer, version history list, and compile action. ETag-based optimistic concurrency with 409 toast on stale saves.

#### Resume-Time Rules Sync — carry the behavior contract across tools
- **Preflight during `sfs resume`** — before launching the target tool, partial-compile only that tool's rule file from current canonical rules and write it with managed-file safety (Case A write / Case B refresh / Case C warn-skip / Case D `--force-rules` overwrite). Supported resume targets: claude-code, codex, copilot, gemini.
- **Source session provenance display** — shows what rules shaped the original session before launching the resumed session (e.g. `Source session used rules v3 (sessionfs). Current project rules are v5. Synced codex.md from SessionFS rules v5.`). Hash-based provenance survives environments where content snapshots were intentionally suppressed.
- **New flags on `sfs resume`** — `--no-rules-sync` skips the preflight entirely; `--force-rules` overwrites an unmanaged target rule file with SessionFS-managed content. `--force-rules` is a one-time permission — the file becomes SessionFS-managed afterward and subsequent resumes refresh it normally.
- **Meaningfully initialized** — preflight compiles and writes only when the project has curated content (`enabled_tools` non-empty, `static_rules` non-empty, or `tool_overrides` non-empty). Untouched default rules rows skip cleanly.
- **Non-fatal semantics** — rules sync failure never fails the resume itself. Warning to stderr, resume continues, exit 0.
- **Partial-compile guarantee** — any compile request with an explicit `tools` override is treated as partial and never bumps canonical history, regardless of whether the override matches `enabled_tools`.

### Fixed
- **Helm chart invalid semver** (`chart.metadata.version "0.9.8.6" is invalid`) — fixed by the v0.9.9 bump. Four-segment versions aren't valid semver; the chart now tracks the same 3-segment version as the Python package.
- **Compile hash included managed marker** — regression fix: `content_hash` is now the body hash only (marker-independent), so no-op detection doesn't break after a version bump. No more infinite-version-bump loops.
- **Atomic optimistic concurrency on PUT /rules** — `SELECT ... FOR UPDATE` row lock ensures two writers holding the same prior ETag can't both commit. Second one receives 409.
- **Concurrent compile race** — version-number contention now retries idempotently; colliding with a same-body-hash winner short-circuits to no-op and aligns local `rules.version` to the winner.
- **First-time rules creation race** — `get_or_create_rules` catches `IntegrityError` and returns the winner's row rather than raising 500.
- **Knowledge injection determinism** — claim ordering priority (decision > convention > pattern > dependency) moved into SQL `ORDER BY` so LIMIT respects it; deterministic `id DESC` tie-breaker on identical timestamps.
- **Path traversal defense** — added `_safe_target_path()` in `cli/cmd_rules.py` used by both `sfs rules compile` and resume preflight; validates target file against canonical `TOOL_FILES[tool]` and confirms the resolved path stays inside `git_root`.
- **Provenance logging visibility** — `watchers/provenance.py` failures now log at `warning` level (was `debug`) so breakage is visible in normal daemon operation. Still non-fatal to session capture.
- **Malformed compile payload hardening** — `preflight_target_tool_rules()` validates `outputs` is a list and `filename`/`content` are strings; returns structured `api-error` result instead of raising.

### Security
- **CVE-2025-71176** — pytest bumped from `>=8.0,<9.0` to `>=9.0.3,<10.0` (dev dependency only; /tmp DoS under certain test configurations).

## [0.9.8.6] - 2026-04-13

### Added
- **Knowledge Base v2 — claim model** — three-layer lifecycle: evidence (raw facts from sync) → claim (promoted active truth) → note (rejected or dismissed). New columns on `knowledge_entries`: `claim_class`, `entity_ref`, `entity_type`, `freshness_class`, `supersession_reason`, `promoted_at`, `promoted_by`, `retrieved_count`, `used_in_answer_count`, `compiled_count` (migration 027).
- **Per-type freshness decay** — new `src/sessionfs/server/services/freshness.py` applies per-type windows: bug 30d, dependency 60d, pattern/discovery 90d, convention 180d, decision 365d. Entries decay from `current` → `stale` → `archived` based on `last_relevant_at`.
- **Auto-promotion at compile** — evidence with `confidence >= 0.5` and `content >= 30 chars` is automatically promoted to `claim` at the start of each compile pass, so compile pulls from a stable active-claim pool.
- **Supersession** — `PUT /api/v1/projects/{id}/entries/{entry_id}/supersede` retires an old claim and links it to a superseding entry with a reason. Superseded entries remain readable for audit but are excluded from compile.
- **Refresh endpoint** — `PUT /api/v1/projects/{id}/entries/{entry_id}/refresh` updates `last_relevant_at` and resets `freshness_class` to `current`; replaces the old "Still valid" no-op (which called undismiss on non-dismissed entries).
- **Rebuild endpoint** — `POST /api/v1/projects/{id}/rebuild` resets `compiled_at=NULL` on all active claims and clears `context_document`, forcing a full compile on settled projects where `compile` would otherwise be a no-op.
- **`sfs project rebuild` CLI** — new command mirrors the rebuild endpoint.
- **Writeback gates for agent contributions** — `add_knowledge` (MCP + API) defaults to `claim_class="note"`. Auto-promotes to `claim` only if specificity gate, semantic dedup (Jaccard-min ≥ 0.85), and rate limit pass. Returns classification feedback so agents know when their entry was rejected vs accepted vs promoted.
- **Section pages as true projections** — compile iterates ALL known types from `slug_map`, not just types in the pending batch. Section pages with zero active claims are deleted inline (previously went stale indefinitely).
- **`used_in_answer_count` tracking** — MCP `ask_project` and API search expose a `_used_in_answer` flag that increments this counter on retrieval, feeding the freshness signal.
- **Dashboard `KnowledgeEntriesTab` v2** — health banner with stale review queue, claim/freshness badges, filter controls, provenance blocks (entity ref + promoted_by + retrieved/used counts), promote/supersede/refresh/dismiss actions, rebuild button.

### Changed
- **Compile default budget lowered to 2000 words** (from 8000) — active-truth-per-token principle produces sharper context documents. Migration 027 backfills existing projects with `kb_max_context_words = 8000 OR NULL` to the new default.
- **Search filters to active claims by default** — `GET /api/v1/projects/{id}/entries/search` excludes evidence, notes, and superseded entries unless `include_stale=true`.
- **MCP `search_project_knowledge`** — same default; returns active-claim results with provenance.
- **MCP `get_project_context`** — filters to active claims when constructing the compiled document.

### Fixed
- **Concept page prune filter** — compile filtered dead pages by `page_type == "auto"`, but concept pages are stored as `page_type == "concept"`. Dead pages were never pruned.
- **`dismiss-stale` safety guard** — added `confidence < 0.5` constraint to prevent bulk-dismissing medium-confidence entries that haven't been referenced recently.
- **Daemon startup order** — `_fetch_remote_settings()` now runs before `full_scan()`. Previously a slow settings fetch blocked watcher startup by ~95 seconds on fresh installs.
- **Codex watcher NoneType crash** — `payload.get("content") or []` pattern in 4 places (content, summary, action blocks); prior code crashed when Codex emitted null fields, halting the entire watcher loop.
- **Daemon PID resolution fallback** — `sfs daemon stop` now falls back to `daemon.json` when `sfsd.pid` is missing, fixing orphaned daemon processes after crashes.

### Documentation
- **`sfs dlp scan/policy` in site CLI docs** — added to `site/src/content/docs/cli.mdx`.

## [0.9.8.5] - 2026-04-12

### Added
- **Dashboard Help page** (`/help`) — MCP-first guidance with 8-tool installer (`sfs mcp install --for <tool>`), agent prompt examples, curated CLI reference, 12-tool MCP reference, external resource links.
- **Admin org back-office endpoints** — `GET/POST /api/v1/admin/orgs` + `PUT /api/v1/admin/orgs/{id}/tier` for internal provisioning, bypassing the Team+ subscription gate.
- **Bulk dismiss-stale endpoint** — `POST /api/v1/projects/{id}/entries/dismiss-stale` atomically dismisses old low-confidence entries (< 0.5 confidence + > 90 days unreferenced). Dashboard surfaces a "Dismiss N stale" button in the knowledge health banner.
- **Dashboard health banner** — `ProjectDetail` Entries tab surfaces stale/low-confidence/decayed counts + actionable recommendations from the health API.
- **Self-hosted Security Posture documentation** — new section in `docs/self-hosted.md` and `site/src/content/docs/self-hosted.mdx` covering non-root UIDs, read-only rootfs, capability drops, seccomp, and `trivy config` verification.
- **`sfs dlp` CLI documentation** — `docs/cli-reference.md` now documents `sfs dlp scan` and `sfs dlp policy` subcommands.

### Fixed
- **Dashboard signup broken on app.sessionfs.dev** — `VITE_API_URL` unset at Vercel build time; `baseUrl` defaulted to `window.location.origin` (static host), returning 405 on POST. Fixed three ways: env var set in Vercel, URL baked into bundle, `app.*` → `api.*` fallback in code.
- **Unguarded localStorage across dashboard** — `main.tsx`, `ThemeToggle`, `SettingsPage` assumed a full Storage interface. Now all call sites use `src/utils/storage.ts` helper with full guards.

### Security
- **GitLab webhook user binding** (HIGH) — webhook accepted any per-user secret match but discarded which user it belonged to, then loaded credentials from `sessions[0].user_id`. A forged MR payload could impersonate another user's GitLab token. Fixed: bind `auth_user_id` to the matching row, scope session lookup + credential load to that user.
- **GitHub installation claim IDOR** (HIGH) — `PUT /settings/github` blindly claimed the first unclaimed installation. Fixed: require `installation_id` from the client, time-windowed claim (15 min), 403/410 on violations, atomic conditional `UPDATE ... WHERE user_id IS NULL` to prevent race conditions.
- **Effective-tier leak** (MEDIUM) — `sync_push` upload limit and `/api/v1/sync/status` read `user.tier` directly instead of resolving the effective org tier. Enterprise org members with personal `free` tier got 50 MB limits. Fixed: both paths now use `get_effective_tier(user, db)`.
- **`pr_comments` unique index scoped** (MEDIUM) — migration 026 widens the unique index from `(repo_full_name, pr_number)` to `(installation_id, repo_full_name, pr_number)` to match runtime query scoping.
- **Security Scan workflow rebuilt** — replaced `aquasecurity/trivy-action` with raw `trivy` binary (`setup-trivy@v0.2.6`, `trivy v0.69.3`); renders Helm chart before misconfig scan; `--severity CRITICAL,HIGH` exit-code enforcement.
- **Dockerfile hardening** — `Dockerfile` and `Dockerfile.mcp` now create a non-root `sessionfs` user (UID 10001) and set `USER 10001` before CMD.
- **Helm chart hardening** — PostgreSQL StatefulSet + `helm test` hook pod now have full `securityContext` with `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`. Fixed pre-existing bug where fallback emptyDir volume was at StatefulSet spec level instead of pod spec level.
- **4 CVE patches** — vite 7.0.0→7.3.2 (3 CVEs), defu ≤6.1.4→6.1.5 (prototype pollution) in `site/package-lock.json`.

### Knowledge Base Lifecycle
- **LLM compile budget enforcement** — `_trim_to_budget()` now applied after LLM response, not just on the simple-compile path.
- **Semantic dedup on all extraction paths** — shared `word_overlap()` + `is_near_duplicate()` helpers; both deterministic and LLM extraction now run a 3-layer filter (exact-match, project-wide overlap, intra-batch).
- **Concept page pruning** — new `_prune_dead_concept_pages()` runs unconditionally before candidate check; fixed `page_type` filter (`"auto"` → `"concept"`).
- **Health stale-entry count** — includes entries with old `last_relevant_at`, not just `IS NULL`.
- **Bulk dismiss confidence guard** — only dismisses entries with `confidence < 0.5`.

### Daemon / Sync
- **Codex watcher crash** — `payload.get("content", [])` returns `None` when key exists with explicit null. Fixed in 4 places with `payload.get("content") or []` pattern.
- **Daemon startup delay** — `_fetch_remote_settings()` moved before watcher `full_scan()` so autosync mode is correct immediately (308 ms vs 96 s).
- **`sfs daemon stop` fallback** — new `_resolve_daemon_pid()` prefers `sfsd.pid`, falls back to `daemon.json`. Both stop and status use it. Also clears `daemon.json` on stale-PID cleanup.
- **DB pool config** — `SFS_DATABASE_POOL_SIZE`, `MAX_OVERFLOW`, `POOL_TIMEOUT`, `POOL_RECYCLE` env vars wired through `ServerConfig` → `init_engine()`.

### Tests
- Backend: 1052 → **1091** (+39 tests in 4 new files: billing webhooks, sync promotion failures, GitHub claim, knowledge lifecycle)
- Dashboard: 22 → **76** (+54 tests in 7 new files: BillingPage, GitHubIntegrationSection, HandoffDetail, ProjectDetail, SessionDetail, AdminDashboard, OrgPage)
- Codex null-content regression tests (2 new)

## [0.9.8.4] - 2026-04-10

### Added
- **Dashboard Help page** — new `/help` route with MCP-first guidance, an 8-tool installer (Claude Code, Codex, Gemini, Cursor, Copilot, Amp, Cline, Roo Code) with live terminal preview and copy-to-clipboard, example agent prompts by use-case, a curated 10-command CLI quick-reference, the full 12-tool MCP reference, and external resource links. Help icon sits between ThemeToggle and the avatar; a Help entry also appears in the mobile drawer.
- **Helm chart Security Posture documentation** — new "Security Posture" section in `docs/self-hosted` covering non-root UIDs, read-only root filesystems, dropped capabilities, RuntimeDefault seccomp, and how to verify with `trivy config` on a rendered chart.

### Fixed
- **Dashboard signup broken on app.sessionfs.dev** — `VITE_API_URL` was not set at Vercel build time, so the LoginPage `baseUrl` defaulted to `window.location.origin`, making signups POST to the static Vercel host and return 405. Fixed three ways: (1) set `VITE_API_URL=https://api.sessionfs.dev` in Vercel for all three environments, (2) bake the URL into the new production bundle, (3) derive `api.<domain>` from `app.<domain>` in the fallback chain so this cannot silently break again.
- **Unguarded localStorage across dashboard** — `main.tsx`, `ThemeToggle`, and `SettingsPage` assumed a full Storage interface. Now all call sites route through a new `src/utils/storage.ts` helper that guards against missing localStorage, plain-object localStorage (vitest 4 + jsdom), SecurityError on access, and QuotaExceededError on write. Shared vitest setup installs an in-memory stub so every test file gets a working localStorage.
- **Help page theme query stale** — resource links now subscribe to `document.documentElement[data-theme]` via `MutationObserver` so sessionfs.dev links update live when the user toggles theme on the Help page.

### Security (post-release hardening)
- **Helm chart — postgres StatefulSet** — container now has a full `securityContext` with `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `runAsNonRoot: true`, `capabilities.drop: [ALL]`, and `seccompProfile.type: RuntimeDefault`. Added `emptyDir` mounts at `/tmp` and `/var/run/postgresql` so postgres can still write its socket directory with a read-only root filesystem. Also fixed a pre-existing bug where the fallback `emptyDir` volume (when `persistence.enabled=false`) was declared at the StatefulSet spec level instead of the pod spec level, making it ineffective.
- **Helm chart — `helm test` hook** — the curl-based connection test pod now has pod and container `securityContext` with `runAsUser: 10001`, `readOnlyRootFilesystem: true`, dropped capabilities, and RuntimeDefault seccomp.
- **Dockerfile + Dockerfile.mcp** — both now create a non-root `sessionfs` user (UID 10001) and set `USER 10001` before `CMD`. Cloud Run and Kubernetes already enforced non-root at the orchestration layer, but the image itself is now compliant for self-hosted users who don't override.
- **Security Scan workflow rewrite** — replaced `aquasecurity/trivy-action` with raw `trivy` binary (via `setup-trivy@v0.2.6`, `trivy v0.69.3`) because the action's `severity:` input does not filter exit-code for `scan-type: config`. Now renders the Helm chart with `helm template` before running misconfig scan so Trivy evaluates real Kubernetes manifests instead of raw `{{- with .Values }}` templates (which produced 40+ false-positive findings on an already-hardened chart). Also enforces `--severity CRITICAL,HIGH` and excludes `node_modules` / `.venv` from the secret scan.
- **vite 7.0.0–7.3.1 → 7.3.2** — fixes three CVEs in `site/package-lock.json`: CVE-2026-39364 (server.fs.deny bypass), CVE-2026-39363 (WebSocket arbitrary file read), CVE-2026-39365 (path traversal in optimized deps). Build-time only; site is statically generated.
- **defu <=6.1.4 → 6.1.5** — fixes CVE-2026-35209 (prototype pollution via `__proto__` key in defaults argument). Transitive dep in the site build.

## [0.9.8.3] - 2026-04-09

### Fixed
- **Connection pool exhaustion** — client-side `asyncio.Semaphore(5)` limits concurrent uploads; server-side per-user semaphore (max 3) with 429+Retry-After; sync client retries 429 with backoff.
- **sync_push connection lifecycle** — split into 3 phases: DB reads → release connection → blob upload (no DB) → fresh session for writes. Connection held ~70ms instead of ~5s.
- **Sync race condition** — blob uploaded to temp key, row committed with temp key first (PK constraint for creates, FOR UPDATE for updates), blob promoted only after commit. Temp blob preserved on any post-commit failure.
- **Pool health endpoint** — `GET /health/pool` returns pool utilization metrics.
- **Sync summary** — shows error count alongside pushed/pulled/conflicts.

## [0.9.8.2] - 2026-04-09

### Fixed
- **Database pool exhaustion** — API pods were using SQLAlchemy defaults (pool_size=5, max_overflow=10) causing 500 errors under load. Now configurable via `SFS_DATABASE_POOL_SIZE` (default 20), `SFS_DATABASE_MAX_OVERFLOW` (default 40), `SFS_DATABASE_POOL_TIMEOUT` (default 60s), `SFS_DATABASE_POOL_RECYCLE` (default 1800s). Connections verified with `pool_pre_ping=True`.

## [0.9.8.1] - 2026-04-09

### Added
- **Knowledge base lifecycle** — entry decay (0.8x confidence after 90 days unreferenced), auto-dismiss past retention period, configurable per-project settings (`kb_retention_days`, `kb_max_context_words`, `kb_section_page_limit`).
- **Quality gates on knowledge entries** — minimum 20 char content, 20/hr rate limit per session, 85% similarity rejection, lower confidence cap for manual/CLI entries.
- **Context document budget** — 8,000 word default cap with priority-aware trimming (high-confidence entries kept first).
- **Section page caps** — 30 items default, "N older entries not shown" footer.
- **Concept page auto-refresh** — regenerate when cluster grows 50%+, auto-delete when all entries dismissed.
- **Actionable health endpoint** — returns recommendations, stale/low-confidence/decayed entry counts.
- **`sfs resume --model`** — specify model for target tool (Claude Code, Codex, Gemini supported; Copilot warns).
- **Concept auto-generation fix** — stop word filtering, meaningful phrase extraction, trigrams for better topic names.
- Database migration 025: lifecycle fields on knowledge_entries + project settings.

### Fixed
- Billing webhook: `_sync_billing_to_org` requires positive match on both customer_id AND subscription_id. Personal subscription events cannot mutate org state.
- Billing webhook: `_find_user_or_org_by_customer` uses subscription_id to disambiguate legacy same-customer data.
- Billing webhook: non-active downgrade clears `stripe_subscription_id`.
- DLP pre-scan only runs when org policy is enabled (not unconditionally).
- `pack_session()` snapshots files into memory before tarring (fixes active session push).
- Session list sorts by `updated_at` (not `created_at`).
- Concept auto-generation filters stop words — no more "From The" or "Instead Of" concepts.
- Dashboard billing page uses typed `BillingStatus` interface.
- `SessionDetail` type includes `dlp_scan_results`.

## [0.9.8] - 2026-04-09

### Added
- **DLP / Secret Scrubbing** — pre-sync content protection with 14 PHI patterns (SSN, MRN, DOB, NPI, patient name, etc.) and 22 secret patterns. Three enforcement modes: BLOCK, REDACT, WARN. Server-side scan of all archive files, org policy via settings JSON, custom patterns and allowlist support.
- **DLP CLI** — `sfs dlp scan <session_id>` for local scanning, `sfs dlp policy` to view org policy. `sfs push` shows DLP preview when org policy is enabled.
- **DLP Dashboard** — Settings > DLP tab for org admins (enable, mode, categories). Session detail shows DLP findings section with pattern types and action taken.
- **DLP API** — `GET/PUT /dlp/policy`, `POST /dlp/scan` (dry-run), `GET /dlp/stats`. Feature-gated to Pro+ tier.
- Database migration 024: `dlp_scan_results` column on sessions.
- 43 new DLP tests (PHI patterns, false negatives, category filtering, allowlist, custom patterns, redaction, policy validation).

### Changed
- Session list sorts by `updated_at` instead of `created_at` — most recently active sessions appear first.
- `pack_session()` snapshots file contents into memory before tarring — handles active sessions with concurrent daemon writes.

### Fixed
- **Billing webhook isolation** — `_find_user_or_org_by_customer()` uses `subscription_id` to disambiguate legacy same-customer data. `_sync_billing_to_org()` requires positive match on both `customer_id` AND `subscription_id` — personal subscription webhooks can never mutate org state.
- **Billing non-active downgrade** — `past_due`/`unpaid`/`paused`/`incomplete_expired` now clears `stripe_subscription_id` on both org and user rows.
- **DLP pre-scan** — only runs when org DLP policy is enabled, not unconditionally on every push.
- **Dashboard type safety** — `SessionDetail` type includes `dlp_scan_results`, `BillingPage` uses typed `BillingStatus` interface.

### Security
- DLP scanning runs server-side at the sync chokepoint — even modified clients cannot bypass it.
- Redacted content (`[REDACTED:TYPE]`) replaces matches in ALL `.json`/`.jsonl` archive files. Original text never stored.
- Org DLP policy changes are admin-only and merge (don't overwrite custom_patterns/allowlist).

## [0.9.7.4] - 2026-04-07

### Fixed
- **MCP workspace detection** — all 6 project-scoped tools now use MCP `roots/list` protocol to detect workspace directory from the AI tool client, with CWD fallback.
- **MCP explicit git_remote** — `search_project_knowledge`, `ask_project`, `add_knowledge`, `update_wiki_page`, and `list_wiki_pages` now accept an optional `git_remote` parameter, matching `get_project_context`. Fixes "No git repository detected" when roots protocol is unavailable.
- **0-byte index.db recovery** — truncated index files are now detected and deleted on startup, triggering automatic rebuild from .sfs files.
- **Auto-reindex on empty index** — if index has 0 sessions after schema creation, flags for full reindex.

## [0.9.7.3] - 2026-04-06

### Added
- **Knowledge base docs** — new page covering the full knowledge loop, entries, compile, wiki, MCP write tools, API endpoints, best practices.
- **Dashboard user guide** — new page covering all 9 dashboard pages and keyboard shortcuts.
- **Organizations docs** — new page covering org creation, invites, roles, seats, billing.
- **Billing & tiers docs** — new page covering 5 tiers, Stripe integration, beta mode.

### Changed
- **CLI reference** — added ~15 missing commands (project *, doctor, init, security, mcp install/uninstall).
- **MCP docs** — all 12 tools documented in 3 categories with full parameter tables.
- **API reference** — added 16 missing endpoints across auth, sessions, share links, handoffs, settings.
- **Quickstart** — added `sfs init` wizard, project knowledge setup, `sfs doctor`.
- **Project context docs** — compile workflow, auto-narrative, knowledge loop integration.
- **Environment vars** — added Stripe, URL, and storage/retention variables.
- **Docs landing** — "memory layer" tagline, knowledge base and dashboard cards, 70+ commands.
- **Site sidebar** — Knowledge Base in Features, new Platform section (Dashboard, Organizations, Billing).
- **Pro tier features** — added "Living knowledge base", "Agent write-back (MCP)", "Project context + wiki".

### Fixed
- `sfs project ask` URL-encodes search queries (spaces in questions broke API calls).
- `sfs project ask` uses keyword-based search instead of exact phrase matching.
- Site knowledge base page: corrected tool names (`update_wiki_page`, `list_wiki_pages`).
- Site footer tagline: "Portable AI coding sessions" → "Memory layer for AI coding agents".
- Docs: removed nonexistent `POST /auth/login` endpoint from self-hosted guide.
- Docs: DLP and enterprise features marked as planned/coming (not shipped).
- Docs: resume `--in` targets corrected to 4 bidirectional tools only.

## [0.9.7.2] - 2026-04-06

### Added
- **Command palette search** — Cmd/Ctrl+K shortcut, arrow key navigation, grouped results, ARIA combobox semantics.
- **Mobile nav drawer** — slide-in hamburger menu with all navigation links, backdrop, ARIA dialog.
- **Mobile search filters** — sheet-style filter panel with staged changes, apply/clear, active filter chips.
- **Focus trapping** — shared `useFocusTrap` hook on all 4 modal dialogs.
- **ARIA live regions** — toast notifications, background task status, handoff badges announce to screen readers.
- **Zod form validation** — create project, handoff, login/signup, judge settings forms validated on blur/submit.
- **Shared wordmark component** — consistent SessionFS branding across shell and login.
- **App-level UI tests** — Vitest + Testing Library setup with route-level tests for search, layout, login.

### Changed
- **Code splitting** — all routes lazy-loaded, main bundle 676KB → 262KB, no chunk size warning.
- **Sessions hero** — compact action-oriented layout with prominent Resume button, inline handoff/project chips.
- **Session rows** — stronger titles, dimmer metadata, solid Resume button on hover.
- **Project cards** — health badges (sessions, auto-narrative), name as title, skeleton loading.
- **Project detail tabs** — fade transitions, sliding underline, workflow hints, context tab as hero artifact.
- **Typography** — stronger light-mode contrast, 6-level type scale, tuned brand color.
- **Product identity** — site-aligned typography and tokens, updated shell and login branding.
- Deduplicated `formatBytes`, `getAvatarColor`, `TOOL_COLORS` into shared utils.

### Fixed
- MCP remote server "No read stream writer available" race condition — `asyncio.Event` ensures transport connected before handling requests.

## [0.9.7.1] - 2026-04-06

### Added
- **GitLab settings CRUD** — `GET/PUT/DELETE /api/v1/settings/gitlab` for bootstrapping GitLab MR integration.
- **Watchlist status API** — `PUT /sync/watch/{session_id}/{status}` for daemon sync lifecycle tracking.
- **Handoff metadata snapshots** — session title/tool/model/tokens frozen at handoff creation (migration 021).
- **Knowledge backfill** — creating a project backfills knowledge from already-synced sessions via blob archives.
- 5 new compiler integration tests (repeated compile dedup, unverified promotion, mixed confidence, cross-batch).

### Fixed
- **Billing isolation** — org Team checkout creates separate Stripe customer; webhook handlers use org-first branching; personal-vs-org subscription detection throughout portal/status/checkout.
- **Handoff security** — recipient verification on claim, auth required on detail/summary, claimed summary blocked, recipient session ID persisted and returned from all routes.
- **Knowledge compiler** — content-level dedup (not session-level bail), verified/unverified promotion across compiles, ephemeral section rebuild, low-confidence entries only in Unverified, intra-batch dedup.
- **Sync engine** — conflict re-dirtying, selective watchlist fetched from server, pagination for remote session list, cross-user session-ID reuse blocked, stale file cleanup on pull.
- **Admin dashboard** — user_id→id contract fix, activity tab endpoint/fields, self-demotion guard, search param, enterprise/starter tiers, org membership cleanup on delete.
- **MCP install** — stale registration repair, malformed config handling, timeout isolation, UnicodeDecodeError handling.
- **Share links** — passwords moved from GET query to POST body, PBKDF2 hashing with salt, configurable API URL, alias-aware revoke.
- **Webhooks** — GitHub signature enforcement when secret configured, GitLab route path fix (`/webhooks/gitlab` → `/gitlab`), GitLab MR comment dedup tracking, per-user webhook secret verification.
- **Dashboard** — compile refreshes all project/wiki queries, search shows aliases, quick preview uses newest-first, jump-to-message always forces oldest-first, message order resets on session change, mobile nav for Billing/Admin, accessible dialogs, keyboard support, billing error display.
- **Org management** — seat capacity re-checked on invite acceptance, invite email normalization, signup email normalization.
- Database migrations 020–023.

### Security
- Share-link passwords no longer exposed in URLs (moved to POST body).
- Share-link password storage upgraded from SHA-256 to PBKDF2-HMAC-SHA256 with random salt.
- GitHub webhooks reject missing signature when secret is configured.
- Handoff detail/summary endpoints require authentication; restrict access to sender/recipient.
- Claimed handoff summaries return 410 (no data leakage after claim).
- Cross-user session-ID reuse blocked on sync push.
- Same-user session-ID reuse expires pending handoffs and revokes share links.
- Org non-admin members cannot access org Stripe portal.

## [0.9.7] - 2026-04-04

### Added
- **Living Project Context** — auto-summarize on sync, knowledge entries (6 types), structured compilation, section pages, concept auto-generation, regenerate.
- **3 MCP write tools** — `add_knowledge`, `update_wiki_page`, `list_wiki_pages` (12 MCP tools total).
- **MCP search tools** — `search_project_knowledge`, `ask_project`.
- **Wiki pages** — `knowledge_pages` + `knowledge_links` tables, multi-document structure, backlinks.
- **Auto-narrative toggle** per project (runs LLM narrative on sync when enabled).
- **Self-healing SQLite index** — auto-deletes corrupted `index.db`, rebuilds from `.sfs` files.
- **`handle_errors` decorator** on all CLI commands (no raw tracebacks).
- **`sfs doctor`** — 8 health checks with auto-repair.
- **Message pagination** — newest-first default, order toggle, sidechain/empty filtering.
- **Multi-select bulk delete + Find Duplicates** in session list.
- **Delete session from session detail.**
- **CLI commands** — `sfs project compile`, `entries`, `health`, `dismiss`, `ask`, `pages`, `page`, `regenerate`, `set`.
- Database migration 018: `knowledge_entries` + `context_compilations`.
- Database migration 019: `knowledge_pages` + `knowledge_links` + `auto_narrative`.

### Changed
- `get_project_context` MCP tool includes wiki pages + pending entries + contribution instructions.
- Compilation creates separate section pages per entry type.
- Dashboard: Knowledge Entries, Pages, History tabs on project detail.
- Session titles skip system/developer prompts, filter "You are " prefix.
- Hide `<synthetic>` model and zero token counts from dashboard.
- Billing page handles API errors gracefully.
- Footer version v0.9.7.
- Project cards show session count.
- Project context renders as formatted markdown (ReactMarkdown).

### Fixed
- Search endpoint uses `check_feature()` not raw `user.tier` (Baptist Health fix).
- Cross-tool resume: full transcript via `--append-system-prompt-file`, CWD-based scope.
- Codex watcher skips `sessionfs_import` sessions.
- All Sessions count uses real API total (not folder count).
- Bulk delete reports partial failures.
- Stronger duplicate detection key (includes etag).
- Project access control on all knowledge + wiki routes (security fix).
- 6 API/dashboard contract mismatches fixed.
- Bookmark icon state wired through prop chain.

### Security
- Project-level access control on all 13 knowledge + wiki routes.
- `dismiss_entry` route access check added.

## [0.9.6] - 2026-03-30

### Added
- **Self-hosted license lifecycle** — migration 017, grace period state machine (valid → warning → degraded → 403), validation logging, admin CLI commands (`sfs admin create-trial`, `sfs admin create-license`, `sfs admin list`, `sfs admin extend`, `sfs admin revoke`), admin API (CRUD + history), Helm local mode (seed job with retry + cache fallback), dashboard licenses tab.
- **Dashboard full redesign** — light/dark mode with proper contrast, resume-first layout replacing analytics cards, date-grouped sessions, lineage grouping (collapsible follow-ups), left rail navigation, page transitions, toast notifications, skeleton loading.
- **Narrative session summaries** — `POST /summary/narrative` generates what_happened, key_decisions, outcome, open_issues from session data via LLM. Dashboard SummaryTab has "Generate Narrative" button. Pro+ tier gated.
- **Project Context dashboard page** — list, detail, markdown editor, create/delete from the web UI.
- **Cross-tool resume improvements** — full conversation transcript via `--append-system-prompt-file`, 50-message trim + handoff context for better continuity.
- **MCP install for all 8 tools** — was 3, now supports all tools. Codex uses `codex mcp add`, Gemini uses `gemini mcp add`.
- **`sfs init` wizard** — auto-detects all 8 tools, optional sync setup during first run.
- **`sfs security scan/fix`** — config permissions audit, API key exposure check, dependency audit.
- **Skill/slash command detection** — recognized across all converters for accurate tool call counts.
- **Multi-select bulk delete** — select and delete multiple sessions from dashboard.
- **Find Duplicates** — dashboard feature to identify duplicate sessions.
- **Delete session from detail** — remove a session directly from the session detail page.
- **Enterprise page** — rewritten for AI governance messaging.
- **Feature page** — `/features/project-context/` deep-dive.
- **Cross-site theme consistency** — dashboard passes `?theme=` parameter to marketing site.
- **Handoff UX** — status stepper and session context card for clearer handoff flow.

### Changed
- LLM Judge revamped — confidence scores (0-100) per finding, CWE mapping (CWE-393, CWE-684, CWE-1104, etc.), evidence linking, dismiss/confirm findings with reason tracking.
- Dashboard analytics section replaced with resume-first hero section.
- Tool call capture fixed for Gemini CLI (reads `toolCalls` array) and Amp (parses `tool_use`/`tool_result` blocks).
- Audit now warns when a session has 0 tool calls.
- Session deduplication — Codex watcher skips `sessionfs_import` sessions.
- Search endpoint fixed to use effective org tier instead of raw `user.tier`.
- 958 tests passing.

### Security
- **Security pipeline** — GitHub Action running pip-audit, Trivy container scanning, and Bandit static analysis on every push.
- **Dependabot** enabled for automated dependency update PRs.
- **SECURITY.md** — responsible disclosure policy published.

## [0.9.5] - 2026-03-30

### Added
- **FSL licensing** — Dual-license model: MIT core (`src/sessionfs/`) + FSL-1.1-Apache-2.0 enterprise (`ee/sessionfs_ee/`). Following PostHog/Sentry pattern.
- **Server-side tier gating** — 5 tiers (Free, Starter, Pro, Team, Enterprise) with 30+ gated features. `require_feature()` middleware on sync, handoff, audit, summary, and project endpoints. 403 responses include `upgrade_url` and `required_tier`.
- **RBAC** — Organizations with admin/member roles. Effective tier resolution (org tier for team users, user tier for solo). Permission checks on billing, invite, and settings endpoints.
- **Organization management** — `POST/GET /api/v1/org`, invite via email with 7-day expiry, accept/revoke invites, change roles, remove members, seat limit enforcement.
- **Stripe billing** — Checkout sessions, Customer Portal, webhook handler (checkout.completed, subscription.updated/deleted, invoice.payment_failed), idempotent event processing.
- **Helm license validation** — `POST /api/v1/helm/validate` endpoint. Init container validates license key on pod start. License tier determines feature availability.
- **Telemetry endpoint** — `POST /api/v1/telemetry` with PII rejection. Opt-in, anonymous usage data collection.
- **Client version tracking** — Sync client sends `X-Client-Version`, `X-Client-Platform`, `X-Client-Device` headers. Dashboard shows version with update prompt when outdated.
- **CLI org commands** — `sfs org info`, `sfs org create`, `sfs org invite`, `sfs org members`, `sfs org remove`.
- **CLI upgrade prompts** — Friendly messages when API returns 403 for tier/role/storage limits.
- **Dashboard billing page** — Tier comparison cards, Stripe Checkout redirect, Customer Portal link, storage usage meter.
- **Dashboard org management** — Members table, invite form, role switching, pending invites list. Admin-only controls hidden for members.
- **Dashboard version widget** — Settings page shows last synced package version, platform, device, and "Update available" badge when outdated.
- Database migration 016: users billing columns, organizations, org_members, org_invites, stripe_events, helm_licenses, telemetry_events.

- **LLM Judge revamp** — Confidence scores (0-100) per finding, evidence snippets with source references, CWE category mapping (CWE-393, CWE-684, CWE-1104, etc.), claim-evidence linking in judge prompt.
- **Audit dismiss/confirm** — Human-in-the-loop: dismiss findings as false positives with reason, un-dismiss, dismissed findings section in dashboard. `POST /audit/dismiss` endpoint persists to blob storage.
- **Narrative session summaries** — LLM-powered: `POST /summary/narrative` generates what_happened, key_decisions, outcome, open_issues from session data. Reuses judge provider system (BYOK). Pro+ tier gated. Dashboard SummaryTab has "Generate Narrative" button.
- **Dashboard analytics cards** — Session list shows: Sessions Today (with yesterday comparison), Tool Breakdown (color bar + top 3), Total Tokens, Peak Hours. Computed client-side from loaded data.
- **Resume preview** — Session detail shows last 3 messages, files touched, and "Resume in [tool]" copy button before tab content.
- **MCP tools expanded** — `get_session_summary` and `get_audit_report` tools added (now 7 total). Summary returns deterministic stats; audit returns trust score, findings, confidence.

### Changed
- Default test user tier set to "pro" (was "free") to reflect pre-gating behavior.
- Sync client sends platform and device info alongside version in request headers.
- `/api/v1/auth/me` returns `last_client_version`, `last_client_platform`, `last_client_device`, `last_sync_at`, `latest_version`.
- Judge prompt restructured: each claim paired with its relevant evidence instead of flat dump.
- AuditTab.tsx rewritten with unified FindingCard component, confidence bars, evidence viewer, dismiss buttons.
- Export formats (Markdown, CSV) include confidence and CWE columns.
- 921 tests passing.

## [0.9.1] - 2026-03-28

### Fixed
- **Handoff resume crashes on receiver's machine** — falls back to CWD when sender's path doesn't exist instead of `FileNotFoundError`
- **SQLite "database locked" errors** — added `busy_timeout=5000ms` to session index and MCP search index
- **Audit modal ignores custom base URL** — `runAudit` now passes `base_url` through the full chain (API client, BackgroundTasks, server)
- **Model discovery fails after saving settings** — server-side `/judge/models` endpoint now uses saved encrypted API key as fallback
- **Re-audit page shows hardcoded models** — AuditModal now loads `base_url` from saved settings and runs model discovery with dropdown
- Added "Test Connection" button to AuditModal for custom base URLs

## [0.9.4] - 2026-03-30

### Added
- **Session lineage** — `parent_session_id` tracked through resume, sync push, and API. `sfs show` displays parent/children. `sfs list --tree` groups by lineage. Dashboard shows fork banner and "↩ fork" badges.
- **Cursor tool call extraction** — reads `agentKv:blob:` layer from Cursor's SQLite DB. Sessions go from 0 to 900+ tool calls, making audit functional for Cursor.
- **Deploy pipelines** — GitHub Actions for Dashboard (Vercel) and Site (Vercel), triggered on push to main when respective dirs change.
- **Product site** — Astro + Starlight site with 26 pages: home, features (4 deep-dives), pricing, enterprise, changelog, blog placeholder, and 16 Starlight docs.
- `sfs daemon rebuild-index` — rebuilds local SQLite index from .sfs files, backfills `source_tool`.
- `sfs summary --today` — shows table of all sessions captured today.
- `GET /api/v1/summaries?since=&until=` — batch summary API for date range reporting.
- Auto-summarize trigger settings API (`GET/PUT /settings/summarize-trigger`).
- Signup rate limit (5 per IP per hour).

### Fixed
- **Claim extractor for Claude Code** — two-strategy extraction (tool-context + standalone regex). 0 claims → 54+.
- **Auto-audit wiring** — `on_sync` trigger now calls audit API after push. `on_pr` checks user setting in webhook handler.
- **GitLab webhook DB session** — uses `_session_factory` instead of non-existent `app.state.db_session_factory`.
- **Daemon settings poll** — async `httpx.AsyncClient` instead of blocking sync `httpx.get`.
- **SummaryTab crash** — moved `useState` hooks before conditional returns (React hooks rules).
- **Audit modal ignores base URL** — `runAudit` now passes `base_url` through full chain.
- **Model discovery with saved key** — server falls back to user's encrypted key when no explicit key provided.
- **Handoff resume path** — falls back to CWD when sender's path doesn't exist on receiver's machine.
- **SQLite "database locked"** — `busy_timeout=5000ms` on session index and MCP search index.
- **`sfs config show`** — Rich markup no longer swallows TOML `[section]` headers.
- **`sfs summary --today`** — checks both local and UTC dates, checks `updated_at` too.
- Evidence truncation raised from 200 to 2000 chars.
- Proper error responses: 422 for 0 claims with tool calls, 502 for LLM errors, 504 for timeouts.

### Security
- **Config file permissions** — `config.toml` and `daemon.json` now `chmod 600` after write (was world-readable).
- **pip-audit in CI** — Python dependency vulnerability scanning on every push.
- **Trivy on GHCR images** — all 3 published images (API, MCP, Dashboard) scanned for CRITICAL/HIGH CVEs.
- **Signup rate limit** — 5 signups per IP per hour prevents account enumeration.

### Changed
- Landing page replaced with Astro + Starlight product site (26 pages).
- README commands table expanded to 34 entries.
- CLI reference expanded to 37+ command sections.
- 848 tests passing.

## [0.9.3] - 2026-03-28

### Added
- **LLM Judge V2** — severity-classified findings (critical/high/low), category detection (test_result, file_existence, command_output, etc.), auto-assigned from category
- **Audit history** — every audit persisted in `audit_reports` table with model, provider, scores, findings
- **Audit history API** — `GET /api/v1/sessions/{id}/audits` (list), `GET /api/v1/audits/{id}` (detail)
- **Auto-audit trigger** — configurable: manual, on_sync, on_pr. Dashboard radio buttons + `audit_trigger` on users
- **GitLab webhook** — `POST /webhooks/gitlab` for merge request events, posts AI context comments on MRs
- **GitLab client** — cloud + self-hosted support, encrypted token storage
- **Session summarization** — deterministic extraction: files modified/read, commands, test results (pytest/jest/go test), packages, errors, duration
- `sfs summary <id>` CLI command with `--format md` for markdown export
- Dashboard Summary tab with metric cards, files, activity, errors sections
- `GET/POST /api/v1/sessions/{id}/summary` API endpoints
- PR/MR comments now include contradictions table when audit exists
- Updated judge prompt to return category alongside verdict
- `audit_reports`, `gitlab_settings`, `session_summaries` database tables (migrations 014-015)

### Fixed
- **Claim extractor for Claude Code sessions** — new two-strategy extraction (tool-context + standalone regex) produces 50+ claims from sessions that previously returned 0
- Evidence truncation raised from 200 to 2000 chars
- Proper error responses: 422 for 0 claims with tool calls, 502 for LLM HTTP errors, 504 for timeouts

### Changed
- Landing page updated for v0.9.3 — Judge V2 spotlight, Session Summary spotlight, Enterprise section, 8-card feature grid, fixed handoff command
- Judge prompt returns `category` instead of `severity` — severity auto-assigned deterministically
- Dashboard AuditTab redesigned — contradictions-first, metric cards, collapsible verified/unverified, audit history

## [0.9.0] - 2026-03-28

### Added
- **Autosync** — automatic session sync to cloud with three modes: off (default), all, or selective
- `sfs sync auto --mode all|selective|off` — set autosync mode
- `sfs sync watch <id>` / `sfs sync unwatch <id>` — manage selective watchlist
- `sfs sync watchlist` — show watched sessions with sync status
- `sfs sync status` — show mode, counts, storage, queued/failed
- API endpoints: `GET/PUT /api/v1/sync/settings`, `GET /api/v1/sync/watchlist`, `POST/DELETE /api/v1/sync/watch/{id}`, `GET /api/v1/sync/status`
- Dashboard autosync settings (radio buttons for off/all/selective)
- Dashboard API client methods for sync settings, watchlist, and status
- Daemon debounces session changes before pushing (30s default, configurable)
- Daemon polls API for settings changes every 60 seconds
- `sync_watchlist` database table for selective mode tracking
- `sync_mode` and `sync_debounce` columns on users table

### Fixed
- **Handoff claim now copies session data** — blob is duplicated in storage, new session record created for recipient (was only recording `claimed_at` without copying)
- **Ingress routes all traffic through dashboard nginx** — removed separate `/api` and `/mcp` ALB target groups that stayed in "unused" state on EKS
- **Nginx body size limit** — added `client_max_body_size 100m` (configurable via `dashboard.clientMaxBodySize`) to prevent 413 on large session uploads
- MCP proxy added to dashboard nginx for internal routing

## [0.8.3] - 2026-03-27

### Added
- `SFS_SMTP_VERIFY_SSL` — disable SSL certificate verification for internal SMTP relays with self-signed certs
- Model discovery endpoint `GET /api/v1/settings/judge/models` — queries OpenAI-compatible `/v1/models` to list available models
- Dashboard auto-discovers models when custom base URL is set (dropdown instead of hardcoded list)

### Fixed
- Handoff email showed wrong command (`sfs pull --handoff`) — corrected to `sfs pull-handoff`
- CLI handoff hint showed same wrong command
- LLM Judge returned 403 when using custom base URL without API key (Ollama, local vLLM) — API key is now optional when `base_url` is set
- SMTP email logger not visible in uvicorn container logs — propagated to root logger

## [0.8.2] - 2026-03-27

### Added
- **Custom base URL for LLM Judge** — `--base-url` flag, `SFS_JUDGE_BASE_URL` env var, and dashboard settings field for LiteLLM, vLLM, Ollama, Azure OpenAI, and any OpenAI-compatible gateway
- **Shared project context** — `sfs project init|edit|show|set-context|get-context` CLI commands, REST API, and MCP `get_project_context` tool for team-shared instructions
- Project context database migration (projects table)
- Judge base URL database migration (user_judge_settings.base_url column)
- `docs/project-context.md` guide

### Fixed
- **Codex resume broken** — rollout files used `source: "custom"` which Codex CLI rejects on resume; changed to `source: "cli"` with `originator: "codex_cli_rs"`
- Codex rollout missing `output`/`exit_code` on `local_shell_call` payloads and `status` on `function_call` payloads
- Codex rollout had extra fields (`id`, `end_turn`) on message payloads that native format doesn't use
- `sfs resume --in codex` hint said `codex --resume` (wrong flag) then `codex --thread` (wrong flag) — Codex uses `codex resume` subcommand

### Changed
- `sfs resume` now auto-launches the target tool instead of printing a copy-paste command:
  - Claude Code: `claude --resume <id>`
  - Codex: `codex resume <uuid>`
  - Gemini: `gemini --resume latest`

## [0.8.1] - 2026-03-26

### Fixed
- Session ID validation now accepts 8-40 character IDs (was 12-20) — fixes sync rejection for short-form IDs like `ses_ae7652a4`
- Rate limiter now respects `SFS_RATE_LIMIT_PER_MINUTE` environment variable — was hardcoded to 100, now reads config (default 120, set to 0 to disable)
- Removed `?sslmode=require` from Helm database URL template — asyncpg handles SSL internally
- MCP service port corrected from 3001 to 8080 in Helm chart — fixes pod crash-loops from failed health probes
- Dashboard no longer hardcodes `api.sessionfs.dev` — uses `VITE_API_URL` env var, falls back to `window.location.origin`
- Dashboard health endpoint returns JSON `{"status":"ok"}` instead of plain text
- S3 bucket names containing `/` (e.g. `my-bucket/prefix`) are split gracefully into bucket + prefix

### Added
- Request logging middleware — all 4xx responses logged at WARNING level with method, path, status, and client IP
- Structured logging for session ID validation failures
- Rate limit rejection logging with client IP and configured limit
- `SFS_S3_PREFIX` environment variable for S3 key prefixes
- `storage.s3.prefix` in Helm values.yaml
- Dashboard nginx ConfigMap in Helm chart — proxies `/api/` to API service for self-hosted deployments
- GHCR dashboard image now built with `VITE_API_URL=https://api.sessionfs.dev`
- `docs/troubleshooting.md` — error responses, session ID format, sync debugging, Kubernetes issues
- IRSA IAM policy example in `docs/self-hosted.md`
- Rate limit configuration docs in `docs/self-hosted.md`

### Changed
- Default rate limit increased from 100 to 120 requests per minute per API key
- Session ID regex changed from `[a-zA-Z0-9]{12,20}` to `[a-z0-9]{8,40}` (lowercase only, wider range)
- `externalDatabase.sslMode` removed from Helm values.yaml (was causing asyncpg errors)

## [0.8.0] - 2026-03-26

### Added
- **`sfs storage`** — shows local disk usage with progress bar, session counts, retention policy
- **`sfs storage prune`** — prune old sessions with `--dry-run` and `--force` flags
- **`sfs daemon restart`** — restart daemon in one command
- **Multi-provider email** — SMTP support alongside Resend, with auto-detection and null provider for air-gapped deployments
- **`SFS_REQUIRE_EMAIL_VERIFICATION`** — disable email verification for internal deployments
- **Gemini model extraction** — reads model name from `logs.json` per session (no more blank Model column)
- **Environment variables reference** — `docs/environment-variables.md` with all `SFS_*` vars
- Gemini 3.1 Pro, 3 Pro, 3 Flash model abbreviations in CLI

### Changed
- Daemon auto-prunes synced sessions hourly based on retention policy (90-day default, 30-day for synced)
- Daemon warns at 80% storage, pauses capture at 95%
- Default local storage cap: 2 GB
- Helm chart security contexts now configurable via values.yaml (no more hardcoded `runAsNonRoot: true`)
- Dashboard nginx runs as non-root on port 8080 (fixes permission errors on EKS)
- Migration job uses `SFS_DATABASE_URL` (was `DATABASE_URL`) and async driver
- Email section in Helm values.yaml supports Resend, SMTP, existing secrets, and provider selection
- `sfs watcher enable/disable` reuses `sfs daemon restart` instead of duplicating logic

### Fixed
- Migration job falling back to SQLite due to env var mismatch (`DATABASE_URL` vs `SFS_DATABASE_URL`)
- asyncpg crash on `?sslmode=require` in database URL — SSL params now handled internally
- Dashboard nginx `chown` permission errors on EKS (`/var/cache/nginx/client_temp`)
- Helm migration URL using sync `postgresql://` driver instead of `postgresql+asyncpg://`
- Gemini sessions showing blank model name in `sfs list`

## [0.7.1] - 2026-03-25

### Fixed
- Landing page rendering raw JavaScript — duplicate script block appended after `</html>`
- Container image publishing to GHCR never triggered — workflow used `release: published` event which doesn't fire from `GITHUB_TOKEN`; changed to `workflow_run` trigger

## [0.7.0] - 2026-03-25

### Added
- **`sfs watcher list`** — shows all 8 tools with enabled/disabled status and install detection
- **`sfs watcher enable/disable`** — toggle tool watchers with automatic daemon restart

### Changed
- Landing page scroll animations visible by default (progressive enhancement)
- Stars badge moved from nav to footer
- Waitlist CTA now functional (mailto)

### Fixed
- Content invisible on first load due to fade-up animation gating visibility
- GitHub App secrets wired to Cloud Run via Terraform

## [0.6.0] - 2026-03-25

### Added
- **GitHub PR AI Context App** — automatically comments on PRs with linked AI sessions, tools, trust scores
- **Webhook handler** at `/webhooks/github` with HMAC-SHA256 signature verification
- **Git metadata indexing** — sessions store normalized remote, branch, commit for fast PR matching
- **PR comment builder** — single and multi-session markdown with dashboard links
- **Installation management** — toggles for auto-comment, trust scores, session links
- **"What is this?" page** — `docs/github-app.md` conversion funnel for PR reviewers
- **Jump to message** in audit findings — switches to Messages tab at correct page
- **Bookmark folders** — create colored folders, bookmark sessions, filter by folder
- **Background audit** — large sessions return 202, floating toast indicator while processing
- **Numbered page navigation** for messages (1, 2, 3... not just Next)
- **Helm chart** for Kubernetes self-hosted deployment
- **GHCR image publish pipeline**

### Fixed
- Search SQL asyncpg ambiguous parameter types
- Cursor parser NULL bubble values
- Audit timeout on large sessions (10 claim-dense chunks max)
- Messages pagination exceeding server 100 limit
- Missing JSONResponse import in audit route

### Security
- `*.pem` added to gitignore
- `github-app-manifest.json` added to private files
- `.vercel/` directories blocked from commits

## [0.5.0] - 2026-03-25

### Added
- **Bookmark folders** — create colored folders, bookmark sessions, filter session list by folder
- **Background audit** — large sessions (500+ msgs) return 202 and run in background with floating toast indicator
- **Helm chart** — production Kubernetes deployment (API, MCP, Dashboard, PostgreSQL, migrations, ingress, network policies, HPA)
- **Dashboard Dockerfile** — containerized dashboard for self-hosted deploy
- **GHCR publish pipeline** — builds and pushes api/mcp/dashboard images on release
- **Audit status endpoint** — `GET /audit/status` for polling background audits
- **Self-hosted docs** — `docs/self-hosted.md` with AWS/GCP/generic K8s examples

### Fixed
- Search SQL ambiguous parameter types on PostgreSQL (asyncpg)
- Cursor parser crash on NULL bubble values in SQLite
- Audit timeout on large sessions (now capped at 10 claim-dense chunks)
- Audit modal closes immediately — no more blocking the UI

## [0.4.0] - 2026-03-24

### Added
- **Session aliases** — `sfs alias ses_d20e auth-debug` then use `auth-debug` everywhere (show, push, pull, resume, search)
- **Created date column** in dashboard session list
- **Server-side pagination** in dashboard (Previous/Next)
- **Landing page redesign** — 10 sections, glass-morphism, scroll animations, free-during-beta pricing
- **Logo PNGs** — portal mark at 512/256/128px

### Changed
- GitHub org migrated from `alwaysnix` to `SessionFS`
- Dashboard readability: brighter colors (#f0f3f6/#d0d7de), 16px base, #0d1117 background
- LLM providers: correct model IDs, reasoning model support, Google auth header
- develop branch is LOCAL ONLY with pre-push hook

### Fixed
- Cursor parser NULL bubble values
- Sync duplicate key race conditions (5 fixes)
- Daemon crash on status write
- OpenRouter response_format rejection
- Admin user list showing deleted users
- Dashboard pagination breaking after first page

### Security
- Deleted develop from public origin (was accidentally pushed)
- Pre-push hook prevents future develop pushes

## [0.3.3] - 2026-03-24

### Added
- **Landing page redesign** — 10 sections, asymmetric hero, glass-morphism, scroll animations
- **Free during beta** pricing — replaced 4-tier grid with beta message
- **Logo PNGs** — 512, 256, 128px versions of the portal mark

### Changed
- GitHub org migrated from `alwaysnix` to `SessionFS`
- All repo URLs, WIF bindings, landing page links, pyproject.toml updated
- Dashboard readability: background `#0d1117`, text `#f0f3f6`/`#d0d7de`, base 16px, all text-xs→text-sm
- Landing readability: body text `#c9d1d9`, terminal 14px min, feature descriptions 16px
- LLM providers: OpenAI `max_completion_tokens`, reasoning models use effort not temperature, Google `x-goog-api-key` header, correct model IDs
- `develop` branch is now LOCAL ONLY with pre-push hook protection

### Fixed
- OpenRouter 400: removed unsupported `response_format`
- OpenAI reasoning models (o3/o4-mini) rejecting `temperature` parameter

### Security
- Deleted `develop` from public origin (was accidentally pushed with internal files)
- Pre-push hook blocks future develop pushes

## [0.3.2] - 2026-03-24

### Added
- **Admin dashboard** — user management, tier control, system stats, audit log
- **Brand identity** — portal logo, color system (#4f9cf7/#3ddc84/#0a0c10), favicon, social preview
- **Tier-based sync limits** — 50MB free, 300MB paid/admin (was flat 10MB)

### Fixed
- Daemon crash on status file write (FileNotFoundError now caught)
- Daemon main loop catches all exceptions (no more crash on transient errors)
- Sync duplicate key race condition (concurrent push from daemon)
- Sync MissingGreenlet from broken async rollback (replaced with upfront check)
- Sync soft-deleted sessions blocking new pushes (reuse deleted rows)
- Sync missing If-Match header on first push (header now optional)
- Admin user list showing deactivated users (now filtered)
- Ruff lint errors for CI green

## [0.3.1] - 2026-03-24

### Added
- **Audit report export** — download as JSON, Markdown, or CSV (CLI `--format` + dashboard dropdown)
- **OpenRouter provider** — access 400+ models via single API key, auto-detected by `/` in model name
- **Stored judge API keys** — Fernet-encrypted at rest, Settings page in dashboard
- **Audit status indicators** — trust score badges on session list, detail sidebar, handoff emails, CLI list
- **`GET /api/v1/auth/me`** — returns user profile (email, tier, verified status)
- **Latest model dropdowns** — Opus 4.6, GPT-5.4, Gemini 3.1, DeepSeek V3.2/R1, o3, o4-mini

### Fixed
- Handoff inbox/sent routes returning 404 (wildcard route was catching them)
- Audit using hardcoded blob path instead of session.blob_key
- UTF-8 decode error when reading session archives with non-ASCII content
- AuditReport type not imported in dashboard AuditTab
- CLI `sfs auth status` now shows email, tier, and verification status

## [0.3.0] - 2026-03-23

### Added
- **Team handoff with email notifications** — `sfs handoff --to email --message` sends notification via Resend with session metadata, git context, and pull instructions
- **Smart workspace resolution** — when pulling a handoff, automatically finds the recipient's local clone by matching git remote URLs
- **Handoff dashboard** — inbox/sent tabs, detail page with claim button, handoff modal on session detail
- **LLM-as-a-Judge hallucination detection** — `sfs audit` evaluates AI responses against tool call evidence (BYOK — user provides their own API key)
- **Multi-provider judge** — supports Anthropic, OpenAI, Google via httpx (no SDK dependencies)
- **Consensus mode** — `sfs audit --consensus` runs 3 passes, reports only where 2+ agree
- **Trust score badges** — session list shows green/yellow/red audit badges
- **Audit dashboard tab** — expandable findings with verdict icons, severity badges, evidence
- **Compatibility guide** — `docs/compatibility.md` with full 8-tool matrix and technical reasons for capture-only
- **Remote MCP server** — `mcp.sessionfs.dev` with OAuth 2.1 PKCE + Dynamic Client Registration

### Changed
- Pricing: free tier changed from 25-session count to 14-day rolling retention
- Capture-only CLI messages now include tool-specific reasons and Copilot in alternatives
- Quickstart rewritten as 5-step hero workflow
- README restructured: hero workflow first, advanced features below
- Judge uses temperature=0 for deterministic output
- Judge verdict rules tightened: hallucination requires proof of contradiction, absence of evidence is unverified

### Fixed
- Dashboard auth persistence via sessionStorage (survives refresh)
- Vercel SPA routing (catch-all rewrite for direct URL access)
- Integer overflow on token counts (bigint migration 004)
- Duplicate search bar removed from session list
- All lint errors fixed (ruff clean, mypy clean)
- Node.js 24 opt-in for GitHub Actions
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-03-23

### Added
- **Copilot CLI support** — full capture and resume via events.jsonl injection
- **Amp support** — session capture from Sourcegraph Amp threads (capture-only)
- **Cline support** — session capture from VS Code extension storage (capture-only)
- **Roo Code support** — session capture from VS Code extension storage (capture-only)
- **MCP server** — 4 tools for AI tool integration (search, context, list, find related)
- **Full-text search** — PostgreSQL FTS for cloud, SQLite FTS5 for local CLI
- **Dashboard search** — search bar with instant results and full results page
- **Session search CLI** — `sfs search` with local and cloud modes
- **MCP install command** — `sfs mcp install --for claude-code|cursor|copilot`
- **Email verification** — gates cloud sync until email verified
- **Rolling retention** — free tier: 14-day cloud retention, Pro: unlimited
- **Share links** — 24h default expiry with optional password
- **10MB sync limit** — clear error with guidance to compact
- **GCS blob store** — Google Cloud Storage backend for production
- **Cloud Run deployment** — api.sessionfs.dev live on GCP
- **GitHub Actions CI/CD** — deploy pipeline with Trivy vulnerability scanning
- **Terraform infrastructure** — separate repo (sessionfs-infra) with plan-on-PR, apply-on-merge

### Changed
- Messaging overhaul: retired "Dropbox" analogy, new tagline "Stop re-prompting. Start resuming."
- Version sourced from pyproject.toml (single source of truth)
- SFS format version decoupled from package version

### Fixed
- Personal paths sanitized from spec examples
- .gitignore safety nets for internal files

## [0.1.0] - 2026-03-22

### Added
- Initial public release
- Claude Code session capture and resume
- Codex CLI session capture and resume
- Gemini CLI session capture and resume
- Cursor IDE session capture (capture-only)
- Background daemon with filesystem event watching
- CLI for browsing, exporting, forking, checkpointing sessions
- Cloud sync with push/pull and ETag conflict detection
- Self-hosted API server (FastAPI + PostgreSQL + S3)
- Web dashboard for session management
- Secret detection and path traversal protection
