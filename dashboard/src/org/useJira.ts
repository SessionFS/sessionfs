/**
 * React-Query hooks for the org-scoped Jira integration (Enterprise,
 * owner/admin). Targets GET/PUT/DELETE under
 * /api/v1/orgs/{org_id}/integrations/jira. Reads are admin-only because they
 * expose credential/config state, so every hook here is only mounted by an
 * admin surface.
 */

import { useEffect, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type {
  JiraConnection,
  JiraConnectionRequest,
  JiraHealth,
  JiraIssueType,
  JiraMapping,
  JiraMappingRequest,
  JiraProjectPage,
  JiraSyncEventsPage,
  JiraSyncSettingsRequest,
  JiraTemplate,
  JiraTemplatePreviewRequest,
  JiraTemplateRequest,
  JiraTemplateVersion,
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

/**
 * Ask the site which sprint / epic-link custom fields it has. Read only: the
 * caller puts the result in the connection form and saving stores it.
 */
export function useDetectJiraFields(orgId: string | undefined) {
  const { auth } = useAuth();
  return useMutation({
    mutationFn: () => auth!.client.detectJiraFields(orgId!),
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

/** Debounce delay between the last keystroke and the Jira project search. */
export const JIRA_PROJECT_SEARCH_DEBOUNCE_MS = 250;
/** Projects per page in the mapping picker (the server allows up to 100). */
export const JIRA_PROJECT_PAGE_SIZE = 50;

/**
 * Search the connected site's Jira projects for the mapping picker. Jira
 * filters on key and name server-side, so large sites (thousands of
 * projects) stay reachable; an empty query returns the first page.
 * The query is debounced so typing does not fire a request per keystroke, and
 * the previous page stays on screen while the next one loads.
 */
export function useJiraProjectSearch(orgId: string | undefined, query: string) {
  const { auth } = useAuth();
  const debounced = useDebouncedValue(query.trim().slice(0, 100), JIRA_PROJECT_SEARCH_DEBOUNCE_MS);
  const result = useQuery<JiraProjectPage>({
    queryKey: ['jiraProjects', orgId, debounced],
    queryFn: () =>
      auth!.client.searchJiraProjects(orgId!, { q: debounced, limit: JIRA_PROJECT_PAGE_SIZE }),
    enabled: !!auth && !!orgId,
    staleTime: 120_000,
    placeholderData: keepPreviousData,
  });
  // "Searching" covers both the debounce window and the request itself.
  const pending = debounced !== query.trim().slice(0, 100) || result.isFetching;
  return { ...result, searching: pending, debouncedQuery: debounced };
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
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

/** Recent two-way sync ledger rows for the admin card (GitHub #98 part B). */
export function useJiraSyncEvents(
  orgId: string | undefined,
  params: { limit?: number; direction?: 'inbound' | 'outbound' } = {},
) {
  const { auth } = useAuth();
  return useQuery<JiraSyncEventsPage>({
    queryKey: ['jiraSyncEvents', orgId, params.limit, params.direction],
    queryFn: () => auth!.client.getJiraSyncEvents(orgId!, params),
    enabled: !!auth && !!orgId,
    staleTime: 30_000,
  });
}

/**
 * Save two-way sync settings (status sync, comment sync, status map) via
 * `PATCH .../integrations/jira/sync`, so an admin can change them without
 * re-entering the Atlassian API token. Send only the fields that changed.
 */
export function useUpdateJiraSync(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: JiraSyncSettingsRequest) => auth!.client.updateJiraSync(orgId!, body),
    onSuccess: (data) => {
      queryClient.setQueryData<JiraConnection>(['jiraConnection', orgId], data);
      void queryClient.invalidateQueries({ queryKey: ['jiraConnection', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraHealth', orgId] });
    },
  });
}

/** Generate/rotate the inbound webhook secret (returned ONCE). */
export function useRotateJiraWebhookSecret(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => auth!.client.rotateJiraWebhookSecret(orgId!),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jiraConnection', orgId] });
    },
  });
}

/** Built-ins (read-only) + this org's active and archived custom templates. */
export function useJiraTemplates(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<JiraTemplate[]>({
    queryKey: ['jiraTemplates', orgId],
    queryFn: () => auth!.client.listJiraTemplates(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 60_000,
  });
}

export function useCreateJiraTemplate(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: JiraTemplateRequest) =>
      auth!.client.createJiraTemplate(orgId!, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jiraTemplates', orgId] });
    },
  });
}

export function useUpdateJiraTemplate(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ slug, body }: { slug: string; body: JiraTemplateRequest }) =>
      auth!.client.updateJiraTemplate(orgId!, slug, body),
    onSuccess: (_data, { slug }) => {
      void queryClient.invalidateQueries({ queryKey: ['jiraTemplates', orgId] });
      void queryClient.invalidateQueries({
        queryKey: ['jiraTemplateVersions', orgId, slug],
      });
    },
  });
}

export function useDeleteJiraTemplate(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => auth!.client.deleteJiraTemplate(orgId!, slug),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jiraTemplates', orgId] });
      // Archiving can put mappings/links on an archived template (health warns).
      void queryClient.invalidateQueries({ queryKey: ['jiraHealth', orgId] });
    },
  });
}

export function useJiraTemplateVersions(orgId: string | undefined, slug: string | null) {
  const { auth } = useAuth();
  return useQuery<JiraTemplateVersion[]>({
    queryKey: ['jiraTemplateVersions', orgId, slug],
    queryFn: () => auth!.client.listJiraTemplateVersions(orgId!, slug!),
    enabled: !!auth && !!orgId && !!slug,
    staleTime: 60_000,
  });
}

export function useUnarchiveJiraTemplate(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => auth!.client.unarchiveJiraTemplate(orgId!, slug),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['jiraTemplates', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['jiraHealth', orgId] });
    },
  });
}

/** Deterministic preview of a template (stored, or the editor's unsaved draft). */
export function usePreviewJiraTemplate(orgId: string | undefined) {
  const { auth } = useAuth();
  return useMutation({
    mutationFn: ({ slug, body }: { slug: string; body: JiraTemplatePreviewRequest }) =>
      auth!.client.previewJiraTemplate(orgId!, slug, body),
  });
}
