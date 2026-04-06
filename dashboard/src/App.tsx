import React, { Suspense } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AuthProvider, useAuth } from './auth/AuthContext';
import { useMe } from './hooks/useMe';
import Layout from './components/Layout';
import { BackgroundTasksProvider } from './components/BackgroundTasks';
import { ToastProvider } from './components/Toast';

// Lazy-loaded page components for code splitting
const LoginPage = React.lazy(() => import('./auth/LoginPage'));
const SessionList = React.lazy(() => import('./sessions/SessionList'));
const SessionDetail = React.lazy(() => import('./sessions/SessionDetail'));
const SearchResults = React.lazy(() => import('./sessions/SearchResults'));
const SettingsPage = React.lazy(() => import('./sessions/SettingsPage'));
const HandoffList = React.lazy(() => import('./handoffs/HandoffList'));
const HandoffDetail = React.lazy(() => import('./handoffs/HandoffDetail'));
const AdminDashboard = React.lazy(() => import('./admin/AdminDashboard'));
const BillingPage = React.lazy(() => import('./billing/BillingPage'));
const OrgPage = React.lazy(() => import('./org/OrgPage'));
const ProjectsPage = React.lazy(() => import('./projects/ProjectsPage'));
const ProjectDetail = React.lazy(() => import('./projects/ProjectDetail'));

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
        <ToastProvider>
        <BackgroundTasksProvider>
        <BrowserRouter>
          <Suspense fallback={<div className="flex items-center justify-center h-screen text-text-muted">Loading…</div>}>
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
              <Route path="/projects" element={<ProjectsPage />} />
              <Route path="/projects/:id" element={<ProjectDetail />} />
              <Route path="/handoffs" element={<HandoffList />} />
              <Route path="/handoffs/:id" element={<HandoffDetail />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="/settings/billing" element={<BillingPage />} />
              <Route path="/settings/organization" element={<OrgPage />} />
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
          </Suspense>
        </BrowserRouter>
        </BackgroundTasksProvider>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
