import { useState } from 'react';
import { useToast } from '../../hooks/useToast';
import {
  useAddTicketComment,
  useApproveTicket,
  useCloseTicket,
  useDismissTicket,
  useTicket,
  useTicketChildren,
  useTicketComments,
} from '../../hooks/useTickets';
import RelativeDate from '../../components/RelativeDate';
import { type Ticket } from '../../api/client';
import { Button, Textarea } from '../../components/ui';
import { Badge } from '../../components/Badge';
import { STATUS_VARIANT, formatStatusLabel } from './constants';
import { JiraTicketSection } from './JiraPromote';
import { errorMessage } from '../../utils/errorMessage';

/* ── Detail panel (unchanged logic, restyled onto primitives) ── */

interface DetailProps {
  projectId: string;
  ticketId: string;
  fallback: Ticket;
  onNavigate: (targetId: string) => void;
  // Jira surface, resolved by the page owner (ProjectDetail) and threaded
  // down so this panel stays testable without an AuthProvider.
  orgId?: string | null;
  isProjectAdmin?: boolean;
  isEnterprise?: boolean;
}

type Action = 'approve' | 'dismiss' | 'close' | 'comment';

export function TicketDetail({
  projectId,
  ticketId,
  fallback,
  onNavigate,
  orgId,
  isProjectAdmin,
  isEnterprise,
}: DetailProps) {
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
      addToast('error', `Update failed: ${errorMessage(exc)}`);
    } finally {
      setSubmitting(null);
    }
  }

  const canApprove = t.status === 'suggested';
  const canDismiss = t.status === 'suggested' || t.status === 'open';
  const canClose = t.kind === 'issue' && t.status === 'in_progress';

  return (
    <div className="px-3 py-3 bg-surface/50 text-sm space-y-3 border-t border-border">
      {t.kind === 'task' && t.parent_ticket_id && (
        <div className="text-xs">
          <Button
            variant="ghost"
            size="sm"
            className="!px-0 !py-0 !text-xs !font-normal !text-brand hover:!bg-transparent hover:underline"
            onClick={() => onNavigate(t.parent_ticket_id!)}
          >
            ← Back to parent Issue:{' '}
            <span className="font-mono">{t.parent_ticket_id}</span>
            {parent ? (
              <>
                {' '}
                — {parent.title}{' '}
                <span className="text-text-tertiary">
                  ({parent.status})
                </span>
              </>
            ) : null}
          </Button>
        </div>
      )}

      <p className="whitespace-pre-wrap text-text-primary">
        {t.description}
      </p>

      {orgId && (
        <JiraTicketSection
          projectId={projectId}
          ticket={t}
          orgId={orgId}
          isProjectAdmin={isProjectAdmin ?? false}
          isEnterprise={isEnterprise ?? false}
        />
      )}

      {t.acceptance_criteria.length > 0 && (
        <div>
          <h4 className="text-micro uppercase font-semibold tracking-wide text-text-tertiary mb-1">
            Acceptance criteria
          </h4>
          <ul className="space-y-0.5">
            {t.acceptance_criteria.map((c, i) => (
              <li key={i} className="pl-3 text-text-secondary">
                ☐ {c}
              </li>
            ))}
          </ul>
        </div>
      )}

      {t.kind === 'issue' && (
        <div>
          <h4 className="text-micro uppercase font-semibold tracking-wide text-text-tertiary mb-1">
            Children ({childIds.length})
          </h4>
          {childIds.length === 0 ? (
            <p className="pl-3 text-text-tertiary text-xs">
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
                      className="w-full text-left flex items-center gap-2 px-2 py-1 rounded hover:bg-bg-sunken outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                      onClick={() => onNavigate(cid)}
                    >
                      <span className="font-mono text-xs text-text-tertiary">
                        {cid}
                      </span>
                      {child ? (
                        <>
                          <Badge
                            variant={STATUS_VARIANT[child.status] || 'default'}
                            tint
                            label={formatStatusLabel(child.status)}
                            size="sm"
                          />
                          <span className="truncate text-text-primary">
                            {child.title}
                          </span>
                          {child.assigned_to && (
                            <span className="text-xs text-text-tertiary">
                              → {child.assigned_to}
                            </span>
                          )}
                        </>
                      ) : q?.isError ? (
                        <span className="text-xs text-danger">
                          (failed to load)
                        </span>
                      ) : (
                        <span className="text-xs text-text-tertiary">
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
          <h4 className="text-micro uppercase font-semibold tracking-wide text-text-tertiary mb-1">
            Depends on
          </h4>
          <ul className="space-y-0.5">
            {t.depends_on.map((d) => (
              <li key={d} className="font-mono text-xs text-text-tertiary">
                {d}
              </li>
            ))}
          </ul>
        </div>
      )}

      {t.completion_notes && (
        <div>
          <h4 className="text-micro uppercase font-semibold tracking-wide text-text-tertiary mb-1">
            Completion notes
          </h4>
          <p className="whitespace-pre-wrap text-text-secondary">
            {t.completion_notes}
          </p>
        </div>
      )}

      {comments && comments.length > 0 && (
        <div>
          <h4 className="text-micro uppercase font-semibold tracking-wide text-text-tertiary mb-1">
            Comments ({comments.length})
          </h4>
          <ul className="space-y-2">
            {comments.map((c) => (
              <li key={c.id} className="border-l-2 border-border pl-3">
                <div className="text-xs text-text-tertiary">
                  <span className="font-medium">
                    {c.author_persona ?? c.author_user_id.slice(0, 8)}
                  </span>
                  {' · '}
                  <RelativeDate iso={c.created_at} />
                </div>
                <p className="whitespace-pre-wrap text-sm text-text-primary">
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
              Close issue
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
