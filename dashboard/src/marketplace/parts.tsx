import { Link } from 'react-router-dom';
import { Badge } from '../components/Badge';
import type { Explanation } from './marketplaceErrors';
import { STATE_COPY, isRevocationFeedIncomplete, type ModerationState } from './moderationState';

/**
 * Shown when the revocations feed could not be read to its end. A truncated
 * replay is never used: labels come from the last COMPLETE read (if any), so
 * they may be missing recent changes and are flagged instead of presented as
 * current.
 */
export function RevocationIncompleteNotice({
  error,
  hasPrevious,
  className = '',
}: {
  error: unknown;
  hasPrevious: boolean;
  className?: string;
}) {
  if (!isRevocationFeedIncomplete(error)) return null;
  return (
    <div role="status" className={`rounded-lg border border-warning/40 bg-[rgba(240,192,64,0.06)] p-3 ${className}`}>
      <p className="text-sm font-semibold text-warning">Moderation state is incomplete</p>
      <p className="mt-1 text-xs leading-5 text-text-secondary">
        {hasPrevious
          ? 'The revocations feed is too long to load in full here, so yanked and blocked labels are from the last complete read and may be missing recent changes.'
          : 'The revocations feed is too long to load in full here, so yanked and blocked labels are not shown.'}{' '}
        Treat a package without a label as unconfirmed; the server still refuses downloads of yanked or blocked
        versions.
      </p>
    </div>
  );
}

/** Yanked / blocked badge; renders nothing for active rows. */
export function ModerationStateBadge({ state, tint = true }: { state: ModerationState; tint?: boolean }) {
  if (state === 'active') return null;
  const copy = STATE_COPY[state];
  return (
    <span title={copy.help} className="inline-flex">
      <Badge label={copy.label} variant={state === 'blocked' ? 'danger' : 'warning'} tint={tint} />
    </span>
  );
}

/** Plain-language server failure: what happened, how to fix it, and the stable code. */
export function ExplanationAlert({
  explanation,
  tone = 'danger',
}: {
  explanation: Explanation;
  tone?: 'danger' | 'warning';
}) {
  const upgrade = explanation.code?.startsWith('upgrade_required');
  const palette = tone === 'danger'
    ? 'border-danger/40 bg-[rgba(240,64,96,0.06)]'
    : 'border-warning/40 bg-[rgba(240,192,64,0.06)]';
  return (
    <div role="alert" className={`rounded-lg border p-3 ${palette}`}>
      <p className={`text-sm font-semibold ${tone === 'danger' ? 'text-danger' : 'text-warning'}`}>{explanation.title}</p>
      {explanation.fix && <p className="mt-1 text-xs leading-5 text-text-secondary">{explanation.fix}</p>}
      <div className="mt-2 flex flex-wrap items-center gap-3">
        {upgrade && (
          <Link to="/settings/billing" className="text-xs font-semibold text-brand hover:underline">
            View plans and billing
          </Link>
        )}
        {explanation.code && <span className="font-mono text-2xs text-text-tertiary">{explanation.code}</span>}
      </div>
    </div>
  );
}
