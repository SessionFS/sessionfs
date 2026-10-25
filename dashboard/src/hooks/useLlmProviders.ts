import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useLlmProviders() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['llmProviders'],
    queryFn: () => auth!.client.getLlmProviders(),
    enabled: !!auth,
    staleTime: 300_000,
    retry: (failureCount) => failureCount < 1,
  });
}
