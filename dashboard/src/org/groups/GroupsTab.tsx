import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Alert, Badge, Button, Card } from '../../components/ui';
import type { ProjectGroupSummary } from '../../api/client';
import {
  UPGRADE_MESSAGE,
  groupErrorMessage,
  groupPath,
  groupRoleLabel,
  groupRoleVariant,
  orgHasProjectGroups,
} from '../../utils/groups';
import CreateGroupDialog from './CreateGroupDialog';
import { useOrgGroups } from './useProjectGroups';

interface GroupsTabProps {
  orgId: string;
  /** Org admin or owner: may create groups and sees every group. */
  isOrgAdmin: boolean;
  /** The org's plan tier, when known (gates creation). */
  tier?: string | null;
}

/**
 * Organization → Groups. Org admins see every group and can create them;
 * other members see the groups they hold a grant on.
 */
function plural(n: number, one: string, many: string) {
  return `${n} ${n === 1 ? one : many}`;
}

export default function GroupsTab({ orgId, isOrgAdmin, tier }: GroupsTabProps) {
  const navigate = useNavigate();
  const groups = useOrgGroups(orgId);
  const [creating, setCreating] = useState(false);
  const planAllows = orgHasProjectGroups(tier);

  return (
    <Card level="elevated" className="rounded-xl">
      <section aria-labelledby="org-groups-heading" className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-3 mb-4">
          <div className="min-w-0 max-w-2xl">
            <h2 id="org-groups-heading" className="text-lg font-semibold text-text-primary">
              Project groups
            </h2>
            <p className="mt-1 text-sm text-text-tertiary">
              Groups collect org projects and give chosen members read or edit access to them.
              They matter most when the organization restricts project visibility.
            </p>
          </div>
          {isOrgAdmin && planAllows && (
            <Button onClick={() => setCreating(true)}>New group</Button>
          )}
        </div>

        {isOrgAdmin && !planAllows && (
          <Alert variant="info" title="Available on Team and Enterprise" className="mb-4">
            {UPGRADE_MESSAGE} Upgrade to create groups and grant access. Existing groups stay
            readable, and you can still remove access from them.
          </Alert>
        )}

        {groups.isLoading ? (
          <p className="text-sm text-text-tertiary py-6 text-center">Loading groups…</p>
        ) : groups.isError ? (
          <Alert variant="danger">Couldn’t load groups: {groupErrorMessage(groups.error)}</Alert>
        ) : (groups.data ?? []).length === 0 ? (
          <div className="rounded-lg border border-dashed border-border px-4 py-8 text-center">
            <p className="text-sm font-medium text-text-primary">No project groups yet</p>
            <p className="mt-1 text-sm text-text-tertiary">
              {isOrgAdmin
                ? planAllows
                  ? 'Create a group, add projects to it, then grant members reader, editor or admin access.'
                  : 'Groups become available when the organization is on the Team or Enterprise plan.'
                : 'You haven’t been given access to any project groups in this organization.'}
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-[var(--border)] rounded-lg border border-border" aria-label="Project groups">
            {(groups.data ?? []).map((g: ProjectGroupSummary) => (
              <li key={g.id} className="flex flex-wrap items-start gap-x-4 gap-y-2 px-4 py-3">
                <div className="min-w-0 flex-1 basis-64">
                  <Link
                    to={groupPath(orgId, g.id)}
                    className="font-medium text-text-primary hover:text-brand hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded-sm"
                  >
                    {g.name}
                  </Link>
                  <div className="font-mono text-xs text-text-tertiary truncate">{g.slug}</div>
                  {g.description && (
                    <p className="mt-1 text-sm text-text-secondary line-clamp-2">{g.description}</p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-3 text-sm text-text-secondary tabular-nums">
                  <span>{plural(g.project_count, 'project', 'projects')}</span>
                  <span aria-hidden="true" className="text-text-tertiary">·</span>
                  <span>{plural(g.member_count, 'person', 'people')}</span>
                  {g.my_role && (
                    <Badge variant={groupRoleVariant(g.my_role)} label={`You: ${groupRoleLabel(g.my_role)}`} tint />
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      <CreateGroupDialog
        orgId={orgId}
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(g) => {
          setCreating(false);
          navigate(groupPath(orgId, g.id));
        }}
      />
    </Card>
  );
}
