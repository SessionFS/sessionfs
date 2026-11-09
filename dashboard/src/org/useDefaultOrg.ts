/**
 * Switch the signed-in user's default organization.
 *
 * A person can belong to several orgs. Pages and commands that don't name
 * an org (the Organization page, billing, `sfs project init`, …) use the
 * default org, so switching it is how the Organization page changes which
 * org it shows. PUT /api/v1/auth/me/default-org refuses an org the user
 * isn't a member of.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';

export function useSetDefaultOrg() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  const base =
    auth?.baseUrl || (window as { __SFS_API_URL__?: string }).__SFS_API_URL__ || '';
  return useMutation<{ default_org_id: string | null }, Error, { orgId: string }>({
    mutationFn: async ({ orgId }) => {
      const resp = await fetch(`${base}/api/v1/auth/me/default-org`, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${auth?.apiKey ?? ''}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ org_id: orgId }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        throw new Error(body?.error?.message || body?.detail || `Could not switch organization (${resp.status})`);
      }
      return resp.json();
    },
    onSuccess: () => {
      // Everything that follows the default org (the Organization page, the
      // sidebar chip, billing, tier gates) has to reload.
      qc.invalidateQueries();
    },
  });
}
