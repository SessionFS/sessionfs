import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useAuth } from '../auth/AuthContext';
import {
  ApiError,
  type ChatThreadCreate,
  type ConversationKindFilter,
  type MeetingActionItem,
  type MeetingCreate,
  type MeetingItemTicketRef,
  type MeetingLinkResult,
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
 *   POST   /api/v1/meetings/{id}/action-items/{item}/link[/preview]
 *   POST   /api/v1/meetings/{id}/action-items/{item}/{unlink|dismiss-suggestion}
 *   GET    /api/v1/meetings/{id}/linkable-tickets?q=
 */

const PAGE_SIZE = 50;

const listKey = (scope: MeetingScope | undefined) => ['meetings', scope?.kind, scope?.id];
const detailKey = (meetingId: string | undefined) => ['meeting', meetingId];

/** A miss that a retry won't fix (404 unknown/deleted, 403, 409 …). */
const retryServerErrors = (count: number, err: unknown) =>
  !(err instanceof ApiError && err.status < 500) && count < 2;

export function useMeetings(scope: MeetingScope | undefined, q: string, kind: ConversationKindFilter = 'meeting') {
  const { auth } = useAuth();
  return useInfiniteQuery<MeetingList>({
    queryKey: [...listKey(scope), q, kind],
    queryFn: ({ pageParam }) =>
      auth!.client.listMeetings(scope!, {
        q: q || undefined,
        kind,
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

export function useUploadChatThread(scope: MeetingScope | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ChatThreadCreate) => auth!.client.uploadChatThread(scope!, body),
    onSuccess: (thread) => {
      qc.setQueryData(detailKey(thread.id), thread);
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


/**
 * Tie a pending item to an existing ticket. The item's own response comes
 * back, and the ticket gains a comment, so its caches are refreshed too.
 */
export function useLinkMeetingItem(meetingId: string | undefined) {
  const { auth } = useAuth();
  return useMeetingWrite(
    meetingId,
    ({ itemId, ticketId }: { itemId: string; ticketId: string }) =>
      auth!.client.linkMeetingItem(meetingId!, itemId, ticketId),
    (prev, result: MeetingLinkResult) => withItem(prev, result.item),
    [['tickets'], ['ticket'], ['ticketComments'], ['ticketEvents']],
  );
}

export function useUnlinkMeetingItem(meetingId: string | undefined) {
  const { auth } = useAuth();
  return useMeetingWrite(meetingId, (itemId: string) => auth!.client.unlinkMeetingItem(meetingId!, itemId), withItem);
}

/** "Not this one": hide the item's suggested ticket. */
export function useDismissMeetingSuggestion(meetingId: string | undefined) {
  const { auth } = useAuth();
  return useMeetingWrite(
    meetingId,
    (itemId: string) => auth!.client.dismissMeetingSuggestion(meetingId!, itemId),
    withItem,
  );
}

/** What linking `itemId` to `ticketId` would add (fetched when one is picked). */
export function useLinkPreview(meetingId: string | undefined, itemId: string | undefined, ticketId: string | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['meetingLinkPreview', meetingId, itemId, ticketId ?? null],
    queryFn: () => auth!.client.previewMeetingLink(meetingId!, itemId!, ticketId),
    enabled: !!auth && !!meetingId && !!itemId && !!ticketId,
    gcTime: 0,
    staleTime: 0,
    retry: false,
  });
}

export const LINKABLE_SEARCH_DEBOUNCE_MS = 250;
const LINKABLE_PAGE_SIZE = 20;

/**
 * Tickets an item of this conversation can be linked to, searched on the
 * server by title or id prefix. Debounced; the previous page stays on screen
 * while the next one loads (`searching` covers both).
 */
export function useLinkableTickets(meetingId: string | undefined, query: string, enabled = true) {
  const { auth } = useAuth();
  const trimmed = query.trim().slice(0, 100);
  const debounced = useDebouncedValue(trimmed, LINKABLE_SEARCH_DEBOUNCE_MS);
  const result = useQuery<{ tickets: MeetingItemTicketRef[] }>({
    queryKey: ['meetingLinkableTickets', meetingId, debounced],
    queryFn: () => auth!.client.listLinkableTickets(meetingId!, { q: debounced || undefined, limit: LINKABLE_PAGE_SIZE }),
    enabled: !!auth && !!meetingId && enabled,
    staleTime: 15_000,
    placeholderData: keepPreviousData,
    retry: retryServerErrors,
  });
  const searching = debounced !== trimmed || result.isFetching;
  return { ...result, searching };
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}
