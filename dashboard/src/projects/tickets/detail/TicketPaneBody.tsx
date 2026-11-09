import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { useToast } from '../../../hooks/useToast';
import {
  useAddTicketComment,
  useApproveTicket,
  useAssignableUsers,
  useCloseTicket,
  useDismissTicket,
  useTicket,
  useTicketChildren,
  useTicketComments,
  useTicketEvents,
  useTicketJiraSyncEvents,
  useTicketReviewState,
  useTicketWatchers,
  useUnwatchTicket,
  useUpdateTicket,
  useWatchTicket,
} from '../../../hooks/useTickets';
import { useMe } from '../../../hooks/useMe';
import { useJiraLink } from '../../../hooks/useJiraTickets';
import type { Ticket } from '../../../api/client';
import { Alert, Button, TabPanel, Tabs } from '../../../components/ui';
import { errorMessage } from '../../../utils/errorMessage';
import { jiraErrorCode } from '../../../utils/jira';
import type { TicketJiraSurface } from '../constants';
import { ReassignPanel } from '../ReassignPanel';
import { ReapprovalConfirm } from '../ReapprovalConfirm';
import { NewTicketModal } from '../NewTicketModal';
import { DismissConfirm } from '../StatusMenu';
import { ticketActionAllowed, useTicketViewer } from '../viewer';
import { TERMINAL_STATUSES } from '../schedule';
import { buildTimeline, collectNames } from './timeline';
import { nextStep, assigneeName, type NextStepAction, type PaneTab } from './nextStep';
import { NextStepCard } from './NextStepCard';
import { PaneHeader, type PaneMode } from './PaneHeader';
import { OverviewTab } from './OverviewTab';
import { ActivityTab, type ActivityFilter } from './ActivityTab';
import { ReviewTab } from './ReviewTab';
import { ChildrenTab } from './ChildrenTab';
import { JiraTab } from './JiraTab';

/* ── Ticket pane body ──
 *
 * Everything inside the pane: the header, the next-step card and the tabs.
 * Data hooks and the write paths are the ones the old inline detail used;
 * only where things sit has changed. */

type Action = 'approve' | 'dismiss' | 'close' | 'comment';

export interface TicketPaneBodyProps {
  projectId: string;
  ticketId: string;
  /** The row from the list, shown until the ticket's own GET lands. */
  fallback?: Ticket;
  /** Tickets the page already has, by id: names related tickets without a fetch. */
  known: Map<string, Ticket>;
  mode: PaneMode;
  titleId: string;
  titleRef: RefObject<HTMLHeadingElement | null>;
  prevId: string | null;
  nextId: string | null;
  onStep: (id: string) => void;
  onNavigate: (id: string) => void;
  onClose: () => void;
  jira?: TicketJiraSurface;
  tab: PaneTab;
  onTab: (tab: PaneTab) => void;
}

/** Load a list of ticket ids and pair each with what has loaded, whatever
 *  order the queries come back in. */
function useRelated(projectId: string, ids: string[], known: Map<string, Ticket>) {
  const queries = useTicketChildren(projectId, ids);
  return useMemo(() => {
    const byId = new Map<string, { ticket?: Ticket; failed?: boolean }>();
    for (const q of queries ?? []) {
      const d = q?.data as Ticket | undefined;
      if (d?.id) byId.set(d.id, { ticket: d });
    }
    return ids.map((id, i) => {
      const hit = byId.get(id);
      if (hit) return { id, ...hit };
      const q = queries?.[i];
      const fromList = known.get(id);
      if (fromList) return { id, ticket: fromList };
      return { id, failed: !!q?.isError };
    });
  }, [queries, ids, known]);
}

export function TicketPaneBody({
  projectId,
  ticketId,
  fallback,
  known,
  mode,
  titleId,
  titleRef,
  prevId,
  nextId,
  onStep,
  onNavigate,
  onClose,
  jira,
  tab,
  onTab,
}: TicketPaneBodyProps) {
  const detailQuery = useTicket(projectId, ticketId);
  const loaded = detailQuery.data ?? fallback;
  if (!loaded) {
    return (
      <div className="space-y-3 p-4">
        <div className="flex justify-end">
          <Button variant="ghost" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
        <h2 id={titleId} ref={titleRef} tabIndex={-1} className="text-lg font-semibold text-text-primary outline-none">
          {ticketId}
        </h2>
        {detailQuery.isError ? (
          <Alert variant="danger">Couldn't load this ticket: {errorMessage(detailQuery.error)}</Alert>
        ) : (
          <p className="text-sm text-text-tertiary">Loading ticket…</p>
        )}
      </div>
    );
  }
  // Keyed by ticket: moving to another ticket starts with fresh editing
  // state (no open reassign editor, filter or draft carried over).
  return (
    <LoadedPane
      key={ticketId}
      projectId={projectId}
      ticketId={ticketId}
      t={loaded}
      known={known}
      mode={mode}
      titleId={titleId}
      titleRef={titleRef}
      prevId={prevId}
      nextId={nextId}
      onStep={onStep}
      onNavigate={onNavigate}
      onClose={onClose}
      jira={jira}
      tab={tab}
      onTab={onTab}
    />
  );
}

function LoadedPane({
  projectId,
  ticketId,
  t,
  known,
  mode,
  titleId,
  titleRef,
  prevId,
  nextId,
  onStep,
  onNavigate,
  onClose,
  jira,
  tab,
  onTab,
}: Omit<TicketPaneBodyProps, 'fallback'> & { t: Ticket }) {
  const { data: comments } = useTicketComments(projectId, ticketId);
  const { data: reviewState } = useTicketReviewState(projectId, ticketId);
  const eventsQuery = useTicketEvents(projectId, ticketId);
  const ticketEvents = useMemo(() => eventsQuery.data?.pages.flatMap((p) => p.events) ?? [], [eventsQuery.data]);
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
  const update = useUpdateTicket(projectId);
  const viewer = useTicketViewer();
  const { addToast } = useToast();
  const [draft, setDraft] = useState('');
  const [sendToJira, setSendToJira] = useState(false);
  const [submitting, setSubmitting] = useState<Action | null>(null);
  const [filter, setFilter] = useState<ActivityFilter>('all');
  const [reassigning, setReassigning] = useState(false);
  const [pendingPriority, setPendingPriority] = useState<string | null>(null);
  const [addingChild, setAddingChild] = useState(false);
  const [confirmDismiss, setConfirmDismiss] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Closing the reassign editor hands focus back to the button that opened it.
  const reassignButtonRef = useRef<HTMLButtonElement>(null);
  const wasReassigning = useRef(false);
  useEffect(() => {
    if (wasReassigning.current && !reassigning) reassignButtonRef.current?.focus();
    wasReassigning.current = reassigning;
  }, [reassigning]);


  // Ticket watching: the current user's row comes from the watchers list
  // and the me profile, so no extra "am I following" endpoint is needed.
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

  const childIds = useMemo(() => (t.kind === 'issue' ? t.child_ticket_ids : []), [t.kind, t.child_ticket_ids]);
  const children = useRelated(projectId, childIds, known);
  const dependencies = useRelated(projectId, t.depends_on, known);
  const { data: parentData } = useTicket(projectId, t.parent_ticket_id ?? undefined);
  const parent = parentData ?? (t.parent_ticket_id ? known.get(t.parent_ticket_id) : undefined);
  const parentProgress = useMemo(() => {
    if (!parent || parent.child_ticket_ids.length === 0) return null;
    const statuses = parent.child_ticket_ids.map((id) => (id === t.id ? t : known.get(id))?.status);
    if (statuses.some((s) => s === undefined)) return null;
    return { done: statuses.filter((s) => TERMINAL_STATUSES.has(s!)).length, total: statuses.length };
  }, [parent, known, t]);

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
  const canDismiss = (t.status === 'suggested' || t.status === 'open') && ticketActionAllowed('dismiss', t, viewer);
  const canClose = t.kind === 'issue' && t.status === 'in_progress' && ticketActionAllowed('close', t, viewer);
  // PUT /tickets/{id} admits the ticket's creator or a project admin for
  // field edits (services/ticket_writes.authorize_ticket_edit). Admin
  // standing comes from the viewer (the project's `can_admin`, resolved by
  // the page); the Jira surface's flag is only a fallback, and unknown means no.
  const isCreator = !!viewer.userId && t.created_by_user_id === viewer.userId;
  const isAdmin = viewer.isProjectAdmin ?? jira?.isProjectAdmin ?? false;
  const canEditFields = isCreator || isAdmin;
  // Any project editor (`can_edit`) may also assign, reassign or unassign.
  const canReassign = canEditFields || viewer.canEdit;
  // A non-editor's change to an approved ticket sends it back to suggested
  // (ticket_writes.apply_ticket_update), so those saves confirm first.
  const needsReapproval = !viewer.canEdit && t.status === 'open';

  // Assignment events carry only a user id. Names come from what the page
  // already has; the assignable-users list is fetched only when an event
  // names someone none of those cover.
  const knownNames = useMemo(() => collectNames(t, ticketEvents), [t, ticketEvents]);
  const needsNames = ticketEvents.some((e) =>
    [e.before, e.after].some((v) => {
      const uid = v && typeof v === 'object' ? (v as { user_id?: unknown }).user_id : null;
      return typeof uid === 'string' && !knownNames.has(uid);
    }),
  );
  // Readers can't list assignable users (403), so they don't ask.
  const { data: assignable } = useAssignableUsers(projectId, needsNames && viewer.canEdit);
  const names = useMemo(() => {
    const m = new Map(knownNames);
    for (const u of assignable ?? []) if (!m.has(u.id)) m.set(u.id, u.display_name || u.email);
    return m;
  }, [knownNames, assignable]);

  const timeline = useMemo(
    () =>
      buildTimeline(t, comments ?? [], syncEvents?.events ?? [], reviewState?.review_state?.rounds ?? [], {
        events: ticketEvents,
        hasMore: !!eventsQuery.hasNextPage,
        names,
      }),
    [t, comments, syncEvents, reviewState, ticketEvents, eventsQuery.hasNextPage, names],
  );

  const review = reviewState?.review_state ?? null;
  const step = nextStep({
    ticket: t,
    review,
    children: children.map((c) => c.ticket).filter((c): c is Ticket => !!c),
    dependencies: dependencies.map((d) => d.ticket).filter((d): d is Ticket => !!d),
    perms: {
      approve: canApprove,
      dismiss: canDismiss,
      close: canClose,
      reassign: canReassign,
      editSchedule: canEditFields,
    },
  });

  // The tabs this ticket has. A tab from a deep link that doesn't apply
  // falls back to Overview.
  const showReview = t.status === 'review' || !!review;
  const showChildren = t.kind === 'issue';
  const showJira = !!jira?.orgId;
  const available: PaneTab[] = [
    'overview',
    'activity',
    ...(showReview ? (['review'] as const) : []),
    ...(showChildren ? (['children'] as const) : []),
    ...(showJira ? (['jira'] as const) : []),
  ];
  const activeTab: PaneTab = available.includes(tab) ? tab : 'overview';

  function openTab(next: PaneTab) {
    onTab(next);
  }

  // Opens the inline editor in the Overview details, once that tab has
  // rendered (the editors and their focus handling live there).
  function editSchedule(field: 'due date' | 'estimate') {
    onTab('overview');
    const open = (tries: number) => {
      const trigger = bodyRef.current?.querySelector<HTMLButtonElement>(`[data-schedule-edit="${field}"]`);
      if (trigger) trigger.click();
      else if (tries > 0) requestAnimationFrame(() => open(tries - 1));
    };
    requestAnimationFrame(() => open(5));
  }

  function onStepAction(a: NextStepAction) {
    switch (a.kind) {
      case 'dismiss':
        setConfirmDismiss(true);
        return;
      case 'approve':
      case 'close':
        void callAction(a.kind);
        return;
      case 'tab':
        openTab(a.tab);
        return;
      case 'goto':
        onNavigate(a.ticketId);
        return;
      case 'assign':
        setReassigning(true);
        return;
      case 'due':
        editSchedule('due date');
        return;
    }
  }

  async function setPriority(value: string, confirmed = false) {
    if (value === t.priority) return;
    if (needsReapproval && !confirmed) {
      setPendingPriority(value);
      return;
    }
    try {
      await update.mutateAsync({ ticketId: t.id, body: { priority: value } });
      setPendingPriority(null);
      addToast('success', `Priority set to ${value}`);
    } catch (exc) {
      setPendingPriority(null);
      addToast('error', `Update failed: ${errorMessage(exc)}`);
    }
  }

  async function copyId() {
    try {
      await navigator.clipboard.writeText(t.id);
      addToast('success', `Copied ${t.id}`);
    } catch {
      addToast('error', 'Copy failed');
    }
  }


  const openFindings = review?.open_findings.length ?? 0;
  const lastRound = review?.rounds[review.rounds.length - 1];
  const childDone = children.filter((c) => c.ticket && TERMINAL_STATUSES.has(c.ticket.status)).length;
  const commentCount = (comments ?? []).filter((c) => c.author_persona !== 'system').length;

  const count = (text: string) => (
    <span className="ml-1.5 text-2xs font-normal tabular-nums text-text-tertiary">{text}</span>
  );
  const tabs = available.map((key) => {
    switch (key) {
      case 'overview':
        return { key, label: 'Overview' };
      case 'activity':
        return { key, label: <>Activity{commentCount > 0 && count(String(commentCount))}</> };
      case 'review':
        return {
          key,
          label: (
            <>
              Review
              {count(
                !lastRound
                  ? 'waiting'
                  : lastRound.verdict === 'VERIFIED-CLEAN'
                    ? `R${lastRound.round} clean`
                    : `R${lastRound.round} · ${openFindings} open`,
              )}
            </>
          ),
        };
      case 'children':
        return { key, label: <>Child tasks{count(`${childDone}/${childIds.length}`)}</> };
      case 'jira':
        return { key, label: <>Jira{t.jira && count(t.jira.issue_key)}</> };
    }
  });

  const tabsId = `ticket-pane-${t.id}`;
  const paneBg = mode === 'split' ? 'bg-bg-elevated' : 'bg-overlay';

  return (
    <div ref={bodyRef} className="space-y-4 text-sm">
      <PaneHeader
        projectId={projectId}
        ticket={t}
        titleId={titleId}
        titleRef={titleRef}
        mode={mode}
        prevId={prevId}
        nextId={nextId}
        onStep={onStep}
        onClose={onClose}
        parent={parent}
        parentProgress={parentProgress}
        onOpenTicket={onNavigate}
        watching={watching}
        muted={muted}
        followBusy={watch.isPending || unwatch.isPending}
        onToggleFollow={() => void (watching ? handleUnfollow() : handleFollow(false))}
        onCopyId={() => void copyId()}
        onOpenJira={showJira ? () => openTab('jira') : null}
        canReassign={canReassign}
        reassigning={reassigning}
        onReassign={() => setReassigning((v) => !v)}
        reassignRef={reassignButtonRef}
        canEditPriority={canEditFields}
        onPriority={(v) => void setPriority(v)}
        canEditSchedule={canEditFields}
        onEditSchedule={editSchedule}
        below={
          <>
            {reassigning && (
              <ReassignPanel
                projectId={projectId}
                ticket={t}
                onDone={() => setReassigning(false)}
                needsReapproval={needsReapproval}
                orgId={jira?.orgId}
              />
            )}
            {pendingPriority && (
              <div
                role="group"
                aria-label="Confirm priority change"
                onKeyDown={(e) => {
                  if (e.key !== 'Escape') return;
                  e.preventDefault();
                  e.stopPropagation();
                  if (!update.isPending) setPendingPriority(null);
                }}
              >
                <ReapprovalConfirm
                  pending={update.isPending}
                  onConfirm={() => void setPriority(pendingPriority, true)}
                  onCancel={() => setPendingPriority(null)}
                />
              </div>
            )}
          </>
        }
      />

      {step && <NextStepCard step={step} onAction={onStepAction} busy={submitting === 'comment' ? null : submitting} />}

      <div className={`sticky top-0 z-10 -mx-4 px-4 ${paneBg}`}>
        <Tabs
          tabs={tabs}
          activeKey={activeTab}
          onChange={(k) => openTab(k as PaneTab)}
          bare
          idPrefix={tabsId}
          aria-label="Ticket sections"
        />
      </div>

      <TabPanel idPrefix={tabsId} tabKey={activeTab} className="outline-none">
        {activeTab === 'overview' && (
          <OverviewTab
            projectId={projectId}
            ticket={t}
            dependencies={dependencies}
            onOpenTicket={onNavigate}
            canEditFields={canEditFields}
            needsReapproval={needsReapproval}
            follow={{
              watching,
              muted,
              followPending: watch.isPending,
              unfollowPending: unwatch.isPending,
              onFollow: () => void handleFollow(false),
              onUnfollow: () => void handleUnfollow(),
              onToggleMute: () => void handleToggleMute(),
            }}
          />
        )}
        {activeTab === 'activity' && (
          <ActivityTab
            items={timeline}
            filter={filter}
            onFilter={setFilter}
            onLoadEarlier={() => void eventsQuery.fetchNextPage()}
            loadingEarlier={eventsQuery.isFetchingNextPage}
            loadEarlierFailed={eventsQuery.isFetchNextPageError}
            stickyComposer
            stickyBg={paneBg}
            composer={{
              draft,
              setDraft,
              canSendToJira,
              sendToJira,
              setSendToJira,
              submitting: submitting === 'comment',
              disabled: submitting !== null,
              onSubmit: () => void callAction('comment'),
            }}
          />
        )}
        {activeTab === 'review' && <ReviewTab review={review} assignee={assigneeName(t)} />}
        {activeTab === 'children' && (
          <ChildrenTab
            children={children}
            onOpenTicket={onNavigate}
            onAddChild={() => setAddingChild(true)}
          />
        )}
        {activeTab === 'jira' && jira?.orgId && (
          <JiraTab
            projectId={projectId}
            ticket={t}
            orgId={jira.orgId}
            isProjectAdmin={jira.isProjectAdmin ?? false}
            isEnterprise={jira.isEnterprise ?? false}
            canEdit={jira.canEdit ?? true}
            onNavigateTicket={onNavigate}
            syncItems={timeline.filter((i) => i.kind === 'sync')}
          />
        )}
      </TabPanel>

      <DismissConfirm
        ticket={t}
        open={confirmDismiss}
        pending={submitting === 'dismiss'}
        onCancel={() => setConfirmDismiss(false)}
        onConfirm={async () => {
          await callAction('dismiss');
          setConfirmDismiss(false);
        }}
      />

      {addingChild && (
        <NewTicketModal
          projectId={projectId}
          orgId={jira?.orgId}
          defaultParentId={t.id}
          onClose={() => setAddingChild(false)}
        />
      )}
    </div>
  );
}
