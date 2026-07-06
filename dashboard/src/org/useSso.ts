/**
 * React-Query hooks for v0.13.x SSO (OIDC) org-level admin configuration.
 *
 * Targets the live backend API at /api/v1/orgs/{org_id}/sso/...:
 *   Provider CRUD:  POST/GET/PATCH/DELETE /provider
 *   Domains:        POST/GET/DELETE /domains, POST /domains/{id}/verify
 *   Enforcement:    PATCH /provider/enforcement
 *   Break-glass:    POST/GET/DELETE /break-glass
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';

/* ── Response types ── */

export interface SsoProviderResponse {
  id: string;
  org_id: string;
  protocol: string;
  display_name: string;
  issuer: string;
  client_id: string;
  client_secret_ref: string;
  allowed_scopes: string[];
  enabled: boolean;
  enforced: boolean;
  created_at: string;
  updated_at: string;
}

export interface SsoDomainResponse {
  id: string;
  domain: string;
  method: string;
  verification_token: string;
  status: string;
  txt_record: string;
  verified_at: string | null;
}

export interface BreakGlassGrantResponse {
  id: string;
  admin_user_id: string;
  expires_at: string;
  revoked_at: string | null;
  created_at: string;
}

/* ── Shared helpers ── */

function useApiBase(): { base: string; headers: { Authorization: string } } {
  const { auth } = useAuth();
  const base =
    auth?.baseUrl || (window as { __SFS_API_URL__?: string }).__SFS_API_URL__ || '';
  const headers = { Authorization: `Bearer ${auth?.apiKey ?? ''}` };
  return { base, headers };
}

function ssoPath(orgId: string, suffix: string) {
  return `/api/v1/orgs/${orgId}/sso${suffix}`;
}

/* ── Provider hooks ── */

export function useSsoProvider(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  return useQuery<SsoProviderResponse | null>({
    queryKey: ['sso-provider', orgId],
    queryFn: async () => {
      const resp = await fetch(`${base}${ssoPath(orgId!, '/provider')}`, { headers });
      if (resp.status === 404) return null;
      if (!resp.ok) throw new Error(`Failed to load SSO provider: ${resp.status}`);
      return resp.json();
    },
    enabled: !!orgId,
    staleTime: 60_000,
  });
}

export function useCreateProvider(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<SsoProviderResponse, Error, Partial<SsoProviderResponse>>({
    mutationFn: async (body) => {
      const resp = await fetch(`${base}${ssoPath(orgId!, '/provider')}`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Create provider failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sso-provider', orgId] });
    },
  });
}

export function useUpdateProvider(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<SsoProviderResponse, Error, Partial<SsoProviderResponse>>({
    mutationFn: async (body) => {
      const resp = await fetch(`${base}${ssoPath(orgId!, '/provider')}`, {
        method: 'PATCH',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Update provider failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sso-provider', orgId] });
      void qc.invalidateQueries({ queryKey: ['sso-domains', orgId] });
    },
  });
}

export function useDeleteProvider(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: async () => {
      const resp = await fetch(`${base}${ssoPath(orgId!, '/provider')}`, {
        method: 'DELETE',
        headers,
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Delete provider failed: ${resp.status}`);
      }
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sso-provider', orgId] });
      void qc.invalidateQueries({ queryKey: ['sso-domains', orgId] });
    },
  });
}

/* ── Domain hooks ── */

export function useDomains(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  return useQuery<SsoDomainResponse[]>({
    queryKey: ['sso-domains', orgId],
    queryFn: async () => {
      const resp = await fetch(`${base}${ssoPath(orgId!, '/domains')}`, { headers });
      if (!resp.ok) throw new Error(`Failed to load domains: ${resp.status}`);
      return resp.json();
    },
    enabled: !!orgId,
    staleTime: 30_000,
  });
}

export function useCreateDomain(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<SsoDomainResponse, Error, { domain: string }>({
    mutationFn: async (body) => {
      const resp = await fetch(`${base}${ssoPath(orgId!, '/domains')}`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Add domain failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sso-domains', orgId] });
    },
  });
}

export function useVerifyDomain(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<SsoDomainResponse, Error, string>({
    mutationFn: async (domainId) => {
      const resp = await fetch(`${base}${ssoPath(orgId!, `/domains/${domainId}/verify`)}`, {
        method: 'POST',
        headers,
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Verify domain failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sso-domains', orgId] });
    },
  });
}

export function useDeleteDomain(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: async (domainId) => {
      const resp = await fetch(`${base}${ssoPath(orgId!, `/domains/${domainId}`)}`, {
        method: 'DELETE',
        headers,
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Delete domain failed: ${resp.status}`);
      }
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sso-domains', orgId] });
    },
  });
}

/* ── Enforcement hook ── */

export function useToggleEnforcement(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<SsoProviderResponse, Error, boolean>({
    mutationFn: async (enforced) => {
      const resp = await fetch(`${base}${ssoPath(orgId!, '/provider/enforcement')}`, {
        method: 'PATCH',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ enforced }),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Toggle enforcement failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sso-provider', orgId] });
    },
  });
}

/* ── Break-glass hooks ── */

export function useBreakGlassGrants(orgId: string | undefined, enabled = true) {
  const { base, headers } = useApiBase();
  return useQuery<BreakGlassGrantResponse[]>({
    queryKey: ['sso-break-glass', orgId],
    queryFn: async () => {
      const resp = await fetch(`${base}${ssoPath(orgId!, '/break-glass')}`, { headers });
      if (!resp.ok) throw new Error(`Failed to load break-glass grants: ${resp.status}`);
      return resp.json();
    },
    // Break-glass is owner-only — don't even fetch/cache it for non-owners.
    enabled: !!orgId && enabled,
    staleTime: 30_000,
  });
}

export function useCreateBreakGlass(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<BreakGlassGrantResponse, Error, { admin_user_id: string }>({
    mutationFn: async (body) => {
      const resp = await fetch(`${base}${ssoPath(orgId!, '/break-glass')}`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Create break-glass grant failed: ${resp.status}`);
      }
      return resp.json();
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sso-break-glass', orgId] });
    },
  });
}

export function useRevokeBreakGlass(orgId: string | undefined) {
  const { base, headers } = useApiBase();
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: async (grantId) => {
      const resp = await fetch(`${base}${ssoPath(orgId!, `/break-glass/${grantId}`)}`, {
        method: 'DELETE',
        headers,
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(text || `Revoke break-glass failed: ${resp.status}`);
      }
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['sso-break-glass', orgId] });
    },
  });
}
