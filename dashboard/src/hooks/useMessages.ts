import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useMessages(sessionId: string, page: number, pageSize = 50) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['messages', sessionId, page, pageSize],
    queryFn: () => auth!.client.getMessages(sessionId, page, pageSize),
    enabled: !!auth && !!sessionId,
    staleTime: 60_000,
  });
}
