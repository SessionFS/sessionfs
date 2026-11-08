import { useEffect, useRef, useState } from 'react';

import type { HelperConversationSummary } from '../api/client';
import { Button, IconButton, Input } from '../components/ui';
import { HELPER_TITLE_MAX, historyGroup, relativeTime, type HistoryGroup } from './format';
import { RenameIcon, Spinner, TrashIcon } from './icons';

/** The row actions sit hidden on hover-capable screens until the row is hovered or focused. */
const ROW_ACTION = 'sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100 sm:focus-visible:opacity-100';

function HistoryRow({
  c,
  active,
  busy,
  opening,
  scopeLabel,
  onOpen,
  onDelete,
  onRename,
}: {
  c: HelperConversationSummary;
  active: boolean;
  busy: boolean;
  opening: boolean;
  scopeLabel: string;
  onOpen: () => void;
  onDelete: () => void;
  onRename: (title: string) => void;
}) {
  const title = c.title || 'Untitled conversation';
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const renameRef = useRef<HTMLButtonElement>(null);
  // Enter or Escape ends the edit before the input's blur fires: the blur
  // must not save a second time (or save after a cancel).
  const ended = useRef(false);

  useEffect(() => {
    if (!editing) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [editing]);

  const start = () => {
    ended.current = false;
    setDraft(c.title ?? '');
    setEditing(true);
  };

  const finish = (save: boolean, refocus: boolean) => {
    if (ended.current) return;
    ended.current = true;
    setEditing(false);
    const next = draft.split(/\s+/).join(' ').trim();
    // An empty title or no change keeps the old one.
    if (save && next && next !== (c.title ?? '')) onRename(next.slice(0, HELPER_TITLE_MAX));
    if (refocus) requestAnimationFrame(() => renameRef.current?.focus());
  };

  if (editing) {
    return (
      <li className="rounded-lg bg-surface-hover px-2 py-1.5">
        <Input
          ref={inputRef}
          value={draft}
          maxLength={HELPER_TITLE_MAX}
          aria-label={`Rename ${title}`}
          hint="Enter to save, Escape to cancel"
          className="!py-1.5"
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => finish(true, false)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              finish(true, true);
            } else if (e.key === 'Escape') {
              // Only the edit ends: the drawer stays open.
              e.preventDefault();
              e.stopPropagation();
              finish(false, true);
            }
          }}
        />
      </li>
    );
  }

  return (
    <li
      className={`group relative flex items-center gap-0.5 rounded-lg transition-colors ${
        active ? 'bg-surface-active' : 'hover:bg-surface-hover'
      }`}
    >
      <button
        type="button"
        disabled={busy}
        aria-current={active ? 'true' : undefined}
        onClick={onOpen}
        className="min-w-0 flex-1 rounded-lg px-2.5 py-2 text-left outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:cursor-not-allowed disabled:opacity-60"
      >
        <span className={`line-clamp-2 text-sm [overflow-wrap:anywhere] ${c.title ? 'text-text-primary' : 'italic text-text-tertiary'}`}>
          {title}
        </span>
        <span className="mt-0.5 flex items-center gap-1.5 text-2xs text-text-tertiary">
          {opening && <Spinner size={10} />}
          <span>{relativeTime(c.updated_at)}</span>
          <span aria-hidden="true">·</span>
          <span className="truncate">{scopeLabel}</span>
        </span>
      </button>
      <IconButton
        ref={renameRef}
        size="sm"
        label={`Rename ${title}`}
        onClick={start}
        className={`!p-1.5 opacity-100 ${ROW_ACTION}`}
      >
        <RenameIcon size={14} />
      </IconButton>
      <IconButton
        size="sm"
        label={`Delete ${title}`}
        disabled={busy}
        onClick={onDelete}
        className={`mr-1.5 !p-1.5 hover:!text-danger opacity-100 ${ROW_ACTION}`}
      >
        <TrashIcon size={14} />
      </IconButton>
    </li>
  );
}

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
  onRename,
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
  onRename: (c: HelperConversationSummary, title: string) => void;
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
            {groups.get(g)!.map((c) => (
              <HistoryRow
                key={c.id}
                c={c}
                active={c.id === activeId}
                busy={busy}
                opening={openingId === c.id}
                scopeLabel={scopeLabel(c)}
                onOpen={() => onOpen(c.id)}
                onDelete={() => onDelete(c)}
                onRename={(t) => onRename(c, t)}
              />
            ))}
          </ul>
        </section>
      ))}
    </nav>
  );
}
