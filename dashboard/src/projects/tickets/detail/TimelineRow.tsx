import type { ReactNode } from 'react';
import RelativeDate from '../../../components/RelativeDate';
import { Badge } from '../../../components/Badge';
import { describeTicketEvent } from '../events';
import { Term } from '../../../help/Term';
import { jiraSyncEventLabel, jiraSyncStateLabel } from '../../../utils/jira';
import type { JiraDelivery, TimelineItem } from './timeline';

/* Timeline rows, moved unchanged from the old TicketDetail.tsx. */

export function DeliveryBadge({ delivery }: { delivery: JiraDelivery }) {
  const state = delivery.state ?? (delivery.applied ? 'done' : 'pending');
  const badge =
    state === 'done'
      ? { variant: 'success' as const, label: 'Sent to Jira' }
      : state === 'skipped'
        ? { variant: 'default' as const, label: 'Not sent to Jira' }
        : state === 'exhausted'
          ? { variant: 'danger' as const, label: 'Jira: gave up' }
          : (delivery.retry_count ?? 0) > 0
            ? { variant: 'warning' as const, label: `Jira: retrying (attempt ${(delivery.retry_count ?? 0) + 1})` }
            : { variant: 'info' as const, label: 'Jira: queued' };
  return (
    <span title={delivery.error ?? undefined}>
      <Badge variant={badge.variant} label={badge.label} size="sm" tint />
    </span>
  );
}

export function Dot({ tone = 'quiet' }: { tone?: 'quiet' | 'comment' | 'success' | 'warning' }) {
  const cls =
    tone === 'comment'
      ? 'h-2.5 w-2.5 bg-bg-elevated border-2 border-border-strong'
      : tone === 'success'
        ? 'h-2.5 w-2.5 bg-success'
        : tone === 'warning'
          ? 'h-2.5 w-2.5 bg-warning'
          : 'h-1.5 w-1.5 bg-border-strong';
  return (
    <span
      aria-hidden="true"
      className={`absolute top-2 -translate-x-1/2 rounded-full ${cls}`}
      style={{ left: '-0.5px' }}
    />
  );
}

export function QuietRow({ children, at, below }: { children: ReactNode; at: string; below?: ReactNode }) {
  return (
    <li className="relative pl-5 py-1">
      <Dot />
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs text-text-tertiary">
        {children}
        <span className="ml-auto shrink-0">
          <RelativeDate iso={at} />
        </span>
      </div>
      {below && <div className="mt-0.5 break-all text-xs text-text-tertiary">{below}</div>}
    </li>
  );
}

export function TimelineRow({ item }: { item: TimelineItem }) {
  if (item.kind === 'event') {
    return <QuietRow at={item.at}>{item.text}</QuietRow>;
  }

  if (item.kind === 'more') return null; // rendered by the list owner

  if (item.kind === 'ticketEvent') {
    const { actor, text } = describeTicketEvent(item.event, item.names);
    return (
      <QuietRow at={item.at}>
        <span>
          <span className="font-medium text-text-secondary">{actor}</span> {text}
        </span>
      </QuietRow>
    );
  }

  if (item.kind === 'sync') {
    const e = item.event;
    const state = jiraSyncStateLabel(e);
    return (
      <QuietRow at={item.at} below={e.error}>
        <span className="text-text-secondary">{jiraSyncEventLabel(e)}</span>
        {e.issue_key && <span className="font-mono">{e.issue_key}</span>}
        <span className="sr-only">{e.direction}</span>
        {state && <Badge variant={state.variant} label={state.label} size="sm" tint />}
      </QuietRow>
    );
  }

  const c = item.comment;
  // Edit diffs and approval notes are written by the server as "system".
  if (c.author_persona === 'system') {
    return (
      <QuietRow at={item.at}>
        <span className="whitespace-pre-wrap">{c.content}</span>
      </QuietRow>
    );
  }

  const author = c.author_persona ?? (c.source === 'jira' ? 'From Jira' : c.author_user_id.slice(0, 8));
  const v = item.verdict;
  const clean = v?.verdict === 'VERIFIED-CLEAN';
  return (
    <li className="relative pl-5 py-2">
      <Dot tone={v ? (clean ? 'success' : 'warning') : 'comment'} />
      <div
        className={`rounded-lg border px-3 py-2 ${
          v ? `border-border border-l-[3px] bg-surface ${clean ? 'border-l-success' : 'border-l-warning'}` : 'border-border bg-surface'
        }`}
      >
        <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-text-tertiary">
          <span className="font-medium text-text-primary">{author}</span>
          {c.source === 'jira' && <Badge variant="info" label="Jira" size="sm" />}
          {v && (
            <>
              <Badge
                variant={clean ? 'success' : 'warning'}
                tint
                label={`Round ${v.round}: ${clean ? 'verified clean' : 'changes requested'}`}
                size="sm"
              />
              <span className="text-2xs">
                <Term id="trusted-reviewer">Trusted reviewer</Term>
              </span>
            </>
          )}
          {item.delivery && <DeliveryBadge delivery={item.delivery} />}
          <span className="ml-auto">
            <RelativeDate iso={c.created_at} />
          </span>
        </div>
        <p className="whitespace-pre-wrap text-sm text-text-primary">{c.content}</p>
        {item.delivery?.error && item.delivery.state !== 'done' && (
          <p className="mt-1 break-all text-xs text-text-tertiary">{item.delivery.error}</p>
        )}
      </div>
    </li>
  );
}
