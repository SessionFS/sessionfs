import { Link } from 'react-router-dom';
import { Badge } from '../components/ui';
import type { ProjectGroupSummary } from '../api/client';
import { groupPath, groupRoleLabel, groupRoleVariant } from '../utils/groups';

/**
 * Project settings → "Groups": the project groups that contain this project
 * (GitHub #101), from the `groups` field of the project detail. The server
 * only fills it for members of the project's org, so the block renders
 * nothing when the list is missing or empty. The caller frames it.
 */
export default function ProjectGroupsBlock({ groups }: { groups: ProjectGroupSummary[] | null | undefined }) {
  if (!groups || groups.length === 0) return null;
  return (
      <section aria-labelledby="settings-groups-heading">
        <h3 id="settings-groups-heading" className="text-sm font-semibold text-text-primary mb-1">
          Groups
        </h3>
        <p className="text-sm text-text-tertiary mb-3">
          Project groups that include this project. Their members get access through the group.
        </p>
        <ul className="flex flex-col gap-2">
          {groups.map((g) => (
            <li key={g.id} className="flex flex-wrap items-center gap-2">
              {g.my_role ? (
                <Link
                  to={groupPath(g.org_id, g.id)}
                  className="text-sm font-medium text-text-primary hover:text-brand hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded-sm"
                >
                  {g.name}
                </Link>
              ) : (
                // Without standing on the group its page would 404, so no link.
                <span className="text-sm font-medium text-text-primary">{g.name}</span>
              )}
              {g.my_role && (
                <Badge variant={groupRoleVariant(g.my_role)} label={`You: ${groupRoleLabel(g.my_role)}`} tint />
              )}
            </li>
          ))}
        </ul>
      </section>
  );
}
