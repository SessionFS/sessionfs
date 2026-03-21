import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useSession(id: string) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['session', id],
    queryFn: () => auth!.client.getSession(id),
    enabled: !!auth && !!id,
    staleTime: 60_000,
  });
}
