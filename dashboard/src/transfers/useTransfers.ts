/**
 * React-Query hooks for v0.10.0 Phase 4 project transfer surface.
 *
 * Targets the Phase 2 routes (KB entry 246):
 *   POST /api/v1/projects/{id}/transfer        initiate
 *   POST /api/v1/transfers/{xfer_id}/accept
 *   POST /api/v1/transfers/{xfer_id}/reject
 *   POST /api/v1/transfers/{xfer_id}/cancel
 *   GET  /api/v1/transfers?direction=&state=
 *
 * Cache keys:
 *   ['transfers', direction, state] — list query.
 *   Every mutation invalidates ['transfers'] (both directions, all states)
 *   because state transitions move rows between filtered subsets.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';

export interface TransferInfo {
  id: string;
  project_id: string | null;
  project_git_remote_snapshot: string | null;
  project_name_snapshot: string | null;
  initiated_by: string;
  target_user_id: string | null;
  from_scope: string;
  to_scope: string;
  state: 'pending' | 'accepted' | 'rejected' | 'cancelled';
  accepted_by: string | null;
  created_at: string;
  accepted_at: string | null;
  updated_at: string;
}

export interface TransferListResponse {
  transfers: TransferInfo[];
}

export type TransferDirection = 'incoming' | 'outgoing';

function useApiBase(): { base: string; headers: { Authorization: string } } {
  const { auth } = useAuth();
  const base =
    auth?.baseUrl || (window as { __SFS_API_URL__?: string }).__SFS_API_URL__ || '';
  const headers = { Authorization: `Bearer ${auth?.apiKey ?? ''}` };
  return { base, headers };
}

export function useTransfers(
  direction: TransferDirection,
  state?: TransferInfo['state'],
) {
  const { base, headers } = useApiBase();
  return useQuery<TransferListResponse>({
    queryKey: ['transfers', direction, state ?? null],
    queryFn: async () => {
      const sp = new URLSearchParams({ direction });
      if (state) sp.set('state', state);
      const resp = await fetch(`${base}/api/v1/transfers?${sp}`, { headers });
      if (!resp.ok) {
        throw new Error(`Failed to load transfers: ${resp.status}`);
      }
      return resp.json();
    },
    staleTime: 30_000,
  });
}

export function useInitiateTransfer(projectId: string | undefined) {
  const { base, headers } = useApiBase();
  const queryClient = useQueryClient();
  return useMutation<TransferInfo, Error, { to: string }>({
    mutationFn: async (body) => {
      const resp = await fetch(`${base}/api/v1/projects/${projectId}/transfer`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Transfer initiate failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['transfers'] });
      // Auto-accept moves `project.org_id` server-side; ProjectDetail
      // reads currentScope from the project cache, so it MUST be
      // refetched or the panel keeps showing the pre-transfer scope and
      // destination set. Codex Phase 4 Round 3 (KB entry 280).
      void queryClient.invalidateQueries({ queryKey: ['project'] });
    },
  });
}

function useTransferStateMutation(action: 'accept' | 'reject' | 'cancel') {
  const { base, headers } = useApiBase();
  const queryClient = useQueryClient();
  return useMutation<TransferInfo, Error, { transferId: string }>({
    mutationFn: async ({ transferId }) => {
      const resp = await fetch(`${base}/api/v1/transfers/${transferId}/${action}`, {
        method: 'POST',
        headers,
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Transfer ${action} failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['transfers'] });
      // accept (and only accept) flips project.org_id server-side, but
      // we invalidate on every state transition for safety and
      // simplicity. The project cache is small and only the project
      // page mounts it; a stale page after reject/cancel would be
      // confusing too. Codex Phase 4 Round 3 (KB entry 280).
      void queryClient.invalidateQueries({ queryKey: ['project'] });
    },
  });
}

export function useAcceptTransfer() {
  return useTransferStateMutation('accept');
}

export function useRejectTransfer() {
  return useTransferStateMutation('reject');
}

export function useCancelTransfer() {
  return useTransferStateMutation('cancel');
}

export interface MyOrgEntry {
  org_id: string;
  name: string;
  role?: string;
}

/**
 * Returns the user's full set of org memberships.
 *
 * Hits the multi-org listing endpoint `GET /api/v1/orgs` (added in
 * Phase 4 Round 3, KB entry 278). Returns every org the user belongs
 * to so TransferPanel's destination dropdown isn't artificially
 * truncated to a single primary org.
 */
export function useMyOrgs() {
  const { base, headers } = useApiBase();
  return useQuery<MyOrgEntry[]>({
    queryKey: ['my-orgs'],
    queryFn: async () => {
      const resp = await fetch(`${base}/api/v1/orgs`, { headers });
      if (!resp.ok) {
        if (resp.status === 404) return [];
        throw new Error(`Failed to load orgs: ${resp.status}`);
      }
      const data = await resp.json();
      return Array.isArray(data?.orgs) ? data.orgs : [];
    },
    staleTime: 60_000,
  });
}
