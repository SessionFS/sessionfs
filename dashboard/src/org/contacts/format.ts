import { ApiError } from '../../api/client';
import { errorMessage } from '../../utils/errorMessage';

/** The server's error code: `details.error` from the envelope, else `code`. */
export function contactErrorCode(err: unknown): string | undefined {
  if (!(err instanceof ApiError)) return undefined;
  const details = err.details as { error?: unknown } | undefined;
  return details && typeof details.error === 'string' ? details.error : err.code;
}

/** The existing contact a 409 `contact_exists` names, when it does. */
export function existingContactId(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  const id = (err.details as { contact_id?: unknown } | undefined)?.contact_id;
  return typeof id === 'string' ? id : null;
}

/** One sentence for a failed contact write. */
export function contactErrorMessage(err: unknown): string {
  switch (contactErrorCode(err)) {
    case 'contact_exists':
      return 'Another contact already has this email.';
    case 'email_is_member':
      return 'That email belongs to a member of this organization. Assign the member instead.';
    case 'email_conflict':
      return 'These contacts have different emails. Clear one of them before merging.';
    case 'contact_changed':
      return 'This contact changed while you were working. Reload and try again.';
    case 'too_many_aliases':
      return 'A contact can have at most 20 other names.';
    case 'invalid_email':
      return 'That email address isn’t valid.';
    case 'invalid_tag':
      return 'Tags are lowercase letters, digits, “.”, “_”, “:” or “-”, at most 10 of them.';
    case 'insufficient_role':
      return 'Only organization owners and admins can do this.';
    case 'participant_not_eligible':
      return 'That contact can’t be used (it may be archived or merged).';
    case 'invalid_merge':
      return 'A contact can’t be merged with itself.';
    default:
      return errorMessage(err);
  }
}

/** Parse a comma- or space-separated tag field. */
export function parseTags(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.split(/[\s,]+/)) {
    const tag = raw.trim().toLowerCase();
    if (tag && !out.includes(tag)) out.push(tag);
  }
  return out;
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

/** Where a referenced meeting or chat opens in the dashboard. */
export function conversationHref(ref: { meeting_id: string; project_id: string | null; group_id: string | null }, orgId: string): string | null {
  const meeting = encodeURIComponent(ref.meeting_id);
  if (ref.project_id) return `/projects/${encodeURIComponent(ref.project_id)}?tab=work&sub=meetings&meeting=${meeting}`;
  if (ref.group_id) {
    return `/settings/organization/groups/${encodeURIComponent(ref.group_id)}?org=${encodeURIComponent(orgId)}&meeting=${meeting}`;
  }
  return null;
}

export function ticketHref(t: { id: string; project_id: string }): string {
  return `/projects/${encodeURIComponent(t.project_id)}?tab=work&sub=tickets&ticket=${encodeURIComponent(t.id)}`;
}
