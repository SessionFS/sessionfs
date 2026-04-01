import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { CreateLicenseRequest } from '../api/client';

export function useAdminLicenses(status?: string) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['admin', 'licenses', status],
    queryFn: () => auth!.client.adminListLicenses(status),
    enabled: !!auth,
    staleTime: 15_000,
  });
}

export function useCreateLicense() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateLicenseRequest) =>
      auth!.client.adminCreateLicense(data),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin', 'licenses'] });
    },
  });
}

export function useExtendLicense() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, days }: { key: string; days: number }) =>
      auth!.client.adminExtendLicense(key, days),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin', 'licenses'] });
    },
  });
}

export function useRevokeLicense() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, reason }: { key: string; reason: string }) =>
      auth!.client.adminRevokeLicense(key, reason),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin', 'licenses'] });
    },
  });
}

export function useLicenseHistory(key: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['admin', 'licenses', 'history', key],
    queryFn: () => auth!.client.adminGetLicenseHistory(key!),
    enabled: !!auth && !!key,
    staleTime: 15_000,
  });
}
