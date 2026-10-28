import type { HelperScope } from './HelperProvider';

export type ScopeKind = 'all' | 'project' | 'group' | 'session';

export interface ScopeOption {
  key: string;
  kind: ScopeKind;
  /** Short chip text, e.g. "Acme Widgets". */
  label: string;
  /** What the option means, shown in the scope menu. */
  description: string;
  scope: HelperScope | undefined;
  /** Menu section heading shown above the first option of a section. */
  section?: string;
}

export function hasScope(scope?: HelperScope): boolean {
  return !!scope && (!!scope.project_id || !!scope.group_id || (scope.session_ids?.length ?? 0) > 0);
}

export function scopeKey(scope?: HelperScope): string {
  if (!scope || !hasScope(scope)) return 'all';
  if (scope.group_id) return `group:${scope.group_id}`;
  if (scope.project_id) return `project:${scope.project_id}`;
  return `sessions:${(scope.session_ids ?? []).join(',')}`;
}

export function scopeKind(scope?: HelperScope): ScopeKind {
  if (!scope || !hasScope(scope)) return 'all';
  if (scope.group_id) return 'group';
  return scope.project_id ? 'project' : 'session';
}

/**
 * The ids a scope sends to the API (turn body and status query); the label is
 * display-only. A group scope is exclusive on the server (422 scope_conflict
 * with a project or session ids), so it is sent on its own.
 */
export function apiScope(
  scope?: HelperScope,
): { project_id?: string; group_id?: string; session_ids?: string[] } | undefined {
  if (!scope || !hasScope(scope)) return undefined;
  if (scope.group_id) return { group_id: scope.group_id };
  return { project_id: scope.project_id, session_ids: scope.session_ids };
}

export const ALL_SESSIONS: ScopeOption = {
  key: 'all',
  kind: 'all',
  label: 'All my sessions',
  description: 'Everything you can read',
  scope: undefined,
};

export function toOption(scope: HelperScope | undefined): ScopeOption {
  const kind = scopeKind(scope);
  if (kind === 'all') return ALL_SESSIONS;
  if (kind === 'project') {
    return {
      key: scopeKey(scope),
      kind,
      label: scope?.label || 'This project',
      description: 'This project’s sessions and knowledge',
      scope,
    };
  }
  if (kind === 'group') {
    return {
      key: scopeKey(scope),
      kind,
      label: scope?.label || 'This project group',
      description: 'Sessions and knowledge across the group’s projects',
      scope,
    };
  }
  const n = scope?.session_ids?.length ?? 0;
  return {
    key: scopeKey(scope),
    kind,
    label: scope?.label || (n === 1 ? 'This session' : `${n} sessions`),
    description: n === 1 ? 'Only this session' : `Only these ${n} sessions`,
    scope,
  };
}

/** A group the caller can see, as a scope option. */
export interface GroupChoice {
  id: string;
  name: string;
  project_count?: number;
}

export function groupOption(g: GroupChoice): ScopeOption {
  const n = g.project_count;
  return {
    key: `group:${g.id}`,
    kind: 'group',
    label: g.name,
    description:
      typeof n === 'number' ? `Project group · ${n} ${n === 1 ? 'project' : 'projects'}` : 'Project group',
    scope: { group_id: g.id, label: g.name },
    section: 'Project groups',
  };
}

/** Fill a missing display label from the places that know it. */
export function withLabel(
  scope: HelperScope | undefined,
  known: Array<HelperScope | undefined>,
  groups: GroupChoice[] = [],
): HelperScope | undefined {
  if (!scope || !hasScope(scope) || scope.label) return scope;
  const key = scopeKey(scope);
  const match = known.find((k) => hasScope(k) && scopeKey(k) === key && k?.label);
  if (match?.label) return { ...scope, label: match.label };
  if (scope.group_id) {
    const g = groups.find((x) => x.id === scope.group_id);
    if (g) return { ...scope, label: g.name };
  }
  return scope;
}

/** The options the scope menu offers: where the drawer was opened, the
 *  current thread's scope, the last scope the user chose, "All my sessions",
 *  then the project groups the user can see. */
export function scopeOptions(
  entry: HelperScope | undefined,
  active: HelperScope | undefined,
  extra: { remembered?: HelperScope; groups?: GroupChoice[] } = {},
): ScopeOption[] {
  const out: ScopeOption[] = [];
  const add = (o: ScopeOption) => {
    if (!out.some((x) => x.key === o.key)) out.push(o);
  };
  const groups = extra.groups ?? [];
  const known = [entry, extra.remembered];
  if (hasScope(entry)) add(toOption(entry));
  // A restored thread's scope may carry no label; borrow a known one.
  if (hasScope(active)) add(toOption(withLabel(active, known, groups)));
  if (hasScope(extra.remembered)) add(toOption(withLabel(extra.remembered, [entry], groups)));
  add(ALL_SESSIONS);
  for (const g of groups) add(groupOption(g));
  return out;
}

/** The option describing `active`, labelled from `entry` when they match. */
export function activeOption(
  entry: HelperScope | undefined,
  active: HelperScope | undefined,
  extra: { remembered?: HelperScope; groups?: GroupChoice[] } = {},
): ScopeOption {
  if (!hasScope(active)) return ALL_SESSIONS;
  return toOption(withLabel(active, [entry, extra.remembered], extra.groups));
}
