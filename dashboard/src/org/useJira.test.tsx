import { renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { useDeleteJiraConnection, useSaveJiraConnection } from './useJira';

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

beforeEach(() => {
  mockUseAuth.mockReset();
  saveJiraConnection.mockReset();
  deleteJiraConnection.mockReset();
  mockUseAuth.mockReturnValue({
    auth: {
      apiKey: 'sk',
      baseUrl: 'http://test',
      client: { saveJiraConnection, deleteJiraConnection },
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
