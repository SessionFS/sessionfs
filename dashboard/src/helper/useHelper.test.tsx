/**
 * The helper status/conversation queries are gated on the drawer being open
 * (enabled: !!auth && open), so merely mounting the panel closed never hits
 * the helper API.
 */
import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import type { HelperStatus } from '../api/client';

const { useAuthMock } = vi.hoisted(() => ({ useAuthMock: vi.fn() }));
vi.mock('../auth/AuthContext', () => ({ useAuth: useAuthMock }));

import { useHelperConversations, useHelperStatus } from './useHelper';

function makeWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

describe('helper query gating', () => {
  const getHelperStatus = vi.fn();
  const listHelperConversations = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    getHelperStatus.mockResolvedValue({
      enabled: true,
      key_source: 'user',
      provider: 'anthropic',
      model: 'claude',
    });
    listHelperConversations.mockResolvedValue([]);
    useAuthMock.mockReturnValue({
      auth: { client: { getHelperStatus, listHelperConversations } },
    });
  });

  it('does not fetch status or conversations while the drawer is closed', () => {
    const qc = new QueryClient();
    renderHook(() => {
      useHelperStatus(false);
      useHelperConversations(false);
    }, { wrapper: makeWrapper(qc) });
    expect(getHelperStatus).not.toHaveBeenCalled();
    expect(listHelperConversations).not.toHaveBeenCalled();
  });

  it('fetches status and conversations once the drawer opens', async () => {
    const qc = new QueryClient();
    const { rerender } = renderHook(
      ({ open }: { open: boolean }) => {
        useHelperStatus(open);
        useHelperConversations(open);
      },
      { wrapper: makeWrapper(qc), initialProps: { open: false } },
    );
    expect(getHelperStatus).not.toHaveBeenCalled();

    rerender({ open: true });
    await waitFor(() => {
      expect(getHelperStatus).toHaveBeenCalled();
      expect(listHelperConversations).toHaveBeenCalled();
    });
  });

  it('passes the session scope to getHelperStatus', async () => {
    const qc = new QueryClient();
    renderHook(() => useHelperStatus(true, { session_ids: ['ses_1', 'ses_2'] }), {
      wrapper: makeWrapper(qc),
    });
    await waitFor(() => {
      expect(getHelperStatus).toHaveBeenCalledWith(
        expect.objectContaining({ session_ids: ['ses_1', 'ses_2'] }),
      );
    });
  });

  it('keying on the scope refetches when the scope changes', async () => {
    const qc = new QueryClient();
    type ScopeProps = { scope?: { project_id?: string; session_ids?: string[] } };
    const { rerender } = renderHook<unknown, ScopeProps>(
      ({ scope }) => useHelperStatus(true, scope),
      { wrapper: makeWrapper(qc), initialProps: { scope: { project_id: 'proj_1' } } },
    );
    await waitFor(() => {
      expect(getHelperStatus).toHaveBeenCalledWith(expect.objectContaining({ project_id: 'proj_1' }));
    });

    rerender({ scope: { session_ids: ['ses_1'] } });
    await waitFor(() => {
      expect(getHelperStatus).toHaveBeenCalledWith(expect.objectContaining({ session_ids: ['ses_1'] }));
    });
  });

  it('keeps the previous scope status as placeholder data while a switch is in flight', async () => {
    const qc = new QueryClient();
    getHelperStatus.mockReset();
    let resolveSecond: (value: HelperStatus) => void = () => {};
    getHelperStatus
      .mockResolvedValueOnce({ enabled: true, key_source: 'user', provider: 'anthropic', model: 'claude' })
      .mockReturnValueOnce(new Promise<HelperStatus>((resolve) => { resolveSecond = resolve; }));

    type Props = { scope?: { project_id?: string; session_ids?: string[] } };
    const { result, rerender } = renderHook<ReturnType<typeof useHelperStatus>, Props>(
      ({ scope }) => useHelperStatus(true, scope),
      { wrapper: makeWrapper(qc), initialProps: { scope: { project_id: 'proj_1' } } },
    );

    await waitFor(() => expect(result.current.data?.enabled).toBe(true));

    rerender({ scope: { session_ids: ['ses_1'] } });
    // While the new scope's status is still loading, the previous status stays
    // as placeholder data (so consumers keep their body/turns mounted) instead
    // of dropping to `data === undefined` and flashing a skeleton.
    await waitFor(() => {
      expect(result.current.isPlaceholderData).toBe(true);
      expect(result.current.data?.enabled).toBe(true);
      expect(result.current.isLoading).toBe(false);
    });

    resolveSecond({ enabled: false, key_source: null, provider: null, model: null });
    await waitFor(() => {
      expect(result.current.isPlaceholderData).toBe(false);
      expect(result.current.data?.enabled).toBe(false);
    });
  });
});
