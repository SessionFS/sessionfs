/**
 * React-Query hooks for the per-admin weekly-digest opt-out (GitHub #97).
 *
 * Targets GET/PUT /api/v1/orgs/{org_id}/members/me/digest. Owner/admin only:
 * the server returns 403 for plain members, so the hooks are gated on
 * `enabled` by the caller.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';

export interface OrgDigestPrefs {
  org_id: string;
  opt_out: boolean;
}

function useApiBase(): { base: string; headers: { Authorization: string } } {
  const { auth } = useAuth();
  const base =
    auth?.baseUrl || (window as { __SFS_API_URL__?: string }).__SFS_API_URL__ || '';
  const headers = { Authorization: `Bearer ${auth?.apiKey ?? ''}` };
  return { base, headers };
}

export function useOrgDigestPrefs(orgId: string | undefined, enabled: boolean) {
  const { base, headers } = useApiBase();
  return useQuery<OrgDigestPrefs>({
    queryKey: ['orgDigestPrefs', orgId],
    queryFn: async () => {
      const resp = await fetch(`${base}/api/v1/orgs/${orgId}/members/me/digest`, { headers });
      if (!resp.ok) throw new Error(`Failed to load digest preferences: ${resp.status}`);
      return resp.json();
    },
    enabled: !!orgId && enabled,
    staleTime: 60_000,
  });
}

export function useUpdateOrgDigestPrefs(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const queryClient = useQueryClient();
  return useMutation<OrgDigestPrefs, Error, boolean>({
    mutationFn: async (optOut) => {
      const resp = await fetch(`${base}/api/v1/orgs/${orgId}/members/me/digest`, {
        method: 'PUT',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ opt_out: optOut }),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Digest preference update failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['orgDigestPrefs', orgId] });
    },
  });
}
