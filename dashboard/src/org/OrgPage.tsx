import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

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
    return <div className="text-center py-12 text-text-muted">Loading...</div>;
  }

  const org = data?.org;
  const members = data?.members || [];
  const isAdmin = data?.current_user_role === 'admin';
  const pendingInvites = invites?.invites || [];

  if (!org) {
    return (
      <div className="max-w-2xl mx-auto px-4 py-12 text-center">
        <h2 className="text-xl font-semibold mb-4">No Organization</h2>
        <p className="text-text-muted mb-6">
          You're not part of an organization. Organizations are available on Team tier and above.
        </p>
        <p className="text-sm text-text-muted">
          Create one via CLI: <code className="bg-bg-secondary px-2 py-1 rounded">sfs org create "My Team" my-team</code>
        </p>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto px-4 py-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">{org.name}</h1>
          <p className="text-text-muted text-sm">
            {org.slug} &middot; {org.tier} tier &middot; {org.seats_used}/{org.seats_limit} seats
          </p>
        </div>
        {isAdmin && (
          <button
            onClick={() => setShowInviteForm(!showInviteForm)}
            className="px-4 py-2 bg-accent text-white rounded-lg hover:bg-accent/90 text-sm"
          >
            Invite Member
          </button>
        )}
      </div>

      {/* Invite form */}
      {showInviteForm && isAdmin && (
        <div className="bg-bg-secondary rounded-lg p-4 mb-6">
          <div className="flex gap-3 items-end">
            <div className="flex-1">
              <label className="block text-sm text-text-muted mb-1">Email</label>
              <input
                type="email"
                value={inviteEmail}
                onChange={(e) => setInviteEmail(e.target.value)}
                placeholder="colleague@company.com"
                className="w-full px-3 py-2 bg-bg-primary border border-border rounded-lg text-sm"
              />
            </div>
            <div>
              <label className="block text-sm text-text-muted mb-1">Role</label>
              <select
                value={inviteRole}
                onChange={(e) => setInviteRole(e.target.value)}
                className="px-3 py-2 bg-bg-primary border border-border rounded-lg text-sm"
              >
                <option value="member">Member</option>
                <option value="admin">Admin</option>
              </select>
            </div>
            <button
              onClick={() => inviteMutation.mutate()}
              disabled={!inviteEmail || inviteMutation.isPending}
              className="px-4 py-2 bg-accent text-white rounded-lg hover:bg-accent/90 disabled:opacity-50 text-sm"
            >
              {inviteMutation.isPending ? 'Sending...' : 'Send Invite'}
            </button>
          </div>
          {inviteMutation.isError && (
            <p className="text-red-400 text-sm mt-2">{(inviteMutation.error as Error).message}</p>
          )}
        </div>
      )}

      {/* Members table */}
      <div className="bg-bg-secondary rounded-lg overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-text-muted">
              <th className="px-4 py-3">Email</th>
              <th className="px-4 py-3">Name</th>
              <th className="px-4 py-3">Role</th>
              <th className="px-4 py-3">Joined</th>
              {isAdmin && <th className="px-4 py-3">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {members.map((m: any) => (
              <tr key={m.user_id} className="border-b border-border/50">
                <td className="px-4 py-3">{m.email}</td>
                <td className="px-4 py-3 text-text-muted">{m.display_name || '-'}</td>
                <td className="px-4 py-3">
                  <span
                    className={`px-2 py-0.5 rounded text-xs font-medium ${
                      m.role === 'admin' ? 'bg-accent/20 text-accent' : 'bg-border text-text-muted'
                    }`}
                  >
                    {m.role}
                  </span>
                </td>
                <td className="px-4 py-3 text-text-muted">{m.joined_at?.slice(0, 10) || '-'}</td>
                {isAdmin && (
                  <td className="px-4 py-3">
                    <div className="flex gap-2">
                      <button
                        onClick={() =>
                          changeRoleMutation.mutate({
                            userId: m.user_id,
                            role: m.role === 'admin' ? 'member' : 'admin',
                          })
                        }
                        className="text-xs text-accent hover:underline"
                      >
                        {m.role === 'admin' ? 'Make Member' : 'Make Admin'}
                      </button>
                      <button
                        onClick={() => {
                          if (confirm(`Remove ${m.email} from the organization?`)) {
                            removeMutation.mutate(m.user_id);
                          }
                        }}
                        className="text-xs text-red-400 hover:underline"
                      >
                        Remove
                      </button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pending invites */}
      {isAdmin && pendingInvites.length > 0 && (
        <div className="mt-8">
          <h2 className="text-lg font-semibold mb-3">Pending Invites</h2>
          <div className="bg-bg-secondary rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-text-muted">
                  <th className="px-4 py-3">Email</th>
                  <th className="px-4 py-3">Role</th>
                  <th className="px-4 py-3">Sent</th>
                  <th className="px-4 py-3">Expires</th>
                </tr>
              </thead>
              <tbody>
                {pendingInvites.map((inv: any) => (
                  <tr key={inv.id} className="border-b border-border/50">
                    <td className="px-4 py-3">{inv.email}</td>
                    <td className="px-4 py-3">{inv.role}</td>
                    <td className="px-4 py-3 text-text-muted">{inv.created_at?.slice(0, 10)}</td>
                    <td className="px-4 py-3 text-text-muted">{inv.expires_at?.slice(0, 10)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
