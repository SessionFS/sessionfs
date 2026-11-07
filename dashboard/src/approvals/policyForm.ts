import type {
  ApprovalFilter,
  ApprovalPolicy,
  ApprovalPolicyBody,
  ApprovalRequirement,
  ApprovalTargetKind,
} from '../api/client';

export const MAX_REQUIREMENTS = 10;
export const MAX_COUNT = 20;
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;

export interface RequirementRow {
  key: number;
  who: 'org_role' | 'users';
  role: 'owner' | 'admin';
  userIds: string[];
  count: string;
}

export interface PolicyFormState {
  name: string;
  description: string;
  targetKind: ApprovalTargetKind;
  blocking: boolean;
  projectIds: string[];
  priority: '' | 'low' | 'medium' | 'high' | 'critical';
  ticketKind: '' | 'task' | 'issue';
  harms: string[];
  checklistClasses: string;
  entryTypes: string[];
  entityRefPrefixes: string;
  requirements: RequirementRow[];
}

export interface PolicyFormErrors {
  name?: string;
  requirements?: string;
  checklistClasses?: string;
  entityRefPrefixes?: string;
  /** Per requirement row key. */
  rows?: Record<number, string>;
}

let nextKey = 1;
export const newRow = (over: Partial<RequirementRow> = {}): RequirementRow => ({
  key: nextKey++,
  who: 'org_role',
  role: 'admin',
  userIds: [],
  count: '1',
  ...over,
});

export function emptyForm(): PolicyFormState {
  return {
    name: '',
    description: '',
    targetKind: 'merge',
    blocking: true,
    projectIds: [],
    priority: '',
    ticketKind: '',
    harms: [],
    checklistClasses: '',
    entryTypes: [],
    entityRefPrefixes: '',
    requirements: [newRow()],
  };
}

export function formFromPolicy(p: ApprovalPolicy): PolicyFormState {
  const f = p.filter ?? {};
  return {
    name: p.name,
    description: p.description ?? '',
    targetKind: p.target_kind,
    blocking: p.blocking,
    projectIds: f.project_ids ?? [],
    priority: f.priority_at_least ?? '',
    ticketKind: f.ticket_kind ?? '',
    harms: f.finding_harm_any ?? [],
    checklistClasses: (f.checklist_class_any ?? []).join(', '),
    entryTypes: f.entry_type_any ?? [],
    entityRefPrefixes: (f.entity_ref_prefix_any ?? []).join('\n'),
    requirements: p.requirements.length
      ? p.requirements.map((r) =>
          r.who === 'org_role'
            ? newRow({ who: 'org_role', role: r.role, count: String(r.count) })
            : newRow({ who: 'users', userIds: r.user_ids, count: String(r.count) }),
        )
      : [newRow()],
  };
}

const splitList = (s: string, sep: RegExp) =>
  Array.from(new Set(s.split(sep).map((x) => x.trim()).filter(Boolean)));

export function validateForm(s: PolicyFormState): PolicyFormErrors {
  const errors: PolicyFormErrors = {};
  if (!s.name.trim()) errors.name = 'Give the policy a name.';
  if (s.requirements.length === 0) errors.requirements = 'Add at least one approver requirement.';
  const rows: Record<number, string> = {};
  for (const r of s.requirements) {
    const n = Number(r.count);
    if (!Number.isInteger(n) || n < 1 || n > MAX_COUNT) {
      rows[r.key] = `Count must be a whole number from 1 to ${MAX_COUNT}.`;
    } else if (r.who === 'users') {
      if (r.userIds.length === 0) rows[r.key] = 'Pick at least one person.';
      else if (n > r.userIds.length) rows[r.key] = 'Count cannot exceed the number of people picked.';
    }
  }
  if (Object.keys(rows).length) errors.rows = rows;
  if (s.targetKind === 'merge') {
    const bad = splitList(s.checklistClasses, /[\s,]+/).find((c) => !SLUG_RE.test(c));
    if (bad) errors.checklistClasses = `"${bad}" is not a valid class. Use lowercase letters, digits and hyphens.`;
  } else {
    const bad = splitList(s.entityRefPrefixes, /\n/).find((p) => p.length > 200);
    if (bad) errors.entityRefPrefixes = 'Each prefix can be at most 200 characters.';
  }
  return errors;
}

export const hasErrors = (e: PolicyFormErrors) =>
  !!(e.name || e.requirements || e.checklistClasses || e.entityRefPrefixes || e.rows);

export function buildPolicyBody(s: PolicyFormState): ApprovalPolicyBody {
  const filter: ApprovalFilter = {};
  if (s.projectIds.length) filter.project_ids = s.projectIds;
  if (s.targetKind === 'merge') {
    if (s.priority) filter.priority_at_least = s.priority;
    if (s.ticketKind) filter.ticket_kind = s.ticketKind;
    if (s.harms.length) filter.finding_harm_any = s.harms;
    const classes = splitList(s.checklistClasses, /[\s,]+/);
    if (classes.length) filter.checklist_class_any = classes;
  } else {
    if (s.entryTypes.length) filter.entry_type_any = s.entryTypes;
    const prefixes = splitList(s.entityRefPrefixes, /\n/);
    if (prefixes.length) filter.entity_ref_prefix_any = prefixes;
  }
  const requirements: ApprovalRequirement[] = s.requirements.map((r) =>
    r.who === 'org_role'
      ? { who: 'org_role', role: r.role, count: Number(r.count) }
      : { who: 'users', user_ids: r.userIds, count: Number(r.count) },
  );
  return {
    name: s.name.trim(),
    description: s.description.trim(),
    target_kind: s.targetKind,
    filter,
    requirements,
    blocking: s.blocking,
  };
}
