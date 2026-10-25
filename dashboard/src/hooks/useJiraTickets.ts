/**
 * React-Query hooks for ticket-level Jira promotion (Enterprise). Preview and
 * publish require `tickets:write` on the project; unlink is project-admin
 * only. A non-admin member can still preview/publish — the ticket response
 * (and these endpoints' error codes) are how the UI learns the org isn't
 * connected or the project isn't mapped.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type { JiraLink, JiraPreview, JiraPublishRequest } from '../api/client';

export function useJiraPreview(
  projectId: string | undefined,
  ticketId: string | undefined,
  enabled: boolean,
) {
  const { auth } = useAuth();
  return useQuery<JiraPreview>({
    queryKey: ['jiraPreview', projectId, ticketId],
    queryFn: () => auth!.client.previewJira(projectId!, ticketId!),
    enabled: !!auth && !!projectId && !!ticketId && enabled,
    // A draft is built from the live ticket each time and can carry a
    // re-publish diff, so never serve a stale preview. The dialog also
    // clears this cache on close; gcTime 0 and retry:false stop a leftover
    // (possibly stale) draft from seeding the next open.
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
}

export function usePublishJira(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      ticketId,
      body,
    }: {
      ticketId: string;
      body: JiraPublishRequest;
    }) => auth!.client.publishJira(projectId!, ticketId, body),
    onSuccess: (_data, { ticketId }) => {
      void queryClient.invalidateQueries({ queryKey: ['ticket', projectId, ticketId] });
      void queryClient.invalidateQueries({ queryKey: ['tickets', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraLink', projectId, ticketId] });
      void queryClient.removeQueries({ queryKey: ['jiraPreview', projectId, ticketId] });
      // The publish writes a Jira sync note into the ticket's comment thread.
      void queryClient.invalidateQueries({ queryKey: ['ticketComments', projectId, ticketId] });
    },
  });
}

export function useJiraLink(
  projectId: string | undefined,
  ticketId: string | undefined,
  enabled: boolean,
) {
  const { auth } = useAuth();
  return useQuery<JiraLink>({
    queryKey: ['jiraLink', projectId, ticketId],
    queryFn: () => auth!.client.getJiraLink(projectId!, ticketId!),
    enabled: !!auth && !!projectId && !!ticketId && enabled,
    staleTime: 30_000,
  });
}

export function useUnlinkJira(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (ticketId: string) => auth!.client.unlinkJira(projectId!, ticketId),
    onSuccess: (_data, ticketId) => {
      void queryClient.invalidateQueries({ queryKey: ['ticket', projectId, ticketId] });
      void queryClient.invalidateQueries({ queryKey: ['tickets', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraLink', projectId, ticketId] });
    },
  });
}
