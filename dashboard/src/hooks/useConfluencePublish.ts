/**
 * React-Query hooks for the project-scoped Confluence surface (Enterprise).
 * Preview and publish reuse the caller's knowledge:read / knowledge:write on
 * the project; the org's connection/mapping state is learned from the preview
 * POST's error codes, not from the admin-only org GETs.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type { ConfluenceLinksResponse, ConfluencePreview, ConfluencePublishRequest } from '../api/client';

/**
 * Draft for a wiki page (`kind: 'wiki_page'`, `slug` set) or the compiled
 * context document (`kind: 'context'`, `slug` undefined → context root). The
 * dialog only mounts the query once open; `enabled` is the open flag.
 */
export function useConfluencePreview(
  projectId: string | undefined,
  kind: 'wiki_page' | 'context',
  slug: string | undefined,
  enabled: boolean,
) {
  const { auth } = useAuth();
  return useQuery<ConfluencePreview>({
    queryKey: ['confluencePreview', projectId, kind, slug ?? null],
    queryFn: () => auth!.client.previewConfluence(projectId!, { kind, slug }),
    enabled: !!auth && !!projectId && enabled,
    // A draft is built from the live page/context each time and can carry a
    // re-publish diff, so never serve a stale preview.
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
}

export function usePublishConfluence(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ConfluencePublishRequest) =>
      auth!.client.publishConfluence(projectId!, body),
    onSuccess: (_data, body) => {
      void queryClient.removeQueries({
        queryKey: ['confluencePreview', projectId, body.kind, body.slug ?? null],
      });
      void queryClient.invalidateQueries({ queryKey: ['confluenceLinks', projectId] });
    },
  });
}

/**
 * The project's Confluence links plus its mapping. Only fetched when the
 * project's Confluence surface is Enterprise; a missing connection or mapping
 * comes back as a 409, which callers treat as "no Confluence here".
 */
export function useConfluenceLinks(projectId: string | undefined, enabled: boolean) {
  const { auth } = useAuth();
  return useQuery<ConfluenceLinksResponse>({
    queryKey: ['confluenceLinks', projectId],
    queryFn: () => auth!.client.listConfluenceLinks(projectId!),
    enabled: !!auth && !!projectId && enabled,
    staleTime: 30_000,
    retry: false,
  });
}

function useAfterLinkChange(projectId: string | undefined) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ['confluenceLinks', projectId] });
    // Re-run any open draft so it picks up the new link state.
    void queryClient.invalidateQueries({ queryKey: ['confluencePreview', projectId] });
  };
}

/** Resolve a drifted link: overwrite Confluence on the next publish, or unlink. */
export function useResolveConfluenceLink(projectId: string | undefined) {
  const { auth } = useAuth();
  const after = useAfterLinkChange(projectId);
  return useMutation({
    mutationFn: ({ linkId, action }: { linkId: string; action: 'overwrite' | 'unlink' }) =>
      auth!.client.resolveConfluenceLink(projectId!, linkId, { action }),
    onSuccess: after,
  });
}

/** Drop a source's Confluence link (the page itself stays in Confluence). */
export function useUnlinkConfluence(projectId: string | undefined) {
  const { auth } = useAuth();
  const after = useAfterLinkChange(projectId);
  return useMutation({
    mutationFn: (linkId: string) => auth!.client.unlinkConfluence(projectId!, linkId),
    onSuccess: after,
  });
}
