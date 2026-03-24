import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useMe() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['me'],
    queryFn: () => auth!.client.getMe(),
    enabled: !!auth,
    staleTime: 60_000,
  });
}
