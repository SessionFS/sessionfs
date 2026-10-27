/**
 * GitHub #92 — the account page's represented-persona badges deep-link here
 * with `?org=<id>` when the persona lives in a secondary org. Shows that
 * org's personas (and the member list used to resolve who each persona
 * represents) instead of the primary org the Organization page normally
 * renders. The OrgPersonasSection is already org-id-driven, so this is just
 * a thin mount with a back link.
 */

import { Link, useSearchParams } from 'react-router-dom';
import { Alert, PageContainer, PageHeader, Tabs } from '../components/ui';
import { useMe } from '../hooks/useMe';
import { useMyOrgs } from '../transfers/useTransfers';
import OrgPersonasSection, { type OrgPersonaMember } from './OrgPersonasSection';
import { useOrgMembers } from './useOrgMembers';
import GroupsTab from './groups/GroupsTab';

type SecondaryTab = 'personas' | 'groups';

export default function SecondaryOrgPage({ orgId }: { orgId: string }) {
  const { data: membersData, isLoading, isError } = useOrgMembers(orgId);
  const { data: me } = useMe();
  const { data: orgs } = useMyOrgs();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab: SecondaryTab = searchParams.get('tab') === 'groups' ? 'groups' : 'personas';
  const org = orgs?.find((o) => o.org_id === orgId);

  const role = membersData?.current_user_role;
  const isAdmin = role === 'admin' || role === 'owner';
  const myUserId = me?.user_id;

  if (isError) {
    return (
      <PageContainer>
        <Alert variant="danger" className="mb-4">
          You don&apos;t have access to this organization, or it no longer exists.
        </Alert>
        <Link to="/settings/organization" className="hover:underline">
          ← Back to your organization
        </Link>
      </PageContainer>
    );
  }

  if (isLoading || !membersData) {
    return <div className="text-center py-12 text-text-tertiary">Loading…</div>;
  }

  const members: OrgPersonaMember[] = (membersData.members ?? []).map((m) => ({
    user_id: m.user_id,
    email: m.email,
  }));

  const selectTab = (key: string) =>
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set('tab', key);
        return next;
      },
      { replace: true },
    );

  return (
    <PageContainer>
      <PageHeader
        title={org?.name ?? 'Organization'}
        description="Personas and project groups for this organization"
        breadcrumb={
          <Link to="/settings/organization" className="hover:underline">
            ← Back to your organization
          </Link>
        }
      />
      <Tabs
        aria-label="Organization sections"
        activeKey={activeTab}
        onChange={selectTab}
        tabs={[
          {
            key: 'personas',
            label: 'Personas',
            content: (
              <OrgPersonasSection
                orgId={orgId}
                isAdmin={isAdmin}
                myUserId={myUserId}
                members={members}
                className="bg-bg-elevated border border-border rounded-xl p-5"
              />
            ),
          },
          {
            key: 'groups',
            label: 'Groups',
            // The plan tier of a secondary org isn't known here; the server
            // answers upgrade_required if the plan lacks groups.
            content: <GroupsTab orgId={orgId} isOrgAdmin={isAdmin} />,
          },
        ]}
      />
    </PageContainer>
  );
}
