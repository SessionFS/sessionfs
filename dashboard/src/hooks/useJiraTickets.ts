/**
 * React-Query hooks for ticket-level Jira promotion (Enterprise). Preview and
 * publish require `tickets:write` on the project; unlink is project-admin
 * only. A non-admin member can still preview/publish — the ticket response
 * (and these endpoints' error codes) are how the UI learns the org isn't
 * connected or the project isn't mapped.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type {
  JiraLink,
  Ticket,
  JiraPreview,
  JiraPublishRequest,
  JiraTicketTemplates,
} from '../api/client';
import { jiraErrorCode } from '../utils/jira';

/**
 * Draft for a ticket. `template` is the per-ticket choice from the promotion
 * dialog's picker; omitted, the server resolves mapping → connection default.
 */
export function useJiraPreview(
  projectId: string | undefined,
  ticketId: string | undefined,
  enabled: boolean,
  template?: string,
) {
  const { auth } = useAuth();
  return useQuery<JiraPreview>({
    queryKey: ['jiraPreview', projectId, ticketId, template ?? null],
    queryFn: () =>
      auth!.client.previewJira(projectId!, ticketId!, template ? { template } : undefined),
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

/**
 * Link an existing Jira issue to an unlinked ticket. On success the returned
 * link seeds the link query and the ticket's embedded summary right away, so
 * the section switches to its linked state without flashing the unlinked
 * controls while the ticket refetches.
 */
export function useLinkJiraIssue(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ ticketId, issueKey }: { ticketId: string; issueKey: string }) =>
      auth!.client.linkJiraIssue(projectId!, ticketId, issueKey),
    onSuccess: (link, { ticketId }) => {
      queryClient.setQueryData<JiraLink>(['jiraLink', projectId, ticketId], link);
      queryClient.setQueryData<Ticket>(['ticket', projectId, ticketId], (prev) =>
        prev
          ? {
              ...prev,
              jira: {
                issue_key: link.issue_key,
                issue_url: link.issue_url,
                last_known_status: link.last_known_status,
              },
            }
          : prev,
      );
      void queryClient.invalidateQueries({ queryKey: ['ticket', projectId, ticketId] });
      void queryClient.invalidateQueries({ queryKey: ['tickets', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraLink', projectId, ticketId] });
      // The timeline may gain a sync note or event for the new link.
      void queryClient.invalidateQueries({ queryKey: ['ticketComments', projectId, ticketId] });
      void queryClient.invalidateQueries({ queryKey: ['ticketJiraSyncEvents', projectId, ticketId] });
    },
    onError: (err, { ticketId }) => {
      // Someone else linked or published it meanwhile: show the real state.
      if (jiraErrorCode(err) === 'jira_already_linked') {
        void queryClient.invalidateQueries({ queryKey: ['ticket', projectId, ticketId] });
        void queryClient.invalidateQueries({ queryKey: ['tickets', projectId] });
      }
    },
  });
}

/**
 * Replace the ticket's title and description with the linked Jira issue's.
 * The response carries the updated ticket and link, seeded straight into the
 * caches before the ticket views refresh.
 */
export function usePullJira(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (ticketId: string) => auth!.client.pullJira(projectId!, ticketId),
    onSuccess: (result, ticketId) => {
      // Merge rather than replace: a pull response may omit the fields a
      // ticket read carries (the Jira link, children, publish flags), and
      // dropping them would flash the ticket as unlinked until the refetch.
      queryClient.setQueryData<Ticket>(['ticket', projectId, ticketId], (prev) =>
        prev
          ? {
              ...prev,
              ...result.ticket,
              jira: result.ticket.jira ?? prev.jira,
              child_ticket_ids: result.ticket.child_ticket_ids ?? prev.child_ticket_ids,
              jira_publish_in_progress: result.ticket.jira_publish_in_progress ?? prev.jira_publish_in_progress,
              jira_publish_unknown: result.ticket.jira_publish_unknown ?? prev.jira_publish_unknown,
            }
          : prev, // nothing cached yet: let the refetch below fill it
      );
      queryClient.setQueryData<JiraLink>(['jiraLink', projectId, ticketId], result.jira);
      void queryClient.invalidateQueries({ queryKey: ['ticket', projectId, ticketId] });
      void queryClient.invalidateQueries({ queryKey: ['tickets', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraLink', projectId, ticketId] });
      void queryClient.invalidateQueries({ queryKey: ['ticketComments', projectId, ticketId] });
      void queryClient.invalidateQueries({ queryKey: ['ticketJiraSyncEvents', projectId, ticketId] });
      // A draft built before the pull would carry the old text.
      queryClient.removeQueries({ queryKey: ['jiraPreview', projectId, ticketId] });
    },
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

/** Templates a member may pick for this ticket (minimal: slug/name/version). */
export function useTicketJiraTemplates(
  projectId: string | undefined,
  ticketId: string | undefined,
  enabled: boolean,
) {
  const { auth } = useAuth();
  return useQuery<JiraTicketTemplates>({
    queryKey: ['jiraTicketTemplates', projectId, ticketId],
    queryFn: () => auth!.client.listTicketJiraTemplates(projectId!, ticketId!),
    enabled: !!auth && !!projectId && !!ticketId && enabled,
    staleTime: 0,
    retry: false,
  });
}
