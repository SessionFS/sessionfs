import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useAdminUsers(params: { page?: number; page_size?: number; search?: string } = {}) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['admin', 'users', params],
    queryFn: () => auth!.client.adminListUsers(params),
    enabled: !!auth,
    staleTime: 15_000,
  });
}

export function useAdminStats() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['admin', 'stats'],
    queryFn: () => auth!.client.adminGetStats(),
    enabled: !!auth,
    staleTime: 30_000,
  });
}

export function useAdminActionLog(params: { page?: number; page_size?: number } = {}) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['admin', 'actions', params],
    queryFn: () => auth!.client.adminGetActionLog(params),
    enabled: !!auth,
    staleTime: 15_000,
  });
}

export function useAdminChangeTier() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, tier }: { userId: string; tier: string }) =>
      auth!.client.adminChangeTier(userId, tier),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin'] });
    },
  });
}

export function useAdminVerifyUser() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ userId }: { userId: string }) =>
      auth!.client.adminVerifyUser(userId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin'] });
    },
  });
}

export function useAdminDeleteUser() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ userId }: { userId: string }) =>
      auth!.client.adminDeleteUser(userId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin'] });
    },
  });
}
