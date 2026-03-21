import { createContext, useContext, useState, useCallback, type ReactNode } from 'react';
import { createApiClient, type ApiClient } from '../api/client';

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

export function AuthProvider({ children }: { children: ReactNode }) {
  const [auth, setAuth] = useState<AuthState | null>(null);

  const login = useCallback(async (baseUrl: string, apiKey: string) => {
    const client = createApiClient(baseUrl, apiKey);
    await client.health();
    setAuth({ apiKey, baseUrl, client });
  }, []);

  const logout = useCallback(() => setAuth(null), []);

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
