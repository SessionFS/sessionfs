/**
 * Plain-language definitions for the product terms the dashboard shows.
 *
 * One entry per term. Rules (enforced by glossary.test.ts):
 * - `id` is unique, lowercase kebab-case.
 * - `definition` is one or two short sentences, at most 220 characters,
 *   saying what the thing is and why it matters. Don't restate the label.
 * - `docs`, when set, is a page under site/src/content/docs (without the
 *   extension); it links to https://sessionfs.dev/<docs>/.
 *
 * Every definition was checked against the site docs or the code. If a
 * behaviour changes, change the definition in the same commit.
 */
export interface GlossaryEntry {
  id: string;
  term: string;
  definition: string;
  docs?: string;
}

export const DOCS_BASE = 'https://sessionfs.dev';

export function docsUrl(slug: string): string {
  return `${DOCS_BASE}/${slug.replace(/^\/+|\/+$/g, '')}/`;
}

export const GLOSSARY = [
  // ── Knowledge base ──
  {
    id: 'kb-claim',
    term: 'Claim',
    definition:
      'Knowledge the project treats as true. Active claims are what compile merges into the project context that agents read.',
    docs: 'knowledge-base',
  },
  {
    id: 'kb-evidence',
    term: 'Evidence',
    definition:
      'A raw fact taken from a session or sent by an agent that has not been promoted yet. Compile promotes it to a claim when its confidence is at least 0.5.',
    docs: 'knowledge-base',
  },
  {
    id: 'kb-note',
    term: 'Note',
    definition:
      'An entry that was dismissed or did not pass the quality checks. It stays visible for audit but compile leaves it out.',
    docs: 'knowledge-base',
  },
  {
    id: 'kb-freshness',
    term: 'Freshness',
    definition:
      'How recently an entry was confirmed relevant. Each type has its own window (bugs 30 days, decisions a year); stale entries stop feeding compile until refreshed.',
    docs: 'knowledge-base',
  },
  {
    id: 'kb-compile',
    term: 'Compile',
    definition:
      'Merges pending claims into the project context document and updates the wiki section pages. Agents read the compiled result, not the raw entries.',
    docs: 'knowledge-base',
  },
  {
    id: 'kb-scheduled-compile',
    term: 'Scheduled compile',
    definition:
      'Compiles the project nightly or weekly, or once enough claims are waiting. Scheduled compiles use the deterministic compiler, never a model.',
    docs: 'knowledge-base',
  },
  {
    id: 'kb-rebuild',
    term: 'Rebuild',
    definition:
      'Recompiles the context document from scratch out of every active claim, instead of adding only what is pending.',
    docs: 'knowledge-base',
  },
  {
    id: 'kb-supersede',
    term: 'Supersede',
    definition:
      'Retires an entry in favour of a newer one. The old entry stays readable for audit but no longer feeds compile.',
    docs: 'knowledge-base',
  },
  {
    id: 'kb-refresh',
    term: 'Still valid',
    definition:
      'Confirms a stale entry is still true. Its freshness goes back to current, so it feeds compile again.',
    docs: 'knowledge-base',
  },
  {
    id: 'kb-promote',
    term: 'Promote',
    definition: 'Turns the entry into a claim, so the next compile includes it in the project context.',
    docs: 'knowledge-base',
  },
  {
    id: 'project-context',
    term: 'Project context',
    definition:
      'One document every agent on the project reads, compiled from the knowledge base. Any tool connected through MCP gets the same version.',
    docs: 'project-context',
  },
  {
    id: 'auto-narrative',
    term: 'Auto Narrative',
    definition:
      'On each sync, turns the session summary into knowledge entries, so the project context keeps up without anyone writing it.',
    docs: 'project-context',
  },
  {
    id: 'rules',
    term: 'Rules',
    definition:
      'One instruction record per project, compiled into CLAUDE.md, codex.md, .cursorrules, Copilot instructions and GEMINI.md so every tool is told the same thing.',
    docs: 'rules',
  },

  // ── Agents, tickets and runs ──
  {
    id: 'agent',
    term: 'Agent',
    definition:
      'An AI role, such as reviewer or implementer, with its own instructions. Tickets can be assigned to it, and sessions captured while it works a ticket are tagged with it.',
    docs: 'review-loop',
  },
  {
    id: 'org-agent',
    term: 'Org agent',
    definition:
      'Defined once for the organization and usable in every project in it. A project agent with the same name takes precedence.',
    docs: 'organizations',
  },
  {
    id: 'represents',
    term: 'Represents',
    definition:
      'The org member this agent stands for. That member can edit its role and instructions, and tickets assigned to it show up in their For You.',
    docs: 'organizations',
  },
  {
    id: 'agent-run',
    term: 'Agent run',
    definition:
      'A record of one agent launch: which agent, ticket and tool, its status, findings and token use. Workers and reviewers each get their own runs.',
    docs: 'review-loop',
  },
  {
    id: 'ticket-suggested',
    term: 'Suggested',
    definition:
      'A ticket an agent or a read-only member filed. Someone with edit access approves it before work queues and loops pick it up.',
    docs: 'review-loop',
  },
  {
    id: 'ticket-lease',
    term: 'Lease',
    definition:
      'Starting a ticket takes its lease and raises the lease number. Writes that carry an older number are refused, so a replaced worker cannot overwrite the new one.',
    docs: 'review-loop-guarantees',
  },
  {
    id: 'work-queue',
    term: 'Work queue',
    definition:
      'A set of tickets an agent wakes up to work through. The server keeps the loop state, so the agent resumes where it stopped without chat memory.',
    docs: 'mcp',
  },
  {
    id: 'service-key',
    term: 'Service key',
    definition:
      'An org API key for automation, limited to listed scopes and optionally to some projects. For review independence it counts as the person who created it.',
    docs: 'api',
  },

  // ── Review loop ──
  {
    id: 'review-loop',
    term: 'Review loop',
    definition:
      'Workers build a change from a written brief and an independent trusted reviewer checks each round. Nothing is ready to merge until that reviewer posts VERIFIED-CLEAN.',
    docs: 'review-loop',
  },
  {
    id: 'trusted-reviewer',
    term: 'Trusted reviewer',
    definition:
      'An identity an org admin registered to post verdicts for one agent. Trust comes from the credential that sends the verdict, never from what the comment says.',
    docs: 'review-loop',
  },
  {
    id: 'verified-clean',
    term: 'VERIFIED-CLEAN',
    definition:
      'The verdict that ends a review loop: the trusted reviewer found nothing left to fix. Wording like APPROVED is shown as clean but never ends the loop.',
    docs: 'review-loop',
  },
  {
    id: 'merge-ready',
    term: 'Ready to merge',
    definition:
      'A trusted VERIFIED-CLEAN on the exact commit, from a reviewer who did not implement the ticket, with no harmful finding open.',
    docs: 'review-loop',
  },
  {
    id: 'reviewed-commit',
    term: 'Reviewed commit',
    definition:
      'The full commit id the reviewer read. A clean verdict only counts toward merge while it matches the branch head.',
    docs: 'review-loop',
  },
  {
    id: 'review-round',
    term: 'Review round',
    definition: 'One pass by the reviewer over a commit, recorded as a verdict and its findings.',
    docs: 'review-loop',
  },
  {
    id: 'harmful-finding',
    term: 'Harmful finding',
    definition:
      'A finding tagged with real harm: data exposed, money spent, wrong actor, crash or other. Untagged findings of medium severity or higher also count, and they block merge.',
    docs: 'review-loop',
  },
  {
    id: 'needs-human',
    term: 'Needs a person',
    definition:
      'The item stopped because it hit its token, cost or review-round limit. Nothing more is spent until a person raises the limit or closes the item.',
    docs: 'review-loop',
  },
  {
    id: 'loop-checklist',
    term: 'Checklist',
    definition:
      'Known traps for this project. Every new brief carries them under Known traps; new kinds of finding are proposed here automatically and a person accepts or retires them.',
    docs: 'review-loop',
  },
  {
    id: 'ship-report',
    term: 'Ship report',
    definition:
      'A summary per release or month: loops, review rounds, findings, harmful findings and whether they were fixed before merge, and cost per accepted change.',
    docs: 'review-loop',
  },
  {
    id: 'merge-check',
    term: 'Merge check',
    definition:
      'A pull request check that turns green only when every linked ticket is ready to merge at exactly that commit. Branch protection can require it.',
    docs: 'git-integration',
  },

  // ── Workflows and approvals ──
  {
    id: 'workflow-template',
    term: 'Workflow template',
    definition:
      'A reusable plan for a review loop: the steps, the agent for each, how many run in parallel and the gate each must pass. Your own tools start the agents.',
    docs: 'workflows',
  },
  {
    id: 'workflow-gate',
    term: 'Gate',
    definition:
      'A condition a step must meet before it counts as done, such as an independent trusted VERIFIED-CLEAN or an approval. Later steps stay blocked until it passes.',
    docs: 'workflows',
  },
  {
    id: 'fan-out',
    term: 'Fan-out',
    definition: 'How many parallel tickets a step creates, from 1 to 10.',
    docs: 'workflows',
  },
  {
    id: 'approval-policy',
    term: 'Approval policy',
    definition:
      'A rule naming what needs sign-off, who must approve and how many approvals it takes. Blocking policies hold the work; advisory ones only add a warning.',
    docs: 'approvals',
  },
  {
    id: 'approval-advisory',
    term: 'Advisory',
    definition: 'This policy only warns. The work can go ahead without the approvals.',
    docs: 'approvals',
  },

  // ── For You, meetings, contacts ──
  {
    id: 'for-you',
    term: 'For You',
    definition:
      'One ranked list of what needs you: tickets, approvals, loop items, reminders, action items and questions. Ranking follows fixed rules, not a model.',
    docs: 'for-you',
  },
  {
    id: 'snooze',
    term: 'Snooze',
    definition: 'Hides the item until the time you pick. It comes back then, or sooner if it changes.',
    docs: 'for-you',
  },
  {
    id: 'action-item',
    term: 'Action item',
    definition:
      'Something a person agreed to do, extracted with the lines it came from. Create a ticket from it or link it to an existing one; nothing is linked automatically.',
    docs: 'meetings',
  },
  {
    id: 'extraction',
    term: 'Extraction',
    definition:
      'Reads the transcript with a model key and lists action items, decisions and who spoke. Nothing becomes a ticket until a person creates one.',
    docs: 'meetings',
  },
  {
    id: 'external-contact',
    term: 'External contact',
    definition:
      'Someone outside SessionFS your org attributes work to, such as a partner or vendor. Meetings and tickets can point at them instead of a loose name.',
    docs: 'contacts',
  },
  {
    id: 'people-to-confirm',
    term: 'People to confirm',
    definition:
      'Names from meetings and chats that match no member, agent or contact. A person decides who each one is; extraction never creates contacts.',
    docs: 'contacts',
  },

  // ── Evaluations and lineage ──
  {
    id: 'eval-suite',
    term: 'Suite',
    definition:
      'A versioned test definition: grader, rubric and pass criteria. Every edit makes a new version, and old versions keep their exact cases.',
    docs: 'evals',
  },
  {
    id: 'eval-grader',
    term: 'Grader',
    definition:
      'The credential that posts results for a run. Your CI runs the grading with your keys; SessionFS only records what it sends.',
    docs: 'evals',
  },
  {
    id: 'eval-verdict',
    term: 'Verdict',
    definition:
      'Passed, failed or incomplete, computed by SessionFS from the case results and the suite’s pass criteria. A grader cannot set it.',
    docs: 'evals',
  },
  {
    id: 'eval-trusted',
    term: 'Trusted run',
    definition:
      'Graded by a registered trusted reviewer who did not produce what is being graded. Only trusted runs count toward promotion.',
    docs: 'evals',
  },
  {
    id: 'eval-unverified',
    term: 'Unverified run',
    definition: 'Stored and shown with the reason, but it never counts toward promotion or any other gate.',
    docs: 'evals',
  },
  {
    id: 'eval-hashes-only',
    term: 'Hashes only',
    definition:
      'Case text never reaches SessionFS. Only a key and a SHA-256 hash are stored, and the grader must send the same hash with each result.',
    docs: 'evals',
  },
  {
    id: 'eval-regression',
    term: 'Regression',
    definition: 'A case that passed in the earlier run and fails in this one.',
    docs: 'evals',
  },
  {
    id: 'artifact',
    term: 'Artifact',
    definition:
      'A pointer to one version of a dataset, model, prompt or checkpoint: a URI, an optional digest and tags. SessionFS never stores or fetches the bytes.',
    docs: 'evals',
  },
  {
    id: 'artifact-stage',
    term: 'Stage',
    definition:
      'Candidate, staging, production or archived. Promoting to staging or production needs a trusted passing run of every required suite on that exact version.',
    docs: 'evals',
  },
  {
    id: 'lineage',
    term: 'Lineage',
    definition:
      'Where an artifact came from, as parent-to-child links such as trained_on or derived_from. You or your pipeline record them; SessionFS never infers them.',
    docs: 'lineage',
  },

  // ── Sessions ──
  {
    id: 'handoff',
    term: 'Handoff',
    definition:
      'Sends a session to a teammate with its conversation and workspace state, and optionally a ticket. When they claim it they get their own copy.',
    docs: 'handoff',
  },
  {
    id: 'checkpoint',
    term: 'Checkpoint',
    definition: 'A named snapshot of a session’s state at one moment, which you can fork from later.',
    docs: 'cli',
  },
  {
    id: 'fork',
    term: 'Fork',
    definition: 'A new, independent session that starts from this session’s history.',
    docs: 'cli',
  },
  {
    id: 'trash',
    term: 'Trash',
    definition: 'Sessions removed from the cloud are kept for 30 days and can be restored until then.',
    docs: 'delete-lifecycle',
  },
  {
    id: 'autosync',
    term: 'Autosync',
    definition:
      'Pushes sessions to the cloud without running sfs push: off, every session, or only the ones on your watchlist. Off by default.',
    docs: 'autosync',
  },
  {
    id: 'llm-judge',
    term: 'Audit',
    definition:
      'Checks what the AI claimed against the real tool output, such as "tests pass" when the exit code was 1, and flags each contradiction with evidence.',
    docs: 'judge',
  },
  {
    id: 'session-helper',
    term: 'Session helper',
    definition:
      'Answers questions over the sessions and knowledge you can already read, with citations. It proposes changes as cards and changes nothing until you confirm.',
    docs: 'session-helper',
  },

  // ── Organization and security ──
  {
    id: 'project-group',
    term: 'Project group',
    definition:
      'A set of org projects. A member’s role on the group gives them read or edit access to every project in it.',
    docs: 'project-groups',
  },
  {
    id: 'restricted-visibility',
    term: 'Restricted project visibility',
    definition:
      'Members see only projects they own or reach through a group; org owners and admins still see all. When off, every member sees every org project.',
    docs: 'project-groups',
  },
  {
    id: 'auto-join',
    term: 'Auto-join',
    definition:
      'People who verify an email at this domain join the org as members. Needs a paid plan, and never adds someone who was removed or already invited.',
    docs: 'organizations',
  },
  {
    id: 'project-transfer',
    term: 'Project transfer',
    definition:
      'Moves a project between your personal space and an organization, or between organizations. The receiving side accepts first, unless you are on both sides.',
    docs: 'organizations',
  },
  {
    id: 'dlp',
    term: 'DLP',
    definition:
      'Scans session content for secrets and health data before it syncs. Warn flags findings, Redact replaces them and Block refuses the sync.',
    docs: 'dashboard',
  },
  {
    id: 'sso-enforcement',
    term: 'Enforce SSO',
    definition:
      'Members must sign in through the org’s identity provider. The owner and service keys are exempt, so the org cannot lock itself out.',
  },
  {
    id: 'break-glass',
    term: 'Break-glass grant',
    definition:
      'A one-hour exemption from SSO enforcement for one admin, issued by the owner, so they can still get in when SSO sign-in is blocked.',
  },
  {
    id: 'client-secret-ref',
    term: 'Client secret reference',
    definition:
      'Where the server reads the secret from, such as env:OIDC_SECRET. The secret itself is never pasted here or stored.',
  },
  {
    id: 'jira-two-way-sync',
    term: 'Two-way sync',
    definition:
      'Status changes, field edits and comments flow between linked tickets and their Jira issues, in the directions you choose.',
    docs: 'integrations/jira',
  },

  // ── Marketplace ──
  {
    id: 'held-update',
    term: 'Held update',
    definition:
      'An update that adds access or changes executable content. Devices keep the installed version until it is approved.',
    docs: 'skills-marketplace',
  },
  {
    id: 'widening',
    term: 'Asks for more access',
    definition:
      'The update adds access, such as a capability, hook, MCP server, network endpoint or tool grant. It always needs explicit approval.',
    docs: 'skills-marketplace',
  },
  {
    id: 'verified-publisher',
    term: 'Verified publisher',
    definition:
      'The publisher proved it controls the namespace with a DNS record or a GitHub account or organization.',
    docs: 'skills-marketplace',
  },
  {
    id: 'trusted-publishing',
    term: 'Published from CI',
    definition:
      'Released by a GitHub or GitLab workflow the publisher registered in advance, with no API key stored in the repository.',
    docs: 'skills-marketplace',
  },
  {
    id: 'works-in',
    term: 'Works in',
    definition:
      'Whether each tool’s installer accepted the package in an isolated sandbox. It checks installation, not how well the skill works.',
    docs: 'skills-marketplace',
  },
  {
    id: 'stale-package',
    term: 'Stale',
    definition:
      'No new version for 180 days, and its works-in check regressed or its source repository was archived.',
    docs: 'skills-marketplace',
  },
  {
    id: 'yank',
    term: 'Yank',
    definition: 'Refuses new installs but leaves installed copies in place, with a warning.',
    docs: 'skills-marketplace',
  },
  {
    id: 'block',
    term: 'Block',
    definition:
      'Removes the package from every device. Local edits are kept in a quarantine folder.',
    docs: 'skills-marketplace',
  },
] as const satisfies readonly GlossaryEntry[];

/** Every glossary id; `<Term id>` only accepts these, so a typo fails tsc. */
export type GlossaryId = (typeof GLOSSARY)[number]['id'];

const BY_ID = new Map<string, GlossaryEntry>(GLOSSARY.map((e) => [e.id, e]));

export function getGlossaryEntry(id: string): GlossaryEntry | undefined {
  return BY_ID.get(id);
}
