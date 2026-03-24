import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useHandoffInbox() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['handoffs', 'inbox'],
    queryFn: () => auth!.client.listInbox(),
    enabled: !!auth,
    staleTime: 30_000,
  });
}

export function useHandoffSent() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['handoffs', 'sent'],
    queryFn: () => auth!.client.listSent(),
    enabled: !!auth,
    staleTime: 30_000,
  });
}

export function useHandoff(id: string) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['handoff', id],
    queryFn: () => auth!.client.getHandoff(id),
    enabled: !!auth && !!id,
    staleTime: 60_000,
  });
}

export function useCreateHandoff() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      sessionId,
      recipientEmail,
      message,
    }: {
      sessionId: string;
      recipientEmail: string;
      message?: string;
    }) => auth!.client.createHandoff(sessionId, recipientEmail, message),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['handoffs'] });
    },
  });
}
