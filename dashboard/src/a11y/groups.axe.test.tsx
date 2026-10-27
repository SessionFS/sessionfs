import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import GroupsTab from '../org/groups/GroupsTab';
import GroupDetailPage from '../org/groups/GroupDetailPage';
import RestrictVisibilitySetting from '../org/groups/RestrictVisibilitySetting';
import ProjectGroupsBlock from '../projects/ProjectGroupsBlock';
import YourGroupsCard from '../settings/YourGroupsCard';
import {
  ORG,
  authState,
  detail,
  makeClient,
  renderWith,
  stubFetch,
  summary,
} from '../org/groups/groupTestHarness';
import { axe } from '../test/axe';

vi.mock('../auth/AuthContext', async () => (await import('../org/groups/groupTestHarness')).authModule);
vi.mock('../hooks/useToast', async () => (await import('../org/groups/groupTestHarness')).toastModule);

beforeEach(() => {
  authState.client = makeClient();
  stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

const wrap = (ui: React.ReactNode) => <main>{ui}</main>;

describe('project groups accessibility', () => {
  it('Groups tab has no axe violations', async () => {
    const { container } = renderWith(wrap(<GroupsTab orgId={ORG} isOrgAdmin tier="team" />));
    await screen.findByRole('link', { name: 'Payments' });
    expect(await axe(container)).toHaveNoViolations();
  });

  it('create dialog has no axe violations', async () => {
    const { container } = renderWith(wrap(<GroupsTab orgId={ORG} isOrgAdmin tier="team" />));
    await userEvent.setup().click(await screen.findByRole('button', { name: 'New group' }));
    await screen.findByRole('dialog', { name: 'New project group' });
    expect(await axe(container)).toHaveNoViolations();
  });

  it('group detail (admin) has no axe violations', async () => {
    const { container } = renderWith(wrap(<GroupDetailPage />), {
      route: `/settings/organization/groups/pg_1?org=${ORG}`,
      path: '/settings/organization/groups/:groupId',
    });
    await screen.findByRole('link', { name: 'Alpha' });
    expect(await axe(container)).toHaveNoViolations();
  });

  it('group detail (reader) has no axe violations', async () => {
    authState.client.getGroup.mockResolvedValue(detail({ my_role: 'reader' }));
    const { container } = renderWith(wrap(<GroupDetailPage />), {
      route: `/settings/organization/groups/pg_1?org=${ORG}`,
      path: '/settings/organization/groups/:groupId',
    });
    await screen.findByRole('link', { name: 'Alpha' });
    expect(await axe(container)).toHaveNoViolations();
  });

  it('restrict visibility setting and its confirmation have no axe violations', async () => {
    const { container } = renderWith(wrap(<RestrictVisibilitySetting orgId={ORG} restricted={false} canEdit tier="team" />));
    expect(await axe(container)).toHaveNoViolations();
    await userEvent.setup().click(screen.getByRole('switch', { name: 'Restrict project visibility' }));
    await screen.findByRole('dialog');
    expect(await axe(container)).toHaveNoViolations();
  });

  it('project groups block and your groups card have no axe violations', async () => {
    const { container } = renderWith(
      wrap(
        <>
          <ProjectGroupsBlock groups={[summary(), summary({ id: 'pg_2', name: 'Other', my_role: null })]} />
          <YourGroupsCard inOrg />
        </>,
      ),
    );
    await screen.findAllByRole('link', { name: 'Payments' });
    expect(await axe(container)).toHaveNoViolations();
  });
});
