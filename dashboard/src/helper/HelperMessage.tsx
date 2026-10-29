import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { Alert, Button, Markdown } from '../components/ui';
import HelperActionCard from './HelperActionCard';
import {
  parseCiteHref,
  meetingCiteHref,
  sourceHref,
  ticketHref,
  wikiHref,
  citationAgeLabel,
  citationAgeSpoken,
  citationTimestamp,
  ERROR_TITLES,
  answerAsPlainText,
  compactTokens,
  countLabel,
  describeToolStep,
  remarkRefs,
  processCitations,
  type HelperError,
  type Source,
  type TokenUsage,
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
import { renderRefLink, useHelperRefs } from './refs';
import AnswerBoundary from './AnswerBoundary';

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
  if (s.kind === 'doc') return s.title;
  if (s.kind === 'ticket') return s.title || `Ticket ${s.ticketId}`;
  if (s.kind === 'wiki') return s.title || `Wiki ${s.slug}`;
  if (s.kind === 'meeting') return `${s.title || `Meeting ${s.meetingId}`}${meetingLinesText(s.ranges)}`;
  const indexes = messageIndex !== undefined ? [messageIndex] : s.messageIndexes;
  const msgs = indexes.length
    ? `, message${indexes.length > 1 ? 's' : ''} ${indexes.join(', ')}`
    : '';
  return `${s.title || `Session ${s.sessionId}`}${msgs}`;
}

const NEW_TAB = ' (opens in new tab)';

/** ", lines 12–15" / ", lines 3–4 and 9" for a meeting source's ranges. */
function meetingLinesText(ranges: { start: number; end: number }[]): string {
  if (ranges.length === 0) return '';
  const parts = ranges.map((r) => (r.start === r.end ? `${r.start}` : `${r.start}–${r.end}`));
  const single = ranges.length === 1 && ranges[0].start === ranges[0].end;
  return `, line${single ? '' : 's'} ${parts.join(', ')}`;
}

/**
 * Inline footnote chip for a `[session:…]` or `[doc:…]` marker. Opens the
 * session at the marker's own message when it named one (`messageIndex`), or
 * the docs page in a new tab for a doc source. A doc chip is a link only once
 * the server confirmed the page (its `doc_citation` carries the URL); before
 * that it is inert, and after the turn settles without confirmation it stays
 * an inert "not found" chip.
 */
export function CitationChip({
  source,
  messageIndex,
  lines,
  onNavigate,
}: {
  source: Source;
  messageIndex?: number;
  /** The line range this marker named (meeting sources). */
  lines?: { start: number; end: number };
  onNavigate: () => void;
}) {
  const base =
    'mx-0.5 inline-flex h-[1.15rem] min-w-[1.15rem] items-center justify-center rounded-[5px] px-1 align-[0.12em] text-2xs font-semibold leading-none tabular-nums no-underline';
  if (source.kind === 'session' && !source.verified) {
    return (
      <span className={`${base} bg-bg-sunken text-text-tertiary`} title="Source not available to you">
        {source.n}
      </span>
    );
  }
  if (source.kind === 'doc' && !source.url) {
    return (
      <span
        className={`${base} bg-bg-sunken text-text-tertiary`}
        title={source.verified ? 'Checking source…' : 'Not found in the SessionFS docs'}
      >
        {source.n}
      </span>
    );
  }
  if ((source.kind === 'ticket' || source.kind === 'wiki' || source.kind === 'meeting') && !source.verified) {
    return (
      <span className={`${base} bg-bg-sunken text-text-tertiary`} title="Source not available to you">
        {source.n}
      </span>
    );
  }
  if (source.kind === 'doc') {
    return (
      <a
        href={source.url ?? undefined}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={`Source ${source.n}: ${sourceLabel(source)}${NEW_TAB}`}
        title={sourceLabel(source)}
        className={`${base} bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] text-brand transition-colors hover:bg-brand hover:text-text-on-brand outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]`}
      >
        {source.n}
      </a>
    );
  }
  if (source.kind === 'ticket' && !source.projectId) {
    // No project to link into: show the marker without a link.
    return (
      <span className={`${base} bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] text-brand`} title={sourceLabel(source)}>
        {source.n}
      </span>
    );
  }
  if (source.kind === 'ticket') {
    return (
      <Link
        to={ticketHref(source.projectId ?? '', source.ticketId)}
        onClick={onNavigate}
        aria-label={`Source ${source.n}: ${sourceLabel(source)}`}
        title={sourceLabel(source)}
        className={`${base} bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] text-brand transition-colors hover:bg-brand hover:text-text-on-brand outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]`}
      >
        {source.n}
      </Link>
    );
  }
  if (source.kind === 'meeting') {
    const range = lines ?? source.ranges[0] ?? null;
    const label = `${source.title || `Meeting ${source.meetingId}`}${range ? meetingLinesText([range]) : ''}`;
    return (
      <Link
        to={meetingCiteHref(source.meetingId, range)}
        onClick={onNavigate}
        aria-label={`Source ${source.n}: ${label}`}
        title={label}
        className={`${base} bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] text-brand transition-colors hover:bg-brand hover:text-text-on-brand outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]`}
      >
        {source.n}
      </Link>
    );
  }
  if (source.kind === 'wiki') {
    return (
      <Link
        to={wikiHref(source.projectId, source.slug)}
        onClick={onNavigate}
        aria-label={`Source ${source.n}: ${sourceLabel(source)}`}
        title={sourceLabel(source)}
        className={`${base} bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] text-brand transition-colors hover:bg-brand hover:text-text-on-brand outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]`}
      >
        {source.n}
      </Link>
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

function sourceAgeLabel(s: Source): string | null {
  // Only session/ticket/wiki citations carry a freshness timestamp; a docs
  // page has none, and a citation without a usable ``at`` shows no badge.
  return s.kind === 'doc' ? null : citationAgeLabel(s.at);
}

/** The chip's accessible name: number, label and (when known) the age in
 *  words — the visible age badge is part of the name, not replaced by it. */
function chipName(s: Source): string {
  const age = s.kind === 'doc' ? null : citationAgeSpoken(s.at);
  return `${s.n}. ${sourceLabel(s)}${age ? `, ${age}` : ''}`;
}

function sourceTitle(s: Source, messageIndex?: number): string {
  const label = sourceLabel(s, messageIndex);
  if (s.kind === 'doc') return label;
  const exact = citationTimestamp(s.at);
  return exact ? `${label} · ${exact}` : label;
}

const CHIP =
  'group flex min-w-0 max-w-full items-center gap-1.5 rounded-md border border-border bg-bg-primary py-1 pl-1 pr-2 text-xs text-text-secondary transition-colors hover:border-border-strong hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]';

function SourceNumber({ n }: { n: number }) {
  return (
    <span className="grid h-4 min-w-4 shrink-0 place-items-center rounded bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] px-1 text-2xs font-semibold tabular-nums text-brand">
      {n}
    </span>
  );
}

function SourceAge({ age }: { age: string | null }) {
  if (!age) return null;
  return <span className="shrink-0 font-mono text-2xs text-text-tertiary">{age}</span>;
}

function Sources({ sources, onNavigate }: { sources: Source[]; onNavigate: () => void }) {
  // Only confirmed sources: readable sessions, resolved docs, and tickets/wiki
  // that came from a tool result this turn.
  const shown = sources.filter((s) => s.verified && (s.kind !== 'doc' || s.url));
  if (shown.length === 0) return null;
  return (
    <div className="mt-3">
      <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wide text-text-tertiary">Sources</p>
      <ul className="flex flex-wrap gap-1.5" aria-label="Sources">
        {shown.map((s) => {
          if (s.kind === 'doc') {
            return (
              <li key={`doc:${s.slug}#${s.anchor ?? ''}`} className="min-w-0 max-w-full">
                <a
                  href={s.url ?? undefined}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={`${s.n}. ${sourceLabel(s)}${NEW_TAB}`}
                  title={sourceTitle(s)}
                  className={CHIP}
                >
                  <SourceNumber n={s.n} />
                  <span className="min-w-0 truncate">{s.title}</span>
                </a>
              </li>
            );
          }
          if (s.kind === 'ticket') {
            const body = (
              <>
                <SourceNumber n={s.n} />
                <span className="min-w-0 truncate">{s.title || s.ticketId}</span>
                <SourceAge age={sourceAgeLabel(s)} />
              </>
            );
            return (
              <li key={`ticket:${s.ticketId}`} className="min-w-0 max-w-full">
                {s.projectId ? (
                  <Link
                    to={ticketHref(s.projectId, s.ticketId)}
                    onClick={onNavigate}
                    aria-label={chipName(s)}
                    title={sourceTitle(s)}
                    className={CHIP}
                  >
                    {body}
                  </Link>
                ) : (
                  // No project to link into: a plain chip, still named for
                  // screen readers via visually hidden text (not a "note"
                  // role or an aria-label on a generic span).
                  <span title={sourceTitle(s)} className={CHIP}>
                    <span aria-hidden="true" className="flex min-w-0 max-w-full items-center gap-1.5">
                      {body}
                    </span>
                    <span className="sr-only">{chipName(s)}</span>
                  </span>
                )}
              </li>
            );
          }
          if (s.kind === 'meeting') {
            const first = s.ranges[0];
            return (
              <li key={`meeting:${s.meetingId}`} className="min-w-0 max-w-full">
                <Link
                  to={meetingCiteHref(s.meetingId, first)}
                  onClick={onNavigate}
                  aria-label={chipName(s)}
                  title={sourceTitle(s)}
                  className={CHIP}
                >
                  <SourceNumber n={s.n} />
                  <MeetingIcon />
                  <span className="min-w-0 truncate">{s.title || 'Meeting'}</span>
                  {first && (
                    <span className="shrink-0 font-mono text-2xs text-text-tertiary">
                      {s.ranges
                        .slice(0, 2)
                        .map((r) => (r.start === r.end ? `L${r.start}` : `L${r.start}–${r.end}`))
                        .join(', ')}
                      {s.ranges.length > 2 ? '…' : ''}
                    </span>
                  )}
                  <SourceAge age={sourceAgeLabel(s)} />
                </Link>
              </li>
            );
          }
          if (s.kind === 'wiki') {
            return (
              <li key={`wiki:${s.projectId}/${s.slug}`} className="min-w-0 max-w-full">
                <Link
                  to={wikiHref(s.projectId, s.slug)}
                  onClick={onNavigate}
                  aria-label={chipName(s)}
                  title={sourceTitle(s)}
                  className={CHIP}
                >
                  <SourceNumber n={s.n} />
                  <span className="min-w-0 truncate">{s.title || s.slug}</span>
                  <SourceAge age={sourceAgeLabel(s)} />
                </Link>
              </li>
            );
          }
          return (
            <li key={s.sessionId} className="min-w-0 max-w-full">
              <Link
                to={sourceHref(s.sessionId, s.messageIndexes[0])}
                onClick={onNavigate}
                aria-label={chipName(s)}
                title={sourceTitle(s)}
                className={CHIP}
              >
                <SourceNumber n={s.n} />
                <span className="min-w-0 truncate">{s.title || s.sessionId}</span>
                {s.messageIndexes.length > 0 && (
                  <span className="shrink-0 font-mono text-2xs text-text-tertiary">
                    msg {s.messageIndexes.slice(0, 3).join(', ')}
                    {s.messageIndexes.length > 3 ? '…' : ''}
                  </span>
                )}
                <SourceAge age={sourceAgeLabel(s)} />
              </Link>
            </li>
          );
        })}
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
  /** Org owners/admins get a link to raise the monthly budget. */
  isOrgAdmin?: boolean;
}

export function ErrorCard({ error, actions }: { error: HelperError; actions: ErrorActions }) {
  const quiet =
    error.kind === 'busy' || error.kind === 'disconnected' || error.kind === 'network' || error.kind === 'budget';
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
    case 'budget':
      body = (
        <>
          Your organization has used this month&rsquo;s token budget for the session helper, so answers on the org
          key are paused until the budget resets on the 1st (UTC).{' '}
          {actions.isOrgAdmin
            ? 'You can raise the budget in your organization settings.'
            : 'Ask an org admin to raise the budget, or add your own model key to keep going.'}
        </>
      );
      action = actions.isOrgAdmin ? (
        <Link to="/settings/organization?tab=policies" onClick={actions.onNavigate} className={linkButtonSmall}>
          Open org settings
        </Link>
      ) : (
        <Link to="/settings/data-protection" onClick={actions.onNavigate} className={linkButtonSmall}>
          Add your own key
        </Link>
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
  if (!action && actions.onRetry && error.kind !== 'upgrade_required' && error.kind !== 'budget') {
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

function CitationRef({
  n,
  messageIndex,
  lines,
}: {
  n: number;
  messageIndex?: number;
  lines?: { start: number; end: number };
}) {
  const { sources, onNavigate } = useContext(CitationContext);
  const s = sources.find((x) => x.n === n);
  return s ? <CitationChip source={s} messageIndex={messageIndex} lines={lines} onNavigate={onNavigate} /> : null;
}

/** A small "meeting" mark so a meeting source reads as one at a glance. */
function MeetingIcon() {
  return (
    <svg className="h-3 w-3 shrink-0 text-text-tertiary" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  );
}

/** Markdown link override: `#sfs-cite-<n>[-m<msg>]` becomes a footnote chip. */
function renderCitationLink(href: string): ReactNode | undefined {
  const cite = parseCiteHref(href);
  if (!cite) return undefined;
  return <CitationRef n={cite.n} messageIndex={cite.messageIndex} lines={cite.lines} />;
}

/** Citation chips first, then ticket / project / session references. */
function renderAnswerLink(href: string, children: ReactNode): ReactNode | undefined {
  return renderCitationLink(href) ?? renderRefLink(href, children);
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

/** Tokens one answer used, e.g. "1.2k in · 260 out". */
export function UsageLine({ usage }: { usage: TokenUsage }) {
  const full = `${usage.input.toLocaleString()} input tokens, ${usage.output.toLocaleString()} output tokens`;
  return (
    <span className="ml-auto px-1.5 text-2xs tabular-nums text-text-tertiary" title={full}>
      <span aria-hidden="true">
        {compactTokens(usage.input)} in · {compactTokens(usage.output)} out
      </span>
      <span className="sr-only">{full}</span>
    </span>
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
  const { repo } = useHelperRefs();
  const refPlugins = useMemo(() => [remarkRefs({ repo, streaming })], [repo, streaming]);

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
            <AnswerBoundary resetKey={markdown}>
              <Markdown
                className={streaming ? 'helper-caret' : ''}
                renderLink={renderAnswerLink}
                remarkPlugins={refPlugins}
              >
                {markdown}
              </Markdown>
            </AnswerBoundary>
          </CitationContext.Provider>
        </div>
      )}
      {showThinking && <ThinkingDots />}

      {turn.status === 'stopped' && (
        <p className="mt-2 text-xs text-text-tertiary">Stopped — the partial answer is saved to this conversation.</p>
      )}

      {turn.error && <ErrorCard error={turn.error} actions={actions} />}

      {turn.actions?.map((a) => <HelperActionCard key={a.id} action={a} onNavigate={onNavigate} />)}

      {settled && <Sources sources={sources} onNavigate={onNavigate} />}

      {settled && hasText && !turn.error && (
        <div className="mt-2 flex items-center gap-1 text-text-tertiary">
          <CopyAnswer text={answerAsPlainText(turn.content, turn.citations)} />
          {turn.status === 'done' && turn.usage && <UsageLine usage={turn.usage} />}
        </div>
      )}
    </div>
  );
}
