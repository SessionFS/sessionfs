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
  useCreateTicket,
  useDismissTicket,
  useTicket,
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
  { value: 'cancelled', label: 'Cancelled' },
];

const STATUS_TONE: Record<string, string> = {
  suggested: 'bg-amber-500/15 text-amber-600',
  open: 'bg-blue-500/15 text-blue-600',
  in_progress: 'bg-brand/15 text-brand',
  blocked: 'bg-orange-500/15 text-orange-600',
  review: 'bg-purple-500/15 text-purple-600',
  done: 'bg-emerald-500/15 text-emerald-600',
  cancelled: 'bg-muted/15 text-muted',
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

export default function TicketsTab({ projectId }: TicketsTabProps) {
  const [statusFilter, setStatusFilter] = useState<string>('');
  const { data, isLoading, error } = useTickets(projectId, {
    status: statusFilter || undefined,
  });
  const [creating, setCreating] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

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
        <div className="border border-border rounded p-6 text-center text-muted">
          No tickets {statusFilter ? `with status "${statusFilter}"` : 'yet'}.
        </div>
      ) : (
        <ul className="border border-border rounded divide-y divide-border">
          {data.map((t) => (
            <li key={t.id}>
              <button
                type="button"
                className="w-full text-left px-3 py-2 hover:bg-surface flex items-start gap-3"
                onClick={() => setSelected(selected === t.id ? null : t.id)}
                aria-expanded={selected === t.id}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 text-sm">
                    <span className="font-mono text-xs text-muted">{t.id}</span>
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
                  </div>
                </div>
              </button>
              {selected === t.id && (
                <TicketDetail
                  projectId={projectId}
                  ticketId={t.id}
                  fallback={t}
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

interface DetailProps {
  projectId: string;
  ticketId: string;
  fallback: Ticket;
}

function TicketDetail({ projectId, ticketId, fallback }: DetailProps) {
  const { data: detail } = useTicket(projectId, ticketId);
  const t = detail ?? fallback;
  const { data: comments } = useTicketComments(projectId, ticketId);
  const approve = useApproveTicket(projectId);
  const dismiss = useDismissTicket(projectId);
  const addComment = useAddTicketComment(projectId);
  const { addToast } = useToast();
  const [draft, setDraft] = useState('');
  const [submitting, setSubmitting] = useState<'approve' | 'dismiss' | 'comment' | null>(null);

  async function callAction(action: 'approve' | 'dismiss' | 'comment') {
    setSubmitting(action);
    try {
      if (action === 'approve') {
        await approve.mutateAsync(ticketId);
        addToast('success', 'Approved');
      } else if (action === 'dismiss') {
        await dismiss.mutateAsync(ticketId);
        addToast('success', 'Dismissed');
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

  return (
    <div className="px-3 py-3 bg-surface/50 text-sm space-y-3">
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
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

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
      });
      addToast('success', `Created ticket "${title.trim()}"`);
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
