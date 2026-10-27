import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { axe } from '../../test/axe';
import IntegrationsTab from './IntegrationsTab';

/**
 * Organization → Integrations overview: one row per integration with its
 * status, site and only the non-zero problems, and a per-integration detail
 * view addressed by `?integration=…&section=…`.
 */

const client = {
  getJiraConnection: vi.fn(),
  getJiraHealth: vi.fn(),
  getConfluenceConnection: vi.fn(),
  getConfluenceHealth: vi.fn(),
  listJiraMappings: vi.fn(),
  listJiraTemplates: vi.fn(),
  listConfluenceMappings: vi.fn(),
  listProjects: vi.fn(),
};

vi.mock('../../auth/AuthContext', () => ({
  useAuth: () => ({ auth: { apiKey: 'sk_test', baseUrl: 'http://test.api', client } }),
}));
vi.mock('../../hooks/useToast', () => ({ useToast: () => ({ addToast: vi.fn() }) }));

const JIRA = {
  connected: true,
  has_token: true,
  site_url: 'https://acme.atlassian.net',
  account_email: 'jira@acme.com',
  token_hint: 'abcd',
  default_template: 'agile',
  last_verified_at: '2026-10-01T12:00:00Z',
};
const HEALTHY_JIRA = {
  unmapped_project_count: 0,
  last_push_failures: [],
  stale_links: [],
  publishes_in_progress: [],
  publishes_unknown: [],
};

function Location() {
  const loc = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location">{loc.search}</output>
      <button type="button" onClick={() => navigate(-1)}>history back</button>
      <button type="button" onClick={() => navigate(1)}>history forward</button>
    </>
  );
}

const BASE = '/settings/organization?tab=integrations';

function renderTab(tier = 'enterprise', url: string | string[] = BASE) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const entries = Array.isArray(url) ? url : [url];
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
        <IntegrationsTab orgId="org_x" tier={tier} />
        <Location />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  for (const fn of Object.values(client)) fn.mockReset();
  client.getJiraConnection.mockResolvedValue(JIRA);
  client.getJiraHealth.mockResolvedValue(HEALTHY_JIRA);
  client.getConfluenceConnection.mockResolvedValue({ connected: false, has_token: false });
  client.getConfluenceHealth.mockResolvedValue({
    unmapped_project_count: 0,
    last_push_failures: [],
    publishes_in_progress: [],
    publishes_unknown: [],
    drifted_links: [],
  });
  client.listJiraMappings.mockResolvedValue([]);
  client.listJiraTemplates.mockResolvedValue([]);
  client.listConfluenceMappings.mockResolvedValue([]);
  client.listProjects.mockResolvedValue([]);
});

describe('IntegrationsTab', () => {
  it('shows one row per integration with status, site and a quiet health line', async () => {
    const { container } = renderTab();
    const list = await screen.findByRole('list', { name: 'Integrations' });
    const [jira, confluence] = within(list).getAllByRole('listitem');

    expect(await within(jira).findByText('All good')).toBeInTheDocument();
    expect(within(jira).getByText('acme.atlassian.net')).toBeInTheDocument();
    expect(within(jira).getByRole('button', { name: 'Manage Jira' })).toBeInTheDocument();
    expect(await within(confluence).findByText('Not connected')).toBeInTheDocument();
    expect(within(confluence).getByRole('button', { name: 'Connect Confluence' })).toBeInTheDocument();
    // An unconnected integration never asks for its health.
    expect(client.getConfluenceHealth).not.toHaveBeenCalled();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('summarises only the problems that are non-zero', async () => {
    client.getJiraHealth.mockResolvedValue({
      ...HEALTHY_JIRA,
      unmapped_project_count: 1,
      last_push_failures: [{ ticket_id: 't_1', issue_key: 'KEY-1', failed_at: null, reason: 'boom' }],
      publishes_in_progress: [{ ticket_id: 't_2', started_at: null }],
    });
    renderTab();
    expect(await screen.findByText('1 unmapped project · 1 push failure')).toBeInTheDocument();
    // In-progress publishes are activity, not problems.
    expect(screen.queryByText(/in progress/)).not.toBeInTheDocument();
  });

  it('opens the detail in the URL and returns through the breadcrumb', async () => {
    renderTab();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Manage Jira' }));

    expect(screen.getByTestId('location')).toHaveTextContent('integration=jira');
    expect(await screen.findByRole('heading', { name: 'Jira', level: 2 })).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Sync' }));
    expect(screen.getByTestId('location')).toHaveTextContent('section=sync');

    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' });
    await user.click(within(crumbs).getByRole('button', { name: 'Integrations' }));
    expect(screen.getByTestId('location')).not.toHaveTextContent('integration=');
    expect(await screen.findByRole('list', { name: 'Integrations' })).toBeInTheDocument();
  });

  it('deep-links straight into a section', async () => {
    renderTab('enterprise', '/settings/organization?tab=integrations&integration=jira&section=connection');
    expect(await screen.findByRole('button', { name: 'Edit connection' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Connection' })).toHaveAttribute('aria-selected', 'true');
  });

  it('Connect opens the connection form of an unconnected integration', async () => {
    client.getConfluenceConnection.mockResolvedValue({ connected: false, has_token: false });
    renderTab();
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Connect Confluence' }));
    expect(screen.getByTestId('location')).toHaveTextContent('integration=confluence&section=connection');
    expect(await screen.findByRole('button', { name: 'Connect Confluence' })).toBeDisabled();
  });

  it('shows the plan requirement below Enterprise without calling the APIs', async () => {
    renderTab('team');
    expect(screen.getAllByText('Available on Enterprise')).toHaveLength(2);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Learn more about Jira' }));
    expect(screen.getByText('Jira Integration')).toBeInTheDocument();
    expect(client.getJiraConnection).not.toHaveBeenCalled();
  });
  it('follows Back and Forward between sections', async () => {
    renderTab('enterprise', [`${BASE}&integration=jira&section=overview`, `${BASE}&integration=jira&section=sync`]);
    const user = userEvent.setup();
    expect(await screen.findByRole('tab', { name: 'Sync' })).toHaveAttribute('aria-selected', 'true');

    await user.click(screen.getByRole('button', { name: 'history back' }));
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
    await user.click(screen.getByRole('button', { name: 'history forward' }));
    expect(screen.getByRole('tab', { name: 'Sync' })).toHaveAttribute('aria-selected', 'true');
  });

  it('falls back to the overview for an unknown section', async () => {
    renderTab('enterprise', `${BASE}&integration=jira&section=bogus`);
    expect(await screen.findByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
  });

  it('replaces history when moving between sections, so Back leaves the detail', async () => {
    renderTab();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Manage Jira' }));
    await user.click(screen.getByRole('tab', { name: 'Sync' }));
    await user.click(screen.getByRole('tab', { name: 'Activity' }));
    await user.click(screen.getByRole('button', { name: 'history back' }));
    expect(screen.getByTestId('location')).not.toHaveTextContent('integration=');
    expect(await screen.findByRole('list', { name: 'Integrations' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'history forward' }));
    expect(screen.getByRole('tab', { name: 'Activity' })).toHaveAttribute('aria-selected', 'true');
  });

  it('moves focus to the detail heading and back to the list', async () => {
    renderTab();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Manage Jira' }));
    expect(screen.getByRole('heading', { name: 'Jira', level: 2 })).toHaveFocus();

    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' });
    await user.click(within(crumbs).getByRole('button', { name: 'Integrations' }));
    expect(screen.getByRole('list', { name: 'Integrations' })).toHaveFocus();
  });

  it('never reports a failed health read as healthy', async () => {
    client.getJiraHealth.mockRejectedValue(new Error('boom'));
    renderTab();
    const list = await screen.findByRole('list', { name: 'Integrations' });
    const [jira] = within(list).getAllByRole('listitem');
    expect(await within(jira).findByText('Health unavailable')).toBeInTheDocument();
    expect(within(jira).queryByText('All good')).not.toBeInTheDocument();
  });

  it('shows an error with Retry, not Connect, when the connection cannot be read', async () => {
    client.getConfluenceConnection.mockRejectedValue(new Error('boom'));
    renderTab();
    const list = await screen.findByRole('list', { name: 'Integrations' });
    const confluence = within(list).getAllByRole('listitem')[1];
    expect(await within(confluence).findByText('Could not load the connection')).toBeInTheDocument();
    expect(within(confluence).queryByText('Not connected')).not.toBeInTheDocument();
    expect(within(confluence).queryByRole('button', { name: 'Connect Confluence' })).not.toBeInTheDocument();

    client.getConfluenceConnection.mockResolvedValue({ connected: false, has_token: false });
    await userEvent.setup().click(within(confluence).getByRole('button', { name: 'Retry loading Confluence' }));
    expect(await within(confluence).findByRole('button', { name: 'Connect Confluence' })).toBeInTheDocument();
  });
});
