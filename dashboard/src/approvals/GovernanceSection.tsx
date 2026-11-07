import { useState } from 'react';
import { TabPanel, Tabs } from '../components/ui';
import { TemplatesPanel } from '../projects/workflows/TemplatesPanel';
import ApprovalLogSection from './ApprovalLogSection';
import PoliciesSection from './PoliciesSection';

type View = 'policies' | 'workflows' | 'log';

/**
 * Org → Governance. Approval policies for everyone; the org's workflow
 * templates (when ``tier`` is given; org admins write them); the approval
 * log only for owners and admins.
 */
export default function GovernanceSection({
  orgId,
  isAdmin,
  tier,
}: {
  orgId: string;
  isAdmin: boolean;
  tier?: string | null;
}) {
  const [view, setView] = useState<string>('policies');
  const showWorkflows = tier !== undefined;
  if (!isAdmin && !showWorkflows) return <PoliciesSection orgId={orgId} />;
  const tabs: { key: View; label: string }[] = [{ key: 'policies', label: 'Policies' }];
  if (showWorkflows) tabs.push({ key: 'workflows', label: 'Workflow templates' });
  if (isAdmin) tabs.push({ key: 'log', label: 'Approval log' });
  const active: View = tabs.some((t) => t.key === view) ? (view as View) : 'policies';
  return (
    <div className="space-y-4">
      <Tabs
        bare
        variant="underline"
        idPrefix="governance"
        aria-label="Governance views"
        activeKey={active}
        onChange={setView}
        tabs={tabs}
      />
      <TabPanel idPrefix="governance" tabKey={active}>
        {active === 'log' ? (
          <ApprovalLogSection orgId={orgId} />
        ) : active === 'workflows' ? (
          <div className="bg-bg-elevated border border-border rounded-xl p-5">
            {/* Workflow templates shared by the org's projects (org admins write them). */}
            <TemplatesPanel target={{ kind: 'org', id: orgId }} tier={tier ?? null} canEdit={isAdmin} />
          </div>
        ) : (
          <PoliciesSection orgId={orgId} />
        )}
      </TabPanel>
    </div>
  );
}
