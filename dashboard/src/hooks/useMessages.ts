import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useMessages(sessionId: string, page: number, pageSize = 50, order: 'oldest' | 'newest' = 'oldest') {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['messages', sessionId, page, pageSize, order],
    queryFn: () => auth!.client.getMessages(sessionId, page, pageSize, order),
    enabled: !!auth && !!sessionId,
    staleTime: 60_000,
  });
}
