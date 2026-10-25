/**
 * React-Query hooks for the session helper's read-only API surface
 * (GET /status, and the conversations list/get/delete). Turn streaming is
 * handled separately in HelperPanel with raw `fetch` + ReadableStream.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type { HelperConversation, HelperConversationSummary, HelperStatus } from '../api/client';

export function useHelperStatus(open: boolean) {
  const { auth } = useAuth();
  return useQuery<HelperStatus>({
    queryKey: ['helperStatus'],
    queryFn: () => auth!.client.getHelperStatus(),
    // The drawer only needs these while it is open — skip the fetch when it is
    // closed so merely mounting the panel does not hit the API.
    enabled: !!auth && open,
    staleTime: 60_000,
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
