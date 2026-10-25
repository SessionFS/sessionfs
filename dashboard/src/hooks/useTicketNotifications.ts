import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

/**
 * React-Query hooks for the me-scoped ticket notification inbox and the
 * per-user email preference. The unread count polls with an ETag (60s) so the
 * header bell stays fresh without re-fetching a stable payload.
 */

export function useTicketNotifications(
  params: { unread?: boolean; limit?: number } = {},
) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['ticket-notifications', 'list', params],
    queryFn: () => auth!.client.listTicketNotifications(params),
    enabled: !!auth,
  });
}

export function useTicketNotificationUnreadCount() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useQuery<{ count: number; etag: string }>({
    queryKey: ['ticket-notifications', 'unread-count'],
    queryFn: async () => {
      const prev = qc.getQueryData<{ count: number; etag: string }>([
        'ticket-notifications',
        'unread-count',
      ]);
      const res = await auth!.client.getTicketNotificationUnreadCount(prev?.etag);
      if (res.notModified) return { count: prev?.count ?? 0, etag: res.etag };
      return { count: res.count, etag: res.etag };
    },
    enabled: !!auth,
    refetchInterval: 60_000,
    staleTime: 0,
  });
}

export function useMarkTicketNotificationsRead() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { ids?: number[]; all?: boolean }) =>
      auth!.client.markTicketNotificationsRead(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['ticket-notifications'] });
    },
  });
}

export function useNotificationSettings() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['notification-settings'],
    queryFn: () => auth!.client.getNotificationSettings(),
    enabled: !!auth,
  });
}

export function useUpdateNotificationSettings() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { ticket_email: string }) =>
      auth!.client.putNotificationSettings(body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['notification-settings'] });
    },
  });
}
