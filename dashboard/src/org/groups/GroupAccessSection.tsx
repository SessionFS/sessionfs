import { useMemo, useState } from 'react';
import { Alert, Badge, Button, Card, Combobox, Select } from '../../components/ui';
import type { ProjectGroupDetail, ProjectGroupMember, ProjectGroupRole } from '../../api/client';
import { useToast } from '../../hooks/useToast';
import { isOrgAdminRole } from '../../utils/roles';
import {
  GROUP_ROLE_HINTS,
  GROUP_ROLE_OPTIONS,
  UPGRADE_MESSAGE,
  groupErrorMessage,
  groupRoleLabel,
  groupRoleVariant,
} from '../../utils/groups';
import type { OrgMemberInfo } from '../useOrgMembers';
import ConfirmDialog from './ConfirmDialog';
import { useGrantGroupMember, useRevokeGroupMember } from './useProjectGroups';

interface GroupAccessSectionProps {
  group: ProjectGroupDetail;
  /** Group admin standing: grant, change roles and revoke. */
  canManage: boolean;
  /** The caller is an org admin/owner, so they keep admin standing whatever their own grant says. */
  isOrgAdmin?: boolean;
  /** False when the org's plan lacks project groups: granting and role changes are blocked. */
  planAllows: boolean;
  /** The org's members, for the picker. Undefined while loading. */
  orgMembers: OrgMemberInfo[] | undefined;
  orgMembersLoading?: boolean;
  myUserId: string | undefined;
}

const roleOptions = GROUP_ROLE_OPTIONS.map((o) => ({ value: o.value, label: o.label }));

export default function GroupAccessSection({
  group,
  canManage,
  isOrgAdmin = false,
  planAllows,
  orgMembers,
  orgMembersLoading = false,
  myUserId,
}: GroupAccessSectionProps) {
  const { addToast } = useToast();
  const grant = useGrantGroupMember(group.org_id, group.id);
  const changeRole = useGrantGroupMember(group.org_id, group.id);
  const revoke = useRevokeGroupMember(group.org_id, group.id);

  const [grantee, setGrantee] = useState('');
  const [role, setRole] = useState<ProjectGroupRole>('reader');
  const [revokeTarget, setRevokeTarget] = useState<ProjectGroupMember | null>(null);
  const [selfDowngrade, setSelfDowngrade] = useState<{ member: ProjectGroupMember; role: ProjectGroupRole } | null>(
    null,
  );

  const orgRoleOf = useMemo(
    () => new Map((orgMembers ?? []).map((m) => [m.user_id, m.role])),
    [orgMembers],
  );

  const granted = useMemo(() => new Set(group.members.map((m) => m.user_id)), [group.members]);
  const candidates = useMemo(
    () =>
      (orgMembers ?? [])
        .filter((m) => !granted.has(m.user_id))
        .map((m) => ({
          value: m.user_id,
          label: m.display_name ? `${m.display_name} (${m.email})` : m.email,
        }))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [orgMembers, granted],
  );

  function handleGrant() {
    if (!grantee) return;
    const who = candidates.find((c) => c.value === grantee)?.label ?? 'Member';
    grant.mutate(
      { userId: grantee, role },
      {
        onSuccess: () => {
          setGrantee('');
          setRole('reader');
          addToast('success', `${who} now has ${groupRoleLabel(role).toLowerCase()} access.`);
        },
      },
    );
  }

  function applyRole(member: ProjectGroupMember, next: ProjectGroupRole, after?: () => void) {
    changeRole.mutate(
      { userId: member.user_id, role: next },
      {
        onSuccess: () => {
          after?.();
          addToast('success', `${member.email} is now ${groupRoleLabel(next).toLowerCase()}.`);
        },
        onError: (err) => {
          if (!after) addToast('error', groupErrorMessage(err));
        },
      },
    );
  }

  function handleRoleChange(member: ProjectGroupMember, next: string) {
    if (next === member.role) return;
    const role = next as ProjectGroupRole;
    // Lowering your own admin grant ends your manage rights unless you are
    // an org admin; only another group admin can give them back.
    if (member.user_id === myUserId && member.role === 'admin' && !isOrgAdmin) {
      changeRole.reset();
      setSelfDowngrade({ member, role });
      return;
    }
    applyRole(member, role);
  }

  function confirmRevoke() {
    if (!revokeTarget) return;
    const target = revokeTarget;
    revoke.mutate(target.user_id, {
      onSuccess: () => {
        setRevokeTarget(null);
        addToast('success', `Removed ${target.email}’s access.`);
      },
    });
  }

  return (
    <Card level="elevated" className="rounded-xl">
      <section aria-labelledby="group-access-heading" className="p-5">
        <h2 id="group-access-heading" className="text-lg font-semibold text-text-primary">
          Access
        </h2>
        <p className="mt-1 mb-4 text-sm text-text-tertiary">
          Readers can open the group’s projects, editors can also change them, and admins can manage this
          group. Org admins and owners manage every group without a grant.
        </p>

        {canManage && planAllows && (
          <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-end">
            <div className="min-w-0 flex-1">
              <Combobox
                title="Member"
                value={grantee}
                onValueChange={(v) => {
                  setGrantee(v);
                  grant.reset();
                }}
                options={candidates}
                placeholder={orgMembersLoading ? 'Loading members…' : 'Choose a member'}
                searchPlaceholder="Search by name or email…"
                loading={orgMembersLoading}
                emptyMessage="Every member of this organization already has access."
              />
            </div>
            <div className="sm:w-36">
              <Select
                title="Role"
                value={role}
                onValueChange={(v) => setRole(v as ProjectGroupRole)}
                options={roleOptions}
              />
            </div>
            <Button onClick={handleGrant} disabled={!grantee} loading={grant.isPending} className="shrink-0">
              Grant access
            </Button>
          </div>
        )}
        {canManage && planAllows && (
          <p className="-mt-2 mb-4 text-xs text-text-tertiary">
            {groupRoleLabel(role)}: {GROUP_ROLE_HINTS[role]}
          </p>
        )}
        {canManage && !planAllows && (
          <Alert variant="info" className="mb-4">
            {UPGRADE_MESSAGE} You can still remove people’s access.
          </Alert>
        )}
        {grant.isError && (
          <Alert variant="danger" className="mb-4">
            {groupErrorMessage(grant.error)}
          </Alert>
        )}

        {group.members.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-text-tertiary">
            {canManage
              ? 'Nobody has been granted access yet. Choose a member above.'
              : 'Nobody has been granted access to this group yet.'}
          </div>
        ) : (
          <ul className="divide-y divide-[var(--border)] rounded-lg border border-border" aria-label="People with access">
            {group.members.map((m) => {
              const isMe = m.user_id === myUserId;
              const orgAdminWithLowerGrant = isOrgAdminRole(orgRoleOf.get(m.user_id)) && m.role !== 'admin';
              return (
                <li key={m.user_id} className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
                  <div className="min-w-0 flex-1 basis-48">
                    <span className="block truncate text-sm text-text-primary">
                      {m.email}
                      {isMe && <span className="ml-1 text-text-tertiary">(you)</span>}
                    </span>
                    {orgAdminWithLowerGrant && canManage && planAllows && (
                      <span className="block text-xs text-text-tertiary">
                        Org admin (also {groupRoleLabel(m.role).toLowerCase()} grant): manages this group regardless.
                      </span>
                    )}
                  </div>
                  {canManage && planAllows ? (
                    <div className="w-32">
                      <Select
                        aria-label={`Role for ${m.email}`}
                        value={m.role}
                        onValueChange={(v) => handleRoleChange(m, v)}
                        options={roleOptions}
                        disabled={changeRole.isPending}
                      />
                    </div>
                  ) : (
                    <Badge
                      variant={groupRoleVariant(orgAdminWithLowerGrant ? 'admin' : m.role)}
                      label={
                        orgAdminWithLowerGrant
                          ? `Org admin (also ${groupRoleLabel(m.role).toLowerCase()} grant)`
                          : groupRoleLabel(m.role)
                      }
                      tint
                    />
                  )}
                  {canManage && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        revoke.reset();
                        setRevokeTarget(m);
                      }}
                      aria-label={`Revoke access for ${m.email}`}
                    >
                      Revoke
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <ConfirmDialog
        open={!!revokeTarget}
        title="Revoke access"
        confirmLabel="Revoke access"
        danger
        pending={revoke.isPending}
        error={revoke.isError ? groupErrorMessage(revoke.error) : null}
        onConfirm={confirmRevoke}
        onClose={() => setRevokeTarget(null)}
      >
        <p>
          Remove <strong className="text-text-primary">{revokeTarget?.email}</strong>’s{' '}
          {groupRoleLabel(revokeTarget?.role).toLowerCase()} access to {group.name}?
        </p>
        {revokeTarget?.user_id === myUserId && !isOrgAdmin && (
          <p>This is your own grant. Unless you are an org admin, you will lose access to this group.</p>
        )}
      </ConfirmDialog>

      <ConfirmDialog
        open={!!selfDowngrade}
        title="Lower your own role?"
        confirmLabel={`Make me ${groupRoleLabel(selfDowngrade?.role).toLowerCase()}`}
        danger
        pending={changeRole.isPending}
        error={selfDowngrade && changeRole.isError ? groupErrorMessage(changeRole.error) : null}
        onConfirm={() => {
          if (selfDowngrade) applyRole(selfDowngrade.member, selfDowngrade.role, () => setSelfDowngrade(null));
        }}
        onClose={() => setSelfDowngrade(null)}
      >
        <p>
          You are about to change your own access to {group.name} from admin to{' '}
          {groupRoleLabel(selfDowngrade?.role).toLowerCase()}.
        </p>
        <p>
          You will no longer be able to manage this group’s projects or people, and only another group admin or an
          org admin can give those rights back.
        </p>
      </ConfirmDialog>
    </Card>
  );
}
