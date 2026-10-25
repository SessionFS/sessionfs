import { useState, type ReactNode } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { useMe } from '../hooks/useMe';
import { getAvatarColor } from '../utils/avatar';
import { useSearchParams } from 'react-router-dom';
import { Dropdown, Button, Dialog, DialogHeader, DialogFooter, PageContainer, PageHeader, Tabs } from '../components/ui';
import OrgSettingsTab from './OrgSettingsTab';
import OrgHelperKeyCard from './OrgHelperKeyCard';
import SsoTab from './SsoTab';
import OrgPersonasSection, { type OrgPersonaMember } from './OrgPersonasSection';
import SecondaryOrgPage from './SecondaryOrgPage';
import ActivateLicensePanel from './ActivateLicensePanel';
import OwnerTransferDialog from './OwnerTransferDialog';
import { JiraCard } from './JiraCard';
import { errorMessage } from '../utils/errorMessage';

type OrgTab = 'members' | 'personas' | 'policies' | 'security' | 'integrations';

export default function OrgPage() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  const headers = { Authorization: `Bearer ${auth?.apiKey ?? ''}` };
  const apiBase = auth?.baseUrl || (window as any).__SFS_API_URL__ || '';

  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('member');
  const [showInviteForm, setShowInviteForm] = useState(false);
  const [removeTarget, setRemoveTarget] = useState<{ user_id: string; email: string } | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();

  const { data, isLoading } = useQuery({
    queryKey: ['org-info'],
    queryFn: async () => {
      const res = await fetch(`${apiBase}/api/v1/org`, { headers });
      if (!res.ok) throw new Error('Failed to load org');
      return res.json();
    },
  });

  const { data: invites } = useQuery({
    queryKey: ['org-invites'],
    queryFn: async () => {
      const res = await fetch(`${apiBase}/api/v1/org/invites`, { headers });
      if (!res.ok) return { invites: [] };
      return res.json();
    },
    enabled: data?.current_user_role === 'admin' || data?.current_user_role === 'owner',
  });

  const inviteMutation = useMutation({
    mutationFn: async () => {
      const res = await fetch(`${apiBase}/api/v1/org/invite`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: inviteEmail, role: inviteRole }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Invite failed');
      }
      return res.json();
    },
    onSuccess: () => {
      setInviteEmail('');
      setShowInviteForm(false);
      queryClient.invalidateQueries({ queryKey: ['org-invites'] });
    },
  });

  const removeMutation = useMutation({
    mutationFn: async (userId: string) => {
      const res = await fetch(`${apiBase}/api/v1/org/members/${userId}`, {
        method: 'DELETE',
        headers,
      });
      if (!res.ok) throw new Error('Remove failed');
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['org-info'] }),
  });

  const changeRoleMutation = useMutation({
    mutationFn: async ({ userId, role }: { userId: string; role: string }) => {
      const res = await fetch(`${apiBase}/api/v1/org/members/${userId}/role`, {
        method: 'PUT',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ role }),
      });
      if (!res.ok) throw new Error('Role change failed');
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['org-info'] }),
  });

  // ── v0.11.0: ownership transfer ──
  const me = useMe();
  const myUserId = me.data?.user_id;
  const orgId = data?.org?.id;
  const [showTransferDialog, setShowTransferDialog] = useState(false);

  const { data: pendingTransfer } = useQuery({
    queryKey: ['owner-transfer', orgId],
    queryFn: async () => {
      const res = await fetch(`${apiBase}/api/v1/orgs/${orgId}/owner/transfer`, { headers });
      if (!res.ok) return null;
      const body = await res.json();
      // GET returns the pending transfer, or {} when none is pending.
      return body && body.transfer_id ? body : null;
    },
    enabled: !!orgId,
  });

  const transferActionMutation = useMutation({
    mutationFn: async ({ transferId, action }: { transferId: number; action: 'accept' | 'cancel' }) => {
      const res = await fetch(
        `${apiBase}/api/v1/orgs/${orgId}/owner/transfer/${transferId}/${action}`,
        { method: 'POST', headers },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error?.message || body?.detail || `Could not ${action} the transfer.`);
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['owner-transfer', orgId] });
      queryClient.invalidateQueries({ queryKey: ['org-info'] });
      queryClient.invalidateQueries({ queryKey: ['me'] });
    },
  });

  if (isLoading) {
    return <div className="text-center py-12 text-text-tertiary">Loading…</div>;
  }

  const org = data?.org;
  const members = data?.members || [];
  const role = data?.current_user_role;
  const isOwner = role === 'owner';
  // Owners can manage members too (owner sits above admin).
  const isAdmin = role === 'admin' || role === 'owner';
  const pendingInvites = invites?.invites || [];
  // Ownership can only be transferred to an existing admin.
  const adminCandidates = members
    .filter((m: any) => m.role === 'admin')
    .map((m: any) => ({ user_id: m.user_id, email: m.email }));

  if (!org) {
    return (
      <div className="max-w-2xl w-full mx-auto px-4 py-12">
        <div className="text-center mb-8">
          <h2 className="text-xl font-semibold text-text-primary mb-4">No Organization</h2>
          <p className="text-text-tertiary mb-6">
            You're not part of an organization. Organizations are available on Team tier and above.
          </p>
          <p className="text-sm text-text-tertiary">
            Or create one via CLI: <code className="bg-surface border border-border px-2 py-1 rounded-md text-text-secondary">sfs org create "My Team" my-team</code>
          </p>
        </div>
        <ActivateLicensePanel />
      </div>
    );
  }

  // GitHub #92 — a represented-persona badge can deep-link to a secondary
  // org's personas via ?org=<id>. Show that org instead of the primary one.
  const requestedOrg = searchParams.get('org');
  if (requestedOrg && requestedOrg !== org.id) {
    return <SecondaryOrgPage orgId={requestedOrg} />;
  }

  const visibleTabs: OrgTab[] = ['members'];
  if (org.id && (isAdmin || myUserId)) visibleTabs.push('personas');
  if (org.id) visibleTabs.push('policies');
  if (org.id && isAdmin) visibleTabs.push('security');
  if (org.id && isAdmin) visibleTabs.push('integrations');
  const requestedTab = searchParams.get('tab');
  const activeTab: OrgTab = visibleTabs.includes(requestedTab as OrgTab) ? (requestedTab as OrgTab) : 'members';
  const selectTab = (key: string) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set('tab', key);
        return next;
      },
      { replace: true },
    );
  };

  const TAB_LABELS: Record<OrgTab, string> = {
    members: 'Members',
    personas: 'Personas',
    policies: 'Policies',
    security: 'Security',
    integrations: 'Integrations',
  };

  const tabContent: Record<OrgTab, ReactNode> = {
    members: (
      <div>
        {isAdmin && (
          <div className="flex items-center justify-end gap-2 mb-4">
            {isOwner && (
              <Button variant="secondary" onClick={() => setShowTransferDialog(true)}>
                Transfer ownership
              </Button>
            )}
            <button
              onClick={() => setShowInviteForm(!showInviteForm)}
              className="bg-brand text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-[var(--brand-hover)] transition-colors"
            >
              Invite Member
            </button>
          </div>
        )}
      {/* Invite form */}
      {showInviteForm && isAdmin && (
        <div className="bg-bg-elevated border border-border rounded-xl p-5 mb-6">
          <h2 className="text-sm font-medium text-text-primary mb-3">Invite a teammate</h2>
          <div className="flex gap-3 items-end">
            <div className="flex-1">
              <label className="block text-sm text-text-tertiary mb-1">Email</label>
              <input
                type="email"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                placeholder="colleague@company.com"
                className="w-full bg-surface border border-border rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
              />
            </div>
            <div>
              <label className="block text-sm text-text-tertiary mb-1">Role</label>
              <Dropdown
                menuLabel="Select role"
                minWidthClass="min-w-[140px]"
                trigger={(open) => (
                  <button
                    type="button"
                    aria-haspopup="menu"
                    aria-expanded={open}
                    className="flex items-center justify-between gap-2 w-[140px] bg-surface border border-border rounded-lg px-3 py-2 text-sm text-text-secondary outline-none focus-visible:border-[var(--brand)] focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                  >
                    <span>{inviteRole === 'admin' ? 'Admin' : 'Member'}</span>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-text-tertiary">
                      <polyline points="6 9 12 15 18 9" />
                    </svg>
                  </button>
                )}
                items={[
                  { key: 'member', label: 'Member' },
                  { key: 'admin', label: 'Admin' },
                ]}
                onSelect={(key) => setInviteRole(key)}
              />
            </div>
            <button
              onClick={() => inviteMutation.mutate()}
              disabled={!inviteEmail || inviteMutation.isPending}
              className="bg-brand text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50"
            >
              {inviteMutation.isPending ? 'Sending…' : 'Send Invite'}
            </button>
          </div>
          {inviteMutation.isError && (
            <p className="text-danger text-sm mt-2">{errorMessage(inviteMutation.error)}</p>
          )}
        </div>
      )}

      {/* Members */}
      <div className="bg-bg-elevated border border-border rounded-xl overflow-hidden mb-6">
        <div className="px-5 py-3 border-b border-border">
          <h2 className="text-lg font-semibold text-text-primary">Members</h2>
        </div>
        <div className="divide-y divide-[var(--border)]">
          {members.map((m: any) => (
            <div key={m.user_id} className="px-5 py-3 flex items-center gap-3 hover:bg-surface-hover transition-colors">
              {/* Avatar */}
              <span className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-medium flex-shrink-0 ${getAvatarColor(m.email)}`}>
                {m.email.charAt(0).toUpperCase()}
              </span>

              {/* Info */}
              <div className="flex-1 min-w-0">
                <div className="text-base text-text-primary truncate">{m.email}</div>
                <div className="text-xs text-text-tertiary">{m.display_name || 'No display name'}</div>
              </div>

              {/* Role badge */}
              <span
                className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                  m.role === 'owner'
                    ? 'bg-warning/15 text-warning'
                    : m.role === 'admin'
                      ? 'bg-brand/15 text-brand'
                      : 'bg-[var(--border)] text-text-tertiary'
                }`}
              >
                {m.role}
              </span>

              {/* Joined */}
              <span className="hidden sm:block text-xs text-text-tertiary w-20 text-right flex-shrink-0">
                {m.joined_at?.slice(0, 10) || '-'}
              </span>

              {/* Actions — the owner row is immutable (transfer ownership to change). */}
              {isAdmin && m.role !== 'owner' && (
                <div className="flex gap-2 ml-2 flex-shrink-0">
                  <button
                    onClick={() =>
                      changeRoleMutation.mutate({
                        userId: m.user_id,
                        role: m.role === 'admin' ? 'member' : 'admin',
                      })
                    }
                    className="text-xs text-brand hover:underline"
                  >
                    {m.role === 'admin' ? 'Make member' : 'Make admin'}
                  </button>
                  <button
                    onClick={() => setRemoveTarget({ user_id: m.user_id, email: m.email })}
                    className="text-xs text-danger hover:underline"
                  >
                    Remove
                  </button>
                </div>
              )}
              {isAdmin && m.role === 'owner' && (
                <span className="text-xs text-text-tertiary ml-2 flex-shrink-0 italic">
                  Owner
                </span>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Pending invites */}
      {isAdmin && pendingInvites.length > 0 && (
        <div className="bg-bg-elevated border border-border rounded-xl overflow-hidden">
          <div className="px-5 py-3 border-b border-border">
            <h2 className="text-lg font-semibold text-text-primary">Pending Invites</h2>
          </div>
          <div className="divide-y divide-[var(--border)]">
            {pendingInvites.map((inv: any) => (
              <div key={inv.id} className="px-5 py-3 flex items-center gap-3">
                <span className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-medium flex-shrink-0 ${getAvatarColor(inv.email)}`}>
                  {inv.email.charAt(0).toUpperCase()}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-base text-text-primary truncate">{inv.email}</div>
                </div>
                <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-warning/10 text-warning">
                  {inv.role}
                </span>
                <span className="text-xs text-text-tertiary">
                  Sent {inv.created_at?.slice(0, 10)}
                </span>
                <span className="text-xs text-text-tertiary">
                  Expires {inv.expires_at?.slice(0, 10)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      </div>
    ),
    personas: (
      <>
      {/* GH #70 — org-level personas (admins and owners manage them).
          GitHub #81 — members see and edit the personas that represent them. */}
      {org?.id && (isAdmin || myUserId) && (
        <OrgPersonasSection
          orgId={org.id}
          isAdmin={isAdmin}
          myUserId={myUserId}
          members={members.map((m: OrgPersonaMember) => ({ user_id: m.user_id, email: m.email }))}
          className="bg-bg-elevated border border-border rounded-xl p-5"
        />
      )}

      </>
    ),
    policies: (
      <>
      {/* v0.10.0 Phase 6 — org defaults (retention, compile model, KB knobs). */}
      {org?.id && (
        <div className="bg-bg-elevated border border-border rounded-xl p-5">
          <OrgSettingsTab orgId={org.id} canEdit={isAdmin} />
        </div>
      )}

      {/* Migration 084 — org-wide helper LLM key (owner/admin). The card
          drops to a "remove or disable" state once the plan no longer
          includes the helper, so it is rendered for admins on every tier. */}
      {org?.id && isAdmin && (
        <div className="bg-bg-elevated border border-border rounded-xl p-5 mt-6">
          <OrgHelperKeyCard orgId={org.id} />
        </div>
      )}

      </>
    ),
    security: (
      <>
      {/* v0.13.x — SSO (OIDC) admin configuration. */}
      {org?.id && isAdmin && (
        <div className="bg-bg-elevated border border-border rounded-xl p-5">
          <SsoTab orgId={org.id} canEdit={isAdmin} isOwner={isOwner} />
        </div>
      )}

      </>
    ),
    integrations: (
      <>
      {/* Jira integration (Enterprise). Admins on lower tiers see an upgrade
          note inside the card; members never see this tab at all. */}
      {org?.id && (
        <JiraCard orgId={org.id} tier={org.tier} />
      )}

      </>
    ),
  };

  return (
    <PageContainer>
      <PageHeader
        title={org.name}
        description={`${org.slug} \u00b7 ${org.tier} tier \u00b7 ${org.seats_used}/${org.seats_limit} seats`}
      />

      {/* v0.11.0 — pending ownership transfer banner */}
      {pendingTransfer && (
        <div className="bg-bg-elevated border border-brand/40 rounded-xl p-5 mb-6">
          {pendingTransfer.to_user_id === myUserId ? (
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h2 className="text-base font-semibold text-text-primary">
                  You've been offered ownership of {org.name}
                </h2>
                <p className="text-sm text-text-tertiary">
                  Accept to become the organization owner, or decline to leave ownership unchanged.
                </p>
                {pendingTransfer.expires_at && (
                  <p className="text-xs text-text-tertiary mt-1">
                    Expires {pendingTransfer.expires_at.slice(0, 10)}
                  </p>
                )}
              </div>
              <div className="flex gap-2 shrink-0">
                <Button
                  onClick={() =>
                    transferActionMutation.mutate({ transferId: pendingTransfer.transfer_id, action: 'accept' })
                  }
                  disabled={transferActionMutation.isPending}
                >
                  Accept ownership
                </Button>
                <Button
                  variant="ghost"
                  onClick={() =>
                    transferActionMutation.mutate({ transferId: pendingTransfer.transfer_id, action: 'cancel' })
                  }
                  disabled={transferActionMutation.isPending}
                >
                  Decline
                </Button>
              </div>
            </div>
          ) : pendingTransfer.from_user_id === myUserId ? (
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h2 className="text-base font-semibold text-text-primary">
                  Ownership transfer pending
                </h2>
                <p className="text-sm text-text-tertiary">
                  Waiting for{' '}
                  {members.find((m: any) => m.user_id === pendingTransfer.to_user_id)?.email ||
                    'the new owner'}{' '}
                  to accept. You'll become an admin once they do.
                </p>
                {pendingTransfer.expires_at && (
                  <p className="text-xs text-text-tertiary mt-1">
                    Expires {pendingTransfer.expires_at.slice(0, 10)}
                  </p>
                )}
              </div>
              <Button
                variant="ghost"
                onClick={() =>
                  transferActionMutation.mutate({ transferId: pendingTransfer.transfer_id, action: 'cancel' })
                }
                disabled={transferActionMutation.isPending}
                className="shrink-0"
              >
                Cancel transfer
              </Button>
            </div>
          ) : (
            <p className="text-sm text-text-tertiary">
              An ownership transfer is pending for this organization.
            </p>
          )}
          {transferActionMutation.isError && (
            <p className="text-danger text-sm mt-2">
              {errorMessage(transferActionMutation.error)}
            </p>
          )}
        </div>
      )}

      <Tabs
        aria-label="Organization sections"
        tabs={visibleTabs.map((key) => ({ key, label: TAB_LABELS[key], content: tabContent[key] }))}
        activeKey={activeTab}
        onChange={selectTab}
      />

      {isOwner && orgId && (
        <OwnerTransferDialog
          open={showTransferDialog}
          onClose={() => setShowTransferDialog(false)}
          orgId={orgId}
          admins={adminCandidates}
          onInitiated={() =>
            queryClient.invalidateQueries({ queryKey: ['owner-transfer', orgId] })
          }
        />
      )}

      <Dialog open={!!removeTarget} onClose={() => setRemoveTarget(null)} titleId="remove-member-title">
        <DialogHeader titleId="remove-member-title">Remove member</DialogHeader>
        <p className="text-sm text-text-secondary mb-6">
          {removeTarget ? `Remove ${removeTarget.email} from the organization?` : ''}
        </p>
        <DialogFooter>
          <Button variant="secondary" onClick={() => setRemoveTarget(null)}>Cancel</Button>
          <Button
            variant="danger"
            onClick={() => {
              if (removeTarget) removeMutation.mutate(removeTarget.user_id);
              setRemoveTarget(null);
            }}
          >
            Remove
          </Button>
        </DialogFooter>
      </Dialog>
    </PageContainer>
  );
}
