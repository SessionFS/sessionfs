import { Link } from 'react-router-dom';
import { Alert, Badge, Card } from '../components/ui';
import { useMyOrgs } from '../transfers/useTransfers';
import { groupErrorMessage, groupPath, groupRoleLabel, groupRoleVariant } from '../utils/groups';
import { useMyGroups } from '../org/groups/useProjectGroups';

/**
 * Settings → Account → "Your groups" (GitHub #101): every project group the
 * caller has standing on, across their orgs. Hidden for people outside any
 * org, who can't be in a group.
 */
export default function YourGroupsCard({ inOrg }: { inOrg: boolean }) {
  const groups = useMyGroups(inOrg);
  const orgs = useMyOrgs();
  if (!inOrg) return null;

  const orgName = (orgId: string) => orgs.data?.find((o) => o.org_id === orgId)?.name;
  const list = groups.data ?? [];
  const multiOrg = new Set(list.map((g) => g.org_id)).size > 1;

  return (
    <Card level="elevated" className="p-5">
      <section aria-labelledby="your-groups-heading">
        <h2 id="your-groups-heading" className="text-lg font-semibold text-text-primary mb-1">
          Your groups
        </h2>
        <p className="text-sm text-text-tertiary mb-3">
          Project groups you have access to. Org admins and owners manage every group in their organization.
        </p>
        {groups.isLoading ? (
          <p className="text-sm text-text-tertiary">Loading groups…</p>
        ) : groups.isError ? (
          <Alert variant="danger">Couldn’t load your groups: {groupErrorMessage(groups.error)}</Alert>
        ) : list.length === 0 ? (
          <p className="text-sm text-text-secondary">You aren’t in any project groups yet.</p>
        ) : (
          <ul className="divide-y divide-[var(--border)] rounded-lg border border-border">
            {list.map((g) => (
              <li key={g.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5">
                <div className="min-w-0 flex-1 basis-40">
                  <Link
                    to={groupPath(g.org_id, g.id)}
                    className="block truncate text-sm font-medium text-text-primary hover:text-brand hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded-sm"
                  >
                    {g.name}
                  </Link>
                  <span className="block truncate text-xs text-text-tertiary">
                    {multiOrg && orgName(g.org_id) ? `${orgName(g.org_id)} · ` : ''}
                    {g.project_count} {g.project_count === 1 ? 'project' : 'projects'}
                  </span>
                </div>
                {g.my_role && <Badge variant={groupRoleVariant(g.my_role)} label={groupRoleLabel(g.my_role)} tint />}
              </li>
            ))}
          </ul>
        )}
      </section>
    </Card>
  );
}
