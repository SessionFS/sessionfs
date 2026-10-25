/**
 * React-Query hooks for the org-scoped Jira integration (Enterprise,
 * owner/admin). Targets GET/PUT/DELETE under
 * /api/v1/orgs/{org_id}/integrations/jira. Reads are admin-only because they
 * expose credential/config state, so every hook here is only mounted by an
 * admin surface.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type {
  JiraConnection,
  JiraConnectionRequest,
  JiraHealth,
  JiraIssueType,
  JiraMapping,
  JiraMappingRequest,
  JiraProject,
} from '../api/client';

export function useJiraConnection(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<JiraConnection>({
    queryKey: ['jiraConnection', orgId],
    queryFn: () => auth!.client.getJiraConnection(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 60_000,
  });
}

export function useSaveJiraConnection(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: JiraConnectionRequest) =>
      auth!.client.saveJiraConnection(orgId!, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jiraConnection', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraHealth', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraMappings', orgId] });
      // The remote project/issue-type catalogs change with the connection.
      void queryClient.invalidateQueries({ queryKey: ['jiraProjects', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraIssueTypes', orgId] });
    },
  });
}

export function useDeleteJiraConnection(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => auth!.client.deleteJiraConnection(orgId!),
    onSuccess: () => {
      queryClient.setQueryData<JiraConnection>(['jiraConnection', orgId], {
        connected: false,
        has_token: false,
      });
      void queryClient.invalidateQueries({ queryKey: ['jiraHealth', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraMappings', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraProjects', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraIssueTypes', orgId] });
    },
  });
}

export function useJiraProjects(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<JiraProject[]>({
    queryKey: ['jiraProjects', orgId],
    queryFn: () => auth!.client.listJiraProjects(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 120_000,
  });
}

export function useJiraIssueTypes(
  orgId: string | undefined,
  projectKey: string | null,
) {
  const { auth } = useAuth();
  return useQuery<JiraIssueType[]>({
    queryKey: ['jiraIssueTypes', orgId, projectKey],
    queryFn: () => auth!.client.listJiraIssueTypes(orgId!, projectKey!),
    enabled: !!auth && !!orgId && !!projectKey,
    staleTime: 120_000,
  });
}

export function useJiraMappings(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<JiraMapping[]>({
    queryKey: ['jiraMappings', orgId],
    queryFn: () => auth!.client.listJiraMappings(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 60_000,
  });
}

export function useSaveJiraMapping(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      projectId,
      body,
    }: {
      projectId: string;
      body: JiraMappingRequest;
    }) => auth!.client.saveJiraMapping(orgId!, projectId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jiraMappings', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraHealth', orgId] });
    },
  });
}

export function useDeleteJiraMapping(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (projectId: string) =>
      auth!.client.deleteJiraMapping(orgId!, projectId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jiraMappings', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraHealth', orgId] });
    },
  });
}

export function useJiraHealth(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<JiraHealth>({
    queryKey: ['jiraHealth', orgId],
    queryFn: () => auth!.client.getJiraHealth(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 60_000,
  });
}
