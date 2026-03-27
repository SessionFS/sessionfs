import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { JudgeSettings } from '../api/client';

export function useJudgeSettings() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['judgeSettings'],
    queryFn: () => auth!.client.getJudgeSettings(),
    enabled: !!auth,
    staleTime: 300_000,
    retry: (failureCount, error) => {
      if (error && 'status' in error && (error as { status: number }).status === 404) return false;
      return failureCount < 1;
    },
  });
}

export function useSaveJudgeSettings() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({ provider, model, apiKey, baseUrl }: { provider: string; model: string; apiKey: string; baseUrl?: string }) =>
      auth!.client.saveJudgeSettings(provider, model, apiKey, baseUrl),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['judgeSettings'] });
    },
  });
}

export function useClearJudgeSettings() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: () => auth!.client.clearJudgeSettings(),
    onSuccess: () => {
      queryClient.setQueryData<JudgeSettings>(['judgeSettings'], {
        provider: '',
        model: '',
        key_set: false,
        base_url: null,
      });
    },
  });
}
