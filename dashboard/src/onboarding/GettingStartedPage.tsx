import { useState } from 'react';
import { Link, useNavigate, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { setItem } from '../utils/storage';
import CopyButton from '../components/CopyButton';
import Wordmark from '../components/Wordmark';

/**
 * Scope the dismissal flag to (baseUrl, apiKey-hash) so different accounts
 * on the same browser (or self-hosted vs cloud) each get their own flag.
 *
 * Uses a simple hash of the full API key (not a prefix) to avoid collisions
 * between users on the same server. Key rotation gives a new hash, which
 * means the user sees onboarding once on the new key — acceptable trade-off
 * vs leaking key material into localStorage.
 */
export function onboardingDismissedKey(baseUrl?: string, apiKey?: string): string {
  const server = (baseUrl || 'default').replace(/[^a-z0-9]/gi, '_').slice(0, 30);
  // Simple djb2 hash of the full key → 8 hex chars. No crypto needed —
  // this is a UI preference key, not a security boundary.
  let h = 5381;
  for (const ch of apiKey || '') h = ((h << 5) + h + ch.charCodeAt(0)) >>> 0;
  const user = h.toString(16).padStart(8, '0');
  return `sfs-onboarding-dismissed-${server}-${user}`;
}

const TOOLS = [
  { id: 'claude-code', label: 'Claude Code', command: 'sfs mcp install --for claude-code' },
  { id: 'codex', label: 'Codex', command: 'sfs mcp install --for codex' },
  { id: 'gemini', label: 'Gemini CLI', command: 'sfs mcp install --for gemini' },
  { id: 'cursor', label: 'Cursor', command: 'sfs mcp install --for cursor' },
  { id: 'copilot', label: 'Copilot CLI', command: 'sfs mcp install --for copilot' },
  { id: 'amp', label: 'Amp', command: 'sfs mcp install --for amp' },
  { id: 'cline', label: 'Cline', command: 'sfs mcp install --for cline' },
  { id: 'roo-code', label: 'Roo Code', command: 'sfs mcp install --for roo-code' },
] as const;

type Tool = (typeof TOOLS)[number];

export default function GettingStartedPage() {
  const { auth } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [selectedTool, setSelectedTool] = useState<Tool>(TOOLS[0]);

  // After signup auto-login, LoginPage passes the raw API key via router state
  // so we can show it once in a dismissible banner.
  const signupApiKey = (location.state as { apiKey?: string } | null)?.apiKey;
  const [showKeyBanner, setShowKeyBanner] = useState(!!signupApiKey);

  // Each query polls until its own completion condition is met. Once
  // the user has at least one session / project the page is going to
  // redirect anyway, so background polling at that point is wasted
  // requests against the API.
  const sessions = useQuery({
    queryKey: ['sessions', { page: 1, page_size: 1 }],
    queryFn: () => auth!.client.listSessions({ page: 1, page_size: 1 }),
    enabled: !!auth,
    refetchInterval: (q) => {
      const total = (q.state.data as { total?: number } | undefined)?.total ?? 0;
      return total > 0 ? false : 10_000;
    },
  });

  const projects = useQuery({
    queryKey: ['projects'],
    queryFn: () => auth!.client.listProjects(),
    enabled: !!auth,
    refetchInterval: (q) => {
      const data = q.state.data as unknown[] | undefined;
      return (data?.length ?? 0) > 0 ? false : 10_000;
    },
  });

  const hasSession = (sessions.data?.total ?? 0) > 0;
  const hasProject = (projects.data?.length ?? 0) > 0;

  function handleSkip() {
    const key = onboardingDismissedKey(auth?.baseUrl, auth?.apiKey);
    setItem(key, '1');
    navigate('/');
  }

  return (
    <div className="max-w-2xl mx-auto px-5 py-10 md:py-16">
      {/* API key banner — shown once after signup */}
      {showKeyBanner && signupApiKey && (
        <div className="mb-6 p-4 bg-[var(--bg-elevated)] border border-[var(--brand)]/30 rounded-lg">
          <div className="flex items-center justify-between gap-3 mb-1">
            <p className="text-sm font-medium text-[var(--text-primary)]">Your API key</p>
            <button
              onClick={() => setShowKeyBanner(false)}
              className="text-xs text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]"
            >
              Dismiss
            </button>
          </div>
          <p className="text-xs text-[var(--text-tertiary)] mb-2">Save this now — you will not see it again.</p>
          <div className="flex items-center gap-2">
            <code className="flex-1 text-xs font-mono bg-[var(--bg-primary)] px-3 py-1.5 rounded border border-[var(--border)] truncate">
              {signupApiKey}
            </code>
            <CopyButton text={signupApiKey} />
          </div>
        </div>
      )}

      {/* Header */}
      <div className="text-center mb-10">
        <div className="inline-block mb-4">
          <Wordmark size="lg" />
        </div>
        <h1 className="text-2xl md:text-3xl font-bold text-[var(--text-primary)] tracking-tight">
          Get started with SessionFS
        </h1>
        <p className="mt-2 text-sm text-[var(--text-secondary)] max-w-md mx-auto">
          Three steps to capture, sync, and share your AI coding sessions.
        </p>
      </div>

      {/* Steps */}
      <div className="space-y-6">
        {/* Step 1: Install */}
        <StepCard
          number={1}
          title="Connect your AI tool"
          description="Pick your tool and run the install command. This registers SessionFS as an MCP server."
          done={hasSession}
        >
          <div className="flex flex-wrap gap-2 mb-4" role="tablist" aria-label="AI tool selector">
            {TOOLS.map((tool) => {
              const active = tool.id === selectedTool.id;
              return (
                <button
                  key={tool.id}
                  role="tab"
                  aria-selected={active}
                  onClick={() => setSelectedTool(tool)}
                  className="px-3 py-1.5 text-xs font-medium rounded-full border transition-colors"
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
          <div className="rounded-lg border overflow-hidden" style={{ backgroundColor: '#0b0d12', borderColor: 'rgba(255,255,255,0.08)' }}>
            <pre className="px-4 py-3 text-sm font-mono overflow-x-auto" style={{ color: 'rgba(255,255,255,0.92)' }}>
              <span style={{ color: 'rgba(255,255,255,0.45)' }}>$ </span>
              <span style={{ color: '#a5d6ff' }}>{selectedTool.command}</span>
            </pre>
          </div>
          <p className="mt-3 text-xs text-[var(--text-tertiary)]">
            Or install the CLI first:{' '}
            <code className="font-mono bg-[var(--bg-tertiary)] px-1.5 py-0.5 rounded text-[var(--text-secondary)]">
              pip install sessionfs && sfs init
            </code>
          </p>
        </StepCard>

        {/* Step 2: Capture */}
        <StepCard
          number={2}
          title="Capture your first session"
          description="Start a coding session in your AI tool. SessionFS captures it automatically."
          done={hasSession}
        >
          <p className="text-sm text-[var(--text-secondary)]">
            Or push an existing session manually:
          </p>
          <div className="mt-2 rounded-lg border overflow-hidden" style={{ backgroundColor: '#0b0d12', borderColor: 'rgba(255,255,255,0.08)' }}>
            <pre className="px-4 py-3 text-sm font-mono overflow-x-auto" style={{ color: 'rgba(255,255,255,0.92)' }}>
              <span style={{ color: 'rgba(255,255,255,0.45)' }}>$ </span>
              <span style={{ color: '#a5d6ff' }}>sfs push {'<session_id>'}</span>
            </pre>
          </div>
        </StepCard>

        {/* Step 3: Create project */}
        <StepCard
          number={3}
          title="Set up project context"
          description="Projects give your AI tools shared memory across sessions."
          done={hasProject}
        >
          <Link
            to="/projects"
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors"
          >
            Create Project
            <span aria-hidden="true">&rarr;</span>
          </Link>
        </StepCard>
      </div>

      {/* Footer */}
      <div className="mt-10 flex flex-col sm:flex-row items-center justify-between gap-4 pt-6 border-t" style={{ borderColor: 'var(--border)' }}>
        <p className="text-sm text-[var(--text-tertiary)]">
          You can always find setup help at{' '}
          <Link to="/help" className="text-[var(--accent)] hover:underline">
            Help
          </Link>
        </p>
        <button
          onClick={handleSkip}
          className="text-sm font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
        >
          Skip to Dashboard &rarr;
        </button>
      </div>
    </div>
  );
}

function StepCard({
  number,
  title,
  description,
  done,
  children,
}: {
  number: number;
  title: string;
  description: string;
  done: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className="rounded-[var(--radius-lg)] border p-5 md:p-6"
      style={{
        backgroundColor: 'var(--bg-secondary)',
        borderColor: done ? 'var(--brand)' : 'var(--border)',
      }}
    >
      <div className="flex items-start gap-4">
        {/* Step indicator */}
        <div
          className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold"
          style={{
            backgroundColor: done ? 'var(--brand)' : 'var(--bg-tertiary)',
            color: done ? 'var(--text-inverse)' : 'var(--text-secondary)',
          }}
          aria-label={done ? `Step ${number} complete` : `Step ${number}`}
        >
          {done ? (
            <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="4 10 8 14 16 6" />
            </svg>
          ) : (
            number
          )}
        </div>

        <div className="flex-1 min-w-0">
          <h2 className="text-base font-semibold text-[var(--text-primary)] mb-1">
            {title}
          </h2>
          <p className="text-sm text-[var(--text-secondary)] mb-4">
            {description}
          </p>
          {children}
        </div>
      </div>
    </div>
  );
}
