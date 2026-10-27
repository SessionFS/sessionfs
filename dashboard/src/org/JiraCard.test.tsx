import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { JiraConnection, JiraHealth, JiraMapping, ProjectContext } from '../api/client';
import { axe } from '../test/axe';
import { JiraCard, type JiraSection } from './JiraCard';

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
const mockSearchJiraProjects = vi.fn();
const mockListJiraIssueTypes = vi.fn();
const mockListMappings = vi.fn();
const mockSaveMapping = vi.fn();
const mockDeleteMapping = vi.fn();
const mockGetHealth = vi.fn();
const mockGetSyncEvents = vi.fn();
const mockListTemplates = vi.fn();
const mockRotateSecret = vi.fn();
const mockUpdateSync = vi.fn();
const addToast = vi.fn();

vi.mock('../auth/AuthContext', () => ({ useAuth: () => mockAuth() }));
vi.mock('../hooks/useToast', () => ({ useToast: () => ({ addToast }) }));

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
}

/** `section` opens a detail section directly (the org page keeps it in the URL). */
function renderCard(tier: string, section?: JiraSection) {
  const qc = makeQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <JiraCard orgId="org_x" tier={tier} section={section} />
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

/** The server's default status map ("none" = unmapped). */
const DEFAULT_STATUS_MAP = {
  transitions: {
    open: 'To Do',
    in_progress: 'In Progress',
    blocked: 'none',
    review: 'none',
    done: 'Done',
    cancelled: 'none',
    closed: 'Done',
  },
  categories: { new: 'none', indeterminate: 'none', done: 'resolve' },
};

const PROJECTS: ProjectContext[] = [
  { id: 'proj_a', name: 'Alpha', git_remote_normalized: 'git@host:alpha.git', context_document: '', owner_id: 'u_1', org_id: 'org_x', created_at: '', updated_at: '' },
  { id: 'proj_b', name: 'Beta', git_remote_normalized: 'git@host:beta.git', context_document: '', owner_id: 'u_2', org_id: 'org_y', created_at: '', updated_at: '' },
];

function orgProject(id: string, name: string, orgId: string | null = 'org_x'): ProjectContext {
  return { id, name, git_remote_normalized: `github.com/acme/${id}`, context_document: '', owner_id: 'u_1', org_id: orgId, created_at: '', updated_at: '' };
}

/** A Jira site with `count` projects; `search` mimics Jira's key/name filter. */
function jiraSite(keys: string[]) {
  return async (_orgId: string, params: { q?: string; limit?: number } = {}) => {
    const q = (params.q ?? '').toLowerCase();
    const limit = params.limit ?? 50;
    const matched = keys.filter((k) => !q || k.toLowerCase().includes(q) || `name ${k}`.toLowerCase().includes(q));
    return {
      projects: matched.slice(0, limit).map((k) => ({ key: k, name: `Name ${k}`, issue_types: [] })),
      has_more: matched.length > limit,
      query: q || null,
      limit,
    };
  };
}

async function openAddMapping(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText(/Connected to/);
  await user.click(screen.getByRole('button', { name: 'Add mapping' }));
  return screen.findByRole('dialog', { name: 'Add mapping' });
}

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
  mockSearchJiraProjects.mockReset();
  mockListJiraIssueTypes.mockReset();
  mockListMappings.mockReset();
  mockSaveMapping.mockReset();
  mockDeleteMapping.mockReset();
  mockGetHealth.mockReset();
  mockGetSyncEvents.mockReset();
  mockListTemplates.mockReset();
  mockRotateSecret.mockReset();
  mockUpdateSync.mockReset();
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
        searchJiraProjects: mockSearchJiraProjects,
        listJiraIssueTypes: mockListJiraIssueTypes,
        listJiraMappings: mockListMappings,
        saveJiraMapping: mockSaveMapping,
        deleteJiraMapping: mockDeleteMapping,
        getJiraHealth: mockGetHealth,
        getJiraSyncEvents: mockGetSyncEvents,
        listJiraTemplates: mockListTemplates,
        rotateJiraWebhookSecret: mockRotateSecret,
        updateJiraSync: mockUpdateSync,
      },
    },
  });

  mockListProjects.mockResolvedValue(PROJECTS);
  mockGetConnection.mockResolvedValue(NOT_CONNECTED);
  mockSearchJiraProjects.mockResolvedValue({
    projects: [{ key: 'KEY', name: 'Acme Project', issue_types: [] }],
    has_more: false,
    query: null,
    limit: 50,
  });
  mockListJiraIssueTypes.mockResolvedValue([{ name: 'Story', id: '10001' }, { name: 'Task', id: '10002' }]);
  mockListMappings.mockResolvedValue(EMPTY_MAPPINGS);
  mockGetHealth.mockResolvedValue(HEALTHY);
  mockGetSyncEvents.mockResolvedValue({ events: [] });
  mockListTemplates.mockResolvedValue([]);
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
    // Sync settings are PATCHed separately; the connect form never sends them.
    const body = mockSaveConnection.mock.calls[0][1];
    expect(body).not.toHaveProperty('sync_status');
    expect(body).not.toHaveProperty('sync_comments');
    expect(body).not.toHaveProperty('status_map');
    await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'Jira connected'));
  });

  it('renders the connected state with site, token hint, mappings and health', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    const { container } = renderCard('enterprise');
    const user = userEvent.setup();

    expect(await screen.findByText(/Connected to/)).toBeInTheDocument();
    expect(screen.getByTitle('https://acme.atlassian.net')).toBeInTheDocument();
    // Overview first: health leads, and a healthy org gets one quiet line.
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByText('Health')).toBeInTheDocument();
    expect(await screen.findByText(/^All good/)).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();

    // Connection is a read-only summary; the form only opens on edit.
    await user.click(screen.getByRole('tab', { name: 'Connection' }));
    expect(screen.getByText(/token abcd/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit connection' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
    expect(screen.queryByLabelText('API token')).not.toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: 'Mappings' }));
    expect(await screen.findByText('Project mappings')).toBeInTheDocument();
    expect(screen.getByText('No mappings yet. Map a SessionFS project to a Jira project to enable promotion.')).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('opens the connection form on edit, asks for the token again, and closes it after saving', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockSaveConnection.mockResolvedValue(CONNECTED);
    renderCard('enterprise', 'connection');
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Edit connection' }));
    expect(screen.getByLabelText('Site URL')).toHaveValue('https://acme.atlassian.net');
    // Focus moves into the form it opened.
    expect(screen.getByLabelText('Site URL')).toHaveFocus();
    const save = screen.getByRole('button', { name: 'Save connection' });
    expect(save).toBeDisabled();
    await user.type(screen.getByLabelText('API token'), 'new-token');
    await user.click(save);

    await waitFor(() =>
      expect(mockSaveConnection).toHaveBeenCalledWith('org_x', {
        site_url: 'https://acme.atlassian.net',
        account_email: 'jira@acme.com',
        api_token: 'new-token',
        default_template: 'agile',
      }),
    );
    expect(addToast).toHaveBeenCalledWith('success', 'Jira connection updated');
    // ...and back to the button that opened it once saved.
    expect(await screen.findByRole('button', { name: 'Edit connection' })).toHaveFocus();
  });

  it('returns focus to Edit connection on Cancel', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    renderCard('enterprise', 'connection');
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Edit connection' }));
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Edit connection' })).toHaveFocus();
  });

  it('only offers Connection and Templates before connecting', async () => {
    renderCard('enterprise');
    await screen.findByText('Not connected');
    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual(['Connection', 'Templates']);
  });

  it('links each health problem to the section that fixes it', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockGetHealth.mockResolvedValue({ ...HEALTHY, unmapped_project_count: 2, webhook_auth_failures_24h: 1 });
    renderCard('enterprise');
    const user = userEvent.setup();

    expect(await screen.findByText('2 unmapped projects')).toBeInTheDocument();
    // Zero counts are never shown.
    expect(screen.queryByText(/push failure/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Map projects' }));
    expect(screen.getByRole('tab', { name: 'Mappings' })).toHaveAttribute('aria-selected', 'true');
    // The fix action's row is gone; focus lands on the tab it opened.
    expect(screen.getByRole('tab', { name: 'Mappings' })).toHaveFocus();
    expect(await screen.findByRole('button', { name: 'Add mapping' })).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: 'Overview' }));
    await user.click(await screen.findByRole('button', { name: 'Check webhook' }));
    expect(screen.getByRole('tab', { name: 'Sync' })).toHaveAttribute('aria-selected', 'true');
  });

  it('reports the chosen section to a controlling parent', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    const onSectionChange = vi.fn();
    render(
      <QueryClientProvider client={makeQueryClient()}>
        <JiraCard orgId="org_x" tier="enterprise" section="overview" onSectionChange={onSectionChange} />
      </QueryClientProvider>,
    );
    await screen.findByText(/Connected to/);
    await userEvent.setup().click(screen.getByRole('tab', { name: 'Activity' }));
    expect(onSectionChange).toHaveBeenCalledWith('activity');
  });

  it('disconnects after confirmation', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockDeleteConnection.mockResolvedValue(undefined);
    renderCard('enterprise', 'connection');
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
    renderCard('enterprise', 'mappings');
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

  describe('SessionFS project picker (GitHub #103)', () => {
    it('lists only this org\'s unmapped projects, sorted, in a scrollable searchable list', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      const many = Array.from({ length: 25 }, (_, i) => orgProject(`proj_${i}`, `Service ${String(i).padStart(2, '0')}`));
      mockListProjects.mockResolvedValue([
        ...many,
        orgProject('proj_other', 'Other org', 'org_y'),
        orgProject('proj_personal', 'Personal', null),
      ]);
      mockListMappings.mockResolvedValue([
        { project_id: 'proj_3', jira_project_key: 'KEY', issue_type: 'Story', template: null, done_status_name: null },
      ]);
      renderCard('enterprise', 'mappings');
      const user = userEvent.setup();
      const dialog = await openAddMapping(user);

      await user.click(within(dialog).getByRole('button', { name: 'SessionFS project' }));
      const list = within(dialog).getByRole('listbox', { name: 'SessionFS project' });
      const names = within(list).getAllByRole('option').map((o) => o.textContent);
      expect(names).toHaveLength(24);
      expect(names[0]).toBe('Service 00');
      expect(names).not.toContain('Service 03');
      expect(names).not.toContain('Other org');
      expect(names).not.toContain('Personal');
      expect(list).toHaveClass('overflow-y-auto');
      expect(within(dialog).getByTestId('combobox-popup').style.maxHeight).toBe('360px');

      await user.type(within(dialog).getByRole('combobox', { name: 'Search sessionfs project' }), 'service 2');
      expect(within(dialog).getAllByRole('option').map((o) => o.textContent)).toEqual([
        'Service 20', 'Service 21', 'Service 22', 'Service 23', 'Service 24',
      ]);
      // Matching by ID works too.
      await user.clear(within(dialog).getByRole('combobox'));
      await user.type(within(dialog).getByRole('combobox'), 'proj_1');
      expect(within(dialog).getAllByRole('option')).toHaveLength(11);
      // Typing activates the first match; ArrowDown moves to the second.
      await user.keyboard('{ArrowDown}{Enter}');
      expect(within(dialog).getByRole('button', { name: 'SessionFS project' })).toHaveTextContent('Service 10');
    });

    it('explains an empty list when the org has no projects', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockListProjects.mockResolvedValue([orgProject('proj_other', 'Other org', 'org_y')]);
      renderCard('enterprise', 'mappings');
      const user = userEvent.setup();
      const dialog = await openAddMapping(user);

      await user.click(within(dialog).getByRole('button', { name: 'SessionFS project' }));
      expect(within(dialog).queryByRole('option')).not.toBeInTheDocument();
      expect(
        within(dialog).getByText('No projects in this organization yet — create one first'),
      ).toBeInTheDocument();
    });

    it('explains an empty list when every org project is already mapped', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockListMappings.mockResolvedValue([
        { project_id: 'proj_a', jira_project_key: 'KEY', issue_type: 'Story', template: null, done_status_name: null },
      ]);
      renderCard('enterprise', 'mappings');
      const user = userEvent.setup();
      await screen.findByText('Alpha');
      const dialog = await openAddMapping(user);

      await user.click(within(dialog).getByRole('button', { name: 'SessionFS project' }));
      expect(
        within(dialog).getByText('All projects in this organization are already mapped'),
      ).toBeInTheDocument();
    });

    it('Escape closes the picker but keeps the dialog open', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      renderCard('enterprise', 'mappings');
      const user = userEvent.setup();
      const dialog = await openAddMapping(user);
      await user.click(within(dialog).getByRole('button', { name: 'SessionFS project' }));
      await user.keyboard('{Escape}');
      expect(within(dialog).queryByRole('listbox')).not.toBeInTheDocument();
      expect(screen.getByRole('dialog', { name: 'Add mapping' })).toBeInTheDocument();
    });
  });

  describe('Jira project picker (GitHub #103)', () => {
    const SITE = ['AACD', 'AAI', 'AIOPS', ...Array.from({ length: 497 }, (_, i) => `B${String(i).padStart(3, '0')}`), 'ZULU'];

    it('loads the first page, says it is truncated, and searches Jira as the user types', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockSearchJiraProjects.mockImplementation(jiraSite(SITE));
      renderCard('enterprise', 'mappings');
      const user = userEvent.setup();
      const dialog = await openAddMapping(user);

      await waitFor(() => expect(mockSearchJiraProjects).toHaveBeenCalledWith('org_x', { q: '', limit: 50 }));
      await user.click(within(dialog).getByRole('button', { name: 'Jira project' }));
      expect(within(dialog).getAllByRole('option')).toHaveLength(50);
      expect(within(dialog).getByText('Showing the first 50 projects — type to search')).toBeInTheDocument();

      // A project far past the first page is reachable through search.
      await user.type(within(dialog).getByRole('combobox', { name: 'Search jira project' }), 'zul');
      await waitFor(() => expect(mockSearchJiraProjects).toHaveBeenLastCalledWith('org_x', { q: 'zul', limit: 50 }));
      // Debounced: no request per keystroke.
      expect(mockSearchJiraProjects).not.toHaveBeenCalledWith('org_x', { q: 'z', limit: 50 });
      const option = await within(dialog).findByRole('option', { name: 'ZULU · Name ZULU' });
      expect(within(dialog).getAllByRole('option')).toHaveLength(1);
      await user.click(option);
      expect(within(dialog).getByRole('button', { name: 'Jira project' })).toHaveTextContent('ZULU · Name ZULU');
      await waitFor(() => expect(mockListJiraIssueTypes).toHaveBeenCalledWith('org_x', 'ZULU'));
    });

    it('does not pick a stale result when Enter is pressed before the search answers', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockSearchJiraProjects.mockImplementation(jiraSite(SITE));
      renderCard('enterprise', 'mappings');
      const user = userEvent.setup();
      const dialog = await openAddMapping(user);
      const trigger = within(dialog).getByRole('button', { name: 'Jira project' });

      await user.click(trigger);
      await within(dialog).findByRole('option', { name: 'AACD · Name AACD' });
      // Still inside the debounce window: the list shows the unfiltered page.
      await user.type(within(dialog).getByRole('combobox', { name: 'Search jira project' }), 'AIOPS{Enter}');
      expect(trigger).toHaveTextContent('Select a Jira project');
      expect(within(dialog).getByRole('listbox')).toHaveAttribute('aria-busy', 'true');

      // Once the AIOPS results land, Enter picks the first of them.
      await waitFor(() => expect(within(dialog).getAllByRole('option')).toHaveLength(1));
      await waitFor(() => expect(within(dialog).getByRole('listbox')).not.toHaveAttribute('aria-busy'));
      await user.keyboard('{Enter}');
      expect(trigger).toHaveTextContent('AIOPS · Name AIOPS');
    });

    it('says when a search matches nothing', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockSearchJiraProjects.mockImplementation(jiraSite(SITE));
      renderCard('enterprise', 'mappings');
      const user = userEvent.setup();
      const dialog = await openAddMapping(user);

      await user.click(within(dialog).getByRole('button', { name: 'Jira project' }));
      await user.type(within(dialog).getByRole('combobox', { name: 'Search jira project' }), 'nope');
      expect(await within(dialog).findByText('No Jira projects match “nope”')).toBeInTheDocument();
      expect(within(dialog).queryByRole('option')).not.toBeInTheDocument();
    });

    it('shows a loading state while the search is in flight', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockSearchJiraProjects.mockImplementation(() => new Promise(() => {}));
      renderCard('enterprise', 'mappings');
      const user = userEvent.setup();
      const dialog = await openAddMapping(user);

      await user.click(within(dialog).getByRole('button', { name: 'Jira project' }));
      expect(within(dialog).getByText('Searching…')).toBeInTheDocument();
    });

    it('selects a Jira project with the keyboard and saves the mapping', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockSearchJiraProjects.mockImplementation(jiraSite(SITE));
      mockSaveMapping.mockImplementation(async (_orgId: string, _projectId: string, body: unknown) => body);
      renderCard('enterprise', 'mappings');
      const user = userEvent.setup();
      const dialog = await openAddMapping(user);

      await selectOption(user, 'SessionFS project', 'Alpha');
      within(dialog).getByRole('button', { name: 'Jira project' }).focus();
      await user.keyboard('{ArrowDown}');
      await user.type(within(dialog).getByRole('combobox', { name: 'Search jira project' }), 'aiops');
      await within(dialog).findByRole('option', { name: 'AIOPS · Name AIOPS' });
      await waitFor(() => expect(within(dialog).getAllByRole('option')).toHaveLength(1));
      await user.keyboard('{Enter}');

      const issueTypeTrigger = within(dialog).getByRole('button', { name: 'Issue type' });
      await waitFor(() => expect(issueTypeTrigger).toBeEnabled());
      await user.click(within(dialog).getByRole('button', { name: 'Save mapping' }));
      await waitFor(() => {
        expect(mockSaveMapping).toHaveBeenCalledWith('org_x', 'proj_a', {
          jira_project_key: 'AIOPS',
          issue_type: 'Story',
          template: null,
          done_status_name: null,
        });
      });
    });

    it('has no axe violations with the picker open', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockSearchJiraProjects.mockImplementation(jiraSite(SITE));
      const { container } = renderCard('enterprise', 'mappings');
      const user = userEvent.setup();
      const dialog = await openAddMapping(user);
      await user.click(within(dialog).getByRole('button', { name: 'Jira project' }));
      await within(dialog).findAllByRole('option');
      expect(await axe(container)).toHaveNoViolations();
    });
  });

  it('edits an existing mapping with prefilled values', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockListMappings.mockResolvedValue([
      { project_id: 'proj_a', jira_project_key: 'KEY', issue_type: 'Story', template: null, done_status_name: 'Done' },
    ]);
    renderCard('enterprise', 'mappings');
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
    renderCard('enterprise', 'mappings');
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
    renderCard('enterprise', 'mappings');
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
    renderCard('enterprise', 'mappings');
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

  it('keeps a mapping on an archived template selectable and saves it unchanged', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockListTemplates.mockResolvedValue([
      {
        slug: 'old-tpl', name: 'Old', description: null, sections: [], version: 2, builtin: false,
        archived_at: '2026-10-01T00:00:00Z', created_at: null, updated_at: null,
        created_by_user_id: null, updated_by_user_id: null,
      },
    ]);
    mockListMappings.mockResolvedValue([
      { project_id: 'proj_a', jira_project_key: 'KEY', issue_type: 'Story', template: 'old-tpl', done_status_name: null },
    ]);
    renderCard('enterprise', 'mappings');
    const user = userEvent.setup();

    expect(await screen.findByText('Old (archived)')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Edit' }));
    const dialog = await screen.findByRole('dialog', { name: 'Edit mapping' });
    expect(within(dialog).getByRole('button', { name: /Template override/ })).toHaveTextContent('Old (archived)');

    await user.click(within(dialog).getByRole('button', { name: 'Save mapping' }));
    await waitFor(() => {
      expect(mockSaveMapping).toHaveBeenCalledWith('org_x', 'proj_a', expect.objectContaining({ template: 'old-tpl' }));
    });
  });

  it('warns about mappings, the default and links on archived templates', async () => {
    mockGetConnection.mockResolvedValue(CONNECTED);
    mockGetHealth.mockResolvedValue({
      ...HEALTHY,
      connection_default_archived: true,
      mappings_on_archived_templates: [{ project_id: 'proj_a', template_slug: 'old-tpl' }],
      archived_template_references: [{ ticket_id: 't_9', template_slug: 'old-tpl', template_version: 2 }],
    });
    renderCard('enterprise');

    expect(await screen.findByText('On archived templates')).toBeInTheDocument();
    expect(screen.getByText('The connection default template')).toBeInTheDocument();
    expect(screen.getByText('t_9')).toBeInTheDocument();
  });

  describe('Two-way sync (GitHub #98 part B)', () => {
    it('explains verdict forwarding and that a personal Jira account still syncs back', async () => {
      mockGetConnection.mockResolvedValue({ ...CONNECTED, sync_status: true, sync_comments: 'both' });
      const { unmount } = renderCard('enterprise', 'sync');

      expect(await screen.findByText(/Connected to/)).toBeInTheDocument();
      expect(screen.getByText(/VERIFIED-CLEAN verdict automatically/)).toBeInTheDocument();
      unmount();

      // The account advice sits on the connection form.
      renderCard('enterprise', 'connection');
      await userEvent.setup().click(await screen.findByRole('button', { name: 'Edit connection' }));
      expect(screen.getByText(/dedicated Jira integration account is recommended/)).toBeInTheDocument();
      expect(screen.getByText(/A personal account works too/)).toBeInTheDocument();
    });

    it('keeps the status map editable while two-way status sync is off, and says when it applies', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      renderCard('enterprise', 'sync');
      const user = userEvent.setup();

      await screen.findByText(/Connected to/);
      expect(screen.getByText(/Changes to the map are saved now and used once sync is on/)).toBeInTheDocument();
      expect(screen.getByLabelText('Open')).toBeEnabled();
      expect(screen.getByRole('button', { name: 'Jira “Done” category' })).toBeEnabled();

      await user.click(screen.getByRole('switch', { name: 'Two-way status sync' }));
      expect(screen.queryByText(/saved now and used once sync is on/)).not.toBeInTheDocument();
    });

    it('prefills the sync controls from the stored connection, showing "none" as Not synced', async () => {
      mockGetConnection.mockResolvedValue({
        ...CONNECTED,
        sync_status: true,
        sync_comments: 'both',
        status_map: {
          transitions: { ...DEFAULT_STATUS_MAP.transitions, done: 'Complete' },
          categories: { ...DEFAULT_STATUS_MAP.categories, done: 'start' },
        },
      });
      renderCard('enterprise', 'sync');

      expect(await screen.findByText(/Connected to/)).toBeInTheDocument();
      expect(screen.getByRole('switch', { name: 'Two-way status sync' })).toHaveAttribute(
        'aria-checked',
        'true',
      );
      expect(screen.getByRole('button', { name: 'Comment sync' })).toHaveTextContent('Both directions');
      expect(screen.getByLabelText('Done')).toHaveValue('Complete');
      expect(screen.getByLabelText('Open')).toHaveValue('To Do');
      // "none" renders as an empty field with a Not synced placeholder.
      expect(screen.getByLabelText('Blocked')).toHaveValue('');
      expect(screen.getByLabelText('Blocked')).toHaveAttribute('placeholder', 'Not synced');
      expect(screen.getByRole('button', { name: 'Jira “Done” category' })).toHaveTextContent('Start the ticket');
      expect(screen.getByRole('button', { name: 'Jira “To Do” category' })).toHaveTextContent('Not synced');
      // Nothing changed yet, so there is nothing to save.
      expect(screen.getByRole('button', { name: 'Save sync settings' })).toBeDisabled();
    });

    it('toggles sync with PATCH, sending only the changed fields and never the token', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockUpdateSync.mockResolvedValue({ ...CONNECTED, sync_status: true, sync_comments: 'outbound' });
      renderCard('enterprise', 'sync');
      const user = userEvent.setup();

      await screen.findByText(/Connected to/);
      await user.click(screen.getByRole('switch', { name: 'Two-way status sync' }));
      await selectOption(user, 'Comment sync', 'Outbound only');
      await user.click(screen.getByRole('button', { name: 'Save sync settings' }));

      await waitFor(() => {
        expect(mockUpdateSync).toHaveBeenCalledWith('org_x', {
          sync_status: true,
          sync_comments: 'outbound',
        });
      });
      expect(mockSaveConnection).not.toHaveBeenCalled();
      await waitFor(() => expect(addToast).toHaveBeenCalledWith('success', 'Jira sync settings saved'));
    });

    it('round-trips "none" for a cleared transition and a Not synced category', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockUpdateSync.mockResolvedValue(CONNECTED);
      renderCard('enterprise', 'sync');
      const user = userEvent.setup();

      await screen.findByText(/Connected to/);
      // Clear a mapped transition, map a previously-unmapped one, and set the
      // Done category to Not synced.
      await user.clear(screen.getByLabelText('Open'));
      await user.type(screen.getByLabelText('Review'), 'Submit for Review');
      await selectOption(user, 'Jira “Done” category', 'Not synced');
      await user.click(screen.getByRole('button', { name: 'Save sync settings' }));

      await waitFor(() => {
        expect(mockUpdateSync).toHaveBeenCalledWith('org_x', {
          status_map: {
            transitions: { ...DEFAULT_STATUS_MAP.transitions, open: 'none', review: 'Submit for Review' },
            categories: { ...DEFAULT_STATUS_MAP.categories, done: 'none' },
          },
        });
      });
    });

    it('treats a whitespace-only transition as "none"', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockUpdateSync.mockResolvedValue(CONNECTED);
      renderCard('enterprise', 'sync');
      const user = userEvent.setup();

      await screen.findByText(/Connected to/);
      await user.clear(screen.getByLabelText('Closed'));
      await user.type(screen.getByLabelText('Closed'), '   ');
      await user.click(screen.getByRole('button', { name: 'Save sync settings' }));

      await waitFor(() => expect(mockUpdateSync).toHaveBeenCalled());
      expect(mockUpdateSync.mock.calls[0][1].status_map.transitions.closed).toBe('none');
    });

    it('shows the PATCH error and keeps the edits', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockUpdateSync.mockRejectedValue(new Error('Jira connection not found'));
      renderCard('enterprise', 'sync');
      const user = userEvent.setup();

      await screen.findByText(/Connected to/);
      await user.click(screen.getByRole('switch', { name: 'Two-way status sync' }));
      await user.click(screen.getByRole('button', { name: 'Save sync settings' }));

      expect(await screen.findByText('Jira connection not found')).toBeInTheDocument();
      expect(screen.getByRole('switch', { name: 'Two-way status sync' })).toHaveAttribute('aria-checked', 'true');
    });

    it('has no axe violations in the sync section', async () => {
      mockGetConnection.mockResolvedValue({ ...CONNECTED, sync_status: true, sync_comments: 'both' });
      renderCard('enterprise', 'sync');

      await screen.findByText(/Connected to/);
      const section = screen.getByRole('region', { name: 'Two-way sync' });
      expect(await axe(section)).toHaveNoViolations();
    });

    it('generates a webhook secret and shows it once', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockRotateSecret.mockResolvedValue({ webhook_secret: 'whsec_s3cret', webhook_secret_last4: 'cret' });
      renderCard('enterprise', 'sync');
      const user = userEvent.setup();

      await screen.findByText(/Connected to/);
      expect(screen.getByText('No webhook secret yet')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'Generate secret' }));

      await waitFor(() => expect(mockRotateSecret).toHaveBeenCalledWith('org_x'));
      expect(await screen.findByText('whsec_s3cret')).toBeInTheDocument();
      expect(addToast).toHaveBeenCalledWith('success', 'Webhook secret generated');
    });

    it('rotates an existing secret and labels the action accordingly', async () => {
      mockGetConnection.mockResolvedValue({
        ...CONNECTED,
        has_webhook_secret: true,
        webhook_secret_last4: 'old4',
      });
      mockRotateSecret.mockResolvedValue({ webhook_secret: 'whsec_new', webhook_secret_last4: 'new4' });
      renderCard('enterprise', 'sync');
      const user = userEvent.setup();

      await screen.findByText(/Connected to/);
      expect(screen.getByRole('button', { name: 'Rotate secret' })).toBeInTheDocument();
      expect(screen.getByTitle('old4')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: 'Rotate secret' }));

      await waitFor(() => expect(mockRotateSecret).toHaveBeenCalledWith('org_x'));
      expect(await screen.findByText('whsec_new')).toBeInTheDocument();
      expect(addToast).toHaveBeenCalledWith('success', 'Webhook secret rotated');
    });

    it('shows the canonical inbound webhook URL', async () => {
      mockGetConnection.mockResolvedValue({
        ...CONNECTED,
        webhook_url: 'http://test.api/api/v1/webhooks/jira/org_x',
      });
      renderCard('enterprise', 'sync');

      expect(await screen.findByText(/Connected to/)).toBeInTheDocument();
      expect(screen.getByText('http://test.api/api/v1/webhooks/jira/org_x')).toBeInTheDocument();
      // The secret is stored encrypted (not hashed); only the last 4 chars show.
      expect(screen.getByText(/stored encrypted so the server can verify/)).toBeInTheDocument();
      expect(screen.queryByText(/stores only a hash/)).toBeNull();
    });

    it('lists recent sync events with direction, issue key and error state', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockGetSyncEvents.mockResolvedValue({
        events: [
          {
            id: 'tse_1',
            ticket_id: 'tk_1',
            issue_key: 'KEY-1',
            direction: 'inbound',
            kind: 'comment',
            payload: {},
            jira_event_id: 'comment:c1',
            applied: true,
            error: null,
            created_at: '2026-10-25T12:00:00Z',
          },
          {
            id: 'tse_2',
            ticket_id: 'tk_2',
            issue_key: 'KEY-2',
            direction: 'outbound',
            kind: 'status',
            payload: { to_status: 'done' },
            jira_event_id: null,
            applied: false,
            error: 'no Jira transition mapped',
            created_at: '2026-10-25T11:00:00Z',
          },
        ],
      });
      renderCard('enterprise', 'activity');

      expect(await screen.findByText('Recent sync events')).toBeInTheDocument();
      expect(await screen.findByText('Jira comment mirrored')).toBeInTheDocument();
      expect(screen.getByText('KEY-1')).toBeInTheDocument();
      // Labels come from the payload, without raw status keys.
      expect(screen.getByText('Status pushed: Done')).toBeInTheDocument();
      expect(screen.getByText('KEY-2')).toBeInTheDocument();
      expect(screen.getByText('failed')).toBeInTheDocument();
    });

    it('shows queued, retrying and gave-up delivery states', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      const base = {
        ticket_id: 'tk_1',
        issue_key: 'KEY-1',
        direction: 'outbound' as const,
        payload: {},
        jira_event_id: null,
        created_at: '2026-10-25T12:00:00Z',
        updated_at: '2026-10-25T12:00:00Z',
      };
      mockGetSyncEvents.mockResolvedValue({
        events: [
          { ...base, id: 'e1', kind: 'comment', applied: false, error: null, state: 'pending', retry_count: 0, next_attempt_at: null },
          { ...base, id: 'e2', kind: 'status', applied: false, error: 'HTTP 503', state: 'pending', retry_count: 2, next_attempt_at: '2026-10-25T12:05:00Z' },
          { ...base, id: 'e3', kind: 'status', applied: false, error: 'HTTP 400', state: 'exhausted', retry_count: 5, next_attempt_at: null },
          { ...base, id: 'e4', kind: 'comment', applied: true, error: null, state: 'done', retry_count: 0, next_attempt_at: null },
        ],
      });
      renderCard('enterprise', 'activity');

      expect(await screen.findByText('queued')).toBeInTheDocument();
      expect(screen.getByText('retrying (attempt 3)')).toBeInTheDocument();
      expect(screen.getByText('gave up')).toBeInTheDocument();
      expect(screen.getByText('HTTP 400')).toBeInTheDocument();
      const section = screen.getByRole('region', { name: 'Recent Jira sync events' });
      expect(await axe(section)).toHaveNoViolations();
    });

    it('surfaces sync failures and timestamps in the health panel', async () => {
      mockGetConnection.mockResolvedValue(CONNECTED);
      mockGetHealth.mockResolvedValue({
        ...HEALTHY,
        failed_sync_events_24h: 2,
        webhook_auth_failures_24h: 1,
        last_inbound_at: '2026-10-25T12:00:00Z',
        last_synced_at: '2026-10-25T11:30:00Z',
      });
      renderCard('enterprise');

      expect(await screen.findByText('Health')).toBeInTheDocument();
      expect(await screen.findByText('2 failed or refused sync events in the last 24 hours')).toBeInTheDocument();
      expect(screen.getByText('1 rejected webhook delivery in the last 24 hours')).toBeInTheDocument();
      expect(screen.getByText('Last inbound Jira event')).toBeInTheDocument();
      expect(screen.getByText('Last outbound sync')).toBeInTheDocument();
    });
  });
});
