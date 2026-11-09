import RelativeDate from '../../../components/RelativeDate';
import type { TicketReviewState } from '../../../api/client';
import { PaneIcon } from './icons';
import { SectionLabel } from './OverviewTab';
import { Term } from '../../../help/Term';

type ReviewState = NonNullable<TicketReviewState['review_state']>;
type Finding = ReviewState['open_findings'][number] & { closed_round?: number | null };

const SEVERITY_TONE: Record<string, string> = {
  CRITICAL: 'text-danger',
  HIGH: 'text-danger',
  MEDIUM: 'text-warning',
  LOW: 'text-text-secondary',
};

const TONE_TEXT = { info: 'text-info', success: 'text-success', warning: 'text-warning' } as const;

function FindingRow({ f, closed }: { f: Finding; closed: boolean }) {
  const sev = (f.severity || '').toUpperCase();
  return (
    <li className="flex items-start gap-2 border-b border-border py-2 last:border-b-0">
      <span
        className={`mt-px w-16 shrink-0 text-2xs font-semibold uppercase tracking-wide ${
          closed ? 'text-text-tertiary' : SEVERITY_TONE[sev] ?? 'text-text-secondary'
        }`}
      >
        {sev || 'Note'}
      </span>
      <span className={`min-w-0 flex-1 text-sm ${closed ? 'text-text-tertiary line-through' : 'text-text-primary'}`}>
        {f.text}
      </span>
      <span className="shrink-0 text-2xs tabular-nums text-text-tertiary">
        R{f.round}
        {closed ? (f.closed_round ? ` → closed R${f.closed_round}` : ' → closed') : ' · open'}
      </span>
    </li>
  );
}

/** Review rounds and findings as the server derives them from trusted
 *  reviewers' verdict comments; nothing here is computed from untrusted text. */
export function ReviewTab({ review, assignee }: { review: ReviewState | null | undefined; assignee: string | null }) {
  const rounds = review?.rounds ?? [];
  const last = rounds[rounds.length - 1];
  const open = (review?.open_findings ?? []) as Finding[];
  const closed = (review?.closed_findings ?? []) as Finding[];
  const clean = last?.verdict === 'VERIFIED-CLEAN';
  const tone = !last ? 'info' : clean ? 'success' : 'warning';

  const head = !last
    ? {
        title: 'Waiting for the first review',
        text: 'No trusted verdict yet. Verdicts from reviewers the organization registered as trusted count; other reviews show as comments.',
      }
    : clean
      ? { title: `Verified clean, round ${last.round}`, text: open.length ? `${open.length} finding${open.length === 1 ? '' : 's'} still open.` : 'No open findings.' }
      : {
          title: `Changes requested, round ${last.round}`,
          text: `${open.length} open finding${open.length === 1 ? '' : 's'}${assignee ? ` for ${assignee}` : ''}.`,
        };

  return (
    <div className="space-y-6 text-sm">
      <div
        className="flex gap-3 rounded-lg border border-border px-3 py-3"
        style={{ backgroundColor: `color-mix(in srgb, var(--${tone}) 6%, var(--surface))` }}
      >
        <span className={`mt-0.5 shrink-0 ${TONE_TEXT[tone]}`}>{!last ? <PaneIcon name="eye" size={18} /> : clean ? <PaneIcon name="shield" size={18} /> : <PaneIcon name="warn" size={18} />}</span>
        <div className="min-w-0 space-y-0.5">
          <p className="font-semibold text-text-primary">{head.title}</p>
          <p className="text-xs leading-5 text-text-secondary">
            {last && (
              <>
                <Term id="trusted-reviewer">Trusted reviewer</Term> · <RelativeDate iso={last.timestamp} />.{' '}
              </>
            )}
            {head.text}
          </p>
        </div>
      </div>

      {rounds.length > 0 && (
        <section>
          <SectionLabel count={rounds.length}>
            <Term id="review-round">Rounds</Term>
          </SectionLabel>
          <ol className="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-2">
            {rounds.map((r) => {
              const ok = r.verdict === 'VERIFIED-CLEAN';
              return (
                <li
                  key={r.comment_id}
                  className={`rounded-md border border-border border-t-[3px] bg-surface px-2.5 py-2 text-xs ${
                    ok ? 'border-t-success' : 'border-t-warning'
                  }`}
                >
                  <p className="font-semibold text-text-primary">Round {r.round}</p>
                  <p className="text-text-secondary">{ok ? 'Verified clean' : 'Changes requested'}</p>
                  <p className="text-text-tertiary">
                    <RelativeDate iso={r.timestamp} />
                  </p>
                </li>
              );
            })}
          </ol>
        </section>
      )}

      {(open.length > 0 || closed.length > 0) && (
        <section>
          <h3 className="mb-1 text-micro font-semibold uppercase tracking-wide text-text-tertiary">
            Findings
            <span className="ml-1.5 font-medium normal-case tracking-normal">
              {open.length} open · {closed.length} closed
            </span>
          </h3>
          <ul>
            {open.map((f, i) => (
              <FindingRow key={`o${i}`} f={f} closed={false} />
            ))}
            {closed.map((f, i) => (
              <FindingRow key={`c${i}`} f={f} closed />
            ))}
          </ul>
        </section>
      )}

      <section>
        <SectionLabel>Merge readiness</SectionLabel>
        <dl className="space-y-1.5 text-xs">
          <div className="flex gap-2">
            <dt className="w-32 shrink-0 text-text-tertiary">Trusted verdict</dt>
            <dd className="text-text-secondary">
              {!last ? (
                'None yet'
              ) : clean ? (
                <>
                  <Term id="verified-clean">Verified clean</Term> in round {last.round}
                </>
              ) : (
                `Changes requested in round ${last.round}`
              )}
            </dd>
          </div>
          <div className="flex gap-2">
            <dt className="w-32 shrink-0 text-text-tertiary">Open findings</dt>
            <dd className="text-text-secondary">{open.length === 0 ? 'None' : open.length}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="w-32 shrink-0 text-text-tertiary">
              <Term id="merge-check">Merge check</Term>
            </dt>
            <dd className="text-text-secondary">
              Runs on the pull request: the verdict has to be on its exact head commit, from a reviewer who did not
              implement it, with any blocking approvals given.
            </dd>
          </div>
        </dl>
      </section>
    </div>
  );
}
