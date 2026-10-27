import { renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import {
  useCreateJiraTemplate,
  useDeleteJiraConnection,
  useDeleteJiraTemplate,
  useJiraTemplateVersions,
  useRotateJiraWebhookSecret,
  useSaveJiraConnection,
  useUpdateJiraSync,
  useUpdateJiraTemplate,
} from './useJira';

/**
 * Org-scoped Jira hooks. Connecting or disconnecting changes the remote
 * project/issue-type catalogs, so both mutations must invalidate
 * ['jiraProjects', orgId] and ['jiraIssueTypes', orgId] in addition to the
 * connection/health/mapping keys.
 */

const mockUseAuth = vi.fn();
vi.mock('../auth/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const saveJiraConnection = vi.fn();
const deleteJiraConnection = vi.fn();
const createJiraTemplate = vi.fn();
const updateJiraTemplate = vi.fn();
const deleteJiraTemplate = vi.fn();
const listJiraTemplateVersions = vi.fn();
const rotateJiraWebhookSecret = vi.fn();
const updateJiraSync = vi.fn();

beforeEach(() => {
  mockUseAuth.mockReset();
  saveJiraConnection.mockReset();
  deleteJiraConnection.mockReset();
  createJiraTemplate.mockReset();
  updateJiraTemplate.mockReset();
  deleteJiraTemplate.mockReset();
  listJiraTemplateVersions.mockReset();
  rotateJiraWebhookSecret.mockReset();
  updateJiraSync.mockReset();
  mockUseAuth.mockReturnValue({
    auth: {
      apiKey: 'sk',
      baseUrl: 'http://test',
      client: {
        saveJiraConnection,
        deleteJiraConnection,
        createJiraTemplate,
        updateJiraTemplate,
        deleteJiraTemplate,
        listJiraTemplateVersions,
        rotateJiraWebhookSecret,
        updateJiraSync,
      },
    },
  });
});

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

describe('useSaveJiraConnection', () => {
  it('invalidates the project and issue-type catalogs on success', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    saveJiraConnection.mockResolvedValue({ connected: true, has_token: true });

    const { result } = renderHook(() => useSaveJiraConnection('org_x'), {
      wrapper: makeWrapper(qc),
    });
    await result.current.mutateAsync({
      site_url: 'https://acme.atlassian.net',
      account_email: 'jira@acme.com',
      api_token: 'secret',
      default_template: 'agile',
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraProjects', 'org_x'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraIssueTypes', 'org_x'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraConnection', 'org_x'] });
  });
});

describe('useDeleteJiraConnection', () => {
  it('invalidates the project and issue-type catalogs on success', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    deleteJiraConnection.mockResolvedValue(undefined);

    const { result } = renderHook(() => useDeleteJiraConnection('org_x'), {
      wrapper: makeWrapper(qc),
    });
    await result.current.mutateAsync();

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraProjects', 'org_x'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraIssueTypes', 'org_x'] });
  });
});

describe('useRotateJiraWebhookSecret', () => {
  it('returns the one-time secret and invalidates the connection', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    rotateJiraWebhookSecret.mockResolvedValue({
      webhook_secret: 'whsec_one',
      webhook_secret_last4: 'one',
    });

    const { result } = renderHook(() => useRotateJiraWebhookSecret('org_x'), {
      wrapper: makeWrapper(qc),
    });
    const secret = await result.current.mutateAsync();

    expect(secret).toEqual({ webhook_secret: 'whsec_one', webhook_secret_last4: 'one' });
    expect(rotateJiraWebhookSecret).toHaveBeenCalledWith('org_x');
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraConnection', 'org_x'] });
  });
});

describe('Jira template hooks', () => {
  it('create invalidates the template list', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    createJiraTemplate.mockResolvedValue({});

    const { result } = renderHook(() => useCreateJiraTemplate('org_x'), {
      wrapper: makeWrapper(qc),
    });
    await result.current.mutateAsync({ slug: 'design-lite', name: 'Design Lite', sections: [] });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraTemplates', 'org_x'] });
  });

  it('update invalidates both the list and that template\'s versions', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    updateJiraTemplate.mockResolvedValue({});

    const { result } = renderHook(() => useUpdateJiraTemplate('org_x'), {
      wrapper: makeWrapper(qc),
    });
    await result.current.mutateAsync({
      slug: 'design-lite',
      body: { slug: 'design-lite', name: 'Design Lite', sections: [] },
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraTemplates', 'org_x'] });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ['jiraTemplateVersions', 'org_x', 'design-lite'],
    });
  });

  it('delete invalidates the template list', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    deleteJiraTemplate.mockResolvedValue(undefined);

    const { result } = renderHook(() => useDeleteJiraTemplate('org_x'), {
      wrapper: makeWrapper(qc),
    });
    await result.current.mutateAsync('design-lite');

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraTemplates', 'org_x'] });
  });

  it('versions query calls the client with the slug', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    listJiraTemplateVersions.mockResolvedValue([]);

    const { result } = renderHook(() => useJiraTemplateVersions('org_x', 'design-lite'), {
      wrapper: makeWrapper(qc),
    });
    await vi.waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(listJiraTemplateVersions).toHaveBeenCalledWith('org_x', 'design-lite');
  });
});

describe('useUpdateJiraSync', () => {
  it('PATCHes only the given fields and refreshes the connection', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    const saved = { connected: true, has_token: true, sync_status: true };
    updateJiraSync.mockResolvedValue(saved);

    const { result } = renderHook(() => useUpdateJiraSync('org_x'), {
      wrapper: makeWrapper(qc),
    });
    await result.current.mutateAsync({ sync_status: true });

    expect(updateJiraSync).toHaveBeenCalledWith('org_x', { sync_status: true });
    expect(saveJiraConnection).not.toHaveBeenCalled();
    expect(qc.getQueryData(['jiraConnection', 'org_x'])).toEqual(saved);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraConnection', 'org_x'] });
  });
});
