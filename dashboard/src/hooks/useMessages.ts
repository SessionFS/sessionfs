import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

/**
 * One page of a session's messages. With `around` (a messages.jsonl line
 * number) the server picks the page that holds that message and `page` is
 * ignored — read the page actually returned from `data.page`.
 */
export function useMessages(
  sessionId: string,
  page: number,
  pageSize = 50,
  order: 'oldest' | 'newest' = 'oldest',
  around?: number,
) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: around != null
      ? ['messages', sessionId, 'around', around, pageSize, order]
      : ['messages', sessionId, page, pageSize, order],
    queryFn: () => auth!.client.getMessages(sessionId, page, pageSize, order, around),
    enabled: !!auth && !!sessionId,
    // A deep link whose message wasn't found (`hidden`/`missing`) is asked
    // again next time instead of being served from cache: the session may
    // have synced the message since.
    staleTime: (query) => {
      const target = (query.state.data as { target?: { status?: string } | null } | undefined)?.target;
      return around != null && target && target.status !== 'found' ? 0 : 60_000;
    },
  });
}
