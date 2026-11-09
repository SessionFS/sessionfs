import type { ReactNode } from 'react';
import type { Ticket } from '../../../api/client';
import { RemindersStrip } from '../../../reminders/RemindersStrip';
import { Badge } from '../../../components/Badge';
import { Button } from '../../../components/ui';
import { TicketFacts } from '../ScheduleFields';
import { TicketLinkRow } from './TicketLinkRow';

export function SectionLabel({ children, count }: { children: ReactNode; count?: number }) {
  return (
    <h3 className="mb-2 text-micro font-semibold uppercase tracking-wide text-text-tertiary">
      {children}
      {count !== undefined && <span className="ml-1.5 font-medium tabular-nums">{count}</span>}
    </h3>
  );
}

export interface FollowState {
  watching: boolean;
  muted: boolean;
  followPending: boolean;
  unfollowPending: boolean;
  onFollow: () => void;
  onUnfollow: () => void;
  onToggleMute: () => void;
}

export function OverviewTab({
  projectId,
  ticket: t,
  dependencies,
  onOpenTicket,
  canEditFields,
  needsReapproval,
  follow,
}: {
  projectId: string;
  ticket: Ticket;
  /** depends_on ids with whatever has loaded for each. */
  dependencies: { id: string; ticket?: Ticket; failed?: boolean }[];
  onOpenTicket: (id: string) => void;
  canEditFields: boolean;
  needsReapproval: boolean;
  follow: FollowState;
}) {
  return (
    <div className="space-y-6 text-sm">
      <section>
        <SectionLabel>Description</SectionLabel>
        {t.description ? (
          <p className="whitespace-pre-wrap leading-6 text-text-primary">{t.description}</p>
        ) : (
          <p className="text-text-tertiary">No description.</p>
        )}
      </section>

      {t.acceptance_criteria.length > 0 && (
        <section>
          <SectionLabel count={t.acceptance_criteria.length}>Acceptance criteria</SectionLabel>
          <ul className="space-y-1.5">
            {t.acceptance_criteria.map((c, i) => (
              <li key={i} className="flex items-start gap-2 text-text-secondary">
                <span aria-hidden="true" className="mt-1 inline-block h-3 w-3 shrink-0 rounded-sm border border-border-strong" />
                <span>{c}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {t.completion_notes && (
        <section>
          <SectionLabel>Completion notes</SectionLabel>
          <p className="whitespace-pre-wrap text-text-secondary">{t.completion_notes}</p>
        </section>
      )}

      {dependencies.length > 0 && (
        <section>
          <SectionLabel count={dependencies.length}>Depends on</SectionLabel>
          <ul className="space-y-1.5">
            {dependencies.map((d) => (
              <li key={d.id}>
                <TicketLinkRow id={d.id} ticket={d.ticket} failed={d.failed} onOpen={onOpenTicket} />
              </li>
            ))}
          </ul>
        </section>
      )}

      <section>
        <SectionLabel>Details</SectionLabel>
        <div className="space-y-3">
          <TicketFacts projectId={projectId} ticket={t} canEdit={canEditFields} needsReapproval={needsReapproval} />
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            {follow.watching ? (
              <>
                <Badge variant="success" tint label={follow.muted ? 'Following (muted)' : 'Following'} size="sm" />
                <Button variant="ghost" size="sm" onClick={follow.onToggleMute} aria-pressed={follow.muted}>
                  {follow.muted ? 'Unmute' : 'Mute'}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={follow.unfollowPending}
                  loading={follow.unfollowPending}
                  onClick={follow.onUnfollow}
                >
                  Unfollow
                </Button>
              </>
            ) : (
              <>
                <span className="text-text-tertiary">Not following</span>
                <Button variant="secondary" size="sm" loading={follow.followPending} onClick={follow.onFollow}>
                  Follow
                </Button>
              </>
            )}
          </div>
          <RemindersStrip kind="ticket" projectId={projectId} sourceId={t.id} />
        </div>
      </section>
    </div>
  );
}
