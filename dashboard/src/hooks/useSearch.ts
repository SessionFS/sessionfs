import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export interface SearchMatch {
  snippet: string;
}

export interface SearchResult {
  session_id: string;
  title: string | null;
  source_tool: string;
  model_id: string | null;
  message_count: number;
  updated_at: string;
  matches: SearchMatch[];
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  page: number;
  page_size: number;
  query: string;
}

export function useSearch(query: string, options?: { tool?: string; days?: number; page?: number }) {
  const { auth } = useAuth();
  return useQuery<SearchResponse>({
    queryKey: ['search', query, options],
    queryFn: () => {
      const sp = new URLSearchParams();
      sp.set('q', query);
      if (options?.tool) sp.set('tool', options.tool);
      if (options?.days) sp.set('days', String(options.days));
      if (options?.page) sp.set('page', String(options.page));
      return auth!.client.search(sp);
    },
    enabled: !!auth && query.length >= 2,
    staleTime: 30_000,
  });
}
