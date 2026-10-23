import type { Ticket } from '../../api/client';

export const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'suggested', label: 'Suggested' },
  { value: 'open', label: 'Open' },
  { value: 'in_progress', label: 'In progress' },
  { value: 'blocked', label: 'Blocked' },
  { value: 'review', label: 'Review' },
  { value: 'done', label: 'Done' },
  { value: 'closed', label: 'Closed' },
  { value: 'cancelled', label: 'Cancelled' },
];

export const KIND_FILTERS: { value: string; label: string }[] = [
  { value: '', label: 'All kinds' },
  { value: 'issue', label: 'Issues' },
  { value: 'task', label: 'Tasks' },
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

export function formatStatusLabel(status: string): string {
  return status.replace(/_/g, ' ');
}

export interface ViewProps {
  tickets: Ticket[];
  projectId: string;
  selected: string | null;
  onSelect: (id: string) => void;
  onNavigate: (targetId: string) => void;
}
