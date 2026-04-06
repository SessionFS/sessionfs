import { useQuery, useMutation } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

const TIERS = [
  {
    name: 'Free',
    price: '$0',
    period: '',
    tier: 'free',
    features: ['8-tool capture', 'Local search', 'Session resume', 'CLI tools'],
  },
  {
    name: 'Starter',
    price: '$4.99',
    period: '/mo',
    tier: 'starter',
    features: ['Cloud sync (500 MB)', 'Dashboard', 'Manual audit', 'MCP local', 'Session summaries'],
  },
  {
    name: 'Pro',
    price: '$14.99',
    period: '/mo',
    tier: 'pro',
    popular: true,
    features: [
      'Everything in Starter',
      'Autosync',
      'Auto-audit',
      'Team handoff',
      'PR/MR comments',
      'Project context',
      'MCP remote',
      'DLP scanning',
    ],
  },
  {
    name: 'Team',
    price: '$14.99',
    period: '/user/mo',
    tier: 'team',
    features: ['Everything in Pro', 'Team management', 'Shared storage (1 GB/user)', 'Org settings'],
  },
];

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

export default function BillingPage() {
  const { auth } = useAuth();
  const headers = { Authorization: `Bearer ${auth?.apiKey ?? ''}` };
  const apiBase = auth?.baseUrl || (window as any).__SFS_API_URL__ || '';

  const { data: billing, isLoading, isError } = useQuery({
    queryKey: ['billing-status'],
    queryFn: async () => {
      const res = await fetch(`${apiBase}/api/v1/billing/status`, { headers });
      if (!res.ok) throw new Error('Failed to load billing');
      return res.json();
    },
  });

  const checkoutMutation = useMutation({
    mutationFn: async (tier: string) => {
      const res = await fetch(`${apiBase}/api/v1/billing/checkout`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ tier, seats: 1 }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        const detail = body?.detail;
        const msg = (typeof detail === 'object' && detail !== null ? detail.message : null)
          || body?.message || (typeof detail === 'string' ? detail : null)
          || `Checkout failed (${res.status})`;
        throw new Error(msg);
      }
      return res.json();
    },
    onSuccess: (data) => {
      window.location.href = data.checkout_url;
    },
  });

  const portalMutation = useMutation({
    mutationFn: async (scope: string | undefined = undefined) => {
      const res = await fetch(`${apiBase}/api/v1/billing/portal`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: scope || 'auto' }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        const detail = body?.detail;
        const msg = (typeof detail === 'object' && detail !== null ? detail.message : null)
          || body?.message || (typeof detail === 'string' ? detail : null)
          || `Portal failed (${res.status})`;
        throw new Error(msg);
      }
      return res.json();
    },
    onSuccess: (data) => {
      window.location.href = data.portal_url;
    },
  });

  if (isLoading) {
    return <div className="text-center py-12 text-[var(--text-tertiary)]">Loading billing...</div>;
  }

  const currentTier = billing?.tier || 'free';
  const hasSubscription = billing?.has_subscription;
  const hasPersonalSub = billing?.has_personal_subscription ?? false;
  const isBeta = billing?.is_beta ?? isError; // Server determines if Stripe is configured
  const isOrgMember = billing?.is_org_member ?? false;
  const isOrgAdmin = billing?.org_role === 'admin';
  const storagePct = billing?.storage_limit_bytes > 0
    ? Math.min(100, ((billing.storage_used_bytes ?? 0) / billing.storage_limit_bytes) * 100)
    : 0;

  return (
    <div className="max-w-4xl mx-auto px-4 py-8">
      <h1 className="text-3xl font-bold tracking-tight text-[var(--text-primary)] mb-6">Billing</h1>

      {/* Current plan info */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-6 mb-8">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-[13px] text-[var(--text-tertiary)] mb-1">Current Plan</p>
            <div className="flex items-center gap-2">
              <p className="text-2xl font-semibold text-[var(--text-primary)] capitalize">{currentTier}</p>
              {isBeta && (
                <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-[var(--accent)]/15 text-[var(--accent)]">
                  Beta — all features included
                </span>
              )}
              {!isBeta && (
                <span className="px-2 py-0.5 text-xs font-medium rounded-full bg-[var(--brand)]/15 text-[var(--brand)]">
                  Current plan
                </span>
              )}
            </div>
          </div>
          {billing?.storage_limit_bytes > 0 && (
            <div className="text-right">
              <p className="text-sm text-[var(--text-tertiary)] mb-1">Storage</p>
              <p className="text-lg font-medium text-[var(--text-primary)]">
                {formatBytes(billing.storage_used_bytes)} / {formatBytes(billing.storage_limit_bytes)}
              </p>
              <div className="w-48 h-2 bg-[var(--border)] rounded-full mt-2 overflow-hidden">
                <div
                  className="h-full bg-[var(--brand)] rounded-full transition-all"
                  style={{ width: `${storagePct}%` }}
                />
              </div>
              <p className="text-xs text-[var(--text-tertiary)] mt-1">{storagePct.toFixed(0)}% used</p>
            </div>
          )}
        </div>

        {hasSubscription && (!isOrgMember || isOrgAdmin) && (
          <div className="mt-5 pt-4 border-t border-[var(--border)] flex gap-3">
            <button
              onClick={() => portalMutation.mutate(undefined)}
              disabled={portalMutation.isPending}
              className="bg-[var(--brand)] text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50"
            >
              {portalMutation.isPending ? 'Redirecting...' : 'Manage Subscription'}
            </button>
            <button
              onClick={() => portalMutation.mutate(undefined)}
              disabled={portalMutation.isPending}
              className="px-5 py-2.5 text-sm font-medium border border-[var(--border)] text-[var(--text-secondary)] rounded-lg hover:bg-[var(--surface-hover)] transition-colors disabled:opacity-50"
            >
              {portalMutation.isPending ? 'Redirecting...' : 'View Invoices'}
            </button>
            {portalMutation.isError && (
              <p className="text-red-500 text-sm self-center">{String(portalMutation.error)}</p>
            )}
          </div>
        )}
        {hasSubscription && isOrgMember && !isOrgAdmin && !hasPersonalSub && (
          <p className="mt-4 text-sm text-[var(--text-tertiary)]">
            Subscription is managed by your organization admin.
          </p>
        )}
        {isOrgMember && hasPersonalSub && (
          <div className="mt-4 p-4 rounded-lg border border-yellow-500/30 bg-yellow-500/5">
            <p className="text-sm text-yellow-600 font-medium mb-2">
              You have a personal subscription that is still active
            </p>
            <p className="text-xs text-[var(--text-tertiary)] mb-3">
              Your organization provides your plan. Cancel your personal subscription to avoid duplicate charges.
            </p>
            <button
              onClick={() => portalMutation.mutate('personal')}
              disabled={portalMutation.isPending}
              className="px-4 py-2 text-sm font-medium border border-yellow-500/30 text-yellow-600 rounded-lg hover:bg-yellow-500/10 transition-colors disabled:opacity-50"
            >
              {portalMutation.isPending ? 'Redirecting...' : 'Manage Personal Subscription'}
            </button>
            {portalMutation.isError && (
              <p className="text-red-500 text-xs mt-2">{String(portalMutation.error)}</p>
            )}
          </div>
        )}
      </div>

      {/* Tier cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {TIERS.map((t) => {
          const isCurrent = currentTier === t.tier;
          const isPro = t.popular;

          return (
            <div
              key={t.tier}
              className={`bg-[var(--bg-elevated)] rounded-xl border p-5 transition-colors ${
                isPro ? 'border-[var(--brand)] ring-1 ring-[var(--brand)]' : 'border-[var(--border)]'
              } ${isCurrent ? 'bg-[var(--brand)]/[0.03]' : ''}`}
            >
              {isPro && (
                <span className="text-xs font-medium text-[var(--brand)] uppercase tracking-wide">Popular</span>
              )}
              <h3 className="text-xl font-semibold text-[var(--text-primary)] mt-1">{t.name}</h3>
              <p className="text-3xl font-bold text-[var(--text-primary)] mt-2">
                {t.price}
                <span className="text-sm font-normal text-[var(--text-tertiary)]">{t.period}</span>
              </p>

              <ul className="mt-4 space-y-2 text-[14px]">
                {t.features.map((f) => (
                  <li key={f} className="flex items-start gap-2 text-[var(--text-secondary)]">
                    <span className="text-green-500 mt-0.5 flex-shrink-0">&#10003;</span>
                    {f}
                  </li>
                ))}
              </ul>

              <div className="mt-6">
                {isCurrent ? (
                  <span className="block text-center py-2 text-[var(--text-tertiary)] text-sm">Current plan</span>
                ) : t.tier === 'free' ? null : isOrgMember && (t.tier === 'starter' || t.tier === 'pro') ? (
                  <span className="block text-center py-2 text-[var(--text-tertiary)] text-sm">Managed by org</span>
                ) : isOrgMember && !isOrgAdmin && t.tier === 'team' ? (
                  <span className="block text-center py-2 text-[var(--text-tertiary)] text-sm">Admin only</span>
                ) : isBeta ? (
                  <span className="block text-center py-2 text-[var(--text-tertiary)] text-sm">Coming soon</span>
                ) : hasSubscription ? (
                  <button
                    onClick={() => portalMutation.mutate(undefined)}
                    disabled={portalMutation.isPending}
                    className="w-full py-2.5 px-5 rounded-lg text-sm font-medium border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] transition-colors disabled:opacity-50"
                  >
                    {portalMutation.isPending ? 'Redirecting...' : 'Change plan'}
                  </button>
                ) : (
                  <button
                    onClick={() => checkoutMutation.mutate(t.tier)}
                    disabled={checkoutMutation.isPending}
                    className={`w-full py-2.5 px-5 rounded-lg text-sm font-semibold transition-colors disabled:opacity-50 ${
                      isPro
                        ? 'bg-[var(--brand)] text-white hover:bg-[var(--brand-hover)]'
                        : 'border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--surface-hover)]'
                    }`}
                  >
                    {checkoutMutation.isPending ? 'Redirecting...' : 'Upgrade'}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {(checkoutMutation.isError || portalMutation.isError) && (
        <p className="text-center text-sm text-red-500 mt-4">
          {String(checkoutMutation.error || portalMutation.error)}
        </p>
      )}

      <p className="text-center text-sm text-[var(--text-tertiary)] mt-8">
        Need Enterprise? <a href="mailto:enterprise@sessionfs.dev" className="text-[var(--brand)] hover:underline">Contact sales</a>
        {' · '}
        <a href="https://sessionfs.dev/enterprise/" className="text-[var(--brand)] hover:underline">Learn more</a>
      </p>

      {/* Storage usage */}
      {billing?.storage_used_bytes != null && (
        <div className="mt-8 pt-6 border-t border-[var(--border)]">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--text-tertiary)] mb-3">Storage Usage</h2>
          <div className="w-full h-3 bg-[var(--border)] rounded-full overflow-hidden">
            <div
              className="h-full bg-[var(--brand)] rounded-full transition-all"
              style={{ width: `${Math.min(100, billing.storage_limit_bytes > 0 ? (billing.storage_used_bytes / billing.storage_limit_bytes) * 100 : 0)}%` }}
            />
          </div>
          <p className="text-sm text-[var(--text-tertiary)] mt-2">
            {formatBytes(billing.storage_used_bytes)} used
            {billing.storage_limit_bytes > 0 && ` of ${formatBytes(billing.storage_limit_bytes)}`}
          </p>
        </div>
      )}
    </div>
  );
}
