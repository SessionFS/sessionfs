import type {
  ApprovalFilter,
  ApprovalPolicy,
  ApprovalRequirement,
  ApprovalState,
  ApprovalTargetKind,
} from '../api/client';

export const TARGET_LABEL: Record<ApprovalTargetKind, string> = {
  merge: 'Merge',
  kb_promote: 'Knowledge promotion',
};

export const STATE_LABEL: Record<string, string> = {
  pending: 'Pending',
  approved: 'Approved',
  rejected: 'Rejected',
  expired: 'Expired',
  superseded: 'Superseded',
};

export function stateVariant(state: ApprovalState | string): 'success' | 'warning' | 'danger' | 'default' {
  if (state === 'approved') return 'success';
  if (state === 'pending') return 'warning';
  if (state === 'rejected') return 'danger';
  return 'default';
}

export function stateLabel(state: string): string {
  return STATE_LABEL[state] ?? state;
}

export const HARMS: { value: string; label: string }[] = [
  { value: 'data-exposed', label: 'Data exposed' },
  { value: 'money-spent', label: 'Money spent' },
  { value: 'wrong-actor', label: 'Wrong actor' },
  { value: 'crash', label: 'Crash' },
  { value: 'other', label: 'Other harm' },
];

export const PRIORITIES = ['low', 'medium', 'high', 'critical'] as const;

export const ENTRY_TYPES = ['decision', 'pattern', 'discovery', 'convention', 'bug', 'dependency'] as const;

/** "1 org admin or owner", "2 of Ada, Grace" ... one phrase per requirement. */
export function summarizeRequirement(
  r: ApprovalRequirement,
  nameOf: (userId: string) => string = (id) => id,
): string {
  if (r.who === 'org_role') {
    return `${r.count} ${r.role === 'owner' ? 'org owner' : 'org admin or owner'}${r.count === 1 ? '' : 's'}`;
  }
  const names = r.user_ids.map(nameOf).join(', ');
  return `${r.count} of ${names}`;
}

export function summarizeRequirements(
  reqs: ApprovalRequirement[],
  nameOf?: (userId: string) => string,
): string {
  if (reqs.length === 0) return 'No approvers';
  return reqs.map((r) => summarizeRequirement(r, nameOf)).join(' and ');
}

export function summarizeFilter(
  f: ApprovalFilter | undefined,
  projectName: (id: string) => string = (id) => id,
): string {
  if (!f) return '';
  const parts: string[] = [];
  if (f.project_ids?.length) parts.push(`Projects: ${f.project_ids.map(projectName).join(', ')}`);
  if (f.priority_at_least) parts.push(`Priority ${f.priority_at_least} or above`);
  if (f.ticket_kind) parts.push(`${f.ticket_kind}s only`);
  if (f.finding_harm_any?.length) parts.push(`Harm: ${f.finding_harm_any.join(', ')}`);
  if (f.checklist_class_any?.length) parts.push(`Checklist: ${f.checklist_class_any.join(', ')}`);
  if (f.entry_type_any?.length) parts.push(`Types: ${f.entry_type_any.join(', ')}`);
  if (f.entity_ref_prefix_any?.length) parts.push(`Refs: ${f.entity_ref_prefix_any.join(', ')}`);
  return parts.join(' · ');
}

export function policyHasWhen(p: ApprovalPolicy): boolean {
  return !!p.filter && Object.keys(p.filter).length > 0;
}

export function approvalRequestHref(id: string): string {
  return `/approvals?request=${encodeURIComponent(id)}`;
}

/** Where the target of a request lives, or null when it has no page. */
export function targetHref(projectId: string, kind: ApprovalTargetKind, targetId: string): string | null {
  if (kind === 'merge') {
    return `/projects/${encodeURIComponent(projectId)}?tab=work&sub=tickets&ticket=${encodeURIComponent(targetId)}`;
  }
  return `/projects/${encodeURIComponent(projectId)}?tab=knowledge`;
}
