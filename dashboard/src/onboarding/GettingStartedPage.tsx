import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { setItem } from '../utils/storage';
import Wordmark from '../components/Wordmark';

export const ONBOARDING_DISMISSED_KEY = 'sfs-onboarding-dismissed';

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
  const [selectedTool, setSelectedTool] = useState<Tool>(TOOLS[0]);

  const sessions = useQuery({
    queryKey: ['sessions', { page: 1, page_size: 1 }],
    queryFn: () => auth!.client.listSessions({ page: 1, page_size: 1 }),
    enabled: !!auth,
    refetchInterval: 10_000,
  });

  const projects = useQuery({
    queryKey: ['projects'],
    queryFn: () => auth!.client.listProjects(),
    enabled: !!auth,
    refetchInterval: 10_000,
  });

  const hasSession = (sessions.data?.total ?? 0) > 0;
  const hasProject = (projects.data?.length ?? 0) > 0;

  function handleSkip() {
    setItem(ONBOARDING_DISMISSED_KEY, '1');
    navigate('/');
  }

  return (
    <div className="max-w-2xl mx-auto px-5 py-10 md:py-16">
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
          done={false}
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
