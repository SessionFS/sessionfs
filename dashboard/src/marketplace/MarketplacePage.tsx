import { useDeferredValue, useState } from 'react';
import { Link } from 'react-router-dom';
import type { MarketplacePackage } from '../api/client';
import EmptyState from '../components/EmptyState';
import { Button, PageContainer, PageHeader } from '../components/ui';
import { useMarketplaceCategories, useMarketplacePackages, useRevocationIndex, type MarketplaceFilterState } from '../hooks/useMarketplace';
import { useMe } from '../hooks/useMe';
import { useMyOrgs } from '../transfers/useTransfers';
import { errorMessage } from '../utils/errorMessage';
import { BrowseToolbar } from './BrowseFilters';
import { ManagedLibrary } from './ManagedLibrary';
import MarketplaceNav from './MarketplaceNav';
import { adminOrgs, canPublishPersonal, listingState } from './moderationState';
import { InstallInYourTool } from './NativeCatalogs';
import { PackageCard } from './PackageCard';
import { PackageDialog } from './PackageDialog';
import { RevocationIncompleteNotice } from './parts';
import PublishDialog from './PublishDialog';

export { isTierGate } from './tierGate';

export default function MarketplacePage() {
  const [query, setQuery] = useState('');
  const deferredQuery = useDeferredValue(query);
  const [tool, setTool] = useState('');
  const [selected, setSelected] = useState<MarketplacePackage | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [filters, setFilters] = useState<MarketplaceFilterState>({ sort: 'relevance' });
  const packages = useMarketplacePackages(deferredQuery, tool, filters);
  const categories = useMarketplaceCategories();
  const revocations = useRevocationIndex();
  const me = useMe();
  const myOrgs = useMyOrgs();
  const canPublish = canPublishPersonal(me.data) || adminOrgs(myOrgs.data, me.data).length > 0;
  const items = packages.data?.pages.flatMap((page) => page.items) ?? [];
  const loadedCount = items.length;

  return (
    <PageContainer width="wide">
      <MarketplaceNav />
      <PageHeader
        title="Marketplace"
        description="Skills and plugins for your coding agents. Each package shows what it can do before you install it."
        actions={
          <>
            <InstallInYourTool />
            {canPublish && <Button onClick={() => setPublishOpen(true)}>Publish package</Button>}
          </>
        }
      />
      {!canPublish && me.data && (
        <p className="-mt-3 mb-5 text-xs text-text-tertiary">
          Publishing needs the Starter plan or above, or an owner or admin role in an organization.{' '}
          <Link to="/settings/billing" className="font-medium text-brand hover:underline">View plans</Link>
        </p>
      )}

      <RevocationIncompleteNotice error={revocations.error} hasPrevious={!!revocations.data} className="mb-5" />

      <BrowseToolbar
        query={query}
        onQuery={setQuery}
        tool={tool}
        onTool={setTool}
        filters={filters}
        onFilters={setFilters}
        categories={categories.data?.items || []}
      />

      <div className="mt-6 flex items-baseline gap-2">
        <h2 className="text-lg font-semibold text-text-primary">Packages</h2>
        {packages.data && (
          <span className="text-sm text-text-tertiary" aria-live="polite" data-testid="package-count">
            {loadedCount}{packages.hasNextPage ? ' loaded' : ''}
          </span>
        )}
      </div>
      {packages.isLoading && (
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-label="Loading marketplace">
          {[0, 1, 2].map((item) => <div key={item} className="h-36 animate-pulse rounded-lg border border-border bg-surface" />)}
        </div>
      )}
      {packages.error && <p role="alert" className="mt-4 rounded-lg border border-danger/30 bg-danger/6 p-4 text-sm text-danger">Marketplace unavailable: {errorMessage(packages.error)}</p>}
      {packages.data && loadedCount === 0 && (
        <EmptyState title="No packages match" description="Try a broader search or another tool. Private organization packages appear only for their members." />
      )}
      {loadedCount > 0 && (
        <ul className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((item) => (
            <li key={item.listing_id} className="min-w-0">
              <PackageCard item={item} state={listingState(item, revocations.data)} onOpen={() => setSelected(item)} />
            </li>
          ))}
        </ul>
      )}
      {packages.hasNextPage && (
        <div className="mt-5 flex justify-center">
          <Button
            variant="secondary"
            onClick={() => void packages.fetchNextPage()}
            loading={packages.isFetchingNextPage}
            disabled={packages.isFetchingNextPage}
          >
            Load more
          </Button>
        </div>
      )}

      <ManagedLibrary />
      {selected && <PackageDialog item={selected} state={listingState(selected, revocations.data)} onClose={() => setSelected(null)} />}
      {publishOpen && <PublishDialog onClose={() => setPublishOpen(false)} />}
    </PageContainer>
  );
}
