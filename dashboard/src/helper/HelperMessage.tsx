import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { Alert, Button, Markdown } from '../components/ui';
import {
  parseCiteHref,
  sourceHref,
  ERROR_TITLES,
  answerAsPlainText,
  countLabel,
  describeToolStep,
  processCitations,
  type HelperError,
  type Source,
  type ToolStep,
  type Turn,
} from './format';
import {
  CheckCircleIcon,
  CheckIcon,
  ChevronRightIcon,
  CopyIcon,
  RetryIcon,
  Spinner,
  XCircleIcon,
} from './icons';
import { linkButtonSmall } from './styles';

// ── User ──

export function UserMessage({ content }: { content: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[88%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-[color-mix(in_srgb,var(--brand)_13%,var(--surface))] px-3.5 py-2 text-base text-text-primary [overflow-wrap:anywhere]">
        <span className="sr-only">You: </span>
        {content}
      </div>
    </div>
  );
}

// ── Tool steps ──

function StepIcon({ status }: { status: ToolStep['status'] }) {
  if (status === 'running') return <Spinner size={13} className="text-brand" />;
  if (status === 'error') return <XCircleIcon size={14} className="text-danger" />;
  return <CheckCircleIcon size={14} className="text-success" />;
}

/**
 * Tool activity as compact, collapsible steps. Open while the answer is being
 * gathered so the reader can follow along; collapsed to a one-line summary
 * once the turn settles (the reader can re-open it).
 */
export function ToolSteps({ steps, live }: { steps: ToolStep[]; live: boolean }) {
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? live;
  const running = steps.find((s) => s.status === 'running');
  const failed = steps.filter((s) => s.status === 'error').length;
  const total = steps.reduce((n, s) => n + (s.itemCount ?? 0), 0);
  const counted = steps.some((s) => s.itemCount != null);

  let summary: ReactNode;
  if (running) {
    summary = <span className="truncate">{describeToolStep(running)}…</span>;
  } else {
    summary = (
      <span className="truncate">
        {steps.length === 1 ? describeToolStep(steps[0]) : `Used ${steps.length} tools`}
        {counted && <span className="text-text-tertiary"> · {countLabel(total)}</span>}
        {failed > 0 && <span className="text-danger"> · {failed} failed</span>}
      </span>
    );
  }

  return (
    <div className="mb-3 rounded-lg border border-border bg-bg-primary/60 text-xs">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setUserOpen(!open)}
        className="flex w-full min-w-0 items-center gap-2 rounded-lg px-2.5 py-2 text-left text-text-secondary outline-none transition-colors hover:text-text-primary focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      >
        {running ? <Spinner size={13} className="text-brand" /> : <StepIcon status={failed ? 'error' : 'ok'} />}
        {summary}
        <ChevronRightIcon
          size={14}
          className={`ml-auto text-text-tertiary transition-transform duration-150 ${open ? 'rotate-90' : ''}`}
        />
      </button>
      {open && (
        <ol className="space-y-1.5 border-t border-border px-2.5 py-2" aria-label="Steps">
          {steps.map((s, i) => (
            <li key={`${s.id}-${i}`} className="flex min-w-0 items-start gap-2">
              <span className="mt-px">
                <StepIcon status={s.status} />
              </span>
              <span className={`min-w-0 flex-1 [overflow-wrap:anywhere] ${s.status === 'running' ? 'text-text-primary' : 'text-text-secondary'}`}>
                {describeToolStep(s)}
                {s.status === 'running' && '…'}
                {s.status === 'error' && <span className="text-danger"> — failed</span>}
              </span>
              {s.status === 'ok' && s.itemCount != null && (
                <span className="shrink-0 tabular-nums text-text-tertiary">{countLabel(s.itemCount)}</span>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

// ── Citations ──

function sourceLabel(s: Source, messageIndex?: number): string {
  const indexes = messageIndex !== undefined ? [messageIndex] : s.messageIndexes;
  const msgs = indexes.length
    ? `, message${indexes.length > 1 ? 's' : ''} ${indexes.join(', ')}`
    : '';
  return `${s.title || `Session ${s.sessionId}`}${msgs}`;
}

/**
 * Inline footnote chip for a `[session:…]` marker. Opens the session at the
 * marker's own message when it named one (`messageIndex`).
 */
export function CitationChip({
  source,
  messageIndex,
  onNavigate,
}: {
  source: Source;
  messageIndex?: number;
  onNavigate: () => void;
}) {
  const base =
    'mx-0.5 inline-flex h-[1.15rem] min-w-[1.15rem] items-center justify-center rounded-[5px] px-1 align-[0.12em] text-2xs font-semibold leading-none tabular-nums no-underline';
  if (!source.verified) {
    return (
      <span className={`${base} bg-bg-sunken text-text-tertiary`} title="Source not available to you">
        {source.n}
      </span>
    );
  }
  return (
    <Link
      to={sourceHref(source.sessionId, messageIndex)}
      onClick={onNavigate}
      aria-label={`Source ${source.n}: ${sourceLabel(source, messageIndex)}`}
      title={sourceLabel(source, messageIndex)}
      className={`${base} bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] text-brand transition-colors hover:bg-brand hover:text-text-on-brand outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]`}
    >
      {source.n}
    </Link>
  );
}

function Sources({ sources, onNavigate }: { sources: Source[]; onNavigate: () => void }) {
  const shown = sources.filter((s) => s.verified);
  if (shown.length === 0) return null;
  return (
    <div className="mt-3">
      <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-text-tertiary">Sources</p>
      <ul className="flex flex-wrap gap-1.5" aria-label="Sources">
        {shown.map((s) => (
          <li key={s.sessionId} className="min-w-0 max-w-full">
            <Link
              to={sourceHref(s.sessionId, s.messageIndexes[0])}
              onClick={onNavigate}
              aria-label={`${s.n}. ${sourceLabel(s)}`}
              title={sourceLabel(s)}
              className="group flex min-w-0 max-w-full items-center gap-1.5 rounded-md border border-border bg-bg-primary py-1 pl-1 pr-2 text-xs text-text-secondary transition-colors hover:border-border-strong hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            >
              <span className="grid h-4 min-w-4 shrink-0 place-items-center rounded bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] px-1 text-2xs font-semibold tabular-nums text-brand">
                {s.n}
              </span>
              <span className="min-w-0 truncate">{s.title || s.sessionId}</span>
              {s.messageIndexes.length > 0 && (
                <span className="shrink-0 font-mono text-2xs text-text-tertiary">
                  msg {s.messageIndexes.slice(0, 3).join(', ')}
                  {s.messageIndexes.length > 3 ? '…' : ''}
                </span>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ── Errors ──

export interface ErrorActions {
  onRetry?: () => void;
  onNewConversation: () => void;
  onNavigate: () => void;
  keyHint?: ReactNode;
}

export function ErrorCard({ error, actions }: { error: HelperError; actions: ErrorActions }) {
  const quiet = error.kind === 'busy' || error.kind === 'disconnected' || error.kind === 'network';
  let body: ReactNode = error.message;
  let action: ReactNode = null;
  switch (error.kind) {
    case 'busy':
      body = 'You already have the maximum number of answers running. Wait for one to finish, then try again.';
      break;
    case 'disconnected':
      body = 'The connection closed before the answer finished. What arrived is kept above.';
      break;
    case 'key_required':
      body = (
        <>
          Add a model key in Settings → Data protection → Security review model.
          {actions.keyHint}
        </>
      );
      action = (
        <Link to="/settings/data-protection" onClick={actions.onNavigate} className={linkButtonSmall}>
          Open settings
        </Link>
      );
      break;
    case 'scope_mismatch':
      body = 'This conversation was started with a different scope. Start a new one to ask about this scope.';
      action = (
        <Button size="sm" variant="secondary" onClick={actions.onNewConversation}>
          New conversation
        </Button>
      );
      break;
    case 'upgrade_required':
      action = (
        <Link to="/settings/billing" onClick={actions.onNavigate} className={linkButtonSmall}>
          View plans
        </Link>
      );
      break;
  }
  if (!action && actions.onRetry && error.kind !== 'upgrade_required') {
    action = (
      <Button size="sm" variant="secondary" onClick={actions.onRetry}>
        <RetryIcon size={13} />
        Retry
      </Button>
    );
  }
  return (
    <Alert variant={quiet ? 'warning' : 'danger'} title={ERROR_TITLES[error.kind]} className="mt-1">
      <p>{body}</p>
      {action && <div className="mt-2.5 flex flex-wrap gap-2">{action}</div>}
    </Alert>
  );
}

// ── Assistant ──

const CitationContext = createContext<{ sources: Source[]; onNavigate: () => void }>({
  sources: [],
  onNavigate: () => {},
});

function CitationRef({ n, messageIndex }: { n: number; messageIndex?: number }) {
  const { sources, onNavigate } = useContext(CitationContext);
  const s = sources.find((x) => x.n === n);
  return s ? <CitationChip source={s} messageIndex={messageIndex} onNavigate={onNavigate} /> : null;
}

/** Markdown link override: `#sfs-cite-<n>[-m<msg>]` becomes a footnote chip. */
function renderCitationLink(href: string): ReactNode | undefined {
  const cite = parseCiteHref(href);
  if (!cite) return undefined;
  return <CitationRef n={cite.n} messageIndex={cite.messageIndex} />;
}

function ThinkingDots() {
  return (
    <span className="inline-flex items-center gap-2 py-1 text-sm text-text-tertiary">
      <span className="inline-flex gap-1" aria-hidden="true">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-text-tertiary" />
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-text-tertiary [animation-delay:150ms]" />
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-text-tertiary [animation-delay:300ms]" />
      </span>
      Thinking
    </span>
  );
}

function CopyAnswer({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
      aria-label={copied ? 'Copied' : 'Copy answer'}
      className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs text-text-tertiary transition-colors hover:bg-surface-hover hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
    >
      {copied ? <CheckIcon size={13} className="text-success" /> : <CopyIcon size={13} />}
      <span aria-hidden="true">{copied ? 'Copied' : 'Copy'}</span>
    </button>
  );
}

export function AssistantMessage({
  turn,
  actions,
}: {
  turn: Turn;
  actions: ErrorActions;
}) {
  const streaming = turn.status === 'streaming';
  const settled = !streaming;
  const { markdown, sources } = useMemo(
    () => processCitations(turn.content, turn.citations, { streaming, settled }),
    [turn.content, turn.citations, streaming, settled],
  );
  const { onNavigate } = actions;

  // The link renderer keeps one identity for the whole stream (a new one per
  // token would rebuild Markdown's component map and remount every block);
  // the chips read the current sources from context instead.
  const citeContext = useMemo(() => ({ sources, onNavigate }), [sources, onNavigate]);

  const running = turn.toolSteps.some((s) => s.status === 'running');
  const showThinking = streaming && !markdown.trim() && !running && !turn.error;
  const hasText = markdown.trim().length > 0;

  return (
    <div className="min-w-0">
      <span className="sr-only">Helper: </span>
      {turn.toolSteps.length > 0 && <ToolSteps steps={turn.toolSteps} live={streaming && !hasText} />}

      {hasText && (
        <div className="helper-answer">
          <CitationContext.Provider value={citeContext}>
            <Markdown className={streaming ? 'helper-caret' : ''} renderLink={renderCitationLink}>
              {markdown}
            </Markdown>
          </CitationContext.Provider>
        </div>
      )}
      {showThinking && <ThinkingDots />}

      {turn.status === 'stopped' && (
        <p className="mt-2 text-xs text-text-tertiary">Stopped — the partial answer is saved to this conversation.</p>
      )}

      {turn.error && <ErrorCard error={turn.error} actions={actions} />}

      {settled && <Sources sources={sources} onNavigate={onNavigate} />}

      {settled && hasText && !turn.error && (
        <div className="mt-2 flex items-center gap-1 text-text-tertiary">
          <CopyAnswer text={answerAsPlainText(turn.content, turn.citations)} />
        </div>
      )}
    </div>
  );
}
