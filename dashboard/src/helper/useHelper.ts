/**
 * React-Query hooks for the session helper's read-only API surface
 * (GET /status, and the conversations list/get/delete). Turn streaming is
 * handled separately in HelperPanel with raw `fetch` + ReadableStream.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type { HelperConversation, HelperConversationSummary, HelperStatus } from '../api/client';
import type { HelperScope } from './HelperProvider';
import { scopeKey } from './scope';

export function useHelperStatus(open: boolean, scope?: HelperScope) {
  const { auth } = useAuth();
  return useQuery<HelperStatus>({
    queryKey: ['helperStatus', scopeKey(scope)],
    queryFn: () =>
      auth!.client.getHelperStatus(
        scope ? { project_id: scope.project_id, session_ids: scope.session_ids } : undefined,
      ),
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
