import type { HelperConversationSummary } from '../api/client';
import { relativeTime } from './format';
import { ArrowUpIcon, SparkIcon } from './icons';
import type { ScopeOption } from './scope';

const SUGGESTIONS: Record<ScopeOption['kind'], string[]> = {
  project: [
    'What changed in this project over the last week?',
    'What decisions have we made here, and why?',
    'Which problems keep coming up across sessions?',
    'Summarize the most recent sessions',
  ],
  group: [
    'What changed across these projects this week?',
    'Which decisions affect more than one project?',
    'Which problems keep coming up across the group?',
    'Summarize the most recent sessions in the group',
  ],
  session: [
    'Summarize this session',
    'What was decided, and why?',
    'What was left unfinished?',
    'Which files changed, and what for?',
  ],
  all: [
    'What did I work on this week?',
    'Find the session where I fixed a failing test',
    'What have I decided about caching, and why?',
    'Summarize my three most recent sessions',
  ],
};

function heading(scope: ScopeOption): string {
  if (scope.kind === 'project') return `Ask about ${scope.label === 'This project' ? 'this project' : scope.label}`;
  if (scope.kind === 'group')
    return `Ask about ${scope.label === 'This project group' ? 'this project group' : scope.label}`;
  if (scope.kind === 'session') return 'Ask about this session';
  return 'Ask about your sessions';
}

export default function HelperEmptyState({
  scope,
  onPick,
  recent,
  onOpenConversation,
  onShowHistory,
  disabled = false,
}: {
  scope: ScopeOption;
  /** True while the new scope's availability is being rechecked. */
  disabled?: boolean;
  onPick: (prompt: string) => void;
  recent: HelperConversationSummary[];
  onOpenConversation: (id: string) => void;
  onShowHistory: () => void;
}) {
  return (
    <div className="px-4 py-6 sm:px-5 sm:py-8">
      <span className="grid h-10 w-10 place-items-center rounded-xl bg-[color-mix(in_oklch,var(--brand)_16%,transparent)] text-brand">
        <SparkIcon size={20} />
      </span>
      <h3 className="mt-4 !text-lg !font-semibold text-text-primary [overflow-wrap:anywhere]">{heading(scope)}</h3>
      <p className="mt-1.5 max-w-prose text-sm text-text-secondary">
        Answers draw only on sessions and project knowledge you can already read, and link back to every source.
      </p>

      <h4 className="mt-6 text-2xs font-semibold uppercase tracking-wide text-text-tertiary">Try asking</h4>
      <ul className="mt-2 space-y-1.5">
        {SUGGESTIONS[scope.kind].map((s) => (
          <li key={s}>
            <button
              type="button"
              onClick={() => onPick(s)}
              disabled={disabled}
              className="group flex w-full items-center gap-3 rounded-lg border border-border bg-bg-primary px-3 py-2.5 text-left text-sm text-text-secondary transition-colors disabled:cursor-not-allowed disabled:opacity-60 hover:border-border-strong hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            >
              <span className="min-w-0 flex-1">{s}</span>
              <ArrowUpIcon size={14} className="rotate-45 text-text-tertiary transition-colors group-hover:text-brand" />
            </button>
          </li>
        ))}
      </ul>

      {recent.length > 0 && (
        <>
          <div className="mt-7 flex items-center justify-between">
            <h4 className="text-2xs font-semibold uppercase tracking-wide text-text-tertiary">Recent conversations</h4>
            <button
              type="button"
              onClick={onShowHistory}
              className="rounded text-xs font-medium text-brand hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
            >
              View all
            </button>
          </div>
          <ul className="-mx-2 mt-1.5">
            {recent.slice(0, 3).map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => onOpenConversation(c.id)}
                  className="flex w-full min-w-0 items-baseline gap-3 rounded-md px-2 py-1.5 text-left text-sm text-text-secondary transition-colors hover:bg-surface-hover hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                >
                  <span className="min-w-0 flex-1 truncate">{c.title || 'Untitled conversation'}</span>
                  <span className="shrink-0 text-2xs text-text-tertiary">{relativeTime(c.updated_at)}</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
