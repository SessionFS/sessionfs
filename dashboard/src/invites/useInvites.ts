/**
 * v0.10.22 — Org-invite acceptance hooks (tk_6afbcfefe5804c1d).
 *
 * Targets the routes added in the backend half of this ticket:
 *   GET  /api/v1/org/invites/me
 *   POST /api/v1/org/invite/{invite_id}/accept
 *   POST /api/v1/org/invite/{invite_id}/decline
 *
 * Cache keys:
 *   ['org-invites', 'me'] — the logged-in user's pending invites list.
 *   Every mutation invalidates that key so the InvitesPage row + banner
 *   count both refresh after Accept/Decline.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';

export interface PendingInvite {
  invite_id: string;
  org_id: string;
  org_name: string;
  role: string;
  invited_by_email: string;
  created_at: string;
  expires_at: string;
}

export interface MyInvitesResponse {
  invites: PendingInvite[];
}

function useApiBase(): { base: string; headers: { Authorization: string } } {
  const { auth } = useAuth();
  const base =
    auth?.baseUrl || (window as { __SFS_API_URL__?: string }).__SFS_API_URL__ || '';
  const headers = { Authorization: `Bearer ${auth?.apiKey ?? ''}` };
  return { base, headers };
}

export function useMyInvites() {
  const { base, headers } = useApiBase();
  return useQuery<MyInvitesResponse>({
    queryKey: ['org-invites', 'me'],
    queryFn: async () => {
      const resp = await fetch(`${base}/api/v1/org/invites/me`, { headers });
      if (!resp.ok) {
        throw new Error(`Failed to load invites: ${resp.status}`);
      }
      return resp.json();
    },
    staleTime: 30_000,
  });
}

export function useAcceptInvite() {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<{ org_id: string; role: string }, Error, { inviteId: string }>({
    mutationFn: async ({ inviteId }) => {
      const resp = await fetch(`${base}/api/v1/org/invite/${inviteId}/accept`, {
        method: 'POST',
        headers,
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        const msg =
          body?.error?.message || body?.detail || `Accept failed: ${resp.status}`;
        throw new Error(msg);
      }
      return resp.json();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['org-invites'] });
      // The accept side-effect lands an OrgMember row, so the user's
      // org membership list also changes — invalidate that too so the
      // org settings page refreshes after acceptance.
      qc.invalidateQueries({ queryKey: ['my-orgs'] });
    },
  });
}

export function useDeclineInvite() {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<
    { invite_id: string; declined_at: string },
    Error,
    { inviteId: string; reason?: string }
  >({
    mutationFn: async ({ inviteId, reason }) => {
      const resp = await fetch(`${base}/api/v1/org/invite/${inviteId}/decline`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: reason || null }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        const msg =
          body?.error?.message || body?.detail || `Decline failed: ${resp.status}`;
        throw new Error(msg);
      }
      return resp.json();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['org-invites'] });
    },
  });
}
