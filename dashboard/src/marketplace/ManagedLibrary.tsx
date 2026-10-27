import { useState } from 'react';
import type { SkillAssignment } from '../api/client';
import { useEffectiveSkillAssignments, useOrgSkillControl } from '../hooks/useMarketplace';
import { useHeldUpdateCounts } from '../hooks/useMarketplaceDisclosure';
import { useMe } from '../hooks/useMe';
import { useMyOrgs } from '../transfers/useTransfers';
import { errorMessage } from '../utils/errorMessage';
import { HeldUpdateQueues } from './HeldUpdates';
import { ChevronIcon } from './icons';
import { adminOrgs, type OrgMembership } from './moderationState';
import { RegistryList } from './RegistryList';
import { isTierGate } from './tierGate';

const DOCS_URL = 'https://sessionfs.dev/skills-marketplace/#4-organization-assignments';

const TARGET_COPY: Record<SkillAssignment['target_type'], string> = {
  org: 'everyone in the organization',
  user: 'you',
  device: 'one device',
};

function DocsLink() {
  return (
    <a href={DOCS_URL} target="_blank" rel="noopener noreferrer" className="font-medium text-brand hover:underline">
      How organization libraries work
    </a>
  );
}

/** Held-update review for administrators: collapsed with the total across
 *  every org they administer, open by default when any org has updates. */
function UpdatesToReview({ orgs: administered, standalone = false }: { orgs: OrgMembership[]; standalone?: boolean }) {
  const [chosen, setChosen] = useState('');
  const [open, setOpen] = useState<boolean | null>(null);
  const counts = useHeldUpdateCounts(administered.map((org) => org.org_id));
  // Orgs below the Team plan have no assignments to update: leave them out.
  const orgs = administered.filter((org) => !isTierGate(counts.errors[org.org_id]));
  if (orgs.length === 0) return null;
  // Start on an org that has something waiting.
  const firstWithUpdates = orgs.find((org) => (counts.byOrg[org.org_id] ?? 0) > 0)?.org_id;
  const orgId = orgs.some((org) => org.org_id === chosen) ? chosen : firstWithUpdates ?? orgs[0].org_id;
  // An org whose count failed for another reason is unknown, not zero.
  const unknown = Object.values(counts.errors).some((error) => !isTierGate(error));
  const label = !counts.loaded
    ? null
    : unknown
      ? counts.total > 0 ? `${counts.total}+` : '?'
      : `${counts.total}${counts.more ? '+' : ''}`;
  const isOpen = open ?? counts.total > 0;
  // An org whose count failed is "unknown", not 0: leave it off the picker.
  const knownCounts = Object.fromEntries(
    Object.entries(counts.byOrg).filter(([id]) => !(id in counts.errors)),
  );
  const details = (
    <details
      className="group mt-4"
      open={isOpen}
      onToggle={(event) => {
        const next = (event.currentTarget as HTMLDetailsElement).open;
        if (next !== isOpen) setOpen(next);
      }}
      data-testid="updates-to-review"
    >
      <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 rounded text-sm font-medium text-text-secondary hover:text-text-primary outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)] [&::-webkit-details-marker]:hidden">
        <ChevronIcon size={14} className="transition-transform group-open:rotate-90" />
        Updates to review{label !== null && ` (${label})`}
        {orgs.length > 1 && <span className="font-normal text-text-tertiary">across {orgs.length} organizations</span>}
      </summary>
      {/* The queue builds diffs: load it only once the section is open and
          the counts (which decide which orgs qualify) are in. */}
      {isOpen && (counts.loaded ? (
        <HeldUpdateQueues orgs={orgs} value={orgId} onChange={setChosen} counts={knownCounts} />
      ) : (
        <p className="mt-3 text-sm text-text-tertiary">Loading…</p>
      ))}
    </details>
  );
  if (!standalone) return details;
  return (
    <section aria-label="Organization library" className="mt-10 border-t border-border pt-5">
      <p className="text-sm text-text-tertiary">Review package updates for the organizations you administer.</p>
      {details}
    </section>
  );
}

function AssignmentList({ items }: { items: SkillAssignment[] }) {
  return (
    <ul className="mt-2 divide-y divide-border border-y border-border">
      {items.map((assignment) => (
        <li key={assignment.id} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 py-2.5 text-sm">
          <span className="min-w-0 truncate font-mono text-text-primary">{assignment.version_id}</span>
          <span className="text-xs text-text-tertiary">
            <span className={assignment.mode === 'required' ? 'font-medium text-warning' : ''}>
              {assignment.mode === 'required' ? 'Required' : 'Optional'}
            </span>{' '}
            for {TARGET_COPY[assignment.target_type] ?? assignment.target_type}
            {assignment.target_tools.length > 0 ? ` · ${assignment.target_tools.join(', ')}` : ''}
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * What the viewer's organization manages for them: assigned packages and the
 * registries the org publishes from. Collapses to one quiet line when there is
 * nothing to show, instead of empty boxes.
 */
export function ManagedLibrary() {
  const me = useMe();
  const org = useOrgSkillControl(me.data?.org_id);
  const effective = useEffectiveSkillAssignments();
  const myOrgs = useMyOrgs();
  // Every org the viewer owns or administers, not only the primary one.
  const managed = adminOrgs(myOrgs.data, me.data);

  if (!me.data?.org_id) {
    if (managed.length === 0) return null;
    return <UpdatesToReview orgs={managed} standalone />;
  }

  const assignments = effective.data?.items || [];
  const registries = org.registries.data?.items || [];
  const gated = isTierGate(org.registries.error);
  const registryError = !gated && org.registries.error;
  const loading = org.registries.isLoading || effective.isLoading;
  const canAdminPrimary = managed.some((item) => item.org_id === me.data?.org_id);

  if (!loading && !gated && !registryError && registries.length === 0 && assignments.length === 0) {
    return (
      <section aria-label="Organization library" className="mt-10 border-t border-border pt-5">
        <p className="text-sm text-text-tertiary">
          Your organization hasn&apos;t assigned any packages to you. <DocsLink />
        </p>
        <UpdatesToReview orgs={managed} />
      </section>
    );
  }

  return (
    <section aria-labelledby="managed-library-heading" className="mt-10 border-t border-border pt-6">
      <h2 id="managed-library-heading" className="text-lg font-semibold text-text-primary">From your organization</h2>
      <p className="mt-1 text-sm text-text-tertiary">
        Packages your organization assigns to you, and the registries it publishes from. <DocsLink />
      </p>
      <div className="mt-5 grid gap-8 lg:grid-cols-2">
        <section aria-labelledby="assignments-heading">
          <h3 id="assignments-heading" className="text-sm font-semibold text-text-primary">
            Assigned to you <span className="font-normal text-text-tertiary">{assignments.length}</span>
          </h3>
          {effective.isLoading ? (
            <p className="mt-2 text-sm text-text-tertiary">Loading…</p>
          ) : assignments.length === 0 ? (
            <p className="mt-2 text-sm text-text-tertiary">Nothing assigned yet.</p>
          ) : (
            <AssignmentList items={assignments} />
          )}
        </section>
        <section aria-labelledby="registries-heading">
          <h3 id="registries-heading" className="text-sm font-semibold text-text-primary">
            Registries {!gated && <span className="font-normal text-text-tertiary">{registries.length}</span>}
          </h3>
          {gated ? (
            <p role="status" className="mt-2 text-sm text-text-secondary">
              <span className="font-semibold text-warning">Upgrade to Team</span>{' '}
              to publish from organization registries. Ask your organization owner to upgrade.
            </p>
          ) : (
            <>
              <p className="mt-0.5 text-xs text-text-tertiary">New matching tags are published to your organization after the usual checks.</p>
              {org.registries.isLoading ? (
                <p className="mt-2 text-sm text-text-tertiary">Loading…</p>
              ) : registryError ? (
                <p role="alert" className="mt-2 text-sm text-danger">Registries unavailable: {errorMessage(org.registries.error)}</p>
              ) : registries.length === 0 ? (
                <p className="mt-2 text-sm text-text-tertiary">No registries configured.</p>
              ) : (
                <RegistryList registries={registries} orgId={me.data.org_id} canAdmin={canAdminPrimary} />
              )}
            </>
          )}
        </section>
      </div>
      <UpdatesToReview orgs={managed} />
    </section>
  );
}
