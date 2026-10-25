import type { HelperConversationSummary } from '../api/client';
import { Button, IconButton } from '../components/ui';
import { historyGroup, relativeTime, type HistoryGroup } from './format';
import { Spinner, TrashIcon } from './icons';

const ORDER: HistoryGroup[] = ['Today', 'Yesterday', 'Previous 7 days', 'Older'];

export default function HelperHistory({
  conversations,
  loading,
  error,
  onRetry,
  activeId,
  openingId,
  busy,
  orgId,
  orgName,
  onOpen,
  onDelete,
  onNew,
}: {
  conversations: HelperConversationSummary[] | undefined;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  activeId: string | null;
  openingId: string | null;
  /** True while an answer streams: switching threads is disabled. */
  busy: boolean;
  orgId: string | null;
  orgName: string | null;
  onOpen: (id: string) => void;
  onDelete: (c: HelperConversationSummary) => void;
  onNew: () => void;
}) {
  if (loading) {
    return (
      <div className="space-y-2 px-4 py-5" aria-busy="true" aria-label="Loading conversations">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="animate-skeleton-pulse rounded-lg bg-surface-hover p-3">
            <div className="h-3 w-3/4 rounded bg-surface-active" />
            <div className="mt-2 h-2.5 w-1/4 rounded bg-surface-active" />
          </div>
        ))}
      </div>
    );
  }
  if (error) {
    return (
      <div className="px-5 py-10 text-center">
        <p className="text-sm text-text-secondary">Couldn't load your conversations.</p>
        <Button size="sm" variant="secondary" className="mt-3" onClick={onRetry}>
          Retry
        </Button>
      </div>
    );
  }
  if (!conversations || conversations.length === 0) {
    return (
      <div className="px-5 py-10 text-center">
        <p className="text-base font-medium text-text-primary">No conversations yet</p>
        <p className="mt-1 text-sm text-text-tertiary">Questions you ask are kept here so you can pick them up later.</p>
        <Button size="sm" className="mt-4" onClick={onNew}>
          Ask a question
        </Button>
      </div>
    );
  }

  const groups = new Map<HistoryGroup, HelperConversationSummary[]>();
  for (const c of conversations) {
    const g = historyGroup(c.updated_at);
    groups.set(g, [...(groups.get(g) ?? []), c]);
  }

  const scopeLabel = (c: HelperConversationSummary) => {
    if (!c.org_id) return 'Personal';
    return c.org_id === orgId && orgName ? orgName : 'Organization';
  };

  return (
    <nav aria-label="Conversation history" className="px-2 py-3 sm:px-3">
      {busy && (
        <p className="mx-2 mb-2 rounded-md bg-bg-sunken px-2.5 py-1.5 text-xs text-text-tertiary">
          Stop the current answer to switch conversations.
        </p>
      )}
      {ORDER.filter((g) => groups.has(g)).map((g) => (
        <section key={g} className="mb-3" aria-labelledby={`helper-history-${g.replace(/\W+/g, '-')}`}>
          <h3
            id={`helper-history-${g.replace(/\W+/g, '-')}`}
            className="px-2.5 pb-1 pt-2 !text-2xs !font-semibold uppercase tracking-wide text-text-tertiary"
          >
            {g}
          </h3>
          <ul>
            {groups.get(g)!.map((c) => {
              const title = c.title || 'Untitled conversation';
              const active = c.id === activeId;
              return (
                <li
                  key={c.id}
                  className={`group relative flex items-center gap-1 rounded-lg transition-colors ${
                    active ? 'bg-surface-active' : 'hover:bg-surface-hover'
                  }`}
                >
                  <button
                    type="button"
                    disabled={busy}
                    aria-current={active ? 'true' : undefined}
                    onClick={() => onOpen(c.id)}
                    className="min-w-0 flex-1 rounded-lg px-2.5 py-2 text-left outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <span className={`line-clamp-2 text-sm [overflow-wrap:anywhere] ${c.title ? 'text-text-primary' : 'italic text-text-tertiary'}`}>
                      {title}
                    </span>
                    <span className="mt-0.5 flex items-center gap-1.5 text-2xs text-text-tertiary">
                      {openingId === c.id && <Spinner size={10} />}
                      <span>{relativeTime(c.updated_at)}</span>
                      <span aria-hidden="true">·</span>
                      <span className="truncate">{scopeLabel(c)}</span>
                    </span>
                  </button>
                  <IconButton
                    size="sm"
                    label={`Delete ${title}`}
                    disabled={busy}
                    onClick={() => onDelete(c)}
                    className="mr-1.5 !p-1.5 hover:!text-danger opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100"
                  >
                    <TrashIcon size={14} />
                  </IconButton>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </nav>
  );
}
