/**
 * React-Query hooks for the org-scoped Confluence integration (Enterprise,
 * owner/admin). Targets GET/PUT/DELETE under
 * /api/v1/orgs/{org_id}/integrations/confluence. Reads are admin-only because
 * they expose credential/config state, so every hook here is only mounted by
 * an admin surface.
 */

import { useEffect, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type {
  ConfluenceConnection,
  ConfluenceConnectionRequest,
  ConfluenceHealth,
  ConfluenceMapping,
  ConfluenceMappingRequest,
  ConfluenceSpacePage,
  ConfluenceSpaceSearch,
  ConfluenceTemplate,
} from '../api/client';

export function useConfluenceConnection(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<ConfluenceConnection>({
    queryKey: ['confluenceConnection', orgId],
    queryFn: () => auth!.client.getConfluenceConnection(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 60_000,
  });
}

export function useSaveConfluenceConnection(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ConfluenceConnectionRequest) =>
      auth!.client.saveConfluenceConnection(orgId!, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['confluenceConnection', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['confluenceHealth', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['confluenceMappings', orgId] });
    },
  });
}

export function useDeleteConfluenceConnection(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => auth!.client.deleteConfluenceConnection(orgId!),
    onSuccess: () => {
      queryClient.setQueryData<ConfluenceConnection>(['confluenceConnection', orgId], {
        connected: false,
        has_token: false,
      });
      void queryClient.invalidateQueries({ queryKey: ['confluenceHealth', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['confluenceMappings', orgId] });
    },
  });
}

/** Debounce delay between the last keystroke and the space/page search. */
export const CONFLUENCE_SPACE_SEARCH_DEBOUNCE_MS = 250;
/** Spaces/pages per page in the mapping picker (the server allows up to 100). */
export const CONFLUENCE_SPACE_PAGE_SIZE = 50;

/**
 * Search the connected site's Confluence spaces for the mapping picker. An
 * empty query returns the first page; the query is debounced so typing does
 * not fire a request per keystroke, and the previous page stays on screen
 * while the next one loads.
 */
export function useConfluenceSpaceSearch(orgId: string | undefined, query: string) {
  const { auth } = useAuth();
  const debounced = useDebouncedValue(query.trim().slice(0, 100), CONFLUENCE_SPACE_SEARCH_DEBOUNCE_MS);
  const result = useQuery<ConfluenceSpaceSearch>({
    queryKey: ['confluenceSpaces', orgId, debounced],
    queryFn: () =>
      auth!.client.searchConfluenceSpaces(orgId!, { q: debounced, limit: CONFLUENCE_SPACE_PAGE_SIZE }),
    enabled: !!auth && !!orgId,
    staleTime: 120_000,
    placeholderData: keepPreviousData,
  });
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

/** Search pages in a picked space (for the parent-page picker). */
export function useConfluencePageSearch(
  orgId: string | undefined,
  spaceKey: string | null,
  query: string,
) {
  const { auth } = useAuth();
  const debounced = useDebouncedValue(query.trim().slice(0, 100), CONFLUENCE_SPACE_SEARCH_DEBOUNCE_MS);
  const result = useQuery<ConfluenceSpacePage[]>({
    queryKey: ['confluencePages', orgId, spaceKey, debounced],
    queryFn: () =>
      auth!.client.listConfluenceSpacePages(orgId!, spaceKey!, {
        q: debounced,
        limit: CONFLUENCE_SPACE_PAGE_SIZE,
      }),
    enabled: !!auth && !!orgId && !!spaceKey,
    staleTime: 120_000,
    placeholderData: keepPreviousData,
  });
  const pending = debounced !== query.trim().slice(0, 100) || result.isFetching;
  return { ...result, searching: pending, debouncedQuery: debounced };
}

export function useConfluenceMappings(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<ConfluenceMapping[]>({
    queryKey: ['confluenceMappings', orgId],
    queryFn: () => auth!.client.listConfluenceMappings(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 60_000,
  });
}

export function useSaveConfluenceMapping(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      projectId,
      body,
    }: {
      projectId: string;
      body: ConfluenceMappingRequest;
    }) => auth!.client.saveConfluenceMapping(orgId!, projectId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['confluenceMappings', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['confluenceHealth', orgId] });
    },
  });
}

export function useDeleteConfluenceMapping(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (projectId: string) =>
      auth!.client.deleteConfluenceMapping(orgId!, projectId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['confluenceMappings', orgId] });
      void queryClient.invalidateQueries({ queryKey: ['confluenceHealth', orgId] });
    },
  });
}

export function useConfluenceHealth(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<ConfluenceHealth>({
    queryKey: ['confluenceHealth', orgId],
    queryFn: () => auth!.client.getConfluenceHealth(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 60_000,
  });
}

/** The five built-in templates (read-only) for pickers and mapping labels. */
export function useConfluenceTemplates(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<ConfluenceTemplate[]>({
    queryKey: ['confluenceTemplates', orgId],
    queryFn: () => auth!.client.listConfluenceTemplates(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 60_000,
  });
}
