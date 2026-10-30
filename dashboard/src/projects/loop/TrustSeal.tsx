import { useId, useState } from 'react';
import RelativeDate from '../../components/RelativeDate';
import { Button, Dialog, DialogFooter, DialogHeader, MonoText } from '../../components/ui';
import type { Readiness } from '../../api/reviewLoop';
import { shortSha } from './format';

interface TrustSealProps {
  readiness: Pick<Readiness, 'ready' | 'reasons' | 'verdict'>;
  /** Compact: one line, reasons collapsed to the first one. */
  compact?: boolean;
}

function Check() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

/**
 * The trust seal: who reviewed, which commit, when. Click opens the verdict.
 * When the change is not ready it says so and lists why. Quiet by design:
 * a sealed change is the normal state, so no loud colour.
 */
export function TrustSeal({ readiness, compact = false }: TrustSealProps) {
  const [open, setOpen] = useState(false);
  const titleId = useId();
  const { ready, reasons, verdict } = readiness;

  if (!ready) {
    const shown = compact ? reasons.slice(0, 1) : reasons;
    const more = reasons.length - shown.length;
    return (
      <div className="text-xs text-text-secondary" data-testid="trust-seal-not-ready">
        <span className="inline-flex items-center gap-1.5 rounded-full border border-border px-2 py-0.5 font-medium text-text-secondary">
          Not ready
        </span>
        {shown.length > 0 && (
          <ul className="mt-1.5 space-y-0.5 text-text-tertiary">
            {shown.map((r) => (
              <li key={r}>{r}</li>
            ))}
            {more > 0 && <li>and {more} more</li>}
          </ul>
        )}
      </div>
    );
  }

  if (!verdict) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border border-border px-2 py-0.5 text-xs font-medium text-text-secondary">
        <Check /> Ready
      </span>
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-xs text-text-secondary outline-none transition-colors hover:border-border-strong hover:bg-surface-hover focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      >
        <span className="text-brand"><Check /></span>
        <span className="font-medium text-text-primary">{verdict.reviewer || 'Trusted reviewer'}</span>
        <span className="text-text-tertiary" aria-hidden="true">·</span>
        <span className="font-mono">{shortSha(verdict.reviewed_commit)}</span>
        <span className="text-text-tertiary" aria-hidden="true">·</span>
        {verdict.at && <RelativeDate iso={verdict.at} />}
        <span className="sr-only">Open the verdict</span>
      </button>
      <Dialog open={open} onClose={() => setOpen(false)} titleId={titleId}>
        <DialogHeader titleId={titleId}>Verdict</DialogHeader>
        <dl className="space-y-3 text-sm">
          <div>
            <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Reviewed by</dt>
            <dd className="text-text-primary">{verdict.reviewer || 'Trusted reviewer'}</dd>
          </div>
          <div>
            <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">Reviewed commit</dt>
            <dd>{verdict.reviewed_commit ? <MonoText value={verdict.reviewed_commit} copyable /> : <span className="text-text-primary">Not recorded</span>}</dd>
          </div>
          <div>
            <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">At</dt>
            <dd className="text-text-primary">{verdict.at ? new Date(verdict.at).toLocaleString() : 'Time not recorded'}</dd>
          </div>
        </dl>
        <p className="mt-4 text-xs text-text-tertiary">
          A trusted reviewer other than the author found no open findings on exactly this commit.
        </p>
        <DialogFooter>
          <Button variant="secondary" onClick={() => setOpen(false)}>Close</Button>
        </DialogFooter>
      </Dialog>
    </>
  );
}
