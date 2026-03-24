import { useState, useCallback } from 'react';
import {
  useAdminUsers,
  useAdminStats,
  useAdminActionLog,
  useAdminChangeTier,
  useAdminVerifyUser,
  useAdminDeleteUser,
} from '../hooks/useAdmin';
import type { AdminUser } from '../api/client';
import RelativeDate from '../components/RelativeDate';
import ConfirmModal from './ConfirmModal';

const TIER_COLORS: Record<string, string> = {
  free: 'bg-gray-500/20 text-gray-400',
  pro: 'bg-blue-500/20 text-blue-400',
  team: 'bg-purple-500/20 text-purple-400',
  admin: 'bg-red-500/20 text-red-400',
};

const TIERS = ['free', 'pro', 'team', 'admin'] as const;

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

export default function AdminDashboard() {
  const [emailSearch, setEmailSearch] = useState('');
  const [expandedUserId, setExpandedUserId] = useState<string | null>(null);
  const [pendingTier, setPendingTier] = useState<Record<string, string>>({});
  const [confirmDelete, setConfirmDelete] = useState<AdminUser | null>(null);

  const { data: stats, isLoading: statsLoading } = useAdminStats();
  const { data: usersData, isLoading: usersLoading, error: usersError } = useAdminUsers({
    page_size: 100,
    email: emailSearch || undefined,
  });
  const { data: actionsData } = useAdminActionLog({ page_size: 20 });

  const changeTier = useAdminChangeTier();
  const verifyUser = useAdminVerifyUser();
  const deleteUser = useAdminDeleteUser();

  const handleToggleExpand = useCallback((userId: string) => {
    setExpandedUserId((prev) => (prev === userId ? null : userId));
  }, []);

  const handleChangeTier = useCallback(
    (userId: string) => {
      const tier = pendingTier[userId];
      if (!tier) return;
      changeTier.mutate({ userId, tier });
    },
    [pendingTier, changeTier],
  );

  const handleVerify = useCallback(
    (userId: string) => {
      verifyUser.mutate({ userId });
    },
    [verifyUser],
  );

  const handleDeleteConfirm = useCallback(() => {
    if (!confirmDelete) return;
    deleteUser.mutate({ userId: confirmDelete.user_id });
    setConfirmDelete(null);
    setExpandedUserId(null);
  }, [confirmDelete, deleteUser]);

  return (
    <div className="max-w-7xl mx-auto px-4 py-4">
      <h1 className="text-xl font-semibold text-text-primary mb-6">Admin Dashboard</h1>

      {/* Stats Overview */}
      <section className="mb-8">
        <h2 className="text-sm font-medium text-text-secondary uppercase tracking-wider mb-3">
          Overview
        </h2>
        {statsLoading && (
          <div className="text-text-muted text-sm">Loading stats...</div>
        )}
        {stats && (
          <>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-4">
              <StatCard label="Total Users" value={stats.users.total} />
              <StatCard label="Verified Users" value={stats.users.verified} />
              <StatCard label="Total Sessions" value={stats.sessions.total} />
              <StatCard label="Storage Used" value={formatBytes(stats.sessions.total_size_bytes)} />
              <StatCard label="Pending Handoffs" value={stats.handoffs.pending} />
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {/* Tier breakdown */}
              <div className="border border-border rounded-lg p-4 bg-bg-secondary">
                <h3 className="text-sm font-medium text-text-secondary uppercase tracking-wider mb-3">
                  Users by Tier
                </h3>
                <div className="space-y-2">
                  {Object.entries(stats.users.by_tier).map(([tier, count]) => (
                    <div key={tier} className="flex items-center justify-between text-sm">
                      <span className="text-text-secondary capitalize">{tier}</span>
                      <span className={`px-2 py-0.5 rounded text-sm font-medium ${TIER_COLORS[tier] || 'bg-gray-500/20 text-gray-400'}`}>
                        {count}
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Tool breakdown */}
              <div className="border border-border rounded-lg p-4 bg-bg-secondary">
                <h3 className="text-sm font-medium text-text-secondary uppercase tracking-wider mb-3">
                  Sessions by Tool
                </h3>
                <div className="space-y-2">
                  {Object.entries(stats.sessions.by_tool)
                    .sort(([, a], [, b]) => b - a)
                    .map(([tool, count]) => {
                      const maxCount = Math.max(...Object.values(stats.sessions.by_tool));
                      const pct = maxCount > 0 ? (count / maxCount) * 100 : 0;
                      return (
                        <div key={tool} className="text-sm">
                          <div className="flex items-center justify-between mb-1">
                            <span className="text-text-secondary">{tool}</span>
                            <span className="text-text-muted tabular-nums">{count}</span>
                          </div>
                          <div className="h-1.5 bg-bg-tertiary rounded-full overflow-hidden">
                            <div
                              className="h-full bg-accent rounded-full transition-all"
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                        </div>
                      );
                    })}
                  {Object.keys(stats.sessions.by_tool).length === 0 && (
                    <div className="text-text-muted text-sm">No sessions yet</div>
                  )}
                </div>
              </div>
            </div>
          </>
        )}
      </section>

      {/* User Management */}
      <section className="mb-8">
        <h2 className="text-sm font-medium text-text-secondary uppercase tracking-wider mb-3">
          User Management
        </h2>
        <div className="mb-3">
          <input
            type="text"
            value={emailSearch}
            onChange={(e) => setEmailSearch(e.target.value)}
            placeholder="Search by email..."
            className="w-full max-w-sm px-3 py-1.5 bg-bg-secondary border border-border rounded text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent"
          />
        </div>

        {usersError && (
          <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded text-red-400 text-sm">
            Failed to load users: {String(usersError)}
          </div>
        )}

        {usersLoading && (
          <div className="text-text-muted text-sm">Loading users...</div>
        )}

        {usersData && usersData.users.length > 0 && (
          <div className="border border-border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-bg-secondary text-text-secondary text-sm uppercase tracking-wider">
                  <th className="px-3 py-2 text-left">Email</th>
                  <th className="px-3 py-2 text-left w-24">Tier</th>
                  <th className="px-3 py-2 text-center w-20">Verified</th>
                  <th className="px-3 py-2 text-right w-20">Sessions</th>
                  <th className="px-3 py-2 text-left w-28">Created</th>
                </tr>
              </thead>
              <tbody>
                {usersData.users.map((user) => (
                  <UserRow
                    key={user.user_id}
                    user={user}
                    expanded={expandedUserId === user.user_id}
                    pendingTier={pendingTier[user.user_id]}
                    onToggle={() => handleToggleExpand(user.user_id)}
                    onPendingTierChange={(tier) =>
                      setPendingTier((prev) => ({ ...prev, [user.user_id]: tier }))
                    }
                    onSaveTier={() => handleChangeTier(user.user_id)}
                    onVerify={() => handleVerify(user.user_id)}
                    onDelete={() => setConfirmDelete(user)}
                    isSavingTier={changeTier.isPending}
                    isVerifying={verifyUser.isPending}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {usersData && usersData.users.length === 0 && !usersLoading && (
          <div className="text-center py-8 text-text-muted text-sm">No users found</div>
        )}

        {usersData && (
          <div className="mt-2 text-sm text-text-muted">
            Showing {usersData.users.length} of {usersData.total} users
          </div>
        )}
      </section>

      {/* Recent Admin Actions */}
      <section>
        <h2 className="text-sm font-medium text-text-secondary uppercase tracking-wider mb-3">
          Recent Admin Actions
        </h2>
        {actionsData && actionsData.actions.length > 0 ? (
          <div className="border border-border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-bg-secondary text-text-secondary text-sm uppercase tracking-wider">
                  <th className="px-3 py-2 text-left">Admin</th>
                  <th className="px-3 py-2 text-left">Action</th>
                  <th className="px-3 py-2 text-left">Target</th>
                  <th className="px-3 py-2 text-left w-28">When</th>
                </tr>
              </thead>
              <tbody>
                {actionsData.actions.map((action) => (
                  <tr key={action.id} className="border-t border-border">
                    <td className="px-3 py-2 text-text-secondary">{action.admin_email}</td>
                    <td className="px-3 py-2 text-text-primary">{action.action}</td>
                    <td className="px-3 py-2 text-text-muted font-mono text-sm">{action.target}</td>
                    <td className="px-3 py-2 text-text-muted text-sm">
                      <RelativeDate iso={action.timestamp} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="text-center py-8 text-text-muted text-sm">No recent actions</div>
        )}
      </section>

      {/* Delete confirmation modal */}
      <ConfirmModal
        open={!!confirmDelete}
        title="Delete User"
        message={
          confirmDelete
            ? `Are you sure you want to delete ${confirmDelete.email}? This will permanently remove the user and all their data. This action cannot be undone.`
            : ''
        }
        confirmLabel="Delete User"
        destructive
        onConfirm={handleDeleteConfirm}
        onCancel={() => setConfirmDelete(null)}
      />
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="border border-border rounded-lg p-3 bg-bg-secondary">
      <div className="text-sm text-text-muted mb-1">{label}</div>
      <div className="text-lg font-semibold text-text-primary tabular-nums">{value}</div>
    </div>
  );
}

interface UserRowProps {
  user: AdminUser;
  expanded: boolean;
  pendingTier: string | undefined;
  onToggle: () => void;
  onPendingTierChange: (tier: string) => void;
  onSaveTier: () => void;
  onVerify: () => void;
  onDelete: () => void;
  isSavingTier: boolean;
  isVerifying: boolean;
}

function UserRow({
  user,
  expanded,
  pendingTier,
  onToggle,
  onPendingTierChange,
  onSaveTier,
  onVerify,
  onDelete,
  isSavingTier,
  isVerifying,
}: UserRowProps) {
  const tierColor = TIER_COLORS[user.tier] || 'bg-gray-500/20 text-gray-400';
  const selectedTier = pendingTier ?? user.tier;
  const tierChanged = selectedTier !== user.tier;

  return (
    <>
      <tr
        onClick={onToggle}
        className="border-t border-border hover:bg-bg-tertiary cursor-pointer transition-colors"
      >
        <td className="px-3 py-2 text-text-primary">{user.email}</td>
        <td className="px-3 py-2">
          <span className={`px-2 py-0.5 rounded text-sm font-medium ${tierColor}`}>
            {user.tier}
          </span>
        </td>
        <td className="px-3 py-2 text-center">
          {user.email_verified ? (
            <span className="text-green-400" title="Verified">&#10003;</span>
          ) : (
            <span className="text-red-400" title="Not verified">&#10007;</span>
          )}
        </td>
        <td className="px-3 py-2 text-right text-text-secondary tabular-nums">
          {user.session_count}
        </td>
        <td className="px-3 py-2 text-text-muted text-sm">
          <RelativeDate iso={user.created_at} />
        </td>
      </tr>
      {expanded && (
        <tr className="border-t border-border bg-bg-tertiary">
          <td colSpan={5} className="px-4 py-3">
            <div className="flex flex-wrap items-center gap-4">
              {/* Change Tier */}
              <div className="flex items-center gap-2">
                <label className="text-sm text-text-muted">Tier:</label>
                <select
                  value={selectedTier}
                  onChange={(e) => onPendingTierChange(e.target.value)}
                  className="px-2 py-1 bg-bg-secondary border border-border rounded text-sm text-text-secondary focus:outline-none"
                >
                  {TIERS.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
                {tierChanged && (
                  <button
                    onClick={(e) => { e.stopPropagation(); onSaveTier(); }}
                    disabled={isSavingTier}
                    className="px-3 py-1 text-sm bg-accent hover:bg-accent/90 text-white rounded transition-colors disabled:opacity-50"
                  >
                    {isSavingTier ? 'Saving...' : 'Save'}
                  </button>
                )}
              </div>

              {/* Verify Email */}
              {!user.email_verified && (
                <button
                  onClick={(e) => { e.stopPropagation(); onVerify(); }}
                  disabled={isVerifying}
                  className="px-3 py-1 text-sm bg-green-600 hover:bg-green-700 text-white rounded transition-colors disabled:opacity-50"
                >
                  {isVerifying ? 'Verifying...' : 'Verify Email'}
                </button>
              )}

              {/* Delete User */}
              <button
                onClick={(e) => { e.stopPropagation(); onDelete(); }}
                className="px-3 py-1 text-sm bg-red-600 hover:bg-red-700 text-white rounded transition-colors"
              >
                Delete User
              </button>

              <span className="text-sm text-text-muted ml-auto">
                ID: <span className="font-mono">{user.user_id}</span>
              </span>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
