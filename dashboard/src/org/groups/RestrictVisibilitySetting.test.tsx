import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import RestrictVisibilitySetting from './RestrictVisibilitySetting';
import { ORG, authState, makeClient, renderWith, stubFetch, toast } from './groupTestHarness';

vi.mock('../../auth/AuthContext', async () => (await import('./groupTestHarness')).authModule);
vi.mock('../../hooks/useToast', async () => (await import('./groupTestHarness')).toastModule);

const SETTINGS = `/api/v1/orgs/${ORG}/settings`;

let puts: unknown[];
function stubSettings(answer: (body: Record<string, unknown>) => unknown = (b) => ({ ...b })) {
  puts = [];
  return stubFetch({
    [SETTINGS]: (init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      if (init?.method === 'PUT') puts.push(body);
      return answer(body);
    },
  });
}

beforeEach(() => {
  authState.client = makeClient();
  toast.addToast.mockReset();
  stubSettings();
});
afterEach(() => vi.unstubAllGlobals());

describe('RestrictVisibilitySetting', () => {
  it('explains both states', () => {
    renderWith(<RestrictVisibilitySetting orgId={ORG} restricted={false} canEdit tier="team" />);
    expect(screen.getByRole('switch', { name: 'Restrict project visibility' })).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByText(/every member of this organization can see and edit every org project/)).toBeInTheDocument();
    expect(screen.getByText(/Org admins and owners still see and change everything/)).toBeInTheDocument();
    expect(screen.getByText(/A reader grant is read-only/)).toBeInTheDocument();
    expect(screen.getByText(/no longer gives access to a restricted project/)).toBeInTheDocument();
  });

  it('asks for confirmation before turning it on, then saves only that field', async () => {
    const user = userEvent.setup();
    renderWith(<RestrictVisibilitySetting orgId={ORG} restricted={false} canEdit tier="team" />);
    await user.click(screen.getByRole('switch', { name: 'Restrict project visibility' }));
    const dialog = screen.getByRole('dialog', { name: 'Restrict project visibility?' });
    expect(puts).toHaveLength(0);
    await user.click(within(dialog).getByRole('button', { name: 'Restrict visibility' }));
    await waitFor(() => expect(puts).toEqual([{ restrict_project_visibility: true }]));
    await waitFor(() => expect(toast.addToast).toHaveBeenCalledWith('success', 'Project visibility is now restricted.'));
  });

  it('cancelling the confirmation saves nothing', async () => {
    const user = userEvent.setup();
    renderWith(<RestrictVisibilitySetting orgId={ORG} restricted={false} canEdit tier="team" />);
    await user.click(screen.getByRole('switch', { name: 'Restrict project visibility' }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(puts).toHaveLength(0);
  });

  it('confirms turning it off too', async () => {
    const user = userEvent.setup();
    renderWith(<RestrictVisibilitySetting orgId={ORG} restricted canEdit tier="team" />);
    await user.click(screen.getByRole('switch', { name: 'Restrict project visibility' }));
    const dialog = screen.getByRole('dialog', { name: 'Open projects to every member?' });
    await user.click(within(dialog).getByRole('button', { name: 'Turn off restriction' }));
    await waitFor(() => expect(puts).toEqual([{ restrict_project_visibility: false }]));
  });

  it('blocks turning it on without the plan feature, with an upgrade notice', () => {
    renderWith(<RestrictVisibilitySetting orgId={ORG} restricted={false} canEdit tier="pro" />);
    expect(screen.getByRole('switch', { name: 'Restrict project visibility' })).toBeDisabled();
    expect(screen.getByText('Available on Team and Enterprise')).toBeInTheDocument();
  });

  it('still lets an org that downgraded turn it off', () => {
    renderWith(<RestrictVisibilitySetting orgId={ORG} restricted canEdit tier="pro" />);
    expect(screen.getByRole('switch', { name: 'Restrict project visibility' })).toBeEnabled();
    expect(screen.queryByText('Available on Team and Enterprise')).not.toBeInTheDocument();
  });

  it('maps a server upgrade_required answer to the upgrade notice', async () => {
    stubSettings(() => ({
      __status: 403,
      body: { error: { code: '403', message: 'Restricted project visibility requires the Team plan or above.', details: { error: 'upgrade_required' } } },
    }));
    const user = userEvent.setup();
    renderWith(<RestrictVisibilitySetting orgId={ORG} restricted={false} canEdit />);
    await user.click(screen.getByRole('switch', { name: 'Restrict project visibility' }));
    const dialog = screen.getByRole('dialog', { name: 'Restrict project visibility?' });
    await user.click(within(dialog).getByRole('button', { name: 'Restrict visibility' }));
    expect(await within(dialog).findByText(/Team or Enterprise plan/)).toBeInTheDocument();
  });

  it('shows a save error only in the open dialog, then inline once it closes', async () => {
    stubSettings(() => ({ __status: 500, body: { error: { code: '500', message: 'db down' } } }));
    const user = userEvent.setup();
    renderWith(<RestrictVisibilitySetting orgId={ORG} restricted={false} canEdit tier="team" />);
    await user.click(screen.getByRole('switch', { name: 'Restrict project visibility' }));
    const dialog = screen.getByRole('dialog', { name: 'Restrict project visibility?' });
    expect(within(dialog).getByText(/edit or admin|editor or admin/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Restrict visibility' }));
    expect(await within(dialog).findByText(/db down/)).toBeInTheDocument();
    expect(screen.getAllByText(/db down/)).toHaveLength(1);
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.getAllByText(/db down/)).toHaveLength(1);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows the upgrade answer once, not inline and in the dialog', async () => {
    stubSettings(() => ({
      __status: 403,
      body: { error: { code: '403', message: 'nope', details: { error: 'upgrade_required' } } },
    }));
    const user = userEvent.setup();
    renderWith(<RestrictVisibilitySetting orgId={ORG} restricted={false} canEdit />);
    await user.click(screen.getByRole('switch', { name: 'Restrict project visibility' }));
    const dialog = screen.getByRole('dialog', { name: 'Restrict project visibility?' });
    await user.click(within(dialog).getByRole('button', { name: 'Restrict visibility' }));
    await within(dialog).findByText(/Team or Enterprise plan/);
    expect(screen.getAllByText(/Team or Enterprise plan/)).toHaveLength(1);
    expect(screen.queryByText('Available on Team and Enterprise')).not.toBeInTheDocument();
  });

  it('is read-only for members', () => {
    renderWith(<RestrictVisibilitySetting orgId={ORG} restricted canEdit={false} tier="team" />);
    expect(screen.getByRole('switch', { name: 'Restrict project visibility' })).toBeDisabled();
    expect(screen.getByText(/Only org owners and admins can change this/)).toBeInTheDocument();
  });
});
