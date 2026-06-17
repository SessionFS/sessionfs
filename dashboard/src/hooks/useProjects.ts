import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useProjects() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['projects'],
    queryFn: () => auth!.client.listProjects(),
    enabled: !!auth,
    staleTime: 30_000,
  });
}

export function useProject(remote: string) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['project', remote],
    queryFn: () => auth!.client.getProject(remote),
    enabled: !!auth && !!remote,
    staleTime: 60_000,
  });
}

export function useCreateProject() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: { name: string; git_remote_normalized: string }) =>
      auth!.client.createProject(data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useUpdateProjectContext() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ remote, doc }: { remote: string; doc: string }) =>
      auth!.client.updateProjectContext(remote, doc),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      void queryClient.invalidateQueries({ queryKey: ['project', variables.remote] });
    },
  });
}

export function useDeleteProject() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => auth!.client.deleteProject(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      void queryClient.invalidateQueries({ queryKey: ['project'] });
    },
  });
}

export function useKnowledgeEntries(
  projectId: string | undefined,
  params: { pending?: boolean; type?: string; limit?: number } = {},
) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['knowledgeEntries', projectId, params],
    queryFn: () => auth!.client.listKnowledgeEntries(projectId!, params),
    enabled: !!auth && !!projectId,
    staleTime: 15_000,
  });
}

export function useDismissEntry(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (entryId: number) =>
      auth!.client.dismissEntry(projectId!, entryId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledgeEntries', projectId] });
    },
  });
}

export function useDismissStaleEntries(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => auth!.client.dismissStaleEntries(projectId!),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledgeEntries', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projectHealth', projectId] });
    },
  });
}

export function useCompileProject(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => auth!.client.compileProject(projectId!),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledgeEntries', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['compilations', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      void queryClient.invalidateQueries({ queryKey: ['project'] });
      void queryClient.invalidateQueries({ queryKey: ['wikiPages', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['wikiPage', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projectHealth', projectId] });
    },
  });
}

export function useCompilations(projectId: string | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['compilations', projectId],
    queryFn: () => auth!.client.listCompilations(projectId!),
    enabled: !!auth && !!projectId,
    staleTime: 30_000,
  });
}

export function useProjectHealth(projectId: string | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['projectHealth', projectId],
    queryFn: () => auth!.client.getProjectHealth(projectId!),
    enabled: !!auth && !!projectId,
    staleTime: 60_000,
  });
}

export function useWikiPages(projectId: string | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['wikiPages', projectId],
    queryFn: () => auth!.client.listWikiPages(projectId!),
    enabled: !!auth && !!projectId,
    staleTime: 15_000,
  });
}

export function useWikiPage(projectId: string | undefined, slug: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['wikiPage', projectId, slug],
    queryFn: () => auth!.client.getWikiPage(projectId!, slug!),
    enabled: !!auth && !!projectId && !!slug,
    staleTime: 30_000,
  });
}

export function useUpdateWikiPage(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ slug, content, title }: { slug: string; content: string; title?: string }) =>
      auth!.client.updateWikiPage(projectId!, slug, content, title),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ['wikiPages', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['wikiPage', projectId, variables.slug] });
    },
  });
}

export function useDeleteWikiPage(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => auth!.client.deleteWikiPage(projectId!, slug),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['wikiPages', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['wikiPage', projectId] });
    },
  });
}

export function useRegenerateWikiPage(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => auth!.client.regenerateWikiPage(projectId!, slug),
    onSuccess: (_data, slug) => {
      void queryClient.invalidateQueries({ queryKey: ['wikiPages', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['wikiPage', projectId, slug] });
    },
  });
}

export function usePromoteEntry(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (entryId: number) =>
      auth!.client.promoteEntry(projectId!, entryId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledgeEntries', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projectHealth', projectId] });
    },
  });
}

export function useSupersedeEntry(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ entryId, superseding_id, reason }: { entryId: number; superseding_id: number; reason: string }) =>
      auth!.client.supersedeEntry(projectId!, entryId, { superseding_id, reason }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledgeEntries', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projectHealth', projectId] });
    },
  });
}

export function useRebuildProject(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => auth!.client.rebuildProject(projectId!),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledgeEntries', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projectHealth', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['compilations', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      void queryClient.invalidateQueries({ queryKey: ['project'] });
    },
  });
}

export function useRefreshEntry(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (entryId: number) =>
      auth!.client.refreshEntry(projectId!, entryId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledgeEntries', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projectHealth', projectId] });
    },
  });
}

export function useUpdateProjectSettings(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (settings: { auto_narrative?: boolean }) =>
      auth!.client.updateProjectSettings(projectId!, settings),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      void queryClient.invalidateQueries({ queryKey: ['project'] });
    },
  });
}

// ── Multi-Repo Projects (v0.11) ──

export function useProjectRepos(projectId: string | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['projectRepos', projectId],
    queryFn: () => auth!.client.listProjectRepos(projectId!),
    enabled: !!auth && !!projectId,
    staleTime: 30_000,
  });
}

export function useLinkRepo(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { git_remote: string; is_primary?: boolean }) =>
      auth!.client.linkRepo(projectId!, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projectRepos', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      void queryClient.invalidateQueries({ queryKey: ['project'] });
    },
  });
}

export function useUnlinkRepo(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (repoId: string) =>
      auth!.client.unlinkRepo(projectId!, repoId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projectRepos', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      void queryClient.invalidateQueries({ queryKey: ['project'] });
    },
  });
}

export function useMergeProject(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { source_project_id: string; dry_run: boolean }) =>
      auth!.client.mergeProject(projectId!, body) as Promise<import('../api/client').MergeDryRunResponse | import('../api/client').MergeExecuteResponse>,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projectRepos', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      void queryClient.invalidateQueries({ queryKey: ['project'] });
    },
  });
}

export function useUpdateProjectName(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      auth!.client.updateProject(projectId!, { name }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
      void queryClient.invalidateQueries({ queryKey: ['project'] });
    },
  });
}
