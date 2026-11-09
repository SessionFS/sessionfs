import type { RefObject } from 'react';
import { Button, Kbd, Select, Tabs } from '../../components/ui';
import { VIEWS, type GroupBy, type ListParams, type SortBy, type ViewId } from './views';

/* ── Toolbar ──
 *
 * Title and count, search, List/Board, New ticket; then the saved views
 * (a tablist with counts; Blocked and Overdue counts turn red when not zero)
 * and the filter bar (group, sort, quick filters). */

const GROUP_OPTIONS: { value: GroupBy; label: string }[] = [
  { value: 'status', label: 'Status' },
  { value: 'issue', label: 'Issue' },
  { value: 'assignee', label: 'Assignee' },
  { value: 'none', label: 'None' },
];

const SORT_OPTIONS: { value: SortBy; label: string }[] = [
  { value: 'priority', label: 'Priority' },
  { value: 'due', label: 'Due date' },
  { value: 'updated', label: 'Updated' },
];

export const VIEWS_ID_PREFIX = 'tk-views';

function ListIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />
    </svg>
  );
}

function BoardIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="7" height="16" rx="1.5" />
      <rect x="14" y="4" width="7" height="10" rx="1.5" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true" className="tk-search-ic">
      <circle cx="11" cy="11" r="7" />
      <path d="M20 20l-3.5-3.5" />
    </svg>
  );
}

export function TicketToolbar({
  params,
  onParams,
  counts,
  shown,
  total,
  viewMode,
  onViewMode,
  onNew,
  onHelp,
  searchRef,
  disabled = false,
}: {
  params: ListParams;
  onParams: (patch: Partial<ListParams>) => void;
  counts: Record<ViewId, number> | null;
  shown: number;
  total: number;
  viewMode: 'list' | 'board';
  onViewMode: (mode: 'list' | 'board') => void;
  onNew: () => void;
  /** Opens the keyboard shortcuts dialog. */
  onHelp: () => void;
  searchRef?: RefObject<HTMLInputElement | null>;
  disabled?: boolean;
}) {
  return (
    <div className="tk-toolbar">
      <div className="tk-titlerow">
        <h2 id="tickets-heading" className="tk-heading">
          Tickets
          {counts && (
            <span className="tk-heading-count">
              {shown === total ? `${total}` : `${shown} of ${total}`}
            </span>
          )}
        </h2>
        <div className="tk-search">
          <SearchIcon />
          <input
            ref={searchRef}
            type="search"
            aria-keyshortcuts="/"
            aria-label="Search tickets"
            placeholder="Search titles, ids, people, Jira keys"
            value={params.q}
            disabled={disabled}
            onChange={(e) => onParams({ q: e.target.value })}
          />
          <span className="tk-search-kbd" aria-hidden="true">
            <Kbd>/</Kbd>
          </span>
        </div>
        <div className="tk-seg" role="group" aria-label="Layout">
          <button type="button" aria-pressed={viewMode === 'list'} onClick={() => onViewMode('list')}>
            <ListIcon />
            List
          </button>
          <button type="button" aria-pressed={viewMode === 'board'} onClick={() => onViewMode('board')}>
            <BoardIcon />
            Board
          </button>
        </div>
        <Button variant="primary" size="sm" onClick={onNew} className="tk-new" aria-keyshortcuts="C">
          New ticket
          <kbd className="tk-new-kbd" aria-hidden="true">C</kbd>
        </Button>
      </div>

      <Tabs
        bare
        variant="line"
        idPrefix={VIEWS_ID_PREFIX}
        aria-label="Saved views"
        activeKey={params.view}
        onChange={(key) => onParams({ view: key as ViewId })}
        tabs={VIEWS.map((v) => {
          const n = counts?.[v.id];
          const tone = n && v.tone ? ` is-${v.tone}` : '';
          return {
            key: v.id,
            label: (
              <span className="tk-view">
                {v.label}
                {typeof n === 'number' && (
                  <>
                    <span className={`tk-view-n${tone}`} aria-hidden="true">
                      {n}
                    </span>
                    <span className="sr-only">{` (${n} ${n === 1 ? 'ticket' : 'tickets'})`}</span>
                  </>
                )}
              </span>
            ),
          };
        })}
      />

      <div className="tk-filters">
        {viewMode === 'list' && (
          <div className="tk-fsel">
            <span className="tk-fsel-label" aria-hidden="true">Group</span>
            <Select
              aria-label="Group by"
              value={params.group}
              onValueChange={(v) => onParams({ group: v as GroupBy })}
              options={GROUP_OPTIONS}
              className="w-auto"
            />
          </div>
        )}
        <div className="tk-fsel">
          <span className="tk-fsel-label" aria-hidden="true">Sort</span>
          <Select
            aria-label="Sort by"
            value={params.sort}
            onValueChange={(v) => onParams({ sort: v as SortBy })}
            options={SORT_OPTIONS}
            className="w-auto"
          />
        </div>
        <button
          type="button"
          className="tk-chip"
          aria-pressed={params.issuesOnly}
          onClick={() => onParams({ issuesOnly: !params.issuesOnly })}
        >
          Issues only
        </button>
        <button
          type="button"
          className="tk-chip"
          aria-pressed={params.highOnly}
          onClick={() => onParams({ highOnly: !params.highOnly })}
        >
          High + critical
        </button>
        {params.view === 'all' && (
          <button
            type="button"
            className="tk-chip"
            aria-pressed={params.showFinished}
            onClick={() => onParams({ showFinished: !params.showFinished })}
          >
            Show finished
          </button>
        )}
        <button type="button" className="tk-keyhint" onClick={onHelp} aria-keyshortcuts="?">
          <span aria-hidden="true">
            <Kbd>J</Kbd> <Kbd>K</Kbd> move <Kbd>↵</Kbd> open <Kbd>X</Kbd> select <Kbd>?</Kbd> all keys
          </span>
          <span className="sr-only">Keyboard shortcuts</span>
        </button>
      </div>
    </div>
  );
}
