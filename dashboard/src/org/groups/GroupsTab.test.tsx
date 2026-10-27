import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import GroupsTab from './GroupsTab';
import { ORG, apiError, authState, makeClient, renderWith, stubFetch, summary } from './groupTestHarness';

vi.mock('../../auth/AuthContext', async () => (await import('./groupTestHarness')).authModule);
vi.mock('../../hooks/useToast', async () => (await import('./groupTestHarness')).toastModule);

beforeEach(() => {
  authState.client = makeClient();
  stubFetch();
});
afterEach(() => vi.unstubAllGlobals());

describe('GroupsTab', () => {
  it('lists groups with counts and the caller’s role', async () => {
    authState.client.listOrgGroups.mockResolvedValue([
      summary(),
      summary({ id: 'pg_2', name: 'Design', slug: 'design', project_count: 3, member_count: 1, my_role: 'reader', description: null }),
    ]);
    renderWith(<GroupsTab orgId={ORG} isOrgAdmin tier="team" />);
    const link = await screen.findByRole('link', { name: 'Payments' });
    expect(link).toHaveAttribute('href', `/settings/organization/groups/pg_1?org=${ORG}`);
    expect(screen.getByText('Money things')).toBeInTheDocument();
    expect(screen.getByText('3 projects')).toBeInTheDocument();
    expect(screen.getByText('1 person')).toBeInTheDocument();
    expect(screen.getByText('You: Reader')).toBeInTheDocument();
    expect(authState.client.listOrgGroups).toHaveBeenCalledWith(ORG);
  });

  it('creates a group with a slug derived from the name, then opens it', async () => {
    const user = userEvent.setup();
    renderWith(<GroupsTab orgId={ORG} isOrgAdmin tier="enterprise" />, { path: '/' });
    await user.click(await screen.findByRole('button', { name: 'New group' }));
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Checkout & Payments');
    expect(screen.getByRole('textbox', { name: 'Slug' })).toHaveValue('checkout-payments');
    await user.type(screen.getByRole('textbox', { name: 'Description (optional)' }), 'EU services');
    await user.click(screen.getByRole('button', { name: 'Create group' }));
    await waitFor(() =>
      expect(authState.client.createGroup).toHaveBeenCalledWith(ORG, {
        name: 'Checkout & Payments',
        slug: 'checkout-payments',
        description: 'EU services',
      }),
    );
    // Navigated to the new group's page.
    expect(await screen.findByText('elsewhere')).toBeInTheDocument();
  });

  it('keeps an edited slug and validates it', async () => {
    const user = userEvent.setup();
    renderWith(<GroupsTab orgId={ORG} isOrgAdmin tier="team" />, { path: '/' });
    await user.click(await screen.findByRole('button', { name: 'New group' }));
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Ops');
    const slug = screen.getByRole('textbox', { name: 'Slug' });
    await user.clear(slug);
    await user.type(slug, '-bad');
    expect(screen.getByText(/Use lowercase letters/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Create group' }));
    expect(authState.client.createGroup).not.toHaveBeenCalled();
    await user.type(screen.getByRole('textbox', { name: 'Name' }), ' team');
    expect(slug).toHaveValue('-bad'); // an edited slug no longer follows the name
  });

  it('shows the duplicate-slug conflict inside the dialog', async () => {
    authState.client.createGroup.mockRejectedValue(apiError(409, '409', "slug 'ops' exists"));
    const user = userEvent.setup();
    renderWith(<GroupsTab orgId={ORG} isOrgAdmin tier="team" />, { path: '/' });
    await user.click(await screen.findByRole('button', { name: 'New group' }));
    await user.type(screen.getByRole('textbox', { name: 'Name' }), 'Ops');
    await user.click(screen.getByRole('button', { name: 'Create group' }));
    expect(await screen.findByText(/A group with that slug already exists/)).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'New project group' })).toBeInTheDocument();
  });

  it('shows an upgrade notice instead of “New group” on plans without groups', async () => {
    authState.client.listOrgGroups.mockResolvedValue([]);
    renderWith(<GroupsTab orgId={ORG} isOrgAdmin tier="pro" />);
    expect(await screen.findByText('Available on Team and Enterprise')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'New group' })).not.toBeInTheDocument();
  });

  it('members see no create button and a member-specific empty state', async () => {
    authState.client.listOrgGroups.mockResolvedValue([]);
    renderWith(<GroupsTab orgId={ORG} isOrgAdmin={false} tier="team" />);
    expect(await screen.findByText(/haven’t been given access to any project groups/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'New group' })).not.toBeInTheDocument();
  });

  it('reports a load failure', async () => {
    authState.client.listOrgGroups.mockRejectedValue(apiError(500, '500', 'db down'));
    renderWith(<GroupsTab orgId={ORG} isOrgAdmin tier="team" />);
    expect(await screen.findByText(/Couldn’t load groups: db down/)).toBeInTheDocument();
  });
});
