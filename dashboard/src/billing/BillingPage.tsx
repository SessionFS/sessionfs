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

  const { data: billing, isLoading } = useQuery({
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
      if (!res.ok) throw new Error('Checkout failed');
      return res.json();
    },
    onSuccess: (data) => {
      window.location.href = data.checkout_url;
    },
  });

  const portalMutation = useMutation({
    mutationFn: async () => {
      const res = await fetch(`${apiBase}/api/v1/billing/portal`, {
        method: 'POST',
        headers,
      });
      if (!res.ok) throw new Error('Portal failed');
      return res.json();
    },
    onSuccess: (data) => {
      window.location.href = data.portal_url;
    },
  });

  if (isLoading) {
    return <div className="text-center py-12 text-text-muted">Loading billing...</div>;
  }

  const currentTier = billing?.tier || 'free';
  const hasSubscription = billing?.has_subscription;

  return (
    <div className="max-w-4xl mx-auto px-4 py-8">
      <h1 className="text-2xl font-bold mb-6">Billing</h1>

      {/* Current plan info */}
      <div className="bg-bg-secondary rounded-lg p-6 mb-8">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm text-text-muted">Current Plan</p>
            <p className="text-xl font-semibold capitalize">{currentTier}</p>
          </div>
          {billing?.storage_limit_bytes > 0 && (
            <div className="text-right">
              <p className="text-sm text-text-muted">Storage</p>
              <p className="text-lg">
                {formatBytes(billing.storage_used_bytes)} / {formatBytes(billing.storage_limit_bytes)}
              </p>
              <div className="w-48 h-2 bg-border rounded-full mt-1">
                <div
                  className="h-2 bg-accent rounded-full"
                  style={{
                    width: `${Math.min(100, (billing.storage_used_bytes / billing.storage_limit_bytes) * 100)}%`,
                  }}
                />
              </div>
            </div>
          )}
        </div>

        {hasSubscription && (
          <div className="mt-4 flex gap-3">
            <button
              onClick={() => portalMutation.mutate()}
              className="px-4 py-2 bg-accent text-white rounded-lg hover:bg-accent/90"
            >
              Manage Subscription
            </button>
            <button
              onClick={() => portalMutation.mutate()}
              className="px-4 py-2 border border-border rounded-lg hover:bg-bg-secondary"
            >
              View Invoices
            </button>
          </div>
        )}
      </div>

      {/* Tier cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {TIERS.map((t) => (
          <div
            key={t.tier}
            className={`rounded-lg border p-5 ${
              t.popular ? 'border-accent ring-1 ring-accent' : 'border-border'
            } ${currentTier === t.tier ? 'bg-accent/5' : 'bg-bg-primary'}`}
          >
            {t.popular && (
              <span className="text-xs font-medium text-accent uppercase tracking-wide">Popular</span>
            )}
            <h3 className="text-lg font-semibold mt-1">{t.name}</h3>
            <p className="text-2xl font-bold mt-2">
              {t.price}
              <span className="text-sm font-normal text-text-muted">{t.period}</span>
            </p>

            <ul className="mt-4 space-y-2 text-sm">
              {t.features.map((f) => (
                <li key={f} className="flex items-start gap-2">
                  <span className="text-green-500 mt-0.5">&#10003;</span>
                  {f}
                </li>
              ))}
            </ul>

            <div className="mt-6">
              {currentTier === t.tier ? (
                <span className="block text-center py-2 text-text-muted text-sm">Current plan</span>
              ) : t.tier === 'free' ? null : (
                <button
                  onClick={() => checkoutMutation.mutate(t.tier)}
                  disabled={checkoutMutation.isPending}
                  className="w-full py-2 px-4 bg-accent text-white rounded-lg hover:bg-accent/90 disabled:opacity-50 text-sm font-medium"
                >
                  {checkoutMutation.isPending ? 'Redirecting...' : 'Upgrade'}
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      <p className="text-center text-sm text-text-muted mt-8">
        Need Enterprise? <a href="mailto:sales@sessionfs.dev" className="text-accent hover:underline">Contact sales</a>
      </p>
    </div>
  );
}
