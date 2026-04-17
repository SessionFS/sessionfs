import { Navigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { getItem, setItem } from '../utils/storage';
import { onboardingDismissedKey } from './GettingStartedPage';

/** Legacy global key from pre-scoped implementation. */
const LEGACY_KEY = 'sfs-onboarding-dismissed';

/**
 * Wraps the root "/" route. Redirects first-time users (0 sessions, 0 projects,
 * onboarding not dismissed) to /getting-started. Everyone else passes through.
 *
 * Performance: uses the SAME query key as SessionList ('sessions' with page 1,
 * page_size 20) so React Query deduplicates the network request. No redundant
 * fetch. When dismissed (most users), no queries fire at all.
 */
export default function OnboardingGate({ children }: { children: React.ReactNode }) {
  const { auth } = useAuth();

  const scopedKey = onboardingDismissedKey(auth?.baseUrl, auth?.apiKey);

  // Migrate: if the old global key was set, copy to the scoped key and clear it.
  if (getItem(LEGACY_KEY) === '1') {
    setItem(scopedKey, '1');
    setItem(LEGACY_KEY, '');
  }

  // If onboarding was dismissed (most returning users), skip all queries.
  // This path renders children immediately — zero network overhead.
  const dismissed = getItem(scopedKey) === '1';

  // Use the same query key + page_size as SessionList so React Query shares
  // the cache entry. The gate never fires a redundant request.
  const sessions = useQuery({
    queryKey: ['sessions', { page: 1, page_size: 20 }],
    queryFn: () => auth!.client.listSessions({ page: 1, page_size: 20 }),
    enabled: !!auth && !dismissed,
    staleTime: 30_000,
  });

  const projects = useQuery({
    queryKey: ['projects'],
    queryFn: () => auth!.client.listProjects(),
    enabled: !!auth && !dismissed,
    staleTime: 30_000,
  });

  // Dismissed → immediate render, zero queries
  if (dismissed) return <>{children}</>;

  // While loading, show a minimal placeholder — not null (blank screen)
  // and not children (which mounts SessionList + fires extra queries).
  // The placeholder is invisible for fast responses (<100ms) via a CSS
  // animation delay, so returning users never see it.
  if (sessions.isLoading || projects.isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh] animate-[fadeIn_0.3s_ease-in_0.15s_both]">
        <p className="text-sm text-[var(--text-tertiary)]">Loading...</p>
      </div>
    );
  }

  // Errors → pass through (don't misroute existing users)
  if (sessions.isError || projects.isError) return <>{children}</>;

  const hasSession = (sessions.data?.total ?? 0) > 0;
  const hasProject = (projects.data?.length ?? 0) > 0;

  if (!hasSession && !hasProject) {
    return <Navigate to="/getting-started" replace />;
  }

  return <>{children}</>;
}
