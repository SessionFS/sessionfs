import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../../auth/AuthContext';

export interface PrimaryOrgInfo {
  org: { id: string; name: string; slug: string; tier: string } | null;
  current_user_role?: string | null;
}

/**
 * The caller's primary org (`GET /api/v1/org`) — the only place the dashboard
 * learns an org's plan tier. Shares the `['org-info']` cache entry with the
 * Organization page.
 */
export function usePrimaryOrgInfo() {
  const { auth } = useAuth();
  const base = auth?.baseUrl || (window as { __SFS_API_URL__?: string }).__SFS_API_URL__ || '';
  return useQuery<PrimaryOrgInfo>({
    queryKey: ['org-info'],
    queryFn: async () => {
      const res = await fetch(`${base}/api/v1/org`, {
        headers: { Authorization: `Bearer ${auth?.apiKey ?? ''}` },
      });
      if (!res.ok) throw new Error('Failed to load org');
      return res.json();
    },
    enabled: !!auth,
  });
}
