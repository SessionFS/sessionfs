/**
 * React-Query hooks for the session helper's read-only API surface
 * (GET /status, and the conversations list/get/delete). Turn streaming is
 * handled separately in HelperPanel with raw `fetch` + ReadableStream.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type { HelperAction, HelperActionConfirmBody, HelperConversation, HelperConversationSummary, HelperStatus } from '../api/client';
import type { HelperScope } from './HelperProvider';
import { apiScope, scopeKey, type GroupChoice } from './scope';

export function useHelperStatus(open: boolean, scope?: HelperScope) {
  const { auth } = useAuth();
  return useQuery<HelperStatus>({
    queryKey: ['helperStatus', scopeKey(scope)],
    queryFn: () =>
      auth!.client.getHelperStatus(apiScope(scope)),
    // The drawer only needs these while it is open — skip the fetch when it is
    // closed so merely mounting the panel does not hit the API. Keying on the
    // scope (not just the project) means a scope switch or a restored thread
    // refetches the availability for its actual scope.
    enabled: !!auth && open,
    staleTime: 60_000,
    // Keep the previous scope's status on screen while a scope switch or a
    // restored thread's scope refetches, so the composer and restored turns
    // don't unmount into a skeleton flash (the panel disables Send while the
    // replacement status is pending via `isPlaceholderData`).
    placeholderData: keepPreviousData,
  });
}

export function useHelperConversations(open: boolean) {
  const { auth } = useAuth();
  return useQuery<HelperConversationSummary[]>({
    queryKey: ['helperConversations'],
    queryFn: () => auth!.client.listHelperConversations(),
    enabled: !!auth && open,
    staleTime: 30_000,
  });
}

/** Project groups the caller can see (GET /me/groups), offered as scopes. */
export function useHelperGroups(open: boolean) {
  const { auth } = useAuth();
  return useQuery<GroupChoice[]>({
    // Shares the cache entry of the groups pages' useMyGroups.
    queryKey: ['myGroups'],
    queryFn: () => auth!.client.listMyGroups(),
    enabled: !!auth && open,
    staleTime: 60_000,
    // Groups are an optional extra in the menu: no retries, no error UI.
    retry: false,
  });
}

export function useHelperConversation(conversationId: string | null) {
  const { auth } = useAuth();
  return useQuery<HelperConversation>({
    queryKey: ['helperConversation', conversationId],
    queryFn: () => auth!.client.getHelperConversation(conversationId!),
    enabled: !!auth && !!conversationId,
  });
}

export function useDeleteHelperConversation() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (conversationId: string) => auth!.client.deleteHelperConversation(conversationId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['helperConversations'] });
    },
  });
}

// ── Confirm-first write actions ──

export const helperActionKey = (id: string) => ['helperAction', id] as const;

/**
 * One proposed action. A card streamed in this turn starts from the proposal
 * (`seed`) without a request; a card restored from history reads the current
 * status (pending / confirmed / cancelled / expired) from the server.
 */
export function useHelperAction(id: string, seed: HelperAction, fromHistory: boolean) {
  const { auth } = useAuth();
  return useQuery<HelperAction>({
    queryKey: helperActionKey(id),
    queryFn: () => auth!.client.getHelperAction(id),
    enabled: !!auth && fromHistory,
    initialData: fromHistory ? undefined : seed,
    staleTime: Infinity,
    retry: false,
  });
}

/** The decision a card can take: cancel, or confirm with an optional body. */
export type HelperDecision =
  | { decision: 'confirm'; body?: HelperActionConfirmBody }
  | { decision: 'cancel' };

/** Confirm or cancel an action; the result replaces the cached record. */
export function useDecideHelperAction(id: string) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (d: HelperDecision) =>
      d.decision === 'confirm'
        ? auth!.client.confirmHelperAction(id, d.body)
        : auth!.client.cancelHelperAction(id),
    onSuccess: (action, d) => {
      queryClient.setQueryData(helperActionKey(id), action);
      if (d.decision === 'confirm') {
        // Whatever the action touched is now stale wherever it is shown.
        for (const key of ['projects', 'project', 'compileStatus', 'sessions', 'session', 'tickets', 'ticket']) {
          void queryClient.invalidateQueries({ queryKey: [key] });
        }
        // Jira-touching actions also change the ticket's link, its timeline
        // comments and its sync events.
        if (touchesJira(action)) {
          for (const key of ['jiraLink', 'ticketComments', 'ticketJiraSyncEvents']) {
            void queryClient.invalidateQueries({ queryKey: [key] });
          }
        }
      }
    },
  });
}

/** Whether a confirmed action may have changed a ticket's Jira state. */
export function touchesJira(action: { kind: string; params?: Record<string, unknown> }): boolean {
  if (action.kind === 'link_jira' || action.kind === 'jira_overwrite') return true;
  if (action.kind === 'create_ticket') return action.params?.publish_to_jira === true;
  if (action.kind === 'comment_ticket') return action.params?.send_to_jira === true;
  return false;
}
