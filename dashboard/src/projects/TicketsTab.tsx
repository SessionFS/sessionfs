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

import { useEffect, useState } from 'react';
import { useToast } from '../hooks/useToast';
import {
  useAddTicketComment,
  useApproveTicket,
  useCloseTicket,
  useCreateTicket,
  useDismissTicket,
  useTicket,
  useTicketChildren,
  useTicketComments,
  useTickets,
} from '../hooks/useTickets';
import RelativeDate from '../components/RelativeDate';
import { ApiError, type Ticket } from '../api/client';
import {
  Button,
  Dialog,
  DialogHeader,
  DialogFooter,
  Drawer,
  Input,
  Select,
  Textarea,
} from '../components/ui';

interface TicketsTabProps {
  projectId: string;
}

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'suggested', label: 'Suggested' },
  { value: 'open', label: 'Open' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'blocked', label: 'Blocked' },
  { value: 'review', label: 'Review' },
  { value: 'done', label: 'Done' },
  { value: 'closed', label: 'Closed' },
  { value: 'cancelled', label: 'Cancelled' },
];

const KIND_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'issue', label: 'Issues' },
  { value: 'task', label: 'Tasks' },
];

/** Statuses that appear as board columns (all non-empty filter values). */
const BOARD_STATUSES = STATUS_FILTERS.filter((s) => s.value !== '');

const STATUS_TONE: Record<string, string> = {
  suggested: 'bg-amber-500/15 text-amber-600',
  open: 'bg-blue-500/15 text-blue-600',
  in_progress: 'bg-brand/15 text-brand',
  blocked: 'bg-orange-500/15 text-orange-600',
  review: 'bg-purple-500/15 text-purple-600',
  done: 'bg-emerald-500/15 text-emerald-600',
  closed: 'bg-emerald-500/15 text-emerald-600',
  cancelled: 'bg-[var(--border)]/30 text-[var(--text-tertiary)]',
};

const KIND_TONE: Record<string, string> = {
  issue: 'bg-indigo-500/15 text-indigo-600',
  task: 'bg-[var(--border)]/30 text-[var(--text-tertiary)]',
};

const PRIORITY_TONE: Record<string, string> = {
  low: 'text-[var(--text-tertiary)]',
  medium: '',
  high: 'text-amber-600',
  critical: 'text-[var(--danger)]',
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ${
        STATUS_TONE[status] ?? 'bg-[var(--border)]/30 text-[var(--text-tertiary)]'
      }`}
    >
      {status}
    </span>
  );
}

function KindBadge({ kind }: { kind: string }) {
  const label = kind === 'issue' ? 'Issue' : 'Task';
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
        KIND_TONE[kind] ?? KIND_TONE.task
      }`}
    >
      {label}
    </span>
  );
}

export default function TicketsTab({ projectId }: TicketsTabProps) {
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [kindFilter, setKindFilter] = useState<string>('');
  const { data, isLoading, error } = useTickets(projectId, {
    status: statusFilter || undefined,
    kind: kindFilter || undefined,
  });
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
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
  if (error) return <p role="alert">Failed to load tickets: {String(error)}</p>;
  if (!data) return null;

  const tickets = data;

  return (
    <section aria-labelledby="tickets-heading" className="space-y-4">
      {/* ── Header row ── */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h2 id="tickets-heading" className="text-lg font-semibold">
          Tickets
          <span className="ml-2 text-sm text-[var(--text-tertiary)]">
            {tickets.length} {tickets.length === 1 ? 'ticket' : 'tickets'}
          </span>
        </h2>
        <div className="flex items-center gap-2">
          <Select
            aria-label="Filter by kind"
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value)}
            options={KIND_FILTERS}
            className="w-auto"
          />
          <Select
            aria-label="Filter by status"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            options={STATUS_FILTERS}
            className="w-auto"
          />

          {/* List / Board toggle */}
          <div className="flex items-center rounded-lg border border-[var(--border)] bg-[var(--surface)] overflow-hidden">
            <button
              type="button"
              onClick={() => handleViewMode('list')}
              className={`p-2 transition-colors ${
                viewMode === 'list'
                  ? 'bg-[var(--bg-elevated)] text-[var(--text-primary)]'
                  : 'text-[var(--text-tertiary)] hover:text-[var(--text-primary)]'
              }`}
              title="List view"
              aria-label="List view"
              aria-pressed={viewMode === 'list'}
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
              >
                <line x1="8" y1="6" x2="21" y2="6" />
                <line x1="8" y1="12" x2="21" y2="12" />
                <line x1="8" y1="18" x2="21" y2="18" />
                <line x1="3" y1="6" x2="3.01" y2="6" />
                <line x1="3" y1="12" x2="3.01" y2="12" />
                <line x1="3" y1="18" x2="3.01" y2="18" />
              </svg>
            </button>
            <button
              type="button"
              onClick={() => handleViewMode('board')}
              className={`p-2 transition-colors ${
                viewMode === 'board'
                  ? 'bg-[var(--bg-elevated)] text-[var(--text-primary)]'
                  : 'text-[var(--text-tertiary)] hover:text-[var(--text-primary)]'
              }`}
              title="Board view"
              aria-label="Board view"
              aria-pressed={viewMode === 'board'}
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
              >
                <rect x="3" y="3" width="7" height="7" />
                <rect x="14" y="3" width="7" height="7" />
                <rect x="3" y="14" width="7" height="7" />
                <rect x="14" y="14" width="7" height="7" />
              </svg>
            </button>
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
        />
      ) : (
        <ListView
          tickets={tickets}
          projectId={projectId}
          selected={selected}
          onSelect={(id) => setSelected(selected === id ? null : id)}
          onNavigate={navigateTo}
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

/* ── List view ── */

interface ViewProps {
  tickets: Ticket[];
  projectId: string;
  selected: string | null;
  onSelect: (id: string) => void;
  onNavigate: (targetId: string) => void;
}

function ListView({ tickets, projectId, selected, onSelect, onNavigate }: ViewProps) {
  return (
    <ul className="border border-[var(--border)] rounded-lg divide-y divide-[var(--border)]">
      {tickets.map((t) => (
        <li key={t.id}>
          <button
            type="button"
            className={`w-full text-left px-3 py-2 hover:bg-[var(--surface)] flex items-start gap-3 border-l-2 ${
              t.kind === 'issue' ? 'border-indigo-500/60' : 'border-transparent'
            }`}
            onClick={() => onSelect(t.id)}
            aria-expanded={selected === t.id}
          >
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 text-sm flex-wrap">
                <span className="text-mono-chip">{t.id}</span>
                <KindBadge kind={t.kind} />
                <StatusBadge status={t.status} />
                <span
                  className={`text-xs uppercase tracking-wide ${PRIORITY_TONE[t.priority] ?? ''}`}
                >
                  {t.priority}
                </span>
                {t.assigned_to && (
                  <span className="text-xs text-[var(--text-tertiary)]">
                    → {t.assigned_to}
                  </span>
                )}
              </div>
              <div className="font-medium truncate text-[var(--text-primary)]">
                {t.title}
              </div>
              <div className="text-xs text-[var(--text-tertiary)]">
                <RelativeDate iso={t.updated_at} /> · {t.acceptance_criteria.length}{' '}
                criteria
                {t.depends_on.length > 0 ? ` · ${t.depends_on.length} deps` : ''}
                {t.kind === 'issue' && t.child_ticket_ids.length > 0
                  ? ` · ${t.child_ticket_ids.length} child ${
                      t.child_ticket_ids.length === 1 ? 'task' : 'tasks'
                    }`
                  : ''}
              </div>
            </div>
          </button>
          {selected === t.id && (
            <TicketDetail
              projectId={projectId}
              ticketId={t.id}
              fallback={t}
              onNavigate={onNavigate}
            />
          )}
        </li>
      ))}
    </ul>
  );
}

/* ── Board view ──
 *
 * Columns are derived from the statuses the tab already knows. There is
 * NO drag-and-drop: every FSM transition that changes status
 * (start/complete/resolve/block/escalate) is CLI/MCP-only and must not
 * be draggable. The only dashboard-side transitions are button actions
 * (approve/dismiss/close) which appear as explicit buttons inside the
 * expanded detail — not via drag. */

function BoardView({ tickets, projectId, selected, onSelect, onNavigate }: ViewProps) {
  const selectedTicket = tickets.find((t) => t.id === selected) ?? null;

  return (
    <>
      <div className="overflow-x-auto -mx-1 px-1">
        <div className="flex gap-3 min-w-[1100px]">
          {BOARD_STATUSES.map((status) => {
            const columnTickets = tickets.filter((t) => t.status === status.value);
            return (
              <div key={status.value} className="flex-1 min-w-[180px] max-w-[260px]">
                {/* Column header */}
                <h3 className="text-micro uppercase font-semibold text-[var(--text-tertiary)] mb-2 px-1 flex items-center gap-1.5">
                  {status.label}
                  <span className="text-[var(--text-tertiary)]/60 tabular-nums">
                    {columnTickets.length}
                  </span>
                </h3>

                {columnTickets.length === 0 ? (
                  <div className="border border-dashed border-[var(--border)] rounded-lg p-3 text-[12px] text-[var(--text-tertiary)] text-center">
                    —
                  </div>
                ) : (
                  <div className="space-y-2">
                    {columnTickets.map((t) => (
                      <BoardCard
                        key={t.id}
                        ticket={t}
                        isExpanded={selected === t.id}
                        onToggle={() => onSelect(t.id)}
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Board detail drawer — right-side panel instead of in-column expansion */}
      <Drawer
        open={selected !== null}
        onClose={() => onSelect(selected!)}
        titleId="ticket-detail-title"
      >
        {selectedTicket && (
          <div className="space-y-3">
            <h2
              id="ticket-detail-title"
              className="text-lg font-semibold text-[var(--text-primary)]"
            >
              {selectedTicket.title}
            </h2>
            <TicketDetail
              projectId={projectId}
              ticketId={selectedTicket.id}
              fallback={selectedTicket}
              onNavigate={onNavigate}
            />
          </div>
        )}
      </Drawer>
    </>
  );
}

/* ── Board card ── */

function BoardCard({
  ticket: t,
  isExpanded,
  onToggle,
}: {
  ticket: Ticket;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className={`w-full text-left bg-[var(--surface)] border border-[var(--border)] rounded-lg p-3 hover:bg-[var(--bg-elevated)] hover:border-[var(--border-strong)] transition-[background-color,border-color] duration-150 outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
        isExpanded ? 'border-[var(--brand)] ring-1 ring-[var(--brand)]/30' : ''
      }`}
      aria-expanded={isExpanded}
      aria-haspopup="dialog"
    >
      {/* Kind + ID row */}
      <div className="flex items-center gap-1.5 mb-1.5 flex-wrap">
        <KindBadge kind={t.kind} />
        <span className="text-mono-chip text-[11px]">{t.id.slice(0, 12)}</span>
      </div>

      {/* Title */}
      <div className="text-[13px] font-medium text-[var(--text-primary)] mb-2 leading-snug line-clamp-2">
        {t.title}
      </div>

      {/* Meta row */}
      <div className="flex items-center gap-1.5 flex-wrap text-[11px] text-[var(--text-tertiary)]">
        <span className={`uppercase tracking-wide font-semibold ${PRIORITY_TONE[t.priority] ?? ''}`}>
          {t.priority}
        </span>
        {t.assigned_to && (
          <span className="text-mono-chip text-[11px]">{t.assigned_to}</span>
        )}
        {t.acceptance_criteria.length > 0 && (
          <>
            <span className="opacity-40">·</span>
            <span className="tabular-nums">{t.acceptance_criteria.length} crit</span>
          </>
        )}
        {t.depends_on.length > 0 && (
          <>
            <span className="opacity-40">·</span>
            <span className="tabular-nums">{t.depends_on.length} dep</span>
          </>
        )}
      </div>
    </button>
  );
}

/* ── Empty state ── */

function EmptyState({
  kindFilter,
  statusFilter,
}: {
  kindFilter: string;
  statusFilter: string;
}) {
  if (kindFilter === 'issue') {
    return (
      <div className="border border-[var(--border)] rounded-lg p-6 text-center text-[var(--text-tertiary)] space-y-1">
        <p className="font-medium text-[15px] text-[var(--text-secondary)]">
          No Issues yet.
        </p>
        <p className="text-[13px]">
          An Issue is a PM-triaged container that rolls up one or more Tasks. File
          an Issue when a user-facing problem needs cross-team work; file a Task
          for a single executor's unit of work.
        </p>
      </div>
    );
  }
  return (
    <div className="border border-[var(--border)] rounded-lg p-6 text-center text-[var(--text-tertiary)]">
      No tickets{' '}
      {statusFilter
        ? `with status "${statusFilter}"`
        : kindFilter
          ? `of kind "${kindFilter}"`
          : 'yet'}
      .
    </div>
  );
}

/* ── Detail panel (unchanged logic, restyled onto primitives) ── */

interface DetailProps {
  projectId: string;
  ticketId: string;
  fallback: Ticket;
  onNavigate: (targetId: string) => void;
}

type Action = 'approve' | 'dismiss' | 'close' | 'comment';

function TicketDetail({ projectId, ticketId, fallback, onNavigate }: DetailProps) {
  const { data: detail } = useTicket(projectId, ticketId);
  const t = detail ?? fallback;
  const { data: comments } = useTicketComments(projectId, ticketId);
  const approve = useApproveTicket(projectId);
  const dismiss = useDismissTicket(projectId);
  const close = useCloseTicket(projectId);
  const addComment = useAddTicketComment(projectId);
  const { addToast } = useToast();
  const [draft, setDraft] = useState('');
  const [submitting, setSubmitting] = useState<Action | null>(null);

  const childIds = t.kind === 'issue' ? t.child_ticket_ids : [];
  const childQueries = useTicketChildren(projectId, childIds);
  const { data: parent } = useTicket(
    projectId,
    t.parent_ticket_id ?? undefined,
  );

  async function callAction(action: Action) {
    setSubmitting(action);
    try {
      if (action === 'approve') {
        await approve.mutateAsync(ticketId);
        addToast('success', 'Approved');
      } else if (action === 'dismiss') {
        await dismiss.mutateAsync(ticketId);
        addToast('success', 'Dismissed');
      } else if (action === 'close') {
        await close.mutateAsync(ticketId);
        addToast('success', 'Issue closed');
      } else if (action === 'comment') {
        if (!draft.trim()) return;
        await addComment.mutateAsync({ ticketId, content: draft.trim() });
        setDraft('');
      }
    } catch (exc) {
      const msg =
        exc instanceof ApiError ? `${exc.status}: ${exc.message}` : String(exc);
      addToast('error', msg);
    } finally {
      setSubmitting(null);
    }
  }

  const canApprove = t.status === 'suggested';
  const canDismiss = t.status === 'suggested' || t.status === 'open';
  const canClose = t.kind === 'issue' && t.status === 'in_progress';

  return (
    <div className="px-3 py-3 bg-[var(--surface)]/50 text-sm space-y-3 border-t border-[var(--border)]">
      {t.kind === 'task' && t.parent_ticket_id && (
        <div className="text-xs">
          <button
            type="button"
            className="text-[var(--brand)] hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded"
            onClick={() => onNavigate(t.parent_ticket_id!)}
          >
            ← Back to parent Issue:{' '}
            <span className="font-mono">{t.parent_ticket_id}</span>
            {parent ? (
              <>
                {' '}
                — {parent.title}{' '}
                <span className="text-[var(--text-tertiary)]">
                  ({parent.status})
                </span>
              </>
            ) : null}
          </button>
        </div>
      )}

      <p className="whitespace-pre-wrap text-[var(--text-primary)]">
        {t.description}
      </p>

      {t.acceptance_criteria.length > 0 && (
        <div>
          <h4 className="text-micro uppercase font-semibold tracking-wide text-[var(--text-tertiary)] mb-1">
            Acceptance criteria
          </h4>
          <ul className="space-y-0.5">
            {t.acceptance_criteria.map((c, i) => (
              <li key={i} className="pl-3 text-[var(--text-secondary)]">
                ☐ {c}
              </li>
            ))}
          </ul>
        </div>
      )}

      {t.kind === 'issue' && (
        <div>
          <h4 className="text-micro uppercase font-semibold tracking-wide text-[var(--text-tertiary)] mb-1">
            Children ({childIds.length})
          </h4>
          {childIds.length === 0 ? (
            <p className="pl-3 text-[var(--text-tertiary)] text-xs">
              No child Tasks yet. File a Task with this Issue as the parent to
              populate this rollup.
            </p>
          ) : (
            <ul className="space-y-1">
              {childIds.map((cid, i) => {
                const q = childQueries[i];
                const child = q?.data;
                return (
                  <li key={cid}>
                    <button
                      type="button"
                      className="w-full text-left flex items-center gap-2 px-2 py-1 rounded hover:bg-[var(--bg-sunken)] outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                      onClick={() => onNavigate(cid)}
                    >
                      <span className="font-mono text-xs text-[var(--text-tertiary)]">
                        {cid}
                      </span>
                      {child ? (
                        <>
                          <StatusBadge status={child.status} />
                          <span className="truncate text-[var(--text-primary)]">
                            {child.title}
                          </span>
                          {child.assigned_to && (
                            <span className="text-xs text-[var(--text-tertiary)]">
                              → {child.assigned_to}
                            </span>
                          )}
                        </>
                      ) : q?.isError ? (
                        <span className="text-xs text-[var(--danger)]">
                          (failed to load)
                        </span>
                      ) : (
                        <span className="text-xs text-[var(--text-tertiary)]">
                          loading…
                        </span>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}

      {t.depends_on.length > 0 && (
        <div>
          <h4 className="text-micro uppercase font-semibold tracking-wide text-[var(--text-tertiary)] mb-1">
            Depends on
          </h4>
          <ul className="space-y-0.5">
            {t.depends_on.map((d) => (
              <li key={d} className="font-mono text-xs text-[var(--text-tertiary)]">
                {d}
              </li>
            ))}
          </ul>
        </div>
      )}

      {t.completion_notes && (
        <div>
          <h4 className="text-micro uppercase font-semibold tracking-wide text-[var(--text-tertiary)] mb-1">
            Completion notes
          </h4>
          <p className="whitespace-pre-wrap text-[var(--text-secondary)]">
            {t.completion_notes}
          </p>
        </div>
      )}

      {comments && comments.length > 0 && (
        <div>
          <h4 className="text-micro uppercase font-semibold tracking-wide text-[var(--text-tertiary)] mb-1">
            Comments ({comments.length})
          </h4>
          <ul className="space-y-2">
            {comments.map((c) => (
              <li key={c.id} className="border-l-2 border-[var(--border)] pl-3">
                <div className="text-xs text-[var(--text-tertiary)]">
                  <span className="font-medium">
                    {c.author_persona ?? c.author_user_id.slice(0, 8)}
                  </span>
                  {' · '}
                  <RelativeDate iso={c.created_at} />
                </div>
                <p className="whitespace-pre-wrap text-sm text-[var(--text-primary)]">
                  {c.content}
                </p>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ── Comment input ── */}
      <div className="flex items-end gap-2 pt-2">
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Add comment…"
          rows={2}
          className="min-h-0 flex-1"
        />
        <Button
          variant="secondary"
          size="sm"
          disabled={!draft.trim() || submitting !== null}
          loading={submitting === 'comment'}
          onClick={() => callAction('comment')}
        >
          Comment
        </Button>
      </div>

      {/* ── Action buttons ── */}
      {(canApprove || canDismiss || canClose) && (
        <div className="flex gap-2 pt-1">
          {canApprove && (
            <Button
              variant="primary"
              size="sm"
              disabled={submitting !== null}
              loading={submitting === 'approve'}
              onClick={() => callAction('approve')}
            >
              Approve (open)
            </Button>
          )}
          {canClose && (
            <Button
              variant="primary"
              size="sm"
              disabled={submitting !== null}
              loading={submitting === 'close'}
              onClick={() => callAction('close')}
            >
              Close Issue
            </Button>
          )}
          {canDismiss && (
            <Button
              variant="danger"
              size="sm"
              disabled={submitting !== null}
              loading={submitting === 'dismiss'}
              onClick={() => callAction('dismiss')}
            >
              Dismiss
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

/* ── New ticket modal ── */

interface NewModalProps {
  projectId: string;
  onClose: () => void;
}

function NewTicketModal({ projectId, onClose }: NewModalProps) {
  const create = useCreateTicket(projectId);
  const { addToast } = useToast();

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [criteria, setCriteria] = useState('');
  const [priority, setPriority] = useState<
    'low' | 'medium' | 'high' | 'critical'
  >('medium');
  const [assignedTo, setAssignedTo] = useState('');
  const [kind, setKind] = useState<'task' | 'issue'>('task');
  const [parentTicketId, setParentTicketId] = useState<string>('');
  const [submitting, setSubmitting] = useState(false);

  // Open + in_progress Issues are valid parents.
  const { data: openIssues } = useTickets(projectId, {
    kind: 'issue',
    status: 'open',
  });
  const { data: activeIssues } = useTickets(projectId, {
    kind: 'issue',
    status: 'in_progress',
  });
  const issueOptions = [...(openIssues ?? []), ...(activeIssues ?? [])];

  useEffect(() => {
    if (kind === 'issue' && parentTicketId) setParentTicketId('');
  }, [kind, parentTicketId]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      const acceptanceCriteria = criteria
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean);
      await create.mutateAsync({
        title: title.trim(),
        description: description.trim(),
        priority,
        assigned_to: assignedTo.trim() || null,
        acceptance_criteria: acceptanceCriteria,
        kind,
        parent_ticket_id:
          kind === 'task' && parentTicketId ? parentTicketId : null,
      });
      addToast('success', `Created ${kind} "${title.trim()}"`);
      onClose();
    } catch (exc) {
      const msg =
        exc instanceof ApiError
          ? `${exc.status}: ${exc.message}`
          : String(exc);
      addToast('error', msg);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      titleId="ticket-new-title"
      className="max-w-2xl"
    >
      <DialogHeader titleId="ticket-new-title">New ticket</DialogHeader>
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Kind + Parent */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <Select
              id="field-kind"
              value={kind}
              onChange={(e) =>
                setKind(e.target.value as 'task' | 'issue')
              }
              aria-label="Ticket kind"
              options={[
                { value: 'task', label: 'Task' },
                { value: 'issue', label: 'Issue' },
              ]}
              title="Kind"
            />
            <p className="text-[12px] text-[var(--text-tertiary)] mt-1">
              {kind === 'issue'
                ? 'PM-triaged container. Requires project owner or org admin.'
                : 'A single executor unit of work.'}
            </p>
          </div>

          {kind === 'task' && (
            <div>
              <label className="text-[13px] font-medium text-[var(--text-secondary)] block mb-1">
                Parent Issue (optional)
              </label>
              <select
                value={parentTicketId}
                onChange={(e) => setParentTicketId(e.target.value)}
                className="w-full bg-[var(--bg-sunken)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] font-mono appearance-none focus:border-[var(--brand)] focus:shadow-[0_0_0_3px_var(--brand-glow)] outline-none"
                aria-label="Parent Issue"
              >
                <option value="">— none —</option>
                {issueOptions.map((iss) => (
                  <option key={iss.id} value={iss.id}>
                    {iss.id} · {iss.title.slice(0, 50)}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>

        <Input
          id="field-title"
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          required
          title="Title"
          placeholder="Implement session export"
        />

        <Textarea
          id="field-description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          required
          rows={8}
          title="Description (markdown)"
          className="font-mono"
        />

        <Textarea
          id="field-criteria"
          value={criteria}
          onChange={(e) => setCriteria(e.target.value)}
          rows={4}
          title="Acceptance criteria (one per line)"
          placeholder="Migration adds … table\nTier gate enforces …\nTests cover …"
        />

        <div className="grid grid-cols-2 gap-3">
          <Select
            id="field-priority"
            value={priority}
            onChange={(e) =>
              setPriority(e.target.value as typeof priority)
            }
            title="Priority"
            options={[
              { value: 'low', label: 'low' },
              { value: 'medium', label: 'medium' },
              { value: 'high', label: 'high' },
              { value: 'critical', label: 'critical' },
            ]}
          />

          <Input
            id="field-assign-to"
            type="text"
            value={assignedTo}
            onChange={(e) => setAssignedTo(e.target.value)}
            title="Assign to persona"
            placeholder="atlas, sentinel, …"
            className="font-mono"
          />
        </div>

        <DialogFooter>
          <Button variant="secondary" type="button" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" loading={submitting}>
            Create
          </Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}
