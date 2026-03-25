import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { AuditReport } from '../api/client';
import { useState, useEffect, useRef } from 'react';

export function useAudit(sessionId: string) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['audit', sessionId],
    queryFn: () => auth!.client.getAudit(sessionId),
    enabled: !!auth && !!sessionId,
    staleTime: 300_000,
    retry: (failureCount, error) => {
      if (error && 'status' in error && (error as { status: number }).status === 404) return false;
      return failureCount < 1;
    },
  });
}

export function useRunAudit() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  const [polling, setPolling] = useState(false);
  const [pollingSessionId, setPollingSessionId] = useState('');
  const intervalRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  // Poll for background audit completion
  useEffect(() => {
    if (!polling || !pollingSessionId || !auth) return;

    intervalRef.current = setInterval(async () => {
      try {
        const report = await auth.client.getAudit(pollingSessionId);
        if (report && report.session_id) {
          // Audit completed
          queryClient.setQueryData(['audit', pollingSessionId], report);
          setPolling(false);
          setPollingSessionId('');
        }
      } catch {
        // Still running or not found — keep polling
      }
    }, 5000);

    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [polling, pollingSessionId, auth, queryClient]);

  const mutation = useMutation({
    mutationFn: async ({
      sessionId,
      model,
      llmApiKey,
      provider,
    }: {
      sessionId: string;
      model: string;
      llmApiKey: string;
      provider?: string;
    }) => {
      const resp = await auth!.client.runAudit(sessionId, model, llmApiKey, provider);
      return resp;
    },
    onSuccess: (data: AuditReport | { status: string; session_id: string }) => {
      if ('status' in data && data.status === 'accepted') {
        // Background audit — start polling
        setPollingSessionId(data.session_id);
        setPolling(true);
      } else if ('findings' in data) {
        // Synchronous audit — done immediately
        queryClient.setQueryData(['audit', (data as AuditReport).session_id], data);
      }
    },
  });

  return { ...mutation, isPolling: polling };
}
