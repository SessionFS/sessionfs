/**
 * Shared fixtures for the project-group suites. Each suite mocks
 * `../../auth/AuthContext` and `../../hooks/useToast` with the objects
 * exported here, so components run against real react-query hooks with a
 * fake API client and a stubbed fetch (for the hooks that call fetch).
 */

import type { ReactElement } from 'react';
import { render } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { vi } from 'vitest';
import { ApiError, type ProjectGroupDetail, type ProjectGroupSummary } from '../../api/client';

export const ORG = 'org_1';
export const ME = { user_id: 'u_me', email: 'me@example.test', org_id: ORG };

export const summary = (extra: Partial<ProjectGroupSummary> = {}): ProjectGroupSummary => ({
  id: 'pg_1',
  org_id: ORG,
  slug: 'payments',
  name: 'Payments',
  description: 'Money things',
  member_count: 2,
  project_count: 1,
  my_role: 'admin',
  ...extra,
});

export const detail = (extra: Partial<ProjectGroupDetail> = {}): ProjectGroupDetail => ({
  id: 'pg_1',
  org_id: ORG,
  slug: 'payments',
  name: 'Payments',
  description: 'Money things',
  my_role: 'admin',
  members: [
    { user_id: 'u_peer', email: 'peer@example.test', role: 'editor', granted_by_user_id: 'u_me', created_at: null },
  ],
  projects: ['proj_a'],
  ...extra,
});

export const projects = [
  { id: 'proj_a', name: 'Alpha', git_remote_normalized: 'acme/alpha', org_id: ORG, can_edit: true },
  { id: 'proj_b', name: 'Beta', git_remote_normalized: 'acme/beta', org_id: ORG, can_edit: true },
  { id: 'proj_c', name: 'Gamma (read only)', git_remote_normalized: 'acme/gamma', org_id: ORG, can_edit: false },
  { id: 'proj_p', name: 'Personal', git_remote_normalized: 'me/personal', org_id: null, can_edit: true },
];

export const orgMembers = [
  { user_id: 'u_me', email: 'me@example.test', display_name: null, role: 'admin', joined_at: null },
  { user_id: 'u_peer', email: 'peer@example.test', display_name: 'Peer', role: 'member', joined_at: null },
  { user_id: 'u_new', email: 'new@example.test', display_name: 'New Person', role: 'member', joined_at: null },
];

/** The fake API client behind `useAuth().auth.client`. */
export function makeClient() {
  return {
    getMe: vi.fn().mockResolvedValue(ME),
    listProjects: vi.fn().mockResolvedValue(projects),
    listOrgGroups: vi.fn().mockResolvedValue([summary()]),
    createGroup: vi.fn().mockResolvedValue(detail({ id: 'pg_new', name: 'New' })),
    getGroup: vi.fn().mockResolvedValue(detail()),
    updateGroup: vi.fn().mockResolvedValue(detail()),
    deleteGroup: vi.fn().mockResolvedValue(undefined),
    grantGroupMember: vi.fn().mockResolvedValue(detail()),
    revokeGroupMember: vi.fn().mockResolvedValue(detail()),
    addGroupProject: vi.fn().mockResolvedValue(detail()),
    removeGroupProject: vi.fn().mockResolvedValue(detail()),
    listProjectGroups: vi.fn().mockResolvedValue([]),
    listMyGroups: vi.fn().mockResolvedValue([summary()]),
  };
}

export const authState: { client: ReturnType<typeof makeClient> } = { client: makeClient() };
export const toast = { addToast: vi.fn(), removeToast: vi.fn(), toasts: [] };

/** `vi.mock('../../auth/AuthContext', () => authModule)` — reads the current fake client. */
export const authModule = {
  useAuth: () => ({
    auth: { apiKey: 'sk_test_fake', baseUrl: 'http://test.api', client: authState.client },
  }),
};
export const toastModule = { useToast: () => toast };

export function apiError(status: number, code: string, message = 'error', details?: Record<string, unknown>) {
  return new ApiError(status, JSON.stringify({ error: { code, message, details } }));
}

type FetchRoutes = Record<string, unknown | ((init?: RequestInit) => unknown)>;

/**
 * Stub fetch for the fetch-based hooks (org members, /org, org settings,
 * my orgs). Keys are path suffixes; a function value receives the init so a
 * PUT can be asserted. Return `{ __status, body }` to answer with an error.
 */
export function stubFetch(routes: FetchRoutes = {}) {
  const defaults: FetchRoutes = {
    '/api/v1/org': { org: { id: ORG, name: 'Acme', slug: 'acme', tier: 'team' }, current_user_role: 'admin' },
    [`/api/v1/orgs/${ORG}/members`]: { org_id: ORG, members: orgMembers, current_user_role: 'admin' },
    '/api/v1/orgs': { orgs: [{ org_id: ORG, name: 'Acme', role: 'admin' }] },
  };
  const all = { ...defaults, ...routes };
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url, 'http://test.api').pathname;
    const hit = Object.keys(all)
      .sort((a, b) => b.length - a.length)
      .find((k) => path === k);
    let value = hit ? all[hit] : {};
    if (typeof value === 'function') value = (value as (i?: RequestInit) => unknown)(init);
    const v = value as { __status?: number; body?: unknown };
    const status = v && typeof v === 'object' && '__status' in v ? v.__status! : 200;
    const body = status === 200 ? value : v.body;
    return {
      ok: status < 400,
      status,
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

export function renderWith(ui: ReactElement, { route = '/', path = '*' }: { route?: string; path?: string } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path={path} element={ui} />
          {path !== '*' && <Route path="*" element={<p>elsewhere</p>} />}
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, qc };
}
