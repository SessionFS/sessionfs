import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AuthProvider, useAuth } from './auth/AuthContext';
import { useMe } from './hooks/useMe';
import LoginPage from './auth/LoginPage';
import Layout from './components/Layout';
import SessionList from './sessions/SessionList';
import SessionDetail from './sessions/SessionDetail';
import SearchResults from './sessions/SearchResults';
import SettingsPage from './sessions/SettingsPage';
import HandoffList from './handoffs/HandoffList';
import HandoffDetail from './handoffs/HandoffDetail';
import AdminDashboard from './admin/AdminDashboard';
import { BackgroundTasksProvider } from './components/BackgroundTasks';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { auth } = useAuth();
  if (!auth) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const { auth } = useAuth();
  const me = useMe();
  if (!auth) return <Navigate to="/login" replace />;
  if (me.isLoading) return <div className="text-center py-12 text-text-muted">Loading...</div>;
  if (me.data?.tier !== 'admin') return <Navigate to="/" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BackgroundTasksProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              element={
                <ProtectedRoute>
                  <Layout />
                </ProtectedRoute>
              }
            >
              <Route path="/" element={<SessionList />} />
              <Route path="/search" element={<SearchResults />} />
              <Route path="/sessions/:id" element={<SessionDetail />} />
              <Route path="/handoffs" element={<HandoffList />} />
              <Route path="/handoffs/:id" element={<HandoffDetail />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route
                path="/admin"
                element={
                  <AdminRoute>
                    <AdminDashboard />
                  </AdminRoute>
                }
              />
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
        </BackgroundTasksProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
