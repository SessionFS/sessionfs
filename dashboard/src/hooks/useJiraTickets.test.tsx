import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { ApiError, type Ticket } from '../api/client';
import { useJiraPreview, useLinkJiraIssue, usePublishJira, usePullJira } from './useJiraTickets';

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
const linkJiraIssue = vi.fn();
const pullJira = vi.fn();

function makeClient() {
  return {
    publishJira,
    previewJira,
    linkJiraIssue,
    pullJira,
  };
}

beforeEach(() => {
  mockUseAuth.mockReset();
  publishJira.mockReset();
  previewJira.mockReset();
  linkJiraIssue.mockReset();
  pullJira.mockReset();
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

    const query = qc.getQueryCache().find({ queryKey: ['jiraPreview', 'proj_1', 't_1', null] });
    expect(query?.options.retry).toBe(false);
    expect(query?.options.gcTime).toBe(0);

    unmount();
    // With gcTime 0 the cache entry is dropped as soon as it has no observers.
    await waitFor(() => {
      expect(qc.getQueryCache().find({ queryKey: ['jiraPreview', 'proj_1', 't_1', null] })).toBeUndefined();
    });
  });
});

describe('useLinkJiraIssue', () => {
  const LINK = {
    ticket_id: 't_1',
    provider: 'jira',
    issue_key: 'ABC-123',
    issue_url: 'https://acme.atlassian.net/browse/ABC-123',
    last_pushed_at: null,
    last_known_status: 'To Do',
    last_status_checked_at: null,
    payload_hash: null,
    comment_sync_outbound: false,
    can_send_to_jira: false,
  };

  it('posts the key, seeds the link + ticket caches and refreshes the ticket views', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    qc.setQueryData(['ticket', 'proj_1', 't_1'], { id: 't_1', title: 'Fix login', jira: null } as unknown as Ticket);
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    linkJiraIssue.mockResolvedValue(LINK);

    const { result } = renderHook(() => useLinkJiraIssue('proj_1'), { wrapper: makeWrapper(qc) });
    await result.current.mutateAsync({ ticketId: 't_1', issueKey: 'ABC-123' });

    expect(linkJiraIssue).toHaveBeenCalledWith('proj_1', 't_1', 'ABC-123');
    // The section switches to its linked state before the refetch lands.
    expect(qc.getQueryData(['jiraLink', 'proj_1', 't_1'])).toEqual(LINK);
    expect((qc.getQueryData(['ticket', 'proj_1', 't_1']) as Ticket).jira).toEqual({
      issue_key: 'ABC-123',
      issue_url: 'https://acme.atlassian.net/browse/ABC-123',
      last_known_status: 'To Do',
    });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['ticket', 'proj_1', 't_1'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['tickets', 'proj_1'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['jiraLink', 'proj_1', 't_1'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['ticketComments', 'proj_1', 't_1'] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['ticketJiraSyncEvents', 'proj_1', 't_1'] });
  });

  it('refreshes the ticket when it turns out to be linked already', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    linkJiraIssue.mockRejectedValue(
      new ApiError(
        409,
        JSON.stringify({ error: { code: '409', message: 'Conflict', details: { error: 'jira_already_linked' } } }),
      ),
    );

    const { result } = renderHook(() => useLinkJiraIssue('proj_1'), { wrapper: makeWrapper(qc) });
    await expect(result.current.mutateAsync({ ticketId: 't_1', issueKey: 'ABC-123' })).rejects.toBeInstanceOf(ApiError);

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['ticket', 'proj_1', 't_1'] });
    expect(qc.getQueryData(['jiraLink', 'proj_1', 't_1'])).toBeUndefined();
  });
});

describe('usePullJira', () => {
  it('merges the pulled ticket into the cached one, refreshes the timeline and drops any draft', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const cached = {
      id: 't_1',
      title: 'Old title',
      description: 'Old text',
      jira: { issue_key: 'ABC-1', issue_url: 'https://acme.atlassian.net/browse/ABC-1', last_known_status: 'To Do' },
      child_ticket_ids: ['t_2'],
      jira_publish_in_progress: false,
      jira_publish_unknown: false,
    } as unknown as Ticket;
    qc.setQueryData(['ticket', 'proj_1', 't_1'], cached);
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries');
    const removeSpy = vi.spyOn(qc, 'removeQueries');
    // A pull response without the read-only fields a ticket GET carries.
    const ticket = { id: 't_1', title: 'From Jira', description: 'Jira text' } as unknown as Ticket;
    const jira = { ticket_id: 't_1', issue_key: 'ABC-1', attached_not_published: true };
    pullJira.mockResolvedValue({ ticket, jira, updated_fields: ['title', 'description'] });

    const { result } = renderHook(() => usePullJira('proj_1'), { wrapper: makeWrapper(qc) });
    await result.current.mutateAsync('t_1');

    expect(pullJira).toHaveBeenCalledWith('proj_1', 't_1');
    // The new text lands, and the ticket never looks unlinked or childless.
    expect(qc.getQueryData(['ticket', 'proj_1', 't_1'])).toEqual({
      ...cached,
      title: 'From Jira',
      description: 'Jira text',
    });
    expect(qc.getQueryData(['jiraLink', 'proj_1', 't_1'])).toEqual(jira);
    for (const key of [
      ['ticket', 'proj_1', 't_1'],
      ['tickets', 'proj_1'],
      ['jiraLink', 'proj_1', 't_1'],
      ['ticketComments', 'proj_1', 't_1'],
      ['ticketJiraSyncEvents', 'proj_1', 't_1'],
    ]) {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: key });
    }
    expect(removeSpy).toHaveBeenCalledWith({ queryKey: ['jiraPreview', 'proj_1', 't_1'] });
  });

  it('does not seed a partial ticket when none is cached', async () => {
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    pullJira.mockResolvedValue({ ticket: { id: 't_1', title: 'x' }, jira: { issue_key: 'ABC-1' }, updated_fields: [] });
    const { result } = renderHook(() => usePullJira('proj_1'), { wrapper: makeWrapper(qc) });
    await result.current.mutateAsync('t_1');
    expect(qc.getQueryData(['ticket', 'proj_1', 't_1'])).toBeUndefined();
  });
});
