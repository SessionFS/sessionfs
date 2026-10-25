/**
 * The helper status/conversation queries are gated on the drawer being open
 * (enabled: !!auth && open), so merely mounting the panel closed never hits
 * the helper API.
 */
import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';

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
});
