import { test, type Page } from '@playwright/test';
import { appendFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { mockApi } from './fixtures/mock-api';
import { PROJECT_ID, me, sessions } from './fixtures/data';

/**
 * Visual scenarios for the session-helper drawer. The helper API is mocked on
 * top of the shared fixtures: status, conversation list/get, and the SSE turn
 * stream (a complete stream via route.fulfill; a mid-answer stream by patching
 * fetch so the body stays open). Screenshots are viewport-sized because the
 * drawer is fixed over the page.
 */

const require = createRequire(import.meta.url);
const AXE_PATH = require.resolve('axe-core/axe.min.js');

const OUT = process.env.SFS_VISUAL_OUT!;
const PORT = process.env.SFS_VISUAL_PORT!;
const BASE_URL = `http://127.0.0.1:${PORT}`;
const FAKE_KEY = 'sk_sfs_FAKE_VISUAL_TEST_KEY_0000000000';

const NOW = Date.parse('2026-10-25T12:00:00Z');
const ago = (mins: number) => new Date(NOW - mins * 60_000).toISOString();

const conversations = [
  { id: 'hc_1', org_id: 'org_fixture01', title: 'Why did the billing webhook retry twice?', created_at: ago(30), updated_at: ago(12) },
  { id: 'hc_2', org_id: 'org_fixture01', title: 'Summarize last week on the sync queue', created_at: ago(60 * 26), updated_at: ago(60 * 26) },
  { id: 'hc_3', org_id: null, title: 'Where did we decide on Redis vs Postgres advisory locks for the importer?', created_at: ago(60 * 24 * 4), updated_at: ago(60 * 24 * 4) },
  { id: 'hc_4', org_id: null, title: null, created_at: ago(60 * 24 * 12), updated_at: ago(60 * 24 * 12) },
];

const S1 = sessions[0];
const S2 = sessions[1];

const ANSWER = [
  'The duplicate retry came from the **webhook handler acknowledging late** — Stripe re-sent the event before the first attempt committed [session:' + S1.id + ' msg:42].',
  '',
  'What the team changed:',
  '',
  '1. Persist the event id in `stripe_events` *before* doing work, and skip ids already seen [session:' + S1.id + ' msg:57].',
  '2. Move the email side-effect behind the commit so a retry can no longer send twice.',
  '3. Added a regression test that replays the same event concurrently [session:' + S2.id + '].',
  '',
  '```python',
  'if await seen(event.id):',
  '    return Response(status_code=200)',
  'await record(event.id)',
  '```',
  '',
  '| Attempt | Result |',
  '|---|---|',
  '| 1 | committed after 31 s |',
  '| 2 | duplicate email sent |',
].join('\n');

function sse(frames: Array<[string, unknown]>): string {
  return frames.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join('');
}

const TOOL_FRAMES: Array<[string, unknown]> = [
  ['tool_call', { id: 'tc_1', name: 'search_sessions', args: { query: 'billing webhook retry' } }],
  ['tool_result', { id: 'tc_1', name: 'search_sessions', ok: true, item_count: 6, session_ids: [S1.id, S2.id], message_indexes: [] }],
  ['tool_call', { id: 'tc_2', name: 'get_session_excerpts', args: { session_id: S1.id, query: 'retry' } }],
  ['tool_result', { id: 'tc_2', name: 'get_session_excerpts', ok: true, item_count: 1, session_ids: [S1.id], message_indexes: [42, 57] }],
];

const ANSWER_FRAMES: Array<[string, unknown]> = [
  ['conversation', { conversation_id: 'hc_new' }],
  ...TOOL_FRAMES,
  ...ANSWER.match(/[\s\S]{1,40}/g)!.map((delta) => ['text', { delta }] as [string, unknown]),
  ['citation', { session_id: S1.id, message_index: 42, title: S1.title }],
  ['citation', { session_id: S1.id, message_index: 57, title: S1.title }],
  ['citation', { session_id: S2.id, message_index: null, title: S2.title }],
  ['done', { stop_reason: 'stop', input_tokens: 1200, output_tokens: 260, key_source: 'user' }],
];

const STREAM_FRAMES: Array<[string, unknown]> = [
  ['conversation', { conversation_id: 'hc_new' }],
  ...TOOL_FRAMES.slice(0, 3),
];

type Scenario = {
  name: string;
  status?: { enabled: boolean; key_source: string | null; provider: string | null; model: string | null };
  meRole?: string;
  turn?: 'answer' | 'busy' | 'stream';
  ask?: string;
  openHistory?: boolean;
  openScope?: boolean;
  scrollUp?: boolean;
};

const OK_STATUS = { enabled: true, key_source: 'user', provider: 'anthropic', model: 'claude-sonnet-4' };

const SCENARIOS: Scenario[] = [
  { name: 'helper-empty' },
  { name: 'helper-answer', turn: 'answer', ask: 'Why did the billing webhook retry twice?' },
  { name: 'helper-streaming', turn: 'stream', ask: 'Why did the billing webhook retry twice?' },
  { name: 'helper-history', openHistory: true },
  { name: 'helper-no-key', status: { enabled: true, key_source: null, provider: null, model: null }, meRole: 'member' },
  { name: 'helper-not-entitled', status: { enabled: false, key_source: null, provider: null, model: null } },
  { name: 'helper-busy', turn: 'busy', ask: 'Summarize this project' },
  { name: 'helper-scrolled-up', turn: 'answer', ask: 'Why did the billing webhook retry twice?', scrollUp: true },
  { name: 'helper-scope-menu', turn: 'answer', ask: 'Why did the billing webhook retry twice?', openScope: true },
];

async function installHelperMocks(page: Page, sc: Scenario) {
  const status = sc.status ?? OK_STATUS;
  await page.route(
    (u) => u.pathname.startsWith('/api/v1/helper/') || u.pathname === '/api/v1/auth/me',
    async (route) => {
      const url = new URL(route.request().url());
      const p = url.pathname;
      const json = (status: number, body: unknown) =>
        route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (p === '/api/v1/auth/me') return json(200, { ...me, org_role: sc.meRole ?? me.org_role });
      if (p === '/api/v1/helper/status') return json(200, status);
      if (p === '/api/v1/helper/conversations') return json(200, conversations);
      if (p.startsWith('/api/v1/helper/conversations/')) {
        return json(200, {
          ...conversations[0],
          scope: { project_id: PROJECT_ID, session_ids: [] },
          messages: [
            { id: 'm1', role: 'user', content: conversations[0].title, citations: null },
            { id: 'm2', role: 'assistant', content: ANSWER, citations: [{ session_id: S1.id, message_index: 42, title: S1.title }] },
          ],
        });
      }
      if (p === '/api/v1/helper/turns') {
        if (sc.turn === 'busy') {
          return json(409, {
            detail: {
              code: 'helper_busy',
              error: 'helper_busy',
              message: 'You already have the maximum number of helper turns running.',
            },
          });
        }
        return route.fulfill({
          status: 200,
          headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-store' },
          body: sse(ANSWER_FRAMES),
        });
      }
      return json(404, { detail: 'not found' });
    },
  );

  if (sc.turn === 'stream') {
    // Keep the body open after the queued frames so the drawer stays mid-answer.
    await page.addInitScript(
      ({ queued, partial }) => {
        const orig = window.fetch.bind(window);
        window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
          const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
          if (!url.includes('/api/v1/helper/turns')) return orig(input, init);
          const enc = new TextEncoder();
          const body = new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(enc.encode(queued));
              setTimeout(() => controller.enqueue(enc.encode(partial)), 150);
            },
          });
          return Promise.resolve(new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } }));
        };
      },
      {
        queued: sse(STREAM_FRAMES),
        partial: sse([['text', { delta: 'The duplicate retry came from the **webhook handler acknowledging late** — Stripe re-sent the event before' }]]),
      },
    );
  }
}

async function openDrawer(page: Page) {
  const btn = page.getByRole('button', { name: 'Ask the helper' }).last();
  await btn.click();
  await page.locator('[aria-labelledby="helper-panel-title"]').waitFor();
  await page.waitForTimeout(350);
}

async function ask(page: Page, text: string) {
  const box = page.getByLabel('Ask the session helper');
  await box.fill(text);
  await box.press('Enter');
}

const THEMES = ['dark', 'light'] as const;
const WIDTHS: Array<[number, number]> = [
  [1440, 900],
  [834, 1112],
  [390, 844],
];

for (const theme of THEMES) {
  for (const [width, height] of WIDTHS) {
    test.describe(`helper ${theme} ${width}`, () => {
      test.use({ viewport: { width, height }, colorScheme: theme });

      for (const sc of SCENARIOS) {
        test(sc.name, async ({ page }) => {
          await mockApi(page);
          await installHelperMocks(page, sc);
          const errors: string[] = [];
          page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));

          await page.addInitScript(
            ({ key, base, theme, now }) => {
              try {
                sessionStorage.setItem('sessionfs_auth', JSON.stringify({ apiKey: key, baseUrl: base }));
                localStorage.setItem('sfs-theme', theme);
              } catch {
                /* storage unavailable */
              }
              // Freeze "now" so relative times in the history list are stable.
              const RealDate = Date;
              const fixed = now;
              class FrozenDate extends RealDate {
                constructor(...args: unknown[]) {
                  if (args.length === 0) super(fixed);
                  else super(...(args as [string]));
                }
                static now() {
                  return fixed;
                }
              }
              (window as unknown as { Date: DateConstructor }).Date = FrozenDate as unknown as DateConstructor;
            },
            { key: FAKE_KEY, base: BASE_URL, theme, now: NOW },
          );

          await page.goto(`/projects/${PROJECT_ID}`, { waitUntil: 'networkidle' });
          await page.waitForTimeout(300);
          await openDrawer(page);

          if (sc.openHistory) {
            const historyBtn = page.getByRole('button', { name: /history|conversations/i }).first();
            if (await historyBtn.count()) await historyBtn.click();
            await page.waitForTimeout(250);
          }
          if (sc.ask) {
            await ask(page, sc.ask);
            await page.waitForTimeout(sc.turn === 'stream' ? 700 : 900);
          }
          if (sc.scrollUp) {
            await page.locator('[aria-label="Conversation"]').evaluate((el) => {
              const scroller = el.parentElement!;
              scroller.scrollTop = 0;
              scroller.dispatchEvent(new Event('scroll'));
            });
            await page.waitForTimeout(250);
          }
          if (sc.openScope) {
            const scopeBtn = page.getByRole('button', { name: /change scope/i });
            if (await scopeBtn.count()) await scopeBtn.click();
            await page.waitForTimeout(200);
          }

          const file = `${sc.name}__${theme}__${width}.png`;
          await page.screenshot({ path: join(OUT, file) });

          const clipped = await page.evaluate(() => {
            const dlg = document.querySelector('[aria-labelledby="helper-panel-title"]');
            if (!dlg) return ['no dialog'];
            const box = dlg.getBoundingClientRect();
            const out: string[] = [];
            for (const el of Array.from(dlg.querySelectorAll('*'))) {
              const r = el.getBoundingClientRect();
              if (r.width === 0 || r.height === 0) continue;
              if (el.closest('.sr-only')) continue;
              let scroller = false;
              for (let p = el.parentElement; p && p !== dlg; p = p.parentElement) {
                const o = getComputedStyle(p).overflowX;
                if ((o === 'auto' || o === 'scroll' || o === 'hidden') && p.scrollWidth > p.clientWidth) scroller = true;
              }
              if (scroller) continue;
              if (r.right > box.right + 1 || r.left < box.left - 1) {
                out.push(`${el.tagName.toLowerCase()}.${String(el.className).split(/\s+/).slice(0, 3).join('.')} [${Math.round(r.left)}..${Math.round(r.right)}]`);
              }
            }
            return out.slice(0, 8);
          });

          await page.addScriptTag({ path: AXE_PATH });
          const axe = await page.evaluate(async () => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const res = await (window as any).axe.run('[aria-labelledby="helper-panel-title"]', { resultTypes: ['violations'] });
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            return res.violations.map((v: any) => ({
              id: v.id,
              impact: v.impact,
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              detail: v.nodes.slice(0, 6).map((n: any) => `${String(n.target.join(' ')).slice(0, 80)} :: ${(n.any[0]?.message || n.failureSummary || '').slice(0, 140)}`),
            }));
          });

          appendFileSync(
            join(OUT, 'helper-report.jsonl'),
            JSON.stringify({ scenario: sc.name, theme, width, file, clipped, errors, axe }) + '\n',
          );
        });
      }
    });
  }
}
