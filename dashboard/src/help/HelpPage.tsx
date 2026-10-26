import { useEffect, useId, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { getTheme, type Theme } from '../utils/theme';
import { Badge, Button, Card, Input, PageContainer, PageHeader } from '../components/ui';
import { useHelper } from '../helper/HelperProvider';
import mcpToolData from './mcpTools.json';

type Tool = {
  id: string;
  label: string;
  command: string;
};

const TOOLS: Tool[] = [
  { id: 'claude-code', label: 'Claude Code', command: 'sfs mcp install --for claude-code' },
  { id: 'codex', label: 'Codex', command: 'sfs mcp install --for codex' },
  { id: 'gemini', label: 'Gemini CLI', command: 'sfs mcp install --for gemini' },
  { id: 'cursor', label: 'Cursor', command: 'sfs mcp install --for cursor' },
  { id: 'copilot', label: 'Copilot CLI', command: 'sfs mcp install --for copilot' },
  { id: 'amp', label: 'Amp', command: 'sfs mcp install --for amp' },
  { id: 'cline', label: 'Cline', command: 'sfs mcp install --for cline' },
  { id: 'roo-code', label: 'Roo Code', command: 'sfs mcp install --for roo-code' },
  { id: 'kilo-code', label: 'Kilo Code', command: 'sfs mcp install --for kilo-code' },
  { id: 'antigravity', label: 'Antigravity', command: 'sfs mcp install --for antigravity' },
];

type UseCase = {
  icon: string;
  title: string;
  prompts: string[];
};

const USE_CASES: UseCase[] = [
  {
    icon: '🔍',
    title: 'Find & Resume',
    prompts: [
      'Search my sessions for the auth middleware bug',
      'What was I working on yesterday?',
      'Load the context from my last session in this repo',
      'Find sessions that touched package.json',
    ],
  },
  {
    icon: '🤝',
    title: 'Team Handoff',
    prompts: [
      'Hand off this session to sarah@company.com',
      'Show me the handoffs waiting for me',
      'Claim the handoff Sarah sent me',
      'Comment on that handoff: tests pass, deploy is next',
    ],
  },
  {
    icon: '🔎',
    title: 'Audit & Verify',
    prompts: [
      'Show the audit report for my last session (after sfs audit)',
      'Which audit findings are still open on that session?',
      'Which rules version was that session working under?',
      'What context did that session retrieve?',
    ],
  },
  {
    icon: '📚',
    title: 'Project Knowledge',
    prompts: [
      'What does this project use for authentication?',
      'Add a knowledge entry: we use PostgreSQL, not MySQL',
      'Update the wiki with our new branching strategy',
      'What decisions have been made about the API design?',
    ],
  },
  {
    icon: '🎫',
    title: 'Tickets & Agents',
    prompts: [
      'What tickets are assigned to atlas?',
      'Start the next open ticket as atlas',
      'Follow this ticket',
      'What changed on the tickets I follow?',
      'Which review findings are still open on this ticket?',
      'Work through the review queue until it is clean',
    ],
  },
  {
    icon: '🗂️',
    title: 'Sessions & Checkpoints',
    prompts: [
      'Checkpoint this session before the refactor',
      'Fork a new session from the before-refactor checkpoint',
      'Rename my last session to "SSO redirect fix"',
      'List my sessions from the last 7 days',
    ],
  },
  {
    icon: '📋',
    title: 'Project Rules',
    prompts: [
      'What are the current project rules?',
      'Show me the compiled rules for Codex',
      'What preferences should I follow in this repo?',
      'Which tools are enabled for canonical rules?',
    ],
  },
  {
    icon: '🧩',
    title: 'Skills Marketplace',
    prompts: [
      'Find a skill for writing database migrations',
      'What would installing that skill change in my repo?',
      'Compare the last two versions of that skill',
      'Request that skill for this project',
    ],
  },
];

type CliCommand = {
  cmd: string;
  desc: string;
};

const CLI_COMMANDS: CliCommand[] = [
  { cmd: 'sfs list', desc: 'List sessions (filter: --tool, --since, --tag)' },
  { cmd: 'sfs show <id>', desc: 'View session content' },
  { cmd: 'sfs resume <id>', desc: 'Resume in your AI tool' },
  { cmd: 'sfs push <id>', desc: 'Sync to cloud' },
  { cmd: 'sfs sync', desc: 'Sync all pending sessions' },
  { cmd: 'sfs search <query>', desc: 'Full-text search across sessions' },
  { cmd: 'sfs handoff <id> --to <email>', desc: 'Hand off a session' },
  { cmd: 'sfs audit <id>', desc: 'Run LLM Judge audit' },
  { cmd: 'sfs project edit', desc: 'Edit project context for this repo' },
  { cmd: 'sfs ticket list', desc: 'List tickets (filter: --assigned-to, --status, --priority)' },
  { cmd: 'sfs ticket inbox', desc: 'Updates on the tickets you follow' },
  { cmd: 'sfs helper ask "<question>"', desc: 'Ask the session helper (Enterprise)' },
  { cmd: 'sfs rules init', desc: 'Set up canonical project rules (enables per-tool compilers)' },
  { cmd: 'sfs rules edit', desc: 'Edit canonical static preferences in $EDITOR' },
  { cmd: 'sfs rules compile', desc: 'Compile canonical rules to tool files (CLAUDE.md, codex.md, ...)' },
  { cmd: 'sfs dlp scan <id>', desc: 'Scan for secrets/PHI' },
];

type McpTool = {
  name: string;
  desc: string;
};

type McpToolGroupData = {
  id: string;
  label: string;
  tools: McpTool[];
};

// One source of truth for the tool list: tests/unit/test_help_mcp_tools.py
// fails when this file and the MCP server's registered tools drift apart.
const MCP_TOOL_GROUPS: McpToolGroupData[] = mcpToolData.groups;
const MCP_TOOL_COUNT = MCP_TOOL_GROUPS.reduce((sum, group) => sum + group.tools.length, 0);

function buildSiteHref(theme: Theme, path: string): string {
  const sep = path.includes('?') ? '&' : '?';
  return `https://sessionfs.dev${path}${sep}theme=${theme}`;
}

type CopyState = 'idle' | 'copied' | 'error';

function TerminalCopyButton({ text }: { text: string }) {
  const [state, setState] = useState<CopyState>('idle');

  async function handleCopy() {
    // Feature check: clipboard API only exists in secure contexts (HTTPS/localhost)
    const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
    if (!clipboard || typeof clipboard.writeText !== 'function') {
      setState('error');
      setTimeout(() => setState('idle'), 2500);
      return;
    }
    try {
      await clipboard.writeText(text);
      setState('copied');
      setTimeout(() => setState('idle'), 1500);
    } catch {
      // Permission denied, document not focused, etc.
      setState('error');
      setTimeout(() => setState('idle'), 2500);
    }
  }

  const color =
    state === 'copied'
      ? 'text-code-well-success'
      : state === 'error'
        ? 'text-code-well-danger'
        : 'text-code-well-control-fg';

  return (
    <button
      onClick={handleCopy}
      aria-label="Copy command"
      aria-live="polite"
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 text-2xs font-medium rounded-md border border-code-well-control-border bg-code-well-control-bg transition-colors ${color}`}
    >
      {state === 'copied' && (
        <>
          <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="4 10 8 14 16 6" />
          </svg>
          Copied
        </>
      )}
      {state === 'error' && (
        <>
          <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="10" cy="10" r="8" />
            <line x1="10" y1="6" x2="10" y2="11" />
            <line x1="10" y1="14" x2="10" y2="14.01" />
          </svg>
          Copy failed
        </>
      )}
      {state === 'idle' && (
        <>
          <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <rect x="6" y="6" width="11" height="11" rx="1.5" />
            <path d="M13 6V4.5A1.5 1.5 0 0 0 11.5 3h-7A1.5 1.5 0 0 0 3 4.5v7A1.5 1.5 0 0 0 4.5 13H6" />
          </svg>
          Copy
        </>
      )}
    </button>
  );
}

function Terminal({ command, output }: { command: string; output: string }) {
  return (
    <div className="rounded-md overflow-hidden border border-code-well-border bg-code-well-bg shadow-md">
      {/* Title bar */}
      <div className="flex items-center justify-between px-3 py-2 bg-code-well-bar-bg border-b border-code-well-bar-border">
        <div className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-code-well-dot-red" />
          <span className="w-2.5 h-2.5 rounded-full bg-code-well-dot-yellow" />
          <span className="w-2.5 h-2.5 rounded-full bg-code-well-dot-green" />
        </div>
        <span className="text-2xs font-mono text-code-well-muted">terminal</span>
        <TerminalCopyButton text={command} />
      </div>
      {/* Body */}
      <pre tabIndex={0} className="px-4 py-4 text-sm font-mono leading-relaxed overflow-x-auto whitespace-pre text-code-well-fg">
        <span className="text-code-well-muted">$ </span>
        <span className="text-code-well-accent">{command}</span>
        {'\n\n'}
        <span className="text-code-well-fg opacity-85">{output}</span>
      </pre>
    </div>
  );
}

function SectionHeading({
  id,
  eyebrow,
  title,
  subtitle,
}: {
  id?: string;
  eyebrow?: string;
  title: string;
  subtitle?: ReactNode;
}) {
  return (
    <div className="mb-6">
      {eyebrow && (
        <div className="text-2xs font-semibold uppercase tracking-wider mb-2 text-brand">
          {eyebrow}
        </div>
      )}
      <h2 id={id} className="text-xl font-bold text-text-primary leading-tight">
        {title}
      </h2>
      {subtitle && (
        <p className="mt-1.5 text-base text-text-secondary leading-relaxed">
          {subtitle}
        </p>
      )}
    </div>
  );
}

const textLink = 'text-brand underline underline-offset-2 hover:opacity-80';

export default function HelpPage() {
  const [selectedTool, setSelectedTool] = useState<Tool>(TOOLS[0]);
  const [theme, setTheme] = useState<Theme>(() => getTheme());
  const { openHelper } = useHelper();

  // Subscribe to theme changes: ThemeToggle writes data-theme in an effect,
  // so we watch for attribute mutations to keep resource-link query params fresh.
  // The initial theme value comes from the useState lazy initializer above, which
  // reads localStorage first (ThemeToggle's source of truth) so there's no need
  // to re-read on mount here.
  useEffect(() => {
    if (typeof document === 'undefined') return;
    const el = document.documentElement;
    const observer = new MutationObserver(() => {
      const next = el.getAttribute('data-theme');
      if (next === 'light' || next === 'dark') setTheme(next);
    });
    observer.observe(el, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, []);

  const siteHref = (path: string) => buildSiteHref(theme, path);

  const fakeOutput = `✓ MCP server registered
✓ ${selectedTool.label} will now have access to ${MCP_TOOL_COUNT} tools

Restart ${selectedTool.label} to activate.`;

  return (
    <PageContainer className="md:px-8 md:py-10">
      {/* Hero */}
      <PageHeader
        className="mb-12 md:mb-16"
        breadcrumb={
          <span className="text-2xs font-semibold uppercase tracking-wider text-brand">
            Help & Quickstart
          </span>
        }
        title="You don't need to memorize commands."
        description={
          <span className="block text-md md:text-lg text-text-secondary leading-relaxed max-w-3xl">
            Install the MCP server for your AI tool, and your agent learns SessionFS automatically.
            Just tell it what you want.
          </span>
        }
      />

      {/* Section 1 — Install MCP server */}
      <section className="mb-12 md:mb-16">
        <SectionHeading
          eyebrow="Step 1"
          title="Install the MCP server"
          subtitle="Pick your AI tool. The command registers SessionFS as an MCP server so your agent can call its tools."
        />

        {/* Tool pills */}
        <div className="flex flex-wrap gap-2 mb-5" role="tablist" aria-label="AI tool selector">
          {TOOLS.map((tool) => {
            const active = tool.id === selectedTool.id;
            return (
              <button
                key={tool.id}
                role="tab"
                aria-selected={active}
                onClick={() => setSelectedTool(tool)}
                className={`px-3.5 py-1.5 text-sm font-medium rounded-full border transition-colors ${
                  active
                    ? 'bg-brand text-text-inverse border-brand'
                    : 'bg-transparent text-text-secondary border-border'
                }`}
              >
                {tool.label}
              </button>
            );
          })}
        </div>

        <Terminal command={selectedTool.command} output={fakeOutput} />

        <p className="mt-4 text-sm text-text-tertiary text-center md:text-left">
          That's it. Your AI agent now knows how to use SessionFS.
        </p>
      </section>

      {/* Section 2 — Use cases */}
      <section className="mb-12 md:mb-16">
        <SectionHeading
          eyebrow="Step 2"
          title="What you can tell your agent"
          subtitle="Plain-language prompts your agent will understand once SessionFS is installed."
        />

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {USE_CASES.map((useCase) => (
            <Card key={useCase.title} level="surface" className="p-5">
              <div className="flex items-center gap-2 mb-3">
                <span className="text-lg leading-none" aria-hidden="true">
                  {useCase.icon}
                </span>
                <h3 className="text-md font-semibold text-text-primary">{useCase.title}</h3>
              </div>
              <ul className="space-y-2">
                {useCase.prompts.map((prompt) => (
                  <li key={prompt} className="text-sm italic leading-relaxed text-text-secondary">
                    "{prompt}"
                  </li>
                ))}
              </ul>
            </Card>
          ))}
        </div>
      </section>

      {/* Section 3 — Session helper */}
      <section className="mb-12 md:mb-16" aria-labelledby="help-session-helper">
        <SectionHeading
          id="help-session-helper"
          eyebrow="Enterprise"
          title="Session helper"
          subtitle="Ask about your sessions, your projects and SessionFS itself, right here in the dashboard. Answers cite the sessions, messages and docs they draw on."
        />

        <Card level="surface" className="p-5 md:p-6 mb-4">
          <p className="text-sm leading-relaxed text-text-secondary max-w-3xl">
            Ask things like "find the session where we fixed the SSO redirect" or "what did we
            decide about Redis, and why?". The helper sees only what you can already read: your
            own sessions, the projects you can open, and your organization membership. It runs on
            your own model key or your organization's, never one owned by SessionFS.
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-3">
            {openHelper && (
              <Button variant="primary" size="sm" onClick={() => openHelper()}>
                Ask the helper
              </Button>
            )}
            <a
              href={siteHref('/session-helper/')}
              target="_blank"
              rel="noopener noreferrer"
              className={`text-sm ${textLink}`}
            >
              Session helper docs
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          </div>
        </Card>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <HelperFeature title="Clickable references">
            Ids in an answer are links. A ticket id (<code className="font-mono text-xs">tk_…</code>)
            opens a preview over the conversation with its status, priority, assignee, description
            and latest comments, plus a button to open the ticket. Project and session ids open their
            pages, and a cited message opens the session at that message.
          </HelperFeature>
          <HelperFeature title="Actions you confirm">
            The helper can turn Auto Narrative on or off for one or more projects and move sessions
            to Trash. It never changes anything on its own: each proposal lists every target and
            what will happen to it, and nothing happens until you press Confirm. Proposals expire
            after 15 minutes.
          </HelperFeature>
          <HelperFeature title="Answers from the docs">
            Ask how SessionFS works, such as how to set up Jira or what Auto Narrative does, and it
            searches the docs on sessionfs.dev instead of guessing. Those answers link to the pages
            and sections they used, and each link opens in a new tab.
          </HelperFeature>
          <HelperFeature title="Trash and history">
            Sessions moved to Trash can be restored from Sessions → Trash for 30 days. Earlier
            threads are listed under Conversations in the helper, so you can pick one back up.
          </HelperFeature>
        </div>

        <p className="mt-4 text-sm leading-relaxed text-text-secondary">
          Model settings, custom instructions and token usage are in{' '}
          <Link to="/settings/assistant" className={textLink}>
            Settings → Assistant
          </Link>
          . Org owners and admins set the model, budget and instructions used with the organization's key under Organization → Policies → Assistant.
        </p>
      </section>

      {/* Section 4 — Prefer the CLI */}
      <section className="mb-12 md:mb-16">
        <SectionHeading
          eyebrow="Optional"
          title="Prefer the CLI?"
          subtitle="The full SessionFS CLI is still here when you want direct control."
        />

        <div className="rounded-md overflow-hidden border border-code-well-border bg-code-well-bg shadow-sm mb-6">
          <pre className="px-4 py-4 text-sm font-mono leading-relaxed overflow-x-auto text-code-well-fg">
            {['pip install sessionfs', 'sfs init', 'sfs list', 'sfs push ses_abc123'].map((cmd, idx, all) => (
              <span key={cmd}>
                <span className="text-code-well-muted">$ </span>
                <span className="text-code-well-accent">{cmd}</span>
                {idx < all.length - 1 ? '\n' : null}
              </span>
            ))}
          </pre>
        </div>

        <div className="rounded-lg border overflow-hidden bg-bg-secondary border-border">
          <ul className="divide-y divide-border">
            {CLI_COMMANDS.map((row) => (
              <li
                key={row.cmd}
                className="grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-1 sm:gap-4 px-4 py-3"
              >
                <code className="text-xs font-mono text-text-primary break-words">{row.cmd}</code>
                <span className="text-sm text-text-secondary">{row.desc}</span>
              </li>
            ))}
          </ul>
        </div>

        <div className="mt-4 text-sm">
          <a href={siteHref('/cli/')} className="inline-flex items-center gap-1 transition-colors text-brand">
            Full CLI reference
            <span aria-hidden="true">→</span>
          </a>
        </div>
      </section>

      {/* Section 5 — MCP Tools */}
      <section className="mb-12 md:mb-16" aria-labelledby="help-mcp-tools">
        <SectionHeading
          id="help-mcp-tools"
          eyebrow="Reference"
          title={`MCP Tools (${MCP_TOOL_COUNT})`}
          subtitle={
            <>
              The tools your agent gets when SessionFS is installed. Some need a paid plan; the{' '}
              <a href={siteHref('/mcp/')} target="_blank" rel="noopener noreferrer" className={textLink}>
                MCP reference
                <span className="sr-only"> (opens in a new tab)</span>
              </a>{' '}
              has the details for each one.
            </>
          }
        />

        <McpToolBrowser groups={MCP_TOOL_GROUPS} total={MCP_TOOL_COUNT} />
      </section>

      {/* Section 6 — More resources */}
      <section className="mb-12">
        <SectionHeading title="More resources" />

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
          <ResourceLink href={siteHref('/quickstart/')} icon="📖" label="Full documentation" />
          <ResourceLink href={siteHref('/cli/')} icon="📋" label="CLI reference" />
          <ResourceLink href={siteHref('/mcp/')} icon="🔌" label="MCP tools reference" />
          <ResourceLink href={siteHref('/session-helper/')} icon="💡" label="Session helper" />
          <ResourceLink href={siteHref('/pricing/')} icon="💰" label="Pricing & plans" />
          <ResourceLink href={siteHref('/enterprise/')} icon="🏢" label="Enterprise" />
          <ResourceLink href="https://github.com/SessionFS/sessionfs/issues" icon="🐛" label="Report an issue" external />
          <ResourceLink href="mailto:support@sessionfs.dev" icon="💬" label="Support" />
          <ResourceLink href={siteHref('/changelog/')} icon="📝" label="Changelog" />
        </div>
      </section>
    </PageContainer>
  );
}

function HelperFeature({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card level="surface" className="p-5">
      <h3 className="text-md font-semibold text-text-primary mb-2">{title}</h3>
      <p className="text-sm leading-relaxed text-text-secondary">{children}</p>
    </Card>
  );
}

function matchesQuery(tool: McpTool, q: string): boolean {
  return tool.name.toLowerCase().includes(q) || tool.desc.toLowerCase().includes(q);
}

function McpToolBrowser({ groups, total }: { groups: McpToolGroupData[]; total: number }) {
  const [query, setQuery] = useState('');
  const [openIds, setOpenIds] = useState<Set<string>>(() => new Set());
  const baseId = useId();
  const q = query.trim().toLowerCase();

  const visible = useMemo(
    () =>
      groups
        .map((group) => ({ ...group, tools: q ? group.tools.filter((t) => matchesQuery(t, q)) : group.tools }))
        .filter((group) => group.tools.length > 0),
    [groups, q],
  );
  const matchCount = visible.reduce((sum, group) => sum + group.tools.length, 0);
  const allOpen = visible.length > 0 && visible.every((group) => openIds.has(group.id));

  function onQueryChange(next: string) {
    setQuery(next);
    const nq = next.trim().toLowerCase();
    // Typing a filter opens every group that still has a match, so results show at once.
    if (nq) {
      setOpenIds(new Set(groups.filter((g) => g.tools.some((t) => matchesQuery(t, nq))).map((g) => g.id)));
    }
  }

  function toggle(id: string) {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setOpenIds(allOpen ? new Set() : new Set(visible.map((group) => group.id)));
  }

  return (
    <div>
      <div className="flex flex-col sm:flex-row sm:items-center gap-3 mb-3">
        <div className="flex-1 min-w-0">
          <Input
            type="search"
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            placeholder="Filter tools, e.g. ticket or wiki"
            aria-label="Filter MCP tools"
          />
        </div>
        <Button variant="secondary" size="sm" onClick={toggleAll} disabled={visible.length === 0} className="self-start sm:self-auto">
          {allOpen ? 'Collapse all' : 'Expand all'}
        </Button>
      </div>

      <p className="text-xs text-text-tertiary mb-3" aria-live="polite">
        {q ? `${matchCount} of ${total} tools match` : `${total} tools in ${groups.length} groups`}
      </p>

      {visible.length === 0 ? (
        <Card level="surface" className="p-5">
          <p className="text-sm text-text-secondary">No tools match "{query.trim()}".</p>
        </Card>
      ) : (
        <Card level="surface" className="divide-y divide-border overflow-hidden">
          {visible.map((group) => {
            const open = openIds.has(group.id);
            const panelId = `${baseId}-${group.id}`;
            return (
              <div key={group.id}>
                <h3>
                  <button
                    type="button"
                    aria-expanded={open}
                    aria-controls={panelId}
                    onClick={() => toggle(group.id)}
                    className="w-full flex items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-hover outline-none focus-visible:shadow-[inset_0_0_0_2px_var(--brand)]"
                  >
                    <span className="text-sm font-semibold text-text-primary">{group.label}</span>
                    <span className="flex items-center gap-2 shrink-0">
                      <Badge label={String(group.tools.length)} />
                      <svg
                        aria-hidden="true"
                        width="14"
                        height="14"
                        viewBox="0 0 20 20"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        className={`text-text-tertiary transition-transform ${open ? 'rotate-180' : ''}`}
                      >
                        <polyline points="5 8 10 13 15 8" />
                      </svg>
                    </span>
                  </button>
                </h3>
                {open && (
                  <ul
                    id={panelId}
                    aria-label={`${group.label} tools`}
                    className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-3 px-4 pb-4 pt-1"
                  >
                    {group.tools.map((tool) => (
                      <li key={tool.name} className="min-w-0">
                        <code className="block text-xs font-mono mb-0.5 text-text-primary break-words">
                          {tool.name}
                        </code>
                        <span className="block text-xs leading-snug text-text-secondary">{tool.desc}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            );
          })}
        </Card>
      )}
    </div>
  );
}

function ResourceLink({
  href,
  icon,
  label,
  external,
}: {
  href: string;
  icon: string;
  label: string;
  external?: boolean;
}) {
  const isExternal = external || href.startsWith('http') || href.startsWith('mailto:');
  return (
    <a
      href={href}
      target={isExternal && !href.startsWith('mailto:') ? '_blank' : undefined}
      rel={isExternal && !href.startsWith('mailto:') ? 'noopener noreferrer' : undefined}
      className="flex items-center gap-3 px-4 py-3 rounded-md border transition-colors hover:bg-surface-hover bg-bg-secondary border-border"
    >
      <span className="text-md leading-none" aria-hidden="true">
        {icon}
      </span>
      <span className="text-base flex-1 text-text-primary">{label}</span>
      <span aria-hidden="true" className="text-text-tertiary">
        →
      </span>
    </a>
  );
}
