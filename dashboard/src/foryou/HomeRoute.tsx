import React, { Suspense } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { prefersSessionsHome } from './model';

const ForYouPage = React.lazy(() => import('./ForYouPage'));
const SessionList = React.lazy(() => import('../sessions/SessionList'));

/** Session-list query parameters that older links put on "/". */
const SESSION_PARAMS = ['view', 'model'];

/**
 * The dashboard home. For You by default; Sessions when the person chose it
 * as their home (For You → Home). Old "/?view=trash" and "/?model=…" links
 * keep working by moving to /sessions with the same query.
 */
export default function HomeRoute() {
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  if (SESSION_PARAMS.some((p) => params.has(p))) {
    return <Navigate to={`/sessions${search}`} replace />;
  }
  return (
    <Suspense fallback={<div className="flex items-center justify-center py-16 text-sm text-text-tertiary">Loading…</div>}>
      {prefersSessionsHome() ? <SessionList /> : <ForYouPage />}
    </Suspense>
  );
}
