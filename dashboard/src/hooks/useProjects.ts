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

export function useCompileProject(projectId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => auth!.client.compileProject(projectId!),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledgeEntries', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['compilations', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
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
