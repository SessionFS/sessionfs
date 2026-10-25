import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { useJiraPreview, usePublishJira } from './useJiraTickets';

/**
 * Hook-level behaviour for ticket Jira promotion. The publish mutation must
 * invalidate the ticket's comment thread (the server writes a Jira sync note)
 * in addition to the ticket/list/link/preview keys; the preview query must
 * never retry and never leave a stale draft cached after unmount.
 */

const mockUseAuth = vi.fn();
vi.mock('../auth/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const publishJira = vi.fn();
const previewJira = vi.fn();

function makeClient() {
  return {
    publishJira,
    previewJira,
  };
}

beforeEach(() => {
  mockUseAuth.mockReset();
  publishJira.mockReset();
  previewJira.mockReset();
  mockUseAuth.mockReturnValue({ auth: { apiKey: 'sk', baseUrl: 'http://test', client: makeClient() } });
});

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

describe('usePublishJira', () => {
  it('invalidates ticket/list/link/comments and drops (not refetches) the preview on success', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    const removeSpy = vi.spyOn(qc, 'removeQueries');
    publishJira.mockResolvedValue({ issue_key: 'KEY-1' });

    const { result } = renderHook(() => usePublishJira('proj_1'), {
      wrapper: makeWrapper(qc),
    });

    await result.current.mutateAsync({
      ticketId: 't_1',
      body: { summary: 's', sections: [], confirm: true },
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['ticketComments', 'proj_1', 't_1'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['ticket', 'proj_1', 't_1'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['tickets', 'proj_1'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraLink', 'proj_1', 't_1'] });
    // Refetching the preview would POST it again and may spend model tokens.
    expect(invalidateSpy).not.toHaveBeenCalledWith({ queryKey: ['jiraPreview', 'proj_1', 't_1'] });
    expect(removeSpy).toHaveBeenCalledWith({ queryKey: ['jiraPreview', 'proj_1', 't_1'] });
  });
});

describe('useJiraPreview', () => {
  it('disables retries and leaves no cached draft after unmount (gcTime 0)', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    previewJira.mockResolvedValue({ ticket_id: 't_1' });

    const { unmount } = renderHook(() => useJiraPreview('proj_1', 't_1', true), {
      wrapper: makeWrapper(qc),
    });
    await waitFor(() => expect(previewJira).toHaveBeenCalled());

    const query = qc.getQueryCache().find({ queryKey: ['jiraPreview', 'proj_1', 't_1'] });
    expect(query?.options.retry).toBe(false);
    expect(query?.options.gcTime).toBe(0);

    unmount();
    // With gcTime 0 the cache entry is dropped as soon as it has no observers.
    await waitFor(() => {
      expect(qc.getQueryCache().find({ queryKey: ['jiraPreview', 'proj_1', 't_1'] })).toBeUndefined();
    });
  });
});
