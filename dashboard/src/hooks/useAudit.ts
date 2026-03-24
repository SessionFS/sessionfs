import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { AuditReport } from '../api/client';

export function useAudit(sessionId: string) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['audit', sessionId],
    queryFn: () => auth!.client.getAudit(sessionId),
    enabled: !!auth && !!sessionId,
    staleTime: 300_000,
    retry: (failureCount, error) => {
      // Don't retry 404s — just means no audit exists yet
      if (error && 'status' in error && (error as { status: number }).status === 404) return false;
      return failureCount < 1;
    },
  });
}

export function useRunAudit() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: ({
      sessionId,
      model,
      llmApiKey,
      provider,
    }: {
      sessionId: string;
      model: string;
      llmApiKey: string;
      provider?: string;
    }): Promise<AuditReport> => {
      return auth!.client.runAudit(sessionId, model, llmApiKey, provider);
    },
    onSuccess: (data) => {
      queryClient.setQueryData(['audit', data.session_id], data);
    },
  });
}
