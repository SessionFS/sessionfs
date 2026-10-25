import { ApiError } from '../api/client';
import { isOrgAdminRole } from './roles';

/**
 * Built-in Jira story templates (server side: `templates.py`). Custom
 * templates are a later piece; the org admin picks one of these when
 * connecting and a project mapping may override it.
 */
export const JIRA_TEMPLATES: { value: string; label: string }[] = [
  { value: 'agile', label: 'Agile Standard' },
  { value: 'compliance', label: 'Enterprise / Compliance' },
  { value: 'sre', label: 'Platform / SRE' },
  { value: 'bug', label: 'Bug Report' },
];

export function jiraTemplateLabel(slug: string | null | undefined): string {
  return JIRA_TEMPLATES.find((t) => t.value === slug)?.label ?? slug ?? '—';
}

/**
 * The Jira routes raise `{"error": "<code>", "message": ...}` inside the
 * FastAPI detail, which the global handler re-envelopes as
 * `{error: {code: "<status>", message, details: {error: "<code>"}}}`. The
 * dashboard's ApiError therefore carries the numeric status in `code` and the
 * semantic Jira code in `details.error` — read the latter when it exists so
 * callers can branch on `jira_project_unmapped` etc.
 */
export function jiraErrorCode(err: unknown): string | undefined {
  if (!(err instanceof ApiError)) return undefined;
  const details = err.details as { error?: unknown } | undefined;
  if (details && typeof details.error === 'string') return details.error;
  return err.code;
}

/**
 * Jira promotion is gated on the user's *effective* tier being Enterprise.
 * Legacy accounts still carry `tier: 'admin'` (pre-migration), which the
 * backend resolves to enterprise — mirror that here so they are not locked
 * out of a feature they are paying for.
 */
export function hasJiraEnterpriseTier(
  me: { tier: string; effective_tier: string } | null | undefined,
): boolean {
  if (!me) return false;
  return me.effective_tier === 'enterprise' || me.tier === 'admin';
}

/**
 * Project-admin rule for the ticket's Unlink control, mirroring the server's
 * `user_is_project_admin`: the project owner, or an admin/owner of the org
 * that scopes the project. The org role must come from the full membership
 * list (`myOrgs`) for the *project's* org — never `me.org_id`, which only
 * describes the primary org and would wrongly deny secondary-org admins.
 */
export function canUnlinkTicket(
  project: { owner_id: string; org_id?: string | null } | null | undefined,
  me: { user_id: string } | null | undefined,
  myOrgs: { org_id: string; role?: string }[] | null | undefined,
): boolean {
  if (!project || !me) return false;
  if (project.owner_id === me.user_id) return true;
  const orgId = project.org_id;
  if (!orgId) return false;
  const membership = myOrgs?.find((o) => o.org_id === orgId);
  return isOrgAdminRole(membership?.role);
}
