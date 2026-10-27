import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useToast } from '../../hooks/useToast';
import {
  useAddTicketComment,
  useApproveTicket,
  useCloseTicket,
  useDismissTicket,
  useTicket,
  useTicketChildren,
  useTicketComments,
  useTicketJiraSyncEvents,
  useTicketReviewState,
  useTicketWatchers,
  useUnwatchTicket,
  useWatchTicket,
} from '../../hooks/useTickets';
import { useMe } from '../../hooks/useMe';
import { useJiraLink } from '../../hooks/useJiraTickets';
import RelativeDate from '../../components/RelativeDate';
import type { JiraSyncEvent, Ticket, TicketComment, TicketReviewRound } from '../../api/client';
import { Button, Checkbox, Textarea } from '../../components/ui';
import { Badge } from '../../components/Badge';
import { PRIORITY_TONE, STATUS_DOT, STATUS_VARIANT, formatStatusLabel } from './constants';
import { KindBadge } from './KindBadge';
import { JiraTicketSection } from './JiraPromote';
import { AssigneeLabel } from './AssigneeLabel';
import { ReassignPanel } from './ReassignPanel';
import { ticketActionAllowed, useTicketViewer } from './viewer';
import { errorMessage } from '../../utils/errorMessage';
import { jiraErrorCode, jiraSyncEventLabel, jiraSyncStateLabel } from '../../utils/jira';

/* ── Ticket detail ──
 *
 * Header (status, assignee, Jira and the actions), the ticket's own content,
 * secondary sections that fold away, then one chronological activity
 * timeline — comments, trusted review verdicts, Jira sync events and
 * lifecycle events — with the comment composer at its end. System events are
 * quiet one-liners so people's comments stand out. */

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
  /** Project editor (`can_edit`); older servers omit it, so default true. */
  canEdit?: boolean;
  /** Rendered inside a drawer that already frames it: drop the panel chrome. */
  bare?: boolean;
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
  canEdit = true,
  bare = false,
}: DetailProps) {
  const { data: detail } = useTicket(projectId, ticketId);
  const t = detail ?? fallback;
  const { data: comments } = useTicketComments(projectId, ticketId);
  const { data: reviewState } = useTicketReviewState(projectId, ticketId);
  // Jira sync surfaces only exist for a linked ticket. Gating the queries on
  // the link keeps unlinked tickets (and non-Enterprise projects, which can
  // never be linked) from calling endpoints that would 403/409.
  const linked = !!t.jira;
  const { data: syncEvents } = useTicketJiraSyncEvents(projectId, ticketId, linked);
  const jiraLink = useJiraLink(projectId, ticketId, linked);
  // Server-computed: true only for project editors when the org's comment
  // sync is outbound/both. Never offer (or send) the flag otherwise.
  const canSendToJira = linked && jiraLink.data?.can_send_to_jira === true;
  const approve = useApproveTicket(projectId);
  const dismiss = useDismissTicket(projectId);
  const close = useCloseTicket(projectId);
  const addComment = useAddTicketComment(projectId);
  const viewer = useTicketViewer();
  const { addToast } = useToast();
  const [draft, setDraft] = useState('');
  const [sendToJira, setSendToJira] = useState(false);
  const [submitting, setSubmitting] = useState<Action | null>(null);
  const [reassigning, setReassigning] = useState(false);
  // Closing the reassign editor hands focus back to the button that opened it.
  const reassignButtonRef = useRef<HTMLButtonElement>(null);
  const wasReassigning = useRef(false);
  useEffect(() => {
    if (wasReassigning.current && !reassigning) reassignButtonRef.current?.focus();
    wasReassigning.current = reassigning;
  }, [reassigning]);

  // Ticket watching (follow / unfollow / mute). The current user's row is
  // derived from the watchers list + the me profile, so no extra "am I
  // following" endpoint is needed.
  const me = useMe();
  const { data: watchers } = useTicketWatchers(projectId, ticketId);
  const watch = useWatchTicket(projectId);
  const unwatch = useUnwatchTicket(projectId);
  const myWatch = watchers?.find((w) => w.user_id === me.data?.user_id);
  const watching = !!myWatch;
  const muted = myWatch?.muted ?? false;

  async function handleFollow(mute: boolean) {
    try {
      await watch.mutateAsync({ ticketId, muted: mute });
      addToast('success', mute ? 'Following (muted)' : 'Following');
    } catch (exc) {
      addToast('error', `Follow failed: ${errorMessage(exc)}`);
    }
  }

  async function handleUnfollow() {
    try {
      await unwatch.mutateAsync(ticketId);
      addToast('success', 'Unfollowed');
    } catch (exc) {
      addToast('error', `Unfollow failed: ${errorMessage(exc)}`);
    }
  }

  async function handleToggleMute() {
    try {
      await watch.mutateAsync({ ticketId, muted: !muted });
      addToast('success', muted ? 'Unmuted' : 'Muted');
    } catch (exc) {
      addToast('error', `Mute failed: ${errorMessage(exc)}`);
    }
  }

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
        await addComment.mutateAsync({
          ticketId,
          content: draft.trim(),
          // Sent only when the server says this caller may push comments to
          // Jira (linked ticket, editor, outbound comment sync on).
          sendToJira: canSendToJira && sendToJira ? true : undefined,
        });
        setDraft('');
        setSendToJira(false);
      }
    } catch (exc) {
      if (action === 'comment' && jiraErrorCode(exc) === 'jira_comment_sync_off') {
        // Comment sync was switched off (or the link removed) since the
        // link was loaded: nothing was posted. Refresh so the checkbox goes
        // away, and keep the draft so it can be posted here only.
        setSendToJira(false);
        void jiraLink.refetch?.();
        addToast(
          'error',
          'Comment not posted: sending comments to Jira is off for this ticket. Post it again to keep it in SessionFS only.',
        );
        return;
      }
      addToast('error', `Update failed: ${errorMessage(exc)}`);
    } finally {
      setSubmitting(null);
    }
  }

  const canApprove = t.status === 'suggested' && ticketActionAllowed('approve', t, viewer);
  const canDismiss =
    (t.status === 'suggested' || t.status === 'open') && ticketActionAllowed('dismiss', t, viewer);
  const canClose =
    t.kind === 'issue' && t.status === 'in_progress' && ticketActionAllowed('close', t, viewer);
  // PUT /tickets/{id} admits the ticket's creator or a project admin. The
  // admin flag only arrives with the Jira surface; without it, editors see
  // the control and the server has the final say.
  const isCreator = !!viewer.userId && t.created_by_user_id === viewer.userId;
  const canReassign = isCreator || (viewer.canEdit && (isProjectAdmin ?? true));

  const timeline = useMemo(
    () => buildTimeline(t, comments ?? [], syncEvents?.events ?? [], reviewState?.review_state?.rounds ?? []),
    [t, comments, syncEvents, reviewState],
  );

  const actions = (canApprove || canDismiss || canClose) && (
    <div className="flex flex-wrap items-center gap-2">
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
          variant="ghost"
          size="sm"
          className="!text-danger"
          disabled={submitting !== null}
          loading={submitting === 'dismiss'}
          onClick={() => callAction('dismiss')}
        >
          Dismiss
        </Button>
      )}
    </div>
  );

  const followControls = (
    <div className="flex flex-wrap items-center gap-1.5">
      {watching ? (
        <>
          <Badge variant="success" tint label={muted ? 'Following (muted)' : 'Following'} size="sm" />
          <Button variant="ghost" size="sm" onClick={handleToggleMute} aria-pressed={muted}>
            {muted ? 'Unmute' : 'Mute'}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={unwatch.isPending}
            loading={unwatch.isPending}
            onClick={handleUnfollow}
          >
            Unfollow
          </Button>
        </>
      ) : (
        <Button variant="secondary" size="sm" loading={watch.isPending} onClick={() => handleFollow(false)}>
          Follow
        </Button>
      )}
    </div>
  );

  const children = t.kind === 'issue' && (
    <Disclosure title={`Children (${childIds.length})`} defaultOpen>
      {childIds.length === 0 ? (
        <p className="text-xs text-text-tertiary">
          No child Tasks yet. File a Task with this Issue as the parent to populate this rollup.
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {childIds.map((cid, i) => {
            const q = childQueries[i];
            const child = q?.data;
            return (
              <li key={cid}>
                <button
                  type="button"
                  className="w-full text-left flex items-center gap-2 px-1 py-1.5 rounded hover:bg-surface-hover outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                  onClick={() => onNavigate(cid)}
                >
                  <span className="font-mono text-xs text-text-tertiary">{cid}</span>
                  {child ? (
                    <>
                      <Badge
                        variant={STATUS_VARIANT[child.status] || 'default'}
                        tint
                        label={formatStatusLabel(child.status)}
                        size="sm"
                      />
                      <span className="truncate text-text-primary">{child.title}</span>
                      {(child.assigned_to || child.assigned_user_id) && (
                        <span className="inline-flex min-w-0 items-center gap-1 text-xs text-text-tertiary">
                          → <AssigneeLabel ticket={child} />
                        </span>
                      )}
                    </>
                  ) : q?.isError ? (
                    <span className="text-xs text-danger">(failed to load)</span>
                  ) : (
                    <span className="text-xs text-text-tertiary">loading…</span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Disclosure>
  );

  return (
    <div className={`text-sm space-y-5 ${bare ? '' : 'border-t border-border bg-bg-elevated px-4 py-4'}`}>
      {/* ── Header: state, people, Jira, actions ── */}
      <div className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 min-w-0">
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden="true" className={`w-1.5 h-1.5 rounded-full shrink-0 ${STATUS_DOT[t.status] || 'bg-text-tertiary'}`} />
              <span className="text-xs font-medium capitalize text-text-primary">{formatStatusLabel(t.status)}</span>
            </span>
            <KindBadge kind={t.kind} />
            <span className={`text-xs uppercase tracking-wide ${PRIORITY_TONE[t.priority] ?? 'text-text-secondary'}`}>
              {t.priority}
            </span>
            <span className="inline-flex min-w-0 items-center gap-1 text-xs text-text-secondary">
              {t.assigned_to || t.assigned_user_id ? (
                <>
                  Assigned to <AssigneeLabel ticket={t} emphasis />
                </>
              ) : (
                <span className="text-text-tertiary">Unassigned</span>
              )}
              {canReassign && !reassigning && (
                <Button
                  ref={reassignButtonRef}
                  variant="ghost"
                  size="sm"
                  className="!px-1.5 !py-0.5 !text-xs !font-normal !text-brand"
                  onClick={() => setReassigning(true)}
                >
                  {t.assigned_to || t.assigned_user_id ? 'Reassign' : 'Assign'}
                </Button>
              )}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {actions}
            {followControls}
          </div>
        </div>

        {reassigning && (
          <ReassignPanel projectId={projectId} ticket={t} onDone={() => setReassigning(false)} />
        )}

        {orgId && (
          <JiraTicketSection
            projectId={projectId}
            ticket={t}
            orgId={orgId}
            isProjectAdmin={isProjectAdmin ?? false}
            isEnterprise={isEnterprise ?? false}
            canEdit={canEdit}
          />
        )}

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
                  <span className="text-text-tertiary">({parent.status})</span>
                </>
              ) : null}
            </Button>
          </div>
        )}
      </div>

      {/* ── The ticket itself ── */}
      {t.description && (
        <p className="whitespace-pre-wrap leading-6 text-text-primary">{t.description}</p>
      )}

      {t.acceptance_criteria.length > 0 && (
        <div>
          <SectionLabel>Acceptance criteria</SectionLabel>
          <ul className="space-y-1">
            {t.acceptance_criteria.map((c, i) => (
              <li key={i} className="flex items-start gap-2 text-text-secondary">
                <span aria-hidden="true" className="mt-1 inline-block h-3 w-3 shrink-0 rounded-sm border border-border-strong" />
                <span>{c}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ── Secondary sections, folded ── */}
      {(children || t.depends_on.length > 0 || t.completion_notes) && (
        <div className="divide-y divide-border rounded-lg border border-border bg-surface">
          {t.completion_notes && (
            <Disclosure title="Completion notes" defaultOpen>
              <p className="whitespace-pre-wrap text-text-secondary">{t.completion_notes}</p>
            </Disclosure>
          )}
          {children}
          {t.depends_on.length > 0 && (
            <Disclosure title={`Depends on (${t.depends_on.length})`}>
              <ul className="space-y-0.5">
                {t.depends_on.map((d) => (
                  <li key={d} className="font-mono text-xs text-text-secondary">
                    {d}
                  </li>
                ))}
              </ul>
            </Disclosure>
          )}
        </div>
      )}

      {/* ── Activity ── */}
      <section aria-label="Activity">
        <SectionLabel>Activity</SectionLabel>
        {timeline.length > 0 && (
          <ol className="relative ml-1.5 border-l border-border">
            {timeline.map((item) => (
              <TimelineRow key={item.key} item={item} />
            ))}
          </ol>
        )}

        {/* Composer: the end of the timeline. In the drawer it stays in view
            while the thread scrolls inside the panel; inline in the list it
            simply follows the timeline, so it never covers the page. */}
        <div className={bare ? 'sticky bottom-0 z-10 -mx-1 mt-3 bg-overlay px-1 pb-1 pt-2' : 'mt-3'}>
        <div className="rounded-lg border border-border bg-surface p-2 shadow-[var(--shadow-sm)]">
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Add comment…"
            aria-label="Add comment"
            rows={2}
            className="min-h-0 !border-0 !bg-transparent !shadow-none focus-visible:!shadow-none"
          />
          <div className="flex flex-wrap items-center justify-between gap-2 px-1 pt-1">
            {canSendToJira ? (
              <Checkbox
                checked={sendToJira}
                onChange={(e) => setSendToJira(e.target.checked)}
                label="Send to Jira"
                description="Also post this comment to the linked Jira issue."
              />
            ) : (
              <span />
            )}
            <Button
              variant={draft.trim() ? 'primary' : 'secondary'}
              size="sm"
              disabled={!draft.trim() || submitting !== null}
              loading={submitting === 'comment'}
              onClick={() => callAction('comment')}
            >
              Comment
            </Button>
          </div>
        </div>
        </div>
      </section>
    </div>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <h3 className="text-micro uppercase font-semibold tracking-wide text-text-tertiary mb-2">{children}</h3>
  );
}

function Disclosure({ title, defaultOpen = false, children }: { title: string; defaultOpen?: boolean; children: ReactNode }) {
  return (
    <details open={defaultOpen} className="group px-3 py-2">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 rounded-sm py-0.5 text-xs font-semibold text-text-secondary outline-none hover:text-text-primary focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] [&::-webkit-details-marker]:hidden">
        {title}
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="transition-transform group-open:rotate-180">
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </summary>
      <div className="pt-2">{children}</div>
    </details>
  );
}

/* ── Timeline ── */

/** Delivery of a comment's outbound push to Jira, folded onto the comment. */
type JiraDelivery = JiraSyncEvent;

type TimelineItem =
  | { key: string; at: string; kind: 'event'; text: ReactNode }
  | { key: string; at: string; kind: 'comment'; comment: TicketComment; verdict?: TicketReviewRound; delivery?: JiraDelivery }
  | { key: string; at: string; kind: 'sync'; event: JiraSyncEvent };

/** Inbound comments and field edits already arrive as ticket comments (a
 *  mirrored Jira comment or a system note), so a successful ledger row for
 *  them would only repeat it. Failed, refused or skipped ones stay visible. */
function duplicatesAComment(e: JiraSyncEvent): boolean {
  if (e.direction !== 'inbound' || (e.kind !== 'comment' && e.kind !== 'field_edit')) return false;
  return e.applied !== false && !e.error && e.state !== 'skipped' && e.state !== 'exhausted' && e.state !== 'pending';
}

function outboundCommentId(e: JiraSyncEvent): string | null {
  if (e.direction !== 'outbound' || e.kind !== 'comment') return null;
  const id = e.payload?.comment_id;
  return typeof id === 'string' && id ? id : null;
}

function buildTimeline(
  t: Ticket,
  comments: TicketComment[],
  events: JiraSyncEvent[],
  rounds: TicketReviewRound[],
): TimelineItem[] {
  const verdicts = new Map(rounds.map((r) => [r.comment_id, r]));
  const commentIds = new Set(comments.map((c) => c.id));
  // The ledger is newest-first; walk it oldest-first so events that share a
  // timestamp keep their real order, and the latest delivery per comment wins.
  const chronological = [...events].reverse();
  const deliveries = new Map<string, JiraDelivery>();
  for (const e of chronological) {
    const cid = outboundCommentId(e);
    if (cid && commentIds.has(cid)) deliveries.set(cid, e);
  }

  const items: TimelineItem[] = [];
  if (t.created_at) {
    items.push({
      key: 'created',
      at: t.created_at,
      kind: 'event',
      text: t.created_by_persona ? (
        <>
          Created by <span className="font-medium text-text-secondary">{t.created_by_persona}</span>
        </>
      ) : (
        'Created'
      ),
    });
  }
  for (const c of comments) {
    items.push({
      key: `c:${c.id}`,
      at: c.created_at,
      kind: 'comment',
      comment: c,
      verdict: verdicts.get(c.id),
      delivery: deliveries.get(c.id),
    });
  }
  for (const e of chronological) {
    if (duplicatesAComment(e)) continue;
    const cid = outboundCommentId(e);
    if (cid && commentIds.has(cid)) continue; // shown on its comment
    items.push({ key: `s:${e.id}`, at: e.created_at ?? '', kind: 'sync', event: e });
  }
  if (t.resolved_at) {
    items.push({ key: 'resolved', at: t.resolved_at, kind: 'event', text: `Resolved (${formatStatusLabel(t.status)})` });
  }
  // Stable chronological order; undated rows keep their place at the end.
  return items
    .map((item, i) => ({ item, i }))
    .sort((a, b) => {
      const ta = a.item.at ? Date.parse(a.item.at) : Infinity;
      const tb = b.item.at ? Date.parse(b.item.at) : Infinity;
      return ta === tb ? a.i - b.i : ta - tb;
    })
    .map(({ item }) => item);
}

function DeliveryBadge({ delivery }: { delivery: JiraDelivery }) {
  const state = delivery.state ?? (delivery.applied ? 'done' : 'pending');
  const badge =
    state === 'done'
      ? { variant: 'success' as const, label: 'Sent to Jira' }
      : state === 'skipped'
        ? { variant: 'default' as const, label: 'Not sent to Jira' }
        : state === 'exhausted'
          ? { variant: 'danger' as const, label: 'Jira: gave up' }
          : (delivery.retry_count ?? 0) > 0
            ? { variant: 'warning' as const, label: `Jira: retrying (attempt ${(delivery.retry_count ?? 0) + 1})` }
            : { variant: 'info' as const, label: 'Jira: queued' };
  return (
    <span title={delivery.error ?? undefined}>
      <Badge variant={badge.variant} label={badge.label} size="sm" tint />
    </span>
  );
}

function Dot({ tone = 'quiet' }: { tone?: 'quiet' | 'comment' | 'success' | 'warning' }) {
  const cls =
    tone === 'comment'
      ? 'h-2.5 w-2.5 bg-bg-elevated border-2 border-border-strong'
      : tone === 'success'
        ? 'h-2.5 w-2.5 bg-success'
        : tone === 'warning'
          ? 'h-2.5 w-2.5 bg-warning'
          : 'h-1.5 w-1.5 bg-border-strong';
  return (
    <span
      aria-hidden="true"
      className={`absolute top-2 -translate-x-1/2 rounded-full ${cls}`}
      style={{ left: '-0.5px' }}
    />
  );
}

function QuietRow({ children, at, below }: { children: ReactNode; at: string; below?: ReactNode }) {
  return (
    <li className="relative pl-5 py-1">
      <Dot />
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs text-text-tertiary">
        {children}
        <span className="ml-auto shrink-0">
          <RelativeDate iso={at} />
        </span>
      </div>
      {below && <div className="mt-0.5 break-all text-xs text-text-tertiary">{below}</div>}
    </li>
  );
}

function TimelineRow({ item }: { item: TimelineItem }) {
  if (item.kind === 'event') {
    return <QuietRow at={item.at}>{item.text}</QuietRow>;
  }

  if (item.kind === 'sync') {
    const e = item.event;
    const state = jiraSyncStateLabel(e);
    return (
      <QuietRow at={item.at} below={e.error}>
        <span className="text-text-secondary">{jiraSyncEventLabel(e)}</span>
        {e.issue_key && <span className="font-mono">{e.issue_key}</span>}
        <span className="sr-only">{e.direction}</span>
        {state && <Badge variant={state.variant} label={state.label} size="sm" tint />}
      </QuietRow>
    );
  }

  const c = item.comment;
  // Edit diffs and approval notes are written by the server as "system".
  if (c.author_persona === 'system') {
    return (
      <QuietRow at={item.at}>
        <span className="whitespace-pre-wrap">{c.content}</span>
      </QuietRow>
    );
  }

  const author = c.author_persona ?? (c.source === 'jira' ? 'From Jira' : c.author_user_id.slice(0, 8));
  const v = item.verdict;
  const clean = v?.verdict === 'VERIFIED-CLEAN';
  return (
    <li className="relative pl-5 py-2">
      <Dot tone={v ? (clean ? 'success' : 'warning') : 'comment'} />
      <div
        className={`rounded-lg border px-3 py-2 ${
          v ? `border-border border-l-[3px] bg-surface ${clean ? 'border-l-success' : 'border-l-warning'}` : 'border-border bg-surface'
        }`}
      >
        <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-text-tertiary">
          <span className="font-medium text-text-primary">{author}</span>
          {c.source === 'jira' && <Badge variant="info" label="Jira" size="sm" />}
          {v && (
            <>
              <Badge
                variant={clean ? 'success' : 'warning'}
                tint
                label={`Round ${v.round}: ${clean ? 'verified clean' : 'changes requested'}`}
                size="sm"
              />
              <span title="Posted by a reviewer this organization registered as trusted; it counts toward the review state.">
                <Badge variant="default" label="Trusted reviewer" size="sm" />
              </span>
            </>
          )}
          {item.delivery && <DeliveryBadge delivery={item.delivery} />}
          <span className="ml-auto">
            <RelativeDate iso={c.created_at} />
          </span>
        </div>
        <p className="whitespace-pre-wrap text-sm text-text-primary">{c.content}</p>
        {item.delivery?.error && item.delivery.state !== 'done' && (
          <p className="mt-1 break-all text-xs text-text-tertiary">{item.delivery.error}</p>
        )}
      </div>
    </li>
  );
}
