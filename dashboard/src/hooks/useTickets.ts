import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { ApiError, type AssignableUser, type JiraSyncEventsPage, type Ticket, type TicketComment, type TicketCreate, type TicketPreview, type TicketReviewState, type TicketUpdate, type TicketWatcher } from '../api/client';

/**
 * React-Query hooks for v0.10.1 Tickets (Team+).
 *
 * Read-mostly surface for the dashboard. The FSM lifecycle transitions
 * (start, complete, resolve, block/unblock) live behind the CLI / MCP
 * because they're inherently agent-driven and require the local
 * provenance bundle. The dashboard exposes the moderation transitions
 * a human reviewer wants:
 *   - approve   (suggested → open)
 *   - dismiss   (suggested/open → cancelled)
 *   - comment
 */

export function useTickets(
  projectId: string | undefined,
  filters: {
    status?: string;
    assigned_to?: string;
    /** `me`: tickets assigned to the caller as a person. */
    assigned_user?: string;
    priority?: string;
    kind?: string;
  } = {},
) {
  const { auth } = useAuth();
  return useQuery<Ticket[]>({
    queryKey: ['tickets', projectId, filters],
    queryFn: () => auth!.client.listTickets(projectId!, filters),
    enabled: !!auth && !!projectId,
    staleTime: 15_000,
  });
}

/**
 * Parallel-fetch child Tickets for an Issue rollup. Each child shares the
 * `['ticket', projectId, id]` key so cached child reads hit the same entry
 * as a direct expand of that child.
 */
export function useTicketChildren(
  projectId: string | undefined,
  ids: string[] | undefined,
) {
  const { auth } = useAuth();
  return useQueries({
    queries: (ids ?? []).map((id) => ({
      queryKey: ['ticket', projectId, id],
      queryFn: () => auth!.client.getTicket(projectId!, id),
      enabled: !!auth && !!projectId,
      staleTime: 15_000,
    })),
  });
}

export function useTicket(
  projectId: string | undefined,
  ticketId: string | undefined,
) {
  const { auth } = useAuth();
  return useQuery<Ticket>({
    queryKey: ['ticket', projectId, ticketId],
    queryFn: () => auth!.client.getTicket(projectId!, ticketId!),
    enabled: !!auth && !!projectId && !!ticketId,
    staleTime: 15_000,
  });
}

export function useTicketComments(
  projectId: string | undefined,
  ticketId: string | undefined,
) {
  const { auth } = useAuth();
  return useQuery<TicketComment[]>({
    queryKey: ['ticketComments', projectId, ticketId],
    queryFn: () => auth!.client.listTicketComments(projectId!, ticketId!),
    enabled: !!auth && !!projectId && !!ticketId,
    staleTime: 10_000,
  });
}

/** Trusted review rounds for a ticket: which comments are verdicts. */
export function useTicketReviewState(
  projectId: string | undefined,
  ticketId: string | undefined,
) {
  const { auth } = useAuth();
  return useQuery<TicketReviewState>({
    queryKey: ['ticketReviewState', projectId, ticketId],
    queryFn: () => auth!.client.getTicketReviewState(projectId!, ticketId!),
    enabled: !!auth && !!projectId && !!ticketId,
    staleTime: 10_000,
  });
}

/** Two-way sync ledger rows for one linked ticket (Jira activity timeline). */
export function useTicketJiraSyncEvents(
  projectId: string | undefined,
  ticketId: string | undefined,
  /** Only fetch for a Jira-linked ticket; unlinked tickets would 403/409. */
  enabled = true,
) {
  const { auth } = useAuth();
  return useQuery<JiraSyncEventsPage>({
    queryKey: ['ticketJiraSyncEvents', projectId, ticketId],
    queryFn: () => auth!.client.getTicketJiraSyncEvents(projectId!, ticketId!),
    enabled: !!auth && !!projectId && !!ticketId && enabled,
    staleTime: 15_000,
  });
}

export function useCreateTicket(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: TicketCreate) => auth!.client.createTicket(projectId!, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['tickets', projectId] });
    },
  });
}

/** Partial update (PUT). Used for reassignment from the ticket detail. */
export function useUpdateTicket(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ticketId, body }: { ticketId: string; body: TicketUpdate }) =>
      auth!.client.updateTicket(projectId!, ticketId, body),
    onSuccess: (_data, { ticketId }) => {
      void qc.invalidateQueries({ queryKey: ['tickets', projectId] });
      void qc.invalidateQueries({ queryKey: ['ticket', projectId, ticketId] });
      // The server posts a system diff comment for every edit.
      void qc.invalidateQueries({ queryKey: ['ticketComments', projectId, ticketId] });
    },
  });
}

/** People a ticket in this project may be assigned to. */
export function useAssignableUsers(projectId: string | undefined, enabled = true) {
  const { auth } = useAuth();
  return useQuery<AssignableUser[]>({
    queryKey: ['assignableUsers', projectId],
    queryFn: async () => (await auth!.client.listAssignableUsers(projectId!)).users,
    enabled: !!auth && !!projectId && enabled,
    staleTime: 60_000,
    // A 403 (a reader who may not assign people) won't change on a retry.
    retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
  });
}

export function useApproveTicket(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ticketId: string) =>
      auth!.client.approveTicket(projectId!, ticketId),
    onSuccess: (_data, ticketId) => {
      void qc.invalidateQueries({ queryKey: ['tickets', projectId] });
      void qc.invalidateQueries({ queryKey: ['ticket', projectId, ticketId] });
    },
  });
}

export function useDismissTicket(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ticketId: string) =>
      auth!.client.dismissTicket(projectId!, ticketId),
    onSuccess: (_data, ticketId) => {
      void qc.invalidateQueries({ queryKey: ['tickets', projectId] });
      void qc.invalidateQueries({ queryKey: ['ticket', projectId, ticketId] });
    },
  });
}

export function useCloseTicket(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ticketId: string) =>
      auth!.client.closeTicket(projectId!, ticketId),
    onSuccess: (_data, ticketId) => {
      void qc.invalidateQueries({ queryKey: ['tickets', projectId] });
      void qc.invalidateQueries({ queryKey: ['ticket', projectId, ticketId] });
    },
  });
}

export function useAddTicketComment(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      ticketId,
      content,
      sendToJira,
    }: {
      ticketId: string;
      content: string;
      sendToJira?: boolean;
    }) => auth!.client.addTicketComment(projectId!, ticketId, content, sendToJira),
    onSuccess: (_data, { ticketId, sendToJira }) => {
      void qc.invalidateQueries({
        queryKey: ['ticketComments', projectId, ticketId],
      });
      // A reviewer's comment may be a trusted verdict.
      void qc.invalidateQueries({
        queryKey: ['ticketReviewState', projectId, ticketId],
      });
      if (sendToJira) {
        // The outbound push is queued in the sync ledger; show it right away.
        void qc.invalidateQueries({
          queryKey: ['ticketJiraSyncEvents', projectId, ticketId],
        });
      }
    },
  });
}

/* ── Ticket watching (follow / unfollow / mute) ── */

export function useTicketWatchers(
  projectId: string | undefined,
  ticketId: string | undefined,
) {
  const { auth } = useAuth();
  return useQuery<TicketWatcher[]>({
    queryKey: ['ticketWatchers', projectId, ticketId],
    queryFn: () => auth!.client.listTicketWatchers(projectId!, ticketId!),
    enabled: !!auth && !!projectId && !!ticketId,
    staleTime: 15_000,
  });
}

export function useWatchTicket(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ticketId, muted }: { ticketId: string; muted?: boolean }) =>
      auth!.client.watchTicket(projectId!, ticketId, muted ?? false),
    onSuccess: (_data, { ticketId }) => {
      void qc.invalidateQueries({ queryKey: ['ticketWatchers', projectId, ticketId] });
    },
  });
}

export function useUnwatchTicket(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ticketId: string) =>
      auth!.client.unwatchTicket(projectId!, ticketId),
    onSuccess: (_data, ticketId) => {
      void qc.invalidateQueries({ queryKey: ['ticketWatchers', projectId, ticketId] });
    },
  });
}

/**
 * A ticket by id alone (for link previews). A 404 means "missing or not
 * readable" and won't change on a retry.
 */
export function useTicketPreview(ticketId: string | null | undefined) {
  const { auth } = useAuth();
  return useQuery<TicketPreview>({
    queryKey: ['ticketPreview', ticketId],
    queryFn: () => auth!.client.getTicketById(ticketId!),
    enabled: !!auth && !!ticketId,
    staleTime: 30_000,
    gcTime: 5 * 60_000,
    retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 1,
  });
}
