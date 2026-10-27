import { ApiError, type ConfluenceDriftDetails } from '../api/client';
import { errorMessage } from './errorMessage';

/**
 * Built-in Confluence page templates (server side:
 * `integrations/docs/templates.py`). Only built-ins exist in the MVP — there
 * are no org-custom docs templates — so the pickers are fixed.
 */
export const CONFLUENCE_TEMPLATES: { value: string; label: string }[] = [
  { value: 'reference', label: 'Reference' },
  { value: 'architecture', label: 'Architecture' },
  { value: 'runbook', label: 'Runbook' },
  { value: 'onboarding', label: 'Onboarding' },
  { value: 'concept', label: 'Concept' },
];

/** The fixed built-in template options for connection/mapping pickers. */
export function confluenceTemplateOptions(): { value: string; label: string }[] {
  return CONFLUENCE_TEMPLATES;
}

/** Label for a built-in template slug, falling back to the slug itself. */
export function confluenceTemplateLabel(slug: string | null | undefined): string {
  const builtin = CONFLUENCE_TEMPLATES.find((t) => t.value === slug);
  if (builtin) return builtin.label;
  return slug ?? '—';
}

/**
 * The Confluence routes raise `{"error": "<code>", "message": ...}` inside the
 * FastAPI detail, re-enveloped by the global handler as
 * `{error: {code, message, details: {error: "<code>"}}}`. Read the semantic
 * code from `details.error` so callers can branch on
 * `confluence_mapping_required`, `confluence_page_drifted`, etc.
 */
export function confluenceErrorCode(err: unknown): string | undefined {
  if (!(err instanceof ApiError)) return undefined;
  const details = err.details as { error?: unknown } | undefined;
  if (details && typeof details.error === 'string') return details.error;
  return err.code;
}

/**
 * Confluence publishing is Enterprise and project-scoped, mirroring the Jira
 * surface: the plan that pays for the project (its org's, or its owner's for
 * personal projects) decides it. A project response carries its own
 * `effective_tier`; older servers omit it, in which case fall back to the
 * caller's tier.
 */
export function hasConfluenceEnterpriseTier(
  project: { effective_tier?: string | null } | null | undefined,
  me: { tier?: string | null; effective_tier?: string | null } | null | undefined,
): boolean {
  if (project?.effective_tier) return project.effective_tier === 'enterprise';
  if (me?.tier === 'admin') return true;
  const tier = me?.effective_tier || me?.tier || 'free';
  return tier === 'enterprise';
}

/**
 * Whether the caller may use the project's org Confluence connection at all:
 * any member of the org that scopes the project (the server returns 403
 * org_membership_required to everyone else). Stays false while membership
 * loads.
 */
export function isConfluenceOrgMember(
  project: { org_id?: string | null } | null | undefined,
  myOrgs: { org_id: string }[] | null | undefined,
): boolean {
  const orgId = project?.org_id;
  if (!orgId) return false;
  return !!myOrgs?.some((o) => o.org_id === orgId);
}

/**
 * Confluence surface resolved by the page owner (ProjectDetail) and threaded
 * into the wiki and context sections, mirroring `TicketJiraSurface`. The
 * org-scoped connection/mapping GETs are admin-only, so a member learns the
 * connection/mapping state from the preview POST's error codes inside the
 * publish dialog instead.
 */
export interface ConfluenceSurface {
  orgId: string;
  isEnterprise: boolean;
  /** Project-admin standing (`can_admin`): resolve and unlink are admin-only. */
  canAdmin?: boolean;
}

/** The preview → confirm → publish dialog's friendly, user-actionable errors. */
export function confluenceFriendlyError(err: unknown): string {
  switch (confluenceErrorCode(err)) {
    case 'confluence_not_connected':
      return 'This organization has not connected Confluence yet.';
    case 'confluence_mapping_required':
      return 'Ask your admin to add a Confluence mapping for this project.';
    case 'upgrade_required':
      return 'Publishing to Confluence requires the Enterprise plan.';
    case 'insufficient_role':
    case 'service_key_not_allowed':
      return "You don't have permission to publish to Confluence.";
    case 'org_membership_required':
      return "You aren't a member of this project's organization.";
    case 'publish_in_progress':
      return 'Another publish for this page is already in progress.';
    case 'confluence_page_drifted':
      return 'The Confluence page was edited since the last publish; a project admin can resolve the conflict first.';
    case 'content_too_large':
    case 'content_too_large_for_confluence':
      return 'This page is too large to publish to Confluence.';
    case 'confluence_connection_failed':
      return 'Confluence rejected the stored connection; an admin should re-save the connection.';
    case 'confluence_upstream_error':
    case 'confluence_timeout':
      return 'Confluence is unreachable right now; try again shortly.';
    case 'template_changed':
      return 'The template changed since this draft was built. Close this dialog and open it again to preview with the new template.';
    case 'confluence_link_stale':
      return "This page's Confluence link belongs to another site or organization; unlink it, then publish again.";
    case 'confluence_create_refused':
      return 'Confluence refused to create the page; a page with this title may already exist in the space. Rename the page or ask an admin to change the mapping.';
    case 'confluence_title_conflict':
      return 'A Confluence page with this title already belongs to another SessionFS source, so it was not taken over. Rename the page or ask an admin to change the mapping.';
    case 'confluence_page_missing':
      return 'The linked Confluence page was deleted, so the link was removed. Publish again to create a fresh page.';
    case 'confluence_adopt_rejected':
      return 'An earlier publish could not be confirmed and the matching Confluence page cannot be adopted safely; a project admin can unlink this source.';
    case 'publish_superseded':
      return 'This publish was superseded by a newer one; no page changed.';
    case 'link_changed':
      return 'This page link changed while publishing; no update was applied.';
    case 'confluence_link_invalid':
      return 'This page link is invalid; a project admin can unlink it first.';
    default:
      return errorMessage(err, 'Could not publish to Confluence');
  }
}

/**
 * The drift details of a 409 `confluence_page_drifted`, or null for any other
 * error. The server bounds `diff`; `diff_truncated` says whether it was cut.
 */
export function confluenceDriftDetails(err: unknown): ConfluenceDriftDetails | null {
  if (confluenceErrorCode(err) !== 'confluence_page_drifted') return null;
  const details = (err as ApiError).details ?? {};
  const diff = Array.isArray(details.diff) ? details.diff.filter((l): l is string => typeof l === 'string') : [];
  return {
    error: 'confluence_page_drifted',
    link_id: typeof details.link_id === 'string' ? details.link_id : undefined,
    page_id: typeof details.page_id === 'string' ? details.page_id : null,
    stored_version: typeof details.stored_version === 'number' ? details.stored_version : null,
    remote_version: typeof details.remote_version === 'number' ? details.remote_version : null,
    diff,
    diff_truncated: details.diff_truncated === true,
  };
}

/** Friendly errors for resolving or unlinking a page link (project admins only). */
export function confluenceLinkActionError(err: unknown): string {
  switch (confluenceErrorCode(err)) {
    case 'insufficient_role':
    case 'service_key_not_allowed':
      return 'Only a project admin can resolve or unlink a Confluence page.';
    case 'publish_in_progress':
      return 'A publish for this page is in progress; try again when it finishes.';
    case 'confluence_link_stale':
      return 'This link belongs to another site or organization; only unlinking is possible.';
    default:
      return errorMessage(err, 'Could not update the Confluence link');
  }
}
