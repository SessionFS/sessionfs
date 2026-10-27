import { screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import YourGroupsCard from './YourGroupsCard';
import {
  apiError,
  authState,
  makeClient,
  renderWith,
  stubFetch,
  summary,
} from '../org/groups/groupTestHarness';

vi.mock('../auth/AuthContext', async () => (await import('../org/groups/groupTestHarness')).authModule);

beforeEach(() => {
  authState.client = makeClient();
  stubFetch({
    '/api/v1/orgs': { orgs: [{ org_id: 'org_1', name: 'Acme' }, { org_id: 'org_2', name: 'Beta Corp' }] },
  });
});
afterEach(() => vi.unstubAllGlobals());

describe('YourGroupsCard', () => {
  it('lists the caller’s groups with roles and links', async () => {
    authState.client.listMyGroups.mockResolvedValue([
      summary(),
      summary({ id: 'pg_2', org_id: 'org_2', name: 'Docs', project_count: 1, my_role: 'reader' }),
    ]);
    renderWith(<YourGroupsCard inOrg />);
    expect(await screen.findByRole('link', { name: 'Payments' })).toHaveAttribute(
      'href',
      '/settings/organization/groups/pg_1?org=org_1',
    );
    expect(screen.getByText('Reader')).toBeInTheDocument();
    expect(screen.getByText('Admin')).toBeInTheDocument();
    // Groups span two orgs, so each row names its org.
    expect(await screen.findByText(/Beta Corp · 1 project/)).toBeInTheDocument();
  });

  it('shows an empty state', async () => {
    authState.client.listMyGroups.mockResolvedValue([]);
    renderWith(<YourGroupsCard inOrg />);
    expect(await screen.findByText(/aren’t in any project groups yet/)).toBeInTheDocument();
  });

  it('shows a load error', async () => {
    authState.client.listMyGroups.mockRejectedValue(apiError(500, '500', 'boom'));
    renderWith(<YourGroupsCard inOrg />);
    expect(await screen.findByText(/Couldn’t load your groups: boom/)).toBeInTheDocument();
  });

  it('is hidden and does not fetch outside an org', () => {
    const { container } = renderWith(<YourGroupsCard inOrg={false} />);
    expect(container).toBeEmptyDOMElement();
    expect(authState.client.listMyGroups).not.toHaveBeenCalled();
  });
});
