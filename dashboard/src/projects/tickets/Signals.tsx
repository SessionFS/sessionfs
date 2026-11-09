import type { Ticket } from '../../api/client';
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

function Ic({ d }: { d: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" className="tk-pill-ic">
      <path d={d} />
    </svg>
  );
}

const SHIELD = 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM8.5 12l2.5 2.5 4.5-5';
const WARN = 'M12 3l9.5 17h-19zM12 10v4M12 17v.01';
const EYE = 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6';
const LOCK = 'M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5z';

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
          <Ic d={review.kind === 'clean' ? SHIELD : review.kind === 'changes' ? WARN : EYE} />
          {reviewSignalText(review)}
        </span>
      )}
      {blockers > 0 && (
        <span
          className="tk-pill tk-pill-blocked"
          title={`Waiting on ${blockers} unfinished ticket${blockers === 1 ? '' : 's'}`}
        >
          <Ic d={LOCK} />
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
