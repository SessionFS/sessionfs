import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { useToast } from '../hooks/useToast';
import { isOrgAdminRole } from '../utils/roles';
import { Card } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';

const DLP_MODES = [
  { value: 'warn', label: 'Warn -- Flag findings but take no action' },
  { value: 'redact', label: 'Redact -- Replace sensitive content before sync' },
  { value: 'block', label: 'Block -- Prevent sync of sessions with findings' },
] as const;

const DLP_CATEGORIES = [
  { value: 'secrets', label: 'Secrets', description: 'API keys, tokens, passwords, private keys' },
  { value: 'phi', label: 'PHI', description: 'Protected Health Information (HIPAA)' },
] as const;

export default function DlpPolicyCard() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  const { addToast } = useToast();

  // Fetch billing status to check org membership
  const { data: billing } = useQuery({
    queryKey: ['billing-status'],
    queryFn: async () => {
      const res = await fetch(`${auth!.baseUrl}/api/v1/billing/status`, {
        headers: { Authorization: `Bearer ${auth!.apiKey}` },
      });
      if (!res.ok) throw new Error('Failed to load billing');
      return res.json();
    },
    enabled: !!auth,
  });

  const isOrgMember = billing?.is_org_member ?? false;
  const isOrgAdmin = isOrgAdminRole(billing?.org_role);

  // Fetch DLP policy
  const { data: policy, isLoading } = useQuery({
    queryKey: ['dlp-policy'],
    queryFn: () => auth!.client.getDLPPolicy(),
    enabled: !!auth && isOrgAdmin,
  });

  const updatePolicy = useMutation({
    mutationFn: (updates: { enabled?: boolean; mode?: string; categories?: string[] }) =>
      auth!.client.updateDLPPolicy(updates),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dlp-policy'] });
      addToast('success', 'DLP policy updated');
    },
  });

  if (!auth) return null;

  // Not in an org at all
  if (!isOrgMember) {
    return (
      <div className="space-y-5">
        <Card level="elevated" className="p-5">
          <h2 className="text-lg font-semibold text-text-primary mb-2">Data Loss Prevention</h2>
          <p className="text-sm text-text-tertiary">
            DLP scanning is available for Team and Enterprise plans.
          </p>
        </Card>
      </div>
    );
  }

  // In an org but not admin
  if (!isOrgAdmin) {
    return (
      <div className="space-y-5">
        <Card level="elevated" className="p-5">
          <h2 className="text-lg font-semibold text-text-primary mb-2">Data Loss Prevention</h2>
          <p className="text-sm text-text-tertiary">
            Only organization admins can configure DLP policies.
          </p>
        </Card>
      </div>
    );
  }

  const enabled = policy?.enabled ?? false;
  const mode = policy?.mode ?? 'warn';
  const categories = policy?.categories ?? [];

  function handleToggle() {
    updatePolicy.mutate({ enabled: !enabled });
  }

  function handleModeChange(newMode: string) {
    updatePolicy.mutate({ mode: newMode });
  }

  function handleCategoryToggle(cat: string) {
    const next = categories.includes(cat)
      ? categories.filter((c) => c !== cat)
      : [...categories, cat];
    updatePolicy.mutate({ categories: next });
  }

  return (
    <div className="space-y-5">
      <Card level="elevated" className="p-5">
        <h2 className="text-lg font-semibold text-text-primary mb-2">Data Loss Prevention</h2>
        <p className="text-sm text-text-tertiary mb-4">
          Automatically scan synced sessions for sensitive data such as secrets and protected health information.
        </p>

        {isLoading ? (
          <div className="text-sm text-text-tertiary py-2">Loading policy…</div>
        ) : (
          <>
            {/* Enable toggle */}
            <div className="flex items-center gap-3 mb-5">
              <button
                onClick={handleToggle}
                disabled={updatePolicy.isPending}
                className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] ${
                  enabled ? 'bg-brand' : 'bg-[var(--border)]'
                }`}
                role="switch"
                aria-checked={enabled}
                aria-label="Automatic DLP scanning"
              >
                <span
                  className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                    enabled ? 'translate-x-5' : 'translate-x-0'
                  }`}
                />
              </button>
              <span className="text-sm text-text-primary font-medium">
                {enabled ? 'Enabled' : 'Disabled'}
              </span>
            </div>

            {/* Mode selector */}
            <div className="mb-5">
              <label className="text-sm text-text-tertiary block mb-2">Mode</label>
              <div className="space-y-2">
                {DLP_MODES.map(({ value, label }) => (
                  <label key={value} className="flex items-center gap-2.5 text-sm text-text-secondary cursor-pointer">
                    <input
                      type="radio"
                      name="dlp-mode"
                      checked={mode === value}
                      onChange={() => handleModeChange(value)}
                      disabled={updatePolicy.isPending || !enabled}
                      className="text-brand focus-visible:ring-[var(--brand)]"
                    />
                    {label}
                  </label>
                ))}
              </div>
            </div>

            {/* Categories */}
            <div>
              <label className="text-sm text-text-tertiary block mb-2">Categories</label>
              <div className="space-y-2">
                {DLP_CATEGORIES.map(({ value, label, description }) => (
                  <label key={value} className="flex items-start gap-2.5 text-sm text-text-secondary cursor-pointer">
                    <input
                      type="checkbox"
                      checked={categories.includes(value)}
                      onChange={() => handleCategoryToggle(value)}
                      disabled={updatePolicy.isPending || !enabled}
                      className="mt-0.5 rounded border-border bg-surface text-brand focus-visible:ring-[var(--brand)]"
                    />
                    <div>
                      <span className="text-text-primary font-medium">{label}</span>
                      <span className="block text-xs text-text-tertiary">{description}</span>
                    </div>
                  </label>
                ))}
              </div>
            </div>

            {updatePolicy.isError && (
              <div className="text-sm text-danger mt-3">
                {errorMessage(updatePolicy.error, 'Failed to update DLP policy.')}
              </div>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
