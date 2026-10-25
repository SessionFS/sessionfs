/**
 * v0.10.3 — Tickets tab (v0.10.1 backend, v0.10.3 dashboard surface).
 *
 * Read-mostly moderation surface for project tickets. Lists tickets
 * filtered by status, expands one at a time into a detail panel showing
 * description / acceptance criteria / comments / dependencies, and
 * exposes the human-driven FSM transitions: approve (suggested→open),
 * dismiss (suggested/open→cancelled), close (in_progress→closed), and
 * comment.
 *
 * Lifecycle transitions tied to local provenance (start, complete,
 * resolve, block, escalate) live in the CLI / MCP — they require the
 * active-ticket bundle on the agent's machine.
 *
 * Phase 3 restyle: migrated onto ui/ primitives + added board view
 * with list/board toggle persisted as localStorage sfs-tickets-view.
 * Board is view-only — no drag-and-drop because every FSM transition
 * that changes status (start/complete/resolve/block/escalate) is
 * CLI/MCP-only. Dashboard actions (approve/dismiss/close) stay as
 * explicit buttons on the card.
 */

import { useState } from 'react';
import { useTickets } from '../hooks/useTickets';
import { Button, IconButton, Select } from '../components/ui';
import { KIND_FILTERS, STATUS_FILTERS, type TicketJiraSurface } from './tickets/constants';
import { ListView } from './tickets/ListView';
import { BoardView } from './tickets/BoardView';
import { EmptyState } from './tickets/EmptyState';
import { NewTicketModal } from './tickets/NewTicketModal';
import { errorMessage } from '../utils/errorMessage';

interface TicketsTabProps {
  projectId: string;
  jira?: TicketJiraSurface;
  /** Ticket id from a deep link (?ticket=…) — expanded on first mount. */
  initialTicketId?: string | null;
}

export default function TicketsTab({ projectId, jira, initialTicketId }: TicketsTabProps) {
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [kindFilter, setKindFilter] = useState<string>('');
  const { data, isLoading, error } = useTickets(projectId, {
    status: statusFilter || undefined,
    kind: kindFilter || undefined,
  });
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(initialTicketId ?? null);
  const [viewMode, setViewMode] = useState<'list' | 'board'>(() => {
    const stored =
      typeof window !== 'undefined'
        ? window.localStorage.getItem('sfs-tickets-view')
        : null;
    return stored === 'board' ? 'board' : 'list';
  });

  function handleViewMode(mode: 'list' | 'board') {
    setViewMode(mode);
    try {
      window.localStorage.setItem('sfs-tickets-view', mode);
    } catch {
      /* noop */
    }
  }

  /**
   * Re-target the expanded row to a child Task or parent Issue. Resets
   * status + kind filters so the navigation target is guaranteed visible
   * (otherwise clicking the breadcrumb on a Task with a `closed` parent
   * while filtered to `in_progress` would silently drop the parent row).
   */
  function navigateTo(targetId: string) {
    setStatusFilter('');
    setKindFilter('');
    setSelected(targetId);
  }

  if (isLoading) return <p>Loading tickets…</p>;
  if (error) return <p role="alert">Failed to load tickets: {errorMessage(error)}</p>;
  if (!data) return null;

  const tickets = data;

  return (
    <section aria-labelledby="tickets-heading" className="space-y-4">
      {/* ── Header row ── */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h2 id="tickets-heading" className="text-lg font-semibold">
          Tickets
          <span className="ml-2 text-sm text-text-tertiary">
            {tickets.length} {tickets.length === 1 ? 'ticket' : 'tickets'}
          </span>
        </h2>
        <div className="flex items-center gap-2 flex-wrap">
          <Select
            aria-label="Filter by kind"
            value={kindFilter}
            onValueChange={setKindFilter}
            options={KIND_FILTERS}
            className="w-auto"
          />
          <Select
            aria-label="Filter by status"
            value={statusFilter}
            onValueChange={setStatusFilter}
            options={STATUS_FILTERS}
            className="w-auto"
          />

          {/* List / Board toggle */}
          <div className="flex items-center rounded-lg border border-border bg-surface overflow-hidden">
            <IconButton
              label="List view"
              variant="ghost"
              onClick={() => handleViewMode('list')}
              aria-pressed={viewMode === 'list'}
              className={`!rounded-none ${viewMode === 'list' ? '!bg-bg-elevated !text-text-primary' : ''}`}
            >
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <line x1="8" y1="6" x2="21" y2="6" />
                <line x1="8" y1="12" x2="21" y2="12" />
                <line x1="8" y1="18" x2="21" y2="18" />
                <line x1="3" y1="6" x2="3.01" y2="6" />
                <line x1="3" y1="12" x2="3.01" y2="12" />
                <line x1="3" y1="18" x2="3.01" y2="18" />
              </svg>
            </IconButton>
            <IconButton
              label="Board view"
              variant="ghost"
              onClick={() => handleViewMode('board')}
              aria-pressed={viewMode === 'board'}
              className={`!rounded-none ${viewMode === 'board' ? '!bg-bg-elevated !text-text-primary' : ''}`}
            >
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <rect x="3" y="3" width="7" height="7" />
                <rect x="14" y="3" width="7" height="7" />
                <rect x="3" y="14" width="7" height="7" />
                <rect x="14" y="14" width="7" height="7" />
              </svg>
            </IconButton>
          </div>

          <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
            New ticket
          </Button>
        </div>
      </div>

      {/* ── Content ── */}
      {tickets.length === 0 ? (
        <EmptyState kindFilter={kindFilter} statusFilter={statusFilter} />
      ) : viewMode === 'board' ? (
        <BoardView
          tickets={tickets}
          projectId={projectId}
          selected={selected}
          onSelect={(id) => setSelected(selected === id ? null : id)}
          onNavigate={navigateTo}
          jira={jira}
        />
      ) : (
        <ListView
          tickets={tickets}
          projectId={projectId}
          selected={selected}
          onSelect={(id) => setSelected(selected === id ? null : id)}
          onNavigate={navigateTo}
          jira={jira}
        />
      )}

      {/* ── New ticket modal ── */}
      {creating && (
        <NewTicketModal
          projectId={projectId}
          onClose={() => setCreating(false)}
        />
      )}
    </section>
  );
}
