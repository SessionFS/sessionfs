import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useChanges(sessionId: string) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['changes', sessionId],
    queryFn: () => auth!.client.getChanges(sessionId),
    enabled: !!auth && !!sessionId,
    staleTime: 60_000,
  });
}
