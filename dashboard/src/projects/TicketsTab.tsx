/**
 * Tickets tab: a list that answers "what needs me?".
 *
 * Saved views with counts (Mine, Triage, Needs review, Blocked, Overdue,
 * All), group / sort, quick filters and search, over one-line rows that
 * carry the signals that decide the next step (review verdict, blockers,
 * Issue progress, Jira, due). The whole project list is fetched once and
 * filtered here; list state lives in the page URL when the page owns it.
 *
 * The dashboard only makes the moderation moves (approve, dismiss, close an
 * Issue) and reassigns. Start, complete, resolve, block and escalate need
 * the active-ticket bundle on the agent's machine, so they stay in the CLI /
 * MCP, and the status menu says so.
 *
 * The board stays view-only: no drag-and-drop, since every status move it
 * could suggest is CLI/MCP-only.
 *
 * Keyboard (useTicketKeys, listed by ?): j/k move between rows (one row is
 * in the tab order at a time), Enter/o open, x selects, s/a open the
 * status / assignee menu, c, /, b, 1-6 and [ ]. Selected rows get a bulk
 * bar (BulkBar).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { TicketViewerContext, type TicketViewer } from './tickets/viewer';
import { useTickets } from '../hooks/useTickets';
import { Alert, Button, TabPanel } from '../components/ui';
import type { TicketJiraSurface } from './tickets/constants';
import { BoardView } from './tickets/BoardView';
import { BulkBar } from './tickets/BulkBar';
import { NewTicketModal } from './tickets/NewTicketModal';
import { ShortcutsDialog } from './tickets/ShortcutsDialog';
import { TicketList } from './tickets/TicketList';
import { SPLIT_QUERY, TicketPane } from './tickets/detail/TicketPane';
import { TicketToolbar, VIEWS_ID_PREFIX } from './tickets/TicketToolbar';
import { selectRange, toggleId, visibleSelection } from './tickets/selection';
import { useTicketKeys, type TicketKeyAction, type TicketKeyContext } from './tickets/useTicketKeys';
import {
  DEFAULT_PARAMS,
  VIEWS,
  boardOrder,
  groupTickets,
  hiddenFinishedCount,
  indexById,
  viewCounts,
  viewDef,
  visibleTickets,
  type ListParams,
} from './tickets/views';
import { errorMessage } from '../utils/errorMessage';
import { Term } from '../help/Term';
import './tickets/tickets.css';

const DEFAULT_VIEWER: TicketViewer = { canEdit: true, userId: null };

interface TicketsTabProps {
  projectId: string;
  jira?: TicketJiraSurface;
  /** Ticket id from a deep link (?ticket=…): opened in the ticket pane. */
  initialTicketId?: string | null;
  /** Pane tab from a deep link (?ticket_tab=overview|activity|review|children|jira). */
  initialTicketTab?: string | null;
  /** The page URL owns the open ticket and its tab: called when either
   *  changes here (`tab` null = the default Overview). */
  onOpenTicketChange?: (ticketId: string | null, tab: string | null) => void;
  /** Who is viewing: hides actions the server would refuse (see tickets/viewer). */
  viewer?: TicketViewer;
  /** List state (view, group, sort, search, quick filters), owned by the
   *  page URL when provided; local state otherwise. */
  params?: ListParams;
  onParamsChange?: (next: ListParams) => void;
}

export default function TicketsTab({
  projectId,
  jira,
  initialTicketId,
  initialTicketTab,
  onOpenTicketChange,
  viewer,
  params: paramsProp,
  onParamsChange,
}: TicketsTabProps) {
  const [paramsLocal, setParamsLocal] = useState<ListParams>(DEFAULT_PARAMS);
  const params = paramsProp ?? paramsLocal;
  const setParams = (next: ListParams) => {
    if (onParamsChange) onParamsChange(next);
    else setParamsLocal(next);
  };
  const patch = (p: Partial<ListParams>) => setParams({ ...params, ...p });

  const { data, isLoading, error, refetch } = useTickets(projectId);
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(initialTicketId ?? null);
  // A deep link that changes while the tab is mounted (a notification
  // clicked from this page) opens that ticket too.
  const [linkedId, setLinkedId] = useState(initialTicketId);
  if (initialTicketId !== linkedId) {
    setLinkedId(initialTicketId);
    if (initialTicketId) setSelected(initialTicketId);
  }
  // Mirror the open ticket into the page URL. From an effect, so it runs
  // after a navigation's list-params write has rendered (two URL writes in
  // one tick would overwrite each other).
  const reported = useRef(initialTicketId ?? null);
  useEffect(() => {
    if (reported.current === selected) return;
    reported.current = selected;
    onOpenTicketChange?.(selected, null);
  }, [selected, onOpenTicketChange]);
  const rootRef = useRef<HTMLElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [checked, setChecked] = useState<Set<string>>(() => new Set());
  const [anchor, setAnchor] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [bulkAssignOpen, setBulkAssignOpen] = useState(false);
  const [viewMode, setViewMode] = useState<'list' | 'board'>(() => {
    try {
      return window.localStorage.getItem('sfs-tickets-view') === 'board' ? 'board' : 'list';
    } catch {
      return 'list';
    }
  });

  function handleViewMode(mode: 'list' | 'board') {
    setViewMode(mode);
    // Selection and bulk actions are a list feature.
    if (mode === 'board') setChecked(new Set());
    try {
      window.localStorage.setItem('sfs-tickets-view', mode);
    } catch {
      /* noop */
    }
  }

  const resolvedViewer = viewer ?? DEFAULT_VIEWER;
  const userId = resolvedViewer.userId;
  const all = useMemo(() => data ?? [], [data]);
  const byId = useMemo(() => indexById(all), [all]);
  const counts = useMemo(() => (data ? viewCounts(all, userId) : null), [data, all, userId]);
  const visible = useMemo(() => {
    const list = visibleTickets(all, params, userId);
    // A deep-linked finished ticket stays visible in All without unfolding
    // every other finished ticket.
    const keep = selected ? byId.get(selected) : undefined;
    if (keep && !list.includes(keep) && visibleTickets([keep], { ...params, showFinished: true }, userId).length) {
      return [...list, keep];
    }
    return list;
  }, [all, byId, params, userId, selected]);
  const groups = useMemo(
    () => groupTickets(visible, params.group, params.sort, { userId, all }),
    [visible, params.group, params.sort, userId, all],
  );
  // Prev / next in the pane walk the rows in the order they are shown.
  const paneOrder = useMemo(
    () => [
      ...new Map(
        (viewMode === 'board'
          ? boardOrder(visible.filter((t) => t.status !== 'cancelled'), params.sort)
          : groups.flatMap((g) => g.rows.map((r) => r.ticket))
        ).map((t) => [t.id, t]),
      ).values(),
    ],
    [viewMode, visible, groups, params.sort],
  );
  const orderIds = useMemo(() => paneOrder.map((t) => t.id), [paneOrder]);
  const hiddenFinished = useMemo(() => hiddenFinishedCount(all, params), [all, params]);
  const cancelledCount = useMemo(
    () => visibleTickets(all, { ...params, showFinished: true }, userId).filter((t) => t.status === 'cancelled').length,
    [all, params, userId],
  );

  // Roving tabindex: the focused row, else the open one, else the first.
  const tabStopId =
    focusId && orderIds.includes(focusId)
      ? focusId
      : selected && orderIds.includes(selected)
        ? selected
        : (orderIds[0] ?? null);
  // Bulk actions only reach selected rows that are on screen.
  const selectedIds = viewMode === 'list' ? visibleSelection(checked, orderIds) : [];
  const selectedTickets = selectedIds.map((id) => byId.get(id)).filter((t): t is NonNullable<typeof t> => !!t);

  function onCheck(id: string, range: boolean) {
    setChecked((prev) => (range ? selectRange(orderIds, anchor, id, prev) : toggleId(prev, id)));
    setAnchor(id);
  }

  /** Rows (or cards) as laid out, so rows in collapsed groups are skipped. */
  const rowEls = useCallback(
    () =>
      Array.from(
        rootRef.current?.querySelectorAll<HTMLElement>('.tk-groups [data-ticket-row], .tk-board [data-ticket-row]') ?? [],
      ),
    [],
  );

  const onKey = (action: TicketKeyAction, { scope }: TicketKeyContext): boolean => {
    const splitOpen = !!selected && viewMode === 'list' && window.matchMedia?.(SPLIT_QUERY).matches === true;
    const paneTarget = scope === 'pane' || splitOpen;
    const pane = () => rootRef.current?.querySelector<HTMLElement>('[data-ticket-pane]') ?? document.querySelector<HTMLElement>('[data-ticket-pane]');
    const row = (id: string | null) =>
      id ? (rootRef.current?.querySelector<HTMLElement>(`.tk-groups [data-ticket-id="${CSS.escape(id)}"]`) ?? null) : null;
    const focusedRowId = () => {
      const el = (document.activeElement as HTMLElement | null)?.closest?.<HTMLElement>('[data-ticket-id], [data-ticket-row]');
      const fromDom = el?.dataset.ticketId ?? el?.dataset.ticketRow ?? null;
      if (fromDom && orderIds.includes(fromDom)) return fromDom;
      return focusId && orderIds.includes(focusId) ? focusId : null;
    };
    switch (action.type) {
      case 'move': {
        if (scope === 'pane') {
          const i = selected ? orderIds.indexOf(selected) : -1;
          const next = i >= 0 ? orderIds[i + action.delta] : undefined;
          if (next) setSelected(next);
          return true;
        }
        const rows = rowEls();
        if (!rows.length) return false;
        const cur = focusedRowId() ?? selected;
        const at = cur ? rows.findIndex((r) => r.dataset.ticketRow === cur) : -1;
        const nextIdx =
          at < 0 ? (action.delta > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, at + action.delta));
        const el = rows[nextIdx];
        const id = el.dataset.ticketRow!;
        setFocusId(id);
        el.focus({ preventScroll: true });
        el.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
        // Beside the list, the open ticket follows the focused row.
        if (splitOpen) setSelected(id);
        return true;
      }
      case 'open': {
        const ae = document.activeElement as HTMLElement | null;
        // Enter on any other control does that control's job.
        if (ae && ae !== document.body && !ae.dataset.ticketRow && ae.matches('button, a, input, summary, [role="tab"]')) {
          return false;
        }
        const id = focusedRowId();
        if (!id) return false;
        setSelected(id);
        return true;
      }
      case 'escape':
        if (!selectedIds.length) return false;
        setChecked(new Set());
        return true;
      case 'toggleSelect': {
        const id = focusedRowId();
        if (!id || viewMode !== 'list') return false;
        onCheck(id, false);
        return true;
      }
      case 'status':
      case 'assign': {
        if (action.type === 'assign' && scope === 'tab' && selectedIds.length) {
          setBulkAssignOpen(true);
          return true;
        }
        const where = paneTarget ? pane() : row(focusedRowId());
        const trigger = where?.querySelector<HTMLElement>(action.type === 'status' ? '[data-tk-status]' : '[data-tk-assign]');
        if (!trigger) return false;
        trigger.click();
        return true;
      }
      case 'new':
        setCreating(true);
        return true;
      case 'search':
        if (!searchRef.current) return false;
        searchRef.current.focus();
        return true;
      case 'toggleLayout':
        handleViewMode(viewMode === 'list' ? 'board' : 'list');
        return true;
      case 'view': {
        const v = VIEWS[action.index];
        if (!v) return false;
        patch({ view: v.id });
        return true;
      }
      case 'paneTab': {
        if (!selected) return false;
        const tabs = Array.from(pane()?.querySelectorAll<HTMLElement>('[role="tablist"] [role="tab"]') ?? []);
        if (!tabs.length) return false;
        const at = tabs.findIndex((t) => t.getAttribute('aria-selected') === 'true');
        const next = tabs[(at + action.delta + tabs.length) % tabs.length];
        next.click();
        next.focus();
        return true;
      }
      case 'help':
        setHelpOpen(true);
        return true;
    }
    return false;
  };
  useTicketKeys(rootRef, onKey, !!data);


  /**
   * Open a child Task or parent Issue from the detail. Resets the view and
   * filters so the target is guaranteed to be in the list.
   */
  function navigateTo(targetId: string) {
    const target = byId.get(targetId);
    setParams({ ...DEFAULT_PARAMS, group: params.group, sort: params.sort, showFinished: !!target && ['done', 'closed', 'cancelled'].includes(target.status) });
    setSelected(targetId);
  }

  const filtersOn = !!params.q.trim() || params.issuesOnly || params.highOnly;
  let body;
  if (isLoading) {
    body = (
      <div className="tk-rows tk-skeleton" aria-busy="true" aria-label="Loading tickets">
        {Array.from({ length: 8 }, (_, i) => (
          <div key={i} className="tk-skel-row">
            <i className="tk-skel-dot" />
            <i style={{ width: 84 }} />
            <i style={{ width: `${30 + ((i * 13) % 40)}%` }} />
            <i className="tk-skel-end" style={{ width: 60 }} />
            <i style={{ width: 90 }} />
          </div>
        ))}
      </div>
    );
  } else if (error) {
    body = (
      <Alert variant="danger" action={<Button variant="secondary" size="sm" onClick={() => void refetch()}>Retry</Button>}>
        Couldn’t load tickets: {errorMessage(error)}
      </Alert>
    );
  } else if (all.length === 0) {
    body = (
      <div className="tk-empty">
        <h3>No tickets in this project yet</h3>
        <p>
          Tickets are how people and agents hand work to each other. Agents file them with <code>create_ticket</code>{' '}
          (MCP) or <code>sfs ticket create</code>; those arrive as <Term id="ticket-suggested">Suggested</Term> for
          someone to approve.
        </p>
        <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
          New ticket
        </Button>
      </div>
    );
  } else if (visible.length === 0 && hiddenFinished === 0) {
    const [title, text] = params.q.trim()
      ? [`No tickets match “${params.q.trim()}”`, 'Search looks at titles, ids, descriptions, assignees and Jira keys.']
      : viewDef(params.view).empty;
    body = (
      <div className="tk-empty">
        <h3>{title}</h3>
        {text && <p>{text}</p>}
        {filtersOn && (
          <Button variant="secondary" size="sm" onClick={() => patch({ q: '', issuesOnly: false, highOnly: false })}>
            Clear filters
          </Button>
        )}
      </div>
    );
  } else if (viewMode === 'board') {
    body = (
      <BoardView
        tickets={visible}
        byId={byId}
        sort={params.sort}
        selected={selected}
        onSelect={(id) => setSelected(selected === id ? null : id)}
        tabStopId={tabStopId}
        onFocusCard={setFocusId}
        finishedHidden={params.view === 'all' && !params.showFinished}
        cancelledCount={cancelledCount}
        onShowCancelled={() => {
          handleViewMode('list');
          patch({ view: 'all', showFinished: true });
        }}
      />
    );
  } else {
    body = (
      <TicketList
        groups={groups}
        byId={byId}
        projectId={projectId}
        selected={selected}
        onSelect={(id) => setSelected(selected === id ? null : id)}
        jira={jira}
        hiddenFinished={hiddenFinished}
        onShowFinished={() => patch({ showFinished: true })}
        checked={checked}
        onCheck={onCheck}
        tabStopId={tabStopId}
        onFocusRow={setFocusId}
      />
    );
  }

  return (
    <TicketViewerContext.Provider value={resolvedViewer}>
      <section ref={rootRef} aria-labelledby="tickets-heading" className={`tkl${selectedIds.length ? ' is-selecting' : ''}`}>
        <TicketToolbar
          params={params}
          onParams={patch}
          counts={counts}
          shown={visible.length}
          total={all.length}
          viewMode={viewMode}
          onViewMode={handleViewMode}
          onNew={() => setCreating(true)}
          onHelp={() => setHelpOpen(true)}
          searchRef={searchRef}
          disabled={isLoading}
        />
        <p className="sr-only" aria-live="polite">
          {data ? `${visible.length} ${visible.length === 1 ? 'ticket' : 'tickets'} shown` : ''}
        </p>
        <TabPanel idPrefix={VIEWS_ID_PREFIX} tabKey={params.view} className="tk-body">
          {/* The ticket pane owns the open ticket's detail in every layout:
              beside the list, as a slide-over, or full screen on phones. */}
          <TicketPane
            projectId={projectId}
            ticketId={selected}
            tickets={paneOrder}
            viewMode={viewMode}
            onStep={setSelected}
            onNavigate={navigateTo}
            onClose={() => setSelected(null)}
            jira={jira}
            initialTab={initialTicketTab}
            onTabChange={(tab) => onOpenTicketChange?.(selected, tab === 'overview' ? null : tab)}
          >
            {body}
          </TicketPane>
        </TabPanel>

        {selectedTickets.length > 0 && (
          <BulkBar
            projectId={projectId}
            selected={selectedTickets}
            onSettled={(keep) => setChecked(new Set(keep))}
            onClear={() => setChecked(new Set())}
            orgId={jira?.orgId}
            isProjectAdmin={resolvedViewer.isProjectAdmin ?? jira?.isProjectAdmin}
            assignOpen={bulkAssignOpen}
            onAssignOpenChange={setBulkAssignOpen}
          />
        )}

        {creating && (
          <NewTicketModal
            projectId={projectId}
            orgId={jira?.orgId}
            onClose={() => setCreating(false)}
            onCreated={(t) => setSelected(t.id)}
          />
        )}
        <ShortcutsDialog open={helpOpen} onClose={() => setHelpOpen(false)} />
      </section>
    </TicketViewerContext.Provider>
  );
}
