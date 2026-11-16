import { Eye, Lock, ShieldCheck, TriangleAlert, type LucideIcon } from 'lucide-react';
import type { Ticket } from '../../api/client';
import { Icon } from '../../components/ui/Icon';
import {
  childRollup,
  jiraKey,
  openBlockers,
  reviewSignal,
  reviewSignalText,
  reviewSignalTitle,
  type ById,
} from './views';

/* ── Row signals ──
 *
 * The facts that decide what to do with a ticket, as small pills after the
 * title: the latest trusted review verdict, how many unfinished tickets it
 * waits on, an Issue's child progress, and its Jira key. Each pill carries
 * its meaning in words (icon + text), so colour is never the only cue.
 * `compact` is the phone layout (no Jira, a bare blocker count). */

function Ic({ icon }: { icon: LucideIcon }) {
  return <Icon icon={icon} size={14} className="tk-pill-ic" />;
}

export function Signals({ ticket: t, byId, compact = false }: { ticket: Ticket; byId: ById; compact?: boolean }) {
  const review = reviewSignal(t);
  const blockers = t.status === 'review' ? 0 : openBlockers(t, byId);
  const roll = t.kind === 'issue' ? childRollup(t, byId) : null;
  const jira = compact ? null : jiraKey(t);
  if (!review && !blockers && !roll && !jira) return null;
  return (
    <>
      {review && (
        <span className={`tk-pill tk-pill-${review.kind}`} title={reviewSignalTitle(review)}>
          <Ic icon={review.kind === 'clean' ? ShieldCheck : review.kind === 'changes' ? TriangleAlert : Eye} />
          {reviewSignalText(review)}
        </span>
      )}
      {blockers > 0 && (
        <span
          className="tk-pill tk-pill-blocked"
          title={`Waiting on ${blockers} unfinished ticket${blockers === 1 ? '' : 's'}`}
        >
          <Ic icon={Lock} />
          {compact ? (
            <>
              {blockers}
              <span className="sr-only"> unfinished dependencies</span>
            </>
          ) : (
            `Waiting on ${blockers}`
          )}
        </span>
      )}
      {roll && (
        <span className="tk-pill tk-pill-progress" title={`${roll.finished} of ${roll.total} child tasks finished`}>
          <span className="tk-bar" aria-hidden="true">
            <i style={{ width: `${roll.total ? Math.round((roll.finished / roll.total) * 100) : 0}%` }} />
          </span>
          {roll.finished}/{roll.total}
          <span className="sr-only"> child tasks finished</span>
        </span>
      )}
      {jira && (
        <span className="tk-pill tk-pill-jira" title={`Jira issue ${jira}`}>
          <span className="sr-only">Jira </span>
          {jira}
        </span>
      )}
    </>
  );
}
