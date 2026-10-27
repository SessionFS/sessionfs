import { expect, test, type Page } from '@playwright/test';
import { appendFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { mockApi } from './fixtures/mock-api';
import {
  GROUP_ID,
  LINKED_TICKET_ID,
  ORG_ID,
  PROJECT_ID,
  confluenceDriftError,
  confluenceLink,
  confluencePreview,
  jiraConnection,
  jiraHealth,
  jiraProjectPage,
  me,
  orgHelperSettingsMember,
  orgInfo,
  effectiveAssignments,
  projectGroupDetail,
  projects,
  skillRegistries,
} from './fixtures/data';

const require = createRequire(import.meta.url);
const AXE_PATH = require.resolve('axe-core/axe.min.js');

const OUT = process.env.SFS_VISUAL_OUT!;
const PORT = process.env.SFS_VISUAL_PORT!;
const BASE_URL = `http://127.0.0.1:${PORT}`;

// Obviously fake: the API is fully intercepted, nothing here is a credential.
const FAKE_KEY = 'sk_sfs_FAKE_VISUAL_TEST_KEY_0000000000';

// Project pages are routed by id (a legacy remote URL redirects to it).
const PROJECT = PROJECT_ID;

interface Scenario {
  name: string;
  path: string;
  /** Interaction to run after load and before the screenshot (e.g. open a menu). */
  prepare?: (page: Page) => Promise<void>;
  /** Extra API overrides, registered after the shared mocks so they win. */
  mock?: (page: Page) => Promise<void>;
}

const fulfillJson = (status: number, body: unknown) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
});

const PAGES: Scenario[] = [
  { name: 'sessions', path: '/' },
  { name: 'projects', path: '/projects' },
  { name: 'project-overview', path: `/projects/${PROJECT}` },
  { name: 'project-knowledge', path: `/projects/${PROJECT}?tab=knowledge` },
  { name: 'project-work', path: `/projects/${PROJECT}?tab=work` },
  { name: 'project-rules-skills', path: `/projects/${PROJECT}?tab=config` },
  { name: 'project-settings', path: `/projects/${PROJECT}?tab=settings` },
  { name: 'handoffs', path: '/handoffs' },
  { name: 'ticket-inbox', path: '/inbox' },
  { name: 'invites', path: '/invites' },
  { name: 'transfers', path: '/transfers' },
  { name: 'marketplace', path: '/marketplace' },
  { name: 'org-members', path: '/settings/organization?tab=members' },
  { name: 'org-security', path: '/settings/organization?tab=security' },
  { name: 'org-groups', path: '/settings/organization?tab=groups' },
  { name: 'org-policies', path: '/settings/organization?tab=policies' },
  { name: 'group-detail', path: `/settings/organization/groups/${GROUP_ID}?org=${ORG_ID}` },
  { name: 'settings-account', path: '/settings/account' },
  { name: 'settings-preferences', path: '/settings/preferences' },
  { name: 'settings-integrations', path: '/settings/integrations' },
  { name: 'settings-data-protection', path: '/settings/data-protection' },
  { name: 'settings-sync', path: '/settings/sync' },
  { name: 'help', path: '/help' },
  { name: 'billing', path: '/settings/billing' },
];

const THEMES = ['dark', 'light'] as const;
const WIDTHS = [375, 768, 1280];

// The shared top bar at every width the sidebar changes it, expanded and
// collapsed. The centred tagline must never run into the actions.
const assertTaglineClear = async (page: Page) => {
  const header = page.locator('header').first();
  const tagline = header.getByText('Memory Layer For AI Agents');
  const inbox = header.getByRole('button', { name: /^Inbox/ });
  const actions = await inbox.locator('xpath=..').boundingBox();
  if (!actions) throw new Error('top bar actions not rendered');
  if (!(await tagline.isVisible())) return;
  const t = await tagline.boundingBox();
  if (t && t.x + t.width > actions.x - 8) {
    throw new Error(`tagline (right=${Math.round(t.x + t.width)}) overlaps the actions (left=${Math.round(actions.x)})`);
  }
};
const SHELL_SCENARIOS: Scenario[] = [
  { name: 'shell-topbar', path: '/', prepare: assertTaglineClear },
  {
    name: 'shell-topbar-collapsed',
    path: '/',
    mock: async (page) => {
      await page.addInitScript(() => {
        try {
          localStorage.setItem('sfs-sidebar-collapsed', 'true');
        } catch {
          /* storage unavailable */
        }
      });
    },
    prepare: assertTaglineClear,
  },
];
const SHELL_WIDTHS = [375, 768, 900, 1024, 1280];

// Open-overlay scenarios, captured at a phone and a wide desktop width.
const OVERLAY_SCENARIOS: Scenario[] = [
  {
    name: 'sessions-model-filter',
    path: '/?model=gpt-5&model=retired-model-x',
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Filter by model' }).click();
      await page.getByRole('dialog', { name: 'Filter by model' }).waitFor();
    },
  },
  {
    name: 'help-tools-expanded',
    path: '/help',
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Expand all' }).click();
      await page.getByRole('button', { name: 'Collapse all' }).waitFor();
    },
  },
];
const OVERLAY_WIDTHS = [390, 1440];

// "Test connection" on the personal judge key form: a success and an error.
const judgeTestScenario = (name: string, result: Record<string, unknown>, waitFor: string): Scenario => ({
  name,
  path: '/settings/data-protection',
  mock: async (page) => {
    await page.route(
      (u) => u.pathname === '/api/v1/settings/judge',
      (route) =>
        route.fulfill(
          fulfillJson(200, { provider: 'deepseek', model: 'deepseek-v4-pro', key_set: true, base_url: null }),
        ),
    );
    await page.route(
      (u) => u.pathname === '/api/v1/settings/judge/test',
      (route) => route.fulfill(fulfillJson(200, result)),
    );
  },
  prepare: async (page) => {
    await page.getByRole('button', { name: 'Test connection' }).click();
    await page.getByText(waitFor).waitFor();
  },
});

const JUDGE_TEST_SCENARIOS: Scenario[] = [
  judgeTestScenario(
    'judge-test-success',
    {
      ok: true,
      code: 'ok',
      message: 'Connected to api.deepseek.com · deepseek-v4-pro',
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      endpoint_host: 'api.deepseek.com',
      latency_ms: 420,
    },
    'Connection works',
  ),
  judgeTestScenario(
    'judge-test-error',
    {
      ok: false,
      code: 'endpoint_not_api',
      message:
        "This address doesn't accept chat requests. platform.deepseek.com is DeepSeek's web console, not its API. Use https://api.deepseek.com, or leave Base URL empty to use DeepSeek's API.",
      provider: 'deepseek',
      model: 'deepseek-v4-pro',
      endpoint_host: 'platform.deepseek.com',
      latency_ms: 310,
    },
    "This address isn't a chat API",
  ),
];

// Assistant (session helper) settings: org admin + member, personal, gated.
const ASSISTANT_SCENARIOS: Scenario[] = [
  { name: 'assistant-org-admin', path: '/settings/organization?tab=policies' },
  {
    name: 'assistant-org-admin-errors',
    path: '/settings/organization?tab=policies',
    mock: async (page) => {
      await page.route(
        (u) => u.pathname.endsWith('/helper-settings'),
        (route) =>
          route.request().method() === 'PUT'
            ? route.fulfill(
                fulfillJson(422, {
                  error: {
                    code: '422',
                    message: 'Validation error',
                    details: {
                      errors: [
                        {
                          loc: ['body', 'settings', 'max_output_tokens'],
                          msg: 'Input should be less than or equal to 32000',
                          type: 'less_than_equal',
                        },
                      ],
                    },
                  },
                }),
              )
            : route.fallback(),
      );
    },
    prepare: async (page) => {
      const field = page.getByRole('textbox', { name: 'Max output tokens', exact: true });
      await field.fill('32000');
      await page.getByLabel('Instructions for the assistant').fill('x'.repeat(4012));
      await page.getByRole('button', { name: 'Save changes' }).click();
      await page.getByText('Use at most 4,000 characters.').waitFor();
      await page.getByLabel('Instructions for the assistant').fill('Cite the newest session first.');
      await page.getByRole('button', { name: 'Save changes' }).click();
      await page.getByText('Must be less than or equal to 32000.').waitFor();
    },
  },
  {
    name: 'assistant-org-member',
    path: '/settings/organization?tab=policies',
    mock: async (page) => {
      await page.route(
        (u) =>
          u.pathname === '/api/v1/org' || u.pathname === '/api/v1/auth/me' || u.pathname.endsWith('/helper-settings'),
        (route) => {
          const p = new URL(route.request().url()).pathname;
          if (p === '/api/v1/org') return route.fulfill(fulfillJson(200, { ...orgInfo, current_user_role: 'member' }));
          if (p === '/api/v1/auth/me') return route.fulfill(fulfillJson(200, { ...me, org_role: 'member' }));
          return route.fulfill(fulfillJson(200, orgHelperSettingsMember));
        },
      );
    },
  },
  {
    name: 'assistant-org-member-budget',
    path: '/settings/organization?tab=policies',
    mock: async (page) => {
      await page.route(
        (u) =>
          u.pathname === '/api/v1/org' || u.pathname === '/api/v1/auth/me' || u.pathname.endsWith('/helper-settings'),
        (route) => {
          const p = new URL(route.request().url()).pathname;
          if (p === '/api/v1/org') return route.fulfill(fulfillJson(200, { ...orgInfo, current_user_role: 'member' }));
          if (p === '/api/v1/auth/me') return route.fulfill(fulfillJson(200, { ...me, org_role: 'member' }));
          return route.fulfill(fulfillJson(200, { ...orgHelperSettingsMember, budget_reached: true, budget_used_pct: 100 }));
        },
      );
    },
  },
  { name: 'assistant-settings', path: '/settings/assistant' },
  {
    name: 'assistant-settings-gated',
    path: '/settings/assistant',
    mock: async (page) => {
      await page.route(
        (u) => u.pathname === '/api/v1/settings/helper',
        (route) =>
          route.fulfill(
            fulfillJson(403, {
              error: {
                code: 'upgrade_required',
                message: "This feature requires the user's plan to be enterprise or above.",
                details: { feature: 'session_helper', required_tier: 'enterprise' },
              },
            }),
          ),
      );
    },
  },
];

// Org settings → Integrations → Jira → Add mapping (GitHub #103): both
// pickers open inside the dialog, with a large Jira site and many projects.
const manyOrgProjects = Array.from({ length: 24 }, (_, i) => ({
  ...projects[0],
  id: `proj_fixture_${String(i).padStart(2, '0')}`,
  name: `${['Billing', 'Checkout', 'Identity', 'Mobile', 'Payments', 'Search'][i % 6]} service ${i + 1}`,
  repos: [],
}));
const jiraMappingMocks = async (page: Page, orgProjects: unknown[] = manyOrgProjects) => {
  await page.route(
    (u) => u.pathname === '/api/v1/projects/' || u.pathname === '/api/v1/projects',
    (route) => route.fulfill(fulfillJson(200, orgProjects)),
  );
  await page.route(
    (u) => /\/integrations\/jira\/projects$/.test(u.pathname),
    (route) => {
      const url = new URL(route.request().url());
      const limit = Number(url.searchParams.get('limit') || 50);
      return route.fulfill(fulfillJson(200, jiraProjectPage(url.searchParams.get('q') || '', limit)));
    },
  );
};
const JIRA_MAPPINGS_PATH = '/settings/organization?tab=integrations&integration=jira&section=mappings';
const openAddMapping = async (page: Page) => {
  // The Confluence card has its own "Add mapping" button on the same tab.
  await page.getByRole('region', { name: 'Jira project mappings' }).getByRole('button', { name: 'Add mapping' }).click();
  await page.getByRole('dialog', { name: 'Add mapping' }).waitFor();
};

const JIRA_MAPPING_SCENARIOS: Scenario[] = [
  {
    name: 'jira-mapping-sessionfs-picker',
    path: JIRA_MAPPINGS_PATH,
    mock: (page) => jiraMappingMocks(page),
    prepare: async (page) => {
      await openAddMapping(page);
      await page.getByRole('button', { name: 'SessionFS project' }).click();
      await page.getByRole('listbox', { name: 'SessionFS project' }).waitFor();
    },
  },
  {
    name: 'jira-mapping-jira-picker',
    path: JIRA_MAPPINGS_PATH,
    mock: (page) => jiraMappingMocks(page),
    prepare: async (page) => {
      await openAddMapping(page);
      await page.getByRole('button', { name: 'Jira project' }).click();
      await page.getByText('Showing the first 50 projects — type to search').waitFor();
    },
  },
  {
    name: 'jira-mapping-jira-search',
    path: JIRA_MAPPINGS_PATH,
    mock: (page) => jiraMappingMocks(page),
    prepare: async (page) => {
      await openAddMapping(page);
      await page.getByRole('button', { name: 'Jira project' }).click();
      await page.getByRole('combobox', { name: 'Search jira project' }).fill('ai');
      // Wait for the server page to replace the unfiltered one.
      await page.getByRole('option', { name: 'AACD · Team AACD' }).waitFor({ state: 'detached' });
      await page.keyboard.press('ArrowDown');
    },
  },
  {
    name: 'jira-mapping-no-projects',
    path: JIRA_MAPPINGS_PATH,
    mock: (page) => jiraMappingMocks(page, []),
    prepare: async (page) => {
      await openAddMapping(page);
      await page.getByRole('button', { name: 'SessionFS project' }).click();
      await page.getByText('No projects in this organization yet — create one first').waitFor();
    },
  },
];

// Organization → Integrations: the overview rows, then each section of the
// Jira and Confluence detail views. Captured at phone, tablet and desktop.
const INTEGRATIONS = '/settings/organization?tab=integrations';
const JIRA_DETAIL = `${INTEGRATIONS}&integration=jira`;
const CONFLUENCE_DETAIL = `${INTEGRATIONS}&integration=confluence`;
const INTEGRATION_SCENARIOS: Scenario[] = [
  { name: 'org-integrations', path: INTEGRATIONS },
  {
    name: 'org-integrations-not-connected',
    path: INTEGRATIONS,
    mock: async (page) => {
      await page.route(
        (u) => /\/integrations\/(jira|confluence)$/.test(u.pathname),
        (route) => route.fulfill(fulfillJson(200, { connected: false, has_token: false })),
      );
    },
  },
  { name: 'org-jira-overview', path: JIRA_DETAIL },
  { name: 'org-jira-connection', path: `${JIRA_DETAIL}&section=connection` },
  {
    name: 'org-jira-connection-edit',
    path: `${JIRA_DETAIL}&section=connection`,
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Edit connection' }).click();
      await page.getByLabel('API token').waitFor();
    },
  },
  {
    name: 'org-jira-not-connected',
    path: JIRA_DETAIL,
    mock: async (page) => {
      await page.route(
        (u) => /\/integrations\/jira$/.test(u.pathname),
        (route) => route.fulfill(fulfillJson(200, { connected: false, has_token: false })),
      );
    },
  },
  { name: 'org-jira-mappings', path: `${JIRA_DETAIL}&section=mappings` },
  { name: 'org-jira-templates', path: `${JIRA_DETAIL}&section=templates` },
  {
    name: 'org-jira-templates-builtins',
    path: `${JIRA_DETAIL}&section=templates`,
    prepare: async (page) => {
      await page.getByRole('button', { name: /built-in templates/ }).click();
      await page.getByText('Agile Standard').waitFor();
    },
  },
  { name: 'org-jira-sync', path: `${JIRA_DETAIL}&section=sync` },
  {
    name: 'org-jira-sync-off',
    path: `${JIRA_DETAIL}&section=sync`,
    mock: async (page) => {
      await page.route(
        (u) => /\/integrations\/jira$/.test(u.pathname),
        (route) => route.fulfill(fulfillJson(200, { ...jiraConnection, sync_status: false, sync_comments: 'off' })),
      );
    },
  },
  {
    name: 'org-jira-webhook-secret',
    path: `${JIRA_DETAIL}&section=sync`,
    mock: async (page) => {
      await page.route(
        (u) => /\/integrations\/jira\/webhook-secret$/.test(u.pathname),
        (route) =>
          route.fulfill(fulfillJson(200, { webhook_secret: 'whsec_FAKE_fixture_9f3a2c7d41e04b8b', webhook_secret_last4: '4b8b' })),
      );
    },
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Rotate secret' }).click();
      await page.getByText(/It will not be shown again/).waitFor();
    },
  },
  { name: 'org-jira-activity', path: `${JIRA_DETAIL}&section=activity` },
  {
    name: 'org-jira-healthy',
    path: JIRA_DETAIL,
    mock: async (page) => {
      await page.route(
        (u) => /\/integrations\/jira\/health$/.test(u.pathname),
        (route) =>
          route.fulfill(
            fulfillJson(200, { ...jiraHealth, unmapped_project_count: 0, failed_sync_events_24h: 0 }),
          ),
      );
    },
  },
  { name: 'org-confluence-overview', path: CONFLUENCE_DETAIL },
  { name: 'org-confluence-connection', path: `${CONFLUENCE_DETAIL}&section=connection` },
  { name: 'org-confluence-mappings', path: `${CONFLUENCE_DETAIL}&section=mappings` },
];
const INTEGRATION_WIDTHS = [390, 768, 1440];

// Project groups (GitHub #101): dialogs, pickers, read-only and gated states.
const GROUP_PATH = `/settings/organization/groups/${GROUP_ID}?org=${ORG_ID}`;
const groupOrgProjects = async (page: Page) => {
  await page.route(
    (u) => u.pathname === '/api/v1/projects/' || u.pathname === '/api/v1/projects',
    (route) => route.fulfill(fulfillJson(200, [projects[0], ...manyOrgProjects.map((p) => ({ ...p, org_id: ORG_ID, can_edit: true }))])),
  );
};
const GROUP_SCENARIOS: Scenario[] = [
  {
    name: 'group-create-dialog',
    path: '/settings/organization?tab=groups',
    prepare: async (page) => {
      await page.getByRole('button', { name: 'New group' }).click();
      await page.getByRole('dialog', { name: 'New project group' }).waitFor();
      await page.getByRole('textbox', { name: 'Name' }).fill('Checkout & Payments — EU');
    },
  },
  {
    name: 'group-create-conflict',
    path: '/settings/organization?tab=groups',
    mock: async (page) => {
      await page.route(
        (u) => u.pathname === `/api/v1/orgs/${ORG_ID}/groups`,
        (route) =>
          route.request().method() === 'POST'
            ? route.fulfill(
                fulfillJson(409, {
                  error: { code: '409', message: "A project group with slug 'payments-platform' already exists in this org" },
                }),
              )
            : route.fallback(),
      );
    },
    prepare: async (page) => {
      await page.getByRole('button', { name: 'New group' }).click();
      await page.getByRole('textbox', { name: 'Name' }).fill('Payments platform');
      await page.getByRole('button', { name: 'Create group' }).click();
      await page.getByText('A group with that slug already exists').waitFor();
    },
  },
  {
    name: 'org-groups-upgrade',
    path: '/settings/organization?tab=groups',
    mock: async (page) => {
      await page.route(
        (u) => u.pathname === '/api/v1/org',
        (route) => route.fulfill(fulfillJson(200, { ...orgInfo, org: { ...orgInfo.org, tier: 'pro' } })),
      );
      await page.route(
        (u) => u.pathname === `/api/v1/orgs/${ORG_ID}/groups`,
        (route) => route.fulfill(fulfillJson(200, [])),
      );
    },
  },
  {
    name: 'org-groups-member-empty',
    path: '/settings/organization?tab=groups',
    mock: async (page) => {
      await page.route(
        (u) => u.pathname === '/api/v1/org' || u.pathname === `/api/v1/orgs/${ORG_ID}/groups`,
        (route) => {
          const p = new URL(route.request().url()).pathname;
          if (p === '/api/v1/org') return route.fulfill(fulfillJson(200, { ...orgInfo, current_user_role: 'member' }));
          return route.fulfill(fulfillJson(200, []));
        },
      );
    },
  },
  {
    name: 'group-add-project-picker',
    path: GROUP_PATH,
    mock: groupOrgProjects,
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Add a project' }).click();
      await page.getByRole('listbox', { name: 'Add a project' }).waitFor();
    },
  },
  {
    name: 'group-grant-member-picker',
    path: GROUP_PATH,
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Member', exact: true }).click();
      await page.getByRole('listbox', { name: 'Member' }).waitFor();
    },
  },
  {
    name: 'group-revoke-confirm',
    path: GROUP_PATH,
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Revoke access for peer@example.test' }).click();
      await page.getByRole('dialog', { name: 'Revoke access' }).waitFor();
    },
  },
  {
    name: 'group-add-project-forbidden',
    path: GROUP_PATH,
    mock: async (page) => {
      await groupOrgProjects(page);
      await page.route(
        (u) => /\/groups\/[^/]+\/projects\/[^/]+$/.test(u.pathname),
        (route) =>
          route.fulfill(
            fulfillJson(403, { error: { code: 'project_edit_forbidden', message: 'You can read this project but not change it.' } }),
          ),
      );
    },
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Add a project' }).click();
      await page.getByRole('option').first().click();
      await page.getByRole('button', { name: 'Add project' }).click();
      await page.getByText('You can only add projects you can edit').waitFor();
    },
  },
  {
    name: 'group-detail-reader',
    path: GROUP_PATH,
    mock: async (page) => {
      await page.route(
        (u) => u.pathname === '/api/v1/org' || u.pathname === `/api/v1/orgs/${ORG_ID}/groups/${GROUP_ID}`,
        (route) => {
          const p = new URL(route.request().url()).pathname;
          if (p === '/api/v1/org') return route.fulfill(fulfillJson(200, { ...orgInfo, current_user_role: 'member' }));
          return route.fulfill(fulfillJson(200, { ...projectGroupDetail, my_role: 'reader' }));
        },
      );
      await page.route(
        (u) => u.pathname === `/api/v1/orgs/${ORG_ID}/members`,
        (route) =>
          route.fulfill(
            fulfillJson(200, { org_id: ORG_ID, members: orgInfo.members, seats_used: 3, seats_limit: 50, current_user_role: 'member' }),
          ),
      );
    },
  },
  {
    name: 'group-detail-missing',
    path: `/settings/organization/groups/pg_gone?org=${ORG_ID}`,
    mock: async (page) => {
      await page.route(
        (u) => u.pathname === `/api/v1/orgs/${ORG_ID}/groups/pg_gone`,
        (route) => route.fulfill(fulfillJson(404, { error: { code: '404', message: 'Project group not found' } })),
      );
    },
  },
  {
    name: 'group-self-downgrade',
    path: GROUP_PATH,
    mock: async (page) => {
      await page.route(
        (u) => u.pathname === '/api/v1/org' || u.pathname === `/api/v1/orgs/${ORG_ID}/members`,
        (route) => {
          const p = new URL(route.request().url()).pathname;
          if (p === '/api/v1/org') return route.fulfill(fulfillJson(200, { ...orgInfo, current_user_role: 'member' }));
          return route.fulfill(
            fulfillJson(200, { org_id: ORG_ID, members: orgInfo.members, seats_used: 3, seats_limit: 50, current_user_role: 'member' }),
          );
        },
      );
    },
    prepare: async (page) => {
      await page.getByRole('button', { name: `Role for ${me.email}` }).click();
      await page.getByRole('option', { name: 'Reader' }).click();
      await page.getByRole('dialog', { name: 'Lower your own role?' }).waitFor();
    },
  },
  {
    name: 'group-org-admin-grant',
    path: GROUP_PATH,
    mock: async (page) => {
      await page.route(
        (u) => u.pathname === `/api/v1/orgs/${ORG_ID}/groups/${GROUP_ID}`,
        (route) =>
          route.fulfill(
            fulfillJson(200, {
              ...projectGroupDetail,
              members: projectGroupDetail.members.map((m) => (m.user_id === me.user_id ? { ...m, role: 'reader' } : m)),
            }),
          ),
      );
    },
  },
  {
    name: 'org-secondary-groups',
    path: '/settings/organization?org=org_fixture_other&tab=groups',
    mock: async (page) => {
      await page.route(
        (u) => u.pathname.startsWith('/api/v1/orgs/org_fixture_other/') || u.pathname === '/api/v1/orgs',
        (route) => {
          const p = new URL(route.request().url()).pathname;
          if (p === '/api/v1/orgs')
            return route.fulfill(fulfillJson(200, { orgs: [{ org_id: 'org_fixture_other', name: 'Globex Partners', role: 'admin' }] }));
          if (p.endsWith('/members'))
            return route.fulfill(
              fulfillJson(200, { org_id: 'org_fixture_other', members: orgInfo.members, seats_used: 3, seats_limit: 10, current_user_role: 'admin' }),
            );
          if (p.endsWith('/groups'))
            return route.fulfill(fulfillJson(200, [{ id: 'pg_other', org_id: 'org_fixture_other', slug: 'partners', name: 'Partner integrations', description: null, member_count: 2, project_count: 1, my_role: 'admin' }]));
          return route.fulfill(fulfillJson(200, []));
        },
      );
    },
  },
  {
    name: 'project-settings-editor',
    path: `/projects/${PROJECT_ID}?tab=settings`,
    mock: async (page) => {
      await page.route(
        (u) => u.pathname === `/api/v1/projects/${PROJECT_ID}`,
        (route) => route.fulfill(fulfillJson(200, { ...projects[0], can_edit: true, can_admin: false, groups: [] })),
      );
    },
  },
  {
    name: 'org-restrict-confirm',
    path: '/settings/organization?tab=policies',
    prepare: async (page) => {
      await page.getByRole('switch', { name: 'Restrict project visibility' }).click();
      await page.getByRole('dialog', { name: 'Restrict project visibility?' }).waitFor();
    },
  },
  {
    name: 'org-restrict-upgrade',
    path: '/settings/organization?tab=policies',
    mock: async (page) => {
      await page.route(
        (u) => u.pathname === '/api/v1/org',
        (route) => route.fulfill(fulfillJson(200, { ...orgInfo, org: { ...orgInfo.org, tier: 'pro' } })),
      );
    },
  },
];

// Confluence (Enterprise): the org card and the project publish dialog.
const confluenceProject = async (page: Page) => {
  await page.route(
    (u) => u.pathname === `/api/v1/projects/${PROJECT_ID}`,
    (route) =>
      route.fulfill(fulfillJson(200, { ...projects[0], effective_tier: 'enterprise', can_edit: true, can_admin: true, groups: [] })),
  );
  // useMyOrgs reads `{orgs: [...]}`; membership in the project's org gates
  // the Confluence surface.
  await page.route(
    (u) => u.pathname === '/api/v1/orgs',
    (route) => route.fulfill(fulfillJson(200, { orgs: [{ org_id: ORG_ID, name: 'Acme Fixtures', role: 'admin' }] })),
  );
};
const confluencePreviewRoute = async (page: Page, body: unknown) => {
  await page.route(
    (u) => u.pathname === `/api/v1/projects/${PROJECT_ID}/confluence/preview`,
    (route) => route.fulfill(fulfillJson(200, body)),
  );
};
const CONFLUENCE_PAGE_PATH = `/projects/${PROJECT_ID}?tab=knowledge&sub=pages&page=architecture`;
const openConfluenceDialog = async (page: Page) => {
  await page.getByRole('button', { name: 'Publish to Confluence' }).click();
  await page.getByRole('dialog', { name: /Confluence/ }).getByLabel('Title').waitFor();
};
const CONFLUENCE_SCENARIOS: Scenario[] = [
  { name: 'org-confluence-card', path: '/settings/organization?tab=integrations' },
  {
    name: 'confluence-publish-dialog',
    path: CONFLUENCE_PAGE_PATH,
    mock: async (page) => {
      await confluenceProject(page);
      await confluencePreviewRoute(page, confluencePreview);
    },
    prepare: openConfluenceDialog,
  },
  {
    name: 'confluence-publish-drifted',
    path: CONFLUENCE_PAGE_PATH,
    mock: async (page) => {
      await confluenceProject(page);
      await confluencePreviewRoute(page, { ...confluencePreview, dropped: [], notes: [] });
      await page.route(
        (u) => u.pathname === `/api/v1/projects/${PROJECT_ID}/confluence/publish`,
        (route) => route.fulfill(fulfillJson(409, confluenceDriftError)),
      );
    },
    prepare: async (page) => {
      await openConfluenceDialog(page);
      await page.getByRole('button', { name: 'Publish update' }).click();
      await page.getByLabel('Changes made in Confluence').waitFor();
    },
  },
  {
    name: 'confluence-publish-stale',
    path: CONFLUENCE_PAGE_PATH,
    mock: async (page) => {
      await confluenceProject(page);
      const { existing: _existing, ...rest } = confluencePreview;
      void _existing;
      await confluencePreviewRoute(page, {
        ...rest,
        dropped: [],
        notes: [],
        // Real server shape: 200 with the link flagged stale, no page URL,
        // no `existing`.
        link: { ...confluenceLink, stale: true, page_url: null },
      });
    },
    prepare: openConfluenceDialog,
  },
];

// Ticket detail: an open ticket, and a Jira-linked one with review rounds,
// Jira activity and a mixed comment thread (list view and board drawer).
const TICKET_PATH = (id: string) => `/projects/${PROJECT_ID}?tab=work&sub=tickets&ticket=${id}`;
const enterpriseProject = confluenceProject;
const TICKET_SCENARIOS: Scenario[] = [
  { name: 'ticket-open', path: TICKET_PATH('tk_fixture2') },
  // Project → Settings grouped into sections, at the same three widths.
  { name: 'project-settings-sections', path: `/projects/${PROJECT_ID}?tab=settings` },
  { name: 'ticket-jira-linked', path: TICKET_PATH(LINKED_TICKET_ID), mock: enterpriseProject },
  {
    name: 'ticket-jira-linked-drawer',
    path: TICKET_PATH(LINKED_TICKET_ID),
    mock: async (page) => {
      await enterpriseProject(page);
      await page.addInitScript(() => {
        try {
          localStorage.setItem('sfs-tickets-view', 'board');
        } catch {
          /* storage unavailable */
        }
      });
    },
    prepare: async (page) => {
      await page.getByRole('dialog', { name: /Split ProjectDetail/ }).waitFor();
    },
  },
];

// Marketplace (calmer browse page): filters, install dialog, package detail,
// managed library with content, publish dialog, publishers and moderation.
const adminOfFixtureOrg = async (page: Page) => {
  await page.route(
    (u) => u.pathname === '/api/v1/orgs',
    (route) => route.fulfill(fulfillJson(200, { orgs: [{ org_id: ORG_ID, name: 'Acme Fixtures', role: 'admin' }] })),
  );
};
const openPackage = async (page: Page) => {
  await page.getByRole('button', { name: 'Database Migration Reviewer For Large Monorepos' }).click();
  await page.getByRole('dialog', { name: 'Database Migration Reviewer For Large Monorepos' }).waitFor();
};
const MARKETPLACE_SCENARIOS: Scenario[] = [
  {
    name: 'marketplace-filters',
    path: '/marketplace',
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Filters' }).click();
      const panel = page.getByRole('dialog', { name: 'Filters' });
      await panel.waitFor();
      await panel.getByLabel('Verified publishers only').check();
      await panel.getByLabel('Hide deprecated packages').check();
    },
  },
  {
    name: 'marketplace-filter-chips',
    path: '/marketplace',
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Filters' }).click();
      const panel = page.getByRole('dialog', { name: 'Filters' });
      await panel.getByLabel('Verified publishers only').check();
      await panel.getByLabel('Published from CI only').check();
      await panel.getByRole('button', { name: 'Done' }).click();
      await page.getByRole('button', { name: 'Testing' }).click();
      await page.getByRole('group', { name: 'Active filters' }).waitFor();
    },
  },
  {
    name: 'marketplace-install-dialog',
    path: '/marketplace',
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Install in your tool' }).click();
      await page.getByRole('dialog', { name: 'Install in your tool' }).waitFor();
    },
  },
  { name: 'marketplace-package-detail', path: '/marketplace', prepare: openPackage },
  {
    name: 'marketplace-package-install-details',
    path: '/marketplace',
    prepare: async (page) => {
      await openPackage(page);
      await page.getByRole('tab', { name: 'Install details' }).click();
      await page.getByTestId('disclosure-card').waitFor();
    },
  },
  {
    name: 'marketplace-package-compatibility',
    path: '/marketplace',
    prepare: async (page) => {
      await openPackage(page);
      await page.getByRole('tab', { name: 'Compatibility' }).click();
      await page.getByRole('heading', { name: 'Works in' }).waitFor();
    },
  },
  {
    name: 'marketplace-managed-library',
    path: '/marketplace',
    mock: async (page) => {
      await adminOfFixtureOrg(page);
      await page.route(
        (u) => u.pathname === `/api/v1/orgs/${ORG_ID}/skills/registries` || u.pathname === '/api/v1/users/me/skills/assignments',
        (route) => {
          const p = new URL(route.request().url()).pathname;
          return route.fulfill(fulfillJson(200, p.endsWith('/registries') ? skillRegistries : effectiveAssignments));
        },
      );
    },
    prepare: async (page) => {
      await page.getByRole('heading', { name: 'From your organization' }).scrollIntoViewIfNeeded();
    },
  },
  {
    name: 'marketplace-publish-dialog',
    path: '/marketplace',
    mock: adminOfFixtureOrg,
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Publish package' }).click();
      await page.getByRole('dialog', { name: 'Publish a package' }).waitFor();
    },
  },
  { name: 'marketplace-publishers', path: '/marketplace/publishers', mock: adminOfFixtureOrg },
  { name: 'marketplace-moderation', path: '/marketplace/moderation', mock: adminOfFixtureOrg },
  { name: 'marketplace-usage', path: '/marketplace/usage', mock: adminOfFixtureOrg },
  {
    name: 'marketplace-analytics',
    path: '/marketplace/publishers/pub_fixture01/analytics',
    mock: async (page) => {
      await adminOfFixtureOrg(page);
      await page.route(
        (u) => u.pathname === '/api/v1/publishers/pub_fixture01/analytics',
        (route) =>
          route.fulfill(
            fulfillJson(403, { error: { code: 'upgrade_required', message: 'Publisher analytics need the Team plan.', details: { feature: 'publisher_analytics' } } }),
          ),
      );
    },
  },
  {
    name: 'marketplace-report-detail',
    path: '/marketplace/moderation',
    mock: adminOfFixtureOrg,
    prepare: async (page) => {
      await page.getByRole('button', { name: 'Review report spr_fixture1' }).click();
      await page.getByRole('dialog', { name: /Malicious/ }).waitFor();
    },
  },
];

const RUNS: Array<{ width: number; scenarios: Scenario[] }> = [
  ...WIDTHS.map((width) => ({ width, scenarios: PAGES })),
  ...SHELL_WIDTHS.map((width) => ({ width, scenarios: SHELL_SCENARIOS })),
  ...OVERLAY_WIDTHS.map((width) => ({ width, scenarios: OVERLAY_SCENARIOS })),
  ...OVERLAY_WIDTHS.map((width) => ({ width, scenarios: ASSISTANT_SCENARIOS })),
  ...OVERLAY_WIDTHS.map((width) => ({ width, scenarios: JUDGE_TEST_SCENARIOS })),
  ...OVERLAY_WIDTHS.map((width) => ({ width, scenarios: JIRA_MAPPING_SCENARIOS })),
  ...OVERLAY_WIDTHS.map((width) => ({ width, scenarios: GROUP_SCENARIOS })),
  ...OVERLAY_WIDTHS.map((width) => ({ width, scenarios: CONFLUENCE_SCENARIOS })),
  ...OVERLAY_WIDTHS.map((width) => ({ width, scenarios: MARKETPLACE_SCENARIOS })),
  ...INTEGRATION_WIDTHS.map((width) => ({ width, scenarios: INTEGRATION_SCENARIOS })),
  ...INTEGRATION_WIDTHS.map((width) => ({ width, scenarios: TICKET_SCENARIOS })),
];

for (const theme of THEMES) {
  for (const { width, scenarios } of RUNS) {
    test.describe(`${theme} ${width}`, () => {
      test.use({ viewport: { width, height: 900 }, colorScheme: theme });

      for (const pg of scenarios) {
        test(pg.name, async ({ page }) => {
          const misses = await mockApi(page);
          if (pg.mock) await pg.mock(page);
          const errors: string[] = [];
          page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
          page.on('console', (m) => {
            if (m.type() === 'error') errors.push(`console: ${m.text()}`);
          });

          await page.addInitScript(
            ({ key, base, theme }) => {
              try {
                sessionStorage.setItem('sessionfs_auth', JSON.stringify({ apiKey: key, baseUrl: base }));
                localStorage.setItem('sfs-theme', theme);
              } catch {
                /* storage unavailable */
              }
            },
            { key: FAKE_KEY, base: BASE_URL, theme },
          );

          await page.goto(pg.path, { waitUntil: 'networkidle' });
          // Lazy chunks and react-query settle after the network goes quiet.
          await page.waitForTimeout(400);
          if (pg.prepare) {
            await pg.prepare(page);
            await page.waitForTimeout(150);
          }

          const file = `${pg.name}__${theme}__${width}.png`;
          await page.screenshot({ path: join(OUT, file), fullPage: true });
          // Requests the page made up to the capture (plus a short settle for
          // ones already in flight) are deterministic: an unmocked one fails
          // the scenario. Anything later (a poll timer, a refetch racing the
          // axe run) is only reported, so timing can't make a scenario flaky.
          await page.waitForTimeout(250);
          const capturedMisses = [...new Set(misses)];

          const metrics = await page.evaluate(() => {
            const el = document.scrollingElement!;
            const text = document.body.innerText.trim();
            return {
              scrollWidth: el.scrollWidth,
              innerWidth: window.innerWidth,
              textLength: text.length,
              hasBoundary: /something went wrong|unexpected error|failed to load/i.test(text),
              loginShown: location.pathname === '/login',
              sample: text.slice(0, 80).replace(/\s+/g, ' '),
            };
          });

          // document scrollWidth misses content clipped by an overflow-hidden ancestor, so
          // also look for visible elements whose right edge is past the viewport.
          const clipped = await page.evaluate(() => {
            const vw = window.innerWidth;
            const out: string[] = [];
            const describe = (el: Element) =>
              `${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${
                typeof el.className === 'string' && el.className
                  ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.')
                  : ''
              }`;
            const inScroller = (el: Element) => {
              for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
                const o = getComputedStyle(p).overflowX;
                if ((o === 'auto' || o === 'scroll') && p.scrollWidth > p.clientWidth) return true;
              }
              return false;
            };
            for (const el of Array.from(document.body.querySelectorAll('*'))) {
              const r = el.getBoundingClientRect();
              if (r.width === 0 || r.height === 0 || r.right <= vw + 1) continue;
              const cs = getComputedStyle(el);
              if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
              if (r.left >= vw || el.closest('[aria-hidden="true"], .sr-only')) continue;
              if (inScroller(el) || cs.pointerEvents === 'none') continue;
              out.push(`${describe(el)} right=${Math.round(r.right)}`);
            }
            return out.slice(0, 8);
          });

          let broken: string | null = null;
          if (metrics.loginShown) broken = 'redirected to /login (auth seed not honoured)';
          else if (metrics.textLength < 40) broken = `near-blank page (${metrics.textLength} chars)`;
          else if (metrics.hasBoundary) broken = `error text on page: "${metrics.sample}"`;

          await page.addScriptTag({ path: AXE_PATH });
          const axe = await page.evaluate(async () => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const res = await (window as any).axe.run(document, { resultTypes: ['violations'] });
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            return res.violations.map((v: any) => ({
              id: v.id,
              impact: v.impact,
              nodes: v.nodes.length,
              targets: v.nodes
                .slice(0, 3)
                .map((n: any) => String(n.target.join(' ')).slice(0, 120)),
              detail: v.nodes
                .slice(0, 12)
                .map((n: any) => `${String(n.target.join(' ')).slice(0, 70)} :: ${(n.any[0]?.message || n.failureSummary || '').slice(0, 150)}`),
            }));
          });

          const row = {
            page: pg.name,
            theme,
            width,
            file,
            overflowPx: Math.max(0, metrics.scrollWidth - metrics.innerWidth),
            broken,
            clipped,
            errors: [...new Set(errors)].slice(0, 5),
            unmocked: capturedMisses,
            lateUnmocked: [...new Set(misses)].filter((m) => !capturedMisses.includes(m)),
            axe,
          };
          appendFileSync(join(OUT, 'report.jsonl'), JSON.stringify(row) + '\n');
          // Layout and a11y findings are collected into the report. An
          // unmocked API call fails the scenario: the page rendered against a
          // 501 rather than fixture data, so the screenshot can't be trusted
          // and the fixtures have drifted from the client.
          expect(row.unmocked, 'API requests with no fixture in e2e/fixtures/mock-api.ts').toEqual([]);
        });
      }
    });
  }
}
