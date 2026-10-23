import { Badge } from '../components/Badge';
import { TableShell } from '../components/ui';
import type { MarketplacePackage, VerifyStatus } from '../api/client';
import { installsLabel } from './discoveryUtils';

/** Stale / unmaintained / deprecated flags (marketplace v2 §9.2). */
export function FreshnessBadges({ item }: { item: MarketplacePackage }) {
  const freshness = item.freshness;
  return (
    <>
      {item.deprecation && <Badge label="deprecated" variant="warning" tint />}
      {freshness?.stale && <Badge label="stale" variant="warning" tint />}
      {freshness?.unmaintained && <Badge label="unmaintained" variant="warning" tint />}
    </>
  );
}

export function DeprecationNotice({ item, onOpenSuccessor }: { item: MarketplacePackage; onOpenSuccessor?: (packageId: string) => void }) {
  const deprecation = item.deprecation;
  if (!deprecation) return null;
  const successor = deprecation.successor;
  return (
    <div role="status" className="mb-4 rounded-lg border border-warning/40 bg-[rgba(240,192,64,0.06)] p-3 text-sm text-warning">
      <p className="font-semibold">Deprecated by its publisher.</p>
      {deprecation.message && <p className="mt-1 text-xs text-text-secondary">{deprecation.message}</p>}
      {successor && (
        <p className="mt-1 text-xs text-text-secondary">
          Successor:{' '}
          {onOpenSuccessor ? (
            <button type="button" className="font-mono text-brand hover:underline" onClick={() => onOpenSuccessor(successor.package_id)}>
              {successor.namespace}/{successor.name}
            </button>
          ) : (
            <span className="font-mono">{successor.namespace}/{successor.name}</span>
          )}
        </p>
      )}
    </div>
  );
}

const STATUS_COPY: Record<VerifyStatus, { label: string; tone: string }> = {
  passing: { label: 'works', tone: 'text-accent' },
  regressed: { label: 'regressed', tone: 'text-danger' },
  failing: { label: 'failing', tone: 'text-warning' },
  unknown: { label: 'not yet verified', tone: 'text-text-tertiary' },
};

function shortDate(value: string | null | undefined): string {
  return value ? value.slice(0, 10) : '—';
}

/** Installs by target tool (30 days) as horizontal bars, with last updated
 *  and last verified per tool. Counts under 10 show as "<10" and draw a
 *  minimal bar, so small consumers are never singled out. */
export function ToolFreshnessTable({ item }: { item: MarketplacePackage }) {
  const tools = Object.keys(item.compatibility || {});
  const byTool = item.installs?.by_tool || {};
  const fresh = item.freshness?.tools || {};
  const max = Math.max(1, ...tools.map((tool) => byTool[tool]?.count ?? 0));
  if (tools.length === 0) return null;
  return (
    <section className="mt-6" aria-labelledby="tool-freshness-heading">
      <h3 id="tool-freshness-heading" className="text-sm font-semibold text-text-primary">Installs and freshness by tool</h3>
      <p className="mt-1 text-xs text-text-tertiary">
        Installs over the last {item.installs?.window_days ?? 30} days (counts under 10 show as "&lt;10").
        {item.freshness?.computed_at ? ` Freshness computed ${shortDate(item.freshness.computed_at)}.` : ' Freshness not computed yet.'}
      </p>
      <TableShell minWidth={620} bordered wrapperClassName="mt-2">
          <thead className="bg-bg-sunken text-xs uppercase tracking-wider text-text-tertiary">
            <tr>
              <th className="px-3 py-2">Tool</th>
              <th className="px-3 py-2">Installs</th>
              <th className="px-3 py-2">Last updated</th>
              <th className="px-3 py-2">Last verified</th>
              <th className="px-3 py-2">Works in</th>
            </tr>
          </thead>
          <tbody>
            {tools.map((tool) => {
              const count = byTool[tool];
              const width = count?.count != null ? Math.max(2, Math.round((count.count / max) * 100)) : count ? 2 : 0;
              const row = fresh[tool];
              const status = STATUS_COPY[row?.verify_status ?? 'unknown'];
              return (
                <tr key={tool} className="border-t border-border">
                  <td className="px-3 py-2 font-mono text-text-primary">{tool}</td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <div className="h-2 w-28 rounded bg-bg-sunken" aria-hidden="true">
                        <div className="h-2 rounded bg-brand" style={{ width: `${width}%` }} />
                      </div>
                      <span className="font-mono text-xs text-text-secondary">{installsLabel(count) === '—' ? '0' : installsLabel(count)}</span>
                    </div>
                  </td>
                  <td className="px-3 py-2 text-xs text-text-secondary">{shortDate(row?.last_updated_at)}</td>
                  <td className="px-3 py-2 text-xs text-text-secondary">{shortDate(row?.last_verified_at)}</td>
                  <td className={`px-3 py-2 text-xs font-semibold ${status.tone}`}>
                    {status.label}{row?.stale ? ' · stale' : ''}
                  </td>
                </tr>
              );
            })}
          </tbody>
      </TableShell>
    </section>
  );
}
