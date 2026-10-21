import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Badge } from '../components/Badge';
import EmptyState from '../components/EmptyState';
import { Card, Select } from '../components/ui';
import { useOrgSkillUsage } from '../hooks/useMarketplace';
import { useMe } from '../hooks/useMe';
import { useMyOrgs } from '../transfers/useTransfers';
import { isTierGate } from './MarketplacePage';
import { adminOrgs } from './moderationState';

/** What an organization's members actually use (marketplace v2 §9.2), for
 *  that org's owners and admins (Team plan). Exact counts of the org's own
 *  members; never another organization's data. */
export default function OrgUsagePage() {
  const me = useMe();
  const myOrgs = useMyOrgs();
  const orgs = adminOrgs(myOrgs.data, me.data);
  const [orgId, setOrgId] = useState('');
  const [days, setDays] = useState(30);
  const active = orgId || orgs[0]?.org_id || '';
  const usage = useOrgSkillUsage(active || undefined, days);
  const items = usage.data?.items || [];
  return (
    <main className="mx-auto w-full max-w-5xl space-y-5 px-4 py-7 sm:px-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <Link to="/marketplace" className="text-xs text-text-tertiary hover:text-text-primary">Marketplace</Link>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-text-primary">Skill usage</h1>
          <p className="mt-1 max-w-3xl text-sm text-text-secondary">
            Installs, active installs and translation failures from your organization's members, by package and tool,
            with stale and deprecated packages called out.
          </p>
        </div>
        <div className="flex gap-2">
          {orgs.length > 1 && (
            <Select aria-label="Organization" value={active} onValueChange={setOrgId} options={orgs.map((org) => ({ value: org.org_id, label: org.name }))} />
          )}
          <Select
            aria-label="Window"
            value={String(days)}
            onValueChange={(value) => setDays(Number(value))}
            options={[{ value: '7', label: 'Last 7 days' }, { value: '30', label: 'Last 30 days' }, { value: '90', label: 'Last 90 days' }]}
          />
        </div>
      </header>
      {!myOrgs.isLoading && orgs.length === 0 && (
        <EmptyState title="No organization to show" description="Organization owners and admins can see their members' skill usage here." />
      )}
      {usage.isLoading && active && <p className="text-sm text-text-tertiary">Loading usage…</p>}
      {usage.error && (isTierGate(usage.error) ? (
        <Card className="p-4" role="status">
          <p className="text-sm font-semibold text-warning">Upgrade to Team</p>
          <p className="mt-1 text-xs text-text-secondary">Organization skill usage is part of the Team plan.</p>
        </Card>
      ) : (
        <p role="alert" className="text-sm text-danger">Usage unavailable: {String(usage.error)}</p>
      ))}
      {usage.data && items.length === 0 && (
        <EmptyState title="No usage recorded" description={`No marketplace installs from this organization in the last ${days} days.`} />
      )}
      {items.length > 0 && (
        <Card className="overflow-x-auto p-5">
          <table className="w-full min-w-[720px] text-left text-sm">
            <thead className="text-xs uppercase tracking-wider text-text-tertiary">
              <tr>
                <th className="py-2">Package</th>
                <th className="py-2">Installs</th>
                <th className="py-2">Active</th>
                <th className="py-2">Uninstalls</th>
                <th className="py-2">Translation failures</th>
                <th className="py-2">Tools</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.package_id} className="border-t border-border align-top">
                  <td className="py-2 pr-3">
                    <p className="font-mono text-text-primary">{item.namespace}/{item.name}</p>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {item.deprecated && <Badge label="deprecated" variant="warning" tint />}
                      {item.stale && <Badge label="stale" variant="warning" tint />}
                      {item.unmaintained && <Badge label="unmaintained" variant="warning" tint />}
                    </div>
                  </td>
                  <td className="py-2 font-mono text-xs">{item.installs}</td>
                  <td className="py-2 font-mono text-xs">{item.active}</td>
                  <td className="py-2 font-mono text-xs">{item.uninstalls}</td>
                  <td className={`py-2 font-mono text-xs ${item.translation_failures ? 'text-warning' : ''}`}>{item.translation_failures}</td>
                  <td className="py-2 text-xs text-text-secondary">
                    {item.by_tool.map((row) => `${row.tool} (${row.installs})`).join(', ')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </main>
  );
}
