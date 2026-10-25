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
 * Jira promotion is an Enterprise feature. It is *project-scoped*: the plan
 * that pays for the project (its org's, or its owner's for personal projects)
 * decides it, not whichever org the caller happens to have as their primary.
 * So a Team-seat user opening a project owned by an Enterprise org may still
 * promote tickets there.
 *
 * The project response carries its own `effective_tier` (tk_792504a140604e62);
 * older servers omit it, in which case fall back to the caller's effective
 * tier, then their raw tier. Legacy accounts still carry `tier: 'admin'`
 * (pre-migration), which the backend resolves to enterprise — mirror that
 * here, but only in that fallback: a project with its own effective_tier gates
 * even platform admins, matching `check_project_feature`.
 */
export function hasJiraEnterpriseTier(
  project: { effective_tier?: string | null } | null | undefined,
  me: { tier?: string | null; effective_tier?: string | null } | null | undefined,
): boolean {
  // A newer server reports the project's own effective_tier, and that is the
  // single source of truth: a platform admin gets no bypass here, because the
  // backend's `check_project_feature` has none either.
  if (project?.effective_tier) return project.effective_tier === 'enterprise';

  // Older servers omit the project's effective_tier, in which case fall back to
  // the caller's tier. Legacy accounts still carry `tier: 'admin'`
  // (pre-migration), which the backend resolves to enterprise — mirror that
  // here so they are not locked out of a feature they are paying for.
  if (me?.tier === 'admin') return true;
  const tier = me?.effective_tier || me?.tier || 'free';
  return tier === 'enterprise';
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
