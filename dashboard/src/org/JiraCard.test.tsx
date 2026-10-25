import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { JiraConnection, JiraHealth, JiraMapping, ProjectContext } from '../api/client';
import { axe } from '../test/axe';
import { JiraCard } from './JiraCard';

/**
 * Org Settings → Integrations → Jira (Enterprise, owner/admin). The card
 * itself gates on tier (non-Enterprise shows an upgrade note; the tab itself
 * is admin-gated in OrgPage). This suite covers tier gating, connection save
 * validation, disconnect, mapping CRUD, and the health panel, plus axe.
 */

const mockAuth = vi.fn();
const mockListProjects = vi.fn();
const mockGetConnection = vi.fn();
const mockSaveConnection = vi.fn();
const mockDeleteConnection = vi.fn();
const mockListJiraProjects = vi.fn();
const mockListJiraIssueTypes = vi.fn();
const mockListMappings = vi.fn();
const mockSaveMapping = vi.fn();
const mockDeleteMapping = vi.fn();
const mockGetHealth = vi.fn();
const addToast = vi.fn();

vi.mock('../auth/AuthContext', () => ({ useAuth: () => mockAuth() }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

function renderCard(tier: string) {
  const qc = makeQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <JiraCard orgId="org_x" tier={tier} />
    </QueryClientProvider>,
  );
}

const NOT_CONNECTED: JiraConnection = { connected: false, has_token: false };
const CONNECTED: JiraConnection = {
  connected: true,
  has_token: true,
  site_url: 'https://acme.atlassian.net',
  account_email: 'jira@acme.com',
  token_hint: 'abcd',
  default_template: 'agile',
  last_verified_at: '2026-10-01T12:00:00Z',
};

const PROJECTS: ProjectContext[] = [
  { id: 'proj_a', name: 'Alpha', git_remote_normalized: 'git@host:alpha.git', context_document: '', owner_id: 'u_1', org_id: 'org_x', created_at: '', updated_at: '' },
  { id: 'proj_b', name: 'Beta', git_remote_normalized: 'git@host:beta.git', context_document: '', owner_id: 'u_2', org_id: 'org_y', created_at: '', updated_at: '' },
];

const HEALTHY: JiraHealth = {
  unmapped_project_count: 0,
  last_push_failures: [],
  stale_links: [],
  publishes_in_progress: [],
  publishes_unknown: [],
};

const EMPTY_MAPPINGS: JiraMapping[] = [];

beforeEach(() => {
  mockAuth.mockReset();
  mockListProjects.mockReset();
  mockGetConnection.mockReset();
  mockSaveConnection.mockReset();
  mockDeleteConnection.mockReset();
  mockListJiraProjects.mockReset();
  mockListJiraIssueTypes.mockReset();
  mockListMappings.mockReset();
  mockSaveMapping.mockReset();
  mockDeleteMapping.mockReset();
  mockGetHealth.mockReset();
  addToast.mockReset();

  mockAuth.mockReturnValue({
    auth: {
      apiKey: 'sk_test',
      baseUrl: 'http://test.api',
      client: {
        listProjects: mockListProjects,
        getJiraConnection: mockGetConnection,
        saveJiraConnection: mockSaveConnection,
        deleteJiraConnection: mockDeleteConnection,
        listJiraProjects: mockListJiraProjects,
        listJiraIssueTypes: mockListJiraIssueTypes,
        listJiraMappings: mockListMappings,
        saveJiraMapping: mockSaveMapping,
        deleteJiraMapping: mockDeleteMapping,
        getJiraHealth: mockGetHealth,
      },
    },
  });

  mockListProjects.mockResolvedValue(PROJECTS);
  mockGetConnection.mockResolvedValue(NOT_CONNECTED);
  mockListJiraProjects.mockResolvedValue([{ key: 'KEY', name: 'Acme Project', issue_types: [] }]);
  mockListJiraIssueTypes.mockResolvedValue([{ name: 'Story', id: '10001' }, { name: 'Task', id: '10002' }]);
  mockListMappings.mockResolvedValue(EMPTY_MAPPINGS);
  mockGetHealth.mockResolvedValue(HEALTHY);
});

/** Click a custom Select trigger by its placeholder/title, then pick an option. */
async function selectOption(user: ReturnType<typeof userEvent.setup>, triggerName: string, optionName: string) {
  await user.click(screen.getByRole('button', { name: triggerName }));
  await user.click(screen.getByRole('option', { name: optionName }));
}

describe('JiraCard', () => {
  it('shows the Enterprise upgrade note for non-Enterprise admins', () => {
    renderCard('team');
    expect(screen.getByText('Jira Integration')).toBeInTheDocument();
    expect(screen.getByText(/Available on Enterprise/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /connect jira/i })).not.toBeInTheDocument();
  });

  it('shows the not-connected state with a disabled connect button', async () => {
    renderCard('enterprise');
    expect(await screen.findByText('Not connected')).toBeInTheDocument();
    expect(screen.getByLabelText('Site URL')).toBeInTheDocument();
    expect(screen.getByLabelText('Account email')).toBeInTheDocument();
    expect(screen.getByLabelText('API token')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Template' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect Jira' })).toBeDisabled();
  });

  it('enables Connect only once site, email, token and template are set, and posts the payload', async () => {
    renderCard('enterprise');
    const user = userEvent.setup();
    await screen.findByText('Not connected');

    const connect = screen.getByRole('button', { name: 'Connect Jira' });
    await user.type(screen.getByLabelText('Site URL'), 'https://acme.atlassian.net');
    await user.type(screen.getByLabelText('Account email'), 'jira@acme.com');
    await user.type(screen.getByLabelText('API token'), 'secret');
    // Template still unset — the button must stay disabled.
    expect(connect).toBeDisabled();

    await selectOption(user, 'Template', 'Agile Standard');
    expect(connect).toBeEnabled();

    mockSaveConnection.mockResolvedValue(CONNECTED);
    await user.click(connect);

    await waitFor(() => {
      expect(mockSaveConnection).toHaveBeenCalledWith('org_x', {
        site_url: 'https://acme.atlassian.net',
        account_email: 'jira@acme.com',
        api_token: 'secret',
        default_template: 'agile',
      });
    });
    await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'Jira connected'));
  });

  it('renders the connected state with site, token hint, mappings and health', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    const { container } = renderCard('enterprise');

    expect(await screen.findByText(/Connected to/)).toBeInTheDocument();
    expect(screen.getByTitle('https://acme.atlassian.net')).toBeInTheDocument();
    expect(screen.getByText(/token abcd/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Update connection' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
    // Mappings + health panels render once connected.
    expect(await screen.findByText('Project mappings')).toBeInTheDocument();
    expect(screen.getByText('No mappings yet. Map a SessionFS project to a Jira project to enable promotion.')).toBeInTheDocument();
    expect(await screen.findByText('Health')).toBeInTheDocument();

    expect(await axe(container)).toHaveNoViolations();
  });

  it('disconnects after confirmation', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockDeleteConnection.mockResolvedValue(undefined);
    renderCard('enterprise');
    const user = userEvent.setup();

    await screen.findByText(/Connected to/);
    await user.click(screen.getByRole('button', { name: 'Disconnect' }));

    const dialog = await screen.findByRole('dialog', { name: 'Disconnect Jira?' });
    await user.click(within(dialog).getByRole('button', { name: 'Disconnect' }));

    await waitFor(() => expect(mockDeleteConnection).toHaveBeenCalledWith('org_x'));
    await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'Jira disconnected'));
  });

  it('adds a mapping with a lazily-loaded issue type', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockSaveMapping.mockImplementation(async (_orgId: string, _projectId: string, body: unknown) => body);
    renderCard('enterprise');
    const user = userEvent.setup();

    await screen.findByText(/Connected to/);
    await user.click(screen.getByRole('button', { name: 'Add mapping' }));

    const dialog = await screen.findByRole('dialog', { name: 'Add mapping' });
    await selectOption(user, 'SessionFS project', 'Alpha');
    await selectOption(user, 'Jira project', 'KEY · Acme Project');

    // Issue types load lazily once a Jira project is chosen; the first one
    // (Story) is pre-selected. Switch it to Task to exercise the override.
    const issueTypeTrigger = within(dialog).getByRole('button', { name: 'Issue type' });
    await waitFor(() => expect(issueTypeTrigger).toBeEnabled());
    await user.click(issueTypeTrigger);
    await user.click(within(dialog).getByRole('option', { name: 'Task' }));

    await user.click(within(dialog).getByRole('button', { name: 'Save mapping' }));

    await waitFor(() => {
      expect(mockSaveMapping).toHaveBeenCalledWith('org_x', 'proj_a', {
        jira_project_key: 'KEY',
        issue_type: 'Task',
        template: null,
        done_status_name: null,
      });
    });
    await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'Mapping saved for Alpha'));
  });

  it('edits an existing mapping with prefilled values', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockListMappings.mockResolvedValue([
      { project_id: 'proj_a', jira_project_key: 'KEY', issue_type: 'Story', template: null, done_status_name: 'Done' },
    ]);
    renderCard('enterprise');
    const user = userEvent.setup();

    await screen.findByText('Alpha');
    await user.click(screen.getByRole('button', { name: 'Edit' }));

    const dialog = await screen.findByRole('dialog', { name: 'Edit mapping' });
    expect(within(dialog).getByRole('button', { name: 'SessionFS project' })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Jira project' })).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Done status (optional)')).toHaveValue('Done');

    await user.click(within(dialog).getByRole('button', { name: 'Save mapping' }));

    await waitFor(() => {
      expect(mockSaveMapping).toHaveBeenCalledWith('org_x', 'proj_a', {
        jira_project_key: 'KEY',
        issue_type: 'Story',
        template: null,
        done_status_name: 'Done',
      });
    });
  });

  it('keeps the stored issue type and disables Save while issue types load', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockListMappings.mockResolvedValue([
      { project_id: 'proj_a', jira_project_key: 'KEY', issue_type: 'Bug', template: null, done_status_name: null },
    ]);
    // Never resolves: the issue-type list stays in flight.
    mockListJiraIssueTypes.mockImplementation(() => new Promise(() => {}));
    renderCard('enterprise');
    const user = userEvent.setup();

    await screen.findByText('Alpha');
    await user.click(screen.getByRole('button', { name: 'Edit' }));

    const dialog = await screen.findByRole('dialog', { name: 'Edit mapping' });
    expect(within(dialog).getByRole('button', { name: 'Save mapping' })).toBeDisabled();
  });

  it('keeps the stored issue type when issue types fail to load', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockListMappings.mockResolvedValue([
      { project_id: 'proj_a', jira_project_key: 'KEY', issue_type: 'Bug', template: null, done_status_name: null },
    ]);
    mockListJiraIssueTypes.mockRejectedValue(new Error('nope'));
    mockSaveMapping.mockImplementation(async (_orgId: string, _projectId: string, body: unknown) => body);
    renderCard('enterprise');
    const user = userEvent.setup();

    await screen.findByText('Alpha');
    await user.click(screen.getByRole('button', { name: 'Edit' }));

    const dialog = await screen.findByRole('dialog', { name: 'Edit mapping' });
    await user.click(within(dialog).getByRole('button', { name: 'Save mapping' }));

    await waitFor(() => {
      expect(mockSaveMapping).toHaveBeenCalledWith('org_x', 'proj_a', {
        jira_project_key: 'KEY',
        issue_type: 'Bug',
        template: null,
        done_status_name: null,
      });
    });
  });

  it('removes a mapping after confirmation', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockListMappings.mockResolvedValue([
      { project_id: 'proj_a', jira_project_key: 'KEY', issue_type: 'Story', template: null, done_status_name: null },
    ]);
    mockDeleteMapping.mockResolvedValue(undefined);
    renderCard('enterprise');
    const user = userEvent.setup();

    await screen.findByText('Alpha');
    await user.click(screen.getByRole('button', { name: 'Remove' }));

    const dialog = await screen.findByRole('dialog', { name: 'Remove mapping?' });
    await user.click(within(dialog).getByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(mockDeleteMapping).toHaveBeenCalledWith('org_x', 'proj_a'));
  });

  it('surfaces unmapped, failure, stale, in-progress and unknown health issues', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockGetHealth.mockResolvedValue({
      unmapped_project_count: 1,
      last_push_failures: [{ ticket_id: 't_1', issue_key: 'KEY-1', failed_at: null, reason: 'boom' }],
      stale_links: [{ ticket_id: 't_2', issue_key: 'KEY-2', last_status_checked_at: null }],
      publishes_in_progress: [{ ticket_id: 't_3', started_at: null }],
      publishes_unknown: [{ ticket_id: 't_4', started_at: null }],
    });
    renderCard('enterprise');

    expect(await screen.findByText('1 unmapped project')).toBeInTheDocument();
    expect(await screen.findByText('1 push failure')).toBeInTheDocument();
    expect(screen.getByText('1 stale link')).toBeInTheDocument();
    expect(screen.getByText('1 publish in progress')).toBeInTheDocument();
    expect(screen.getByText('1 publish with an unknown result')).toBeInTheDocument();
  });
});
