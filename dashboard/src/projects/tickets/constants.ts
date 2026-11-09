
/** The one set of status names, in sentence case, used on every surface. */
export const STATUS_LABEL: Record<string, string> = {
  suggested: 'Suggested',
  open: 'Open',
  in_progress: 'In progress',
  blocked: 'Blocked',
  review: 'In review',
  done: 'Done',
  closed: 'Closed',
  cancelled: 'Cancelled',
};

export const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All statuses' },
  ...Object.entries(STATUS_LABEL).map(([value, label]) => ({ value, label })),
];

/** Statuses that appear as board columns (all non-empty filter values). */
export const BOARD_STATUSES = STATUS_FILTERS.filter((s) => s.value !== '');

export const KIND_TONE: Record<string, string> = {
  issue: 'bg-info/15 text-info',
  task: 'bg-border/30 text-text-tertiary',
};

export const PRIORITY_TONE: Record<string, string> = {
  low: 'text-text-tertiary',
  medium: '',
  high: 'text-warning',
  critical: 'text-danger',
};

/** Maps status to Badge tint variant semantic color. */
export const STATUS_VARIANT: Record<string, 'success' | 'warning' | 'danger' | 'info' | 'default'> = {
  suggested: 'warning',
  open: 'info',
  in_progress: 'info',
  blocked: 'danger',
  review: 'info',
  done: 'success',
  closed: 'success',
  cancelled: 'default',
};

/** Colored dot indicator — semantic token background for each status. */
export const STATUS_DOT: Record<string, string> = {
  suggested: 'bg-warning',
  open: 'bg-info',
  in_progress: 'bg-brand',
  blocked: 'bg-danger',
  review: 'bg-info',
  done: 'bg-success',
  closed: 'bg-success',
  cancelled: 'bg-text-tertiary',
};

/** Sentence-case status name ("In progress"). Unknown values fall back to
 *  the raw value with underscores as spaces, first letter capitalised. */
export function formatStatusLabel(status: string): string {
  const known = STATUS_LABEL[status];
  if (known) return known;
  const words = status.replace(/_/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Joins the present parts of a meta line with a middle dot, so a missing
 *  part never leaves a dangling separator. */
export function joinMeta(parts: (string | false | null | undefined)[]): string {
  return parts.filter((p): p is string => typeof p === 'string' && p.length > 0).join(' · ');
}

/** "1 dependency" / "3 dependencies"-style counts. */
export function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** Jira surface details for the ticket detail, resolved by the page owner. */
export interface TicketJiraSurface {
  orgId: string;
  isProjectAdmin: boolean;
  isEnterprise: boolean;
  /** The project's `can_edit` flag: only editors may link an existing issue. */
  canEdit?: boolean;
}

