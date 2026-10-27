import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConfluenceConnection, ConfluenceHealth, ConfluenceMapping, ProjectContext } from '../api/client';
import { axe } from '../test/axe';
import { ConfluenceCard, type ConfluenceSection } from './ConfluenceCard';

/**
 * Org Settings → Integrations → Confluence (Enterprise, owner/admin). The card
 * gates on tier (non-Enterprise shows an upgrade note; the tab itself is
 * admin-gated in OrgPage). Covers tier gating, connection save, disconnect,
 * mapping CRUD, and the health panel.
 */

const mockAuth = vi.fn();
const mockListProjects = vi.fn();
const mockGetConnection = vi.fn();
const mockSaveConnection = vi.fn();
const mockDeleteConnection = vi.fn();
const mockSearchSpaces = vi.fn();
const mockListSpacePages = vi.fn();
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

/** `section` opens a detail section directly (the org page keeps it in the URL). */
function renderCard(tier: string, section?: ConfluenceSection) {
  const qc = makeQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <ConfluenceCard orgId="org_x" tier={tier} section={section} />
    </QueryClientProvider>,
  );
}

const NOT_CONNECTED: ConfluenceConnection = { connected: false, has_token: false };
const CONNECTED: ConfluenceConnection = {
  connected: true,
  has_token: true,
  site_url: 'https://acme.atlassian.net',
  account_email: 'confluence@acme.com',
  token_hint: 'abcd',
  default_template: 'reference',
  batch_window_minutes: 15,
  last_verified_at: '2026-10-01T12:00:00Z',
};

const PROJECTS: ProjectContext[] = [
  { id: 'proj_a', name: 'Alpha', git_remote_normalized: 'git@host:alpha.git', context_document: '', owner_id: 'u_1', org_id: 'org_x', created_at: '', updated_at: '' },
  { id: 'proj_b', name: 'Beta', git_remote_normalized: 'git@host:beta.git', context_document: '', owner_id: 'u_2', org_id: 'org_y', created_at: '', updated_at: '' },
];

const HEALTHY: ConfluenceHealth = {
  unmapped_project_count: 0,
  last_push_failures: [],
  publishes_in_progress: [],
  publishes_unknown: [],
  drifted_links: [],
};

const EMPTY_MAPPINGS: ConfluenceMapping[] = [];

beforeEach(() => {
  mockAuth.mockReset();
  mockListProjects.mockReset();
  mockGetConnection.mockReset();
  mockSaveConnection.mockReset();
  mockDeleteConnection.mockReset();
  mockSearchSpaces.mockReset();
  mockListSpacePages.mockReset();
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
        getConfluenceConnection: mockGetConnection,
        saveConfluenceConnection: mockSaveConnection,
        deleteConfluenceConnection: mockDeleteConnection,
        searchConfluenceSpaces: mockSearchSpaces,
        listConfluenceSpacePages: mockListSpacePages,
        listConfluenceMappings: mockListMappings,
        saveConfluenceMapping: mockSaveMapping,
        deleteConfluenceMapping: mockDeleteMapping,
        getConfluenceHealth: mockGetHealth,
      },
    },
  });

  mockListProjects.mockResolvedValue(PROJECTS);
  mockGetConnection.mockResolvedValue(NOT_CONNECTED);
  mockSearchSpaces.mockResolvedValue({
    spaces: [{ key: 'DEV', name: 'Dev Space' }],
    has_more: false,
    query: null,
    limit: 50,
  });
  mockListSpacePages.mockResolvedValue([{ id: '123', title: 'Parent Page' }]);
  mockListMappings.mockResolvedValue(EMPTY_MAPPINGS);
  mockGetHealth.mockResolvedValue(HEALTHY);
});

/** Click a custom Select/Combobox trigger by its title, then pick an option. */
async function selectOption(user: ReturnType<typeof userEvent.setup>, triggerName: string, optionName: string) {
  await user.click(screen.getByRole('button', { name: triggerName }));
  await user.click(screen.getByRole('option', { name: optionName }));
}

describe('ConfluenceCard', () => {
  it('shows the Enterprise upgrade note for non-Enterprise admins', () => {
    renderCard('team');
    expect(screen.getByText('Confluence Integration')).toBeInTheDocument();
    expect(screen.getByText(/Available on Enterprise/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /connect confluence/i })).not.toBeInTheDocument();
  });

  it('shows the not-connected state with a disabled connect button', async () => {
    renderCard('enterprise');
    expect(await screen.findByText('Not connected')).toBeInTheDocument();
    expect(screen.getByLabelText('Site URL')).toBeInTheDocument();
    expect(screen.getByLabelText('Account email')).toBeInTheDocument();
    expect(screen.getByLabelText('API token')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Default template' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect Confluence' })).toBeDisabled();
  });

  it('enables Connect only once all fields are set, and posts the payload', async () => {
    renderCard('enterprise');
    const user = userEvent.setup();
    await screen.findByText('Not connected');

    const connect = screen.getByRole('button', { name: 'Connect Confluence' });
    await user.type(screen.getByLabelText('Site URL'), 'https://acme.atlassian.net');
    await user.type(screen.getByLabelText('Account email'), 'confluence@acme.com');
    await user.type(screen.getByLabelText('API token'), 'secret');
    expect(connect).toBeDisabled();

    await selectOption(user, 'Default template', 'Runbook');
    expect(connect).toBeEnabled();

    mockSaveConnection.mockResolvedValue(CONNECTED);
    await user.click(connect);

    await waitFor(() => {
      expect(mockSaveConnection).toHaveBeenCalledWith('org_x', {
        site_url: 'https://acme.atlassian.net',
        account_email: 'confluence@acme.com',
        api_token: 'secret',
        default_template: 'runbook',
        batch_window_minutes: 15,
      });
    });
    await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'Confluence connected'));
  });

  it('renders the connected state with site, token hint, mappings and health', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    const { container } = renderCard('enterprise');
    const user = userEvent.setup();

    expect(await screen.findByText(/Connected to/)).toBeInTheDocument();
    expect(screen.getByTitle('https://acme.atlassian.net')).toBeInTheDocument();
    expect(await screen.findByText('Health')).toBeInTheDocument();
    expect(await screen.findByText(/^All good/)).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();

    await user.click(screen.getByRole('tab', { name: 'Connection' }));
    expect(screen.getByText(/token abcd/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit connection' })).toBeInTheDocument();
    expect(screen.queryByLabelText('API token')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Edit connection' }));
    expect(screen.getByLabelText('Site URL')).toHaveFocus();
    expect(screen.getByLabelText('API token')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Save connection' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByLabelText('API token')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit connection' })).toHaveFocus();

    await user.click(screen.getByRole('tab', { name: 'Mappings' }));
    expect(await screen.findByText('Project mappings')).toBeInTheDocument();
    expect(screen.getByText('No mappings yet. Map a SessionFS project to a Confluence space to enable publishing.')).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('disconnects after confirmation', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockDeleteConnection.mockResolvedValue(undefined);
    renderCard('enterprise', 'connection');
    const user = userEvent.setup();

    await screen.findByText(/Connected to/);
    await user.click(screen.getByRole('button', { name: 'Disconnect' }));

    const dialog = await screen.findByRole('dialog', { name: 'Disconnect Confluence?' });
    await user.click(within(dialog).getByRole('button', { name: 'Disconnect' }));

    await waitFor(() => expect(mockDeleteConnection).toHaveBeenCalledWith('org_x'));
    await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'Confluence disconnected'));
  });

  it('adds a mapping with space, parent page and publish flags', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockSaveMapping.mockImplementation(async (_orgId: string, _projectId: string, body: unknown) => body);
    renderCard('enterprise', 'mappings');
    const user = userEvent.setup();

    await screen.findByText(/Connected to/);
    await user.click(screen.getByRole('button', { name: 'Add mapping' }));

    const dialog = await screen.findByRole('dialog', { name: 'Add mapping' });
    await selectOption(user, 'SessionFS project', 'Alpha');
    await selectOption(user, 'Confluence space', 'DEV · Dev Space');

    // The parent-page picker loads once a space is picked.
    await waitFor(() => expect(mockListSpacePages).toHaveBeenCalledWith('org_x', 'DEV', { q: '', limit: 50 }));
    await selectOption(user, 'Parent page', 'Parent Page');

    // Toggle the auto-publish checkbox to exercise the flag in the payload.
    await user.click(within(dialog).getByLabelText(/Auto-publish pages/));

    await user.click(within(dialog).getByRole('button', { name: 'Save mapping' }));

    await waitFor(() => {
      expect(mockSaveMapping).toHaveBeenCalledWith('org_x', 'proj_a', {
        space_key: 'DEV',
        parent_page_id: '123',
        template: null,
        publish_context: false,
        auto_publish_pages: true,
      });
    });
    await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'Mapping saved for Alpha'));
  });

  it('edits an existing mapping with prefilled values', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockListMappings.mockResolvedValue([
      { project_id: 'proj_a', space_key: 'DEV', parent_page_id: '123', template: 'runbook', publish_context: true, auto_publish_pages: false },
    ]);
    mockSaveMapping.mockImplementation(async (_orgId: string, _projectId: string, body: unknown) => body);
    renderCard('enterprise', 'mappings');
    const user = userEvent.setup();

    await screen.findByText('Alpha');
    await user.click(screen.getByRole('button', { name: 'Edit' }));

    const dialog = await screen.findByRole('dialog', { name: 'Edit mapping' });
    expect(within(dialog).getByRole('button', { name: 'SessionFS project' })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Confluence space' })).toHaveTextContent('DEV');
    expect(within(dialog).getByLabelText(/Publish the compiled context document/)).toBeChecked();

    await user.click(within(dialog).getByRole('button', { name: 'Save mapping' }));

    await waitFor(() => {
      expect(mockSaveMapping).toHaveBeenCalledWith('org_x', 'proj_a', {
        space_key: 'DEV',
        parent_page_id: '123',
        template: 'runbook',
        publish_context: true,
        auto_publish_pages: false,
      });
    });
  });

  it('removes a mapping after confirmation', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockListMappings.mockResolvedValue([
      { project_id: 'proj_a', space_key: 'DEV', parent_page_id: '123', template: null, publish_context: false, auto_publish_pages: false },
    ]);
    mockDeleteMapping.mockResolvedValue(undefined);
    renderCard('enterprise', 'mappings');
    const user = userEvent.setup();

    await screen.findByText('Alpha');
    await user.click(screen.getByRole('button', { name: 'Remove' }));

    const dialog = await screen.findByRole('dialog', { name: 'Remove mapping?' });
    await user.click(within(dialog).getByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(mockDeleteMapping).toHaveBeenCalledWith('org_x', 'proj_a'));
  });

  it('surfaces unmapped, failure, drifted, in-progress and unknown health issues', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockGetHealth.mockResolvedValue({
      unmapped_project_count: 1,
      last_push_failures: [{ project_id: 'proj_a', source_kind: 'wiki_page', source_key: 'setup', failed_at: null, reason: 'boom' }],
      publishes_in_progress: [{ project_id: 'proj_a', source_kind: 'wiki_page', source_key: 'guide', started_at: null }],
      publishes_unknown: [{ project_id: 'proj_a', source_kind: 'context', source_key: 'root' }],
      drifted_links: [{ project_id: 'proj_a', source_kind: 'wiki_page', source_key: 'api', page_id: '999', stored_version: 1 }],
    });
    renderCard('enterprise');

    expect(await screen.findByText('1 unmapped project')).toBeInTheDocument();
    expect(await screen.findByText('1 push failure')).toBeInTheDocument();
    expect(screen.getByText('1 drifted page')).toBeInTheDocument();
    expect(screen.getByText('1 publish in progress')).toBeInTheDocument();
    expect(screen.getByText('1 publish with an unknown result')).toBeInTheDocument();
  });
});
