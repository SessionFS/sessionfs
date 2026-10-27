import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import MarketplaceNav from './MarketplaceNav';
import type { FlooredCount, PublisherAnalytics, PublisherAnalyticsPackage } from '../api/client';
import { Badge } from '../components/Badge';
import EmptyState from '../components/EmptyState';
import { Button, Card, Dialog, DialogFooter, DialogHeader, Input, PageContainer, PageHeader, Select, TableShell } from '../components/ui';
import { useMarketplaceCategories, usePublisherAnalytics, useUpdatePackageSettings } from '../hooks/useMarketplace';
import { useToast } from '../hooks/useToast';
import { isTierGate } from './MarketplacePage';
import { errorMessage } from '../utils/errorMessage';

const DAY_OPTIONS = [
  { value: '7', label: 'Last 7 days' },
  { value: '30', label: 'Last 30 days' },
  { value: '90', label: 'Last 90 days' },
];

function rate(value: number | null): string {
  return value == null ? 'n/a' : `${(value * 100).toFixed(1)}%`;
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-border bg-bg-elevated p-4">
      <p className="text-2xs uppercase tracking-wider text-text-tertiary">{label}</p>
      <p className="mt-1 font-mono text-2xl font-semibold text-text-primary">{value}</p>
      {hint && <p className="mt-1 text-xs text-text-tertiary">{hint}</p>}
    </div>
  );
}

/** Daily installs as an SVG column chart. Floored days ("<10") draw a short
 *  hatched stub instead of a value, so exact small counts are never shown. */
export function InstallsByDay({ days }: { days: PublisherAnalytics['by_day'] }) {
  const max = Math.max(10, ...days.map((day) => day.installs.count ?? 0));
  const width = 600;
  const height = 120;
  const step = width / Math.max(1, days.length);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Installs per day" className="h-32 w-full">
      {days.map((day, index) => {
        const count = day.installs.count;
        const floored = count == null;
        const h = floored ? 6 : Math.round(((count ?? 0) / max) * (height - 10));
        return (
          <rect
            key={day.day}
            x={index * step + 1}
            y={height - h}
            width={Math.max(1, step - 2)}
            height={h}
            className={floored ? 'fill-[var(--text-tertiary)] opacity-50' : 'fill-[var(--brand)]'}
          >
            <title>{`${day.day}: ${day.installs.display}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

function ToolBars({ rows }: { rows: PublisherAnalytics['by_tool'] }) {
  const max = Math.max(1, ...rows.map((row) => row.installs.count ?? 0));
  return (
    <TableShell minWidth={640}>
        <thead className="text-xs uppercase tracking-wider text-text-tertiary">
          <tr>
            <th className="py-2">Tool</th>
            <th className="py-2">Installs</th>
            <th className="py-2">Active</th>
            <th className="py-2">Uninstall rate</th>
            <th className="py-2">Translation failures</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.tool} className="border-t border-border">
              <td className="py-2 font-mono text-text-primary">{row.tool}</td>
              <td className="py-2">
                <div className="flex items-center gap-2">
                  <div className="h-2 w-32 rounded bg-bg-sunken" aria-hidden="true">
                    <div className="h-2 rounded bg-brand" style={{ width: `${row.installs.count != null ? Math.max(2, (row.installs.count / max) * 100) : 2}%` }} />
                  </div>
                  <span className="font-mono text-xs">{row.installs.display}</span>
                </div>
              </td>
              <td className="py-2 font-mono text-xs">{row.active.display}</td>
              <td className="py-2 font-mono text-xs">{rate(row.uninstall_rate)}</td>
              <td className={`py-2 font-mono text-xs ${row.translation_failures.count ? 'text-warning' : ''}`}>{row.translation_failures.display}</td>
            </tr>
          ))}
        </tbody>
    </TableShell>
  );
}

function count(value: FlooredCount) {
  return value.display;
}

function SettingsDialog({
  publisherId,
  item,
  siblings,
  onClose,
}: {
  publisherId: string;
  item: PublisherAnalyticsPackage;
  siblings: PublisherAnalyticsPackage[];
  onClose: () => void;
}) {
  const categories = useMarketplaceCategories();
  const update = useUpdatePackageSettings(publisherId);
  const { addToast } = useToast();
  const [selected, setSelected] = useState<string[]>(item.categories || []);
  const [deprecated, setDeprecated] = useState(item.deprecated);
  const [message, setMessage] = useState(item.deprecation_message || '');
  const [successor, setSuccessor] = useState(item.successor_package_id || '');
  const [readmePath, setReadmePath] = useState(item.readme_path || '');
  const options = categories.data?.items || [];

  function toggle(slug: string) {
    setSelected((current) => (current.includes(slug) ? current.filter((s) => s !== slug) : current.length >= 3 ? current : [...current, slug]));
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    try {
      await update.mutateAsync({
        packageId: item.package_id,
        body: {
          categories: selected,
          deprecated,
          ...(deprecated ? { deprecation_message: message || null, successor_package_id: successor || null } : {}),
          readme_path: readmePath.trim() || null,
        },
      });
      addToast('success', `Updated ${item.name}.`);
      onClose();
    } catch (error) {
      addToast('error', `Update failed: ${errorMessage(error)}`);
    }
  }

  return (
    <Dialog open onClose={onClose} titleId="package-settings-title">
      <DialogHeader titleId="package-settings-title">Discovery settings · {item.name}</DialogHeader>
      <form onSubmit={save} className="space-y-4">
        <fieldset>
          <legend className="text-xs font-semibold text-text-primary">Categories (up to 3)</legend>
          <div className="mt-2 flex flex-wrap gap-2">
            {options.map((category) => (
              <label key={category.slug} className="flex items-center gap-1 text-xs text-text-secondary">
                <input
                  type="checkbox"
                  checked={selected.includes(category.slug)}
                  disabled={!selected.includes(category.slug) && selected.length >= 3}
                  onChange={() => toggle(category.slug)}
                />
                {category.display_name}
              </label>
            ))}
          </div>
        </fieldset>
        <label className="flex items-center gap-2 text-sm text-text-primary">
          <input type="checkbox" checked={deprecated} onChange={(event) => setDeprecated(event.target.checked)} />
          Deprecated
        </label>
        {deprecated && (
          <>
            <Input title="Message for installers" value={message} onChange={(event) => setMessage(event.target.value)} maxLength={500} />
            <Select
              title="Successor (same publisher)"
              value={successor}
              onValueChange={setSuccessor}
              options={[{ value: '', label: 'No successor' }, ...siblings.filter((p) => p.package_id !== item.package_id).map((p) => ({ value: p.package_id, label: `${p.namespace}/${p.name}` }))]}
            />
          </>
        )}
        <Input title="README path inside the package (optional)" placeholder="README.md" value={readmePath} onChange={(event) => setReadmePath(event.target.value)} maxLength={300} />
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" loading={update.isPending}>Save</Button>
        </DialogFooter>
      </form>
    </Dialog>
  );
}

function PackageRow({ item, onEdit }: { item: PublisherAnalyticsPackage; onEdit: () => void }) {
  return (
    <tr className="border-t border-border align-top">
      <td className="py-2 pr-3">
        <p className="font-mono text-text-primary">{item.namespace}/{item.name}</p>
        <p className="text-xs text-text-tertiary">{item.current_version || 'unversioned'}</p>
        <div className="mt-1 flex flex-wrap gap-1">
          {item.deprecated && <Badge label="deprecated" variant="warning" tint />}
          {item.stale && <Badge label="stale" variant="warning" tint />}
          {item.unmaintained && <Badge label="unmaintained" variant="warning" tint />}
        </div>
      </td>
      <td className="py-2 pr-3 font-mono text-xs">{count(item.installs)}</td>
      <td className="py-2 pr-3 font-mono text-xs">{count(item.active_installs)}</td>
      <td className="py-2 pr-3 font-mono text-xs">{rate(item.uninstall_rate)}</td>
      <td className="py-2 pr-3 text-xs">
        {item.installs_by_version.length === 0 ? '—' : item.installs_by_version.slice(0, 4).map((row) => (
          <span key={row.version_id} className="mr-2 font-mono">{row.version || row.version_id.slice(0, 10)}: {row.installs.display}</span>
        ))}
      </td>
      <td className="py-2 pr-3 text-xs">
        {item.works_in_regressions.length > 0
          ? <span className="text-danger">{item.works_in_regressions.join(', ')}</span>
          : <span className="text-text-tertiary">none</span>}
      </td>
      <td className="py-2 pr-3 text-xs">{item.scan.max_severity || 'none'}</td>
      <td className="py-2 pr-3 text-xs">{item.open_reports}</td>
      <td className="py-2"><Button size="sm" variant="ghost" onClick={onEdit}>Settings…</Button></td>
    </tr>
  );
}

export default function PublisherAnalyticsPage() {
  const { publisherId } = useParams<{ publisherId: string }>();
  const [days, setDays] = useState(30);
  const [editing, setEditing] = useState<PublisherAnalyticsPackage | null>(null);
  const analytics = usePublisherAnalytics(publisherId, days);
  const data = analytics.data;
  return (
    <PageContainer width="wide">
      <MarketplaceNav />
      <PageHeader
        breadcrumb={<Link to="/marketplace/publishers" className="hover:text-text-primary">Publishers</Link>}
        title={<>{data ? <span className="font-mono">{data.namespace}</span> : 'Publisher'} analytics</>}
        description={`Aggregates only. Counts under ${data?.k_floor ?? 10} show as "<${data?.k_floor ?? 10}", and the organizations your installs come from are never shown.`}
        actions={<Select aria-label="Window" value={String(days)} onValueChange={(value) => setDays(Number(value))} options={DAY_OPTIONS} />}
      />
      <div className="space-y-5">
      {analytics.isLoading && <p className="text-sm text-text-tertiary">Loading analytics…</p>}
      {analytics.error && (isTierGate(analytics.error) ? (
        <Card className="p-4" role="status">
          <p className="text-sm font-semibold text-warning">Publisher analytics need a paid plan</p>
          <p className="mt-1 text-xs text-text-secondary">Personal publishers need Starter or above; organization publishers need the Team plan.</p>
        </Card>
      ) : (
        <p role="alert" className="text-sm text-danger">Analytics unavailable: {errorMessage(analytics.error)}</p>
      ))}
      {data && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
            <Tile label="Installs" value={data.totals.installs.display} />
            <Tile label="Active installs" value={data.totals.active_installs.display} hint="Reconcile pings, 30 days" />
            <Tile label="Uninstall rate" value={rate(data.totals.uninstall_rate)} hint={`${data.totals.uninstalls.display} uninstalls`} />
            <Tile label="Translation failures" value={data.totals.translation_failures.display} />
            <Tile label="Service-key installs" value={data.totals.service_installs.display} hint="Counted separately" />
          </div>
          <Card className="p-5">
            <h2 className="text-sm font-semibold text-text-primary">Installs per day</h2>
            <InstallsByDay days={data.by_day} />
          </Card>
          <Card className="p-5">
            <h2 className="text-sm font-semibold text-text-primary">By target tool</h2>
            {data.by_tool.length === 0 ? <p className="mt-2 text-sm text-text-tertiary">No installs in this window.</p> : <ToolBars rows={data.by_tool} />}
          </Card>
          <Card className="p-5">
            <h2 className="text-sm font-semibold text-text-primary">Packages</h2>
            {data.packages.length === 0 ? (
              <EmptyState title="No packages yet" description="Publish under this namespace to see analytics." />
            ) : (
              <TableShell minWidth={900} wrapperClassName="mt-2">
                  <thead className="text-xs uppercase tracking-wider text-text-tertiary">
                    <tr>
                      <th className="py-2">Package</th>
                      <th className="py-2">Installs</th>
                      <th className="py-2">Active</th>
                      <th className="py-2">Uninstall rate</th>
                      <th className="py-2">Update adoption</th>
                      <th className="py-2">Works-in regressions</th>
                      <th className="py-2">Scan</th>
                      <th className="py-2">Open reports</th>
                      <th className="py-2"><span className="sr-only">Actions</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.packages.map((item) => <PackageRow key={item.package_id} item={item} onEdit={() => setEditing(item)} />)}
                  </tbody>
                </TableShell>
            )}
          </Card>
        </>
      )}
      {editing && publisherId && data && (
        <SettingsDialog publisherId={publisherId} item={editing} siblings={data.packages} onClose={() => setEditing(null)} />
      )}
      </div>
    </PageContainer>
  );
}
