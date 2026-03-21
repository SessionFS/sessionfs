import { useQuery } from '@tanstack/react-query';
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
