import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { getAvatarColor } from '../utils/avatar';

export default function OrgPage() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  const headers = { Authorization: `Bearer ${auth?.apiKey ?? ''}` };
  const apiBase = auth?.baseUrl || (window as any).__SFS_API_URL__ || '';

  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('member');
  const [showInviteForm, setShowInviteForm] = useState(false);

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
    enabled: data?.current_user_role === 'admin',
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

  if (isLoading) {
    return <div className="text-center py-12 text-[var(--text-tertiary)]">Loading...</div>;
  }

  const org = data?.org;
  const members = data?.members || [];
  const isAdmin = data?.current_user_role === 'admin';
  const pendingInvites = invites?.invites || [];

  if (!org) {
    return (
      <div className="max-w-2xl mx-auto px-4 py-12 text-center">
        <h2 className="text-xl font-semibold text-[var(--text-primary)] mb-4">No Organization</h2>
        <p className="text-[var(--text-tertiary)] mb-6">
          You're not part of an organization. Organizations are available on Team tier and above.
        </p>
        <p className="text-sm text-[var(--text-tertiary)]">
          Create one via CLI: <code className="bg-[var(--surface)] border border-[var(--border)] px-2 py-1 rounded-md text-[var(--text-secondary)]">sfs org create "My Team" my-team</code>
        </p>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto px-4 py-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-[var(--text-primary)]">{org.name}</h1>
          <p className="text-[15px] text-[var(--text-secondary)]">
            {org.slug} &middot; {org.tier} tier &middot; {org.seats_used}/{org.seats_limit} seats
          </p>
        </div>
        {isAdmin && (
          <button
            onClick={() => setShowInviteForm(!showInviteForm)}
            className="bg-[var(--brand)] text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-[var(--brand-hover)] transition-colors"
          >
            Invite Member
          </button>
        )}
      </div>

      {/* Invite form */}
      {showInviteForm && isAdmin && (
        <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5 mb-6">
          <h3 className="text-sm font-medium text-[var(--text-primary)] mb-3">Invite a teammate</h3>
          <div className="flex gap-3 items-end">
            <div className="flex-1">
              <label className="block text-[13px] text-[var(--text-tertiary)] mb-1">Email</label>
              <input
                type="email"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                placeholder="colleague@company.com"
                className="w-full bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-tertiary)] focus:outline-none focus:border-[var(--brand)]"
              />
            </div>
            <div>
              <label className="block text-[13px] text-[var(--text-tertiary)] mb-1">Role</label>
              <select
                value={inviteRole}
                onChange={(e) => setInviteRole(e.target.value)}
                className="bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)]"
              >
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
            </div>
            <button
              onClick={() => inviteMutation.mutate()}
              disabled={!inviteEmail || inviteMutation.isPending}
              className="bg-[var(--brand)] text-white rounded-lg px-5 py-2.5 text-sm font-semibold hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50"
            >
              {inviteMutation.isPending ? 'Sending...' : 'Send Invite'}
            </button>
          </div>
          {inviteMutation.isError && (
            <p className="text-red-500 text-sm mt-2">{(inviteMutation.error as Error).message}</p>
          )}
        </div>
      )}

      {/* Members */}
      <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl overflow-hidden mb-6">
        <div className="px-5 py-3 border-b border-[var(--border)]">
          <h3 className="text-lg font-semibold text-[var(--text-primary)]">Members</h3>
        </div>
        <div className="divide-y divide-[var(--border)]">
          {members.map((m: any) => (
            <div key={m.user_id} className="px-5 py-3 flex items-center gap-3 hover:bg-[var(--surface-hover)] transition-colors">
              {/* Avatar */}
              <span className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-medium flex-shrink-0 ${getAvatarColor(m.email)}`}>
                {m.email.charAt(0).toUpperCase()}
              </span>

              {/* Info */}
              <div className="flex-1 min-w-0">
                <div className="text-[14px] text-[var(--text-primary)] truncate">{m.email}</div>
                <div className="text-xs text-[var(--text-tertiary)]">{m.display_name || 'No display name'}</div>
              </div>

              {/* Role badge */}
              <span
                className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                  m.role === 'admin'
                    ? 'bg-[var(--brand)]/15 text-[var(--brand)]'
                    : 'bg-[var(--border)] text-[var(--text-tertiary)]'
                }`}
              >
                {m.role}
              </span>

              {/* Joined */}
              <span className="text-xs text-[var(--text-tertiary)] w-20 text-right flex-shrink-0">
                {m.joined_at?.slice(0, 10) || '-'}
              </span>

              {/* Actions */}
              {isAdmin && (
                <div className="flex gap-2 ml-2 flex-shrink-0">
                  <button
                    onClick={() =>
                      changeRoleMutation.mutate({
                        userId: m.user_id,
                        role: m.role === 'admin' ? 'member' : 'admin',
                      })
                    }
                    className="text-xs text-[var(--brand)] hover:underline"
                  >
                    {m.role === 'admin' ? 'Make Member' : 'Make Admin'}
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`Remove ${m.email} from the organization?`)) {
                        removeMutation.mutate(m.user_id);
                      }
                    }}
                    className="text-xs text-red-500 hover:underline"
                  >
                    Remove
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Pending invites */}
      {isAdmin && pendingInvites.length > 0 && (
        <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl overflow-hidden">
          <div className="px-5 py-3 border-b border-[var(--border)]">
            <h3 className="text-lg font-semibold text-[var(--text-primary)]">Pending Invites</h3>
          </div>
          <div className="divide-y divide-[var(--border)]">
            {pendingInvites.map((inv: any) => (
              <div key={inv.id} className="px-5 py-3 flex items-center gap-3">
                <span className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-medium flex-shrink-0 ${getAvatarColor(inv.email)}`}>
                  {inv.email.charAt(0).toUpperCase()}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-[14px] text-[var(--text-primary)] truncate">{inv.email}</div>
                </div>
                <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-yellow-500/10 text-yellow-500">
                  {inv.role}
                </span>
                <span className="text-xs text-[var(--text-tertiary)]">
                  Sent {inv.created_at?.slice(0, 10)}
                </span>
                <span className="text-xs text-[var(--text-tertiary)]">
                  Expires {inv.expires_at?.slice(0, 10)}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
