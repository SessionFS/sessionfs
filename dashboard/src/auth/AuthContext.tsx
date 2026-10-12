import { createContext, useContext, useState, useCallback, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { createApiClient, type ApiClient } from '../api/client';

const STORAGE_KEY = 'sessionfs_auth';

interface AuthState {
  apiKey: string;
  baseUrl: string;
  client: ApiClient;
}

interface AuthContextValue {
  auth: AuthState | null;
  login: (baseUrl: string, apiKey: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function loadStoredAuth(): AuthState | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const { apiKey, baseUrl } = JSON.parse(raw);
    if (!apiKey || !baseUrl) return null;
    const client = createApiClient(baseUrl, apiKey);
    return { apiKey, baseUrl, client };
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [auth, setAuth] = useState<AuthState | null>(loadStoredAuth);
  const queryClient = useQueryClient();

  const login = useCallback(async (baseUrl: string, apiKey: string) => {
    const client = createApiClient(baseUrl, apiKey);
    await client.health();
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ apiKey, baseUrl }));
    // A new identity must never inherit the previous one's cached queries.
    queryClient.clear();
    setAuth({ apiKey, baseUrl, client });
  }, [queryClient]);

  const logout = useCallback(() => {
    sessionStorage.removeItem(STORAGE_KEY);
    setAuth(null);
    // Drop every cached query so the next identity on this browser never sees
    // the previous user's private data inside a staleTime window.
    queryClient.clear();
  }, [queryClient]);

  return (
    <AuthContext.Provider value={{ auth, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be inside AuthProvider');
  return ctx;
}
