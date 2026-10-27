import type { ReactNode } from 'react';
import type { MarketplacePackage } from '../api/client';
import { capabilitySummary, isVerifiedPublisher, VISIBILITY_LABELS } from './capabilities';
import { FreshnessBadges } from './discoveryParts';
import { installsLabel } from './discoveryUtils';
import { AlertIcon, ShieldCheckIcon } from './icons';
import type { ModerationState } from './moderationState';
import { ModerationStateBadge } from './parts';
import { ListingReviewBadge, VerifiedMark } from './publisherParts';

/** Joins meta parts with a dot, never leaving a dangling separator. */
function MetaLine({ parts }: { parts: ReactNode[] }) {
  return (
    <>
      {parts.map((part, index) => (
        <span key={index} className="inline-flex items-center gap-2">
          {index > 0 && <span aria-hidden="true" className="opacity-40">·</span>}
          {part}
        </span>
      ))}
    </>
  );
}

/**
 * One package in the browse grid. The whole card is clickable through the
 * title button's ::after overlay (one interactive element, no nesting);
 * the verified mark sits above the overlay so its tooltip still shows.
 */
export function PackageCard({ item, state, onOpen }: { item: MarketplacePackage; state: ModerationState; onOpen: () => void }) {
  const capability = capabilitySummary(item.risk);
  const statusBadges = (
    <>
      <ListingReviewBadge item={item} />
      <ModerationStateBadge state={state} />
      <FreshnessBadges item={item} />
    </>
  );
  const meta: ReactNode[] = [
    <span key="i" title="Installs in the last 30 days">{installsLabel(item.installs?.total)} installs</span>,
  ];
  return (
    <article className="group relative flex h-full min-w-0 flex-col rounded-lg border border-border bg-surface p-4 transition-[background-color,border-color] duration-150 ease-out hover:border-border-strong hover:bg-bg-elevated">
      <div className="flex h-5 min-w-0 items-center gap-1.5 text-xs text-text-tertiary">
        <span className="truncate font-mono" title={`${item.collection} / ${item.namespace}`}>{item.namespace}</span>
        <VerifiedMark item={item} />
        <span className="ml-auto inline-flex shrink-0 items-center gap-2">
          {item.visibility !== 'public' && (
            <span className="font-medium text-text-secondary" data-testid="card-visibility">
              {VISIBILITY_LABELS[item.visibility] ?? item.visibility}
            </span>
          )}
          <span className="font-mono" data-testid="card-version">{item.version || 'unversioned'}</span>
        </span>
      </div>
      <h3 className="mt-1 text-md font-semibold text-text-primary">
        <button
          type="button"
          onClick={onOpen}
          title={item.display_name}
          className="block w-full truncate text-left outline-none after:absolute after:inset-0 after:rounded-lg focus-visible:after:shadow-[0_0_0_3px_var(--brand-glow)]"
        >
          {item.display_name}
          {isVerifiedPublisher(item) && <span className="sr-only">, verified publisher</span>}
        </button>
      </h3>
      <p className="mt-1 line-clamp-2 text-sm leading-5 text-text-secondary">
        {item.description || 'No description provided.'}
      </p>
      <div className="mt-2 flex flex-wrap gap-1 empty:hidden">{statusBadges}</div>
      <div className="mt-auto flex min-w-0 items-center justify-between gap-3 pt-3 text-xs text-text-tertiary">
        <span className="inline-flex shrink-0 items-center gap-2">
          <MetaLine parts={meta} />
        </span>
        <span
          className={`inline-flex min-w-0 items-center gap-1 ${capability.elevated ? 'text-warning' : ''}`}
          data-testid="capability-summary"
          title={capability.title}
        >
          {capability.elevated ? <AlertIcon size={13} /> : <ShieldCheckIcon size={13} />}
          <span className="truncate">{capability.label}</span>
          <span className="sr-only">. {capability.title}</span>
        </span>
      </div>
    </article>
  );
}
