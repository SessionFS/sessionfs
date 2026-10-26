/**
 * Confirm card for a write action the helper proposed. Nothing changes until
 * the user presses Confirm: the server re-checks access per target and
 * applies the stored targets, never the model's wording. The title and the
 * target list come from the stored preview.
 */

import { useEffect, useId, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';

import { ApiError, type HelperAction, type HelperActionTarget } from '../api/client';
import { errorMessage } from '../utils/errorMessage';
import { Alert, Badge, Button, MonoText } from '../components/ui';
import {
  actionTitle,
  expiryLabel,
  isCancelledError,
  isExpiredError,
  pendingRecord,
  targetState,
  type TurnAction,
} from './actions';
import { helperActionKey, useDecideHelperAction, useHelperAction } from './useHelper';
import { LayersIcon, RetryIcon, TrashIcon } from './icons';

/** Where Trash lives: the sessions list opened on its Trash view. */
export const TRASH_HREF = '/?view=trash';

/** Sessions stay in Trash this long (`TRASH_RETENTION_DAYS` in services/session_trash.py). */
const TRASH_RETENTION_DAYS = 30;

const STATUS_BADGE: Record<HelperAction['status'], { variant: 'info' | 'success' | 'danger' | 'default'; label: string }> = {
  pending: { variant: 'info', label: 'Awaiting confirmation' },
  confirmed: { variant: 'success', label: 'Done' },
  failed: { variant: 'danger', label: 'Failed' },
  cancelled: { variant: 'default', label: 'Cancelled' },
  expired: { variant: 'default', label: 'Expired' },
};

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  return now;
}

function formatDay(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString('en', { month: 'short', day: 'numeric', year: 'numeric' });
}

function onOff(v: unknown): string {
  return v === true ? 'on' : 'off';
}

function TargetRow({
  kind,
  target,
  result,
}: {
  kind: string;
  target: HelperActionTarget;
  result: { ok: boolean; error: string | null } | undefined;
}) {
  const state = targetState(kind, target, result);
  const isSession = kind === 'trash_sessions';
  const meta: string[] = [];
  if (isSession) {
    if (target.tool) meta.push(target.tool);
    const day = formatDay(target.created_at);
    if (day) meta.push(day);
  } else if (target.change === 'will_change') {
    meta.push(`Auto Narrative ${onOff(target.before)} → ${onOff(target.after)}`);
  } else if (target.change === 'unchanged') {
    meta.push(`Already ${onOff(target.after)}`);
  }
  const reason = 'reason' in state ? state.reason : null;
  return (
    <li className="flex min-w-0 items-start gap-3 px-3 py-2">
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-text-primary" title={target.name}>
          {target.name}
        </p>
        <p className="flex min-w-0 flex-wrap items-center gap-x-1.5 text-2xs text-text-tertiary">
          {meta.length > 0 ? <span>{meta.join(' · ')}</span> : <MonoText value={target.id} tailChars={8} />}
          {reason && (
            <span className={state.tone === 'danger' ? 'text-danger' : 'text-text-secondary'}>
              · {reason}
            </span>
          )}
        </p>
      </div>
      <span className="shrink-0">
        <Badge tint variant={state.tone} label={state.label} />
      </span>
    </li>
  );
}

export default function HelperActionCard({
  action,
  onNavigate,
}: {
  action: TurnAction;
  /** Called when a link in the card navigates away (closes the drawer). */
  onNavigate?: () => void;
}) {
  const titleId = useId();
  const fromHistory = action.fromHistory === true;
  const query = useHelperAction(action.id, pendingRecord(action), fromHistory);
  const decide = useDecideHelperAction(action.id);
  const queryClient = useQueryClient();
  const [lastDecision, setLastDecision] = useState<'confirm' | 'cancel' | null>(null);
  const [error, setError] = useState<unknown>(null);

  const record = query.data;
  const pending = record?.status === 'pending';
  const now = useNow(pending);
  const expiry = pending ? expiryLabel(record.expires_at, now) : null;
  // The clock ran out while the card was open: nothing can change any more.
  const expiredLocally = pending && !!record.expires_at && expiry === null;
  const status: HelperAction['status'] | null = record ? (expiredLocally ? 'expired' : record.status) : null;

  function run(decision: 'confirm' | 'cancel') {
    setLastDecision(decision);
    setError(null);
    decide.mutate(decision, {
      onError: (err) => {
        if (record && (isExpiredError(err) || isCancelledError(err))) {
          queryClient.setQueryData<HelperAction>(helperActionKey(action.id), {
            ...record,
            status: isExpiredError(err) ? 'expired' : 'cancelled',
          });
          return;
        }
        setError(err);
      },
    });
  }

  const isTrash = action.kind === 'trash_sessions';
  const display = record ?? pendingRecord(action);
  // A restored card has no preview until the server read lands.
  const title = record || !fromHistory ? actionTitle(display) : 'Loading proposed change…';
  const targets = display.targets.length > 0 ? display.targets : action.targets;
  const results = new Map((record?.result?.targets ?? []).map((r) => [r.id, r]));
  const changing = targets.filter((t) => t.change === 'will_change').length;
  const unchanged = targets.filter((t) => t.change === 'unchanged').length;
  const skipped = targets.filter((t) => t.change === 'skipped').length;
  const applied = [...results.values()].filter((r) => r.ok).length;
  const failed = results.size - applied;
  const appliedCount = results.size > 0 ? applied : changing;
  const noun = (n: number) => (isTrash ? (n === 1 ? 'session' : 'sessions') : n === 1 ? 'project' : 'projects');

  const counts = [
    changing ? `${changing} will change` : null,
    unchanged ? `${unchanged} unchanged` : null,
    skipped ? `${skipped} skipped` : null,
  ]
    .filter(Boolean)
    .join(' · ');

  let badge = status ? STATUS_BADGE[status] : null;
  if (status === 'confirmed' && failed > 0) badge = { variant: 'info', label: 'Partly applied' };

  let footer: ReactNode = null;
  let announcement = '';
  if (fromHistory && query.isLoading) {
    footer = <p className="text-xs text-text-tertiary">Checking this action’s status…</p>;
  } else if (query.isError) {
    const gone = query.error instanceof ApiError && query.error.status === 404;
    footer = (
      <Alert
        variant="warning"
        title={gone ? 'This action is no longer available' : 'Couldn’t check this action’s status'}
        action={
          gone ? undefined : (
            <Button size="sm" variant="secondary" onClick={() => void query.refetch()}>
              <RetryIcon size={13} />
              Retry
            </Button>
          )
        }
      />
    );
  } else if (status === 'pending') {
    footer = (
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Button
          size="sm"
          variant={isTrash ? 'danger' : 'primary'}
          loading={decide.isPending && lastDecision === 'confirm'}
          disabled={decide.isPending}
          onClick={() => run('confirm')}
        >
          {isTrash ? 'Move to Trash' : 'Confirm'}
        </Button>
        <Button
          size="sm"
          variant="secondary"
          loading={decide.isPending && lastDecision === 'cancel'}
          disabled={decide.isPending}
          onClick={() => run('cancel')}
        >
          Cancel
        </Button>
        <span className="ml-auto text-2xs tabular-nums text-text-tertiary">
          {decide.isPending && lastDecision === 'confirm' ? 'Applying…' : expiry}
        </span>
      </div>
    );
  } else if (status === 'confirmed') {
    announcement = failed > 0 ? `Applied ${applied} of ${results.size}. ${failed} failed.` : 'Action applied.';
    footer = (
      <div className="space-y-1.5 text-xs text-text-secondary">
        <p>
          {failed > 0
            ? `Applied ${applied} of ${results.size}; ${failed} failed.`
            : `Applied to ${appliedCount} ${noun(appliedCount)}.`}
        </p>
        {isTrash && applied > 0 && (
          <p>
            Moved to Trash — restore from{' '}
            <Link
              to={TRASH_HREF}
              onClick={onNavigate}
              className="font-medium text-brand underline-offset-2 hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] rounded-sm"
            >
              Trash
            </Link>{' '}
            within {TRASH_RETENTION_DAYS} days.
          </p>
        )}
      </div>
    );
  } else if (status === 'failed') {
    announcement = 'The action failed. Nothing was changed.';
    footer = <p className="text-xs text-text-secondary">Nothing was changed — every target failed its access check.</p>;
  } else if (status === 'cancelled') {
    announcement = lastDecision === 'cancel' ? 'Action cancelled.' : '';
    footer = <p className="text-xs text-text-secondary">Cancelled — nothing was changed.</p>;
  } else if (status === 'expired') {
    footer = (
      <p className="text-xs text-text-secondary">
        Expired before it was confirmed — nothing was changed. Ask again for a fresh proposal.
      </p>
    );
  }

  return (
    <section
      aria-labelledby={titleId}
      className="mt-3 overflow-hidden rounded-xl border border-border bg-bg-primary/60"
    >
      <div className="flex items-start gap-2.5 px-3 pt-3">
        <span
          className={`mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg ${
            isTrash
              ? 'bg-[color-mix(in_oklch,var(--danger)_14%,transparent)] text-danger'
              : 'bg-[color-mix(in_oklch,var(--brand)_14%,transparent)] text-brand'
          }`}
          aria-hidden="true"
        >
          {isTrash ? <TrashIcon size={15} /> : <LayersIcon size={15} />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-2 gap-y-1">
            <p className="text-2xs font-semibold uppercase tracking-wide text-text-tertiary">Proposed change</p>
            {badge && <Badge tint variant={badge.variant} label={badge.label} />}
          </div>
          <h3 id={titleId} className="mt-0.5 text-sm font-semibold text-text-primary [overflow-wrap:anywhere]">
            {title}
          </h3>
          {counts && <p className="mt-0.5 text-2xs text-text-tertiary">{counts}</p>}
        </div>
      </div>

      {targets.length > 0 && (
        <ul
          aria-label={`Targets of: ${title}`}
          tabIndex={0}
          className="mx-3 mt-2.5 max-h-56 divide-y divide-border overflow-y-auto rounded-lg border border-border bg-surface outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
        >
          {targets.map((t) => (
            <TargetRow key={t.id} kind={action.kind} target={t} result={results.get(t.id)} />
          ))}
        </ul>
      )}

      <div className="space-y-2 px-3 py-3">
        {error != null && (
          <Alert
            variant="danger"
            title={lastDecision === 'cancel' ? 'Couldn’t cancel' : 'Couldn’t apply the change'}
            action={
              lastDecision ? (
                <Button size="sm" variant="secondary" onClick={() => run(lastDecision)}>
                  <RetryIcon size={13} />
                  Retry
                </Button>
              ) : undefined
            }
          >
            <p>{errorMessage(error)}</p>
          </Alert>
        )}
        {footer}
      </div>

      <span className="sr-only" role="status" aria-live="polite">
        {announcement}
      </span>
    </section>
  );
}
