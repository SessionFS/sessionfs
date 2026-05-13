/**
 * React-Query hooks for v0.10.0 multi-org member management.
 *
 * Targets the new `/api/v1/orgs/{org_id}/members*` surface from
 * Phase 3a (KB entry 253). The existing single-org hooks in
 * `OrgPage.tsx` target the legacy `/api/v1/org/*` shape; both
 * surfaces enforce the same CEO data-stays invariants because the
 * legacy routes delegate to the same backend service.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';

export interface OrgMemberInfo {
  user_id: string;
  email: string;
  display_name: string | null;
  role: 'admin' | 'member';
  joined_at: string | null;
}

export interface OrgMembersListResponse {
  org_id: string;
  members: OrgMemberInfo[];
  seats_used: number;
  seats_limit: number;
  current_user_role: 'admin' | 'member' | null;
}

export interface RemoveMemberResponse {
  removed: string;
  projects_transferred: number;
  pending_transfers_cancelled: number;
}

function useApiBase(): { base: string; headers: { Authorization: string } } {
  const { auth } = useAuth();
  const base = auth?.baseUrl || (window as { __SFS_API_URL__?: string }).__SFS_API_URL__ || '';
  const headers = { Authorization: `Bearer ${auth?.apiKey ?? ''}` };
  return { base, headers };
}

export function useOrgMembers(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  return useQuery<OrgMembersListResponse>({
    queryKey: ['orgMembers', orgId],
    queryFn: async () => {
      const resp = await fetch(`${base}/api/v1/orgs/${orgId}/members`, { headers });
      if (!resp.ok) {
        throw new Error(`Failed to load members: ${resp.status}`);
      }
      return resp.json();
    },
    enabled: !!orgId,
    staleTime: 30_000,
  });
}

export function useInviteMember(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { email: string; role: 'admin' | 'member' }) => {
      const resp = await fetch(`${base}/api/v1/orgs/${orgId}/members/invite`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Invite failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['orgMembers', orgId] });
    },
  });
}

export function useChangeMemberRole(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: { userId: string; role: 'admin' | 'member' }) => {
      const resp = await fetch(
        `${base}/api/v1/orgs/${orgId}/members/${body.userId}/role`,
        {
          method: 'PUT',
          headers: { ...headers, 'Content-Type': 'application/json' },
          body: JSON.stringify({ role: body.role }),
        },
      );
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Role change failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['orgMembers', orgId] });
    },
  });
}

export function useRemoveMember(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const queryClient = useQueryClient();
  return useMutation<RemoveMemberResponse, Error, { userId: string }>({
    mutationFn: async (body) => {
      const resp = await fetch(
        `${base}/api/v1/orgs/${orgId}/members/${body.userId}`,
        { method: 'DELETE', headers },
      );
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Remove failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['orgMembers', orgId] });
    },
  });
}
