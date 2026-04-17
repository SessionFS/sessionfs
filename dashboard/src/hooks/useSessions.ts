import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useSessions(params: {
  page?: number;
  page_size?: number;
  source_tool?: string;
  tag?: string;
}) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['sessions', params],
    queryFn: () => auth!.client.listSessions(params),
    enabled: !!auth,
    staleTime: 30_000,
  });
}

export function useDeletedSessions() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['sessions', 'deleted'],
    queryFn: () => auth!.client.listDeletedSessions(),
    enabled: !!auth,
    staleTime: 30_000,
  });
}

export function useDeleteSession() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, scope }: { id: string; scope: 'cloud' | 'everywhere' }) =>
      auth!.client.deleteSession(id, scope),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] });
    },
  });
}

export function useRestoreSession() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => auth!.client.restoreSession(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] });
    },
  });
}
