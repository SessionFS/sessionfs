import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type {
  MeetingItemReminderRequest,
  MeetingScope,
  ReminderCreate,
  ReminderPatch,
  ReminderStatus,
  SnoozeRequest,
} from '../api/client';

/** React-Query hooks for /api/v1/me/reminders and the per-source strips. */

const ROOT = 'reminders';

export function useReminders(status: ReminderStatus | 'all') {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [ROOT, 'list', status],
    queryFn: () => auth!.client.listReminders({ status, limit: 100 }),
    enabled: !!auth,
    staleTime: 15_000,
  });
}

export function useReminder(id: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [ROOT, 'one', id],
    queryFn: () => auth!.client.getReminder(id!),
    enabled: !!auth && !!id,
    retry: false,
  });
}

export function useReminderNotes(id: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [ROOT, 'notes', id],
    queryFn: () => auth!.client.listReminderNotes(id!),
    enabled: !!auth && !!id,
  });
}

/** Open reminders that are due now; backs the sidebar badge (polled lightly). */
export function useRemindersDueCount() {
  const { auth } = useAuth();
  return useQuery<{ count: number }>({
    queryKey: [ROOT, 'due-count'],
    queryFn: async () => {
      const res = await auth!.client.listReminders({
        status: 'open',
        due_before: new Date().toISOString(),
        limit: 100,
      });
      return { count: res.items.filter((r) => r.is_due).length };
    },
    enabled: !!auth,
    refetchInterval: 60_000,
    staleTime: 0,
  });
}

export function useSourceReminders(
  kind: 'meeting' | 'ticket',
  projectId: string | undefined,
  sourceId: string | undefined,
) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [ROOT, 'source', kind, projectId, sourceId],
    queryFn: () =>
      kind === 'meeting'
        ? auth!.client.listMeetingReminders(projectId!, sourceId!)
        : auth!.client.listTicketReminders(projectId!, sourceId!),
    enabled: !!auth && !!projectId && !!sourceId,
    staleTime: 15_000,
    retry: false,
  });
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: [ROOT] });
    void qc.invalidateQueries({ queryKey: ['ticket-notifications'] });
  };
}

export function useCreateReminder() {
  const { auth } = useAuth();
  const done = useInvalidate();
  return useMutation({
    mutationFn: (body: ReminderCreate) => auth!.client.createReminder(body),
    onSuccess: done,
  });
}

export function usePatchReminder(id: string) {
  const { auth } = useAuth();
  const done = useInvalidate();
  return useMutation({
    mutationFn: (body: ReminderPatch) => auth!.client.patchReminder(id, body),
    onSuccess: done,
  });
}

export function useSnoozeReminder(id: string) {
  const { auth } = useAuth();
  const done = useInvalidate();
  return useMutation({
    mutationFn: (body: SnoozeRequest) => auth!.client.snoozeReminder(id, body),
    onSuccess: done,
  });
}

export function useDeleteReminder(id: string) {
  const { auth } = useAuth();
  const done = useInvalidate();
  return useMutation({
    mutationFn: () => auth!.client.deleteReminder(id),
    onSuccess: done,
  });
}

export function useAddReminderNote(id: string) {
  const { auth } = useAuth();
  const done = useInvalidate();
  return useMutation({
    mutationFn: (body: string) => auth!.client.addReminderNote(id, body),
    onSuccess: done,
  });
}

export function useCreateMeetingItemReminder(scope: MeetingScope, meetingId: string) {
  const { auth } = useAuth();
  const done = useInvalidate();
  return useMutation({
    mutationFn: (v: { itemId: string; body: MeetingItemReminderRequest }) =>
      auth!.client.createMeetingItemReminder(scope, meetingId, v.itemId, v.body),
    onSuccess: done,
  });
}
