/**
 * Pure helpers for the helper's confirm-first write actions: card titles from
 * the stored preview (never the model's prose), per-target labels, expiry
 * text, and recovering proposals from a stored conversation.
 */

import {
  ApiError,
  type HelperAction,
  type HelperActionProposal,
  type HelperActionTarget,
  type HelperActionTargetResult,
} from '../api/client';

/** A proposal attached to an assistant turn. `fromHistory` cards read their
 *  current status back from the server when they mount. */
export interface TurnAction extends HelperActionProposal {
  fromHistory?: boolean;
}

/** Tools whose stored result text starts with "Proposed action ha_…". */
export const ACTION_TOOLS = new Set([
  'set_project_auto_narrative',
  'trash_sessions',
  'propose_ticket',
  'propose_assignment',
  'propose_comment',
  'propose_jira_link',
  'propose_jira_overwrite',
]);

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

/** The action kind a proposal tool produces; used to rebuild a card's kind
 *  from a stored tool row when restoring a conversation from history. */
export function actionKindForTool(toolName: string | null | undefined): string {
  switch (toolName) {
    case 'set_project_auto_narrative':
      return 'set_auto_narrative';
    case 'trash_sessions':
      return 'trash_sessions';
    case 'propose_ticket':
      return 'create_ticket';
    case 'propose_assignment':
      return 'assign_ticket';
    case 'propose_comment':
      return 'comment_ticket';
    case 'propose_jira_link':
      return 'link_jira';
    case 'propose_jira_overwrite':
      return 'jira_overwrite';
    default:
      return 'set_auto_narrative';
  }
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
  if (action.kind === 'create_ticket') {
    const kind = typeof action.params?.kind === 'string' && action.params.kind ? action.params.kind : 'task';
    const base = `Create ${/^[aeiou]/i.test(kind) ? 'an' : 'a'} ${kind} ticket`;
    // The ticket also leaves SessionFS: say so, with the Jira project.
    if (action.params?.publish_to_jira === true) {
      const project = paramString(action.params, 'jira_project_key');
      return `${base} and publish it to Jira${project ? ` ${project}` : ''}`;
    }
    return base;
  }
  if (action.kind === 'assign_ticket') {
    const person = assignedPerson(action.params);
    if (person) {
      const target = changing[0] ?? action.targets[0];
      const ticket =
        (typeof target?.name === 'string' && target.name) ||
        (typeof action.params?.ticket_id === 'string' && action.params.ticket_id) ||
        'ticket';
      return `Assign ${ticket} to ${person.name}`;
    }
    const to = typeof action.params?.assigned_to === 'string' && action.params.assigned_to
      ? action.params.assigned_to
      : changing[0]?.after;
    return `Assign to ${typeof to === 'string' && to ? to : 'a persona'}`;
  }
  if (action.kind === 'comment_ticket') {
    const name = typeof changing[0]?.name === 'string' && changing[0].name ? changing[0].name : 'ticket';
    // The comment also leaves SessionFS: say so, with the issue it goes to.
    if (action.params?.send_to_jira === true) {
      const key = typeof action.params.issue_key === 'string' && action.params.issue_key
        ? action.params.issue_key
        : 'the linked issue';
      return `Comment on ${name} and post it to Jira ${key}`;
    }
    return `Comment on ${name}`;
  }
  if (action.kind === 'link_jira') {
    const target = changing[0] ?? action.targets[0];
    const name = typeof target?.name === 'string' && target.name ? target.name : 'ticket';
    const key = paramString(action.params, 'issue_key');
    return key ? `Link ${name} to Jira ${key}` : `Link ${name} to a Jira issue`;
  }
  if (action.kind === 'jira_overwrite') {
    const target = changing[0] ?? action.targets[0];
    const name = typeof target?.name === 'string' && target.name ? target.name : 'the ticket';
    const key = paramString(action.params, 'issue_key');
    const issue = key ? `Jira ${key}` : 'the linked Jira issue';
    return overwriteDirection(action.params) === 'from_jira'
      ? `Replace ${name} with ${issue}`
      : `Replace ${issue} with ${name}`;
  }
  return action.summary || 'Proposed change';
}

/** Which side a jira_overwrite replaces; anything unexpected reads as to_jira. */
export function overwriteDirection(params: Record<string, unknown> | undefined): 'to_jira' | 'from_jira' {
  return params?.direction === 'from_jira' ? 'from_jira' : 'to_jira';
}

function paramString(params: Record<string, unknown> | undefined, key: string): string {
  const v = params?.[key];
  return typeof v === 'string' ? v.trim() : '';
}

/** The person an assign_ticket or create_ticket action assigns to (params
 *  carry `assigned_user_id` + `assignee_name` instead of a persona), or null. */
export function assignedPerson(
  params: Record<string, unknown> | undefined,
): { id: string; name: string } | null {
  const id = params?.assigned_user_id;
  if (typeof id !== 'string' || !id) return null;
  // `assignee_name`; proposals stored before the rename carry
  // `assigned_user_display_name`.
  const raw = [params?.assignee_name, params?.assigned_user_display_name].find(
    (v): v is string => typeof v === 'string' && v.trim() !== '',
  );
  const name = raw ? raw.trim() : 'a person';
  return { id, name };
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
  ticket_moved: 'The ticket moved to another project',
  lease_epoch_required: 'Your organization requires a ticket lease to comment',
  jira_comment_sync_off: 'Jira comment sync is off or the ticket is no longer linked',
  jira_edit_required: 'Jira actions need edit access to this project',
  jira_link_changed: 'The ticket is now linked to a different Jira issue',
  jira_already_linked: 'The ticket is already linked to a Jira issue',
  jira_issue_linked_elsewhere: 'That Jira issue is already linked to another ticket',
  jira_issue_not_found: 'No such Jira issue, or the Jira connection can’t see it',
  jira_project_not_mapped: 'That Jira project isn’t mapped to this project',
  jira_project_unmapped: 'This project has no Jira mapping',
  jira_not_connected: 'Jira isn’t connected for this organization',
  jira_timeout: 'Jira didn’t respond in time',
  jira_upstream_error: 'Jira returned an error',
  jira_overwrite_required: 'Replacing the Jira issue needs an explicit choice',
  jira_not_linked: 'The ticket isn’t linked to a Jira issue',
  // Create-and-publish failures (`jira_error`), worded like the ticket page's
  // publish errors (projects/tickets/JiraPromote.tsx friendlyError).
  content_too_large: 'Some content is too large to publish to Jira Cloud',
  content_too_large_for_jira: 'Some content is too large to publish to Jira Cloud',
  jira_adopt_rejected: 'A Jira issue from an earlier publish couldn’t be confirmed as this ticket’s',
  publish_superseded: 'A newer publish superseded this one',
  link_changed: 'The ticket’s Jira link changed while publishing',
  publish_in_progress: 'Another publish for this ticket is already in progress',
  jira_connection_failed: 'Jira rejected the stored connection; an admin should re-save it',
  invalid_issue_key: 'That isn’t a valid Jira issue key',
  assignee_not_eligible: "That person can't be assigned tickets in this project",
  assignee_conflict: 'A ticket can have a person or a persona as assignee, not both',
  error: 'Something went wrong',
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
    if (result.ok) {
      const label =
        kind === 'trash_sessions'
          ? 'In Trash'
          : kind === 'create_ticket'
            ? 'Created'
            : kind === 'assign_ticket'
              ? 'Assigned'
              : kind === 'comment_ticket'
                ? 'Commented'
                : kind === 'link_jira'
                  ? 'Linked'
                  : kind === 'jira_overwrite'
                    ? 'Replaced'
                    : 'Changed';
      return { tone: 'success', label };
    }
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
  return { ...p, status: 'pending', params: p.params ?? {}, result: {}, created_at: null, decided_at: null };
}

/** True when a confirm/cancel failed because the action had already expired. */
export function isExpiredError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.code === 'action_expired';
}

/** True when a confirm failed because the action was cancelled elsewhere. */
export function isCancelledError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.code === 'action_cancelled';
}

/**
 * The Jira side of a confirmed ticket action, from the applied target's
 * result: the issue it was published as / linked to, or why a
 * create-and-publish created the ticket but did not publish it. Null when
 * Jira wasn't involved.
 */
export type JiraOutcome =
  | { kind: 'issue'; issueKey: string; issueUrl: string | null }
  | { kind: 'replaced'; issueKey: string; issueUrl: string | null }
  | { kind: 'pulled'; issueKey: string; fields: string[] }
  | { kind: 'not_published'; reason: string };

export function jiraOutcome(
  kind: string,
  result: HelperAction['result'] | undefined,
  params?: Record<string, unknown>,
): JiraOutcome | null {
  if (!result) return null;
  const applied: HelperActionTargetResult | undefined = (result.targets ?? []).find((r) => r.ok);
  if (kind === 'jira_overwrite') {
    if (!applied) return null;
    const issueKey = applied.issue_key || paramString(params, 'issue_key');
    if (overwriteDirection(params) === 'from_jira') {
      return issueKey ? { kind: 'pulled', issueKey, fields: applied.updated_fields ?? [] } : null;
    }
    // A to_jira replace that Jira refused is written as ok:false (with the
    // code in `error`), so an applied target here always reached Jira.
    return issueKey ? { kind: 'replaced', issueKey, issueUrl: applied.issue_url ?? null } : null;
  }
  if (kind === 'link_jira') {
    if (!applied?.issue_key) return null;
    return { kind: 'issue', issueKey: applied.issue_key, issueUrl: applied.issue_url ?? null };
  }
  if (kind !== 'create_ticket' || !applied) return null;
  if (applied.jira?.issue_key) {
    return { kind: 'issue', issueKey: applied.jira.issue_key, issueUrl: applied.jira.issue_url ?? null };
  }
  if (applied.jira_published === false) {
    return { kind: 'not_published', reason: reasonLabel(applied.jira_error ?? 'error') };
  }
  return null;
}

/** What a pending jira_overwrite card says will be replaced. */
export function overwriteDescription(params: Record<string, unknown> | undefined): string {
  const key = paramString(params, 'issue_key') || 'the linked issue';
  return overwriteDirection(params) === 'from_jira'
    ? `Overwrites this ticket’s title and description with ${key}’s. Other fields are kept.`
    : `Overwrites ${key}’s summary and description in Jira with this ticket (default template). The Jira text isn’t kept.`;
}

/**
 * The footer for an action whose every target failed. Jira actions fail for
 * Jira reasons (not just access), and a to_jira replace that timed out may
 * have reached Jira, so it must not claim nothing changed.
 */
export function failedSummary(
  kind: string,
  params: Record<string, unknown> | undefined,
  results: { ok: boolean; error: string | null }[],
): string {
  const error = results.find((r) => !r.ok)?.error ?? null;
  const jiraKind = kind === 'link_jira' || kind === 'jira_overwrite' || (error ?? '').startsWith('jira_');
  if (!jiraKind) return 'Nothing was changed — every target failed its access check.';
  if (kind === 'jira_overwrite' && overwriteDirection(params) === 'to_jira' && error === 'jira_timeout') {
    const key = paramString(params, 'issue_key');
    return `Jira didn’t confirm the update; check ${key || 'the issue'} in Jira.`;
  }
  return `Not applied: ${reasonLabel(error)}.`;
}
