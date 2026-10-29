import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import {
  ApiError,
  type MeetingActionItem,
  type MeetingCreate,
  type MeetingDetail,
  type MeetingList,
  type MeetingParticipant,
  type MeetingPromoteRequest,
  type MeetingScope,
} from '../api/client';

/**
 * React-Query hooks for meeting transcripts (GitHub #116).
 *
 *   GET    /api/v1/{projects|groups}/{id}/meetings?q=&before=&limit=
 *   POST   /api/v1/{projects|groups}/{id}/meetings
 *   GET    /api/v1/meetings/{id}            DELETE /api/v1/meetings/{id}
 *   POST   /api/v1/meetings/{id}/extract
 *   PUT    /api/v1/meetings/{id}/participants/{pid}
 *   POST   /api/v1/meetings/{id}/action-items/{item}/promote[/preview]
 *   POST   /api/v1/meetings/{id}/action-items/{item}/{dismiss|restore}
 */

const PAGE_SIZE = 50;

const listKey = (scope: MeetingScope | undefined) => ['meetings', scope?.kind, scope?.id];
const detailKey = (meetingId: string | undefined) => ['meeting', meetingId];

/** A miss that a retry won't fix (404 unknown/deleted, 403, 409 …). */
const retryServerErrors = (count: number, err: unknown) =>
  !(err instanceof ApiError && err.status < 500) && count < 2;

export function useMeetings(scope: MeetingScope | undefined, q: string) {
  const { auth } = useAuth();
  return useInfiniteQuery<MeetingList>({
    queryKey: [...listKey(scope), q],
    queryFn: ({ pageParam }) =>
      auth!.client.listMeetings(scope!, {
        q: q || undefined,
        before: (pageParam as string | undefined) || undefined,
        limit: PAGE_SIZE,
      }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!auth && !!scope?.id,
    staleTime: 15_000,
    retry: retryServerErrors,
  });
}

export function useMeeting(meetingId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<MeetingDetail>({
    queryKey: detailKey(meetingId),
    queryFn: () => auth!.client.getMeeting(meetingId!),
    enabled: !!auth && !!meetingId,
    staleTime: 15_000,
    retry: retryServerErrors,
  });
}

export function useUploadMeeting(scope: MeetingScope | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: MeetingCreate) => auth!.client.uploadMeeting(scope!, body),
    onSuccess: (meeting) => {
      qc.setQueryData(detailKey(meeting.id), meeting);
      void qc.invalidateQueries({ queryKey: listKey(scope) });
    },
  });
}

/** Writes on one meeting refresh its detail and every list it may sit in. */
function useMeetingWrite<TVars, TResult>(
  meetingId: string | undefined,
  fn: (vars: TVars) => Promise<TResult>,
  apply?: (prev: MeetingDetail, result: TResult) => MeetingDetail,
  alsoInvalidate: readonly (readonly unknown[])[] = [],
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (result) => {
      for (const key of alsoInvalidate) void qc.invalidateQueries({ queryKey: [...key] });
      if (apply) {
        qc.setQueryData<MeetingDetail>(detailKey(meetingId), (prev) => (prev ? apply(prev, result) : prev));
      }
      void qc.invalidateQueries({ queryKey: detailKey(meetingId) });
      void qc.invalidateQueries({ queryKey: ['meetings'] });
    },
  });
}

const withItem = (prev: MeetingDetail, item: MeetingActionItem): MeetingDetail => ({
  ...prev,
  action_items: prev.action_items.map((i) => (i.id === item.id ? item : i)),
});

export function useExtractMeeting(meetingId: string | undefined) {
  const { auth } = useAuth();
  return useMeetingWrite(
    meetingId,
    () => auth!.client.extractMeeting(meetingId!),
    (_prev, result) => result,
  );
}

export function useDeleteMeeting(meetingId: string | undefined) {
  const { auth } = useAuth();
  return useMeetingWrite(meetingId, () => auth!.client.deleteMeeting(meetingId!));
}

export function useResolveParticipant(meetingId: string | undefined) {
  const { auth } = useAuth();
  return useMeetingWrite(
    meetingId,
    ({ participantId, body }: { participantId: string; body: { user_id: string } | { persona_name: string } }) =>
      auth!.client.resolveMeetingParticipant(meetingId!, participantId, body),
    (prev, p: MeetingParticipant) => ({
      ...prev,
      participants: prev.participants.map((x) => (x.id === p.id ? p : x)),
    }),
  );
}

export function useDismissMeetingItem(meetingId: string | undefined) {
  const { auth } = useAuth();
  return useMeetingWrite(meetingId, (itemId: string) => auth!.client.dismissMeetingItem(meetingId!, itemId), withItem);
}

export function useRestoreMeetingItem(meetingId: string | undefined) {
  const { auth } = useAuth();
  return useMeetingWrite(meetingId, (itemId: string) => auth!.client.restoreMeetingItem(meetingId!, itemId), withItem);
}

/**
 * The ticket draft for one action item; fetched when the dialog opens. A
 * group meeting re-asks with the chosen `targetProjectId` to learn whether
 * Jira publishing is available there (`enabled` off until one is chosen).
 */
export function usePromotePreview(
  meetingId: string | undefined,
  itemId: string | undefined,
  targetProjectId?: string,
  enabled = true,
) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['meetingPromotePreview', meetingId, itemId, targetProjectId ?? null],
    queryFn: () => auth!.client.previewMeetingPromote(meetingId!, itemId!, targetProjectId),
    enabled: !!auth && !!meetingId && !!itemId && enabled,
    // A draft, not shared state: always build a fresh one.
    gcTime: 0,
    staleTime: 0,
    retry: false,
  });
}

export function usePromoteMeetingItem(meetingId: string | undefined) {
  const { auth } = useAuth();
  return useMeetingWrite(
    meetingId,
    ({ itemId, body }: { itemId: string; body: MeetingPromoteRequest }) =>
      auth!.client.promoteMeetingItem(meetingId!, itemId, body),
    (prev, result) => ({
      ...prev,
      action_items: prev.action_items.map((i) =>
        i.id === result.item_id ? { ...i, status: 'promoted', promoted_ticket_id: result.ticket_id } : i,
      ),
    }),
    // The new ticket shows up in the target project's ticket list.
    [['tickets']],
  );
}
