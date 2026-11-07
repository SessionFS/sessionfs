import { useState } from 'react';
import { TabPanel, Tabs } from '../components/ui';
import ApprovalLogSection from './ApprovalLogSection';
import PoliciesSection from './PoliciesSection';

/** Org → Governance. Policies for everyone; the approval log only for owners and admins. */
export default function GovernanceSection({ orgId, isAdmin }: { orgId: string; isAdmin: boolean }) {
  const [view, setView] = useState('policies');
  if (!isAdmin) return <PoliciesSection orgId={orgId} />;
  const active = view === 'log' ? 'log' : 'policies';
  return (
    <div className="space-y-4">
      <Tabs
        bare
        variant="underline"
        idPrefix="governance"
        aria-label="Governance views"
        activeKey={active}
        onChange={setView}
        tabs={[
          { key: 'policies', label: 'Policies' },
          { key: 'log', label: 'Approval log' },
        ]}
      />
      <TabPanel idPrefix="governance" tabKey={active}>
        {active === 'log' ? <ApprovalLogSection orgId={orgId} /> : <PoliciesSection orgId={orgId} />}
      </TabPanel>
    </div>
  );
}
