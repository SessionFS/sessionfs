import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { readRememberedScope, rememberScope, scopeOwner } from '../helper/scopeMemory';
import { AuthProvider, useAuth } from './AuthContext';

vi.mock('../api/client', async (importActual) => ({
  ...(await importActual<typeof import('../api/client')>()),
  createApiClient: () => ({ health: vi.fn().mockResolvedValue({}) }),
}));

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider>{children}</AuthProvider>
    </QueryClientProvider>
  );
}

describe('AuthProvider and the helper scope memory', () => {
  const owner = scopeOwner('https://api.example', 'sk_a');

  it('forgets the remembered helper scope on logout', () => {
    const { result } = renderHook(() => useAuth(), { wrapper });
    rememberScope(owner, { project_id: 'p1', label: 'Secret' });
    act(() => result.current.logout());
    expect(readRememberedScope(owner)).toBeUndefined();
  });

  it('forgets the remembered helper scope on login', async () => {
    const { result } = renderHook(() => useAuth(), { wrapper });
    rememberScope(owner, { group_id: 'g1', label: 'Payments' });
    await act(() => result.current.login('https://api.example', 'sk_b'));
    expect(readRememberedScope(owner)).toBeUndefined();
  });
});
