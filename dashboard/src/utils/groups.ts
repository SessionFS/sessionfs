import { ApiError, type ProjectGroupRole } from '../api/client';
import { errorMessage } from './errorMessage';

/**
 * Project groups (GitHub #101) — shared helpers for the org Groups tab, the
 * group detail page, the restricted-visibility setting and the project /
 * account group lists.
 */

/** A group's detail page. `org` keeps the link valid for any of the caller's orgs. */
export function groupPath(orgId: string, groupId: string): string {
  return `/settings/organization/groups/${encodeURIComponent(groupId)}?org=${encodeURIComponent(orgId)}`;
}

export const GROUP_ROLE_OPTIONS: { value: ProjectGroupRole; label: string }[] = [
  { value: 'reader', label: 'Reader' },
  { value: 'editor', label: 'Editor' },
  { value: 'admin', label: 'Admin' },
];

export const GROUP_ROLE_HINTS: Record<ProjectGroupRole, string> = {
  reader: 'can read the group’s projects.',
  editor: 'can read and change the group’s projects.',
  admin: 'can change the group’s projects and manage the group.',
};

export function groupRoleLabel(role: string | null | undefined): string {
  return GROUP_ROLE_OPTIONS.find((o) => o.value === role)?.label ?? '—';
}

/** Badge colour per role: admins stand out, readers stay neutral. */
export function groupRoleVariant(role: string | null | undefined): 'info' | 'success' | 'default' {
  if (role === 'admin') return 'info';
  if (role === 'editor') return 'success';
  return 'default';
}

/** Mirrors the server's `_slugify` (routes/project_groups.py). */
export function slugifyGroupName(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 100)
    .replace(/-+$/, '');
}

const SLUG_RE = /^[a-z0-9][a-z0-9-]*$/;

/** The server's slug rule: 1–100 lowercase letters, digits and hyphens, not starting with a hyphen. */
export function slugError(slug: string): string | undefined {
  if (!slug) return 'Enter a slug.';
  if (slug.length > 100) return 'Use at most 100 characters.';
  if (!SLUG_RE.test(slug)) return 'Use lowercase letters, numbers and hyphens, starting with a letter or number.';
  return undefined;
}

/** Plans known to lack the `project_groups` feature (server: tiers.py). */
const TIERS_WITHOUT_GROUPS = new Set(['free', 'starter', 'pro']);

/**
 * Whether an org plan tier includes project groups. Only the known lower
 * tiers are blocked; anything else (not loaded, an org other than the
 * caller's primary one, or a tier this dashboard doesn't know yet) is
 * allowed, and the server's `upgrade_required` answer — mapped to the same
 * notice by `isUpgradeRequired` — decides.
 */
export function orgHasProjectGroups(tier: string | null | undefined): boolean {
  if (!tier) return true;
  return !TIERS_WITHOUT_GROUPS.has(tier);
}

/**
 * The plan gate raises `{"error": "upgrade_required", ...}` inside the detail,
 * which the global handler re-envelopes as `{error: {code: "403", details:
 * {error: "upgrade_required"}}}`; newer handlers may send the code directly.
 */
export function isUpgradeRequired(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  const details = err.details as { error?: unknown } | undefined;
  return err.code === 'upgrade_required' || details?.error === 'upgrade_required';
}

export const UPGRADE_MESSAGE = 'Project groups need the Team or Enterprise plan.';

/** A user-facing message for a project-group API error. */
export function groupErrorMessage(err: unknown, fallback = 'Something went wrong. Try again.'): string {
  if (isUpgradeRequired(err)) return UPGRADE_MESSAGE;
  if (err instanceof ApiError) {
    switch (err.code) {
      case 'not_org_member':
        return 'That person is not a member of this organization, so they can’t be given access.';
      case 'project_not_in_org':
        return 'Only projects that belong to this organization can join the group.';
      case 'project_edit_forbidden':
        return 'You can only add projects you can edit. Ask an org admin to add this one.';
      case 'grant_conflict':
      case 'group_project_conflict':
      case 'concurrent_update':
        return 'Someone else changed this group at the same moment. Try again.';
    }
    if (err.status === 409) return 'A group with that slug already exists in this organization. Choose another slug.';
    if (err.status === 404) return 'This group or project no longer exists, or you no longer have access to it.';
    if (err.status === 403) return 'You don’t have permission to do that.';
  }
  return errorMessage(err, fallback);
}
