/**
 * Project group detail (GitHub #101): /settings/organization/groups/:groupId?org=<orgId>.
 *
 * Group admins (an explicit admin grant, or any org admin/owner) edit the
 * group, its projects and who has access; readers and editors see the same
 * page read-only. Only org admins delete a group.
 *
 * Group knowledge search and the group context document are planned as
 * separate sections; they would mount between the header and "Projects".
 */

import { useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ApiError } from '../../api/client';
import { Alert, Badge, Button, PageContainer, PageHeader } from '../../components/ui';
import { useMe } from '../../hooks/useMe';
import { useToast } from '../../hooks/useToast';
import { isOrgAdminRole } from '../../utils/roles';
import { groupErrorMessage, groupRoleLabel, groupRoleVariant, orgHasProjectGroups } from '../../utils/groups';
import { useOrgMembers } from '../useOrgMembers';
import ConfirmDialog from './ConfirmDialog';
import EditGroupDialog from './EditGroupDialog';
import GroupAccessSection from './GroupAccessSection';
import GroupMeetingsSection from './GroupMeetingsSection';
import GroupProjectsSection from './GroupProjectsSection';
import { usePrimaryOrgInfo } from './usePrimaryOrgInfo';
import { useDeleteGroup, useProjectGroup } from './useProjectGroups';

export default function GroupDetailPage() {
  const { groupId } = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { addToast } = useToast();
  const me = useMe();
  const primary = usePrimaryOrgInfo();

  const orgId = searchParams.get('org') || me.data?.org_id || primary.data?.org?.id || undefined;
  const group = useProjectGroup(orgId, groupId);
  const members = useOrgMembers(orgId);
  const del = useDeleteGroup(orgId, groupId);

  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const isPrimaryOrg = !!orgId && primary.data?.org?.id === orgId;
  // The plan tier is only known for the primary org; elsewhere the server's
  // upgrade_required answer drives the same notice.
  const planAllows = orgHasProjectGroups(isPrimaryOrg ? primary.data?.org?.tier : undefined);
  // A secondary org's groups live on its own Organization view (?org=).
  const backTo =
    isPrimaryOrg || !orgId
      ? '/settings/organization?tab=groups'
      : `/settings/organization?org=${encodeURIComponent(orgId)}&tab=groups`;
  const backLabel = '← Groups';
  const breadcrumb = (
    <Link to={backTo} className="hover:underline">
      {backLabel}
    </Link>
  );

  if (!orgId && (me.isLoading || primary.isLoading)) {
    return <div className="text-center py-12 text-text-tertiary">Loading…</div>;
  }

  if (!orgId || group.isError) {
    const notFound = !orgId || (group.error instanceof ApiError && group.error.status === 404);
    return (
      <PageContainer>
        <PageHeader title="Project group" breadcrumb={breadcrumb} />
        <Alert variant="danger">
          {notFound
            ? 'This group doesn’t exist, was deleted, or you don’t have access to it.'
            : `Couldn’t load this group: ${groupErrorMessage(group.error)}`}
        </Alert>
      </PageContainer>
    );
  }

  if (group.isLoading || !group.data) {
    return <div className="text-center py-12 text-text-tertiary">Loading…</div>;
  }

  const g = group.data;
  const meetingOpen = !!searchParams.get('meeting');
  // The members roster carries the caller's role in this org; for the primary
  // org /org answers too (and may land first, or alone if the roster fails).
  const isOrgAdmin = isOrgAdminRole(
    members.data?.current_user_role ?? (isPrimaryOrg ? primary.data?.current_user_role : undefined),
  );
  const canManage = g.my_role === 'admin';

  // The caller revoked their own grant: the server answers with an empty shell.
  if (!g.my_role) {
    return (
      <PageContainer>
        <PageHeader title={g.name} breadcrumb={breadcrumb} />
        <Alert variant="info" title="You no longer have access to this group">
          Your grant was removed. Ask a group admin if you need access again.
        </Alert>
      </PageContainer>
    );
  }

  return (
    <PageContainer>
      <PageHeader
        breadcrumb={breadcrumb}
        title={g.name}
        description={
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-mono">{g.slug}</span>
            <span aria-hidden="true">·</span>
            <span>
              Your role: <Badge variant={groupRoleVariant(g.my_role)} label={groupRoleLabel(g.my_role)} tint />
            </span>
          </span>
        }
        actions={
          canManage || isOrgAdmin ? (
            <>
              {canManage && (
                <Button variant="secondary" onClick={() => setEditing(true)}>
                  Edit
                </Button>
              )}
              {isOrgAdmin && (
                <Button
                  variant="danger"
                  onClick={() => {
                    del.reset();
                    setConfirmDelete(true);
                  }}
                >
                  Delete
                </Button>
              )}
            </>
          ) : undefined
        }
      />

      {g.description && <p className="-mt-2 mb-5 max-w-3xl text-sm text-text-secondary whitespace-pre-line">{g.description}</p>}

      {!canManage && (
        <Alert variant="info" className="mb-5">
          You have {groupRoleLabel(g.my_role).toLowerCase()} access to this group. Group admins manage its projects
          and who has access.
        </Alert>
      )}

      <div className="space-y-5">
        {/* An open meeting (a helper citation, a shared link) leads the page.
            The two sections are moved in the DOM (not with CSS order), so
            reading and tab order match what is on screen; the stable keys
            make it a move, not a remount, so the warnings of an upload that
            just finished survive the switch. */}
        {(meetingOpen ? ['meetings', 'projects'] : ['projects', 'meetings']).map((key) =>
          key === 'projects' ? (
            <GroupProjectsSection
              key="projects"
              group={g}
              canManage={canManage}
              isOrgAdmin={isOrgAdmin}
              planAllows={planAllows}
            />
          ) : (
            <GroupMeetingsSection key="meetings" group={g} canAdmin={canManage || isOrgAdmin} />
          ),
        )}
        <GroupAccessSection
          group={g}
          canManage={canManage}
          isOrgAdmin={isOrgAdmin}
          planAllows={planAllows}
          orgMembers={members.data?.members}
          orgMembersLoading={members.isLoading}
          myUserId={me.data?.user_id}
        />
      </div>

      {editing && <EditGroupDialog group={g} open onClose={() => setEditing(false)} />}

      <ConfirmDialog
        open={confirmDelete}
        title="Delete group"
        confirmLabel="Delete group"
        danger
        pending={del.isPending}
        error={del.isError ? groupErrorMessage(del.error) : null}
        onClose={() => setConfirmDelete(false)}
        onConfirm={() =>
          del.mutate(undefined, {
            onSuccess: () => {
              addToast('success', `Deleted ${g.name}.`);
              navigate(backTo);
            },
          })
        }
      >
        <p>
          Delete <strong className="text-text-primary">{g.name}</strong>? When the organization restricts project
          visibility, everyone who reaches a project only through this group loses that access. The projects
          themselves are not changed, and the slug <span className="font-mono">{g.slug}</span> becomes free to
          reuse.
        </p>
      </ConfirmDialog>
    </PageContainer>
  );
}
