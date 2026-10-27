import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import GroupDetailPage from './GroupDetailPage';
import {
  ORG,
  apiError,
  authState,
  detail,
  makeClient,
  orgMembers,
  renderWith,
  stubFetch,
  toast,
} from './groupTestHarness';

vi.mock('../../auth/AuthContext', async () => (await import('./groupTestHarness')).authModule);
vi.mock('../../hooks/useToast', async () => (await import('./groupTestHarness')).toastModule);

const PATH = '/settings/organization/groups/:groupId';
const ROUTE = `/settings/organization/groups/pg_1?org=${ORG}`;

function renderPage(route = ROUTE) {
  return renderWith(<GroupDetailPage />, { route, path: PATH });
}

function asMember() {
  stubFetch({
    '/api/v1/org': { org: { id: ORG, name: 'Acme', slug: 'acme', tier: 'team' }, current_user_role: 'member' },
    [`/api/v1/orgs/${ORG}/members`]: { org_id: ORG, members: orgMembers, current_user_role: 'member' },
  });
}

beforeEach(() => {
  authState.client = makeClient();
  toast.addToast.mockReset();
  stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

describe('GroupDetailPage — group admin', () => {
  it('renders the header, projects and access', async () => {
    renderPage();
    expect(await screen.findByRole('heading', { level: 1, name: 'Payments' })).toBeInTheDocument();
    expect(screen.getByText('Money things')).toBeInTheDocument();
    expect(await screen.findByRole('link', { name: 'Alpha' })).toHaveAttribute('href', '/projects/proj_a');
    expect(screen.getByText('peer@example.test')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Delete' })).toBeInTheDocument();
    expect(authState.client.getGroup).toHaveBeenCalledWith(ORG, 'pg_1');
  });

  it('labels member projects the caller cannot open', async () => {
    authState.client.getGroup.mockResolvedValue(detail({ projects: ['proj_a', 'proj_secret'] }));
    renderPage();
    expect(await screen.findByText('A project you can’t open')).toBeInTheDocument();
    expect(screen.getAllByTitle('proj_secret').length).toBeGreaterThan(0);
  });

  it('adds a project picked from editable org projects', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole('link', { name: 'Alpha' });
    await user.click(screen.getByRole('button', { name: 'Add a project' }));
    const list = await screen.findByRole('listbox', { name: 'Add a project' });
    // Already in the group, personal and (for org admins, still offered) read-only ones.
    expect(within(list).queryByRole('option', { name: 'Alpha' })).not.toBeInTheDocument();
    expect(within(list).queryByRole('option', { name: 'Personal' })).not.toBeInTheDocument();
    await user.click(within(list).getByRole('option', { name: 'Beta' }));
    await user.click(screen.getByRole('button', { name: 'Add project' }));
    await waitFor(() => expect(authState.client.addGroupProject).toHaveBeenCalledWith(ORG, 'pg_1', 'proj_b'));
    expect(toast.addToast).toHaveBeenCalledWith('success', 'Added Beta to Payments.');
  });

  it('only offers projects the caller can edit when they are not an org admin', async () => {
    asMember();
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole('link', { name: 'Alpha' });
    await user.click(screen.getByRole('button', { name: 'Add a project' }));
    const list = await screen.findByRole('listbox', { name: 'Add a project' });
    expect(within(list).getByRole('option', { name: 'Beta' })).toBeInTheDocument();
    expect(within(list).queryByRole('option', { name: 'Gamma (read only)' })).not.toBeInTheDocument();
    // Members never get Delete.
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
  });

  it('maps project_edit_forbidden when adding fails', async () => {
    authState.client.addGroupProject.mockRejectedValue(apiError(403, 'project_edit_forbidden', 'nope'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole('link', { name: 'Alpha' });
    await user.click(screen.getByRole('button', { name: 'Add a project' }));
    await user.click(await screen.findByRole('option', { name: 'Beta' }));
    await user.click(screen.getByRole('button', { name: 'Add project' }));
    expect(await screen.findByText(/You can only add projects you can edit/)).toBeInTheDocument();
  });

  it('removes a project after confirming', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Remove Alpha from the group' }));
    const dialog = screen.getByRole('dialog', { name: 'Remove project from group' });
    await user.click(within(dialog).getByRole('button', { name: 'Remove project' }));
    await waitFor(() => expect(authState.client.removeGroupProject).toHaveBeenCalledWith(ORG, 'pg_1', 'proj_a'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('cancelling the remove dialog does nothing', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Remove Alpha from the group' }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(authState.client.removeGroupProject).not.toHaveBeenCalled();
  });

  it('grants a member with a role', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('peer@example.test');
    await user.click(screen.getByRole('button', { name: 'Member' }));
    const list = await screen.findByRole('listbox', { name: 'Member' });
    // Already granted members are not offered.
    expect(within(list).queryByRole('option', { name: /peer@example.test/ })).not.toBeInTheDocument();
    await user.click(within(list).getByRole('option', { name: 'New Person (new@example.test)' }));
    await user.click(screen.getByRole('button', { name: 'Role' }));
    await user.click(await screen.findByRole('option', { name: 'Editor' }));
    await user.click(screen.getByRole('button', { name: 'Grant access' }));
    await waitFor(() =>
      expect(authState.client.grantGroupMember).toHaveBeenCalledWith(ORG, 'pg_1', 'u_new', 'editor'),
    );
  });

  it('shows not_org_member when the grantee left the org', async () => {
    authState.client.grantGroupMember.mockRejectedValue(apiError(422, 'not_org_member', 'gone'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('peer@example.test');
    await user.click(screen.getByRole('button', { name: 'Member' }));
    await user.click(await screen.findByRole('option', { name: 'New Person (new@example.test)' }));
    await user.click(screen.getByRole('button', { name: 'Grant access' }));
    expect(await screen.findByText(/not a member of this organization/)).toBeInTheDocument();
  });

  it('changes a role inline', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Role for peer@example.test' }));
    await user.click(await screen.findByRole('option', { name: 'Admin' }));
    await waitFor(() =>
      expect(authState.client.grantGroupMember).toHaveBeenCalledWith(ORG, 'pg_1', 'u_peer', 'admin'),
    );
  });

  it('revokes access after confirming', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Revoke access for peer@example.test' }));
    const dialog = screen.getByRole('dialog', { name: 'Revoke access' });
    await user.click(within(dialog).getByRole('button', { name: 'Revoke access' }));
    await waitFor(() => expect(authState.client.revokeGroupMember).toHaveBeenCalledWith(ORG, 'pg_1', 'u_peer'));
  });

  it('shows a conflict inside the revoke dialog', async () => {
    authState.client.revokeGroupMember.mockRejectedValue(apiError(409, 'concurrent_update', 'race'));
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Revoke access for peer@example.test' }));
    const dialog = screen.getByRole('dialog', { name: 'Revoke access' });
    await user.click(within(dialog).getByRole('button', { name: 'Revoke access' }));
    expect(await within(dialog).findByText(/same moment/)).toBeInTheDocument();
  });

  it('edits the name and description', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Edit' }));
    const name = screen.getByRole('textbox', { name: 'Name' });
    await user.clear(name);
    await user.type(name, 'Payments EU');
    const desc = screen.getByRole('textbox', { name: 'Description (optional)' });
    await user.clear(desc);
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(authState.client.updateGroup).toHaveBeenCalledWith(ORG, 'pg_1', { name: 'Payments EU', description: null }),
    );
  });

  it('org admins delete the group after confirming and return to the list', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Delete' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete group' });
    await user.click(within(dialog).getByRole('button', { name: 'Delete group' }));
    await waitFor(() => expect(authState.client.deleteGroup).toHaveBeenCalledWith(ORG, 'pg_1'));
    expect(await screen.findByText('elsewhere')).toBeInTheDocument();
  });

  it('hides add and grant controls behind an upgrade notice on plans without groups', async () => {
    stubFetch({ '/api/v1/org': { org: { id: ORG, name: 'Acme', slug: 'acme', tier: 'pro' }, current_user_role: 'admin' } });
    renderPage();
    await screen.findByRole('link', { name: 'Alpha' });
    expect(screen.getAllByText(/Team or Enterprise plan/).length).toBe(2);
    expect(screen.queryByRole('button', { name: 'Add project' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Grant access' })).not.toBeInTheDocument();
    // Cleanup still works after a downgrade.
    expect(screen.getByRole('button', { name: 'Remove Alpha from the group' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Revoke access for peer@example.test' })).toBeInTheDocument();
  });
});

describe('GroupDetailPage — review follow-ups', () => {
  it('asks before a group admin lowers their own role', async () => {
    asMember();
    authState.client.getGroup.mockResolvedValue(
      detail({
        members: [{ user_id: 'u_me', email: 'me@example.test', role: 'admin', granted_by_user_id: null, created_at: null }],
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Role for me@example.test' }));
    await user.click(await screen.findByRole('option', { name: 'Reader' }));
    const dialog = screen.getByRole('dialog', { name: 'Lower your own role?' });
    expect(within(dialog).getByText(/only another group admin or an org admin can give those rights back/)).toBeInTheDocument();
    expect(authState.client.grantGroupMember).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole('button', { name: 'Make me reader' }));
    await waitFor(() =>
      expect(authState.client.grantGroupMember).toHaveBeenCalledWith(ORG, 'pg_1', 'u_me', 'reader'),
    );
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('cancelling the self-downgrade changes nothing', async () => {
    asMember();
    authState.client.getGroup.mockResolvedValue(
      detail({
        members: [{ user_id: 'u_me', email: 'me@example.test', role: 'admin', granted_by_user_id: null, created_at: null }],
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Role for me@example.test' }));
    await user.click(await screen.findByRole('option', { name: 'Editor' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    expect(authState.client.grantGroupMember).not.toHaveBeenCalled();
  });

  it('org admins lower their own grant without a prompt', async () => {
    authState.client.getGroup.mockResolvedValue(
      detail({
        members: [{ user_id: 'u_me', email: 'me@example.test', role: 'admin', granted_by_user_id: null, created_at: null }],
      }),
    );
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Role for me@example.test' }));
    await user.click(await screen.findByRole('option', { name: 'Reader' }));
    await waitFor(() =>
      expect(authState.client.grantGroupMember).toHaveBeenCalledWith(ORG, 'pg_1', 'u_me', 'reader'),
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('labels an org admin who also holds a lower grant', async () => {
    authState.client.getGroup.mockResolvedValue(
      detail({
        members: [{ user_id: 'u_me', email: 'me@example.test', role: 'reader', granted_by_user_id: null, created_at: null }],
      }),
    );
    renderPage();
    expect(await screen.findByText(/Org admin \(also reader grant\)/)).toBeInTheDocument();
  });

  it('shows the org-admin label as the badge in read-only views', async () => {
    asMember();
    authState.client.getGroup.mockResolvedValue(
      detail({
        my_role: 'reader',
        members: [{ user_id: 'u_boss', email: 'boss@example.test', role: 'editor', granted_by_user_id: null, created_at: null }],
      }),
    );
    stubFetch({
      [`/api/v1/orgs/${ORG}/members`]: {
        org_id: ORG,
        members: [...orgMembers, { user_id: 'u_boss', email: 'boss@example.test', display_name: null, role: 'owner', joined_at: null }],
        current_user_role: 'member',
      },
    });
    renderPage();
    expect(await screen.findByText('Org admin (also editor grant)')).toBeInTheDocument();
  });

  it('falls back to /org for the caller’s role when the roster fails', async () => {
    stubFetch({ [`/api/v1/orgs/${ORG}/members`]: { __status: 500, body: { detail: 'down' } } });
    renderPage();
    expect(await screen.findByRole('button', { name: 'Delete' })).toBeInTheDocument();
  });

  it('reports a project list failure instead of calling every project unopenable', async () => {
    authState.client.listProjects.mockRejectedValue(apiError(500, '500', 'projects down'));
    renderPage();
    expect(await screen.findByText(/Couldn’t load project details: projects down/)).toBeInTheDocument();
    expect(screen.getByText('Project details unavailable')).toBeInTheDocument();
    expect(screen.queryByText('A project you can’t open')).not.toBeInTheDocument();
  });

  it('qualifies the delete warning with restricted visibility', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(screen.getByText(/When the organization restricts project visibility, everyone who reaches/)).toBeInTheDocument();
  });

  it('links back to a secondary org’s groups', async () => {
    stubFetch({
      '/api/v1/org': { org: { id: 'org_other', name: 'Other', slug: 'o', tier: 'team' }, current_user_role: 'member' },
    });
    renderPage();
    expect(await screen.findByRole('link', { name: '← Groups' })).toHaveAttribute(
      'href',
      `/settings/organization?org=${ORG}&tab=groups`,
    );
  });
});

describe('GroupDetailPage — read-only and error states', () => {
  it.each(['reader', 'editor'] as const)('renders read-only for a group %s', async (role) => {
    asMember();
    authState.client.getGroup.mockResolvedValue(detail({ my_role: role }));
    renderPage();
    expect(await screen.findByText(new RegExp(`You have ${role} access to this group`))).toBeInTheDocument();
    expect(await screen.findByRole('link', { name: 'Alpha' })).toBeInTheDocument();
    for (const name of ['Edit', 'Delete', 'Add project', 'Grant access']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: /Remove Alpha/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Revoke access/ })).not.toBeInTheDocument();
    const access = screen.getByRole('list', { name: 'People with access' });
    expect(within(access).getByText('Editor')).toBeInTheDocument(); // the peer's role as a badge
  });

  it('shows a not-found message for a 404', async () => {
    authState.client.getGroup.mockRejectedValue(apiError(404, '404', 'Project group not found'));
    renderPage();
    expect(await screen.findByText(/doesn’t exist, was deleted, or you don’t have access/)).toBeInTheDocument();
  });

  it('shows the “no longer have access” state after revoking oneself', async () => {
    authState.client.getGroup.mockResolvedValue(detail({ my_role: null, members: [], projects: [] }));
    renderPage();
    expect(await screen.findByText('You no longer have access to this group')).toBeInTheDocument();
  });

  it('falls back to the primary org when ?org is missing', async () => {
    renderPage('/settings/organization/groups/pg_1');
    await screen.findByRole('heading', { level: 1, name: 'Payments' });
    expect(authState.client.getGroup).toHaveBeenCalledWith(ORG, 'pg_1');
  });
});
