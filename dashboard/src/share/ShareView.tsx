import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import MessageBlock from '../sessions/MessageBlock';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { resolveApiBaseUrl } from '../auth/apiBase';

// ── Types ──────────────────────────────────────────────────────────

interface ShareViewData {
  title: string | null;
  tool: string;
  created_at: string;
  message_count: number;
  messages: Record<string, unknown>[];
  owner_display_name: string | null;
}

type PageState =
  | { kind: 'loading' }
  | { kind: 'password_required' }
  | { kind: 'error'; status: number; detail: string }
  | { kind: 'ready'; data: ShareViewData };

// Base URL for the share API — defaults to the dashboard origin's API.
function shareApiBase(): string {
  // M2 (Shield): prod is split-host (app.* dashboard, api.* API) — resolve
  // like every other surface, never assume same-origin.
  return resolveApiBaseUrl();
}

async function fetchShareView(
  token: string,
  password?: string,
): Promise<ShareViewData> {
  const method = password ? 'POST' : 'GET';
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const body = password ? JSON.stringify({ password }) : undefined;

  const resp = await fetch(
    `${shareApiBase()}/api/v1/share/${encodeURIComponent(token)}/view`,
    { method, headers, body },
  );

  if (resp.status === 401) {
    // Password required or bad password — caller handles
    throw { status: 401, detail: await resp.json().then(j => j.detail || 'Password required').catch(() => 'Password required') };
  }
  if (resp.status === 451) {
    throw { status: 451, detail: 'This shared session contains sensitive content and cannot be viewed publicly.' };
  }
  if (!resp.ok) {
    const detail = await resp.json().then(j => j.detail || 'Not found').catch(() => 'Not found');
    throw { status: resp.status, detail };
  }
  return resp.json();
}

// ── OG Meta helper ─────────────────────────────────────────────────
// TODO: SSR — OG meta tags set via JS are invisible to crawlers that
// don't execute JavaScript (Twitter, Slack, Facebook).  When SSR is
// added, these should be rendered server-side so unfurls work everywhere.

function setOGMeta(data: ShareViewData) {
  const title = data.title || 'Shared Session';
  document.title = `${title} — SessionFS`;

  const setMeta = (property: string, content: string) => {
    let el = document.querySelector(`meta[property="${property}"]`);
    if (!el) {
      el = document.createElement('meta');
      el.setAttribute('property', property);
      document.head.appendChild(el);
    }
    el.setAttribute('content', content);
  };

  setMeta('og:title', `${title} — SessionFS`);
  setMeta('og:description', `A ${data.tool} session shared via SessionFS — ${data.message_count} messages`);
  setMeta('og:type', 'website');
}

// ── Tool label ─────────────────────────────────────────────────────

function toolLabel(tool: string): string {
  const labels: Record<string, string> = {
    'claude-code': 'Claude Code',
    codex: 'Codex',
    cursor: 'Cursor',
    'gemini-cli': 'Gemini CLI',
    gemini: 'Gemini CLI',
    copilot: 'GitHub Copilot',
    'copilot-cli': 'GitHub Copilot',
    'kilo-code': 'Kilo Code',
    amp: 'Amp',
    'cline-cli': 'Cline',
    cline: 'Cline',
    'roo-code': 'Roo Code',
    'roo-cline': 'Roo Code',
    chatgpt: 'ChatGPT',
  };
  return labels[tool] || tool;
}

// ── Main component ─────────────────────────────────────────────────

export default function ShareView() {
  const { token } = useParams<{ token: string }>();
  const [state, setState] = useState<PageState>({ kind: 'loading' });
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [passwordError, setPasswordError] = useState('');

  const load = async (pw?: string) => {
    if (!token) return;
    try {
      setSubmitting(true);
      setPasswordError('');
      const data = await fetchShareView(token, pw);
      setOGMeta(data);
      setState({ kind: 'ready', data });
    } catch (err: unknown) {
      const e = err as { status: number; detail: string };
      if (e.status === 401 && !pw) {
        setState({ kind: 'password_required' });
      } else if (e.status === 401 && pw) {
        setPasswordError('Invalid password');
      } else {
        setState({ kind: 'error', status: e.status || 500, detail: e.detail || 'Something went wrong' });
      }
    } finally {
      setSubmitting(false);
    }
  };

  useEffect(() => {
    // M1 (Shield): passwords are NEVER accepted via URL — a protected link
    // always shows the password prompt.
    load(undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const handlePasswordSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    load(password);
  };

  // ── Password prompt ─────────────────────────────────────────
  if (state.kind === 'password_required') {
    return (
      <div className="min-h-screen bg-surface flex items-center justify-center p-4">
        <div className="w-full max-w-sm space-y-6">
          <div className="text-center space-y-2">
            <h1 className="text-xl font-semibold text-text-primary">Password Required</h1>
            <p className="text-sm text-text-tertiary">
              This shared session is password-protected.
            </p>
          </div>
          <form onSubmit={handlePasswordSubmit} className="space-y-4">
            <Input
              type="password"
              placeholder="Enter password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
            />
            {passwordError && (
              <p className="text-sm text-[var(--danger)]">{passwordError}</p>
            )}
            <Button type="submit" className="w-full" disabled={submitting || !password}>
              {submitting ? 'Verifying…' : 'View Session'}
            </Button>
          </form>
        </div>
      </div>
    );
  }

  // ── Error / dead link ───────────────────────────────────────
  if (state.kind === 'error') {
    return (
      <div className="min-h-screen bg-surface flex items-center justify-center p-4">
        <div className="text-center space-y-4 max-w-md">
          <div className="w-12 h-12 mx-auto rounded-full bg-bg-sunken flex items-center justify-center">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-text-tertiary">
              <circle cx="12" cy="12" r="10" />
              <path d="M12 8v4M12 16h.01" />
            </svg>
          </div>
          <h1 className="text-xl font-semibold text-text-primary">
            {state.status === 404 ? 'Link Not Found' : state.status === 451 ? 'Content Blocked' : 'Something Went Wrong'}
          </h1>
          <p className="text-sm text-text-tertiary">
            {state.status === 404
              ? 'This share link may have been revoked, expired, or never existed.'
              : state.status === 451
                ? 'This shared session contains sensitive content and cannot be viewed publicly.'
                : state.detail}
          </p>
        </div>
      </div>
    );
  }

  // ── Loading ─────────────────────────────────────────────────
  if (state.kind === 'loading') {
    return (
      <div className="min-h-screen bg-surface flex items-center justify-center">
        <p className="text-text-muted">Loading session…</p>
      </div>
    );
  }

  // ── Ready — render transcript ───────────────────────────────
  const { data } = state;

  return (
    <div className="min-h-screen bg-surface">
      {/* Header */}
      <header className="border-b border-border bg-bg-sunken/40">
        <div className="max-w-4xl mx-auto px-4 py-6 sm:px-6 lg:px-8">
          <h1 className="text-2xl font-bold text-text-primary mb-2">
            {data.title || 'Untitled Session'}
          </h1>
          <div className="flex flex-wrap items-center gap-3 text-sm text-text-tertiary">
            <span className="inline-flex items-center gap-1.5">
              <span className="inline-block w-2 h-2 rounded-full bg-[var(--accent)]" />
              {toolLabel(data.tool)}
            </span>
            <span aria-hidden="true">·</span>
            <span>{data.message_count} messages</span>
            <span aria-hidden="true">·</span>
            <span>{new Date(data.created_at).toLocaleDateString(undefined, {
              year: 'numeric',
              month: 'short',
              day: 'numeric',
            })}</span>
            {data.owner_display_name && (
              <>
                <span aria-hidden="true">·</span>
                <span>Shared by {data.owner_display_name}</span>
              </>
            )}
          </div>
          <div className="mt-3 pt-3 border-t border-border">
            <p className="text-xs text-text-tertiary">
              Shared via{' '}
              <a
                href="https://sessionfs.dev"
                className="text-[var(--brand)] hover:underline underline-offset-2"
                target="_blank"
                rel="noopener noreferrer"
              >
                SessionFS
              </a>
              {' '}— Memory Layer For AI Agents
            </p>
          </div>
        </div>
      </header>

      {/* Transcript */}
      <main className="max-w-4xl mx-auto px-4 py-8 sm:px-6 lg:px-8">
        {data.messages.length === 0 ? (
          <div className="text-center py-16">
            <p className="text-text-muted">This session contains no messages.</p>
          </div>
        ) : (
          <div className="flex flex-col gap-6">
            {data.messages.map((msg, i) => (
              <MessageBlock key={i} message={msg} />
            ))}
          </div>
        )}
      </main>

      {/* Footer */}
      <footer className="border-t border-border mt-12">
        <div className="max-w-4xl mx-auto px-4 py-6 sm:px-6 lg:px-8 text-center">
          <p className="text-xs text-text-tertiary">
            Powered by{' '}
            <a
              href="https://sessionfs.dev"
              className="text-[var(--brand)] hover:underline underline-offset-2"
              target="_blank"
              rel="noopener noreferrer"
            >
              SessionFS
            </a>
          </p>
        </div>
      </footer>
    </div>
  );
}
