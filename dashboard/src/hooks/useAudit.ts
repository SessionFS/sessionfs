import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

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

// `useRunAudit` was removed in v0.9.9.9 — it was an unused parallel
// polling owner that competed with BackgroundTasksProvider for the
// same audit lifecycle. The toast-driven flow in
// components/BackgroundTasks.tsx is the single owner of run+poll.
