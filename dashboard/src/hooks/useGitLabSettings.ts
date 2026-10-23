import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { GitLabSettingsRequest } from '../api/client';

const KEY = ['gitlab-settings'];

export function useGitLabSettings() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: KEY,
    queryFn: () => auth!.client.getGitLabSettings(),
    enabled: !!auth,
  });
}

export function useSaveGitLabSettings() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: GitLabSettingsRequest) => auth!.client.saveGitLabSettings(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEY }),
  });
}

export function useDisconnectGitLab() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => auth!.client.deleteGitLabSettings(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: KEY }),
  });
}
