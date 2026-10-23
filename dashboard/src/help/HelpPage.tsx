import { useEffect, useState } from 'react';
import { getTheme, type Theme } from '../utils/theme';
import { Card, PageContainer, PageHeader } from '../components/ui';

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
      'Resume my last session in this repo',
      'Find sessions that touched package.json',
    ],
  },
  {
    icon: '🤝',
    title: 'Team Handoff',
    prompts: [
      'Hand off this session to sarah@company.com',
      'Show me pending handoffs',
      'What did Alex work on in this repo last week?',
    ],
  },
  {
    icon: '🔎',
    title: 'Audit & Verify',
    prompts: [
      'Audit this session for hallucinations',
      'Did the AI actually create the files it claimed?',
      'Show me the trust score for my last session',
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
    icon: '☁️',
    title: 'Sync & Manage',
    prompts: [
      'Sync all my unsent sessions',
      'How much storage am I using?',
      'List sessions from the last 7 days',
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
];

type CliCommand = {
  cmd: string;
  desc: string;
};

const CLI_COMMANDS: CliCommand[] = [
  { cmd: 'sfs list', desc: 'List sessions (filter: --tool, --since, --search)' },
  { cmd: 'sfs show <id>', desc: 'View session content' },
  { cmd: 'sfs resume <id>', desc: 'Resume in your AI tool' },
  { cmd: 'sfs push <id>', desc: 'Sync to cloud' },
  { cmd: 'sfs sync', desc: 'Sync all pending sessions' },
  { cmd: 'sfs search <query>', desc: 'Full-text search across sessions' },
  { cmd: 'sfs handoff <id> <email>', desc: 'Hand off a session' },
  { cmd: 'sfs audit <id>', desc: 'Run LLM Judge audit' },
  { cmd: 'sfs project edit', desc: 'Edit project context for this repo' },
  { cmd: 'sfs rules init', desc: 'Set up canonical project rules (enables per-tool compilers)' },
  { cmd: 'sfs rules edit', desc: 'Edit canonical static preferences in $EDITOR' },
  { cmd: 'sfs rules compile', desc: 'Compile canonical rules to tool files (CLAUDE.md, codex.md, ...)' },
  { cmd: 'sfs dlp scan <id>', desc: 'Scan for secrets/PHI' },
];

type McpTool = {
  name: string;
  desc: string;
};

const MCP_SESSION_TOOLS: McpTool[] = [
  { name: 'search_sessions', desc: 'Search across session titles, summaries, files' },
  { name: 'get_session_context', desc: 'Get full session content' },
  { name: 'list_recent_sessions', desc: 'Recent sessions for a repo' },
  { name: 'find_related_sessions', desc: 'Sessions touching same files or errors' },
  { name: 'get_session_summary', desc: 'Structured summary of a session' },
  { name: 'get_audit_report', desc: 'Judge findings for a session' },
  { name: 'get_session_provenance', desc: 'Which rules version instructed a session' },
];

const MCP_KNOWLEDGE_READ: McpTool[] = [
  { name: 'get_project_context', desc: 'Full wiki: overview + pages + concepts' },
  { name: 'get_context_section', desc: 'One section of the context doc' },
  { name: 'get_wiki_page', desc: 'One wiki page in full with backlinks' },
  { name: 'search_project_knowledge', desc: 'Search knowledge entries by query' },
  { name: 'list_knowledge_entries', desc: 'Filter by type / class / freshness / session' },
  { name: 'get_knowledge_entry', desc: 'One entry\u2019s full record' },
  { name: 'get_knowledge_health', desc: 'Pending / compiled counts + recommendations' },
  { name: 'ask_project', desc: 'Q&A against knowledge base + sessions' },
];

const MCP_KNOWLEDGE_WRITE: McpTool[] = [
  { name: 'add_knowledge', desc: 'Add a discovery during a session' },
  { name: 'update_wiki_page', desc: 'Create or update a wiki page' },
  { name: 'list_wiki_pages', desc: 'Browse the wiki structure' },
  { name: 'compile_knowledge_base', desc: 'Trigger a compile pass (heavy, mutating)' },
  { name: 'dismiss_knowledge_entry', desc: 'Retire a wrong/stale entry with audited reason' },
  { name: 'update_entry_confidence', desc: 'Raise (or lower) a KB entry confidence score' },
  { name: 'promote_entry', desc: 'Promote note → claim once quality gates pass' },
];

const MCP_RULES: McpTool[] = [
  { name: 'get_rules', desc: 'Canonical project rules + compilation config' },
  { name: 'get_compiled_rules', desc: 'Compiled rule text for a tool (CLAUDE.md, codex.md, ...)' },
];

const MCP_PERSONAS: McpTool[] = [
  { name: 'list_personas', desc: 'List active agent personas for this project' },
  { name: 'get_persona', desc: 'Full content + role + specializations for one persona' },
  { name: 'create_persona', desc: 'Create a new persona (ASCII name 1-50 chars)' },
  { name: 'assume_persona', desc: 'Work as a persona without a ticket (writes provenance bundle)' },
  { name: 'forget_persona', desc: 'Clear the persona-only bundle (refuses ticket bundles)' },
];

const MCP_TICKETS: McpTool[] = [
  { name: 'list_tickets', desc: 'List tickets filtered by assignee / status / priority' },
  { name: 'get_ticket', desc: 'Full ticket detail including dependencies and comments' },
  { name: 'create_ticket', desc: 'Create a ticket (human or agent source; FSM-validated)' },
  { name: 'start_ticket', desc: 'open → in_progress; returns compiled persona+ticket context; writes bundle' },
  { name: 'complete_ticket', desc: 'in_progress → review; clears bundle iff owned' },
  { name: 'resolve_ticket', desc: 'review → done; runs dependency enrichment + auto-unblock' },
  { name: 'assign_persona', desc: 'Set or change ticket.assigned_to' },
  { name: 'escalate_ticket', desc: 'Bump priority one level (low → medium → high → critical)' },
  { name: 'add_ticket_comment', desc: 'Add a comment with optional persona attribution' },
];

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

function SectionHeading({ eyebrow, title, subtitle }: { eyebrow?: string; title: string; subtitle?: string }) {
  return (
    <div className="mb-6">
      {eyebrow && (
        <div
          className="text-2xs font-semibold uppercase tracking-wider mb-2 text-brand"
        >
          {eyebrow}
        </div>
      )}
      <h2 className="text-xl font-bold text-text-primary leading-tight">
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

export default function HelpPage() {
  const [selectedTool, setSelectedTool] = useState<Tool>(TOOLS[0]);
  const [theme, setTheme] = useState<Theme>(() => getTheme());

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
✓ ${selectedTool.label} will now have access to 36 tools

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
            <Card
              key={useCase.title}
              level="surface"
              className="p-5"
            >
              <div className="flex items-center gap-2 mb-3">
                <span className="text-lg leading-none" aria-hidden="true">
                  {useCase.icon}
                </span>
                <h3 className="text-md font-semibold text-text-primary">
                  {useCase.title}
                </h3>
              </div>
              <ul className="space-y-2">
                {useCase.prompts.map((prompt) => (
                  <li
                    key={prompt}
                    className="text-sm italic leading-relaxed text-text-secondary"
                  >
                    "{prompt}"
                  </li>
                ))}
              </ul>
            </Card>
          ))}
        </div>
      </section>

      {/* Section 3 — Prefer the CLI */}
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

        <div
          className="rounded-lg border overflow-hidden bg-bg-secondary border-border"
        >
          <ul className="divide-y divide-border">
            {CLI_COMMANDS.map((row) => (
              <li
                key={row.cmd}
                className="grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-1 sm:gap-4 px-4 py-3"
              >
                <code
                  className="text-xs font-mono text-text-primary"
                >
                  {row.cmd}
                </code>
                <span className="text-sm text-text-secondary">
                  {row.desc}
                </span>
              </li>
            ))}
          </ul>
        </div>

        <div className="mt-4 text-sm">
          <a
            href={siteHref('/cli/')}
            className="inline-flex items-center gap-1 transition-colors text-brand"
          >
            Full CLI reference
            <span aria-hidden="true">→</span>
          </a>
        </div>
      </section>

      {/* Section 4 — MCP Tools */}
      <section className="mb-12 md:mb-16">
        <SectionHeading
          eyebrow="Reference"
          title="MCP Tools (36)"
          subtitle="The tools your agent gets when SessionFS is installed."
        />

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <McpToolGroup label="Session tools" tools={MCP_SESSION_TOOLS} />
          <McpToolGroup label="Knowledge (read)" tools={MCP_KNOWLEDGE_READ} />
          <McpToolGroup label="Knowledge (write)" tools={MCP_KNOWLEDGE_WRITE} />
          <McpToolGroup label="Rules (read)" tools={MCP_RULES} />
          <McpToolGroup label="Personas" tools={MCP_PERSONAS} />
          <McpToolGroup label="Tickets" tools={MCP_TICKETS} />
        </div>
      </section>

      {/* Section 5 — More resources */}
      <section className="mb-12">
        <SectionHeading title="More resources" />

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
          <ResourceLink href={siteHref('/quickstart/')} icon="📖" label="Full documentation" />
          <ResourceLink href={siteHref('/cli/')} icon="📋" label="CLI reference" />
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

function McpToolGroup({ label, tools }: { label: string; tools: McpTool[] }) {
  return (
    <Card level="surface" className="p-4">
      <div className="text-micro text-text-tertiary uppercase mb-3">
        {label}
      </div>
      <ul className="space-y-3">
        {tools.map((tool) => (
          <li key={tool.name}>
            <code
              className="block text-xs font-mono mb-0.5 text-text-primary"
            >
              {tool.name}
            </code>
            <span
              className="block text-xs leading-snug text-text-secondary"
            >
              {tool.desc}
            </span>
          </li>
        ))}
      </ul>
    </Card>
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
      <span className="text-base flex-1 text-text-primary">
        {label}
      </span>
      <span aria-hidden="true" className="text-text-tertiary">
        →
      </span>
    </a>
  );
}
