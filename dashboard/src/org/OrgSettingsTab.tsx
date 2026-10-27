/**
 * v0.10.0 Phase 6 — Org general settings panel.
 *
 * Edits the three KB creation defaults that new org-scoped projects
 * inherit at project-create time (see routes/projects.py):
 *   - kb_retention_days
 *   - kb_max_context_words
 *   - kb_section_page_limit
 *
 * Round 3 (KB entry 298) narrowed the surface to these three fields
 * after Codex flagged that `retention_days` and `compile_model` had
 * no runtime consumers. Those fields can be re-added in a future
 * phase once the daemon retention / compile-route model-selection
 * paths are wired to read them.
 *
 * DLP policy lives in its own panel (existing `/api/v1/dlp/policy`
 * surface). This component intentionally scopes to non-DLP settings
 * to keep the form straightforward.
 *
 * Backend invariants enforced server-side (Phase 6 KB entry 230 #7):
 *   - Admin role required for PUT.
 *   - Range validation: 1-730 for kb_retention_days, 100-50000 for
 *     kb_max_context_words, 1-200 for kb_section_page_limit.
 *   - PUT body replaces the "general" block entirely; the route does
 *     a structural merge so the DLP block survives.
 */

import { useEffect, useState } from 'react';

import { useToast } from '../hooks/useToast';
import {
  type OrgGeneralSettings,
  useOrgSettings,
  useUpdateOrgSettings,
} from './useOrgSettings';
import { useDomains } from './useSso';
import { Button, Input } from '../components/ui';
import { errorMessage } from '../utils/errorMessage';
import { useOrgDigestPrefs, useUpdateOrgDigestPrefs } from './useOrgDigestPrefs';
import RestrictVisibilitySetting from './groups/RestrictVisibilitySetting';

interface OrgSettingsTabProps {
  orgId: string;
  /** Set to false when the viewer is a plain member; disables edits. */
  canEdit: boolean;
  /** The org's plan tier, when known (gates restricted project visibility). */
  tier?: string | null;
}

type FormState = {
  kb_retention_days: string;
  kb_max_context_words: string;
  kb_section_page_limit: string;
  helper_retention_days: string;
  auto_join_domains: string[];
};

function settingsToForm(s: OrgGeneralSettings | undefined): FormState {
  return {
    kb_retention_days: s?.kb_retention_days?.toString() ?? '',
    kb_max_context_words: s?.kb_max_context_words?.toString() ?? '',
    kb_section_page_limit: s?.kb_section_page_limit?.toString() ?? '',
    helper_retention_days: s?.helper_retention_days?.toString() ?? '',
    auto_join_domains: s?.auto_join_domains ?? [],
  };
}

function parseIntOrNull(v: string): number | null {
  const trimmed = v.trim();
  if (trimmed === '') return null;
  const n = Number(trimmed);
  return Number.isFinite(n) && Number.isInteger(n) ? n : NaN;
}

export default function OrgSettingsTab({ orgId, canEdit, tier }: OrgSettingsTabProps) {
  const { data, isLoading, error } = useOrgSettings(orgId);
  // The domain list is admin-only; members never request it.
  const domains = useDomains(canEdit ? orgId : undefined);
  const update = useUpdateOrgSettings(orgId);
  const { addToast } = useToast();
  // Digest opt-out is owner/admin-only; never fetch for plain members.
  const digestPrefs = useOrgDigestPrefs(orgId, canEdit);
  const updateDigest = useUpdateOrgDigestPrefs(orgId);

  const [form, setForm] = useState<FormState>(settingsToForm(undefined));

  // Re-seed the form from server state once it lands.
  useEffect(() => {
    setForm(settingsToForm(data));
  }, [data]);

  if (isLoading) return <p>Loading settings…</p>;
  if (error) return <p role="alert">Failed to load settings: {errorMessage(error)}</p>;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const payload: OrgGeneralSettings = {
      kb_retention_days: parseIntOrNull(form.kb_retention_days),
      kb_max_context_words: parseIntOrNull(form.kb_max_context_words),
      kb_section_page_limit: parseIntOrNull(form.kb_section_page_limit),
      helper_retention_days: parseIntOrNull(form.helper_retention_days),
    };
    // Only send auto-join domains when they changed, so saving KB settings
    // never re-validates (and fails on) a domain configured earlier.
    const savedDomains = data?.auto_join_domains ?? [];
    const domainsChanged =
      savedDomains.length !== form.auto_join_domains.length ||
      savedDomains.some((d) => !form.auto_join_domains.includes(d));
    if (domainsChanged) payload.auto_join_domains = form.auto_join_domains;
    // Reject any NaN (bad numeric input) before sending.
    for (const [k, v] of Object.entries(payload)) {
      if (Number.isNaN(v as number)) {
        addToast('error', `${k}: must be a whole number or blank`);
        return;
      }
    }
    update.mutate(payload, {
      onSuccess: () => addToast('success', 'Settings saved'),
      onError: (err) => addToast('error', `Save failed: ${errorMessage(err)}`),
    });
  };

  const toggleDomain = (domain: string) => {
    setForm((f) => ({
      ...f,
      auto_join_domains: f.auto_join_domains.includes(domain)
        ? f.auto_join_domains.filter((d) => d !== domain)
        : [...f.auto_join_domains, domain],
    }));
  };

  const toggleDigest = (optOut: boolean) => {
    updateDigest.mutate(optOut, {
      onSuccess: () => addToast('success', optOut ? 'Digest disabled' : 'Digest enabled'),
      onError: (err) => addToast('error', `Update failed: ${errorMessage(err)}`),
    });
  };

  const verifiedDomains = (domains.data ?? []).filter((d) => d.status === 'verified');
  // Configured domains that are no longer verified (deleted or re-pending).
  // Auto-join ignores them; show them so they can be removed. Only judged once
  // the domain list has actually loaded.
  const staleDomains = canEdit && domains.isSuccess
    ? form.auto_join_domains.filter((d) => !verifiedDomains.some((v) => v.domain === d))
    : [];

  const field = (key: keyof FormState, label: string, hint: string) => (
    <div className="flex flex-col gap-1">
      <Input
        type="number"
        value={form[key]}
        onChange={(e: React.ChangeEvent<HTMLInputElement>) => setForm((f) => ({ ...f, [key]: e.target.value }))}
        disabled={!canEdit || update.isPending}
        aria-label={label}
        title={label}
      />
      <span className="text-xs text-text-tertiary">{hint}</span>
    </div>
  );

  return (
    <>
    <section aria-labelledby="org-settings-heading">
      <h2 id="org-settings-heading">Org defaults</h2>
      <p className="text-sm text-text-secondary">
        Creation defaults for new projects added to this org. Existing
        projects keep their current values; change a project's own KB
        knobs from the project settings to override. Leave a field
        blank to fall back to the server's built-in default.
      </p>
      <form onSubmit={handleSubmit} aria-label="Org settings">
        {field(
          'kb_retention_days',
          'KB retention (days)',
          'Knowledge entry retention for new org projects (1-730).',
        )}
        {field(
          'kb_max_context_words',
          'KB compile word budget',
          'Maximum words injected into a compiled context document (100-50000).',
        )}
        {field(
          'kb_section_page_limit',
          'KB section page limit',
          'Maximum pages a single KB section can generate (1-200).',
        )}
        {field(
          'helper_retention_days',
          'Helper message retention (days)',
          'Days session-helper messages are kept before pruning (1-3650).',
        )}

        <div className="flex flex-col gap-2">
          <span className="text-sm text-text-primary">Auto-join by verified domain</span>
          <span className="text-xs text-text-tertiary">
            When a user verifies an email on one of these domains, they are
            automatically added to this org as a member. Requires a verified
            domain and a paid plan (OIDC SSO).
          </span>
          {!canEdit ? (
            <span className="text-sm text-text-secondary">
              {form.auto_join_domains.length > 0
                ? form.auto_join_domains.join(', ')
                : 'Not enabled.'}
            </span>
          ) : domains.isLoading ? (
            <span className="text-xs text-text-tertiary">Loading domains…</span>
          ) : domains.isError ? (
            <span role="alert" className="text-xs text-text-tertiary">
              Could not load verified domains. Auto-join settings are unchanged.
            </span>
          ) : verifiedDomains.length === 0 ? (
            <span className="text-xs text-text-tertiary">
              No verified domains yet. Verify a domain under SSO to enable auto-join.
            </span>
          ) : (
            verifiedDomains.map((d) => {
              const checked = form.auto_join_domains.includes(d.domain);
              return (
                <label
                  key={d.id}
                  className="flex items-center gap-2 px-3 py-2 border border-border rounded-lg bg-bg-primary cursor-pointer hover:bg-surface-hover transition-colors"
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleDomain(d.domain)}
                    disabled={!canEdit || update.isPending}
                    className="accent-[var(--brand)]"
                    aria-label={`Auto-join ${d.domain}`}
                  />
                  <span className="text-sm text-text-primary">{d.domain}</span>
                </label>
              );
            })
          )}
          {staleDomains.map((domain) => (
            <div
              key={`stale-${domain}`}
              role="status"
              className="flex items-center justify-between gap-2 px-3 py-2 border border-border rounded-lg bg-bg-primary"
            >
              <span className="text-sm text-text-secondary">
                {domain} is no longer verified, so auto-join skips it.
              </span>
              <Button
                type="button"
                variant="secondary"
                onClick={() => toggleDomain(domain)}
                disabled={!canEdit || update.isPending}
                aria-label={`Remove ${domain} from auto-join`}
              >
                Remove
              </Button>
            </div>
          ))}
        </div>

        <Button
          type="submit"
          variant="primary"
          disabled={!canEdit || update.isPending}
          loading={update.isPending}
          title={canEdit ? undefined : 'Only org admins can change settings'}
          aria-label="Save org settings"
        >
          Save settings
        </Button>
      </form>
    </section>

    <RestrictVisibilitySetting
      orgId={orgId}
      restricted={!!data?.restrict_project_visibility}
      canEdit={canEdit}
      tier={tier}
    />

    <section aria-labelledby="org-notifications-heading" className="mt-8">
      <h3 id="org-notifications-heading">Notifications</h3>
      <p className="text-sm text-text-secondary">
        A weekly email summarizing this org's activity, sent to owners and
        admins. You can opt out here without affecting anyone else.
      </p>
      <label className="flex items-center gap-2 px-3 py-2 border border-border rounded-lg bg-bg-primary cursor-pointer hover:bg-surface-hover transition-colors max-w-md">
        <input
          type="checkbox"
          checked={!digestPrefs.data?.opt_out}
          onChange={(e) => toggleDigest(!e.target.checked)}
          disabled={!canEdit || updateDigest.isPending || digestPrefs.isLoading}
          className="accent-[var(--brand)]"
          aria-label="Receive the weekly activity digest"
        />
        <span className="text-sm text-text-primary">Send me the weekly activity digest</span>
      </label>
      {digestPrefs.isError && (
        <span role="alert" className="block text-xs text-text-tertiary mt-1">
          Could not load your digest preference.
        </span>
      )}
    </section>
    </>
  );
}
