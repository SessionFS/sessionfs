import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { AttentionFeed, CollaborationRemindRequest } from '../api/client';

/** React-Query hooks for For You (/api/v1/me/for-you). */

export const FOR_YOU_KEY = ['for-you'] as const;
const FEED_KEY = [...FOR_YOU_KEY, 'feed'] as const;

interface FeedState {
  feed: AttentionFeed;
  etag: string;
}

/**
 * The ranked feed. Polls every 60 s while the tab is visible (react-query
 * pauses intervals in background tabs) and sends the last ETag, so an
 * unchanged feed is a 304 that keeps the cached body.
 */
export function useForYouFeed() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useQuery<FeedState>({
    queryKey: FEED_KEY,
    queryFn: async () => {
      const prev = qc.getQueryData<FeedState>(FEED_KEY);
      const res = await auth!.client.getForYou(prev?.etag);
      if (res.notModified && prev) return prev;
      if (!res.data) {
        // A 304 without a cached body (cache was cleared): fetch it fresh.
        const fresh = await auth!.client.getForYou();
        return { feed: fresh.data as AttentionFeed, etag: fresh.etag };
      }
      return { feed: res.data, etag: res.etag };
    },
    enabled: !!auth,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: 0,
  });
}

/** Sidebar badge: items in On your radar, Waiting on you and Your team is asking. */
export function useForYouCount() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [...FOR_YOU_KEY, 'count'],
    queryFn: () => auth!.client.getForYouCount(),
    enabled: !!auth,
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: false,
  });
}

export function useForYouContext(itemKey: string, enabled: boolean) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [...FOR_YOU_KEY, 'context', itemKey],
    queryFn: () => auth!.client.getForYouItemContext(itemKey),
    enabled: !!auth && enabled,
    staleTime: 60_000,
    retry: false,
  });
}

export function useInvalidateForYou() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: FOR_YOU_KEY });
    void qc.invalidateQueries({ queryKey: ['reminders'] });
  };
}

export function useRemindCollaborator() {
  const { auth } = useAuth();
  const invalidate = useInvalidateForYou();
  return useMutation({
    mutationFn: (body: CollaborationRemindRequest) => auth!.client.remindCollaborator(body),
    onSuccess: invalidate,
  });
}
