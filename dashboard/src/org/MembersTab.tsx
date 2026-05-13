/**
 * v0.10.0 Phase 3b — Members management tab.
 *
 * Embedded in the v0.10.0 Org Admin Console (placement TBD in Phase
 * 4-7 — designed standalone so the URL surface that mounts it just
 * needs to pass an org_id).
 *
 * The remove flow exposes the CEO-mandated "data stays, access
 * revoked" wording in a confirm modal (KB entry 230 #3): sessions,
 * KB entries, and project ownership are preserved by the backend
 * `perform_member_removal()` service (Phase 3a). The modal copy
 * pins this user-facing contract so admins know what removal does
 * before they click through.
 */

import { useState } from 'react';

import { useMe } from '../hooks/useMe';
import { useToast } from '../hooks/useToast';
import {
  type OrgMemberInfo,
  useChangeMemberRole,
  useInviteMember,
  useOrgMembers,
  useRemoveMember,
} from './useOrgMembers';

interface MembersTabProps {
  orgId: string;
}

interface RemoveConfirmState {
  member: OrgMemberInfo;
}

export default function MembersTab({ orgId }: MembersTabProps) {
  const { data, isLoading, error } = useOrgMembers(orgId);
  const { data: me } = useMe();
  const { addToast } = useToast();
  const invite = useInviteMember(orgId);
  const changeRole = useChangeMemberRole(orgId);
  const remove = useRemoveMember(orgId);

  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState<'admin' | 'member'>('member');
  const [removeConfirm, setRemoveConfirm] = useState<RemoveConfirmState | null>(null);

  if (isLoading) return <p>Loading members…</p>;
  if (error) return <p role="alert">Failed to load members: {String(error)}</p>;
  if (!data) return null;

  const isAdmin = data.current_user_role === 'admin';
  const adminCount = data.members.filter((m) => m.role === 'admin').length;

  const handleInvite = (e: React.FormEvent) => {
    e.preventDefault();
    if (!inviteEmail.trim()) return;
    invite.mutate(
      { email: inviteEmail.trim(), role: inviteRole },
      {
        onSuccess: () => {
          setInviteEmail('');
          addToast('success', `Invitation sent to ${inviteEmail.trim()}`);
        },
        onError: (err) => addToast('error', `Invite failed: ${err.message}`),
      },
    );
  };

  const handlePromote = (m: OrgMemberInfo) => {
    changeRole.mutate(
      { userId: m.user_id, role: 'admin' },
      {
        onSuccess: () => addToast('success', `${m.email} is now an admin`),
        onError: (err) => addToast('error', `Role change failed: ${err.message}`),
      },
    );
  };

  const handleDemote = (m: OrgMemberInfo) => {
    changeRole.mutate(
      { userId: m.user_id, role: 'member' },
      {
        onSuccess: () => addToast('success', `${m.email} is now a member`),
        onError: (err) => addToast('error', `Role change failed: ${err.message}`),
      },
    );
  };

  const handleConfirmRemove = () => {
    if (!removeConfirm) return;
    const email = removeConfirm.member.email;
    remove.mutate(
      { userId: removeConfirm.member.user_id },
      {
        onSuccess: () => {
          setRemoveConfirm(null);
          addToast('success', `${email} removed from org`);
        },
        onError: (err) => addToast('error', `Remove failed: ${err.message}`),
      },
    );
  };

  return (
    <section aria-labelledby="members-heading">
      <h2 id="members-heading">Members ({data.seats_used} / {data.seats_limit})</h2>

      {isAdmin && (
        <form onSubmit={handleInvite} aria-label="Invite member">
          <label>
            Email
            <input
              type="email"
              value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)}
              required
            />
          </label>
          <label>
            Role
            <select
              value={inviteRole}
              onChange={(e) => setInviteRole(e.target.value as 'admin' | 'member')}
            >
              <option value="member">Member</option>
              <option value="admin">Admin</option>
            </select>
          </label>
          <button type="submit" disabled={invite.isPending || !inviteEmail.trim()}>
            {invite.isPending ? 'Inviting…' : 'Send invite'}
          </button>
        </form>
      )}

      <table>
        <thead>
          <tr>
            <th>Email</th>
            <th>Display name</th>
            <th>Role</th>
            {isAdmin && <th>Actions</th>}
          </tr>
        </thead>
        <tbody>
          {data.members.map((m) => {
            const isSelf = !!me && m.user_id === me.user_id;
            // Last-admin demotion guard (UI mirror of backend; backend
            // is load-bearing).
            const wouldDemoteLastAdmin =
              m.role === 'admin' && adminCount <= 1;
            return (
              <tr key={m.user_id}>
                <td>{m.email}</td>
                <td>{m.display_name ?? '—'}</td>
                <td>
                  <span aria-label={`Role: ${m.role}`}>{m.role}</span>
                </td>
                {isAdmin && (
                  <td>
                    {m.role === 'member' ? (
                      <button
                        onClick={() => handlePromote(m)}
                        disabled={changeRole.isPending}
                        aria-label={`Promote ${m.email} to admin`}
                      >
                        Promote
                      </button>
                    ) : (
                      <button
                        onClick={() => handleDemote(m)}
                        disabled={changeRole.isPending || wouldDemoteLastAdmin || isSelf}
                        title={
                          wouldDemoteLastAdmin
                            ? 'Cannot demote the last admin'
                            : isSelf
                              ? 'Cannot change your own role'
                              : undefined
                        }
                        aria-label={`Demote ${m.email} to member`}
                      >
                        Demote
                      </button>
                    )}
                    <button
                      onClick={() => setRemoveConfirm({ member: m })}
                      disabled={remove.isPending || isSelf}
                      title={isSelf ? 'Cannot remove yourself' : undefined}
                      aria-label={`Remove ${m.email}`}
                    >
                      Remove
                    </button>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>

      {removeConfirm && (
        <div role="dialog" aria-modal="true" aria-labelledby="remove-confirm-title">
          <h3 id="remove-confirm-title">
            Remove {removeConfirm.member.email}?
          </h3>
          <p>
            <strong>Access will be revoked. Data stays.</strong>
          </p>
          <ul>
            <li>Their sessions stay under their account (sessions are user-owned).</li>
            <li>
              Their org-scoped projects auto-transfer to you with an audit trail.
            </li>
            <li>
              Their knowledge-base entries stay in this org's KB with authorship preserved.
            </li>
            <li>
              Pending transfers tied to their org standing here will be cancelled.
            </li>
          </ul>
          <button
            onClick={handleConfirmRemove}
            disabled={remove.isPending}
            aria-label="Confirm remove member"
          >
            {remove.isPending ? 'Removing…' : 'Remove member'}
          </button>
          <button
            onClick={() => setRemoveConfirm(null)}
            disabled={remove.isPending}
          >
            Cancel
          </button>
        </div>
      )}
    </section>
  );
}
