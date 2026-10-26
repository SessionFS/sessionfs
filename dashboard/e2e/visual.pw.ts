import { test, type Page } from '@playwright/test';
import { appendFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { mockApi } from './fixtures/mock-api';
import { PROJECT_ID, me, orgHelperSettingsMember, orgInfo } from './fixtures/data';

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
  { name: 'marketplace', path: '/marketplace' },
  { name: 'org-members', path: '/settings/organization?tab=members' },
  { name: 'org-security', path: '/settings/organization?tab=security' },
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

const RUNS: Array<{ width: number; scenarios: Scenario[] }> = [
  ...WIDTHS.map((width) => ({ width, scenarios: PAGES })),
  ...OVERLAY_WIDTHS.map((width) => ({ width, scenarios: OVERLAY_SCENARIOS })),
  ...OVERLAY_WIDTHS.map((width) => ({ width, scenarios: ASSISTANT_SCENARIOS })),
  ...OVERLAY_WIDTHS.map((width) => ({ width, scenarios: JUDGE_TEST_SCENARIOS })),
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
            unmocked: [...new Set(misses)],
            axe,
          };
          appendFileSync(join(OUT, 'report.jsonl'), JSON.stringify(row) + '\n');
          // Findings are collected into the report; they never fail the run.
        });
      }
    });
  }
}
