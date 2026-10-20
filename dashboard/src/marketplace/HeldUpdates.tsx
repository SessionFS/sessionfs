import { useState } from 'react';
import { ApiError, type HeldUpdate } from '../api/client';
import { Badge } from '../components/Badge';
import { Button, Select } from '../components/ui';
import { useAdvanceAssignment, useHeldUpdates, useMarketplaceDisclosure } from '../hooks/useMarketplaceDisclosure';
import { useToast } from '../hooks/useToast';
import { DisclosureCard, UpdateDiff } from './DisclosureCard';
import type { OrgMembership } from './moderationState';

/**
 * Held updates (marketplace v2 §3.4): org assignments whose package has a
 * newer version. The admin reviews the target version's full disclosure
 * and the update diff, acknowledges code or widening changes (bound to the
 * exact card reviewed), and advances the assignment. Devices never apply a
 * widening or code update on their own.
 */
function HeldUpdateRow({ orgId, item, onStale }: { orgId: string; item: HeldUpdate; onStale: () => void }) {
  const disclosure = useMarketplaceDisclosure(item.to_version_id, item.assignment.target_tools, 'user');
  const advance = useAdvanceAssignment(orgId);
  const { addToast } = useToast();
  const [resetToken, setResetToken] = useState(0);
  // Any change to the card, the update reviewed (its digest covers the
  // baseline, target and full diff) or a refusal clears the acknowledgement.
  const reviewKey = `${disclosure.data?.disclosure_digest ?? ''}|${item.to_version_id}|${item.update_class}|${item.update_digest ?? ''}|${resetToken}`;
  const [acknowledgedKey, setAcknowledgedKey] = useState<string | null>(null);
  const acknowledged = acknowledgedKey === reviewKey;
  const ready = !!disclosure.data && !disclosure.error && !disclosure.isFetching;
  const needsAck = item.update_class === 'widening' || item.update_class === 'code';

  async function submit() {
    if (!disclosure.data) return;
    try {
      await advance.mutateAsync({
        assignmentId: item.assignment.id,
        input: {
          to_version_id: item.to_version_id,
          content_hash: item.to_content_hash,
          ...(item.update_class === 'widening' ? { approve_widening: true } : {}),
          ...(item.update_class === 'code' ? { approve_code_change: true } : {}),
          capability_digest: disclosure.data.capability_digest,
          consent_rule_version: disclosure.data.consent_rule_version ?? 2,
          disclosure_digest: disclosure.data.disclosure_digest ?? null,
          ...(item.update_digest ? { update_digest: item.update_digest } : {}),
        },
      });
      addToast('success', `Advanced ${item.name} to ${item.to_version || item.to_version_id}.`);
    } catch (error) {
      if (error instanceof ApiError && error.code === 'disclosure_changed') {
        void disclosure.refetch?.();
        setResetToken((value) => value + 1);
        addToast('error', 'The disclosure changed. Review the updated disclosure and advance again.');
        return;
      }
      if (
        error instanceof ApiError &&
        (error.code === 'reapproval_required' ||
          error.code === 'assignment_changed' ||
          error.code === 'assignment_revoked' ||
          error.code === 'assignment_superseded')
      ) {
        // The update shown is stale (another advance, a new baseline or a
        // changed diff) or the assignment itself is gone (another admin
        // advanced it first, or moderation revoked it): reload the queue so
        // the refreshed state is reviewed, and clear the acknowledgement.
        onStale();
        setResetToken((value) => value + 1);
        addToast(
          'error',
          error.code === 'assignment_revoked' || error.code === 'assignment_superseded'
            ? 'This assignment was already changed elsewhere. The queue was refreshed.'
            : error.code === 'assignment_changed'
              ? 'This assignment changed. Review the refreshed update and advance again.'
              : 'The update changed since you reviewed it. Review the refreshed changes and advance again.',
        );
        return;
      }
      addToast('error', `Advance failed: ${String(error)}`);
    }
  }

  return (
    <li className="rounded-lg border border-border bg-bg-elevated p-4" data-testid={`held-update-${item.assignment.id}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold text-text-primary">{item.name}</span>
        <span className="font-mono text-xs text-text-tertiary">
          {item.from_version || item.from_version_id} → {item.to_version || item.to_version_id}
        </span>
        <Badge label={`${item.assignment.mode} · ${item.assignment.target_type}`} tint />
      </div>
      <div className="mt-3">
        {item.diff ? (
          <UpdateDiff diff={item.diff} />
        ) : (
          <p role="alert" className="text-xs text-danger" data-testid="held-update-diff-missing">
            The update diff is unavailable, so this update cannot be acknowledged. Reload the queue and try again.
          </p>
        )}
        {!item.update_digest && (
          <p role="alert" className="mt-2 text-xs text-danger" data-testid="held-update-digest-missing">
            The update digest is unavailable, so this update cannot be acknowledged. Reload the queue and try again.
          </p>
        )}
      </div>
      {disclosure.isLoading && <p className="mt-2 text-xs text-text-tertiary">Loading disclosure…</p>}
      {disclosure.error && (
        <p role="alert" className="mt-2 text-xs text-danger">Disclosure unavailable: {String(disclosure.error)}</p>
      )}
      {disclosure.data && <div className="mt-3"><DisclosureCard disclosure={disclosure.data} /></div>}
      {needsAck && item.diff && (
        <label className="mt-3 flex items-start gap-2 text-xs text-text-primary">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={acknowledged}
            onChange={(event) => setAcknowledgedKey(event.target.checked ? reviewKey : null)}
            aria-label={`Acknowledge the update of ${item.name}`}
          />
          <span>
            {item.update_class === 'widening'
              ? 'I reviewed the new access above and approve it for every target of this assignment.'
              : 'I reviewed the changed executable content above and approve it.'}
          </span>
        </label>
      )}
      <div className="mt-3">
        <Button
          size="sm"
          onClick={() => void submit()}
          disabled={!ready || (needsAck && !acknowledged) || !item.diff || !item.update_digest}
          loading={advance.isPending}
        >
          Advance
        </Button>
      </div>
    </li>
  );
}

export default function HeldUpdates({ orgId }: { orgId: string }) {
  const updates = useHeldUpdates(orgId);
  const items = updates.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <section aria-labelledby="held-updates-heading" className="mt-4 rounded-lg border border-border bg-bg-sunken p-4">
      <h3 id="held-updates-heading" className="text-sm font-semibold text-text-primary">Updates to review</h3>
      <p className="mt-1 text-xs text-text-secondary">
        Assigned packages with a newer version. Updates that change executable content or ask for more access stay on the
        current version until you advance them.
      </p>
      {updates.isLoading ? (
        <p className="mt-3 text-sm text-text-tertiary">Loading updates…</p>
      ) : updates.error ? (
        <p role="alert" className="mt-3 text-sm text-danger">Updates unavailable: {String(updates.error)}</p>
      ) : items.length === 0 ? (
        <p className="mt-3 text-sm text-text-tertiary">No assigned package has a newer version.</p>
      ) : (
        <ul className="mt-3 space-y-3">
          {items.map((item) => (
            <HeldUpdateRow key={item.assignment.id} orgId={orgId} item={item} onStale={() => void updates.refetch()} />
          ))}
        </ul>
      )}
      {updates.hasNextPage && (
        <div className="mt-3 flex justify-center">
          <Button variant="secondary" size="sm" onClick={() => void updates.fetchNextPage()} loading={updates.isFetchingNextPage}>
            Load more
          </Button>
        </div>
      )}
    </section>
  );
}

/** Held-update queues for every org the viewer administers (selector when several). */
export function HeldUpdateQueues({ orgs }: { orgs: OrgMembership[] }) {
  const [chosen, setChosen] = useState<string>('');
  if (orgs.length === 0) return null;
  const orgId = orgs.some((org) => org.org_id === chosen) ? chosen : orgs[0].org_id;
  return (
    <div data-testid="held-update-queues">
      {orgs.length > 1 && (
        <div className="mt-4 max-w-xs">
          <Select
            aria-label="Organization for updates to review"
            title="Organization"
            value={orgId}
            onValueChange={setChosen}
            options={orgs.map((org) => ({ value: org.org_id, label: org.name || org.org_id }))}
          />
        </div>
      )}
      <HeldUpdates key={orgId} orgId={orgId} />
    </div>
  );
}
