/**
 * v0.10.3 — Tickets tab (v0.10.1 backend, v0.10.3 dashboard surface).
 *
 * Read-mostly moderation surface for project tickets. Lists tickets
 * filtered by status, expands one at a time into a detail panel showing
 * description / acceptance criteria / comments / dependencies, and
 * exposes the human-driven FSM transitions: approve (suggested→open),
 * dismiss (suggested/open→cancelled), and comment.
 *
 * Lifecycle transitions tied to local provenance (start, complete,
 * resolve, block, escalate) live in the CLI / MCP — they require the
 * active-ticket bundle on the agent's machine.
 */

import { useEffect, useRef, useState } from 'react';
import { useFocusTrap } from '../hooks/useFocusTrap';
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

const STATUS_TONE: Record<string, string> = {
  suggested: 'bg-amber-500/15 text-amber-600',
  open: 'bg-blue-500/15 text-blue-600',
  in_progress: 'bg-brand/15 text-brand',
  blocked: 'bg-orange-500/15 text-orange-600',
  review: 'bg-purple-500/15 text-purple-600',
  done: 'bg-emerald-500/15 text-emerald-600',
  closed: 'bg-emerald-500/15 text-emerald-600',
  cancelled: 'bg-muted/15 text-muted',
};

const KIND_TONE: Record<string, string> = {
  issue: 'bg-indigo-500/15 text-indigo-600',
  task: 'bg-muted/15 text-muted',
};

const PRIORITY_TONE: Record<string, string> = {
  low: 'text-muted',
  medium: '',
  high: 'text-amber-600',
  critical: 'text-danger',
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold ${
        STATUS_TONE[status] ?? 'bg-muted/15 text-muted'
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

  return (
    <section aria-labelledby="tickets-heading" className="space-y-4">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <h2 id="tickets-heading" className="text-lg font-semibold">
          Tickets
          <span className="ml-2 text-sm text-muted">
            {data.length} {data.length === 1 ? 'ticket' : 'tickets'}
          </span>
        </h2>
        <div className="flex items-center gap-2">
          <label className="text-sm">
            <span className="sr-only">Filter by kind</span>
            <select
              aria-label="Filter by kind"
              value={kindFilter}
              onChange={(e) => setKindFilter(e.target.value)}
              className="px-2 py-1 border border-border rounded text-sm bg-surface"
            >
              {KIND_FILTERS.map((k) => (
                <option key={k.value} value={k.value}>
                  {k.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="sr-only">Filter by status</span>
            <select
              aria-label="Filter by status"
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="px-2 py-1 border border-border rounded text-sm bg-surface"
            >
              {STATUS_FILTERS.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="px-3 py-1.5 text-sm rounded bg-brand text-white hover:brightness-110"
            onClick={() => setCreating(true)}
          >
            New ticket
          </button>
        </div>
      </div>

      {data.length === 0 ? (
        <EmptyState kindFilter={kindFilter} statusFilter={statusFilter} />
      ) : (
        <ul className="border border-border rounded divide-y divide-border">
          {data.map((t) => (
            <li key={t.id}>
              <button
                type="button"
                className={`w-full text-left px-3 py-2 hover:bg-surface flex items-start gap-3 border-l-2 ${
                  t.kind === 'issue' ? 'border-indigo-500/60' : 'border-transparent'
                }`}
                onClick={() => setSelected(selected === t.id ? null : t.id)}
                aria-expanded={selected === t.id}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 text-sm flex-wrap">
                    <span className="font-mono text-xs text-muted">{t.id}</span>
                    <KindBadge kind={t.kind} />
                    <StatusBadge status={t.status} />
                    <span className={`text-xs uppercase tracking-wide ${PRIORITY_TONE[t.priority] ?? ''}`}>
                      {t.priority}
                    </span>
                    {t.assigned_to && (
                      <span className="text-xs text-muted">→ {t.assigned_to}</span>
                    )}
                  </div>
                  <div className="font-medium truncate">{t.title}</div>
                  <div className="text-xs text-muted">
                    <RelativeDate iso={t.updated_at} /> · {t.acceptance_criteria.length} criteria
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
                  onNavigate={navigateTo}
                />
              )}
            </li>
          ))}
        </ul>
      )}

      {creating && (
        <NewTicketModal
          projectId={projectId}
          onClose={() => setCreating(false)}
        />
      )}
    </section>
  );
}

function EmptyState({
  kindFilter,
  statusFilter,
}: {
  kindFilter: string;
  statusFilter: string;
}) {
  if (kindFilter === 'issue') {
    return (
      <div className="border border-border rounded p-6 text-center text-muted space-y-1">
        <p className="font-medium text-base">No Issues yet.</p>
        <p className="text-sm">
          An Issue is a PM-triaged container that rolls up one or more Tasks.
          File an Issue when a user-facing problem needs cross-team work;
          file a Task for a single executor's unit of work.
        </p>
      </div>
    );
  }
  return (
    <div className="border border-border rounded p-6 text-center text-muted">
      No tickets{' '}
      {statusFilter ? `with status "${statusFilter}"` : kindFilter ? `of kind "${kindFilter}"` : 'yet'}.
    </div>
  );
}

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
      const msg = exc instanceof ApiError ? `${exc.status}: ${exc.message}` : String(exc);
      addToast('error', msg);
    } finally {
      setSubmitting(null);
    }
  }

  const canApprove = t.status === 'suggested';
  const canDismiss = t.status === 'suggested' || t.status === 'open';
  // Close is the Issue terminator. Server enforces project owner / org admin
  // and returns 403 if the actor lacks standing — we surface that via the
  // toast on the catch above rather than gating client-side.
  const canClose = t.kind === 'issue' && t.status === 'in_progress';

  return (
    <div className="px-3 py-3 bg-surface/50 text-sm space-y-3">
      {t.kind === 'task' && t.parent_ticket_id && (
        <div className="text-xs">
          <button
            type="button"
            className="text-brand hover:underline focus:outline-none focus:ring-2 focus:ring-brand/40 rounded"
            onClick={() => onNavigate(t.parent_ticket_id!)}
          >
            ← Back to parent Issue:{' '}
            <span className="font-mono">{t.parent_ticket_id}</span>
            {parent ? (
              <>
                {' '}
                — {parent.title}{' '}
                <span className="text-muted">({parent.status})</span>
              </>
            ) : null}
          </button>
        </div>
      )}

      <p className="whitespace-pre-wrap">{t.description}</p>

      {t.acceptance_criteria.length > 0 && (
        <div>
          <h4 className="text-xs uppercase tracking-wide text-muted mb-1">
            Acceptance criteria
          </h4>
          <ul className="space-y-0.5">
            {t.acceptance_criteria.map((c, i) => (
              <li key={i} className="pl-3 text-muted">
                ☐ {c}
              </li>
            ))}
          </ul>
        </div>
      )}

      {t.kind === 'issue' && (
        <div>
          <h4 className="text-xs uppercase tracking-wide text-muted mb-1">
            Children ({childIds.length})
          </h4>
          {childIds.length === 0 ? (
            <p className="pl-3 text-muted text-xs">
              No child Tasks yet. File a Task with this Issue as the parent
              to populate this rollup.
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
                      className="w-full text-left flex items-center gap-2 px-2 py-1 rounded hover:bg-bg focus:outline-none focus:ring-2 focus:ring-brand/40"
                      onClick={() => onNavigate(cid)}
                    >
                      <span className="font-mono text-xs text-muted">{cid}</span>
                      {child ? (
                        <>
                          <StatusBadge status={child.status} />
                          <span className="truncate">{child.title}</span>
                          {child.assigned_to && (
                            <span className="text-xs text-muted">
                              → {child.assigned_to}
                            </span>
                          )}
                        </>
                      ) : q?.isError ? (
                        <span className="text-xs text-danger">
                          (failed to load)
                        </span>
                      ) : (
                        <span className="text-xs text-muted">loading…</span>
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
          <h4 className="text-xs uppercase tracking-wide text-muted mb-1">Depends on</h4>
          <ul className="space-y-0.5">
            {t.depends_on.map((d) => (
              <li key={d} className="font-mono text-xs text-muted">
                {d}
              </li>
            ))}
          </ul>
        </div>
      )}

      {t.completion_notes && (
        <div>
          <h4 className="text-xs uppercase tracking-wide text-muted mb-1">Completion notes</h4>
          <p className="whitespace-pre-wrap text-muted">{t.completion_notes}</p>
        </div>
      )}

      {comments && comments.length > 0 && (
        <div>
          <h4 className="text-xs uppercase tracking-wide text-muted mb-1">
            Comments ({comments.length})
          </h4>
          <ul className="space-y-2">
            {comments.map((c) => (
              <li key={c.id} className="border-l-2 border-border pl-3">
                <div className="text-xs text-muted">
                  <span className="font-medium">{c.author_persona ?? c.author_user_id.slice(0, 8)}</span>
                  {' · '}
                  <RelativeDate iso={c.created_at} />
                </div>
                <p className="whitespace-pre-wrap text-sm">{c.content}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex items-end gap-2 pt-2">
        <label className="flex-1 block">
          <span className="sr-only">Comment</span>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Add comment…"
            rows={2}
            className="w-full px-2 py-1 border border-border rounded text-sm bg-bg"
          />
        </label>
        <button
          type="button"
          disabled={!draft.trim() || submitting !== null}
          className="px-3 py-1.5 text-sm rounded border border-border hover:bg-surface disabled:opacity-50"
          onClick={() => callAction('comment')}
        >
          {submitting === 'comment' ? 'Posting…' : 'Comment'}
        </button>
      </div>

      <div className="flex gap-2 pt-1">
        {canApprove && (
          <button
            type="button"
            disabled={submitting !== null}
            className="px-3 py-1.5 text-sm rounded bg-brand text-white hover:brightness-110 disabled:opacity-50"
            onClick={() => callAction('approve')}
          >
            {submitting === 'approve' ? 'Approving…' : 'Approve (open)'}
          </button>
        )}
        {canClose && (
          <button
            type="button"
            disabled={submitting !== null}
            className="px-3 py-1.5 text-sm rounded bg-emerald-600 text-white hover:brightness-110 disabled:opacity-50"
            onClick={() => callAction('close')}
          >
            {submitting === 'close' ? 'Closing…' : 'Close Issue'}
          </button>
        )}
        {canDismiss && (
          <button
            type="button"
            disabled={submitting !== null}
            className="px-3 py-1.5 text-sm rounded border border-border hover:bg-danger hover:text-white disabled:opacity-50"
            onClick={() => callAction('dismiss')}
          >
            {submitting === 'dismiss' ? 'Dismissing…' : 'Dismiss'}
          </button>
        )}
      </div>
    </div>
  );
}

interface NewModalProps {
  projectId: string;
  onClose: () => void;
}

function NewTicketModal({ projectId, onClose }: NewModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef);
  const create = useCreateTicket(projectId);
  const { addToast } = useToast();

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [criteria, setCriteria] = useState('');
  const [priority, setPriority] = useState<'low' | 'medium' | 'high' | 'critical'>('medium');
  const [assignedTo, setAssignedTo] = useState('');
  const [kind, setKind] = useState<'task' | 'issue'>('task');
  const [parentTicketId, setParentTicketId] = useState<string>('');
  const [submitting, setSubmitting] = useState(false);

  // Open + in_progress Issues are valid parents. Server enforces same-project
  // + kind=='issue' on the link; we just narrow the dropdown to useful options.
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
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

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
      const msg = exc instanceof ApiError ? `${exc.status}: ${exc.message}` : String(exc);
      addToast('error', msg);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="ticket-new-title"
        className="bg-bg border border-border rounded p-5 w-full max-w-2xl max-h-[85vh] overflow-y-auto"
      >
        <h3 id="ticket-new-title" className="text-base font-semibold mb-3">
          New ticket
        </h3>
        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-sm text-muted">Kind</span>
              <select
                value={kind}
                onChange={(e) => setKind(e.target.value as 'task' | 'issue')}
                className="w-full mt-1 px-2 py-1 border border-border rounded text-sm bg-surface"
                aria-label="Ticket kind"
              >
                <option value="task">Task</option>
                <option value="issue">Issue</option>
              </select>
              <span className="block text-xs text-muted mt-1">
                {kind === 'issue'
                  ? 'PM-triaged container. Requires project owner or org admin.'
                  : 'A single executor unit of work.'}
              </span>
            </label>

            {kind === 'task' && (
              <label className="block">
                <span className="text-sm text-muted">Parent Issue (optional)</span>
                <select
                  value={parentTicketId}
                  onChange={(e) => setParentTicketId(e.target.value)}
                  className="w-full mt-1 px-2 py-1 border border-border rounded text-sm bg-surface font-mono"
                  aria-label="Parent Issue"
                >
                  <option value="">— none —</option>
                  {issueOptions.map((iss) => (
                    <option key={iss.id} value={iss.id}>
                      {iss.id} · {iss.title.slice(0, 50)}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>

          <label className="block">
            <span className="text-sm text-muted">Title</span>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              required
              className="w-full mt-1 px-2 py-1 border border-border rounded text-sm bg-surface"
            />
          </label>

          <label className="block">
            <span className="text-sm text-muted">Description (markdown)</span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              required
              rows={8}
              className="w-full mt-1 px-2 py-1 border border-border rounded text-sm bg-surface font-mono"
            />
          </label>

          <label className="block">
            <span className="text-sm text-muted">
              Acceptance criteria (one per line)
            </span>
            <textarea
              value={criteria}
              onChange={(e) => setCriteria(e.target.value)}
              rows={4}
              className="w-full mt-1 px-2 py-1 border border-border rounded text-sm bg-surface"
              placeholder={'Migration adds … table\nTier gate enforces …\nTests cover …'}
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-sm text-muted">Priority</span>
              <select
                value={priority}
                onChange={(e) => setPriority(e.target.value as typeof priority)}
                className="w-full mt-1 px-2 py-1 border border-border rounded text-sm bg-surface"
              >
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
                <option value="critical">critical</option>
              </select>
            </label>

            <label className="block">
              <span className="text-sm text-muted">Assign to persona</span>
              <input
                type="text"
                value={assignedTo}
                onChange={(e) => setAssignedTo(e.target.value)}
                placeholder="atlas, sentinel, …"
                className="w-full mt-1 px-2 py-1 border border-border rounded text-sm bg-surface font-mono"
              />
            </label>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              className="px-3 py-1.5 text-sm rounded border border-border hover:bg-surface"
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-3 py-1.5 text-sm rounded bg-brand text-white hover:brightness-110 disabled:opacity-50"
            >
              {submitting ? 'Creating…' : 'Create'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
