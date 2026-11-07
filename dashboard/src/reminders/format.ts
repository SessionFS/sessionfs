import type { Reminder, ReminderSource } from '../api/client';

/** The viewer's IANA timezone (falls back to UTC where the runtime hides it). */
export function userTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/** "Thu 6 Nov 2026, 09:00 Europe/London" — absolute, with the zone named. */
export function formatDue(iso: string, tz?: string | null): string {
  const zone = tz || userTimezone();
  const d = new Date(iso);
  const opts = (zoneName: string): Intl.DateTimeFormatOptions => ({
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: zoneName,
  });
  let text: string;
  let used = zone;
  try {
    text = new Intl.DateTimeFormat('en-GB', opts(zone)).format(d);
  } catch {
    used = 'UTC';
    text = new Intl.DateTimeFormat('en-GB', opts('UTC')).format(d);
  }
  return `${text} ${used}`;
}

/** Value for <input type="datetime-local"> in the viewer's local zone. */
export function toLocalInput(iso: string): string {
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** A datetime-local value (the viewer's wall clock) as a UTC instant. */
export function fromLocalInput(value: string): string | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

/** Due items first (oldest due first), then the rest by due time. */
export function sortReminders(items: Reminder[]): Reminder[] {
  return [...items].sort((a, b) => {
    if (a.is_due !== b.is_due) return a.is_due ? -1 : 1;
    return new Date(a.due_at).getTime() - new Date(b.due_at).getTime();
  });
}

/** In-app link to a reminder's source, or null when it has none to open. */
export function sourceHref(src: ReminderSource): string | null {
  switch (src.kind) {
    case 'meeting':
      return `/meetings/${encodeURIComponent(src.id)}${src.lines ? `?lines=${encodeURIComponent(src.lines)}` : ''}`;
    case 'meeting_item':
    case 'chat_item': {
      if (!src.meeting_id) return null;
      return `/meetings/${encodeURIComponent(src.meeting_id)}${src.lines ? `?lines=${encodeURIComponent(src.lines)}` : ''}`;
    }
    case 'ticket':
      return src.project_id
        ? `/projects/${encodeURIComponent(src.project_id)}?tab=work&sub=tickets&ticket=${encodeURIComponent(src.id)}`
        : null;
    case 'session':
      return `/sessions/${encodeURIComponent(src.id)}`;
    default:
      return null;
  }
}

export function reminderHref(id: string): string {
  return `/reminders?reminder=${encodeURIComponent(id)}`;
}
