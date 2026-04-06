import { useState, useCallback, useRef, useEffect } from 'react';
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
import LicensesTab from './LicensesTab';

type AdminTab = 'users' | 'licenses' | 'activity';

const TIER_COLORS: Record<string, string> = {
  free: 'bg-gray-500/15 text-gray-500',
  starter: 'bg-green-500/15 text-green-500',
  pro: 'bg-blue-500/15 text-blue-500',
  team: 'bg-purple-500/15 text-purple-500',
  enterprise: 'bg-amber-500/15 text-amber-500',
  admin: 'bg-red-500/15 text-red-500',
};

const TIERS = ['free', 'starter', 'pro', 'team', 'enterprise', 'admin'] as const;

const AVATAR_COLORS = [
  'bg-blue-500/20 text-blue-500',
  'bg-green-500/20 text-green-500',
  'bg-purple-500/20 text-purple-500',
  'bg-orange-500/20 text-orange-500',
  'bg-pink-500/20 text-pink-500',
  'bg-teal-500/20 text-teal-500',
];

function getAvatarColor(email: string): string {
  let hash = 0;
  for (let i = 0; i < email.length; i++) {
    hash = email.charCodeAt(i) + ((hash << 5) - hash);
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

export default function AdminDashboard() {
  const [activeTab, setActiveTab] = useState<AdminTab>('users');
  const [emailSearch, setEmailSearch] = useState('');
  const [expandedUserId, setExpandedUserId] = useState<string | null>(null);
  const [pendingTier, setPendingTier] = useState<Record<string, string>>({});
  const [confirmDelete, setConfirmDelete] = useState<AdminUser | null>(null);

  const { data: stats, isLoading: statsLoading } = useAdminStats();
  const { data: usersData, isLoading: usersLoading, error: usersError } = useAdminUsers({
    page_size: 100,
    search: emailSearch || undefined,
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
    deleteUser.mutate({ userId: confirmDelete.id });
    setConfirmDelete(null);
    setExpandedUserId(null);
  }, [confirmDelete, deleteUser]);

  const tabs: { key: AdminTab; label: string }[] = [
    { key: 'users', label: 'Users' },
    { key: 'licenses', label: 'Licenses' },
    { key: 'activity', label: 'Activity' },
  ];

  // Compute storage percentage for progress bar
  const storageUsed = stats?.sessions?.total_size_bytes ?? 0;
  const storageLimit = 500 * 1024 * 1024; // Default 500MB
  const storagePct = storageLimit > 0 ? Math.min(100, (storageUsed / storageLimit) * 100) : 0;

  // Compute active tools count
  const activeTools = stats ? Object.keys(stats.sessions?.by_tool ?? {}).length : 0;

  return (
    <div className="max-w-7xl mx-auto px-4 py-6">
      <h1 className="text-3xl font-bold tracking-tight text-[var(--text-primary)] mb-6">Admin</h1>

      {/* Overview Cards */}
      {statsLoading && (
        <div className="text-[var(--text-tertiary)] text-sm mb-6">Loading stats...</div>
      )}
      {stats && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
          {/* Users card */}
          <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 rounded-lg bg-blue-500/10 flex items-center justify-center flex-shrink-0">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--info)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                  <circle cx="9" cy="7" r="4" />
                  <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
                  <path d="M16 3.13a4 4 0 0 1 0 7.75" />
                </svg>
              </div>
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--text-tertiary)] mb-1">Users</p>
                <p className="text-3xl font-bold text-[var(--text-primary)] tabular-nums">{stats.users.total}</p>
                <p className="text-xs text-[var(--text-tertiary)] mt-1">
                  {stats.users.verified} verified, {stats.users.total - stats.users.verified} pending
                </p>
              </div>
            </div>
          </div>

          {/* Sessions card */}
          <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 rounded-lg bg-green-500/10 flex items-center justify-center flex-shrink-0">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
                  <line x1="8" y1="21" x2="16" y2="21" />
                  <line x1="12" y1="17" x2="12" y2="21" />
                </svg>
              </div>
              <div>
                <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--text-tertiary)] mb-1">Sessions</p>
                <p className="text-3xl font-bold text-[var(--text-primary)] tabular-nums">{stats.sessions.total}</p>
                <p className="text-xs text-[var(--text-tertiary)] mt-1">
                  {activeTools} active tool{activeTools !== 1 ? 's' : ''}
                </p>
              </div>
            </div>
          </div>

          {/* Storage card */}
          <div className="bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl p-5">
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 rounded-lg bg-purple-500/10 flex items-center justify-center flex-shrink-0">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#8b5cf6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <ellipse cx="12" cy="5" rx="9" ry="3" />
                  <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
                  <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
                </svg>
              </div>
              <div className="flex-1">
                <p className="text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--text-tertiary)] mb-1">Storage</p>
                <p className="text-3xl font-bold text-[var(--text-primary)] tabular-nums">{formatBytes(storageUsed)}</p>
                {storageLimit > 0 && (
                  <>
                    <div className="w-full h-1.5 bg-[var(--border)] rounded-full mt-2 overflow-hidden">
                      <div
                        className="h-full bg-purple-500 rounded-full transition-all"
                        style={{ width: `${storagePct}%` }}
                      />
                    </div>
                    <p className="text-xs text-[var(--text-tertiary)] mt-1">
                      {storagePct.toFixed(0)}% of {formatBytes(storageLimit)}
                    </p>
                  </>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Tab Navigation */}
      <div className="flex gap-0 border-b border-[var(--border)] mb-6">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setActiveTab(t.key)}
            className={`px-4 py-2 text-[14px] font-medium transition-colors ${
              activeTab === t.key
                ? 'text-[var(--text-primary)] border-b-2 border-[var(--brand)] -mb-px'
                : 'text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === 'licenses' && <LicensesTab />}

      {/* Activity tab */}
      {activeTab === 'activity' && (
        <section>
          {actionsData && actionsData.actions.length > 0 ? (
            <div className="border border-[var(--border)] rounded-xl overflow-hidden">
              <table className="w-full text-[14px]">
                <thead>
                  <tr className="bg-[var(--bg-elevated)] text-[13px] font-semibold text-[var(--text-tertiary)]">
                    <th className="px-4 py-3 text-left">Admin</th>
                    <th className="px-4 py-3 text-left">Action</th>
                    <th className="px-4 py-3 text-left">Target</th>
                    <th className="px-4 py-3 text-left w-28">When</th>
                  </tr>
                </thead>
                <tbody>
                  {actionsData.actions.map((action) => (
                    <tr key={action.id} className="border-t border-[var(--border)] hover:bg-[var(--surface-hover)] transition-colors">
                      <td className="px-4 py-3 text-[var(--text-secondary)] font-mono text-xs">{action.admin_id.slice(0, 8)}</td>
                      <td className="px-4 py-3 text-[var(--text-primary)]">{action.action}</td>
                      <td className="px-4 py-3 text-[var(--text-tertiary)] font-mono text-xs">{action.target_type}:{action.target_id.slice(0, 12)}</td>
                      <td className="px-4 py-3 text-[var(--text-tertiary)] text-xs">
                        <RelativeDate iso={action.created_at} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-center py-12 text-[var(--text-tertiary)] text-sm">No recent actions</div>
          )}
        </section>
      )}

      {/* User Management */}
      {activeTab === 'users' && (
        <section>
          <div className="mb-4">
            <input
              type="text"
              value={emailSearch}
              onChange={(e) => setEmailSearch(e.target.value)}
              placeholder="Search by email..."
              className="w-full max-w-sm bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-2 text-sm text-[var(--text-primary)] placeholder:text-[var(--text-tertiary)] focus:outline-none focus:border-[var(--brand)]"
            />
          </div>

          {usersError && (
            <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-red-500 text-sm">
              Failed to load users: {String(usersError)}
            </div>
          )}

          {usersLoading && (
            <div className="text-[var(--text-tertiary)] text-sm">Loading users...</div>
          )}

          {usersData && usersData.users.length > 0 && (
            <div className="border border-[var(--border)] rounded-xl overflow-hidden">
              <table className="w-full text-[14px]">
                <thead>
                  <tr className="bg-[var(--bg-elevated)] text-[13px] font-semibold text-[var(--text-tertiary)]">
                    <th className="px-4 py-3 text-left">User</th>
                    <th className="px-4 py-3 text-left w-24">Tier</th>
                    <th className="px-4 py-3 text-center w-20">Verified</th>
                    <th className="px-4 py-3 text-right w-20">Sessions</th>
                    <th className="px-4 py-3 text-left w-28">Created</th>
                    <th className="px-4 py-3 text-center w-12"></th>
                  </tr>
                </thead>
                <tbody>
                  {usersData.users.map((user) => (
                    <UserRow
                      key={user.id}
                      user={user}
                      expanded={expandedUserId === user.id}
                      pendingTier={pendingTier[user.id]}
                      onToggle={() => handleToggleExpand(user.id)}
                      onPendingTierChange={(tier) =>
                        setPendingTier((prev) => ({ ...prev, [user.id]: tier }))
                      }
                      onSaveTier={() => handleChangeTier(user.id)}
                      onVerify={() => handleVerify(user.id)}
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
            <div className="text-center py-12 text-[var(--text-tertiary)] text-sm">No users found</div>
          )}

          {usersData && (
            <div className="mt-3 text-sm text-[var(--text-tertiary)]">
              Showing {usersData.users.length} of {usersData.total} users
            </div>
          )}
        </section>
      )}

      {/* Delete confirmation modal */}
      <ConfirmModal
        open={!!confirmDelete}
        title="Delete User"
        message={
          confirmDelete
            ? `Are you sure you want to deactivate ${confirmDelete.email}? This will revoke API keys, remove org memberships, and disable the account.`
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

/* ------------------------------------------------------------------ */
/*  Action Menu                                                        */
/* ------------------------------------------------------------------ */

function ActionMenu({ children, onToggle }: { children: React.ReactNode; onToggle: () => void }) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  return (
    <div ref={menuRef} className="relative">
      <button
        onClick={(e) => {
          e.stopPropagation();
          setOpen(!open);
          onToggle();
        }}
        className="w-7 h-7 flex items-center justify-center rounded-md text-[var(--text-tertiary)] hover:text-[var(--text-primary)] hover:bg-[var(--surface-hover)] transition-colors"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
          <circle cx="8" cy="3" r="1.5" />
          <circle cx="8" cy="8" r="1.5" />
          <circle cx="8" cy="13" r="1.5" />
        </svg>
      </button>
      {open && (
        <div className="absolute right-0 top-8 z-20 bg-[var(--bg-elevated)] border border-[var(--border)] rounded-lg shadow-[var(--shadow-md)] py-1 min-w-[140px]">
          {children}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  User Row                                                           */
/* ------------------------------------------------------------------ */

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
  const tierColor = TIER_COLORS[user.tier] || 'bg-gray-500/15 text-gray-500';
  const selectedTier = pendingTier ?? user.tier;
  const tierChanged = selectedTier !== user.tier;
  const avatarColor = getAvatarColor(user.email);

  return (
    <>
      <tr
        className="border-t border-[var(--border)] hover:bg-[var(--surface-hover)] transition-colors"
      >
        <td className="px-4 py-3">
          <div className="flex items-center gap-2.5">
            <span className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-medium flex-shrink-0 ${avatarColor}`}>
              {user.email.charAt(0).toUpperCase()}
            </span>
            <span className="text-[var(--text-primary)] truncate">{user.email}</span>
          </div>
        </td>
        <td className="px-4 py-3">
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${tierColor}`}>
            {user.tier}
          </span>
        </td>
        <td className="px-4 py-3 text-center">
          {user.email_verified ? (
            <span className="text-green-500" title="Verified">&#10003;</span>
          ) : (
            <span className="text-red-500" title="Not verified">&#10007;</span>
          )}
        </td>
        <td className="px-4 py-3 text-right text-[var(--text-secondary)] tabular-nums">
          {user.session_count}
        </td>
        <td className="px-4 py-3 text-[var(--text-tertiary)] text-xs">
          <RelativeDate iso={user.created_at} />
        </td>
        <td className="px-4 py-3 text-center">
          <ActionMenu onToggle={() => {}}>
            <button
              onClick={(e) => { e.stopPropagation(); onToggle(); }}
              className="w-full text-left px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] transition-colors"
            >
              Manage
            </button>
            {!user.email_verified && (
              <button
                onClick={(e) => { e.stopPropagation(); onVerify(); }}
                disabled={isVerifying}
                className="w-full text-left px-3 py-1.5 text-sm text-green-500 hover:bg-[var(--surface-hover)] transition-colors disabled:opacity-50"
              >
                {isVerifying ? 'Verifying...' : 'Verify Email'}
              </button>
            )}
            <button
              onClick={(e) => { e.stopPropagation(); onDelete(); }}
              className="w-full text-left px-3 py-1.5 text-sm text-red-500 hover:bg-[var(--surface-hover)] transition-colors"
            >
              Delete User
            </button>
          </ActionMenu>
        </td>
      </tr>
      {expanded && (
        <tr className="border-t border-[var(--border)] bg-[var(--surface-hover)]">
          <td colSpan={6} className="px-4 py-4">
            <div className="flex flex-wrap items-center gap-4">
              {/* Change Tier */}
              <div className="flex items-center gap-2">
                <label className="text-sm text-[var(--text-tertiary)]">Tier:</label>
                <select
                  value={selectedTier}
                  onChange={(e) => onPendingTierChange(e.target.value)}
                  className="bg-[var(--surface)] border border-[var(--border)] rounded-lg px-3 py-1.5 text-sm text-[var(--text-secondary)] focus:outline-none focus:border-[var(--brand)]"
                >
                  {TIERS.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
                {tierChanged && (
                  <button
                    onClick={(e) => { e.stopPropagation(); onSaveTier(); }}
                    disabled={isSavingTier}
                    className="bg-[var(--brand)] text-white rounded-lg px-3 py-1.5 text-sm font-medium hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50"
                  >
                    {isSavingTier ? 'Saving...' : 'Save'}
                  </button>
                )}
              </div>

              <span className="text-xs text-[var(--text-tertiary)] ml-auto font-mono">
                ID: {user.id}
              </span>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
