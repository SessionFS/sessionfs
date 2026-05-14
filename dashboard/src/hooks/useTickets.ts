import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { Ticket, TicketComment, TicketCreate } from '../api/client';

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
  filters: { status?: string; assigned_to?: string; priority?: string } = {},
) {
  const { auth } = useAuth();
  return useQuery<Ticket[]>({
    queryKey: ['tickets', projectId, filters],
    queryFn: () => auth!.client.listTickets(projectId!, filters),
    enabled: !!auth && !!projectId,
    staleTime: 15_000,
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

export function useAddTicketComment(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ticketId, content }: { ticketId: string; content: string }) =>
      auth!.client.addTicketComment(projectId!, ticketId, content),
    onSuccess: (_data, { ticketId }) => {
      void qc.invalidateQueries({
        queryKey: ['ticketComments', projectId, ticketId],
      });
    },
  });
}
