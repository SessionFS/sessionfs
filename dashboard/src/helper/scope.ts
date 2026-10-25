import type { HelperScope } from './HelperProvider';

export type ScopeKind = 'all' | 'project' | 'session';

export interface ScopeOption {
  key: string;
  kind: ScopeKind;
  /** Short chip text, e.g. "Acme Widgets". */
  label: string;
  /** What the option means, shown in the scope menu. */
  description: string;
  scope: HelperScope | undefined;
}

export function hasScope(scope?: HelperScope): boolean {
  return !!scope && (!!scope.project_id || (scope.session_ids?.length ?? 0) > 0);
}

export function scopeKey(scope?: HelperScope): string {
  if (!scope || !hasScope(scope)) return 'all';
  if (scope.project_id) return `project:${scope.project_id}`;
  return `sessions:${(scope.session_ids ?? []).join(',')}`;
}

export function scopeKind(scope?: HelperScope): ScopeKind {
  if (!scope || !hasScope(scope)) return 'all';
  return scope.project_id ? 'project' : 'session';
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
  const n = scope?.session_ids?.length ?? 0;
  return {
    key: scopeKey(scope),
    kind,
    label: scope?.label || (n === 1 ? 'This session' : `${n} sessions`),
    description: n === 1 ? 'Only this session' : `Only these ${n} sessions`,
    scope,
  };
}

/** The options the scope menu offers: where the drawer was opened, the
 *  current thread's scope, and always "All my sessions". */
export function scopeOptions(entry: HelperScope | undefined, active: HelperScope | undefined): ScopeOption[] {
  const out: ScopeOption[] = [];
  const add = (o: ScopeOption) => {
    if (!out.some((x) => x.key === o.key)) out.push(o);
  };
  if (hasScope(entry)) add(toOption(entry));
  if (hasScope(active)) {
    // A restored thread's scope carries no label; borrow the entry's when it matches.
    const sameAsEntry = scopeKey(active) === scopeKey(entry);
    add(toOption(sameAsEntry ? { ...active, label: entry?.label } : active));
  }
  add(ALL_SESSIONS);
  return out;
}

/** The option describing `active`, labelled from `entry` when they match. */
export function activeOption(entry: HelperScope | undefined, active: HelperScope | undefined): ScopeOption {
  if (!hasScope(active)) return ALL_SESSIONS;
  const sameAsEntry = scopeKey(active) === scopeKey(entry);
  return toOption(sameAsEntry ? { ...active, label: active?.label ?? entry?.label } : active);
}
