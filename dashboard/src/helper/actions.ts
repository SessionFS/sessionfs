/**
 * Pure helpers for the helper's confirm-first write actions: card titles from
 * the stored preview (never the model's prose), per-target labels, expiry
 * text, and recovering proposals from a stored conversation.
 */

import { ApiError, type HelperAction, type HelperActionProposal, type HelperActionTarget } from '../api/client';

/** A proposal attached to an assistant turn. `fromHistory` cards read their
 *  current status back from the server when they mount. */
export interface TurnAction extends HelperActionProposal {
  fromHistory?: boolean;
}

/** Tools whose stored result text starts with "Proposed action ha_…". */
export const ACTION_TOOLS = new Set(['set_project_auto_narrative', 'trash_sessions']);

const PROPOSED_RE = /^Proposed action (ha_[0-9a-f]{24})\b/;

/**
 * The action id behind a stored action-tool row, or null. The server links
 * the row to its action structurally (`action_id`); the "Proposed action ha_…"
 * text prefix is only a fallback for rows saved without that link.
 */
export function proposedActionId(
  message: { tool_name: string | null; content: string; action_id?: string | null },
): string | null {
  if (!message.tool_name || !ACTION_TOOLS.has(message.tool_name)) return null;
  if (message.action_id) return message.action_id;
  return PROPOSED_RE.exec(message.content)?.[1] ?? null;
}

function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

/** Card title from the kind + preview targets, e.g. "Move 30 sessions to Trash". */
export function actionTitle(action: Pick<HelperActionProposal, 'kind' | 'summary' | 'targets'> & { params?: Record<string, unknown> }): string {
  const changing = action.targets.filter((t) => t.change === 'will_change');
  if (action.kind === 'set_auto_narrative') {
    const enabled =
      typeof action.params?.enabled === 'boolean' ? action.params.enabled : changing[0]?.after === true;
    return `Turn Auto Narrative ${enabled ? 'on' : 'off'} for ${plural(changing.length, 'project', 'projects')}`;
  }
  if (action.kind === 'trash_sessions') {
    return `Move ${plural(changing.length, 'session', 'sessions')} to Trash`;
  }
  return action.summary || 'Proposed change';
}

const REASONS: Record<string, string> = {
  not_accessible: 'No access',
  not_editor: 'You can view but not edit this project',
  not_owned: 'Not your session',
  already_trashed: 'Already in Trash',
  not_in_scope: 'Outside this conversation',
  upgrade_required: 'Needs a plan upgrade',
  forbidden: 'No access',
  not_found: 'Not found',
};

/** A short human label for a skip/failure reason code. */
export function reasonLabel(code: string | null | undefined): string {
  if (!code) return 'Unknown reason';
  return REASONS[code] ?? code.replace(/_/g, ' ');
}

export type TargetState =
  | { tone: 'info'; label: string }
  | { tone: 'default'; label: string }
  | { tone: 'warning'; label: string; reason: string }
  | { tone: 'success'; label: string }
  | { tone: 'danger'; label: string; reason: string };

/** What happens (or happened) to one target. */
export function targetState(
  kind: string,
  target: HelperActionTarget,
  result: { ok: boolean; error: string | null } | undefined,
): TargetState {
  if (target.change === 'skipped') return { tone: 'warning', label: 'Skipped', reason: reasonLabel(target.reason) };
  if (target.change === 'unchanged') return { tone: 'default', label: 'Unchanged' };
  if (result) {
    if (result.ok) return { tone: 'success', label: kind === 'trash_sessions' ? 'In Trash' : 'Changed' };
    return { tone: 'danger', label: 'Failed', reason: reasonLabel(result.error) };
  }
  return { tone: 'info', label: 'Will change' };
}

/** "Expires in 12 min" / "Expires in 40 s" / null once past. */
export function expiryLabel(expiresAt: string | null, now: number): string | null {
  if (!expiresAt) return null;
  const ms = new Date(expiresAt).getTime() - now;
  if (!Number.isFinite(ms) || ms <= 0) return null;
  if (ms < 60_000) return `Expires in ${Math.ceil(ms / 1000)} s`;
  return `Expires in ${Math.ceil(ms / 60_000)} min`;
}

/** A pending record built from the streamed proposal (before any server read). */
export function pendingRecord(p: HelperActionProposal): HelperAction {
  return { ...p, status: 'pending', params: {}, result: {}, created_at: null, decided_at: null };
}

/** True when a confirm/cancel failed because the action had already expired. */
export function isExpiredError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.code === 'action_expired';
}

/** True when a confirm failed because the action was cancelled elsewhere. */
export function isCancelledError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.code === 'action_cancelled';
}
