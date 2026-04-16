import { useEffect, useState } from 'react';
import { getItem as lsGet } from '../utils/storage';

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
  { cmd: 'sfs rules compile', desc: 'Compile canonical rules to tool files (CLAUDE.md, codex.md, …)' },
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
];

const MCP_KNOWLEDGE_READ: McpTool[] = [
  { name: 'get_project_context', desc: 'Full wiki: overview + pages + concepts' },
  { name: 'search_project_knowledge', desc: 'Search knowledge entries by query' },
  { name: 'ask_project', desc: 'Q&A against knowledge base + sessions' },
];

const MCP_KNOWLEDGE_WRITE: McpTool[] = [
  { name: 'add_knowledge', desc: 'Add a discovery during a session' },
  { name: 'update_wiki_page', desc: 'Create or update a wiki page' },
  { name: 'list_wiki_pages', desc: 'Browse the wiki structure' },
];

const MCP_RULES: McpTool[] = [
  { name: 'get_rules', desc: 'Canonical project rules + compilation config' },
  { name: 'get_compiled_rules', desc: 'Compiled rule text for a tool (CLAUDE.md, codex.md, …)' },
];

function getCurrentTheme(): 'light' | 'dark' {
  if (typeof window === 'undefined') return 'dark';
  // localStorage is ThemeToggle's source of truth (via src/utils/storage).
  const stored = lsGet('sfs-theme');
  if (stored === 'light' || stored === 'dark') return stored;
  // Fall back to the DOM attribute (set by main.tsx before render) and finally
  // the OS preference.
  const domTheme = document.documentElement.getAttribute('data-theme');
  if (domTheme === 'light' || domTheme === 'dark') return domTheme;
  if (typeof window.matchMedia === 'function') {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  return 'dark';
}

function buildSiteHref(theme: 'light' | 'dark', path: string): string {
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
    state === 'copied' ? '#86efac' : state === 'error' ? '#fca5a5' : 'rgba(255,255,255,0.7)';

  return (
    <button
      onClick={handleCopy}
      aria-label="Copy command"
      aria-live="polite"
      className="inline-flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-medium rounded-md transition-colors"
      style={{
        backgroundColor: 'rgba(255,255,255,0.06)',
        color,
        border: '1px solid rgba(255,255,255,0.1)',
      }}
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
    <div
      className="rounded-[var(--radius-md)] overflow-hidden border"
      style={{
        backgroundColor: '#0b0d12',
        borderColor: 'rgba(255,255,255,0.08)',
        boxShadow: 'var(--shadow-md)',
      }}
    >
      {/* Title bar */}
      <div
        className="flex items-center justify-between px-3 py-2"
        style={{
          backgroundColor: 'rgba(255,255,255,0.03)',
          borderBottom: '1px solid rgba(255,255,255,0.06)',
        }}
      >
        <div className="flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: '#ff5f57' }} />
          <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: '#febc2e' }} />
          <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: '#28c840' }} />
        </div>
        <span
          className="text-[11px] font-mono"
          style={{ color: 'rgba(255,255,255,0.4)' }}
        >
          terminal
        </span>
        <TerminalCopyButton text={command} />
      </div>
      {/* Body */}
      <pre
        className="px-4 py-4 text-[13px] font-mono leading-relaxed overflow-x-auto whitespace-pre"
        style={{ color: 'rgba(255,255,255,0.92)' }}
      >
        <span style={{ color: 'rgba(255,255,255,0.45)' }}>$ </span>
        <span style={{ color: '#a5d6ff' }}>{command}</span>
        {'\n\n'}
        <span style={{ color: 'rgba(255,255,255,0.78)' }}>{output}</span>
      </pre>
    </div>
  );
}

function SectionHeading({ eyebrow, title, subtitle }: { eyebrow?: string; title: string; subtitle?: string }) {
  return (
    <div className="mb-6">
      {eyebrow && (
        <div
          className="text-[11px] font-semibold uppercase tracking-wider mb-2"
          style={{ color: 'var(--brand)' }}
        >
          {eyebrow}
        </div>
      )}
      <h2 className="text-[22px] font-bold text-[var(--text-primary)] leading-tight">
        {title}
      </h2>
      {subtitle && (
        <p className="mt-1.5 text-[14px] text-[var(--text-secondary)] leading-relaxed">
          {subtitle}
        </p>
      )}
    </div>
  );
}

export default function HelpPage() {
  const [selectedTool, setSelectedTool] = useState<Tool>(TOOLS[0]);
  const [theme, setTheme] = useState<'light' | 'dark'>(() => getCurrentTheme());

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
✓ ${selectedTool.label} will now have access to 14 tools

Restart ${selectedTool.label} to activate.`;

  return (
    <div className="max-w-5xl mx-auto px-5 md:px-8 py-10 md:py-12">
      {/* Hero */}
      <header className="mb-12 md:mb-16">
        <div
          className="text-[11px] font-semibold uppercase tracking-wider mb-3"
          style={{ color: 'var(--brand)' }}
        >
          Help & Quickstart
        </div>
        <h1 className="text-[32px] md:text-[40px] font-bold leading-tight text-[var(--text-primary)] tracking-tight">
          You don't need to memorize commands.
        </h1>
        <p className="mt-4 text-[16px] md:text-[17px] text-[var(--text-secondary)] leading-relaxed max-w-3xl">
          Install the MCP server for your AI tool, and your agent learns SessionFS automatically.
          Just tell it what you want.
        </p>
      </header>

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
                className="px-3.5 py-1.5 text-[13px] font-medium rounded-full border transition-colors"
                style={{
                  backgroundColor: active ? 'var(--brand)' : 'transparent',
                  color: active ? 'var(--text-inverse)' : 'var(--text-secondary)',
                  borderColor: active ? 'var(--brand)' : 'var(--border)',
                }}
              >
                {tool.label}
              </button>
            );
          })}
        </div>

        <Terminal command={selectedTool.command} output={fakeOutput} />

        <p className="mt-4 text-[13px] text-[var(--text-tertiary)] text-center md:text-left">
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
            <div
              key={useCase.title}
              className="rounded-[var(--radius-lg)] border p-5"
              style={{
                backgroundColor: 'var(--bg-secondary)',
                borderColor: 'var(--border)',
              }}
            >
              <div className="flex items-center gap-2 mb-3">
                <span className="text-[18px] leading-none" aria-hidden="true">
                  {useCase.icon}
                </span>
                <h3 className="text-[15px] font-semibold text-[var(--text-primary)]">
                  {useCase.title}
                </h3>
              </div>
              <ul className="space-y-2">
                {useCase.prompts.map((prompt) => (
                  <li
                    key={prompt}
                    className="text-[13.5px] italic leading-relaxed"
                    style={{ color: 'var(--text-secondary)' }}
                  >
                    "{prompt}"
                  </li>
                ))}
              </ul>
            </div>
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

        <div
          className="rounded-[var(--radius-md)] overflow-hidden border mb-6"
          style={{
            backgroundColor: '#0b0d12',
            borderColor: 'rgba(255,255,255,0.08)',
            boxShadow: 'var(--shadow-sm)',
          }}
        >
          <pre
            className="px-4 py-4 text-[13px] font-mono leading-relaxed overflow-x-auto"
            style={{ color: 'rgba(255,255,255,0.92)' }}
          >
            <span style={{ color: 'rgba(255,255,255,0.45)' }}>$ </span>
            <span style={{ color: '#a5d6ff' }}>pip install sessionfs</span>{'\n'}
            <span style={{ color: 'rgba(255,255,255,0.45)' }}>$ </span>
            <span style={{ color: '#a5d6ff' }}>sfs init</span>{'\n'}
            <span style={{ color: 'rgba(255,255,255,0.45)' }}>$ </span>
            <span style={{ color: '#a5d6ff' }}>sfs list</span>{'\n'}
            <span style={{ color: 'rgba(255,255,255,0.45)' }}>$ </span>
            <span style={{ color: '#a5d6ff' }}>sfs push ses_abc123</span>
          </pre>
        </div>

        <div
          className="rounded-[var(--radius-lg)] border overflow-hidden"
          style={{
            backgroundColor: 'var(--bg-secondary)',
            borderColor: 'var(--border)',
          }}
        >
          <ul className="divide-y" style={{ borderColor: 'var(--border)' }}>
            {CLI_COMMANDS.map((row) => (
              <li
                key={row.cmd}
                className="grid grid-cols-1 sm:grid-cols-[minmax(0,14rem)_1fr] gap-1 sm:gap-4 px-4 py-3"
                style={{ borderColor: 'var(--border)' }}
              >
                <code
                  className="text-[12.5px] font-mono"
                  style={{ color: 'var(--text-primary)' }}
                >
                  {row.cmd}
                </code>
                <span className="text-[13px]" style={{ color: 'var(--text-secondary)' }}>
                  {row.desc}
                </span>
              </li>
            ))}
          </ul>
        </div>

        <div className="mt-4 text-[13px]">
          <a
            href={siteHref('/cli/')}
            className="inline-flex items-center gap-1 transition-colors"
            style={{ color: 'var(--brand)' }}
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
          title="MCP Tools (14)"
          subtitle="The tools your agent gets when SessionFS is installed."
        />

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <McpToolGroup label="Session tools" tools={MCP_SESSION_TOOLS} />
          <McpToolGroup label="Knowledge (read)" tools={MCP_KNOWLEDGE_READ} />
          <McpToolGroup label="Knowledge (write)" tools={MCP_KNOWLEDGE_WRITE} />
          <McpToolGroup label="Rules (read)" tools={MCP_RULES} />
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
    </div>
  );
}

function McpToolGroup({ label, tools }: { label: string; tools: McpTool[] }) {
  return (
    <div
      className="rounded-[var(--radius-lg)] border p-4"
      style={{
        backgroundColor: 'var(--bg-secondary)',
        borderColor: 'var(--border)',
      }}
    >
      <div
        className="text-[11px] font-semibold uppercase tracking-wider mb-3"
        style={{ color: 'var(--text-tertiary)' }}
      >
        {label}
      </div>
      <ul className="space-y-3">
        {tools.map((tool) => (
          <li key={tool.name}>
            <code
              className="block text-[12.5px] font-mono mb-0.5"
              style={{ color: 'var(--text-primary)' }}
            >
              {tool.name}
            </code>
            <span
              className="block text-[12.5px] leading-snug"
              style={{ color: 'var(--text-secondary)' }}
            >
              {tool.desc}
            </span>
          </li>
        ))}
      </ul>
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
      className="flex items-center gap-3 px-4 py-3 rounded-[var(--radius-md)] border transition-colors"
      style={{
        backgroundColor: 'var(--bg-secondary)',
        borderColor: 'var(--border)',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.backgroundColor = 'var(--surface-hover)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.backgroundColor = 'var(--bg-secondary)';
      }}
    >
      <span className="text-[16px] leading-none" aria-hidden="true">
        {icon}
      </span>
      <span className="text-[14px] flex-1" style={{ color: 'var(--text-primary)' }}>
        {label}
      </span>
      <span aria-hidden="true" style={{ color: 'var(--text-tertiary)' }}>
        →
      </span>
    </a>
  );
}
