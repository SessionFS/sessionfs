import { ApiError, type JiraTemplate, type JiraTicketTemplates } from '../api/client';
import { isOrgAdminRole } from './roles';

/**
 * Built-in Jira story templates (server side: `templates.py`). Custom
 * templates come from the org and are merged into pickers after these; a
 * custom slug can never collide with a built-in, so `value` is unambiguous.
 */
export const JIRA_TEMPLATES: { value: string; label: string }[] = [
  { value: 'agile', label: 'Agile Standard' },
  { value: 'compliance', label: 'Enterprise / Compliance' },
  { value: 'sre', label: 'Platform / SRE' },
  { value: 'bug', label: 'Bug Report' },
  { value: 'design', label: 'Design / UX' },
];

/** Fixed source-field vocabulary a template section may draw from. */
export const JIRA_SOURCE_FIELDS: { value: string; label: string }[] = [
  { value: 'title', label: 'Title' },
  { value: 'description', label: 'Description' },
  { value: 'acceptance_criteria', label: 'Acceptance criteria' },
  { value: 'priority', label: 'Priority' },
  { value: 'kind', label: 'Kind' },
  { value: 'persona', label: 'Persona' },
  { value: 'completion_notes', label: 'Completion notes' },
  { value: 'linked_sessions', label: 'Linked sessions' },
];

/**
 * Which source fields each section mode can render (server:
 * `SOURCE_FIELDS_BY_MODE` in templates.py — a backend test keeps these lists
 * identical). Verbatim sections copy list/note content exactly, so only these
 * three fields have a verbatim rendering; rewrite sections can use any field.
 */
export const JIRA_VERBATIM_SOURCE_FIELDS: readonly string[] = [
  'acceptance_criteria',
  'completion_notes',
  'linked_sessions',
];
export const JIRA_REWRITE_SOURCE_FIELDS: readonly string[] = [
  'title',
  'description',
  'acceptance_criteria',
  'priority',
  'kind',
  'persona',
  'completion_notes',
  'linked_sessions',
];

/** The source-field options valid for a section mode, in vocabulary order. */
export function jiraSourceFieldsForMode(
  mode: 'verbatim' | 'rewrite',
): { value: string; label: string }[] {
  const allowed = mode === 'verbatim' ? JIRA_VERBATIM_SOURCE_FIELDS : JIRA_REWRITE_SOURCE_FIELDS;
  return JIRA_SOURCE_FIELDS.filter((f) => allowed.includes(f.value));
}

/**
 * Built-ins + the org's active custom templates, for template pickers.
 *
 * `currentValue` is the field's saved value. When it names an archived custom
 * template it stays selectable for *that field only* (labelled "(archived)"),
 * so re-saving a connection or mapping never silently switches it; archived
 * templates are otherwise never offered as a new choice.
 */
export function jiraTemplateOptions(
  customTemplates?: Pick<JiraTemplate, 'slug' | 'name' | 'builtin' | 'archived_at'>[],
  currentValue?: string | null,
): { value: string; label: string }[] {
  const all = customTemplates ?? [];
  // An org custom template wins over a built-in with the same slug.
  const customSlugs = new Set(all.filter((t) => !t.builtin).map((t) => t.slug));
  const builtins = JIRA_TEMPLATES.filter((t) => !customSlugs.has(t.value));
  const customs = all
    .filter((t) => !t.builtin && (!t.archived_at || t.slug === currentValue))
    .map((t) => ({
      value: t.slug,
      label: t.archived_at ? `${t.name} (archived)` : t.name,
    }));
  return [...builtins, ...customs];
}

/**
 * The promotion dialog's starting template: the link's pinned template — a
 * published ticket keeps its template, so the server lists it even after it
 * was archived — else the inherited default (mapping override → connection
 * default). A pin the server no longer lists (e.g. the template is gone) falls
 * back to the inherited one.
 */
export function defaultJiraTemplateChoice(choices: JiraTicketTemplates): string {
  const selectable = new Set(choices.templates.map((t) => t.slug));
  if (choices.pinned && selectable.has(choices.pinned.slug)) return choices.pinned.slug;
  return choices.inherited_default.slug;
}

/** Label for a resolved template slug, falling back to custom then the slug. */
export function jiraTemplateLabel(
  slug: string | null | undefined,
  customTemplates?: Pick<JiraTemplate, 'slug' | 'name' | 'builtin' | 'archived_at'>[],
): string {
  const custom = customTemplates?.find((t) => !t.builtin && t.slug === slug);
  if (custom) return custom.archived_at ? `${custom.name} (archived)` : custom.name;
  const builtin = JIRA_TEMPLATES.find((t) => t.value === slug);
  if (builtin) return builtin.label;
  return slug ?? '—';
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
 * Whether the caller may use the project's org Jira connection at all,
 * mirroring the server's `_authorize_ticket_jira`: any member of the org
 * that scopes the project. Readers whose only access is a captured session
 * on the project's repo are not members and get 403 org_membership_required,
 * so they see no Jira surface. While the membership list is loading this
 * stays false.
 */
export function isJiraOrgMember(
  project: { org_id?: string | null } | null | undefined,
  myOrgs: { org_id: string }[] | null | undefined,
): boolean {
  const orgId = project?.org_id;
  if (!orgId) return false;
  return !!myOrgs?.some((o) => o.org_id === orgId);
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

/**
 * Delivery state of a two-way sync ledger row, for badges. `done` rows need
 * no badge (the row itself is the record); pending rows say whether they are
 * still queued or already retrying; exhausted rows gave up after the retry
 * budget. Older servers omit `state`, so fall back to `applied`/`error`.
 */
export function jiraSyncStateLabel(event: {
  state?: string | null;
  retry_count?: number | null;
  applied?: boolean;
  error?: string | null;
}): { label: string; variant: 'info' | 'warning' | 'danger' | 'default' } | null {
  const state = event.state ?? (event.applied === false && event.error ? 'failed' : 'done');
  switch (state) {
    case 'pending':
      return (event.retry_count ?? 0) > 0
        ? { label: `retrying (attempt ${(event.retry_count ?? 0) + 1})`, variant: 'warning' }
        : { label: 'queued', variant: 'info' };
    case 'exhausted':
      return { label: 'gave up', variant: 'danger' };
    case 'skipped':
      return { label: 'skipped', variant: 'default' };
    case 'failed':
      return { label: 'failed', variant: 'danger' };
    default:
      return event.applied === false && event.error ? { label: 'failed', variant: 'danger' } : null;
  }
}

const ACTION_DONE: Record<string, string> = {
  resolve: 'ticket resolved',
  start: 'ticket started',
  reopen: 'ticket reopened',
};

function humanStatus(status: unknown): string {
  if (typeof status !== 'string' || !status) return '';
  const text = status.replace(/_/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function payloadString(payload: Record<string, unknown> | null | undefined, key: string): string | null {
  const v = payload?.[key];
  return typeof v === 'string' && v.trim() ? v : null;
}

/**
 * Plain-language label for a two-way sync ledger row, built from its payload
 * (server: services/jira_sync.py and routes/jira.py). Never shows raw
 * underscores; falls back to a generic label when an older row lacks fields.
 */
export function jiraSyncEventLabel(event: {
  direction: string;
  kind: string;
  payload?: Record<string, unknown> | null;
  applied?: boolean;
  state?: string | null;
}): string {
  const p = event.payload ?? {};
  if (event.direction === 'outbound') {
    if (event.kind === 'comment') return 'Comment sent to Jira';
    if (event.kind === 'evidence') return 'Completion evidence pushed';
    if (event.kind === 'status') {
      const to = humanStatus(p.to_status);
      const transition = payloadString(p, 'transition');
      if (to && transition) return `Status pushed: ${to} → Jira “${transition}”`;
      if (to) return `Status pushed: ${to}`;
      return 'Status pushed to Jira';
    }
    return `Sent to Jira (${humanStatus(event.kind).toLowerCase()})`;
  }
  switch (event.kind) {
    case 'comment':
      return event.applied === false || event.state === 'skipped' ? 'Jira comment not mirrored' : 'Jira comment mirrored';
    case 'field_edit': {
      const field = payloadString(p, 'field');
      return field ? `Jira edited ${humanStatus(field).toLowerCase()}` : 'Jira edited a field';
    }
    case 'status': {
      if (p.outcome === 'echo') return 'Echo of our transition (ignored)';
      const target = payloadString(p, 'jira_status');
      const moved = target ? `Jira moved to ${target}` : 'Jira status changed';
      const action = typeof p.action === 'string' ? ACTION_DONE[p.action] : undefined;
      if (p.outcome === 'applied' && action) return `${moved} (${action})`;
      if (p.outcome === 'noop') return `${moved} (no change needed)`;
      if (p.outcome === 'refused') return `${moved} (refused)`;
      return moved;
    }
    case 'error':
      return 'Jira delivery could not be processed';
    default:
      return `Jira ${humanStatus(event.kind).toLowerCase()}`;
  }
}
