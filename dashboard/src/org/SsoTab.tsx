/**
 * v0.13.x — SSO (OIDC) org-admin configuration panel.
 *
 * Four subsections, each gated on canEdit / isOwner:
 *   1. Provider CRUD (POST/GET/PATCH/DELETE)
 *   2. Domain verification (POST/GET/verify/DELETE)
 *   3. Enforcement toggle (PATCH /provider/enforcement)
 *   4. Break-glass grants (POST/GET/DELETE) — owner only
 *
 * Matches the OrgSettingsTab card pattern exactly.
 */

import { useState } from 'react';

import { useToast } from '../hooks/useToast';
import {
  useSsoProvider,
  useCreateProvider,
  useUpdateProvider,
  useDeleteProvider,
  useDomains,
  useCreateDomain,
  useVerifyDomain,
  useDeleteDomain,
  useToggleEnforcement,
  useBreakGlassGrants,
  useCreateBreakGlass,
  useRevokeBreakGlass,
  type SsoProviderResponse,
} from './useSso';
import { Button, Input, Dialog, DialogHeader, DialogFooter } from '../components/ui';

interface SsoTabProps {
  orgId: string;
  /** Set to false when the viewer is a plain member; disables edits. */
  canEdit: boolean;
  /** Only owners can manage break-glass grants. */
  isOwner: boolean;
}

/* ── helpers ── */

function parseErrorText(body: string): string {
  if (!body) return 'Unknown error';
  try {
    const json = JSON.parse(body);
    if (typeof json?.error?.message === 'string') return json.error.message;
    if (typeof json?.detail === 'string') return json.detail;
    // FastAPI 422: detail is an array of {msg, loc}.
    if (Array.isArray(json?.detail) && typeof json.detail[0]?.msg === 'string') {
      return json.detail[0].msg;
    }
  } catch {
    // not JSON
  }
  // Don't echo a raw non-JSON body (an HTML error page or internal detail) —
  // surface a generic message instead.
  return 'Request failed. Please try again.';
}

/* ── sub-components ── */

/** Banner-like callout for the empty-provider state. */
function SsoEmptyState({ onConfigure }: { onConfigure: () => void }) {
  return (
    <div className="text-center py-6" data-testid="sso-empty-state">
      <p className="text-text-secondary text-sm mb-3">
        SSO not configured for this organization.
      </p>
      <Button variant="primary" onClick={onConfigure} aria-label="Configure OIDC provider">
        Configure OIDC provider
      </Button>
    </div>
  );
}

/** Shared confirm-delete dialog. */
function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  message,
  loading,
}: {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: string;
  loading?: boolean;
}) {
  return (
    <Dialog open={open} onClose={onClose} titleId="confirm-dialog-heading">
      <DialogHeader titleId="confirm-dialog-heading">{title}</DialogHeader>
      <p className="text-sm text-text-secondary mb-4">{message}</p>
      <DialogFooter>
        <Button variant="secondary" onClick={onClose} disabled={loading}>
          Cancel
        </Button>
        <Button variant="danger" onClick={onConfirm} loading={loading}>
          Confirm
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

/* ── export ── */

export default function SsoTab({ orgId, canEdit, isOwner }: SsoTabProps) {
  const { addToast } = useToast();

  /* ── provider state ── */
  const provider = useSsoProvider(orgId);
  const createProvider = useCreateProvider(orgId);
  const updateProvider = useUpdateProvider(orgId);
  const deleteProvider = useDeleteProvider(orgId);
  const toggleEnforcement = useToggleEnforcement(orgId);

  const [showProviderForm, setShowProviderForm] = useState(false);
  const [showEditProvider, setShowEditProvider] = useState(false);
  const [confirmDeleteProvider, setConfirmDeleteProvider] = useState(false);

  /* ── domain state ── */
  const domains = useDomains(orgId);
  const createDomain = useCreateDomain(orgId);
  const verifyDomain = useVerifyDomain(orgId);
  const deleteDomain = useDeleteDomain(orgId);

  const [domainInput, setDomainInput] = useState('');
  const [newDomainResult, setNewDomainResult] = useState<{ domain: string; txt_record: string } | null>(null);
  const [confirmDeleteDomainId, setConfirmDeleteDomainId] = useState<string | null>(null);

  /* ── break-glass state ── */
  const grants = useBreakGlassGrants(orgId, isOwner);
  const createGrant = useCreateBreakGlass(orgId);
  const revokeGrant = useRevokeBreakGlass(orgId);

  const [grantUserId, setGrantUserId] = useState('');
  const [confirmRevokeGrantId, setConfirmRevokeGrantId] = useState<string | null>(null);

  /* ── derived ── */
  const providerData: SsoProviderResponse | null = provider.data ?? null;
  const isConfigured = !!providerData;
  // Provider subsection tells us whether enforcement can be enabled.
  const hasEnabledProvider = isConfigured && providerData.enabled;
  const hasVerifiedDomain = (domains.data ?? []).some((d) => d.status === 'verified');
  const canEnforce = hasEnabledProvider && hasVerifiedDomain;

  /* ── loading / error ── */
  if (provider.isLoading) return <p className="text-text-tertiary">Loading SSO settings…</p>;
  if (provider.isError)
    return (
      <p role="alert" className="text-red-500">
        Failed to load SSO settings: {String((provider.error as Error)?.message ?? provider.error)}
      </p>
    );

  /* ── handlers ── */

  const handleCreateProvider = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const scopesRaw = (form.get('allowed_scopes') as string) || 'openid email profile';
    createProvider.mutate(
      {
        display_name: form.get('display_name') as string,
        issuer: form.get('issuer') as string,
        client_id: form.get('client_id') as string,
        client_secret_ref: form.get('client_secret_ref') as string,
        allowed_scopes: scopesRaw.split(/\s+/).filter(Boolean),
      },
      {
        onSuccess: () => {
          addToast('success', 'OIDC provider configured');
          setShowProviderForm(false);
        },
        onError: (err) => addToast('error', `Failed: ${parseErrorText(err.message)}`),
      },
    );
  };

  const handleUpdateProvider = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const scopesRaw = (form.get('allowed_scopes') as string) || '';
    const payload: Record<string, unknown> = {};
    const fields = ['display_name', 'issuer', 'client_id', 'client_secret_ref'] as const;
    for (const f of fields) {
      const v = form.get(f) as string;
      if (v) payload[f] = v;
    }
    if (scopesRaw.trim()) {
      payload.allowed_scopes = scopesRaw.split(/\s+/).filter(Boolean);
    }
    updateProvider.mutate(payload as Partial<SsoProviderResponse>, {
      onSuccess: () => {
        addToast('success', 'Provider updated');
        setShowEditProvider(false);
      },
      onError: (err) => addToast('error', `Update failed: ${parseErrorText(err.message)}`),
    });
  };

  const handleDeleteProvider = () => {
    deleteProvider.mutate(undefined, {
      onSuccess: () => {
        addToast('success', 'OIDC provider removed');
        setConfirmDeleteProvider(false);
      },
      onError: (err) => addToast('error', `Delete failed: ${parseErrorText(err.message)}`),
    });
  };

  const handleToggleEnabled = () => {
    if (!providerData) return;
    updateProvider.mutate(
      { enabled: !providerData.enabled },
      {
        onSuccess: () => addToast('success', providerData.enabled ? 'Provider disabled' : 'Provider enabled'),
        onError: (err) => addToast('error', `Toggle failed: ${parseErrorText(err.message)}`),
      },
    );
  };

  const handleToggleEnforcement = () => {
    if (!providerData) return;
    toggleEnforcement.mutate(!providerData.enforced, {
      onSuccess: (data) =>
        addToast('success', data.enforced ? 'Enforcement enabled' : 'Enforcement disabled'),
      onError: (err) => addToast('error', `Enforcement failed: ${parseErrorText(err.message)}`),
    });
  };

  const handleAddDomain = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!domainInput.trim()) return;
    createDomain.mutate(
      { domain: domainInput.trim() },
      {
        onSuccess: (data) => {
          setNewDomainResult({ domain: data.domain, txt_record: data.txt_record });
          setDomainInput('');
        },
        onError: (err) => addToast('error', `Add domain failed: ${parseErrorText(err.message)}`),
      },
    );
  };

  const handleVerifyDomain = (domainId: string) => {
    verifyDomain.mutate(domainId, {
      onSuccess: (data) =>
        addToast(
          data.status === 'verified' ? 'success' : 'info',
          data.status === 'verified'
            ? `Domain ${data.domain} verified`
            : `Verification pending for ${data.domain} — check DNS`,
        ),
      onError: (err) => addToast('error', `Verify failed: ${parseErrorText(err.message)}`),
    });
  };

  const handleDeleteDomain = () => {
    if (!confirmDeleteDomainId) return;
    deleteDomain.mutate(confirmDeleteDomainId, {
      onSuccess: () => {
        addToast('success', 'Domain removed');
        setConfirmDeleteDomainId(null);
      },
      onError: (err) => addToast('error', `Delete domain failed: ${parseErrorText(err.message)}`),
    });
  };

  const handleCreateGrant = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!grantUserId.trim()) return;
    createGrant.mutate(
      { admin_user_id: grantUserId.trim() },
      {
        onSuccess: () => {
          addToast('success', 'Break-glass grant issued (1 hour)');
          setGrantUserId('');
        },
        onError: (err) => addToast('error', `Grant failed: ${parseErrorText(err.message)}`),
      },
    );
  };

  const handleRevokeGrant = () => {
    if (!confirmRevokeGrantId) return;
    revokeGrant.mutate(confirmRevokeGrantId, {
      onSuccess: () => {
        addToast('success', 'Break-glass grant revoked');
        setConfirmRevokeGrantId(null);
      },
      onError: (err) => addToast('error', `Revoke failed: ${parseErrorText(err.message)}`),
    });
  };

  /* ── render ── */

  return (
    <section aria-labelledby="sso-heading">
      {/* ── Provider ── */}
      <h3 id="sso-heading" className="text-lg font-semibold text-text-primary mb-3">
        SSO (OIDC) Configuration
      </h3>

      <div className="bg-surface border border-border rounded-lg p-4 mb-5">
        <h4 className="text-base font-medium text-text-primary mb-2">Provider</h4>

        {!isConfigured && !showProviderForm && (
          <SsoEmptyState onConfigure={() => setShowProviderForm(true)} />
        )}

        {!isConfigured && showProviderForm && (
          <form onSubmit={handleCreateProvider} aria-label="Configure OIDC provider" className="flex flex-col gap-3">
            <Input title="Display name" name="display_name" required disabled={!canEdit || createProvider.isPending} placeholder="e.g. Google Workspace" />
            <Input title="Issuer URL" name="issuer" type="url" required disabled={!canEdit || createProvider.isPending} placeholder="https://accounts.google.com" />
            <Input title="Client ID" name="client_id" required disabled={!canEdit || createProvider.isPending} />
            <div className="flex flex-col gap-1">
              <Input
                title="Client secret REFERENCE"
                name="client_secret_ref"
                required
                disabled={!canEdit || createProvider.isPending}
                placeholder="env:MY_OIDC_CLIENT_SECRET"
              />
              <span className="text-xs text-text-tertiary" data-testid="secret-ref-hint">
                A reference like <code className="text-xs bg-surface px-1 rounded">env:VAR</code> — NOT the secret value itself
              </span>
            </div>
            <Input
              title="Allowed scopes"
              name="allowed_scopes"
              disabled={!canEdit || createProvider.isPending}
              placeholder="openid email profile"
            />
            <div className="flex gap-2">
              <Button type="submit" variant="primary" disabled={!canEdit || createProvider.isPending} loading={createProvider.isPending}>
                Save provider
              </Button>
              <Button type="button" variant="ghost" onClick={() => setShowProviderForm(false)} disabled={createProvider.isPending}>
                Cancel
              </Button>
            </div>
          </form>
        )}

        {isConfigured && (
          <div className="flex flex-col gap-2" data-testid="provider-configured">
            <FieldRow label="Display name" value={providerData.display_name} />
            <FieldRow label="Issuer" value={providerData.issuer} />
            <FieldRow label="Client ID" value={providerData.client_id} />
            <FieldRow label="Client secret REFERENCE" value={providerData.client_secret_ref} mono />
            <FieldRow label="Scopes" value={providerData.allowed_scopes.join(', ')} />
            <FieldRow label="Enabled" value={providerData.enabled ? 'Yes' : 'No'} />

            {/* Enabled toggle */}
            <div className="flex items-center gap-3 mt-2">
              <label className="text-sm font-medium text-text-secondary">Enabled</label>
              <button
                type="button"
                role="switch"
                aria-checked={providerData.enabled}
                aria-label="Toggle provider enabled"
                disabled={!canEdit || updateProvider.isPending}
                onClick={handleToggleEnabled}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors duration-200 focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50 ${providerData.enabled ? 'bg-brand' : 'bg-surface border border-border'}`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform duration-200 ${providerData.enabled ? 'translate-x-6' : 'translate-x-1'}`}
                />
              </button>
            </div>

            <div className="flex gap-2 mt-3">
              <Button variant="secondary" onClick={() => setShowEditProvider(true)} disabled={!canEdit} aria-label="Edit provider">
                Edit
              </Button>
              <Button variant="danger" onClick={() => setConfirmDeleteProvider(true)} disabled={!canEdit} aria-label="Delete provider">
                Delete
              </Button>
            </div>

            {/* Edit provider dialog */}
            {showEditProvider && (
              <Dialog open={showEditProvider} onClose={() => setShowEditProvider(false)} titleId="edit-provider-heading">
                <DialogHeader titleId="edit-provider-heading">Edit OIDC provider</DialogHeader>
                <form onSubmit={handleUpdateProvider} aria-label="Edit OIDC provider" className="flex flex-col gap-3">
                  <Input title="Display name" name="display_name" defaultValue={providerData.display_name} disabled={updateProvider.isPending} />
                  <Input title="Issuer URL" name="issuer" type="url" defaultValue={providerData.issuer} disabled={updateProvider.isPending} />
                  <Input title="Client ID" name="client_id" defaultValue={providerData.client_id} disabled={updateProvider.isPending} />
                  <div className="flex flex-col gap-1">
                    <Input
                      title="Client secret REFERENCE"
                      name="client_secret_ref"
                      defaultValue={providerData.client_secret_ref}
                      disabled={updateProvider.isPending}
                      placeholder="env:MY_OIDC_CLIENT_SECRET"
                    />
                    <span className="text-xs text-text-tertiary">
                      A reference like <code className="text-xs bg-surface px-1 rounded">env:VAR</code> — NOT the secret value itself
                    </span>
                  </div>
                  <Input
                    title="Allowed scopes"
                    name="allowed_scopes"
                    defaultValue={providerData.allowed_scopes.join(' ')}
                    disabled={updateProvider.isPending}
                  />
                  <DialogFooter>
                    <Button type="button" variant="ghost" onClick={() => setShowEditProvider(false)} disabled={updateProvider.isPending}>
                      Cancel
                    </Button>
                    <Button type="submit" variant="primary" loading={updateProvider.isPending}>
                      Save changes
                    </Button>
                  </DialogFooter>
                </form>
              </Dialog>
            )}
          </div>
        )}

      </div>

      {/* ── Domains ── */}
      {isConfigured && (
        <div className="bg-surface border border-border rounded-lg p-4 mb-5">
          <h4 className="text-base font-medium text-text-primary mb-2">Domains</h4>

          {domains.isLoading && <p className="text-text-tertiary text-sm">Loading domains…</p>}
          {domains.isError && <p role="alert" className="text-red-500 text-sm">{String((domains.error as Error)?.message ?? domains.error)}</p>}

          {domains.data && domains.data.length === 0 && (
            <p className="text-text-tertiary text-sm mb-3">No domains configured yet.</p>
          )}

          {(domains.data ?? []).length > 0 && (
            <ul className="divide-y divide-border mb-3" aria-label="Domain list">
              {(domains.data ?? []).map((d) => (
                <li key={d.id} className="py-2 flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="text-sm text-text-primary truncate">{d.domain}</span>
                    <span
                      className={`shrink-0 px-2 py-0.5 rounded-full text-xs font-medium ${
                        d.status === 'verified'
                          ? 'bg-green-500/15 text-green-500'
                          : 'bg-amber-500/15 text-amber-500'
                      }`}
                    >
                      {d.status}
                    </span>
                  </div>
                  <div className="flex gap-2 shrink-0">
                    {d.status !== 'verified' && (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => handleVerifyDomain(d.id)}
                        disabled={!canEdit || verifyDomain.isPending}
                        loading={verifyDomain.isPending && verifyDomain.variables === d.id}
                        aria-label={`Verify domain ${d.domain}`}
                      >
                        Verify
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setConfirmDeleteDomainId(d.id)}
                      disabled={!canEdit}
                      aria-label={`Delete domain ${d.domain}`}
                    >
                      Delete
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {/* New domain result — show txt_record prominently */}
          {newDomainResult && (
            <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-3 mb-3" data-testid="txt-record-callout">
              <p className="text-sm font-medium text-text-primary mb-1">
                DNS record to publish for <span className="font-mono">{newDomainResult.domain}</span>:
              </p>
              <code className="block text-sm bg-surface border border-border rounded px-3 py-2 mt-1 break-all select-all" data-testid="txt-record-value">
                {newDomainResult.txt_record}
              </code>
            </div>
          )}

          <form onSubmit={handleAddDomain} className="flex gap-2 items-end" aria-label="Add domain">
            <div className="flex-1">
              <Input
                title="Add domain"
                value={domainInput}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setDomainInput(e.target.value)}
                disabled={!canEdit || createDomain.isPending}
                placeholder="example.com"
              />
            </div>
            <Button type="submit" variant="primary" disabled={!canEdit || createDomain.isPending || !domainInput.trim()} loading={createDomain.isPending}>
              Add
            </Button>
          </form>

          {/* Confirm delete domain */}
          <ConfirmDialog
            open={!!confirmDeleteDomainId}
            onClose={() => setConfirmDeleteDomainId(null)}
            onConfirm={handleDeleteDomain}
            title="Remove domain"
            message="This domain will no longer be used for SSO enforcement."
            loading={deleteDomain.isPending}
          />
        </div>
      )}

      {/* ── Enforcement ── */}
      {isConfigured && (
        <div className="bg-surface border border-border rounded-lg p-4 mb-5">
          <h4 className="text-base font-medium text-text-primary mb-2">Enforcement</h4>

          <div className="flex items-center gap-3 mb-2">
            <label className="text-sm font-medium text-text-secondary">Enforce SSO</label>
            <button
              type="button"
              role="switch"
              aria-checked={providerData.enforced}
              aria-label="Toggle SSO enforcement"
              disabled={
                !canEdit ||
                toggleEnforcement.isPending ||
                // Preconditions gate ENABLING only — the admin can always turn
                // enforcement OFF (matches the backend; avoids a lockout if a
                // verified domain later lapses while enforcement is on).
                (!providerData.enforced && !canEnforce)
              }
              onClick={handleToggleEnforcement}
              className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors duration-200 focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] disabled:opacity-50 ${providerData.enforced ? 'bg-brand' : 'bg-surface border border-border'}`}
            >
              <span
                className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform duration-200 ${providerData.enforced ? 'translate-x-6' : 'translate-x-1'}`}
              />
            </button>
          </div>

          {!canEnforce && (
            <p className="text-xs text-text-tertiary mb-2" data-testid="enforcement-hint">
              Requires an enabled provider and at least one verified domain.
            </p>
          )}

          <p className="text-xs text-text-tertiary">
            The org owner is never locked out — break-glass grants allow emergency access.
          </p>

          {toggleEnforcement.isError && (
            <p role="alert" className="text-red-500 text-sm mt-2">
              {parseErrorText((toggleEnforcement.error as Error)?.message ?? '')}
            </p>
          )}
        </div>
      )}

      {/* ── Break-glass ── */}
      {isOwner && (
        <div className="bg-surface border border-border rounded-lg p-4 mb-5">
          <h4 className="text-base font-medium text-text-primary mb-2">Break-glass grants</h4>
          <p className="text-xs text-text-tertiary mb-3">
            One-hour emergency grants for admins to sign in when SSO enforcement blocks them. Owner only.
          </p>

          {grants.isLoading && <p className="text-text-tertiary text-sm">Loading grants…</p>}
          {grants.isError && <p role="alert" className="text-red-500 text-sm">{String((grants.error as Error)?.message ?? grants.error)}</p>}

          {(grants.data ?? []).length > 0 && (
            <ul className="divide-y divide-border mb-3" aria-label="Break-glass grants">
              {(grants.data ?? []).map((g) => {
                const active = !g.revoked_at && new Date(g.expires_at) > new Date();
                return (
                  <li key={g.id} className="py-2 flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2 min-w-0">
                      <code className="text-xs text-text-secondary truncate">{g.admin_user_id}</code>
                      <span
                        className={`shrink-0 px-2 py-0.5 rounded-full text-xs font-medium ${
                          active
                            ? 'bg-green-500/15 text-green-500'
                            : 'bg-text-tertiary/15 text-text-tertiary'
                        }`}
                      >
                        {active ? 'active' : 'expired'}
                      </span>
                    </div>
                    <div className="flex gap-2 shrink-0 items-center">
                      <span className="text-xs text-text-tertiary">
                        expires {g.expires_at?.slice(0, 16).replace('T', ' ')}
                      </span>
                      {active && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setConfirmRevokeGrantId(g.id)}
                          disabled={!canEdit}
                          aria-label={`Revoke grant for ${g.admin_user_id}`}
                        >
                          Revoke
                        </Button>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}

          <form onSubmit={handleCreateGrant} className="flex gap-2 items-end" aria-label="Issue break-glass grant">
            <div className="flex-1">
              <Input
                title="Admin user ID"
                value={grantUserId}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setGrantUserId(e.target.value)}
                disabled={!canEdit || createGrant.isPending}
                placeholder="user ID to grant access"
              />
            </div>
            <Button
              type="submit"
              variant="primary"
              disabled={!canEdit || createGrant.isPending || !grantUserId.trim()}
              loading={createGrant.isPending}
            >
              Issue grant (1h)
            </Button>
          </form>

          {/* Confirm revoke */}
          <ConfirmDialog
            open={!!confirmRevokeGrantId}
            onClose={() => setConfirmRevokeGrantId(null)}
            onConfirm={handleRevokeGrant}
            title="Revoke break-glass grant"
            message="The admin will no longer be able to use this grant to bypass SSO enforcement."
            loading={revokeGrant.isPending}
          />
        </div>
      )}

      {/* Confirm delete provider — always rendered, opened from provider section */}
      <ConfirmDialog
        open={confirmDeleteProvider}
        onClose={() => setConfirmDeleteProvider(false)}
        onConfirm={handleDeleteProvider}
        title="Delete OIDC provider"
        message="This removes the OIDC provider and disables SSO login for the org. Verified domains are NOT removed (delete those separately if needed). This cannot be undone."
        loading={deleteProvider.isPending}
      />
    </section>
  );
}

/** Tiny helper to render a read-only label/value pair. */
function FieldRow({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex flex-col sm:flex-row sm:gap-3">
      <span className="text-sm text-text-tertiary w-40 shrink-0">{label}</span>
      <span className={`text-sm text-text-primary ${mono ? 'font-mono' : ''}`}>{value}</span>
    </div>
  );
}
