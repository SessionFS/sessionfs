import type { TicketNotificationKind } from '../api/client';

export const NOTIFICATION_KIND_LABEL: Record<TicketNotificationKind, string> = {
  your_turn: 'Your turn',
  review_verdict: 'Review verdict',
  status: 'Status',
  assignment: 'Assigned',
  dependency_unblocked: 'Unblocked',
  comment: 'Comment',
};

export const NOTIFICATION_KIND_VARIANT: Record<
  TicketNotificationKind,
  'success' | 'warning' | 'danger' | 'info' | 'default'
> = {
  your_turn: 'warning',
  review_verdict: 'info',
  status: 'default',
  assignment: 'info',
  dependency_unblocked: 'success',
  comment: 'default',
};

export function notificationKindLabel(kind: string): string {
  return NOTIFICATION_KIND_LABEL[kind as TicketNotificationKind] ?? kind;
}

export function notificationKindVariant(
  kind: string,
): 'success' | 'warning' | 'danger' | 'info' | 'default' {
  return (
    NOTIFICATION_KIND_VARIANT[kind as TicketNotificationKind] ?? 'default'
  );
}
